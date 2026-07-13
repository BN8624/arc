# ARC provider 사용량 원장을 SQLite에 기록하고 토큰 admission을 판정한다.
from __future__ import annotations

import json
import os
import sqlite3
import threading
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
        with self._lock:
            if self._ready:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if os.name == "posix":
                    self.path.parent.chmod(0o700)
                with self._connect() as conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=5000")
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
                    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
                    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
                    version = int(conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0])
                    if version > SCHEMA_VERSION:
                        raise UsageLedgerError("UNSUPPORTED_USAGE_SCHEMA_VERSION")
                    if version < 2:
                        self._migrate_v1_to_v2(conn)
                self._ready = True
            except Exception as error:
                if isinstance(error, UsageLedgerError):
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

    def finish_count_tokens(self, *, event_id: str, input_tokens: int | None, dispatched: bool, error_code: str | None = None) -> None:
        status = "SUCCEEDED" if error_code is None else "FAILED"
        self.update_event(event_id, expected_statuses={"PREPARED", "DISPATCHED"}, status=status, provider_dispatched=dispatched, actual_input_tokens=input_tokens, usage_metadata_status="KNOWN" if input_tokens is not None else "MISSING", error_code=error_code)

    def insert_generation(self, *, event_id: str, key_slot_id: str, model: str, call: dict, input_tokens: int | None, max_output_tokens: int, admission: Admission, output_identity: str | None = None, usage_run_id: str | None = None, usage_attempt_id: str | None = None, request_group_id: str | None = None) -> None:
        status = "PREPARED" if admission.allowed else "BLOCKED"
        self.insert_event(event_id=event_id, request_kind="generate_content", key_slot_id=key_slot_id, model=model, call=call, output_identity=output_identity, usage_run_id=usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id, gate_input_tokens=input_tokens, configured_max_output_tokens=max_output_tokens, gate_decision=admission.gate_decision, gate_reason_code=admission.reason_code or admission.warning_code, provider_dispatched=False, status=status, usage_metadata_status="PENDING" if admission.allowed else "NOT_APPLICABLE", token_provenance="provider")

    def mark_dispatched(self, event_id: str) -> None:
        self.update_event(event_id, expected_statuses={"PREPARED"}, status="DISPATCHED", provider_dispatched=True)

    def finish_generation(self, *, event_id: str, usage: UsageNumbers, status: str, error_code: str | None = None) -> None:
        final_status = "USAGE_UNKNOWN" if status == "SUCCEEDED" and usage.metadata_status == "MISSING" else status
        self.update_event(
            event_id,
            expected_statuses={"DISPATCHED"},
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

    def update_event(self, event_id: str, *, expected_statuses: set[str] | None = None, allow_terminal: bool = False, **values: Any) -> None:
        self.ensure()
        values["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "provider_dispatched" in values:
            values["provider_dispatched"] = int(bool(values["provider_dispatched"]))
        columns = ", ".join(f"{key}=?" for key in values)
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT status FROM usage_events WHERE event_id=?", (event_id,)).fetchone()
                if row is None:
                    raise UsageLedgerError("USAGE_EVENT_NOT_FOUND")
                current = row["status"]
                terminal = {"SUCCEEDED", "FAILED", "USAGE_UNKNOWN", "BLOCKED"}
                if expected_statuses is not None and current not in expected_statuses:
                    raise UsageLedgerError("USAGE_EVENT_STATE_CONFLICT")
                if expected_statuses is None and not allow_terminal and current in terminal:
                    raise UsageLedgerError("USAGE_EVENT_TERMINAL")
                cursor = conn.execute(f"UPDATE usage_events SET {columns} WHERE event_id=?", (*values.values(), event_id))
                if cursor.rowcount != 1:
                    raise UsageLedgerError("USAGE_EVENT_UPDATE_FAILED")
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
                self._refresh_legacy_dispatch_time(event_id, dispatch_utc)
                counts["skipped"] += 1
            except UsageLedgerError:
                raise
            except Exception:
                counts["skipped"] += 1
        return counts

    def _refresh_legacy_dispatch_time(self, event_id: str, dispatch_utc: datetime | None) -> None:
        if dispatch_utc is None:
            return
        utc, pacific_ts, pacific_date = pacific_fields(dispatch_utc)
        self.update_event(event_id, allow_terminal=True, utc_dispatch_ts=utc, pacific_dispatch_ts=pacific_ts, pacific_date=pacific_date)

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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


def backup_usage_db(source: Path | None = None, destination: Path | None = None) -> dict:
    source = source or usage_db_path()
    if destination is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = source.with_name(f"{source.stem}.backup-{stamp}{source.suffix}")
    if destination.exists():
        raise UsageLedgerError("USAGE_BACKUP_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    with sqlite3.connect(destination) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise UsageLedgerError("USAGE_BACKUP_INTEGRITY_FAILED")
    return {"source": str(source), "backup": str(destination), "integrity_check": integrity}


def repair_preflight_collision(ledger: UsageLedger, *, apply: bool = False, backup_path: Path | None = None) -> dict:
    if apply:
        if backup_path is None or not backup_path.exists():
            raise UsageLedgerError("USAGE_REPAIR_BACKUP_REQUIRED")
        with sqlite3.connect(backup_path) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise UsageLedgerError("USAGE_REPAIR_BACKUP_INVALID")
    ledger.ensure()
    candidates: list[dict] = []
    unresolved: list[dict] = []
    with ledger._connect() as conn:
        count_rows = conn.execute(
            """
            SELECT * FROM usage_events
            WHERE request_kind='count_tokens'
              AND stage='preflight'
              AND status='FAILED'
              AND actual_input_tokens IS NULL
              AND error_code='TOKEN_COUNT_UNAVAILABLE'
            ORDER BY key_slot_id, event_id
            """
        ).fetchall()
        for count_row in count_rows:
            generation = conn.execute(
                """
                SELECT * FROM usage_events
                WHERE request_kind='generate_content'
                  AND stage='preflight'
                  AND key_slot_id=?
                  AND call_id=?
                  AND ((lease_sequence IS NULL AND ? IS NULL) OR lease_sequence=?)
                  AND status='SUCCEEDED'
                  AND provider_dispatched=1
                  AND typeof(gate_input_tokens)='integer'
                ORDER BY utc_dispatch_ts
                LIMIT 1
                """,
                (count_row["key_slot_id"], count_row["call_id"], count_row["lease_sequence"], count_row["lease_sequence"]),
            ).fetchone()
            if generation is None:
                unresolved.append({"event_id": count_row["event_id"], "key_slot_id": count_row["key_slot_id"], "reason_code": "COMPANION_GENERATION_MISSING"})
                continue
            candidates.append({"event_id": count_row["event_id"], "key_slot_id": count_row["key_slot_id"], "repair_input_tokens": generation["gate_input_tokens"], "companion_event_id": generation["event_id"], "reason_code": "COMPANION_GENERATION_GATE_INPUT"})
    applied = 0
    if apply and candidates:
        provenance = json.dumps({"repair": "issue40_preflight_collision", "source": "companion_generation_gate_input"}, sort_keys=True)
        with ledger._connect() as conn:
            try:
                conn.execute("BEGIN")
                for item in candidates:
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
                          AND status='FAILED'
                          AND actual_input_tokens IS NULL
                          AND error_code='TOKEN_COUNT_UNAVAILABLE'
                        """,
                        (item["repair_input_tokens"], provenance, datetime.now(timezone.utc).isoformat(), item["event_id"]),
                    )
                    if cursor.rowcount != 1:
                        raise UsageLedgerError("USAGE_REPAIR_STATE_CONFLICT")
                    applied += 1
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    return {"mode": "apply" if apply else "dry-run", "repairable": len(candidates), "applied": applied, "skipped": 0, "unresolved": len(unresolved), "candidates": candidates, "unresolved_rows": unresolved}


class TokenGate:
    def __init__(self, ledger: UsageLedger, counter: Callable[[str, str, object], int | None] | None = None, usage_run_id: str | None = None, id_factory: Callable[[], str] | None = None):
        self.ledger = ledger
        self.counter = counter
        self.usage_run_id = usage_run_id or uuid.uuid4().hex
        self.id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def admit(self, *, client: object, model: str, prompt: str, config: object, key_slot_id: str, call: dict, max_output_tokens: int, output_identity: str | None = None) -> tuple[str, int]:
        usage_attempt_id = self.id_factory()
        request_group_id = f"{self.usage_run_id}:{usage_attempt_id}"
        count_event_id = f"{request_group_id}:count_tokens"
        generation_event_id = f"{request_group_id}:generate_content"
        count_prepared = False
        try:
            self.ledger.prepare_count_tokens(event_id=count_event_id, key_slot_id=key_slot_id, model=model, call=call, output_identity=output_identity, usage_run_id=self.usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id)
            count_prepared = True
            input_tokens, dispatched = self._count_tokens(count_event_id, client, model, prompt, config)
            self.ledger.finish_count_tokens(event_id=count_event_id, input_tokens=input_tokens, dispatched=dispatched)
            admission = decide_admission(input_tokens, max_output_tokens)
            self.ledger.insert_generation(event_id=generation_event_id, key_slot_id=key_slot_id, model=model, call=call, input_tokens=input_tokens, max_output_tokens=max_output_tokens, admission=admission, output_identity=output_identity, usage_run_id=self.usage_run_id, usage_attempt_id=usage_attempt_id, request_group_id=request_group_id)
        except UsageEventCollision as error:
            raise TokenAdmissionError("USAGE_EVENT_ID_COLLISION") from error
        except UsageLedgerError:
            raise
        except Exception as error:
            if count_prepared:
                try:
                    self.ledger.finish_count_tokens(event_id=count_event_id, input_tokens=None, dispatched=bool(getattr(error, "_arc_count_dispatched", False)), error_code="TOKEN_COUNT_UNAVAILABLE")
                except Exception:
                    pass
            raise TokenAdmissionError("TOKEN_COUNT_UNAVAILABLE") from error
        if not admission.allowed:
            raise TokenAdmissionError(admission.reason_code or "TOKEN_ADMISSION_BLOCKED")
        return generation_event_id, input_tokens

    def mark_dispatched(self, event_id: str) -> None:
        self.ledger.mark_dispatched(event_id)

    def finish(self, *, event_id: str, response: object | None, succeeded: bool, error_code: str | None = None) -> None:
        usage = parse_usage_metadata(getattr(response, "usage_metadata", None) if response else None)
        self.ledger.finish_generation(event_id=event_id, usage=usage, status="SUCCEEDED" if succeeded else "FAILED", error_code=error_code)

    def _count_tokens(self, event_id: str, client: object, model: str, prompt: str, config: object) -> tuple[int | None, bool]:
        if self.counter:
            return self.counter(model, prompt, config), False
        models = getattr(client, "models", None)
        count_tokens = getattr(models, "count_tokens", None)
        if count_tokens:
            self.ledger.mark_dispatched(event_id)
            try:
                response = count_tokens(model=model, contents=prompt)
            except Exception as error:
                setattr(error, "_arc_count_dispatched", True)
                raise
            return _get_int(response, "total_tokens", "totalTokens"), True
        raise TokenAdmissionError("TOKEN_COUNT_UNAVAILABLE")


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
