# Phase 2 Gemma 키 풀과 비밀 없는 호출 추적을 제공한다.
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import threading
import time
import socket
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
import json
from pathlib import Path
from enum import Enum


MODEL_NAME = "gemma-4-31b-it"
DESK_ORDER = (
    ("planning", "event"), ("planning", "protagonist_action"), ("planning", "relationship"), ("planning", "continuity"), ("planning", "readability_weight"), ("planning", "reader_payoff"), ("planning_merge", "merge"), ("writer", "canonical"),
    ("review", "causality"), ("review", "protagonist_agency"), ("review", "character_consistency"), ("review", "continuity"), ("review", "readability"), ("review", "narrative_weight"), ("review", "payoff_and_hook"), ("review_merge", "merge"), ("revision", "canonical"),
    ("memory", "confirmed_facts"), ("memory", "relationships"), ("memory", "conflicts_and_promises"), ("memory", "important_excerpts"), ("memory_merge", "merge"),
)
LIVE_LOGICAL_ORDER = {desk: index for index, desk in enumerate(DESK_ORDER, start=1)}
PILOT_TRANSITION_ORDER = {("transition", "adapter"): len(DESK_ORDER) + 1}
PILOT_ACCEPTANCE_ORDER = {("pilot_review", role): index for index, role in enumerate(("readability", "character_consistency", "continuity", "rolling_plan_adaptation", "memory_correctness", "narrative_weight", "episode_to_episode_interest"), start=1)}


class LiveConfigError(ValueError):
    """Live configuration is incomplete or unsafe."""


class LiveCallError(RuntimeError):
    def __init__(self, error_class: str, stage: str, role: str, slot: str, message: str, http_status: int | None = None, provider_code: str | None = None):
        super().__init__(message)
        self.error_class, self.stage, self.role, self.slot = error_class, stage, role, slot
        self.http_status, self.provider_code = http_status, provider_code


@dataclass(frozen=True)
class LogicalDesk:
    desk_id: str
    stage: str
    role: str
    logical_order: int
    scope_id: str | None = None


def logical_desk(stage: str, role: str) -> LogicalDesk:
    return LogicalDesk(f"{stage}:{role}", stage, role, LIVE_LOGICAL_ORDER[(stage, role)])


def scoped_logical_desk(scope_id: str, logical_order_base: int, stage: str, role: str) -> LogicalDesk:
    if (stage, role) in LIVE_LOGICAL_ORDER:
        order = LIVE_LOGICAL_ORDER[(stage, role)]
    elif (stage, role) in PILOT_TRANSITION_ORDER:
        order = PILOT_TRANSITION_ORDER[(stage, role)]
    else:
        order = PILOT_ACCEPTANCE_ORDER[(stage, role)]
    return LogicalDesk(
        f"{scope_id}:{stage}:{role}",
        stage,
        role,
        logical_order_base + order,
        scope_id,
    )


class KeyState(str, Enum):
    AVAILABLE = "AVAILABLE"
    IN_USE = "IN_USE"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


