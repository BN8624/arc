from __future__ import annotations
# Provider project별 rolling quota admission과 atomic reservation을 제공한다.

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


PROJECT_QUOTA_CONFIG_INVALID = "PROJECT_QUOTA_CONFIG_INVALID"
PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED = "PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED"
PROJECT_QUOTA_ADMISSION_EXHAUSTED = "PROJECT_QUOTA_ADMISSION_EXHAUSTED"


class ProjectQuotaError(RuntimeError):
    def __init__(self, code: str, reason: str | None = None, *, details: dict | None = None):
        super().__init__(code)
        self.error_code, self.reason, self.details = code, reason, details or {}


@dataclass(frozen=True)
class QuotaLimits:
    rpm: int = 30
    input_tpm: int = 16000
    rpd: int = 14400
    safety_rpm: int = 27
    safety_input_tpm: int = 14000
    safety_rpd: int = 13000
    max_input_tokens_per_request: int = 14000

    def validate(self) -> None:
        if min(self.rpm, self.input_tpm, self.rpd) < 1 or min(self.safety_rpm, self.safety_input_tpm, self.safety_rpd) < 1:
            raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID)
        if not (self.safety_rpm <= self.rpm and self.safety_input_tpm <= self.input_tpm and self.safety_rpd <= self.rpd):
            raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID)
        if self.max_input_tokens_per_request < 1 or self.max_input_tokens_per_request > self.safety_input_tpm:
            raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID)


def default_project_buckets(slots: list[str]) -> dict[str, str]:
    return {slot: f"Q{index:02d}" for index, slot in enumerate(slots, 1)}


def parse_project_buckets(raw: str | None, slots: list[str]) -> dict[str, str]:
    try:
        mapping = default_project_buckets(slots) if not raw else json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID) from error
    if not isinstance(mapping, dict) or set(mapping) != set(slots) or any(not isinstance(slot, str) or not isinstance(bucket, str) or not bucket.strip() for slot, bucket in mapping.items()):
        raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID)
    if any(slot not in slots for slot in mapping):
        raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID)
    return dict(mapping)


def limits_from_environment(env: dict[str, str] | None = None) -> QuotaLimits:
    import os
    env = os.environ if env is None else env
    try:
        limits = QuotaLimits(
            rpm=int(env.get("ARC_PROVIDER_RPM_LIMIT", "30")),
            input_tpm=int(env.get("ARC_PROVIDER_INPUT_TPM_LIMIT", "16000")),
            rpd=int(env.get("ARC_PROVIDER_RPD_LIMIT", "14400")),
            safety_rpm=int(env.get("ARC_PROVIDER_RPM_SAFETY_LIMIT", "27")),
            safety_input_tpm=int(env.get("ARC_PROVIDER_INPUT_TPM_SAFETY_LIMIT", "14000")),
            safety_rpd=int(env.get("ARC_PROVIDER_RPD_SAFETY_LIMIT", "13000")),
            max_input_tokens_per_request=int(env.get("ARC_MAX_INPUT_TOKENS_PER_REQUEST", "14000")),
        )
    except (TypeError, ValueError) as error:
        raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID) from error
    limits.validate()
    return limits


@dataclass
class QuotaReservation:
    reservation_id: str
    bucket_id: str
    input_tokens: int
    created_at: float
    pacific_date: str
    state: str = "RESERVED"
    dispatched: bool = False


