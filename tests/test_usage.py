# ARC 사용량 원장과 토큰 gate의 안전 동작을 검증한다.
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import pytest

from arc.usage import (
    JSON_OUTPUT_LIMIT,
    PROSE_OUTPUT_LIMIT,
    TokenAdmissionError,
    TokenGate,
    UsageLedger,
    UsageLedgerError,
    UsageNumbers,
    backup_usage_db,
    decide_admission,
    pacific_fields,
    repair_preflight_collision,
)


class _CountResponse:
    def __init__(self, total_tokens: object):
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
    def __init__(self, tokens: object):
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
    def __init__(self, tokens: object):
        self.models = _Models(tokens)


class _NoCountClient:
    class _ModelsWithoutCounter:
        generate_calls = 0

    def __init__(self):
        self.models = self._ModelsWithoutCounter()


def _call() -> dict:
    return {"scope_id": "episode:episode_004", "call_id": "L317-A001", "lease_sequence": 179, "stage": "revision", "role": "canonical", "attempt": 1}


def _preflight_call(slot: str) -> dict:
    return {"scope_id": None, "desk_id": f"preflight:{slot}", "call_id": "L000-A001", "lease_sequence": None, "stage": "preflight", "role": slot, "attempt": 1}


def _rows_for_run(path: Path, usage_run_id: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT * FROM usage_events WHERE usage_run_id=? ORDER BY event_id", (usage_run_id,)).fetchall()


def _insert_repair_count(ledger: UsageLedger, *, slot: str = "K01", event_id: str | None = None, **overrides: object) -> str:
    call = _preflight_call(slot)
    values = {
        "event_id": event_id or f"preflight:{slot}:L000-A001:count_tokens:None",
        "request_kind": "count_tokens",
        "key_slot_id": slot,
        "model": "gemma-4-31b-it",
        "call": call,
        "provider_dispatched": True,
        "status": "FAILED",
        "usage_metadata_status": "MISSING",
        "error_code": "TOKEN_COUNT_UNAVAILABLE",
        "token_provenance": "measured",
    }
    values.update(overrides)
    ledger.insert_event(**values)
    return str(values["event_id"])


def _insert_repair_generation(ledger: UsageLedger, *, slot: str = "K01", event_id: str | None = None, call: dict | None = None, **overrides: object) -> str:
    values = {
        "event_id": event_id or f"preflight:{slot}:L000-A001:generate_content:None",
        "request_kind": "generate_content",
        "key_slot_id": slot,
        "model": "gemma-4-31b-it",
        "call": call or _preflight_call(slot),
        "gate_input_tokens": 15,
        "actual_input_tokens": 15,
        "provider_dispatched": True,
        "status": "SUCCEEDED",
        "usage_metadata_status": "KNOWN",
        "token_provenance": "provider",
    }
    values.update(overrides)
    ledger.insert_event(**values)
    return str(values["event_id"])


def _insert_repair_pair(ledger: UsageLedger, *, slot: str = "K01") -> tuple[str, str]:
    return _insert_repair_count(ledger, slot=slot), _insert_repair_generation(ledger, slot=slot)


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


def test_preflight_slots_with_same_call_id_get_distinct_usage_events(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger)

    first_event, _ = gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)
    second_event, _ = gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K02", call=_preflight_call("K02"), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert first_event != second_event
    status = ledger.status()
    assert status["totals"]["count_token_requests"] == 2
    assert status["totals"]["generation_requests"] == 0
    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        event_ids = [row[0] for row in conn.execute("SELECT event_id FROM usage_events ORDER BY event_id")]
    assert len(event_ids) == 4
    assert len(event_ids) == len(set(event_ids))


def test_preflight_same_slot_across_runs_gets_distinct_usage_events(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    first = TokenGate(ledger, usage_run_id="run-a", id_factory=lambda: "attempt-1")
    second = TokenGate(ledger, usage_run_id="run-b", id_factory=lambda: "attempt-1")

    first_event, _ = first.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)
    second_event, _ = second.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert first_event != second_event
    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        rows = conn.execute("SELECT usage_run_id, usage_attempt_id, request_group_id, request_kind FROM usage_events ORDER BY event_id").fetchall()
    assert {row[0] for row in rows} == {"run-a", "run-b"}
    assert {row[1] for row in rows} == {"attempt-1"}
    assert len(rows) == 4


