# ARC 사용량 원장과 토큰 gate의 안전 동작을 검증한다.
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arc.usage import (
    JSON_OUTPUT_LIMIT,
    PROSE_OUTPUT_LIMIT,
    TokenAdmissionError,
    TokenGate,
    UsageLedger,
    UsageLedgerError,
    UsageNumbers,
    decide_admission,
    pacific_fields,
)


class _CountResponse:
    def __init__(self, total_tokens: int):
        self.total_tokens = total_tokens


class _Usage:
    def __init__(self, *, prompt: int | None = None, candidates: int | None = None, thoughts: int | None = None, total: int | None = None):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        if thoughts is not None:
            self.thoughts_token_count = thoughts
        self.total_token_count = total


class _Response:
    def __init__(self, usage: _Usage | None):
        self.usage_metadata = usage


class _Models:
    def __init__(self, tokens: int | Exception):
        self.tokens = tokens
        self.count_calls = 0
        self.generate_calls = 0

    def count_tokens(self, *, model: str, contents: str) -> _CountResponse:
        self.count_calls += 1
        if isinstance(self.tokens, Exception):
            raise self.tokens
        return _CountResponse(self.tokens)

    def generate_content(self, **_: object) -> _Response:
        self.generate_calls += 1
        return _Response(_Usage(prompt=10, candidates=20, thoughts=7, total=37))


class _Client:
    def __init__(self, tokens: int | Exception):
        self.models = _Models(tokens)


def _call() -> dict:
    return {"scope_id": "episode:episode_004", "call_id": "L317-A001", "lease_sequence": 179, "stage": "revision", "role": "canonical", "attempt": 1}


def test_pacific_fields_use_los_angeles_date_across_utc_midnight() -> None:
    _, _, date = pacific_fields(datetime(2026, 7, 13, 1, 30, tzinfo=timezone.utc))
    assert date == "2026-07-12"


def test_pacific_fields_handle_pst_and_pdt_offsets() -> None:
    assert pacific_fields(datetime(2026, 1, 13, 12, 0, tzinfo=timezone.utc))[1].endswith("-08:00")
    assert pacific_fields(datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))[1].endswith("-07:00")


@pytest.mark.parametrize(
    "input_tokens,max_output,allowed,reason,warning",
    [
        (179_999, PROSE_OUTPUT_LIMIT, True, None, None),
        (180_000, PROSE_OUTPUT_LIMIT, True, None, "INPUT_TOKEN_WARNING"),
        (199_999, JSON_OUTPUT_LIMIT, True, None, "INPUT_TOKEN_WARNING"),
        (200_000, JSON_OUTPUT_LIMIT, False, "INPUT_TOKEN_HARD_LIMIT", None),
        (199_999, PROSE_OUTPUT_LIMIT, True, None, "INPUT_TOKEN_WARNING"),
        (1, PROSE_OUTPUT_LIMIT + 1, False, "OUTPUT_TOKEN_LIMIT_EXCEEDED", None),
        (None, JSON_OUTPUT_LIMIT, False, "TOKEN_COUNT_UNAVAILABLE", None),
    ],
)
def test_token_admission_limits(input_tokens: int | None, max_output: int, allowed: bool, reason: str | None, warning: str | None) -> None:
    admission = decide_admission(input_tokens, max_output)
    assert admission.allowed is allowed
    assert admission.reason_code == reason
    assert admission.warning_code == warning


def test_token_gate_records_count_and_generation_usage_with_reasoning(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger)
    client = _Client(123)
    event_id, input_tokens = gate.admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K09", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)
    gate.mark_dispatched(event_id)
    response = client.models.generate_content()
    gate.finish(event_id=event_id, response=response, succeeded=True)

    assert input_tokens == 123
    status = ledger.status()
    assert status["totals"]["provider_requests"] == 2
    assert status["totals"]["generation_requests"] == 1
    assert status["totals"]["count_token_requests"] == 1
    assert status["totals"]["actual_input_tokens"] == 10
    assert status["totals"]["candidate_tokens"] == 20
    assert status["totals"]["reasoning_tokens"] == 7
    assert status["totals"]["combined_output_tokens"] == 27