class ProjectQuotaLedger:
    """Thread-safe rolling admission ledger; dispatch keeps request consumption."""
    def __init__(self, limits: QuotaLimits, *, clock: callable = time.time, pacific_tz: str = "America/Los_Angeles", usage_ledger: object | None = None, bucket_ids: list[str] | None = None):
        limits.validate()
        self.limits, self.clock, self.tz = limits, clock, ZoneInfo(pacific_tz)
        self.usage_ledger, self.bucket_ids = usage_ledger, list(bucket_ids or [])
        self._lock = threading.RLock()
        self._events: dict[str, list[dict]] = {}
        self._daily_events: dict[str, list[dict]] = {}
        self._reservations: dict[str, QuotaReservation] = {}
        self.wait_count = self.reroute_count = self.blocked_count = 0

    def _now(self) -> float:
        return float(self.clock())

    def _date(self, now: float) -> str:
        return datetime.fromtimestamp(now, timezone.utc).astimezone(self.tz).date().isoformat()

    def _prune(self, bucket_id: str, now: float) -> list[dict]:
        events = [item for item in self._events.get(bucket_id, []) if item["timestamp"] > now - 60]
        self._events[bucket_id] = events
        return events

    def _prune_daily(self, bucket_id: str, now: float) -> list[dict]:
        events = [item for item in self._daily_events.get(bucket_id, []) if item["timestamp"] > now - 3 * 86400]
        self._daily_events[bucket_id] = events
        return events

    def reserve(self, bucket_id: str, input_tokens: int, *, now: float | None = None) -> QuotaReservation:
        if input_tokens > self.limits.max_input_tokens_per_request:
            self.blocked_count += 1
            raise ProjectQuotaError(PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED, details={"counted_input_tokens": input_tokens, "single_request_token_limit": self.limits.max_input_tokens_per_request})
        now = self._now() if now is None else now
        if self.usage_ledger is not None:
            if bucket_id not in self.bucket_ids:
                self.bucket_ids.append(bucket_id)
            result = self.usage_ledger.quota_reserve(reservation_id=uuid.uuid4().hex, bucket_id=bucket_id, input_tokens=input_tokens, created_at=now, pacific_date=self._date(now), limits={"safety_rpm": self.limits.safety_rpm, "safety_input_tpm": self.limits.safety_input_tpm, "safety_rpd": self.limits.safety_rpd})
            if not result["ok"]:
                self.blocked_count += 1
                raise ProjectQuotaError(PROJECT_QUOTA_ADMISSION_EXHAUSTED, result["reason"], details={"release_at": result.get("release_at")})
            reservation_id = result.get("reservation_id") or uuid.uuid4().hex
            reservation = QuotaReservation(reservation_id, bucket_id, input_tokens, now, self._date(now))
            with self._lock:
                self._reservations[reservation.reservation_id] = reservation
            return reservation
        with self._lock:
            events = self._prune(bucket_id, now)
            requests = len(events)
            tokens = sum(item["input_tokens"] for item in events)
            daily_events = self._prune_daily(bucket_id, now)
            daily = sum(1 for item in daily_events if item["pacific_date"] == self._date(now))
            if requests + 1 > self.limits.safety_rpm:
                self.blocked_count += 1
                raise ProjectQuotaError(PROJECT_QUOTA_ADMISSION_EXHAUSTED, "RPM_WINDOW_FULL", details={"release_at": min(item["timestamp"] + 60 for item in events) if events else now + 60})
            if tokens + input_tokens > self.limits.safety_input_tpm:
                self.blocked_count += 1
                raise ProjectQuotaError(PROJECT_QUOTA_ADMISSION_EXHAUSTED, "INPUT_TPM_WINDOW_FULL", details={"release_at": min(item["timestamp"] + 60 for item in events) if events else now + 60})
            if daily + 1 > self.limits.safety_rpd:
                self.blocked_count += 1
                raise ProjectQuotaError(PROJECT_QUOTA_ADMISSION_EXHAUSTED, "RPD_DAY_FULL", details={"release_at": None})
            reservation = QuotaReservation(uuid.uuid4().hex, bucket_id, input_tokens, now, self._date(now))
            self._reservations[reservation.reservation_id] = reservation
            event = {"timestamp": now, "input_tokens": input_tokens, "pacific_date": reservation.pacific_date, "reservation_id": reservation.reservation_id, "dispatched": False}
            self._events.setdefault(bucket_id, []).append(event)
            self._daily_events.setdefault(bucket_id, []).append(dict(event))
            return reservation

    def dispatch(self, reservation_id: str) -> None:
        if self.usage_ledger is not None:
            self.usage_ledger.quota_transition(reservation_id, state="DISPATCHED", dispatched=True)
            with self._lock:
                reservation = self._reservations[reservation_id]
                reservation.state, reservation.dispatched = "DISPATCHED", True
            return
        with self._lock:
            reservation = self._reservations[reservation_id]
            reservation.state, reservation.dispatched = "DISPATCHED", True
            for event in self._events.get(reservation.bucket_id, []):
                if event["reservation_id"] == reservation_id:
                    event["dispatched"] = True

    def finish(self, reservation_id: str, *, succeeded: bool) -> None:
        if self.usage_ledger is not None:
            self.usage_ledger.quota_transition(reservation_id, state="SUCCEEDED" if succeeded else "FAILED")
            with self._lock:
                self._reservations[reservation_id].state = "SUCCEEDED" if succeeded else "FAILED"
            return
        with self._lock:
            reservation = self._reservations[reservation_id]
            reservation.state = "SUCCEEDED" if succeeded else "FAILED"

    def release(self, reservation_id: str) -> None:
        if self.usage_ledger is not None:
            self.usage_ledger.quota_release(reservation_id)
            with self._lock:
                if reservation_id in self._reservations:
                    self._reservations[reservation_id].state = "RELEASED"
            return
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if not reservation or reservation.dispatched:
                return
            reservation.state = "RELEASED"
            self._events[reservation.bucket_id] = [item for item in self._events.get(reservation.bucket_id, []) if item["reservation_id"] != reservation_id]
            self._daily_events[reservation.bucket_id] = [item for item in self._daily_events.get(reservation.bucket_id, []) if item["reservation_id"] != reservation_id]

    def snapshot(self, *, now: float | None = None) -> dict:
        now = self._now() if now is None else now
        if self.usage_ledger is not None:
            return self.usage_ledger.quota_snapshot(self.bucket_ids, now=now, limits={"safety_rpm": self.limits.safety_rpm, "safety_input_tpm": self.limits.safety_input_tpm, "safety_rpd": self.limits.safety_rpd}, pacific_date=self._date(now))
        with self._lock:
            result = {}
            for bucket_id in sorted(set(self._events) | {item.bucket_id for item in self._reservations.values()}):
                events = self._prune(bucket_id, now)
                daily_events = self._prune_daily(bucket_id, now)
                daily = sum(1 for item in daily_events if item["pacific_date"] == self._date(now))
                result[bucket_id] = {"bucket_id": bucket_id, "rolling_request_count": len(events), "rolling_reserved_input_tokens": sum(item["input_tokens"] for item in events), "daily_request_count": daily, "remaining_rpm_headroom": max(0, self.limits.safety_rpm - len(events)), "remaining_tpm_headroom": max(0, self.limits.safety_input_tpm - sum(item["input_tokens"] for item in events)), "remaining_rpd_headroom": max(0, self.limits.safety_rpd - daily), "in_flight_reservations": sum(item.state == "RESERVED" for item in self._reservations.values() if item.bucket_id == bucket_id)}
            return result

    def restore(self, snapshot: dict) -> None:
        if self.usage_ledger is not None:
            return
        with self._lock:
            for bucket_id, item in snapshot.items():
                count = int(item.get("rolling_request_count", 0))
                tokens = int(item.get("rolling_reserved_input_tokens", 0))
                per_request = tokens // count if count else 0
                now = self._now()
                self._events[bucket_id] = [{"timestamp": now, "input_tokens": per_request, "pacific_date": self._date(now), "reservation_id": f"restored-{bucket_id}-{index}", "dispatched": True} for index in range(count)]
                self._daily_events[bucket_id] = [dict(event) for event in self._events[bucket_id]]

    def export_state(self) -> dict:
        if self.usage_ledger is not None:
            return {"backend": "usage_ledger"}
        with self._lock:
            return {"events": {bucket: [dict(event) for event in events] for bucket, events in self._events.items()}, "daily_events": {bucket: [dict(event) for event in events] for bucket, events in self._daily_events.items()}, "reservations": {reservation_id: {"reservation_id": item.reservation_id, "bucket_id": item.bucket_id, "input_tokens": item.input_tokens, "created_at": item.created_at, "pacific_date": item.pacific_date, "state": item.state, "dispatched": item.dispatched} for reservation_id, item in self._reservations.items()}}

    def restore_state(self, state: dict) -> None:
        if self.usage_ledger is not None:
            return
        with self._lock:
            self._events = {bucket: [dict(event) for event in events] for bucket, events in state.get("events", {}).items()}
            self._daily_events = {bucket: [dict(event) for event in events] for bucket, events in state.get("daily_events", {}).items()}
            self._reservations = {reservation_id: QuotaReservation(**item) for reservation_id, item in state.get("reservations", {}).items()}
            now = self._now()
            stale = {reservation_id for reservation_id, item in self._reservations.items() if item.state == "RESERVED" and not item.dispatched and now - item.created_at > 60}
            for reservation_id in stale:
                reservation = self._reservations[reservation_id]
                reservation.state = "RELEASED"
                self._events[reservation.bucket_id] = [item for item in self._events.get(reservation.bucket_id, []) if item["reservation_id"] != reservation_id]
                self._daily_events[reservation.bucket_id] = [item for item in self._daily_events.get(reservation.bucket_id, []) if item["reservation_id"] != reservation_id]


