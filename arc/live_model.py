# Phase 2 Gemma 키 풀과 비밀 없는 호출 추적을 제공한다.
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import threading
import time
import socket
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


MODEL_NAME = "gemma-4-31b-it"
SLOT_MAP = {
    ("planning", "event"): "K01", ("planning", "protagonist_action"): "K02", ("planning", "relationship"): "K03", ("planning", "continuity"): "K04", ("planning", "readability_weight"): "K05", ("planning", "reader_payoff"): "K06", ("planning_merge", "merge"): "K07", ("writer", "canonical"): "K08",
    ("review", "causality"): "K09", ("review", "protagonist_agency"): "K10", ("review", "character_consistency"): "K11", ("review", "continuity"): "K01", ("review", "readability"): "K02", ("review", "narrative_weight"): "K03", ("review", "payoff_and_hook"): "K04", ("review_merge", "merge"): "K05", ("revision", "canonical"): "K06",
    ("memory", "confirmed_facts"): "K07", ("memory", "relationships"): "K08", ("memory", "conflicts_and_promises"): "K09", ("memory", "important_excerpts"): "K10", ("memory_merge", "merge"): "K11",
}
LIVE_LOGICAL_ORDER = {key: index for index, key in enumerate(SLOT_MAP, start=1)}
MIN_HEALTHY_SLOTS = 7


def slot_number(slot: str) -> int:
    return int(slot[1:])


def build_health_assignment(healthy_slots: list[str]) -> dict[tuple[str, str], str]:
    slots = sorted(healthy_slots, key=slot_number)
    if len(slots) < MIN_HEALTHY_SLOTS:
        raise LiveConfigError("fewer than seven healthy slots")
    return {key: slots[(order - 1) % len(slots)] for key, order in LIVE_LOGICAL_ORDER.items()}


class LiveConfigError(ValueError):
    """Live configuration is incomplete or unsafe."""


class LiveCallError(RuntimeError):
    def __init__(self, error_class: str, stage: str, role: str, slot: str, message: str, http_status: int | None = None, provider_code: str | None = None):
        super().__init__(message)
        self.error_class, self.stage, self.role, self.slot = error_class, stage, role, slot
        self.http_status, self.provider_code = http_status, provider_code


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


class GemmaPoolClient:
    def __init__(self, config: LiveConfig, client_factory: Callable[[str], object] | None = None, assignments: dict[tuple[str, str], str] | None = None):
        self.config, self._lock, self.calls = config, threading.Lock(), []
        self.pacer = LaunchPacer(config.launch_interval)
        self.assignments = assignments or SLOT_MAP
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

    def generate(self, *, stage: str, role: str, prompt: str) -> str:
        slot, started, tick = (role if stage == "preflight" else self.assignments[(stage, role)]), datetime.now(timezone.utc), time.perf_counter()
        logical_order = 0 if stage == "preflight" else LIVE_LOGICAL_ORDER[(stage, role)]
        with self._lock:
            attempt = 1 + sum(call["stage"] == stage and call["role"] == role for call in self.calls)
            reservation = {"call_id": f"L{logical_order:03d}-A{attempt:03d}", "logical_order": logical_order, "attempt": attempt}
            self.active_by_stage[stage] = self.active_by_stage.get(stage, 0) + 1
            self.max_active_by_stage[stage] = max(self.max_active_by_stage.get(stage, 0), self.active_by_stage[stage])
        try:
            launch_sequence, scheduled, provider_start = self.pacer.wait()
            response = self._clients[slot].models.generate_content(model=self.config.model, contents=prompt, config=self._generation_config(stage))
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                self._append(stage, role, slot, "FAIL", started, tick, prompt, "", response, "EMPTY_RESPONSE", None, reservation, launch_sequence, scheduled, provider_start)
                raise LiveCallError("EMPTY_RESPONSE", stage, role, slot, "provider returned no usable text")
            self._append(stage, role, slot, "PASS", started, tick, prompt, text, response, reservation=reservation, launch_sequence=launch_sequence, scheduled=scheduled, provider_start=provider_start)
            return text
        except LiveCallError:
            raise
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "code", None)
            mapping = {400: "INVALID_REQUEST", 401: "AUTH_ERROR", 403: "PERMISSION_ERROR", 404: "MODEL_NOT_FOUND", 408: "TIMEOUT", 429: "RATE_LIMITED"}
            error_class = mapping.get(status, "PROVIDER_5XX" if isinstance(status, int) and status >= 500 else "TIMEOUT" if isinstance(error, (TimeoutError, socket.timeout)) else "NETWORK_ERROR" if isinstance(error, ConnectionError) else "UNKNOWN_PROVIDER_ERROR")
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

    def _append(self, stage: str, role: str, slot: str, status: str, started: datetime, tick: float, prompt: str, text: str, response: object | None, error_class: str | None = None, http_status: int | None = None, reservation: dict | None = None, launch_sequence: int | None = None, scheduled: float | None = None, provider_start: float | None = None) -> None:
        usage = getattr(response, "usage_metadata", None) if response else None
        with self._lock:
            previous = max((call.get("provider_started_monotonic", 0.0) for call in self.calls), default=0.0)
            self.calls.append({**(reservation or {}), "stage": stage, "role": role, "key_slot": slot, "status": status, "started_at": datetime.now(timezone.utc).isoformat(), "provider_started_at": datetime.now(timezone.utc).isoformat(), "provider_started_monotonic": provider_start, "scheduled_start_at": scheduled, "launch_sequence": launch_sequence, "launch_wait_ms": round(max(0, (provider_start or 0)-(scheduled or 0))*1000), "previous_launch_gap_ms": None if not previous or provider_start is None else round((provider_start-previous)*1000), "finished_at": datetime.now(timezone.utc).isoformat(), "latency_ms": round((time.perf_counter() - tick) * 1000), "input_characters": len(prompt), "output_characters": len(text), "prompt_tokens": getattr(usage, "prompt_token_count", None), "output_tokens": getattr(usage, "candidates_token_count", None), "total_tokens": getattr(usage, "total_token_count", None), "response_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None, "error_class": error_class, "http_status": http_status, "provider_code": None})

    def telemetry(self) -> dict:
        calls = sorted(self.calls, key=lambda call: (call.get("logical_order", 0), call.get("attempt", 0)))
        return {"schema_version": 2, "provider": "gemini_developer_api", "model": self.config.model, "calls": calls, "contract_failures": self.contract_failures, "max_active_by_stage": self.max_active_by_stage}

    def restore_telemetry(self, telemetry: dict) -> None:
        self.calls = list(telemetry["calls"])
        self.contract_failures = list(telemetry.get("contract_failures", []))
        self.max_active_by_stage = dict(telemetry.get("max_active_by_stage", {}))

    def record_contract_failure(self, stage: str, role: str, slot: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.contract_failures.append({"event_id": f"CF{len(self.contract_failures)+1:03d}", "stage": stage, "role": role, "key_slot": slot, "error_class": "CONTRACT_ERROR", "created_at": now, "message": "sanitized contract failure"})

    def close(self) -> None:
        for client in self._clients.values():
            if close := getattr(client, "close", None):
                close()