def test_same_run_retries_get_distinct_attempts_and_shared_request_groups(tmp_path: Path) -> None:
    attempts = iter(["attempt-1", "attempt-2"])
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger, usage_run_id="run-a", id_factory=lambda: next(attempts))

    gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)
    gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        rows = conn.execute("SELECT usage_attempt_id, request_group_id, request_kind FROM usage_events ORDER BY event_id").fetchall()
    assert {row[0] for row in rows} == {"attempt-1", "attempt-2"}
    for attempt in ("attempt-1", "attempt-2"):
        group_rows = [row for row in rows if row[0] == attempt]
        assert {row[1] for row in group_rows} == {f"run-a:{attempt}"}
        assert {row[2] for row in group_rows} == {"count_tokens", "generate_content"}


def test_two_synthetic_preflight_runs_create_44_unique_event_ids(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    for run_id in ("run-a", "run-b"):
        attempts = iter(f"attempt-{slot}" for slot in range(1, 12))
        gate = TokenGate(ledger, usage_run_id=run_id, id_factory=lambda: next(attempts))
        for slot in range(1, 12):
            slot_id = f"K{slot:02d}"
            gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id=slot_id, call=_preflight_call(slot_id), max_output_tokens=JSON_OUTPUT_LIMIT)

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        event_ids = [row[0] for row in conn.execute("SELECT event_id FROM usage_events")]
    assert len(event_ids) == 44
    assert len(set(event_ids)) == 44


