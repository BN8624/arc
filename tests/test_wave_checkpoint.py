# WaveCheckpoint의 무결성과 재개 계약을 검증한다.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.pipeline import PipelineError, WaveCheckpoint


def worker(role: str) -> dict:
    return {"worker_id": f"planning-{role}", "role": role, "verdict": "OK", "primary_finding": "finding", "primary_risk": "risk", "evidence_refs": ["source:current_episode"], "proposal": {"role": role}}


def test_checkpoint_accumulates_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "planning_workers.partial.json"
    checkpoint = WaveCheckpoint(path, "planning", {"context": "x"}, ["event", "relationship"])
    checkpoint.save("event", worker("event"))
    checkpoint.save("relationship", worker("relationship"))
    restored = WaveCheckpoint(path, "planning", {"context": "x"}, ["event", "relationship"])
    assert restored.result("event")["role"] == "event"
    assert restored.result("relationship")["role"] == "relationship"


@pytest.mark.parametrize("field,value", [("routing_schema_version", 1), ("stage", "review"), ("wave_input_hash", "bad"), ("expected_desks", ["planning:event"])])
def test_checkpoint_rejects_metadata_corruption(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "planning_workers.partial.json"
    checkpoint = WaveCheckpoint(path, "planning", {"context": "x"}, ["event", "relationship"])
    checkpoint.save("event", worker("event"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data[field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PipelineError):
        WaveCheckpoint(path, "planning", {"context": "x"}, ["event", "relationship"])


def test_checkpoint_rejects_unknown_desk(tmp_path: Path) -> None:
    path = tmp_path / "planning_workers.partial.json"
    checkpoint = WaveCheckpoint(path, "planning", {"context": "x"}, ["event"])
    with pytest.raises(PipelineError):
        checkpoint.save("unknown", worker("event"))


@pytest.mark.parametrize("field,value", [("routing_mode", "fixed_slot"), ("logical_order", 9), ("result_sha256", "bad")])
def test_checkpoint_rejects_completed_desk_corruption(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "planning_workers.partial.json"
    checkpoint = WaveCheckpoint(path, "planning", {"context": "x"}, ["event"])
    checkpoint.save("event", worker("event"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if field == "routing_mode":
        data[field] = value
    else:
        data["completed_desks"]["planning:event"][field] = value
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PipelineError):
        WaveCheckpoint(path, "planning", {"context": "x"}, ["event"])
