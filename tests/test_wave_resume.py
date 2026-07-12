# Partial checkpoint를 사용한 wave 재개를 검증한다.
from __future__ import annotations

from pathlib import Path

import json
import pytest

from arc.pipeline import MockPipeline


class Client:
    def __init__(self) -> None:
        self.config = type("Config", (), {"max_live": 11})()
        self.calls: list[str] = []

    def generate(self, *, stage: str, role: str, prompt: str) -> str:
        self.calls.append(role)
        return '{"worker_id":"' + stage + '-' + role + '","role":"' + role + '","verdict":"OK","primary_finding":"f","primary_risk":"r","evidence_refs":["source:current_episode"],"proposal":{}}'


def test_planning_partial_skips_completed_desk(tmp_path: Path) -> None:
    client = Client()
    pipeline = MockPipeline(client, mode="live")
    roles = ["event", "relationship"]
    checkpoint = tmp_path / "planning_workers.partial.json"
    from arc.pipeline import WaveCheckpoint
    saved = WaveCheckpoint(checkpoint, "planning", {"context": "x"}, roles)
    saved.save("event", {"worker_id": "planning-event", "role": "event", "verdict": "OK", "primary_finding": "f", "primary_risk": "r", "evidence_refs": ["source:current_episode"], "proposal": {}})
    results = pipeline._wave("planning", roles, {"context": "x"}, tmp_path)
    assert client.calls == ["relationship"]
    assert [item["role"] for item in results] == roles


def test_review_partial_skips_completed_desk(tmp_path: Path) -> None:
    from arc.pipeline import WaveCheckpoint

    client = Client()
    pipeline = MockPipeline(client, mode="live")
    roles = ["causality", "continuity"]
    saved = WaveCheckpoint(tmp_path / "review_workers.partial.json", "review", {"draft": "x"}, roles)
    saved.save("causality", {"worker_id": "review-causality", "role": "causality", "verdict": "OK", "primary_finding": "f", "primary_risk": "r", "evidence_refs": ["source:current_episode"], "proposal": {}})
    results = pipeline._wave("review", roles, {"draft": "x"}, tmp_path)
    assert client.calls == ["continuity"]
    assert [item["role"] for item in results] == roles


def test_memory_partial_skips_completed_desk(tmp_path: Path) -> None:
    from arc.pipeline import WaveCheckpoint

    client = Client()
    pipeline = MockPipeline(client, mode="live")
    roles = ["confirmed_facts", "important_excerpts"]
    payload = {"episode_id": "E001", "final": "final", "memory_before": {"series_compass": "s", "world_rules": [], "characters": [], "confirmed_facts": [], "episode_summaries": [], "important_excerpts": [], "relationship_state": []}}
    saved = WaveCheckpoint(tmp_path / "memory_workers.partial.json", "memory", payload, roles)
    saved.save("confirmed_facts", {"worker_id": "memory-confirmed_facts", "role": "confirmed_facts", "verdict": "OK", "primary_finding": "f", "primary_risk": "r", "evidence_refs": ["source:current_episode"], "proposal": {}})
    results = pipeline._wave("memory", roles, payload, tmp_path)
    assert client.calls == ["important_excerpts"]
    assert [item["role"] for item in results] == roles


def test_terminal_error_preserves_other_successful_partial_results(tmp_path: Path) -> None:
    class TerminalClient(Client):
        def generate(self, *, stage: str, role: str, prompt: str) -> str:
            self.calls.append(role)
            if role == "relationship":
                return "{malformed"
            return super().generate(stage=stage, role=role, prompt=prompt)

    client = TerminalClient()
    pipeline = MockPipeline(client, mode="live")
    roles = ["event", "relationship", "continuity"]
    with pytest.raises(Exception):
        pipeline._wave("planning", roles, {"context": "x"}, tmp_path)

    partial = json.loads((tmp_path / "planning_workers.partial.json").read_text(encoding="utf-8"))
    assert set(partial["completed_desks"]) == {"planning:event", "planning:continuity"}
    assert "planning:relationship" not in partial["completed_desks"]


def test_live_runtime_rejects_legacy_manifest_before_provider_call(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_bytes((Path(__file__).parent / "fixtures" / "synthetic_work.json").read_bytes())
    (tmp_path / "manifest.json").write_text('{"source_hash":"x","scenario":null,"mode":"live"}', encoding="utf-8")
    client = Client()
    with pytest.raises(Exception, match="LEGACY_ROUTING_SCHEMA"):
        MockPipeline(client, mode="live").run(fixture, tmp_path, None)
    assert client.calls == []