def test_collision_does_not_update_existing_terminal_row_or_dispatch(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3", now=lambda: datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
    gate = TokenGate(ledger, usage_run_id="run-a", id_factory=lambda: "same-attempt")
    first_event, _ = gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)
    gate.mark_dispatched(first_event)
    gate.finish(event_id=first_event, response=_Response(_Usage(prompt=15, candidates=1, thoughts=1, total=17)), succeeded=True)
    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        before = conn.execute("SELECT event_id, status, actual_input_tokens, provider_dispatched, updated_at FROM usage_events ORDER BY event_id").fetchall()

    collision_client = _Client(99)
    with pytest.raises(TokenAdmissionError, match="USAGE_EVENT_ID_COLLISION"):
        gate.admit(client=collision_client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert collision_client.models.count_calls == 0
    assert collision_client.models.generate_calls == 0
    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        after = conn.execute("SELECT event_id, status, actual_input_tokens, provider_dispatched, updated_at FROM usage_events ORDER BY event_id").fetchall()
    assert after == before


def test_successful_run_is_unchanged_when_next_run_count_fails(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    first = TokenGate(ledger, usage_run_id="run-a", id_factory=lambda: "attempt-1")
    event_id, _ = first.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)
    first.mark_dispatched(event_id)
    first.finish(event_id=event_id, response=_Response(_Usage(prompt=15, candidates=1, thoughts=1, total=17)), succeeded=True)
    before = _rows_for_run(db, "run-a")

    second_client = _Client(RuntimeError("count failed"))
    second = TokenGate(ledger, usage_run_id="run-b", id_factory=lambda: "attempt-1")
    with pytest.raises(TokenAdmissionError, match="TOKEN_COUNT_UNAVAILABLE"):
        second.admit(client=second_client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert _rows_for_run(db, "run-a") == before
    assert second_client.models.count_calls == 1
    assert second_client.models.generate_calls == 0
    with sqlite3.connect(db) as conn:
        run_b = conn.execute("SELECT request_kind, status, provider_dispatched FROM usage_events WHERE usage_run_id='run-b'").fetchall()
    assert run_b == [("count_tokens", "FAILED", 1)]
    totals = ledger.status()["totals"]
    assert totals["provider_requests"] == 3
    assert totals["count_token_requests"] == 2
    assert totals["generation_requests"] == 1


def test_runtime_event_rejects_mismatched_ownership(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger, usage_run_id="run-a", id_factory=lambda: "attempt-1")
    event_id, _ = gate.admit(client=_Client(15), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_preflight_call("K01"), max_output_tokens=JSON_OUTPUT_LIMIT)

    with pytest.raises(UsageLedgerError, match="USAGE_EVENT_ID_COLLISION"):
        ledger.mark_dispatched(event_id, usage_run_id="run-b", usage_attempt_id="attempt-9", request_group_id="run-b:attempt-9", request_kind="generate_content")

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        assert conn.execute("SELECT status, provider_dispatched FROM usage_events WHERE event_id=?", (event_id,)).fetchone() == ("PREPARED", 0)


def test_count_token_dispatch_is_marked_only_for_actual_endpoint(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger, counter=lambda *_: 15)
    gate.admit(client=_NoCountClient(), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    status = ledger.status()
    assert status["totals"]["provider_requests"] == 0
    assert status["totals"]["count_token_requests"] == 0


def test_count_token_is_dispatched_immediately_before_endpoint_call(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)

    class Models:
        generate_calls = 0

        def count_tokens(self, **_: object) -> _CountResponse:
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT status, provider_dispatched FROM usage_events").fetchone() == ("DISPATCHED", 1)
            return _CountResponse(0)

    client = type("Client", (), {"models": Models()})()
    _, input_tokens = TokenGate(ledger).admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    assert input_tokens == 0
    assert client.models.generate_calls == 0


def test_missing_count_token_endpoint_blocks_without_provider_dispatch(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger)

    with pytest.raises(TokenAdmissionError, match="TOKEN_COUNT_UNAVAILABLE"):
        gate.admit(client=_NoCountClient(), model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    status = ledger.status()
    assert status["totals"]["provider_requests"] == 0
    assert status["totals"]["failed_count"] == 1


@pytest.mark.parametrize("token_value", [None, -1, True])
def test_invalid_injected_count_blocks_without_provider_dispatch(tmp_path: Path, token_value: object) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger, counter=lambda *_: token_value)
    client = _NoCountClient()

    with pytest.raises(TokenAdmissionError, match="TOKEN_COUNT_UNAVAILABLE"):
        gate.admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        row = conn.execute("SELECT status, provider_dispatched, actual_input_tokens, usage_metadata_status, error_code FROM usage_events").fetchone()
    assert row == ("FAILED", 0, None, "MISSING", "TOKEN_COUNT_UNAVAILABLE")
    assert client.models.generate_calls == 0


@pytest.mark.parametrize("token_value", [None, -1, True])
def test_invalid_provider_count_blocks_after_dispatch(tmp_path: Path, token_value: object) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger)
    client = _Client(token_value)

    with pytest.raises(TokenAdmissionError, match="TOKEN_COUNT_UNAVAILABLE"):
        gate.admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        row = conn.execute("SELECT status, provider_dispatched, actual_input_tokens, usage_metadata_status, error_code FROM usage_events").fetchone()
    assert row == ("FAILED", 1, None, "MISSING", "TOKEN_COUNT_UNAVAILABLE")
    assert client.models.count_calls == 1
    assert client.models.generate_calls == 0


def test_provider_count_response_without_token_field_blocks(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")

    class Models:
        generate_calls = 0

        def count_tokens(self, **_: object) -> object:
            return object()

    client = type("Client", (), {"models": Models()})()
    with pytest.raises(TokenAdmissionError, match="TOKEN_COUNT_UNAVAILABLE"):
        TokenGate(ledger).admit(client=client, model="gemma-4-31b-it", prompt="safe prompt", config={}, key_slot_id="K01", call=_call(), max_output_tokens=JSON_OUTPUT_LIMIT)

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        assert conn.execute("SELECT status, provider_dispatched, error_code FROM usage_events").fetchone() == ("FAILED", 1, "TOKEN_COUNT_UNAVAILABLE")
    assert client.models.generate_calls == 0


def test_terminal_usage_row_rejects_general_update(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    call = _call()
    ledger.insert_generation(event_id="event-1", key_slot_id="K01", model="model", call=call, input_tokens=1, max_output_tokens=1, admission=decide_admission(1, 1))
    ledger.mark_dispatched("event-1")
    ledger.finish_generation(event_id="event-1", usage=UsageNumbers(prompt_tokens=1), status="SUCCEEDED")

    with pytest.raises(UsageLedgerError, match="USAGE_EVENT_TERMINAL"):
        ledger.update_event("event-1", status="FAILED")


@pytest.mark.parametrize("expected", [{"SUCCEEDED"}, {"FAILED"}, None])
def test_terminal_row_rejects_expected_status_and_timestamp_only_updates(tmp_path: Path, expected: set[str] | None) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    ledger.insert_event(event_id="terminal", request_kind="count_tokens", key_slot_id="K01", model="model", call=_call(), provider_dispatched=False, status="SUCCEEDED", usage_metadata_status="KNOWN", token_provenance="measured")

    values = {"utc_dispatch_ts": "2030-01-01T00:00:00+00:00"} if expected is None else {"status": "FAILED"}
    with pytest.raises(UsageLedgerError, match="USAGE_EVENT_TERMINAL"):
        ledger.update_event("terminal", expected_statuses=expected, **values)


def test_atomic_state_transitions_and_missing_event(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    call = _call()
    for event_id in ("success", "failure"):
        ledger.insert_generation(event_id=event_id, key_slot_id="K01", model="model", call=call, input_tokens=1, max_output_tokens=1, admission=decide_admission(1, 1))

    with pytest.raises(UsageLedgerError, match="USAGE_EVENT_NOT_FOUND"):
        ledger.mark_dispatched("missing")
    ledger.mark_dispatched("success")
    with pytest.raises(UsageLedgerError, match="USAGE_EVENT_STATE_CONFLICT"):
        ledger.mark_dispatched("success")
    ledger.finish_generation(event_id="success", usage=UsageNumbers(prompt_tokens=1), status="SUCCEEDED")
    ledger.mark_dispatched("failure")
    ledger.finish_generation(event_id="failure", usage=UsageNumbers(), status="FAILED", error_code="PROVIDER_ERROR")

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        assert conn.execute("SELECT event_id, status FROM usage_events ORDER BY event_id").fetchall() == [("failure", "FAILED"), ("success", "SUCCEEDED")]


def test_concurrent_dispatch_has_exactly_one_winner(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    ledger.insert_generation(event_id="event", key_slot_id="K01", model="model", call=_call(), input_tokens=1, max_output_tokens=1, admission=decide_admission(1, 1))
    barrier = Barrier(2)

    def dispatch() -> str:
        barrier.wait()
        try:
            ledger.mark_dispatched("event")
            return "ok"
        except UsageLedgerError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: dispatch(), range(2)))
    assert results.count("ok") == 1
    assert results.count("USAGE_EVENT_STATE_CONFLICT") == 1


def test_concurrent_finish_has_exactly_one_winner(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    ledger.insert_generation(event_id="event", key_slot_id="K01", model="model", call=_call(), input_tokens=1, max_output_tokens=1, admission=decide_admission(1, 1))
    ledger.mark_dispatched("event")
    barrier = Barrier(2)

    def finish() -> str:
        barrier.wait()
        try:
            ledger.finish_generation(event_id="event", usage=UsageNumbers(prompt_tokens=1), status="SUCCEEDED")
            return "ok"
        except UsageLedgerError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: finish(), range(2)))
    assert results.count("ok") == 1
    assert results.count("USAGE_EVENT_TERMINAL") == 1


def test_preflight_collision_repair_uses_companion_generation(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    count_event, generation_event = _insert_repair_pair(ledger)

    dry_run = repair_preflight_collision(ledger)
    assert dry_run["repairable"] == 1
    assert dry_run["applied"] == 0
    assert dry_run["candidates"][0]["companion_event_id"] == generation_event
    backup = tmp_path / "backup.sqlite3"
    backup_usage_db(db, backup)
    applied = repair_preflight_collision(ledger, apply=True, backup_path=backup)
    assert applied["applied"] == 1
    assert applied["before"]["row_count"] == applied["after"]["row_count"] == 2
    assert applied["after"]["success_count"] == applied["before"]["success_count"] + 1
    assert applied["after"]["failed_count"] == applied["before"]["failed_count"] - 1
    assert applied["after"]["recorded_input_tokens"] == applied["before"]["recorded_input_tokens"] + 15
    second = repair_preflight_collision(ledger, apply=True, backup_path=backup)
    assert second["applied"] == 0
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT status, actual_input_tokens, usage_metadata_status, error_code, repair_provenance FROM usage_events WHERE event_id=?", (count_event,)).fetchone()
    assert row[:4] == ("SUCCEEDED", 15, "KNOWN", None)
    provenance = json.loads(row[4])
    assert provenance["repair_type"] == "issue40_preflight_collision"
    assert provenance["source_evidence"] == "companion_generation_gate_input"
    assert provenance["companion_event_id"] == generation_event
    assert provenance["repair_version"] == 1
    assert isinstance(provenance["applied_at"], str)


def test_preflight_collision_repair_requires_exactly_one_companion(tmp_path: Path) -> None:
    missing = UsageLedger(tmp_path / "missing.sqlite3")
    _insert_repair_count(missing)
    assert repair_preflight_collision(missing)["unresolved"] == 1

    ambiguous = UsageLedger(tmp_path / "ambiguous.sqlite3")
    _insert_repair_pair(ambiguous)
    _insert_repair_generation(ambiguous, event_id="duplicate-generation")
    result = repair_preflight_collision(ambiguous)
    assert result["repairable"] == 0
    assert result["unresolved_rows"][0]["reason_code"] == "COMPANION_GENERATION_AMBIGUOUS"


@pytest.mark.parametrize(
    "field,value",
    [
        ("key_slot_id", "K02"),
        ("call_id", "L000-A002"),
        ("role", "K02"),
        ("attempt", 2),
        ("model", "other-model"),
    ],
)
def test_preflight_collision_repair_rejects_companion_identity_mismatch(tmp_path: Path, field: str, value: object) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    _insert_repair_count(ledger)
    call = _preflight_call("K01")
    overrides: dict[str, object] = {}
    if field in call:
        call[field] = value
    else:
        overrides[field] = value
    _insert_repair_generation(ledger, call=call, **overrides)

    result = repair_preflight_collision(ledger)
    assert result["repairable"] == 0
    assert result["unresolved"] == 1


def test_preflight_collision_repair_rejects_invalid_evidence(tmp_path: Path) -> None:
    not_dispatched = UsageLedger(tmp_path / "not-dispatched.sqlite3")
    _insert_repair_count(not_dispatched, provider_dispatched=False)
    _insert_repair_generation(not_dispatched)
    assert repair_preflight_collision(not_dispatched)["unresolved_rows"][0]["reason_code"] == "COUNT_PROVIDER_NOT_DISPATCHED"

    invalid_tokens = UsageLedger(tmp_path / "invalid-tokens.sqlite3")
    _insert_repair_count(invalid_tokens)
    _insert_repair_generation(invalid_tokens, gate_input_tokens=-1, actual_input_tokens=-1)
    assert repair_preflight_collision(invalid_tokens)["unresolved"] == 1

    conflicting_repair = UsageLedger(tmp_path / "conflicting-repair.sqlite3")
    _insert_repair_count(conflicting_repair, repair_provenance=json.dumps({"repair_type": "other"}))
    _insert_repair_generation(conflicting_repair)
    assert repair_preflight_collision(conflicting_repair)["unresolved_rows"][0]["reason_code"] == "REPAIR_PROVENANCE_CONFLICT"


def test_preflight_collision_repair_leaves_unrelated_episode_row_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    _insert_repair_pair(ledger)
    ledger.insert_event(event_id="episode-count", request_kind="count_tokens", key_slot_id="K01", model="gemma-4-31b-it", call=_call(), provider_dispatched=True, status="FAILED", usage_metadata_status="MISSING", error_code="TOKEN_COUNT_UNAVAILABLE", token_provenance="measured")
    with sqlite3.connect(db) as conn:
        before = conn.execute("SELECT * FROM usage_events WHERE event_id='episode-count'").fetchone()
    backup = tmp_path / "backup.sqlite3"
    backup_usage_db(db, backup)

    assert repair_preflight_collision(ledger, apply=True, backup_path=backup)["applied"] == 1

    with sqlite3.connect(db) as conn:
        after = conn.execute("SELECT * FROM usage_events WHERE event_id='episode-count'").fetchone()
    assert after == before


def test_preflight_collision_repair_rolls_back_all_rows_on_state_conflict(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    first, _ = _insert_repair_pair(ledger, slot="K01")
    second, _ = _insert_repair_pair(ledger, slot="K02")
    backup = tmp_path / "backup.sqlite3"
    backup_usage_db(db, backup)
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER block_second_repair BEFORE UPDATE ON usage_events
            WHEN OLD.event_id='{second}'
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )

    with pytest.raises(UsageLedgerError, match="USAGE_REPAIR_STATE_CONFLICT"):
        repair_preflight_collision(ledger, apply=True, backup_path=backup)

    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT event_id, status, actual_input_tokens, repair_provenance FROM usage_events WHERE event_id IN (?, ?) ORDER BY event_id", (first, second)).fetchall()
    assert rows == [(first, "FAILED", None, None), (second, "FAILED", None, None)]


def test_usage_backup_returns_matching_fingerprints(tmp_path: Path) -> None:
    source = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(source)
    _insert_repair_pair(ledger)
    destination = tmp_path / "backup.sqlite3"

    result = backup_usage_db(source, destination)

    assert destination.is_file()
    assert result["integrity_check"] == "ok"
    assert result["source_fingerprint"]["schema_version"] == 2
    assert result["source_fingerprint"]["row_count"] == 2
    assert result["source_fingerprint"]["content_sha256"] == result["backup_fingerprint"]["content_sha256"]
    assert result["source_fingerprint"]["core_schema"] == result["backup_fingerprint"]["core_schema"]
    assert isinstance(result["backup_created_at"], str)


def test_usage_backup_rejects_missing_source_without_creating_database(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite3"
    with pytest.raises(UsageLedgerError, match="USAGE_DB_NOT_FOUND"):
        backup_usage_db(source, tmp_path / "backup.sqlite3")
    assert not source.exists()


def test_usage_backup_rejects_same_or_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "usage.sqlite3"
    UsageLedger(source).ensure()
    with pytest.raises(UsageLedgerError, match="USAGE_BACKUP_SOURCE_DESTINATION_SAME"):
        backup_usage_db(source, source)

    destination = tmp_path / "backup.sqlite3"
    destination.write_bytes(b"existing")
    with pytest.raises(UsageLedgerError, match="USAGE_BACKUP_EXISTS"):
        backup_usage_db(source, destination)
    assert destination.read_bytes() == b"existing"


def test_repair_rejects_invalid_unrelated_and_stale_backups(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    _insert_repair_pair(ledger)

    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not sqlite")
    with pytest.raises(UsageLedgerError, match="USAGE_REPAIR_BACKUP_INVALID"):
        repair_preflight_collision(ledger, apply=True, backup_path=invalid)

    unrelated = tmp_path / "unrelated.sqlite3"
    unrelated_ledger = UsageLedger(unrelated)
    _insert_repair_pair(unrelated_ledger, slot="K02")
    with pytest.raises(UsageLedgerError, match="USAGE_REPAIR_BACKUP_FINGERPRINT_MISMATCH"):
        repair_preflight_collision(ledger, apply=True, backup_path=unrelated)

    stale = tmp_path / "stale.sqlite3"
    backup_usage_db(db, stale)
    ledger.insert_event(event_id="unrelated", request_kind="generate_content", key_slot_id="K09", model="model", call=_call(), provider_dispatched=False, status="BLOCKED", usage_metadata_status="NOT_APPLICABLE", token_provenance="provider")
    with pytest.raises(UsageLedgerError, match="USAGE_REPAIR_BACKUP_FINGERPRINT_MISMATCH"):
        repair_preflight_collision(ledger, apply=True, backup_path=stale)


def test_repair_rejects_snapshot_created_after_repair(tmp_path: Path) -> None:
    db = tmp_path / "usage.sqlite3"
    ledger = UsageLedger(db)
    _insert_repair_pair(ledger)
    before = tmp_path / "before.sqlite3"
    backup_usage_db(db, before)
    assert repair_preflight_collision(ledger, apply=True, backup_path=before)["applied"] == 1
    after = tmp_path / "after.sqlite3"
    backup_usage_db(db, after)

    with pytest.raises(UsageLedgerError, match="USAGE_REPAIR_BACKUP_NOT_PRE_REPAIR"):
        repair_preflight_collision(ledger, apply=True, backup_path=after)


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
    assert ledger.status()["totals"]["usage_unknown_count"] == 1


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


def test_legacy_import_preserves_provider_dispatch_pacific_date(tmp_path: Path) -> None:
    output = tmp_path / "pilot"
    output.mkdir()
    (output / "pilot_live_calls.json").write_text(
        json.dumps(
            {
                "model": "gemma-4-31b-it",
                "calls": [
                    {
                        "call_id": "L001-A001",
                        "lease_sequence": 1,
                        "scope_id": "episode:episode_001",
                        "stage": "writer",
                        "role": "canonical",
                        "attempt": 1,
                        "key_slot": "K01",
                        "status": "PASS",
                        "provider_started_at": "2026-07-13T07:24:46.834740+00:00",
                        "prompt_tokens": 10,
                        "output_tokens": 20,
                        "total_tokens": 35,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ledger = UsageLedger(tmp_path / "usage.sqlite3", now=lambda: datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc))

    assert ledger.import_pilot(output)["imported"] == 1

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        row = conn.execute("SELECT utc_dispatch_ts, pacific_dispatch_ts, pacific_date FROM usage_events").fetchone()
    assert row[0] == "2026-07-13T07:24:46.834740+00:00"
    assert row[1] == "2026-07-13T00:24:46.834740-07:00"
    assert row[2] == "2026-07-13"


def test_legacy_reimport_refreshes_existing_dispatch_time(tmp_path: Path) -> None:
    output = tmp_path / "pilot"
    output.mkdir()
    telemetry = {
        "model": "gemma-4-31b-it",
        "calls": [
            {"call_id": "L001-A001", "lease_sequence": 1, "scope_id": "episode:episode_001", "stage": "writer", "role": "canonical", "attempt": 1, "key_slot": "K01", "status": "PASS", "provider_started_at": "2026-07-13T07:24:46.834740+00:00", "prompt_tokens": 10, "output_tokens": 20, "total_tokens": 35}
        ],
    }
    (output / "pilot_live_calls.json").write_text(json.dumps(telemetry), encoding="utf-8")
    ledger = UsageLedger(tmp_path / "usage.sqlite3", now=lambda: datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc))
    ledger.insert_event(event_id="legacy:L001-A001:1", request_kind="generate_content", key_slot_id="K01", model="gemma-4-31b-it", call=telemetry["calls"][0], provider_dispatched=True, status="SUCCEEDED", usage_metadata_status="KNOWN", token_provenance="legacy", legacy_imported=True)
    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        before = conn.execute("SELECT status, gate_input_tokens, actual_input_tokens, candidate_tokens, reasoning_tokens, provider_total_tokens, provider_dispatched, error_code FROM usage_events").fetchone()

    assert ledger.import_pilot(output)["skipped"] == 1

    with sqlite3.connect(tmp_path / "usage.sqlite3") as conn:
        assert conn.execute("SELECT pacific_dispatch_ts FROM usage_events").fetchone()[0] == "2026-07-13T00:24:46.834740-07:00"
        after = conn.execute("SELECT status, gate_input_tokens, actual_input_tokens, candidate_tokens, reasoning_tokens, provider_total_tokens, provider_dispatched, error_code FROM usage_events").fetchone()
    assert after == before


def test_legacy_reconciliation_rejects_nonlegacy_collision(tmp_path: Path) -> None:
    output = tmp_path / "pilot"
    output.mkdir()
    call = {"call_id": "L001-A001", "lease_sequence": 1, "scope_id": "episode:episode_001", "stage": "writer", "role": "canonical", "attempt": 1, "key_slot": "K01", "status": "PASS", "provider_started_at": "2026-07-13T07:24:46.834740+00:00"}
    (output / "pilot_live_calls.json").write_text(json.dumps({"model": "gemma-4-31b-it", "calls": [call]}), encoding="utf-8")
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    ledger.insert_event(event_id="legacy:L001-A001:1", request_kind="generate_content", key_slot_id="K01", model="gemma-4-31b-it", call=call, provider_dispatched=True, status="SUCCEEDED", usage_metadata_status="KNOWN", token_provenance="provider", legacy_imported=False)

    with pytest.raises(UsageLedgerError, match="USAGE_EVENT_ID_COLLISION"):
        ledger.import_pilot(output)


def test_usage_cli_db_check_and_json_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "usage.sqlite3"
    monkeypatch.setenv("ARC_USAGE_DB", str(db))
    check = subprocess.run([sys.executable, "-m", "arc", "usage", "db-check"], check=True, capture_output=True, text=True)
    assert json.loads(check.stdout)["schema_version"] == 2
    status = subprocess.run([sys.executable, "-m", "arc", "usage", "status", "--json"], check=True, capture_output=True, text=True)
    assert json.loads(status.stdout)["schema_version"] == 2
