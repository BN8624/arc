# Phase 1 병렬 웨이브의 동시성과 결정론적 저장 순서를 검증한다.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.mock_model import MockModelClient
from arc.pipeline import MEMORY_ROLES, PLANNING_ROLES, REVIEW_ROLES, MockPipeline


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_work.json"


def test_parallel_waves_are_concurrent_and_ordered(tmp_path: Path) -> None:
    client = MockModelClient("pass", delays={"planning": 0.02, "review": 0.02, "memory": 0.02})
    run = tmp_path / "run"
    MockPipeline(client).run(FIXTURE, run, "pass")
    assert client.max_active >= 2
    assert client.max_active <= 11
    assert client.max_active_by_stage["planning"] >= 2
    assert client.max_active_by_stage["review"] >= 2
    assert client.max_active_by_stage["memory"] >= 2
    for filename, roles in [("planning_workers.json", PLANNING_ROLES), ("review_workers.json", REVIEW_ROLES), ("memory_workers.json", MEMORY_ROLES)]:
        values = json.loads((run / filename).read_text(encoding="utf-8"))
        assert [value["role"] for value in values] == roles


@pytest.mark.parametrize("stage", ["planning", "review", "memory"])
def test_worker_failure_does_not_create_wave_artifact(tmp_path: Path, stage: str) -> None:
    run = tmp_path / stage
    with pytest.raises(RuntimeError):
        MockPipeline(MockModelClient("pass", fail_at=stage)).run(FIXTURE, run, "pass")
    names = {"planning": "planning_workers.json", "review": "review_workers.json", "memory": "memory_workers.json"}
    assert not (run / names[stage]).exists()
