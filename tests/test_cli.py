# Phase 1 명령줄 인터페이스의 최소 동작을 검증한다.
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


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
