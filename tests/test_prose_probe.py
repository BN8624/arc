from __future__ import annotations
# Bounded prose live probe의 입력, 호출 경계, artifact 안전성을 검증한다.

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from arc.prose_probe import ProseProbeError, prose_live_probe_status, run_prose_live_probe, validate_probe_source
from arc.storage import read_json, write_json, write_text


@dataclass
class Config:
    model: str = "gemma-4-31b-it"


@dataclass
class Gate:
    usage_run_id: str = "probe-run"


class FakeClient:
    def __init__(self, writer: str, revision: str, fail_stage: str | None = None, duplicate: bool = False):
        self.config, self.usage_gate = Config(), Gate()
        self.responses, self.fail_stage, self.duplicate = {"writer": writer, "revision": revision}, fail_stage, duplicate
        self.calls, self.contract_failures = [], []

    def generate(self, *, stage, role, prompt):
        if stage == self.fail_stage:
            raise RuntimeError("transport")
        text = self.responses[stage]
        count = 2 if self.duplicate and stage == "writer" else 1
        for attempt in range(1, count + 1):
            self.calls.append({"stage": stage, "role": role, "status": "PASS", "call_id": f"{stage}-{attempt}", "key_slot": "K01", "lease_sequence": len(self.calls) + 1, "response_sha256": hashlib.sha256(text.encode()).hexdigest(), "output_characters": len(text)})
        return text

    def telemetry(self):
        return {"schema_version": 2, "model": self.config.model, "provider": "fake", "calls": self.calls, "contract_failures": self.contract_failures, "max_active_by_stage": {}}

    def record_contract_failure(self, stage, role, **kwargs):
        self.contract_failures.append({"stage": stage, "role": role, **kwargs})


def source_episode(tmp_path: Path) -> Path:
    root = tmp_path / "pilot"
    episode = root / "episodes" / "episode_002"
    write_json(root / "pilot_manifest.json", {"status": "HOLD", "active_episode_id": "episode_002"})
    hashes = {}
    hashes["context_packet.json"] = write_json(episode / "context_packet.json", {"episode_id": "episode_002"})
    hashes["episode_plan.json"] = write_json(episode / "episode_plan.json", {"episode_id": "episode_002", "immediate_objective": "a", "obstacle": "b", "protagonist_action": "c", "meaningful_change": "d", "episode_ending": "e", "selected_worker_ids": ["planning-event"], "continuity_constraints": []})
    hashes["draft.md"] = write_text(episode / "draft.md", "가" * 3474)
    hashes["draft_contract.json"] = write_json(episode / "draft_contract.json", {"character_count": 3474, "contract_code": "PROSE_UNDERLENGTH_REPAIRABLE", "verdict": "REVISE_REQUIRED"})
    hashes["review_decision.json"] = write_json(episode / "review_decision.json", {"verdict": "REVISE_ONCE", "strengths_to_preserve": [], "required_changes": ["expand"], "evidence_refs": ["draft_contract"]})
    write_json(episode / "manifest.json", {"episode_id": "episode_002", "status": "HOLD", "revision_attempt_state": "REJECTED", "revision_contract_code": "PROSE_TOO_SHORT", "artifact_hashes": hashes})
    return episode


def preflight(tmp_path: Path) -> Path:
    path = tmp_path / "preflight" / "preflight.json"
    write_json(path, {"status": "PASS", "live_run_allowed": True, "pass_slots": 1, "global_blocker_slots": 0, "unknown_slots": 0, "model": "gemma-4-31b-it"})
    return path


def persist_telemetry(output: Path, client: FakeClient) -> None:
    write_json(output / "prose_probe_calls.json", client.telemetry())


def test_probe_passes_with_exactly_one_valid_writer_and_revision_response(tmp_path):
    client = FakeClient("가" * 5200, "나" * 5400)
    output = tmp_path / "probe"
    result = run_prose_live_probe(source_episode(tmp_path), output, preflight(tmp_path), client)
    persist_telemetry(output, client)
    assert result["overall_status"] == "PASS"
    assert [item["actual_content_response_count"] for item in result["stages"]] == [1, 1]
    assert result["source_draft_character_count"] == 3474
    assert result["revision_safe_expansion"] == 1526
    assert prose_live_probe_status(output)["overall_status"] == "PASS"
    serialized = (output / "prose_probe.json").read_text() + (output / "prose_probe_calls.json").read_text()
    assert "가" * 100 not in serialized and "나" * 100 not in serialized


@pytest.mark.parametrize(
    "writer,revision",
    [("가" * 3999, "나" * 5200), ("가" * 5200, "나" * 3999), ("{}", "나" * 5200), ("가" * 5200, "```bad```")],
    ids=("writer-underlength", "revision-underlength", "writer-invalid-shape", "revision-forbidden-marker"),
)
def test_probe_classifies_contract_failures_as_not_proven(tmp_path, writer, revision):
    result = run_prose_live_probe(source_episode(tmp_path), tmp_path / "probe", preflight(tmp_path), FakeClient(writer, revision))
    assert result["overall_status"] == "NOT_PROVEN"


def test_probe_classifies_terminal_transport_failure_as_incomplete(tmp_path):
    result = run_prose_live_probe(source_episode(tmp_path), tmp_path / "probe", preflight(tmp_path), FakeClient("가" * 5200, "나" * 5200, fail_stage="writer"))
    assert result["overall_status"] == "INCOMPLETE"
    assert result["stages"] == []


def test_probe_blocks_duplicate_content_evidence(tmp_path):
    result = run_prose_live_probe(source_episode(tmp_path), tmp_path / "probe", preflight(tmp_path), FakeClient("가" * 5200, "나" * 5200, duplicate=True))
    assert result["overall_status"] == "SAFETY_BLOCKED"


def test_probe_rejects_invalid_source_before_client_use(tmp_path):
    episode = source_episode(tmp_path)
    manifest = read_json(episode / "manifest.json")
    manifest["revision_attempt_state"] = "COMPLETED"
    write_json(episode / "manifest.json", manifest)
    with pytest.raises(ProseProbeError, match="revision rejection"):
        validate_probe_source(episode)


def test_probe_rejects_hash_mismatch_and_unknown_artifact(tmp_path):
    episode = source_episode(tmp_path)
    (episode / "draft.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ProseProbeError, match="integrity"):
        validate_probe_source(episode)
    episode = source_episode(tmp_path / "second")
    (episode / "unknown.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ProseProbeError, match="integrity"):
        validate_probe_source(episode)


def test_probe_rejects_existing_output_before_provider_call(tmp_path):
    client = FakeClient("가" * 5200, "나" * 5200)
    output = tmp_path / "probe"
    output.mkdir()
    with pytest.raises(ProseProbeError, match="already exists"):
        run_prose_live_probe(source_episode(tmp_path), output, preflight(tmp_path), client)
    assert client.calls == []