def test_thoughts_zero_is_distinct_from_missing(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    call = _call()
    ledger.insert_generation(event_id="known-zero", key_slot_id="K01", model="model", call=call, input_tokens=1, max_output_tokens=1, admission=decide_admission(1, 1))
    ledger.mark_dispatched("known-zero")
    ledger.finish_generation(event_id="known-zero", usage=UsageNumbers(prompt_tokens=1, candidate_tokens=2, reasoning_tokens=0, total_tokens=3), status="SUCCEEDED")
    ledger.insert_generation(event_id="missing", key_slot_id="K01", model="model", call={**call, "call_id": "L001-A002"}, input_tokens=1, max_output_tokens=1, admission=decide_admission(1, 1))
    ledger.mark_dispatched("missing")
    ledger.finish_generation(event_id="missing", usage=UsageNumbers(prompt_tokens=1, candidate_tokens=2, reasoning_tokens=None, total_tokens=3), status="SUCCEEDED")

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        rows = conn.execute("SELECT event_id, reasoning_tokens, combined_output_tokens, status FROM usage_events ORDER BY event_id").fetchall()
    assert rows[0] == ("known-zero", 0, 2, "SUCCEEDED")
    assert rows[1] == ("missing", None, None, "SUCCEEDED")


def test_count_token_failure_blocks_generation(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger)
    client = _Client(RuntimeError("count failed"))

    with pytest.raises(TokenAdmissionError):
        gate.admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert client.models.generate_calls == 0
    assert ledger.status()["totals"]["generation_requests"] == 0


def test_blocked_generation_is_not_counted_as_provider_request(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger)
    client = _Client(200_000)

    with pytest.raises(TokenAdmissionError):
        gate.admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    status = ledger.status()
    assert status["totals"]["blocked_count"] == 1
    assert status["totals"]["generation_requests"] == 0
    assert status["totals"]["provider_requests"] == 1
    assert client.models.generate_calls == 0


def test_db_failure_blocks_before_count_or_generation(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "missing" / "usage.sqlite3")
    ledger.path.parent.write_text("not a directory", encoding="utf-8")
    gate = TokenGate(ledger)
    client = _Client(1)

    with pytest.raises(UsageLedgerError):
        gate.admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert client.models.count_calls == 0
    assert client.models.generate_calls == 0


def test_ledger_does_not_store_raw_prompt_response_or_secret(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    gate = TokenGate(ledger)
    client = _Client(10)
    event_id, _ = gate.admit(client=client, model="gemma-4-31b-it", prompt="SECRET_PROMPT key-abc", config={}, key_slot_id="K02", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)
    gate.mark_dispatched(event_id)
    gate.finish(event_id=event_id, response=_Response(_Usage(prompt=10, candidates=1, thoughts=1, total=12)), succeeded=True)

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT key_slot_id, model, call_id FROM usage_events").fetchall()
    assert {row[0] for row in rows} == {"K02"}
    assert "SECRET_PROMPT" not in json.dumps(rows)
    assert "key-abc" not in json.dumps(rows)


def test_legacy_import_is_idempotent_and_derives_safe_reasoning(tmp_path: Path) -> None:
    output = tmp_path / "pilot"
    output.mkdir()
    original = {
        "model": "gemma-4-31b-it",
        "calls": [
            {"call_id": "L001-A001", "lease_sequence": 1, "scope_id": "episode:episode_001", "desk_id": "episode:episode_001:writer:canonical", "stage": "writer", "role": "canonical", "attempt": 1, "key_slot": "K01", "status": "PASS", "prompt_tokens": 10, "output_tokens": 20, "total_tokens": 35},
            {"call_id": "L001-A002", "lease_sequence": 2, "scope_id": "episode:episode_001", "desk_id": "episode:episode_001:writer:canonical", "stage": "writer", "role": "canonical", "attempt": 2, "status": "FAIL", "prompt_tokens": 10, "output_tokens": 20, "total_tokens": 25},
        ],
    }
    telemetry = output / "pilot_live_calls.json"
    telemetry.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    before = telemetry.read_bytes()
    ledger = UsageLedger(tmp_path / "usage.sqlite3")

    first = ledger.import_pilot(output)
    second = ledger.import_pilot(output)

    assert first["imported"] == 2
    assert first["derived_reasoning"] == 1
    assert first["usage_unknown"] == 1
    assert first["key_slot_unknown"] == 1
    assert second["skipped"] == 2
    assert telemetry.read_bytes() == before


def test_usage_cli_db_check_and_json_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "usage.sqlite3"
    monkeypatch.setenv("ARC_USAGE_DB", str(db))
    check = subprocess.run([sys.executable, "-m", "arc", "usage", "db-check"], check=True, capture_output=True, text=True)
    assert json.loads(check.stdout)["schema_version"] == 1
    status = subprocess.run([sys.executable, "-m", "arc", "usage", "status", "--json"], check=True, capture_output=True, text=True)
    assert json.loads(status.stdout)["schema_version"] == 1
