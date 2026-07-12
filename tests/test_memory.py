# Phase 1A 장기 기억 적용과 불변식을 독립적으로 검증한다.
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from arc.contracts import ContractError, apply_memory_update, validate_memory, validate_review
from arc.mock_model import MockModelClient
from arc.pipeline import MockPipeline, status


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_work.json"


def source() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def update() -> dict:
    return {"episode_id": "SYN001", "confirmed_facts_added": ["new fact"], "relationship_changes": ["new relationship"], "conflicts_resolved": ["synthetic resolved conflict"], "conflicts_opened": ["new conflict"], "promises_added": ["new promise"], "important_excerpts_added": ["new excerpt"], "episode_summary": "new summary", "required_next_episode_continuity": ["new continuity"], "evidence_refs": ["final.md"]}


def test_apply_preserves_full_memory_and_is_pure() -> None:
    original, change = source(), update()
    snapshot = copy.deepcopy(original)
    result = apply_memory_update(original, change)
    assert original == snapshot
    for field in ("fixture_id", "series_compass", "world_rules", "characters", "rolling_plan"):
        assert result[field] == original[field]
    assert result["confirmed_facts"][-1] == "new fact"
    assert result["relationship_state"][-1] == "new relationship"
    assert "synthetic resolved conflict" not in result["open_conflicts"]
    assert result["open_conflicts"][-1] == "new conflict"
    assert result["promises"][-1] == "new promise"
    assert result["important_excerpts"][-1] == "new excerpt"
    assert result["episode_summaries"][-1] == "new summary"
    assert result["required_next_episode_continuity"][-1] == "new continuity"
    assert result["last_completed_episode_id"] == "SYN001"
    assert result == apply_memory_update(original, change)


@pytest.mark.parametrize("field,value", [("conflicts_resolved", ["missing conflict"]), ("conflicts_opened", ["synthetic resolved conflict"]), ("confirmed_facts_added", ["synthetic existing fact"]), ("confirmed_facts_added", ["future plan"]), ("episode_summary", "synthetic existing summary")])
def test_apply_rejects_invalid_memory_changes(field: str, value: list[str]) -> None:
    original, change = source(), update()
    if value == ["future plan"]:
        original["rolling_plan"]["near_horizon"] = value
    change[field] = value
    with pytest.raises(ContractError):
        apply_memory_update(original, change)


@pytest.mark.parametrize("field,value", [("confirmed_facts_added", ["duplicate", "duplicate"]), ("evidence_refs", "final.md"), ("evidence_refs", ["draft.md"]), ("promises_added", "not-a-list")])
def test_memory_validation_rejects_type_duplicate_and_evidence(field: str, value: object) -> None:
    change = update()
    change[field] = value
    with pytest.raises(ContractError):
        validate_memory(change, "SYN001")


@pytest.mark.parametrize("value", [
    {"verdict": "UNKNOWN", "strengths_to_preserve": [], "required_changes": [], "evidence_refs": []},
    {"verdict": "PASS", "strengths_to_preserve": [], "required_changes": ["x"], "evidence_refs": []},
    {"verdict": "REVISE_ONCE", "strengths_to_preserve": [], "required_changes": [], "evidence_refs": []},
    {"verdict": "REVISE_ONCE", "strengths_to_preserve": [], "required_changes": ["x", "x"], "evidence_refs": []},
    {"verdict": "REVISE_ONCE", "strengths_to_preserve": [], "required_changes": ["a", "b", "c", "d"], "evidence_refs": []},
])
def test_review_contract_rejections(value: dict) -> None:
    with pytest.raises(ContractError):
        validate_review(value)


def test_resume_after_memory_merged_has_zero_model_calls(tmp_path: Path) -> None:
    class InterruptedPipeline(MockPipeline):
        def _commit(self, run_dir: Path, manifest: dict, filename: str, value: dict | list | str, stage: str, text: bool = False) -> None:
            super()._commit(run_dir, manifest, filename, value, stage, text)
            if stage == "MEMORY_MERGED":
                raise RuntimeError("simulated process interruption")

    run = tmp_path / "run"
    with pytest.raises(RuntimeError):
        InterruptedPipeline(MockModelClient("pass")).run(FIXTURE, run, "pass")
    final_hash = (run / "final.md").read_bytes()
    client = MockModelClient("pass")
    MockPipeline(client).run(FIXTURE, run, "pass")
    current = status(run)
    assert client.calls == []
    assert (run / "final.md").read_bytes() == final_hash
    assert current["status"] == "COMPLETE"
    assert current["memory_merged"] is True and current["memory_applied"] is True
