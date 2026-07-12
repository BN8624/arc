# Phase 1 수직 루프의 시나리오와 정본 산출물을 검증한다.
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from arc.contracts import ContractError
from arc.mock_model import MockModelClient
from arc.pipeline import MockPipeline, status


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_work.json"


@pytest.mark.parametrize("scenario, verdict, final_exists, revisions", [("pass", "PASS", True, 0), ("revise", "REVISE_ONCE", True, 1), ("hold", "HOLD", False, 0)])
def test_scenarios(tmp_path: Path, scenario: str, verdict: str, final_exists: bool, revisions: int) -> None:
    client = MockModelClient(scenario, delays={"planning": 0.01, "review": 0.01, "memory": 0.01})
    run = tmp_path / scenario
    MockPipeline(client).run(FIXTURE, run, scenario)
    current = status(run)
    assert current["status"] == ("HOLD" if scenario == "hold" else "COMPLETE")
    assert current["review_verdict"] == verdict
    assert current["final_exists"] is final_exists
    assert current["revision_count"] == revisions
    if final_exists:
        source = run / ("revised.md" if scenario == "revise" else "draft.md")
        assert source.read_bytes() == (run / "final.md").read_bytes()
        assert current["memory_merged"] is True
        assert current["memory_applied"] is True
    else:
        assert current["memory_merged"] is False
        assert current["memory_applied"] is False


def test_context_separates_facts_and_future_plan(tmp_path: Path) -> None:
    source = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source["confirmed_facts"] = ["past"]
    source["rolling_plan"]["near_horizon"] = ["future"]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(source), encoding="utf-8")
    MockPipeline(MockModelClient("hold")).run(fixture, tmp_path / "run", "hold")
    context = json.loads((tmp_path / "run" / "context_packet.json").read_text(encoding="utf-8"))
    assert context["confirmed_facts"] == ["past"]
    assert context["rolling_plan"]["near_horizon"] == ["future"]


def test_malformed_review_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        MockPipeline(MockModelClient("pass", malformed_at="review_merge")).run(FIXTURE, tmp_path / "run", "pass")
    assert not (tmp_path / "run" / "review_decision.json").exists()


def test_memory_failure_preserves_final_and_resumes(tmp_path: Path) -> None:
    run = tmp_path / "run"
    with pytest.raises(RuntimeError):
        MockPipeline(MockModelClient("pass", fail_at="memory_merge")).run(FIXTURE, run, "pass")
    original = hashlib.sha256((run / "final.md").read_bytes()).hexdigest()
    client = MockModelClient("pass")
    MockPipeline(client).run(FIXTURE, run, "pass")
    assert hashlib.sha256((run / "final.md").read_bytes()).hexdigest() == original
    assert not any(stage == "writer" for stage, _, _ in client.calls)


def test_writer_and_revision_do_not_receive_raw_worker_outputs(tmp_path: Path) -> None:
    client = MockModelClient("revise")
    MockPipeline(client).run(FIXTURE, tmp_path / "run", "revise")
    prompts = {stage: prompt for stage, _, prompt in client.calls if stage in {"writer", "revision"}}
    assert "planning_workers" not in prompts["writer"]
    assert "review_workers" not in prompts["revision"]
