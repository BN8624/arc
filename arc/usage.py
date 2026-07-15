# ARC provider 사용량 원장을 SQLite에 기록하고 토큰 admission을 판정한다.
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 2
PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_USAGE_DB = Path(".arc/usage.sqlite3")

CONTEXT_TOKEN_LIMIT = 256_000
COMBINED_TOKEN_CEILING = 240_000
INPUT_TOKEN_WARNING = 180_000
INPUT_TOKEN_HARD_LIMIT = 200_000
PROSE_OUTPUT_LIMIT = 32_768
JSON_OUTPUT_LIMIT = 8_192


class UsageLedgerError(RuntimeError):
    """Usage ledger operation failed."""


class UsageEventCollision(UsageLedgerError):
    """Usage event identity collided with an existing row."""


def _is_sqlite_lock_error(error: sqlite3.OperationalError) -> bool:
    return "locked" in str(error).lower() or "busy" in str(error).lower()


class TokenAdmissionError(RuntimeError):
    """Provider generation was blocked before dispatch."""


@dataclass(frozen=True)
class Admission:
    allowed: bool
    gate_decision: str
    reason_code: str | None
    warning_code: str | None = None


@dataclass(frozen=True)
class UsageNumbers:
    prompt_tokens: int | None = None
    candidate_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None

    @property
    def combined_output_tokens(self) -> int | None:
        if self.candidate_tokens is None or self.reasoning_tokens is None:
            return None
        return self.candidate_tokens + self.reasoning_tokens

    @property
    def metadata_status(self) -> str:
        return "KNOWN" if any(value is not None for value in (self.prompt_tokens, self.candidate_tokens, self.reasoning_tokens, self.total_tokens, self.cached_tokens)) else "MISSING"


def usage_db_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("ARC_USAGE_DB", str(DEFAULT_USAGE_DB)))


def pacific_fields(dispatch_utc: datetime | None = None) -> tuple[str, str, str]:
    utc = dispatch_utc or datetime.now(timezone.utc)
    if utc.tzinfo is None:
        utc = utc.replace(tzinfo=timezone.utc)
    utc = utc.astimezone(timezone.utc)
    pacific = utc.astimezone(PACIFIC)
    return utc.isoformat(), pacific.isoformat(), pacific.date().isoformat()


def parse_usage_metadata(value: object | None) -> UsageNumbers:
    if value is None:
        return UsageNumbers()
    return UsageNumbers(
        prompt_tokens=_get_int(value, "prompt_token_count", "promptTokenCount"),
        candidate_tokens=_get_int(value, "candidates_token_count", "candidatesTokenCount"),
        reasoning_tokens=_get_int(value, "thoughts_token_count", "thoughtsTokenCount"),
        total_tokens=_get_int(value, "total_token_count", "totalTokenCount"),
        cached_tokens=_get_int(value, "cached_content_token_count", "cachedContentTokenCount"),
    )


def _get_int(value: object, *names: str) -> int | None:
    for name in names:
        item = getattr(value, name, None)
        if item is None and isinstance(value, dict):
            item = value.get(name)
        if isinstance(item, int) and not isinstance(item, bool):
            return item
    return None


def decide_admission(input_tokens: int | None, max_output_tokens: int) -> Admission:
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool) or input_tokens < 0:
        return Admission(False, "BLOCK", "TOKEN_COUNT_UNAVAILABLE")
    if max_output_tokens > PROSE_OUTPUT_LIMIT:
        return Admission(False, "BLOCK", "OUTPUT_TOKEN_LIMIT_EXCEEDED")
    if input_tokens >= INPUT_TOKEN_HARD_LIMIT:
        return Admission(False, "BLOCK", "INPUT_TOKEN_HARD_LIMIT")
    if input_tokens + max_output_tokens > COMBINED_TOKEN_CEILING:
        return Admission(False, "BLOCK", "COMBINED_TOKEN_BUDGET_EXCEEDED")
    warning = "INPUT_TOKEN_WARNING" if input_tokens >= INPUT_TOKEN_WARNING else None
    return Admission(True, "WARN" if warning else "ALLOW", None, warning)


