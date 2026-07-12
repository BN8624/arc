# Phase 2 Gemma 키 풀과 비밀 없는 호출 추적을 제공한다.
from __future__ import annotations

import hashlib
import importlib.metadata
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


MODEL_NAME = "gemma-4-31b-it"
SLOT_MAP = {
    ("planning", "event"): "K01", ("planning", "protagonist_action"): "K02", ("planning", "relationship"): "K03", ("planning", "continuity"): "K04", ("planning", "readability_weight"): "K05", ("planning", "reader_payoff"): "K06", ("planning_merge", "merge"): "K07", ("writer", "canonical"): "K08",
    ("review", "causality"): "K09", ("review", "protagonist_agency"): "K10", ("review", "character_consistency"): "K11", ("review", "continuity"): "K01", ("review", "readability"): "K02", ("review", "narrative_weight"): "K03", ("review", "payoff_and_hook"): "K04", ("review_merge", "merge"): "K05", ("revision", "canonical"): "K06",
    ("memory", "confirmed_facts"): "K07", ("memory", "relationships"): "K08", ("memory", "conflicts_and_promises"): "K09", ("memory", "important_excerpts"): "K10", ("memory_merge", "merge"): "K11",
}


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
    timeout: int = 600
    json_limit: int = 8192
    prose_limit: int = 32768
    thinking_level: str = "high"

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "LiveConfig":
        env = os.environ if env is None else env
        try:
            config = cls(env.get("MODEL", ""), {f"K{i:02d}": env.get(f"GOOGLE_API_KEY_{i}", "") for i in range(1, 12)}, int(env.get("ARC_MAX_LIVE", "11")), int(env.get("ARC_REQUEST_TIMEOUT_SECONDS", "600")), int(env.get("ARC_JSON_MAX_OUTPUT_TOKENS", "8192")), int(env.get("ARC_PROSE_MAX_OUTPUT_TOKENS", "32768")), env.get("ARC_THINKING_LEVEL", "high"))
        except ValueError as error:
            raise LiveConfigError("numeric live configuration is invalid") from error
        config.validate()
        return config

    def validate(self) -> None:
        if self.model != MODEL_NAME:
            raise LiveConfigError("MODEL must be gemma-4-31b-it")
        if any(not value for value in self.keys.values()) or len(set(self.keys.values())) != 11:
            raise LiveConfigError("eleven distinct non-empty key slots are required")
        if not 1 <= self.max_live <= 11 or self.timeout <= 0 or not 1 <= self.json_limit <= 32768 or not 1 <= self.prose_limit <= 32768:
            raise LiveConfigError("live limits are invalid")
        if self.thinking_level not in {"low", "medium", "high"}:
            raise LiveConfigError("thinking level is invalid")


class GemmaPoolClient:
    def __init__(self, config: LiveConfig, client_factory: Callable[[str], object] | None = None):
        self.config, self._lock, self.calls = config, threading.Lock(), []
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
        slot, started, tick = (role if stage == "preflight" else SLOT_MAP[(stage, role)]), datetime.now(timezone.utc), time.perf_counter()
        with self._lock:
            self.active_by_stage[stage] = self.active_by_stage.get(stage, 0) + 1
            self.max_active_by_stage[stage] = max(self.max_active_by_stage.get(stage, 0), self.active_by_stage[stage])
        try:
            response = self._clients[slot].models.generate_content(model=self.config.model, contents=prompt, config=self._generation_config(stage))
            text = getattr(response, "text", None)
            if not isinstance(text, str) or not text.strip():
                raise LiveCallError("EMPTY_RESPONSE", stage, role, slot, "provider returned no usable text")
            self._append(stage, role, slot, "PASS", started, tick, prompt, text, response)
            return text
        except LiveCallError:
            raise
        except Exception as error:
            status = getattr(error, "status_code", None) or getattr(error, "code", None)
            mapping = {400: "INVALID_REQUEST", 401: "AUTH_ERROR", 403: "PERMISSION_ERROR", 404: "MODEL_NOT_FOUND", 408: "TIMEOUT", 429: "RATE_LIMITED"}
            error_class = mapping.get(status, "PROVIDER_5XX" if isinstance(status, int) and status >= 500 else "UNKNOWN_PROVIDER_ERROR")
            self._append(stage, role, slot, "FAIL", started, tick, prompt, "", None, error_class, status if isinstance(status, int) else None)
            raise LiveCallError(error_class, stage, role, slot, "provider request failed", status if isinstance(status, int) else None) from None
        finally:
            with self._lock:
                self.active_by_stage[stage] -= 1

    def _generation_config(self, stage: str):
        values = {"candidateCount": 1, "maxOutputTokens": self.config.json_limit if stage not in {"writer", "revision"} else self.config.prose_limit, "thinkingConfig": {"thinkingLevel": self.config.thinking_level}}
        if stage not in {"writer", "revision"}:
            values["responseMimeType"] = "application/json"
        return self._types.GenerateContentConfig(**values) if self._types else values

    def _append(self, stage: str, role: str, slot: str, status: str, started: datetime, tick: float, prompt: str, text: str, response: object | None, error_class: str | None = None, http_status: int | None = None) -> None:
        usage = getattr(response, "usage_metadata", None) if response else None
        with self._lock:
            self.calls.append({"call_id": f"C{len(self.calls) + 1:03d}", "stage": stage, "role": role, "key_slot": slot, "status": status, "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(), "latency_ms": round((time.perf_counter() - tick) * 1000), "input_characters": len(prompt), "output_characters": len(text), "prompt_tokens": getattr(usage, "prompt_token_count", None), "output_tokens": getattr(usage, "candidates_token_count", None), "total_tokens": getattr(usage, "total_token_count", None), "response_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None, "error_class": error_class, "http_status": http_status, "provider_code": None})

    def telemetry(self) -> dict:
        return {"schema_version": 1, "provider": "gemini_developer_api", "model": self.config.model, "calls": self.calls, "max_active_by_stage": self.max_active_by_stage}

    def restore_telemetry(self, telemetry: dict) -> None:
        self.calls = list(telemetry["calls"])
        self.max_active_by_stage = dict(telemetry.get("max_active_by_stage", {}))

    def record_contract_failure(self, stage: str, role: str, slot: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.calls.append({"call_id": f"C{len(self.calls) + 1:03d}", "stage": stage, "role": role, "key_slot": slot, "status": "FAIL", "started_at": now, "finished_at": now, "latency_ms": 0, "input_characters": 0, "output_characters": 0, "prompt_tokens": None, "output_tokens": None, "total_tokens": None, "response_sha256": None, "error_class": "CONTRACT_ERROR", "http_status": None, "provider_code": None})

    def close(self) -> None:
        for client in self._clients.values():
            if close := getattr(client, "close", None):
                close()
