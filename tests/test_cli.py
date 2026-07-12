# Phase 1 명령줄 인터페이스의 최소 동작을 검증한다.
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from arc.cli import classify_preflight


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_work.json"


def test_cli_run_and_status(tmp_path: Path) -> None:
    run = tmp_path / "run"
    command = [sys.executable, "-m", "arc", "mock-run", str(FIXTURE), "--output", str(run), "--scenario", "pass"]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(first.stdout)["status"] == "COMPLETE"
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert json.loads(second.stdout)["no_op"] is True
    state = subprocess.run([sys.executable, "-m", "arc", "mock-status", str(run)], check=True, capture_output=True, text=True)
    assert json.loads(state.stdout)["memory_merged"] is True
    assert json.loads(state.stdout)["memory_applied"] is True


def result(slot: str, error_class: str | None = None) -> dict:
    return {"slot": slot, "status": "PASS" if error_class is None else "FAIL", "error_class": error_class, "http_status": None, "latency_ms": 0}


@pytest.mark.parametrize(
    "errors,allowed,degraded",
    [([], True, False), (["PROVIDER_5XX"] * 9, True, True), (["RATE_LIMITED"] * 10, True, True), (["AUTH_ERROR"] * 10, True, True), (["PROVIDER_5XX"] * 11, False, False), (["MODEL_NOT_FOUND", "PROVIDER_5XX"], False, False), (["UNKNOWN_PROVIDER_ERROR", "PROVIDER_5XX"], False, False)],
)
def test_dynamic_pool_admission_is_not_a_healthy_key_threshold(errors: list[str], allowed: bool, degraded: bool) -> None:
    values = [result(f"K{index:02d}", error) for index, error in enumerate(errors, start=1)]
    if len(errors) < 11:
        values.append(result("K11"))
    admission = classify_preflight(values)
    assert admission["live_run_allowed"] is allowed
    assert admission["degraded_admission"] is degraded


def test_actual_preflight_shape_is_degraded_but_admitted() -> None:
    values = [result("K06"), result("K10")] + [result(f"K{index:02d}", "PROVIDER_5XX") for index in (1, 2, 3, 4, 5, 7, 8, 9, 11)]
    admission = classify_preflight(values)
    assert admission["live_run_allowed"] is True
    assert admission["degraded_admission"] is True
    assert len(admission["categories"]["PASS"]) == 2
    assert len(admission["categories"]["TRANSIENT"]) == 9