class UsageLedger:
    def __init__(self, path: Path | None = None, now: Callable[[], datetime] | None = None):
        self.path = path or usage_db_path()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._ready = False

    def ensure(self) -> None:
        for attempt in range(10):
            try:
                self._ensure_once()
                return
            except sqlite3.OperationalError as error:
                if not _is_sqlite_lock_error(error) or attempt == 9:
                    raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error
                time.sleep(min(0.05 * (2 ** attempt), 0.5))

    def _ensure_once(self) -> None:
        with self._lock:
            if self._ready:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if os.name == "posix":
                    self.path.parent.chmod(0o700)
                with self._connect(timeout=0.25, busy_timeout_ms=250) as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=250")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS usage_events (
                            event_id TEXT PRIMARY KEY,
                            usage_run_id TEXT,
                            usage_attempt_id TEXT,
                            request_group_id TEXT,
                            run_identity TEXT,
                            output_identity TEXT,
                            call_id TEXT,
                            lease_sequence INTEGER,
                            key_slot_id TEXT NOT NULL,
                            request_kind TEXT NOT NULL,
                            model TEXT,
                            episode TEXT,
                            stage TEXT,
                            role TEXT,
                            attempt INTEGER,
                            utc_dispatch_ts TEXT NOT NULL,
                            pacific_dispatch_ts TEXT NOT NULL,
                            pacific_date TEXT NOT NULL,
                            gate_input_tokens INTEGER,
                            actual_input_tokens INTEGER,
                            configured_max_output_tokens INTEGER,
                            candidate_tokens INTEGER,
                            reasoning_tokens INTEGER,
                            combined_output_tokens INTEGER,
                            provider_total_tokens INTEGER,
                            cached_tokens INTEGER,
                            gate_decision TEXT,
                            gate_reason_code TEXT,
                            provider_dispatched INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            usage_metadata_status TEXT NOT NULL,
                            error_code TEXT,
                            legacy_imported INTEGER NOT NULL DEFAULT 0,
                            token_provenance TEXT NOT NULL,
                            repair_provenance TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS quota_reservations (
                            reservation_id TEXT PRIMARY KEY,
                            bucket_id TEXT NOT NULL,
                            input_tokens INTEGER NOT NULL,
                            created_at REAL NOT NULL,
                            pacific_date TEXT NOT NULL,
                            state TEXT NOT NULL,
                            dispatched INTEGER NOT NULL DEFAULT 0
                        )
                        """
                    )
                    conn.execute("CREATE INDEX IF NOT EXISTS quota_reservations_bucket_time ON quota_reservations(bucket_id, created_at)")
                    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
                    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
                    version = int(conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0])
                    if version > SCHEMA_VERSION:
                        raise UsageLedgerError("UNSUPPORTED_USAGE_SCHEMA_VERSION")
                    if version < 2:
                        conn.execute("SAVEPOINT usage_v1_to_v2")
                        try:
                            self._migrate_v1_to_v2(conn)
                            conn.execute("RELEASE SAVEPOINT usage_v1_to_v2")
                        except Exception:
                            conn.execute("ROLLBACK TO SAVEPOINT usage_v1_to_v2")
                            conn.execute("RELEASE SAVEPOINT usage_v1_to_v2")
                            raise
                self._ready = True
            except Exception as error:
                if isinstance(error, UsageLedgerError):
                    raise
                if isinstance(error, sqlite3.OperationalError) and _is_sqlite_lock_error(error):
                    raise
                raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_events)").fetchall()}
        additions = {
            "usage_run_id": "TEXT",
            "usage_attempt_id": "TEXT",
            "request_group_id": "TEXT",
            "repair_provenance": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE usage_events ADD COLUMN {name} {definition}")
        conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))

    def schema_version(self) -> int:
        self.ensure()
        with self._connect() as conn:
            return int(conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0])

    def prepare_count_tokens(self, *, event_id: str, key_slot_id: str, model: str, call: dict, output_identity: str | None = None, usage_run_id: str | None = None, usage_attempt_id: str | None = None, request_group_id: str | None = None) -> None:
        self.insert_event(event_id=event_id, request_kind="count_tokens", key_slot_id=key_slot_id, model=model, call=call, output_identity=output_identity, usage_run_id=usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id, gate_decision="ALLOW", provider_dispatched=False, status="PREPARED", usage_metadata_status="NOT_APPLICABLE", token_provenance="measured")

    def finish_count_tokens(
        self,
        *,
        event_id: str,
        input_tokens: int | None,
        dispatched: bool,
        error_code: str | None = None,
        usage_run_id: str | None = None,
        usage_attempt_id: str | None = None,
        request_group_id: str | None = None,
    ) -> None:
        status = "SUCCEEDED" if error_code is None else "FAILED"
        self.update_event(
            event_id,
            expected_statuses={"PREPARED", "DISPATCHED"},
            usage_run_id=usage_run_id,
            usage_attempt_id=usage_attempt_id,
            request_group_id=request_group_id,
            request_kind="count_tokens",
            status=status,
            provider_dispatched=dispatched,
            actual_input_tokens=input_tokens,
            usage_metadata_status="KNOWN" if input_tokens is not None else "MISSING",
            error_code=error_code,
        )

    def insert_generation(self, *, event_id: str, key_slot_id: str, model: str, call: dict, input_tokens: int | None, max_output_tokens: int, admission: Admission, output_identity: str | None = None, usage_run_id: str | None = None, usage_attempt_id: str | None = None, request_group_id: str | None = None) -> None:
        status = "PREPARED" if admission.allowed else "BLOCKED"
        self.insert_event(event_id=event_id, request_kind="generate_content", key_slot_id=key_slot_id, model=model, call=call, output_identity=output_identity, usage_run_id=usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id, gate_input_tokens=input_tokens, configured_max_output_tokens=max_output_tokens, gate_decision=admission.gate_decision, gate_reason_code=admission.reason_code or admission.warning_code, provider_dispatched=False, status=status, usage_metadata_status="PENDING" if admission.allowed else "NOT_APPLICABLE", token_provenance="provider")

    def mark_dispatched(
        self,
        event_id: str,
        *,
        usage_run_id: str | None = None,
        usage_attempt_id: str | None = None,
        request_group_id: str | None = None,
        request_kind: str | None = None,
    ) -> None:
        self.update_event(
            event_id,
            expected_statuses={"PREPARED"},
            usage_run_id=usage_run_id,
            usage_attempt_id=usage_attempt_id,
            request_group_id=request_group_id,
            request_kind=request_kind,
            status="DISPATCHED",
            provider_dispatched=True,
        )

    def finish_generation(
        self,
        *,
        event_id: str,
        usage: UsageNumbers,
        status: str,
        error_code: str | None = None,
        usage_run_id: str | None = None,
        usage_attempt_id: str | None = None,
        request_group_id: str | None = None,
    ) -> None:
        final_status = "USAGE_UNKNOWN" if status == "SUCCEEDED" and usage.metadata_status == "MISSING" else status
        self.update_event(
            event_id,
            expected_statuses={"DISPATCHED"},
            usage_run_id=usage_run_id,
            usage_attempt_id=usage_attempt_id,
            request_group_id=request_group_id,
            request_kind="generate_content",
            status=final_status,
            actual_input_tokens=usage.prompt_tokens,
            candidate_tokens=usage.candidate_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            combined_output_tokens=usage.combined_output_tokens,
            provider_total_tokens=usage.total_tokens,
            cached_tokens=usage.cached_tokens,
            usage_metadata_status=usage.metadata_status,
            error_code=error_code,
        )

    def cancel_generation(self, *, event_id: str, error_code: str, usage_run_id: str | None = None, usage_attempt_id: str | None = None, request_group_id: str | None = None) -> None:
        self.update_event(
            event_id,
            expected_statuses={"PREPARED"},
            usage_run_id=usage_run_id,
            usage_attempt_id=usage_attempt_id,
            request_group_id=request_group_id,
            request_kind="generate_content",
            status="FAILED",
            provider_dispatched=False,
            usage_metadata_status="NOT_APPLICABLE",
            error_code=error_code,
        )

    def quota_reserve(self, *, reservation_id: str, bucket_id: str, input_tokens: int, created_at: float, pacific_date: str, limits: dict[str, int]) -> dict:
        self.ensure()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("DELETE FROM quota_reservations WHERE created_at <= ?", (created_at - 3 * 86400,))
                rolling = conn.execute("SELECT created_at, input_tokens FROM quota_reservations WHERE bucket_id=? AND state != 'RELEASED' AND created_at > ?", (bucket_id, created_at - 60)).fetchall()
                daily = conn.execute("SELECT COUNT(*) FROM quota_reservations WHERE bucket_id=? AND state != 'RELEASED' AND pacific_date=?", (bucket_id, pacific_date)).fetchone()[0]
                if len(rolling) + 1 > limits["safety_rpm"]:
                    conn.rollback()
                    return {"ok": False, "reason": "RPM_WINDOW_FULL", "release_at": min(row["created_at"] + 60 for row in rolling)}
                if sum(row["input_tokens"] for row in rolling) + input_tokens > limits["safety_input_tpm"]:
                    conn.rollback()
                    return {"ok": False, "reason": "INPUT_TPM_WINDOW_FULL", "release_at": min(row["created_at"] + 60 for row in rolling)}
                if daily + 1 > limits["safety_rpd"]:
                    conn.rollback()
                    return {"ok": False, "reason": "RPD_DAY_FULL", "release_at": None}
                conn.execute("INSERT INTO quota_reservations(reservation_id,bucket_id,input_tokens,created_at,pacific_date,state,dispatched) VALUES(?,?,?,?,?,?,0)", (reservation_id, bucket_id, input_tokens, created_at, pacific_date, "RESERVED"))
                conn.commit()
                return {"ok": True, "reservation_id": reservation_id}
        except sqlite3.IntegrityError as error:
            raise UsageLedgerError("USAGE_EVENT_ID_COLLISION") from error
        except Exception as error:
            if isinstance(error, UsageLedgerError):
                raise
            raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def quota_transition(self, reservation_id: str, *, state: str, dispatched: bool | None = None) -> None:
        self.ensure()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                values = [state]
                columns = "state=?"
                if dispatched is not None:
                    columns += ", dispatched=?"
                    values.append(int(dispatched))
                values.append(reservation_id)
                expected = "RESERVED" if state == "DISPATCHED" else "DISPATCHED" if state in {"SUCCEEDED", "FAILED"} else "RESERVED"
                cursor = conn.execute(f"UPDATE quota_reservations SET {columns} WHERE reservation_id=? AND state=?", (*values, expected))
                if cursor.rowcount != 1:
                    raise UsageLedgerError("USAGE_EVENT_STATE_CONFLICT")
                conn.commit()
        except UsageLedgerError:
            raise
        except Exception as error:
            raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def quota_release(self, reservation_id: str) -> None:
        self.ensure()
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE quota_reservations SET state='RELEASED' WHERE reservation_id=? AND state='RESERVED'", (reservation_id,))
                conn.commit()
        except Exception as error:
            raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def quota_snapshot(self, bucket_ids: list[str], *, now: float, limits: dict[str, int], pacific_date: str) -> dict:
        self.ensure()
        with self._connect() as conn:
            result = {}
            for bucket_id in sorted(set(bucket_ids)):
                rolling = conn.execute("SELECT input_tokens, state FROM quota_reservations WHERE bucket_id=? AND state != 'RELEASED' AND created_at > ?", (bucket_id, now - 60)).fetchall()
                daily = conn.execute("SELECT COUNT(*) FROM quota_reservations WHERE bucket_id=? AND state != 'RELEASED' AND pacific_date=?", (bucket_id, pacific_date)).fetchone()[0]
                result[bucket_id] = {"bucket_id": bucket_id, "rolling_request_count": len(rolling), "rolling_reserved_input_tokens": sum(row["input_tokens"] for row in rolling), "daily_request_count": daily, "remaining_rpm_headroom": max(0, limits["safety_rpm"] - len(rolling)), "remaining_tpm_headroom": max(0, limits["safety_input_tpm"] - sum(row["input_tokens"] for row in rolling)), "remaining_rpd_headroom": max(0, limits["safety_rpd"] - daily), "in_flight_reservations": sum(row["state"] == "RESERVED" for row in rolling)}
            return result

    def insert_event(self, **values: Any) -> None:
        self.ensure()
        dispatch_utc = values.pop("dispatch_utc", None)
        now = self.now()
        utc, pacific_ts, pacific_date = pacific_fields(dispatch_utc or now)
        call = values.pop("call", {}) or {}
        row = {
            "usage_run_id": values.pop("usage_run_id", None),
            "usage_attempt_id": values.pop("usage_attempt_id", None),
            "request_group_id": values.pop("request_group_id", None),
            "run_identity": call.get("scope_id"),
            "output_identity": values.pop("output_identity", None),
            "call_id": call.get("call_id"),
            "lease_sequence": call.get("lease_sequence"),
            "episode": _episode_from_scope(call.get("scope_id")),
            "stage": call.get("stage"),
            "role": call.get("role"),
            "attempt": call.get("attempt"),
            "utc_dispatch_ts": utc,
            "pacific_dispatch_ts": pacific_ts,
            "pacific_date": pacific_date,
            "gate_input_tokens": None,
            "actual_input_tokens": None,
            "configured_max_output_tokens": None,
            "candidate_tokens": None,
            "reasoning_tokens": None,
            "combined_output_tokens": None,
            "provider_total_tokens": None,
            "cached_tokens": None,
            "gate_decision": None,
            "gate_reason_code": None,
            "provider_dispatched": 0,
            "status": "PREPARED",
            "usage_metadata_status": "PENDING",
            "error_code": None,
            "legacy_imported": 0,
            "token_provenance": "provider",
            "repair_provenance": None,
            "created_at": utc,
            "updated_at": now.isoformat(),
            **values,
        }
        row["provider_dispatched"] = int(bool(row["provider_dispatched"]))
        self._write_row(row)

    def update_event(
        self,
        event_id: str,
        *,
        expected_statuses: set[str] | None = None,
        usage_run_id: str | None = None,
        usage_attempt_id: str | None = None,
        request_group_id: str | None = None,
        request_kind: str | None = None,
        **values: Any,
    ) -> None:
        self.ensure()
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "provider_dispatched" in values:
            values["provider_dispatched"] = int(bool(values["provider_dispatched"]))
        columns = ", ".join(f"{key}=?" for key in values)
        terminal = {"SUCCEEDED", "FAILED", "USAGE_UNKNOWN", "BLOCKED"}
        expected = set(expected_statuses or {"PREPARED", "DISPATCHED"})
        target_status = values.get("status")
        transition_sql = "1=1"
        transition_params: list[object] = []
        if target_status == "DISPATCHED":
            transition_sql = "status='PREPARED'"
        elif target_status == "SUCCEEDED":
            transition_sql = "(status='DISPATCHED' OR (status='PREPARED' AND request_kind='count_tokens' AND ?=0))"
            transition_params.append(values.get("provider_dispatched", 0))
        elif target_status == "FAILED":
            transition_sql = "(status='DISPATCHED' OR (status='PREPARED' AND request_kind IN ('count_tokens','generate_content')))"
        elif target_status == "USAGE_UNKNOWN":
            transition_sql = "status='DISPATCHED' AND request_kind='generate_content'"
        elif target_status is not None:
            raise UsageLedgerError("USAGE_EVENT_STATE_CONFLICT")

        ownership_values = (usage_run_id, usage_attempt_id, request_group_id)
        ownership_supplied = any(value is not None for value in ownership_values)
        if ownership_supplied and not all(isinstance(value, str) and value for value in ownership_values):
            raise UsageLedgerError("USAGE_EVENT_ID_COLLISION")
        ownership_sql = (
            "usage_run_id=? AND usage_attempt_id=? AND request_group_id=?"
            if ownership_supplied
            else "usage_run_id IS NULL AND usage_attempt_id IS NULL AND request_group_id IS NULL"
        )
        ownership_params: list[object] = list(ownership_values) if ownership_supplied else []
        if request_kind is not None:
            ownership_sql += " AND request_kind=?"
            ownership_params.append(request_kind)
        expected_placeholders = ",".join("?" for _ in expected)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""
                    UPDATE usage_events
                    SET {columns}
                    WHERE event_id=?
                      AND status IN ({expected_placeholders})
                      AND status NOT IN ('SUCCEEDED','FAILED','USAGE_UNKNOWN','BLOCKED')
                      AND {transition_sql}
                      AND {ownership_sql}
                    """,
                    (*values.values(), event_id, *sorted(expected), *transition_params, *ownership_params),
                )
                if cursor.rowcount == 1:
                    return
                row = conn.execute(
                    "SELECT status, usage_run_id, usage_attempt_id, request_group_id, request_kind FROM usage_events WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise UsageLedgerError("USAGE_EVENT_NOT_FOUND")
                if row["status"] in terminal:
                    raise UsageLedgerError("USAGE_EVENT_TERMINAL")
                row_ownership = (row["usage_run_id"], row["usage_attempt_id"], row["request_group_id"])
                if row_ownership != ownership_values or (request_kind is not None and row["request_kind"] != request_kind):
                    raise UsageLedgerError("USAGE_EVENT_ID_COLLISION")
                raise UsageLedgerError("USAGE_EVENT_STATE_CONFLICT")
        except Exception as error:
            if isinstance(error, UsageLedgerError):
                raise
            raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def status(self, date: str | None = None) -> dict:
        self.ensure()
        target_date = date or pacific_fields(self.now())[2]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT key_slot_id,
                       SUM(CASE WHEN provider_dispatched=1 THEN 1 ELSE 0 END) AS provider_requests,
                       SUM(CASE WHEN request_kind='generate_content' AND provider_dispatched=1 THEN 1 ELSE 0 END) AS generation_requests,
                       SUM(CASE WHEN request_kind='count_tokens' AND provider_dispatched=1 THEN 1 ELSE 0 END) AS count_token_requests,
                       SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS success_count,
                       SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN status='USAGE_UNKNOWN' OR usage_metadata_status='MISSING' OR combined_output_tokens IS NULL THEN 1 ELSE 0 END) AS usage_unknown_count,
                       SUM(CASE WHEN request_kind='generate_content' THEN actual_input_tokens ELSE NULL END) AS actual_input_tokens,
                       SUM(CASE WHEN request_kind='generate_content' THEN candidate_tokens ELSE NULL END) AS candidate_tokens,
                       SUM(CASE WHEN request_kind='generate_content' THEN reasoning_tokens ELSE NULL END) AS reasoning_tokens,
                       SUM(CASE WHEN request_kind='generate_content' THEN combined_output_tokens ELSE NULL END) AS combined_output_tokens,
                       SUM(CASE WHEN request_kind='generate_content' THEN provider_total_tokens ELSE NULL END) AS provider_total_tokens,
                       SUM(CASE WHEN status='BLOCKED' THEN 1 ELSE 0 END) AS blocked_count
                FROM usage_events
                WHERE pacific_date=?
                GROUP BY key_slot_id
                ORDER BY key_slot_id
                """,
                (target_date,),
            ).fetchall()
        keys = [_row_dict(row) for row in rows]
        totals = _sum_key_rows(keys)
        warnings = self._warnings_for_date(target_date)
        return {"schema_version": self.schema_version(), "pacific_timezone": "America/Los_Angeles", "pacific_date": target_date, "totals": totals, "keys": keys, "warnings": warnings}

    def import_pilot(self, output: Path) -> dict:
        telemetry_path = output / "pilot_live_calls.json"
        data = json.loads(telemetry_path.read_text(encoding="utf-8"))
        counts = {"imported": 0, "skipped": 0, "derived_reasoning": 0, "usage_unknown": 0, "key_slot_unknown": 0}
        for call in data.get("calls", []):
            event_id = f"legacy:{call.get('call_id') or call.get('desk_id')}:{call.get('lease_sequence')}"
            dispatch_utc = _legacy_dispatch_time(call)
            key_slot = call.get("key_slot") or "UNKNOWN_SLOT"
            key_slot_unknown = key_slot == "UNKNOWN_SLOT"
            prompt_tokens = _clean_int(call.get("prompt_tokens"))
            candidate_tokens = _clean_int(call.get("output_tokens"))
            total_tokens = _clean_int(call.get("total_tokens"))
            reasoning_tokens = None
            provenance = "legacy"
            derived_reasoning = False
            if prompt_tokens is not None and candidate_tokens is not None and total_tokens is not None:
                derived = total_tokens - prompt_tokens - candidate_tokens
                if derived >= 0:
                    reasoning_tokens = derived
                    provenance = "legacy_derived"
                    derived_reasoning = True
            combined = candidate_tokens + reasoning_tokens if candidate_tokens is not None and reasoning_tokens is not None else None
            usage_status = "KNOWN" if prompt_tokens is not None or candidate_tokens is not None or total_tokens is not None or reasoning_tokens is not None else "MISSING"
            usage_unknown = usage_status == "MISSING" or combined is None
            try:
                self.insert_event(
                    event_id=event_id,
                    dispatch_utc=dispatch_utc,
                    request_kind="generate_content",
                    key_slot_id=key_slot,
                    model=data.get("model"),
                    call=call,
                    gate_input_tokens=prompt_tokens,
                    actual_input_tokens=prompt_tokens,
                    configured_max_output_tokens=None,
                    candidate_tokens=candidate_tokens,
                    reasoning_tokens=reasoning_tokens,
                    combined_output_tokens=combined,
                    provider_total_tokens=total_tokens,
                    gate_decision="LEGACY_IMPORTED",
                    provider_dispatched=True,
                    status="SUCCEEDED" if call.get("status") == "PASS" else "FAILED",
                    usage_metadata_status=usage_status,
                    error_code=call.get("error_class"),
                    legacy_imported=True,
                    token_provenance=provenance,
                )
                counts["imported"] += 1
                if derived_reasoning:
                    counts["derived_reasoning"] += 1
                if usage_unknown:
                    counts["usage_unknown"] += 1
                if key_slot_unknown:
                    counts["key_slot_unknown"] += 1
            except UsageEventCollision:
                self._refresh_legacy_dispatch_time(event_id, dispatch_utc, call)
                counts["skipped"] += 1
            except UsageLedgerError:
                raise
            except Exception:
                counts["skipped"] += 1
        return counts

    def _refresh_legacy_dispatch_time(self, event_id: str, dispatch_utc: datetime | None, call: dict) -> None:
        if dispatch_utc is None:
            return
        expected_event_id = f"legacy:{call.get('call_id') or call.get('desk_id')}:{call.get('lease_sequence')}"
        if event_id != expected_event_id:
            raise UsageEventCollision("USAGE_EVENT_ID_COLLISION")
        utc, pacific_ts, pacific_date = pacific_fields(dispatch_utc)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE usage_events
                    SET utc_dispatch_ts=?, pacific_dispatch_ts=?, pacific_date=?, updated_at=?
                    WHERE event_id=?
                      AND legacy_imported=1
                      AND usage_run_id IS NULL
                      AND usage_attempt_id IS NULL
                      AND request_group_id IS NULL
                      AND request_kind='generate_content'
                      AND call_id IS ?
                      AND lease_sequence IS ?
                    """,
                    (utc, pacific_ts, pacific_date, datetime.now(timezone.utc).isoformat(), event_id, call.get("call_id"), call.get("lease_sequence")),
                )
                if cursor.rowcount != 1:
                    raise UsageEventCollision("USAGE_EVENT_ID_COLLISION")
        except Exception as error:
            if isinstance(error, UsageLedgerError):
                raise
            raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def _warnings_for_date(self, date: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT key_slot_id, call_id, gate_reason_code FROM usage_events WHERE pacific_date=? AND gate_reason_code='INPUT_TOKEN_WARNING' ORDER BY key_slot_id, call_id", (date,)).fetchall()
        return [_row_dict(row) for row in rows]

    def _write_row(self, row: dict) -> None:
        try:
            with self._connect() as conn:
                columns = list(row)
                placeholders = ",".join("?" for _ in columns)
                conn.execute(f"INSERT INTO usage_events ({','.join(columns)}) VALUES ({placeholders})", tuple(row[column] for column in columns))
        except sqlite3.IntegrityError:
            raise UsageEventCollision("USAGE_EVENT_ID_COLLISION")
        except Exception as error:
            raise UsageLedgerError("USAGE_DB_UNAVAILABLE") from error

    def _connect(self, *, timeout: float = 5.0, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        return conn


def _database_fingerprint(conn: sqlite3.Connection, path: Path) -> dict:
    conn.row_factory = sqlite3.Row
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise UsageLedgerError("USAGE_DB_INTEGRITY_FAILED")
    tables = []
    for table in ("schema_version", "usage_events"):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise UsageLedgerError("USAGE_DB_SCHEMA_INVALID")
        columns = [
            {"name": row["name"], "type": row["type"], "notnull": row["notnull"], "default": row["dflt_value"], "pk": row["pk"]}
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        tables.append({"name": table, "columns": columns})
    version_rows = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY rowid").fetchall()]
    if len(version_rows) != 1 or not isinstance(version_rows[0], int):
        raise UsageLedgerError("USAGE_DB_SCHEMA_INVALID")
    usage_columns = [item["name"] for item in tables[1]["columns"]]
    rows = [list(row) for row in conn.execute(f"SELECT {','.join(usage_columns)} FROM usage_events ORDER BY event_id").fetchall()]
    payload = json.dumps({"schema_version_rows": version_rows, "core_schema": tables, "usage_rows": rows}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "path": str(path.resolve()),
        "schema_version": version_rows[0],
        "row_count": len(rows),
        "core_schema": tables,
        "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "integrity_check": integrity,
    }


def _fingerprints_match(left: dict, right: dict) -> bool:
    keys = ("schema_version", "row_count", "core_schema", "content_sha256")
    return all(left[key] == right[key] for key in keys)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def backup_usage_db(source: Path | None = None, destination: Path | None = None) -> dict:
    source = Path(source or usage_db_path())
    if not source.exists():
        raise UsageLedgerError("USAGE_DB_NOT_FOUND")
    if not source.is_file():
        raise UsageLedgerError("USAGE_DB_NOT_FILE")
    if destination is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = source.with_name(f"{source.stem}.backup-{stamp}{source.suffix}")
    destination = Path(destination)
    source_resolved = source.resolve()
    destination_resolved = destination.resolve()
    if source_resolved == destination_resolved or (destination.exists() and os.path.samefile(source, destination)):
        raise UsageLedgerError("USAGE_BACKUP_SOURCE_DESTINATION_SAME")
    if destination.exists():
        raise UsageLedgerError("USAGE_BACKUP_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _read_only_connection(source_resolved) as src, sqlite3.connect(destination) as dst:
            dst.row_factory = sqlite3.Row
            src.backup(dst)
            source_fingerprint = _database_fingerprint(src, source_resolved)
            backup_fingerprint = _database_fingerprint(dst, destination_resolved)
        if not _fingerprints_match(source_fingerprint, backup_fingerprint):
            raise UsageLedgerError("USAGE_BACKUP_FINGERPRINT_MISMATCH")
    except Exception as error:
        if isinstance(error, UsageLedgerError):
            raise
        raise UsageLedgerError("USAGE_BACKUP_FAILED") from error
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "source": str(source),
        "backup": str(destination),
        "backup_created_at": created_at,
        "integrity_check": backup_fingerprint["integrity_check"],
        "source_fingerprint": source_fingerprint,
        "backup_fingerprint": backup_fingerprint,
    }


def _known_collision_repair(provenance: object) -> bool:
    if not isinstance(provenance, str):
        return False
    try:
        value = json.loads(provenance)
    except (TypeError, ValueError):
        return False
    return value.get("repair_type") == "issue40_preflight_collision" or value.get("repair") == "issue40_preflight_collision"


def _count_candidate_issue(row: sqlite3.Row) -> str | None:
    slot = row["key_slot_id"]
    if row["repair_provenance"] is not None:
        return "REPAIR_PROVENANCE_CONFLICT"
    if row["stage"] != "preflight":
        return "COUNT_STAGE_MISMATCH"
    if slot not in {f"K{index:02d}" for index in range(1, 12)}:
        return "COUNT_KEY_SLOT_INVALID"
    if row["call_id"] != "L000-A001" or row["lease_sequence"] is not None or row["role"] != slot or row["attempt"] != 1:
        return "COUNT_CALL_IDENTITY_MISMATCH"
    if row["event_id"] != f"preflight:{slot}:L000-A001:count_tokens:None":
        return "COUNT_EVENT_IDENTITY_MISMATCH"
    if any(row[name] is not None for name in ("usage_run_id", "usage_attempt_id", "request_group_id")) or row["legacy_imported"] != 0:
        return "COUNT_NOT_PRE_V2"
    if not isinstance(row["model"], str) or not row["model"]:
        return "COUNT_MODEL_MISSING"
    if row["status"] != "FAILED" or row["actual_input_tokens"] is not None or row["usage_metadata_status"] != "MISSING" or row["error_code"] != "TOKEN_COUNT_UNAVAILABLE":
        return "COUNT_FAILURE_SHAPE_MISMATCH"
    if row["provider_dispatched"] != 1:
        return "COUNT_PROVIDER_NOT_DISPATCHED"
    if row["token_provenance"] != "measured":
        return "COUNT_TOKEN_PROVENANCE_MISMATCH"
    return None


def _timestamp_not_before(value: object, baseline: object) -> bool:
    if not isinstance(value, str) or not isinstance(baseline, str):
        return False
    try:
        return datetime.fromisoformat(value) >= datetime.fromisoformat(baseline)
    except ValueError:
        return False


def _matching_companions(count_row: sqlite3.Row, generation_rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    matches = []
    for row in generation_rows:
        gate_tokens = row["gate_input_tokens"]
        if not isinstance(gate_tokens, int) or isinstance(gate_tokens, bool) or gate_tokens < 0:
            continue
        actual_tokens = row["actual_input_tokens"]
        if actual_tokens is not None and actual_tokens != gate_tokens:
            continue
        if any(row[name] is not None for name in ("usage_run_id", "usage_attempt_id", "request_group_id")) or row["legacy_imported"] != 0:
            continue
        if not (
            row["stage"] == "preflight"
            and row["status"] == "SUCCEEDED"
            and row["provider_dispatched"] == 1
            and row["error_code"] is None
            and row["key_slot_id"] == count_row["key_slot_id"]
            and row["call_id"] == count_row["call_id"]
            and row["lease_sequence"] == count_row["lease_sequence"]
            and row["role"] == count_row["role"]
            and row["attempt"] == count_row["attempt"]
            and row["model"] == count_row["model"]
            and row["run_identity"] == count_row["run_identity"]
            and row["output_identity"] == count_row["output_identity"]
            and _timestamp_not_before(row["utc_dispatch_ts"], count_row["utc_dispatch_ts"])
            and _timestamp_not_before(row["created_at"], count_row["created_at"])
        ):
            continue
        matches.append(row)
    return matches


def _repair_candidates(conn: sqlite3.Connection) -> tuple[list[dict], list[dict], int]:
    count_rows = conn.execute(
        "SELECT * FROM usage_events WHERE request_kind='count_tokens' AND event_id LIKE 'preflight:%:count_tokens:%' ORDER BY key_slot_id, event_id"
    ).fetchall()
    generation_rows = conn.execute("SELECT * FROM usage_events WHERE request_kind='generate_content'").fetchall()
    candidates: list[dict] = []
    unresolved: list[dict] = []
    skipped = 0
    for count_row in count_rows:
        if _known_collision_repair(count_row["repair_provenance"]):
            skipped += 1
            continue
        issue = _count_candidate_issue(count_row)
        if issue is not None:
            unresolved.append({"event_id": count_row["event_id"], "key_slot_id": count_row["key_slot_id"], "reason_code": issue})
            continue
        companions = _matching_companions(count_row, generation_rows)
        if len(companions) != 1:
            reason = "COMPANION_GENERATION_MISSING_OR_MISMATCH" if not companions else "COMPANION_GENERATION_AMBIGUOUS"
            unresolved.append({"event_id": count_row["event_id"], "key_slot_id": count_row["key_slot_id"], "reason_code": reason})
            continue
        generation = companions[0]
        candidates.append(
            {
                "event_id": count_row["event_id"],
                "key_slot_id": count_row["key_slot_id"],
                "repair_input_tokens": generation["gate_input_tokens"],
                "companion_event_id": generation["event_id"],
                "reason_code": "COMPANION_GENERATION_GATE_INPUT",
                "_expected_updated_at": count_row["updated_at"],
            }
        )
    return candidates, unresolved, skipped


def _repair_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS row_count,
               SUM(CASE WHEN provider_dispatched=1 THEN 1 ELSE 0 END) AS provider_requests,
               SUM(CASE WHEN status='SUCCEEDED' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN status='FAILED' THEN 1 ELSE 0 END) AS failed_count,
               SUM(actual_input_tokens) AS recorded_input_tokens
        FROM usage_events
        """
    ).fetchone()
    return {key: row[key] if row[key] is not None else 0 for key in row.keys()}


def _row_documents(conn: sqlite3.Connection) -> dict[str, dict]:
    return {row["event_id"]: _row_dict(row) for row in conn.execute("SELECT * FROM usage_events ORDER BY event_id").fetchall()}


def _backup_matches_completed_repair(current: sqlite3.Connection, backup: sqlite3.Connection) -> bool:
    backup_candidates, backup_unresolved, _ = _repair_candidates(backup)
    if not backup_candidates or backup_unresolved:
        return False
    current_rows = _row_documents(current)
    backup_rows = _row_documents(backup)
    if current_rows.keys() != backup_rows.keys():
        return False
    candidates = {item["event_id"]: item for item in backup_candidates}
    mutable = {"status", "actual_input_tokens", "usage_metadata_status", "error_code", "repair_provenance", "updated_at"}
    for event_id, backup_row in backup_rows.items():
        current_row = current_rows[event_id]
        item = candidates.get(event_id)
        if item is None:
            if current_row != backup_row:
                return False
            continue
        try:
            provenance = json.loads(current_row["repair_provenance"])
        except (TypeError, ValueError):
            return False
        if not (
            provenance.get("repair_type") == "issue40_preflight_collision"
            and provenance.get("source_evidence") == "companion_generation_gate_input"
            and provenance.get("companion_event_id") == item["companion_event_id"]
            and provenance.get("repair_version") == 1
            and isinstance(provenance.get("applied_at"), str)
            and current_row["status"] == "SUCCEEDED"
            and current_row["actual_input_tokens"] == item["repair_input_tokens"]
            and current_row["usage_metadata_status"] == "KNOWN"
            and current_row["error_code"] is None
        ):
            return False
        if any(current_row[key] != backup_row[key] for key in backup_row.keys() - mutable):
            return False
    return True


def _validate_repair_backup(current: sqlite3.Connection, ledger_path: Path, backup_path: Path, current_fingerprint: dict, candidates: list[dict], unresolved: list[dict]) -> dict:
    if not backup_path.exists() or not backup_path.is_file():
        raise UsageLedgerError("USAGE_REPAIR_BACKUP_REQUIRED")
    if ledger_path.resolve() == backup_path.resolve() or os.path.samefile(ledger_path, backup_path):
        raise UsageLedgerError("USAGE_REPAIR_BACKUP_INVALID")
    try:
        with _read_only_connection(backup_path) as backup:
            backup_fingerprint = _database_fingerprint(backup, backup_path)
            for key in ("schema_version", "row_count", "core_schema"):
                if current_fingerprint[key] != backup_fingerprint[key]:
                    raise UsageLedgerError("USAGE_REPAIR_BACKUP_FINGERPRINT_MISMATCH")
            backup_candidates, backup_unresolved, _ = _repair_candidates(backup)
            if not backup_candidates or backup_unresolved:
                raise UsageLedgerError("USAGE_REPAIR_BACKUP_NOT_PRE_REPAIR")
            if not _fingerprints_match(current_fingerprint, backup_fingerprint):
                if candidates or unresolved or not _backup_matches_completed_repair(current, backup):
                    raise UsageLedgerError("USAGE_REPAIR_BACKUP_FINGERPRINT_MISMATCH")
            return backup_fingerprint
    except Exception as error:
        if isinstance(error, UsageLedgerError):
            raise
        raise UsageLedgerError("USAGE_REPAIR_BACKUP_INVALID") from error


def _public_candidates(candidates: list[dict]) -> list[dict]:
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in candidates]


def repair_preflight_collision(ledger: UsageLedger, *, apply: bool = False, backup_path: Path | None = None) -> dict:
    ledger.ensure()
    applied = 0
    backup_fingerprint = None
    with ledger._connect() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            before = _repair_summary(conn)
            baseline_fingerprint = _database_fingerprint(conn, ledger.path)
            candidates, unresolved, skipped = _repair_candidates(conn)
            if apply:
                if backup_path is None:
                    raise UsageLedgerError("USAGE_REPAIR_BACKUP_REQUIRED")
                backup_fingerprint = _validate_repair_backup(conn, ledger.path, Path(backup_path), baseline_fingerprint, candidates, unresolved)
                pending_applied = 0
                applied_at = datetime.now(timezone.utc).isoformat()
                for item in candidates:
                    provenance = json.dumps(
                        {
                            "repair_type": "issue40_preflight_collision",
                            "source_evidence": "companion_generation_gate_input",
                            "companion_event_id": item["companion_event_id"],
                            "repair_version": 1,
                            "applied_at": applied_at,
                        },
                        sort_keys=True,
                    )
                    cursor = conn.execute(
                        """
                        UPDATE usage_events
                        SET status='SUCCEEDED',
                            actual_input_tokens=?,
                            usage_metadata_status='KNOWN',
                            error_code=NULL,
                            repair_provenance=?,
                            updated_at=?
                        WHERE event_id=?
                          AND request_kind='count_tokens'
                          AND stage='preflight'
                          AND status='FAILED'
                          AND provider_dispatched=1
                          AND actual_input_tokens IS NULL
                          AND usage_metadata_status='MISSING'
                          AND error_code='TOKEN_COUNT_UNAVAILABLE'
                          AND repair_provenance IS NULL
                          AND usage_run_id IS NULL
                          AND usage_attempt_id IS NULL
                          AND request_group_id IS NULL
                          AND updated_at=?
                        """,
                        (item["repair_input_tokens"], provenance, applied_at, item["event_id"], item["_expected_updated_at"]),
                    )
                    if cursor.rowcount != 1:
                        raise UsageLedgerError("USAGE_REPAIR_STATE_CONFLICT")
                    pending_applied += 1
                conn.commit()
                applied = pending_applied
            else:
                conn.rollback()
            after = _repair_summary(conn) if apply else before
        except Exception as error:
            conn.rollback()
            if isinstance(error, UsageLedgerError):
                raise
            raise UsageLedgerError("USAGE_REPAIR_FAILED") from error
    return {
        "mode": "apply" if apply else "dry-run",
        "repairable": len(candidates),
        "applied": applied,
        "skipped": skipped,
        "unresolved": len(unresolved),
        "candidates": _public_candidates(candidates),
        "unresolved_rows": unresolved,
        "before": before,
        "after": after,
        "baseline_fingerprint": baseline_fingerprint,
        "backup_fingerprint": backup_fingerprint,
    }


class TokenGate:
    def __init__(self, ledger: UsageLedger, counter: Callable[[str, str, object], int | None] | None = None, usage_run_id: str | None = None, id_factory: Callable[[], str] | None = None):
        self.ledger = ledger
        self.counter = counter
        self.usage_run_id = usage_run_id or uuid.uuid4().hex
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._generation_ownership: dict[str, dict[str, str]] = {}

    def admit(self, *, client: object, model: str, prompt: str, config: object, key_slot_id: str, call: dict, max_output_tokens: int, output_identity: str | None = None, max_input_tokens: int | None = None) -> tuple[str, int]:
        usage_attempt_id = self.id_factory()
        request_group_id = f"{self.usage_run_id}:{usage_attempt_id}"
        count_event_id = f"{request_group_id}:count_tokens"
        generation_event_id = f"{request_group_id}:generate_content"
        count_ownership = {"usage_run_id": self.usage_run_id, "usage_attempt_id": usage_attempt_id, "request_group_id": request_group_id}
        generation_ownership = dict(count_ownership)
        count_prepared = False
        try:
            self.ledger.prepare_count_tokens(event_id=count_event_id, key_slot_id=key_slot_id, model=model, call=call, output_identity=output_identity, usage_run_id=self.usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id)
            count_prepared = True
            input_tokens, dispatched = self._count_tokens(count_event_id, count_ownership, client, model, prompt, config)
            self.ledger.finish_count_tokens(event_id=count_event_id, input_tokens=input_tokens, dispatched=dispatched, **count_ownership)
            if max_input_tokens is not None and input_tokens > max_input_tokens:
                raise TokenAdmissionError("PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED")
            admission = decide_admission(input_tokens, max_output_tokens)
            self.ledger.insert_generation(event_id=generation_event_id, key_slot_id=key_slot_id, model=model, call=call, input_tokens=input_tokens, max_output_tokens=max_output_tokens, admission=admission, output_identity=output_identity, usage_run_id=self.usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id)
        except TokenAdmissionError as error:
            if str(error) == "PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED":
                raise
            if count_prepared:
                try:
                    self.ledger.finish_count_tokens(event_id=count_event_id, input_tokens=None, dispatched=bool(getattr(error, "_arc_count_dispatched", False)), error_code="TOKEN_COUNT_UNAVAILABLE", **count_ownership)
                except Exception:
                    pass
            raise TokenAdmissionError("TOKEN_COUNT_UNAVAILABLE") from error
        except UsageEventCollision as error:
            raise TokenAdmissionError("USAGE_EVENT_ID_COLLISION") from error
        except UsageLedgerError:
            raise
        except Exception as error:
            if count_prepared:
                try:
                    self.ledger.finish_count_tokens(event_id=count_event_id, input_tokens=None, dispatched=bool(getattr(error, "_arc_count_dispatched", False)), error_code="TOKEN_COUNT_UNAVAILABLE", **count_ownership)
                except Exception:
                    pass
            raise TokenAdmissionError("TOKEN_COUNT_UNAVAILABLE") from error
        if not admission.allowed:
            raise TokenAdmissionError(admission.reason_code or "TOKEN_ADMISSION_BLOCKED")
        self._generation_ownership[generation_event_id] = generation_ownership
        return generation_event_id, input_tokens

    def mark_dispatched(self, event_id: str) -> None:
        self.ledger.mark_dispatched(event_id, request_kind="generate_content", **self._generation_ownership.get(event_id, {}))

    def finish(self, *, event_id: str, response: object | None, succeeded: bool, error_code: str | None = None) -> None:
        usage = parse_usage_metadata(getattr(response, "usage_metadata", None) if response else None)
        self.ledger.finish_generation(event_id=event_id, usage=usage, status="SUCCEEDED" if succeeded else "FAILED", error_code=error_code, **self._generation_ownership.get(event_id, {}))

    def cancel(self, event_id: str, error_code: str) -> None:
        self.ledger.cancel_generation(event_id=event_id, error_code=error_code, **self._generation_ownership.get(event_id, {}))

    def _count_tokens(self, event_id: str, ownership: dict[str, str], client: object, model: str, prompt: str, config: object) -> tuple[int, bool]:
        if self.counter:
            return _require_token_count(self.counter(model, prompt, config), dispatched=False), False
        models = getattr(client, "models", None)
        count_tokens = getattr(models, "count_tokens", None)
        if count_tokens:
            self.ledger.mark_dispatched(event_id, request_kind="count_tokens", **ownership)
            try:
                response = count_tokens(model=model, contents=prompt)
            except Exception as error:
                setattr(error, "_arc_count_dispatched", True)
                raise
            return _require_token_count(_get_int(response, "total_tokens", "totalTokens"), dispatched=True), True
        raise TokenAdmissionError("TOKEN_COUNT_UNAVAILABLE")


def _require_token_count(value: object, *, dispatched: bool) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    error = TokenAdmissionError("TOKEN_COUNT_UNAVAILABLE")
    setattr(error, "_arc_count_dispatched", dispatched)
    raise error


def _row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _sum_key_rows(rows: list[dict]) -> dict:
    if not rows:
        return {"provider_requests": 0, "generation_requests": 0, "count_token_requests": 0, "success_count": 0, "failed_count": 0, "usage_unknown_count": 0, "actual_input_tokens": None, "candidate_tokens": None, "reasoning_tokens": None, "combined_output_tokens": None, "provider_total_tokens": None, "blocked_count": 0}
    result: dict[str, int | None] = {}
    for key in rows[0]:
        if key == "key_slot_id":
            continue
        values = [row[key] for row in rows if row[key] is not None]
        result[key] = sum(values) if values else None
    for key in ("provider_requests", "generation_requests", "count_token_requests", "success_count", "failed_count", "usage_unknown_count", "blocked_count"):
        result[key] = int(result.get(key) or 0)
    return result


def _episode_from_scope(scope_id: str | None) -> str | None:
    if not isinstance(scope_id, str):
        return None
    if scope_id.startswith("episode:"):
        return scope_id.split(":", 1)[1]
    return None


def _clean_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _legacy_dispatch_time(call: dict) -> datetime | None:
    for key in ("provider_started_at", "started_at"):
        value = call.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None