class DynamicKeyPool:
    """Lease fungible API-key slots to logical desks."""
    def __init__(self, slots: list[str], monotonic: Callable[[], float] = time.monotonic, state_store: "RoutingStateStore | None" = None, utcnow: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._clock, self._lock, self._slots, self._cursor, self._sequence, self._store = monotonic, threading.Condition(), {slot: {"state": KeyState.AVAILABLE, "failures": 0, "cooldown": 0.0, "lease": 0} for slot in slots}, 0, 0, state_store
        self._utcnow = utcnow
        if state_store and state_store.path.exists():
            state = state_store.load()
            self._cursor, self._sequence = state["next_round_robin_cursor"], state["next_lease_sequence"] - 1
            for slot, saved in state["keys"].items():
                cooldown = saved["cooldown_until"]
                remaining = max(0.0, (datetime.fromisoformat(cooldown) - self._utcnow()).total_seconds()) if cooldown else 0.0
                self._slots[slot] = {"state": KeyState.AVAILABLE if saved["state"] == KeyState.COOLDOWN and not remaining else KeyState(saved["state"]), "failures": saved["consecutive_transient_failures"], "cooldown": self._clock() + remaining, "lease": saved["last_lease_sequence"]}

    def _persist(self) -> None:
        if self._store:
            self._store.save(cursor=self._cursor % len(self._slots), lease_sequence=self._sequence + 1, keys={slot: {"state": item["state"], "consecutive_transient_failures": item["failures"], "cooldown_until": (self._utcnow() + timedelta(seconds=max(0.0, item["cooldown"] - self._clock()))).isoformat() if item["state"] == KeyState.COOLDOWN else None, "last_lease_sequence": item["lease"]} for slot, item in self._slots.items()})

    def lease(self) -> tuple[str, int]:
        with self._lock:
            while True:
                now = self._clock()
                for item in self._slots.values():
                    if item["state"] == KeyState.COOLDOWN and item["cooldown"] <= now:
                        item["state"] = KeyState.AVAILABLE
                names = list(self._slots)
                for offset in range(len(names)):
                    index = (self._cursor + offset) % len(names)
                    slot, item = names[index], self._slots[names[index]]
                    if item["state"] == KeyState.AVAILABLE:
                        self._cursor, self._sequence, item["state"], item["lease"] = index + 1, self._sequence + 1, KeyState.IN_USE, self._sequence + 1
                        self._persist()
                        return slot, item["lease"]
                waits = [item["cooldown"] - now for item in self._slots.values() if item["state"] == KeyState.COOLDOWN]
                if not waits:
                    raise LiveConfigError("all key slots are disabled")
                self._lock.wait(timeout=max(0.0, min(waits)))

    def release(self, slot: str, error_class: str | None = None) -> None:
        with self._lock:
            item = self._slots[slot]
            if error_class in {"AUTH_ERROR", "PERMISSION_ERROR"}:
                item["state"] = KeyState.DISABLED
            elif error_class in {"RATE_LIMITED", "PROVIDER_5XX", "TIMEOUT", "NETWORK_ERROR"}:
                item["failures"] += 1
                item["cooldown"] = self._clock() + (10.0 if item["failures"] == 1 else 30.0)
                item["state"] = KeyState.COOLDOWN
            else:
                item["failures"], item["state"] = 0, KeyState.AVAILABLE
            self._persist()
            self._lock.notify_all()


class RoutingStateStore:
    """Persists dynamic pool state without secrets or process-local objects."""
    def __init__(self, path: Path, slots: list[str]):
        self.path, self.slots = path, slots

    def save(self, *, cursor: int, lease_sequence: int, keys: dict) -> None:
        from .storage import write_json
        write_json(self.path, {"routing_schema_version": 2, "routing_mode": "dynamic_key_pool", "next_round_robin_cursor": cursor, "next_lease_sequence": lease_sequence, "keys": keys})

    def load(self) -> dict:
        from .storage import read_json
        data = read_json(self.path)
        if data.get("routing_schema_version") != 2 or data.get("routing_mode") != "dynamic_key_pool" or set(data.get("keys", {})) != set(self.slots) or not isinstance(data.get("next_lease_sequence"), int) or data["next_lease_sequence"] < 1:
            raise LiveConfigError("invalid routing state")
        if not isinstance(data.get("next_round_robin_cursor"), int) or not 0 <= data["next_round_robin_cursor"] < len(self.slots):
            raise LiveConfigError("invalid routing cursor")
        for item in data["keys"].values():
            if set(item) != {"state", "consecutive_transient_failures", "cooldown_until", "last_lease_sequence"} or item["state"] not in set(KeyState) or not isinstance(item["consecutive_transient_failures"], int) or item["consecutive_transient_failures"] < 0 or not isinstance(item["last_lease_sequence"], int) or item["last_lease_sequence"] < 0:
                raise LiveConfigError("invalid routing key state")
            cooldown = item["cooldown_until"]
            if (cooldown is None) != (item["state"] != KeyState.COOLDOWN):
                raise LiveConfigError("invalid routing cooldown")
            if cooldown:
                try:
                    parsed = datetime.fromisoformat(cooldown)
                except (TypeError, ValueError) as error:
                    raise LiveConfigError("invalid routing cooldown") from error
                if parsed.tzinfo is None:
                    raise LiveConfigError("invalid routing cooldown")
            if item.get("state") == KeyState.IN_USE:
                item["state"] = KeyState.AVAILABLE
        return data


def scope_projection(telemetry: dict, scope_id: str) -> dict:
    """Deterministically project one scope out of canonical telemetry."""
    return {
        "schema_version": telemetry.get("schema_version"),
        "provider": telemetry.get("provider"),
        "model": telemetry.get("model"),
        "calls": [call for call in telemetry.get("calls", []) if call.get("scope_id") == scope_id],
        "contract_failures": [item for item in telemetry.get("contract_failures", []) if item.get("scope_id") == scope_id],
    }


class AtomicTelemetryStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def save(self, telemetry: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(telemetry, handle, ensure_ascii=False, indent=2, sort_keys=True)
                    handle.write("\n")
                os.replace(tmp_name, self.path)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
                raise

    def load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class LiveConfig:
    model: str
    keys: dict[str, str]
    max_live: int = 11
    launch_interval: float = 2.0
    timeout: int = 600
    json_limit: int = 8192
    prose_limit: int = 32768
    thinking_level: str = "high"

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "LiveConfig":
        env = os.environ if env is None else env
        try:
            config = cls(env.get("MODEL", ""), {f"K{i:02d}": env.get(f"GOOGLE_API_KEY_{i}", "") for i in range(1, 12)}, int(env.get("ARC_MAX_LIVE", "11")), float(env["ARC_LAUNCH_INTERVAL_SECONDS"]), int(env.get("ARC_REQUEST_TIMEOUT_SECONDS", "600")), int(env.get("ARC_JSON_MAX_OUTPUT_TOKENS", "8192")), int(env.get("ARC_PROSE_MAX_OUTPUT_TOKENS", "32768")), env.get("ARC_THINKING_LEVEL", "high"))
        except (ValueError, KeyError) as error:
            raise LiveConfigError("numeric live configuration is invalid") from error
        config.validate()
        return config

    def validate(self) -> None:
        if self.model != MODEL_NAME:
            raise LiveConfigError("MODEL must be gemma-4-31b-it")
        if any(not value for value in self.keys.values()) or len(set(self.keys.values())) != 11:
            raise LiveConfigError("eleven distinct non-empty key slots are required")
        if not 1 <= self.max_live <= 11 or not math.isfinite(self.launch_interval) or not 1 <= self.launch_interval <= 60 or self.timeout <= 0 or not 1 <= self.json_limit <= 32768 or not 1 <= self.prose_limit <= 32768:
            raise LiveConfigError("live limits are invalid")
        if self.thinking_level not in {"low", "medium", "high"}:
            raise LiveConfigError("thinking level is invalid")


class LaunchPacer:
    def __init__(self, interval: float, monotonic: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep):
        self.interval, self.monotonic, self.sleeper, self.lock, self.next_allowed, self.sequence = interval, monotonic, sleeper, threading.Lock(), 0.0, 0

    def wait(self) -> tuple[int, float, float]:
        with self.lock:
            now = self.monotonic()
            scheduled = max(now, self.next_allowed)
            self.next_allowed = scheduled + self.interval
            self.sequence += 1
            sequence = self.sequence
        wait = max(0.0, scheduled - self.monotonic())
        if wait:
            self.sleeper(wait)
        return sequence, scheduled, self.monotonic()


class ScopedGemmaPoolClient:
    def __init__(self, base: "GemmaPoolClient", scope_id: str, logical_order_base: int):
        self.base = base
        self.scope_id = scope_id
        self.logical_order_base = logical_order_base
        self.config = base.config
        self.pool = base.pool
        self.pacer = base.pacer

    def generate_for_desk(self, *, desk: LogicalDesk, prompt: str) -> str:
        return self.base.generate_for_desk(desk=scoped_logical_desk(self.scope_id, self.logical_order_base, desk.stage, desk.role), prompt=prompt)

    def generate(self, *, stage: str, role: str, prompt: str) -> str:
        return self.base.generate_for_desk(desk=scoped_logical_desk(self.scope_id, self.logical_order_base, stage, role), prompt=prompt)

    @property
    def sdk_version(self) -> str:
        return self.base.sdk_version

    def telemetry(self) -> dict:
        return self.base.telemetry(scope_id=self.scope_id)

    def restore_telemetry(self, telemetry: dict) -> None:
        invalid = [call.get("desk_id") for call in telemetry.get("calls", []) if call.get("scope_id") != self.scope_id]
        if invalid:
            raise ValueError(f"telemetry projection contains calls outside {self.scope_id}: {invalid[:3]}")

    def record_contract_failure(self, stage: str, role: str, slot: str | None = None, contract_code: str | None = None, character_count: int | None = None) -> None:
        self.base.record_contract_failure(stage, role, slot, contract_code=contract_code, scope_id=self.scope_id, character_count=character_count)

    def close(self) -> None:
        return None


class GemmaPoolClient:
    def __init__(self, config: LiveConfig, client_factory: Callable[[str], object] | None = None, state_store: RoutingStateStore | None = None, telemetry_sink: Callable[[dict], None] | None = None, usage_gate: object | None = None):
        self.config, self._lock, self.calls = config, threading.Lock(), []
        self._telemetry_sink = telemetry_sink
        uses_real_provider = client_factory is None
        if usage_gate is None and uses_real_provider:
            from .usage import TokenGate, UsageLedger
            usage_gate = TokenGate(UsageLedger())
        self.usage_gate = usage_gate
        self.pacer = LaunchPacer(config.launch_interval)
        self.pool = DynamicKeyPool(list(config.keys), state_store=state_store)
        self.contract_failures: list[dict] = []
        self.active_by_stage: dict[str, int] = {}
        self.max_active_by_stage: dict[str, int] = {}
        if client_factory is None:
            from google import genai
            from google.genai import types
            options = types.HttpOptions(timeout=config.timeout * 1000, retryOptions=types.HttpRetryOptions(attempts=1))
            client_factory, self._types = lambda key: genai.Client(api_key=key, http_options=options), types
        else:
            self._types = None
        self._clients = {slot: client_factory(key) for slot, key in config.keys.items()}

    @property
    def sdk_version(self) -> str:
        return importlib.metadata.version("google-genai")

    def probe_key(self, *, key_slot: str, prompt: str) -> str:
        return self._invoke(key_slot, LogicalDesk(f"preflight:{key_slot}", "preflight", key_slot, 0), prompt)

    def scope(self, *, scope_id: str, logical_order_base: int) -> ScopedGemmaPoolClient:
        return ScopedGemmaPoolClient(self, scope_id, logical_order_base)

    def generate_for_desk(self, *, desk: LogicalDesk, prompt: str) -> str:
        while True:
            slot, lease_sequence = self.pool.lease()
            try:
                text = self._invoke(slot, desk, prompt, lease_sequence)
                self.pool.release(slot)
                return text
            except LiveCallError as error:
                self.pool.release(slot, error.error_class)
                if error.error_class in {"RATE_LIMITED", "PROVIDER_5XX", "TIMEOUT", "NETWORK_ERROR", "AUTH_ERROR", "PERMISSION_ERROR"}:
                    continue
                raise

    def generate(self, *, stage: str, role: str, prompt: str) -> str:
        return self.generate_for_desk(desk=logical_desk(stage, role), prompt=prompt)

    def _invoke(self, slot: str, desk: LogicalDesk, prompt: str, lease_sequence: int | None = None) -> str:
        started, tick = datetime.now(timezone.utc), time.perf_counter()
        stage, role, logical_order = desk.stage, desk.role, desk.logical_order
        generation_event_id = None
        response = None
        with self._lock:
            attempt = 1 + sum(call.get("desk_id", f"{call['stage']}:{call['role']}") == desk.desk_id for call in self.calls)
            reservation = {"call_id": f"L{logical_order:03d}-A{attempt:03d}", "scope_id": desk.scope_id, "desk_id": desk.desk_id, "logical_order": logical_order, "attempt": attempt, "lease_sequence": lease_sequence}
            self.active_by_stage[stage] = self.active_by_stage.get(stage, 0) + 1
            self.max_active_by_stage[stage] = max(self.max_active_by_stage.get(stage, 0), self.active_by_stage[stage])
        try:
            config = self._generation_config(stage)
            call = {**reservation, "stage": stage, "role": role}
            if self.usage_gate:
                generation_event_id, _ = self.usage_gate.admit(client=self._clients[slot], model=self.config.model, prompt=prompt, config=config, key_slot_id=slot, call=call, max_output_tokens=self._max_output_tokens(stage))
            launch_sequence, scheduled, provider_start = self.pacer.wait()
            if self.usage_gate and generation_event_id:
                self.usage_gate.mark_dispatched(generation_event_id)
            response = self._clients[slot].models.generate_content(model=self.config.model, contents=prompt, config=config)
            text = getattr(response, "text", None)
            if not isinstance(text, str) or (stage not in {"writer", "revision"} and not text.strip()):
                if self.usage_gate and generation_event_id:
                    self.usage_gate.finish(event_id=generation_event_id, response=response, succeeded=False, error_code="EMPTY_RESPONSE")
                self._append(stage, role, slot, "FAIL", started, tick, prompt, "", response, "EMPTY_RESPONSE", None, reservation, launch_sequence, scheduled, provider_start)
                raise LiveCallError("EMPTY_RESPONSE", stage, role, slot, "provider returned no usable text")
            if self.usage_gate and generation_event_id:
                self.usage_gate.finish(event_id=generation_event_id, response=response, succeeded=True)
            self._append(stage, role, slot, "PASS", started, tick, prompt, text, response, reservation=reservation, launch_sequence=launch_sequence, scheduled=scheduled, provider_start=provider_start)
            return text
        except LiveCallError:
            raise
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "code", None)
            mapping = {400: "INVALID_REQUEST", 401: "AUTH_ERROR", 403: "PERMISSION_ERROR", 404: "MODEL_NOT_FOUND", 408: "TIMEOUT", 429: "RATE_LIMITED"}
            if error.__class__.__name__ in {"TokenAdmissionError", "UsageLedgerError"} and getattr(error, "args", None):
                error_class = str(error.args[0])
            else:
                error_class = mapping.get(status, "PROVIDER_5XX" if isinstance(status, int) and status >= 500 else "TIMEOUT" if isinstance(error, (TimeoutError, socket.timeout)) else "NETWORK_ERROR" if isinstance(error, ConnectionError) else "UNKNOWN_PROVIDER_ERROR")
            if self.usage_gate and generation_event_id:
                self.usage_gate.finish(event_id=generation_event_id, response=response, succeeded=False, error_code=error_class)
            self._append(stage, role, slot, "FAIL", started, tick, prompt, "", None, error_class, status if isinstance(status, int) else None, reservation, locals().get("launch_sequence"), locals().get("scheduled"), locals().get("provider_start"))
            raise LiveCallError(error_class, stage, role, slot, "provider request failed", status if isinstance(status, int) else None) from None
        finally:
            with self._lock:
                self.active_by_stage[stage] -= 1

    def _generation_config(self, stage: str):
        values = {"candidateCount": 1, "maxOutputTokens": self.config.json_limit if stage not in {"writer", "revision"} else self.config.prose_limit, "thinkingConfig": {"thinkingLevel": self.config.thinking_level}}
        if stage not in {"writer", "revision"}:
            values["responseMimeType"] = "application/json"
        return self._types.GenerateContentConfig(**values) if self._types else values

    def _max_output_tokens(self, stage: str) -> int:
        return self.config.prose_limit if stage in {"writer", "revision"} else self.config.json_limit

    def _append(self, stage: str, role: str, slot: str, status: str, started: datetime, tick: float, prompt: str, text: str, response: object | None, error_class: str | None = None, http_status: int | None = None, reservation: dict | None = None, launch_sequence: int | None = None, scheduled: float | None = None, provider_start: float | None = None) -> None:
        usage = getattr(response, "usage_metadata", None) if response else None
        with self._lock:
            previous = max((call.get("provider_started_monotonic", 0.0) for call in self.calls), default=0.0)
            self.calls.append({**(reservation or {}), "stage": stage, "role": role, "key_slot": slot, "status": status, "started_at": datetime.now(timezone.utc).isoformat(), "provider_started_at": datetime.now(timezone.utc).isoformat(), "provider_started_monotonic": provider_start, "scheduled_start_at": scheduled, "launch_sequence": launch_sequence, "launch_wait_ms": round(max(0, (provider_start or 0)-(scheduled or 0))*1000), "previous_launch_gap_ms": None if not previous or provider_start is None else round((provider_start-previous)*1000), "finished_at": datetime.now(timezone.utc).isoformat(), "latency_ms": round((time.perf_counter() - tick) * 1000), "input_characters": len(prompt), "output_characters": len(text), "prompt_tokens": getattr(usage, "prompt_token_count", None), "output_tokens": getattr(usage, "candidates_token_count", None), "total_tokens": getattr(usage, "total_token_count", None), "response_sha256": hashlib.sha256(text.encode()).hexdigest() if isinstance(text, str) else None, "error_class": error_class, "http_status": http_status, "provider_code": None})
            if self._telemetry_sink:
                self._telemetry_sink(self._telemetry_snapshot())

    def _telemetry_snapshot(self, scope_id: str | None = None) -> dict:
        calls = sorted(self.calls, key=lambda call: (call.get("logical_order", 0), call.get("attempt", 0)))
        snapshot = {"schema_version": 2, "provider": "gemini_developer_api", "model": self.config.model, "calls": calls, "contract_failures": self.contract_failures, "max_active_by_stage": self.max_active_by_stage}
        return scope_projection(snapshot, scope_id) if scope_id is not None else snapshot

    def telemetry(self, scope_id: str | None = None) -> dict:
        with self._lock:
            return self._telemetry_snapshot(scope_id)

    def restore_telemetry(self, telemetry: dict) -> None:
        with self._lock:
            self.calls = list(telemetry["calls"])
            self.contract_failures = list(telemetry.get("contract_failures", []))
            self.max_active_by_stage = dict(telemetry.get("max_active_by_stage", {}))

    def record_contract_failure(self, stage: str, role: str, slot: str | None = None, contract_code: str | None = None, scope_id: str | None = None, character_count: int | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            recent = next((call for call in reversed(self.calls) if call.get("scope_id") == scope_id and call["stage"] == stage and call["role"] == role), None)
            key_slot = slot or (recent or {}).get("key_slot") or "UNKNOWN"
            message = "sanitized planning merge contract failure" if stage == "planning_merge" else "sanitized prose contract failure" if stage in {"writer", "revision"} else "sanitized contract failure"
            event = {"event_id": f"CF{len(self.contract_failures)+1:03d}", "scope_id": scope_id, "desk_id": (recent or {}).get("desk_id", f"{stage}:{role}"), "stage": stage, "role": role, "key_slot": key_slot, "call_id": (recent or {}).get("call_id"), "contract_code": contract_code, "error_class": "CONTRACT_ERROR", "created_at": now, "message": message}
            if character_count is not None:
                event["character_count"] = character_count
            duplicate = any(item.get("call_id") == event["call_id"] and item.get("contract_code") == contract_code and item.get("stage") == stage and item.get("role") == role and item.get("scope_id") == scope_id for item in self.contract_failures)
            if duplicate:
                return
            self.contract_failures.append(event)
            if self._telemetry_sink:
                self._telemetry_sink(self._telemetry_snapshot())

    def close(self) -> None:
        for client in self._clients.values():
            if close := getattr(client, "close", None):
                close()
