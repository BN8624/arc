# Phase 1 재개, no-op, 입력·산출물 안전성을 검증한다.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.mock_model import MockModelClient
from arc.pipeline import MockPipeline, PipelineError
from arc.storage import StorageError


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_work.json"


@pytest.mark.parametrize("scenario", ["pass", "hold"])
def test_completed_runs_are_noop(tmp_path: Path, scenario: str) -> None:
    run = tmp_path / scenario
    MockPipeline(MockModelClient(scenario)).run(FIXTURE, run, scenario)
    client = MockModelClient(scenario)
    result = MockPipeline(client).run(FIXTURE, run, scenario)
    assert result["no_op"] is True
    assert client.calls == []


def test_source_change_is_rejected(tmp_path: Path) -> None:
    run = tmp_path / "run"
    MockPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")
    source = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source["fixture_id"] = "changed"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(PipelineError):
        MockPipeline(MockModelClient("pass")).run(changed, run, "pass")


def test_tampered_artifact_is_rejected(tmp_path: Path) -> None:
    run = tmp_path / "run"
    MockPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")
    (run / "final.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(StorageError):
        MockPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")


def test_orphan_artifact_is_rejected_and_atomic_temps_do_not_remain(tmp_path: Path) -> None:
    run = tmp_path / "run"
    MockPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")
    assert not list(run.glob(".*.tmp"))
    (run / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StorageError):
        MockPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")


def test_scenario_change_is_rejected(tmp_path: Path) -> None:
    run = tmp_path / "run"
    MockPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")
    with pytest.raises(PipelineError):
        MockPipeline(MockModelClient("hold")).run(FIXTURE, run, "hold")


@pytest.mark.parametrize("failed_stage", ["planning", "writer", "review", "revision"])
def test_failure_resumes_without_repeating_completed_writer(tmp_path: Path, failed_stage: str) -> None:
    run = tmp_path / failed_stage
    with pytest.raises(RuntimeError):
        MockPipeline(MockModelClient("revise", fail_at=failed_stage)).run(FIXTURE, run, "revise")
    client = MockModelClient("revise")
    MockPipeline(client).run(FIXTURE, run, "revise")
    if failed_stage in {"review", "revision"}:
        assert not any(stage == "writer" for stage, _, _ in client.calls)