class ProjectQuotaAdmission:
    def __init__(self, slots: list[str], limits: QuotaLimits, mapping: dict[str, str] | None = None, *, ledger: ProjectQuotaLedger | None = None, clock: callable = time.time, waiter: callable | None = None, usage_ledger: object | None = None):
        self.slots = list(slots)
        self.mapping = mapping or default_project_buckets(self.slots)
        if set(self.mapping) != set(self.slots) or any(not self.mapping[slot] for slot in self.slots):
            raise ProjectQuotaError(PROJECT_QUOTA_CONFIG_INVALID)
        self.limits, self.ledger = limits, ledger or ProjectQuotaLedger(limits, clock=clock, usage_ledger=usage_ledger, bucket_ids=list(set(self.mapping.values())))
        self.clock, self.waiter = clock, waiter or time.sleep

    def reserve_for_slot(self, slot: str, input_tokens: int) -> QuotaReservation:
        return self.ledger.reserve(self.mapping[slot], input_tokens)

    def reserve_for_candidates(self, candidates: list[str], input_tokens: int, *, deadline: float | None = None) -> QuotaReservation:
        attempted: set[str] = set()
        while True:
            reasons = []
            release_times = []
            for index, slot in enumerate(candidates):
                if slot in attempted:
                    continue
                attempted.add(slot)
                try:
                    reservation = self.reserve_for_slot(slot, input_tokens)
                    if index:
                        self.ledger.reroute_count += index
                    return reservation
                except ProjectQuotaError as error:
                    reasons.append(error.reason)
                    release_at = error.details.get("release_at")
                    if release_at is not None:
                        release_times.append(float(release_at))
            if not release_times:
                raise ProjectQuotaError(PROJECT_QUOTA_ADMISSION_EXHAUSTED, reasons[-1] if reasons and all(reason == reasons[0] for reason in reasons) else "NO_PROJECT_AVAILABLE_BEFORE_DEADLINE")
            now = float(self.clock())
            wait_until = min(release_times)
            wait_seconds = max(0.0, wait_until - now)
            if deadline is not None and now + wait_seconds > deadline:
                raise ProjectQuotaError(PROJECT_QUOTA_ADMISSION_EXHAUSTED, "NO_PROJECT_AVAILABLE_BEFORE_DEADLINE", details={"release_at": wait_until})
            self.ledger.wait_count += 1
            self.waiter(wait_seconds)
            attempted.clear()

    def telemetry(self) -> dict:
        return {"provider_active_limits": {"rpm": self.limits.rpm, "input_tpm": self.limits.input_tpm, "rpd": self.limits.rpd}, "provider_safety_limits": {"rpm": self.limits.safety_rpm, "input_tpm": self.limits.safety_input_tpm, "rpd": self.limits.safety_rpd}, "quota_project_bucket_count": len(set(self.mapping.values())), "quota_wait_count": self.ledger.wait_count, "quota_reroute_count": self.ledger.reroute_count, "quota_blocked_count": self.ledger.blocked_count, "buckets": self.ledger.snapshot(), "ledger_state": self.ledger.export_state()}
