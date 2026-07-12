# 다섯 회차 mock pilot의 순차 실행과 복구 계약을 검증한다.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.mock_model import MockModelClient
from arc.pilot import PilotError, PilotPipeline, pilot_status
from arc.pilot_contracts import validate_pilot_fixture
from arc.storage import StorageError


FIXTURE = Path(__file__).parent / "fixtures" / "pilot_synthetic_work.json"


def run(tmp_path: Path, scenario: str = "pass") -> tuple[MockModelClient, Path]:
    client = MockModelClient("pass")
    output = tmp_path / scenario
    PilotPipeline(client, scenario).run(FIXTURE, output)
    return client, output


def test_pass_pilot_runs_five_sequential_episodes_and_noops(tmp_path: Path) -> None:
    client, output = run(tmp_path)
    current = pilot_status(output)
    assert current["status"] == "COMPLETE"
    assert current["completed_episode_count"] == 5
    assert current["completed_transition_count"] == 4
    assert current["writer_call_count"] == 5
    assert current["acceptance_verdict"] == "PASS"
    assert current["memory_chain_valid"] is True and current["rolling_plan_adapted"] is True
    calls = len(client.calls)
    result = PilotPipeline(client, "pass").run(FIXTURE, output)
    assert result["no_op"] is True and len(client.calls) == calls


def test_episode_hold_stops_before_later_episode_sources(tmp_path: Path) -> None:
    client, output = run(tmp_path, "episode_hold")
    current = pilot_status(output)
    assert current["status"] == "HOLD"
    assert current["completed_episode_count"] == 2
    assert current["completed_transition_count"] == 2
    assert not (output / "episode_sources" / "episode_004.json").exists()
    calls = len(client.calls)
    assert PilotPipeline(client, "episode_hold").run(FIXTURE, output)["no_op"] is True
    assert len(client.calls) == calls


def test_pilot_hold_preserves_all_episodes_without_automatic_revision(tmp_path: Path) -> None:
    client, output = run(tmp_path, "pilot_hold")
    current = pilot_status(output)
    assert current["status"] == "HOLD"
    assert current["completed_episode_count"] == 5
    assert current["acceptance_verdict"] == "HOLD"
    assert current["revision_count"] == 0


def test_changed_pilot_input_and_root_tamper_fail_closed(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    changed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    changed["pilot_id"] = "changed"
    fixture = tmp_path / "changed.json"
    fixture.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(PilotError):
        PilotPipeline(MockModelClient("pass"), "pass").run(fixture, output)
    (output / "pilot_acceptance.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StorageError):
        pilot_status(output)


def test_interrupted_third_episode_resumes_without_rerunning_completed_episodes(tmp_path: Path) -> None:
    class InterruptedClient(MockModelClient):
        def __init__(self) -> None:
            super().__init__("pass")
            self.merges = 0
            self.fail = True

        def generate(self, *, stage: str, role: str, prompt: str) -> str:
            if stage == "planning_merge":
                self.merges += 1
                if self.fail and self.merges == 3:
                    raise RuntimeError("simulated third-episode interruption")
            return super().generate(stage=stage, role=role, prompt=prompt)

    client = InterruptedClient()
    output = tmp_path / "resume"
    with pytest.raises(RuntimeError):
        PilotPipeline(client, "pass").run(FIXTURE, output)
    assert pilot_status(output)["completed_episode_count"] == 2
    completed_writer_calls = sum(stage == "writer" for stage, _, _ in client.calls)
    client.fail = False
    PilotPipeline(client, "pass").run(FIXTURE, output)
    assert pilot_status(output)["status"] == "COMPLETE"
    assert sum(stage == "writer" for stage, _, _ in client.calls) == completed_writer_calls + 3


def test_pilot_fixture_rejects_duplicate_episode_ids() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["episode_ids"][4] = fixture["episode_ids"][3]
    with pytest.raises(Exception):
        validate_pilot_fixture(fixture)


def test_pilot_review_uses_all_seven_client_desks(tmp_path: Path) -> None:
    client, _ = run(tmp_path)
    roles = [role for stage, role, _ in client.calls if stage == "pilot_review"]
    assert len(roles) == 7
    assert set(roles) == {"readability", "character_consistency", "continuity", "rolling_plan_adaptation", "memory_correctness", "narrative_weight", "episode_to_episode_interest"}


def test_existing_transition_and_source_reconcile_without_rebuild(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transition_id = "episode_002_to_episode_003"
    manifest["status"] = "RUNNING"
    manifest["completed_transitions"].remove(transition_id)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    class SpyPilot(PilotPipeline):
        def _transition(self, *args, **kwargs):
            raise AssertionError("transition rebuilt")

    SpyPilot(MockModelClient("pass"), "pass").run(FIXTURE, output)
    assert transition_id in json.loads(manifest_path.read_text(encoding="utf-8"))["completed_transitions"]


def test_acceptance_partial_resumes_only_missing_dimension(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "RUNNING"
    manifest["acceptance_verdict"] = None
    for name in ("pilot_review_workers.json", "pilot_acceptance.json"):
        manifest["artifact_hashes"].pop(name)
        (output / name).unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    partial_client = MockModelClient("pass", malformed_at="pilot_review:continuity")
    with pytest.raises(Exception):
        PilotPipeline(partial_client, "pass").run(FIXTURE, output)
    completed = {role for stage, role, _ in partial_client.calls if stage == "pilot_review"} - {"continuity"}
    resume_client = MockModelClient("pass")
    PilotPipeline(resume_client, "pass").run(FIXTURE, output)
    resumed = [role for stage, role, _ in resume_client.calls if stage == "pilot_review"]
    assert completed.isdisjoint(resumed)
    assert resumed == ["continuity"]


def _transition_resume_state(tmp_path: Path) -> tuple[Path, dict, str]:
    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    transition_id = "episode_002_to_episode_003"
    manifest["status"] = "RUNNING"
    return output, manifest, transition_id


def test_transition_artifact_only_resume_does_not_rebuild(tmp_path: Path) -> None:
    output, manifest, transition_id = _transition_resume_state(tmp_path)
    manifest["completed_transitions"].remove(transition_id)
    manifest["artifact_hashes"].pop("episode_sources/episode_003.json")
    (output / "episode_sources" / "episode_003.json").unlink()
    (output / "pilot_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    class Spy(PilotPipeline):
        def _transition(self, *args, **kwargs):
            raise AssertionError("transition rebuilt")

    Spy(MockModelClient("pass"), "pass").run(FIXTURE, output)
    assert (output / "episode_sources" / "episode_003.json").exists()


def test_transition_and_source_resume_only_reconciles_manifest(tmp_path: Path) -> None:
    output, manifest, transition_id = _transition_resume_state(tmp_path)
    manifest["completed_transitions"].remove(transition_id)
    (output / "pilot_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    class Spy(PilotPipeline):
        def _transition(self, *args, **kwargs):
            raise AssertionError("transition rebuilt")

    Spy(MockModelClient("pass"), "pass").run(FIXTURE, output)
    assert transition_id in json.loads((output / "pilot_manifest.json").read_text(encoding="utf-8"))["completed_transitions"]


def test_completed_transition_is_not_reexecuted(tmp_path: Path) -> None:
    output, manifest, _ = _transition_resume_state(tmp_path)
    (output / "pilot_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    class Spy(PilotPipeline):
        def _transition(self, *args, **kwargs):
            raise AssertionError("transition rebuilt")

    Spy(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_corrupted_transition_hash_fails_closed(tmp_path: Path) -> None:
    output, manifest, transition_id = _transition_resume_state(tmp_path)
    (output / "transitions" / f"{transition_id}.json").write_text("{}", encoding="utf-8")
    (output / "pilot_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StorageError):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_corrupted_next_source_hash_fails_closed(tmp_path: Path) -> None:
    output, manifest, _ = _transition_resume_state(tmp_path)
    (output / "episode_sources" / "episode_003.json").write_text("{}", encoding="utf-8")
    (output / "pilot_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(StorageError):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_canonical_review_workers_resume_without_client_calls(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "RUNNING"
    manifest["acceptance_verdict"] = None
    manifest["artifact_hashes"].pop("pilot_acceptance.json")
    (output / "pilot_acceptance.json").unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = MockModelClient("pass")
    PilotPipeline(client, "pass").run(FIXTURE, output)
    assert not [call for call in client.calls if call[0] == "pilot_review"]
    assert pilot_status(output)["status"] == "COMPLETE"


def test_acceptance_exists_manifest_unfinalized_reconciles(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "RUNNING"
    manifest["acceptance_verdict"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = MockModelClient("pass")
    PilotPipeline(client, "pass").run(FIXTURE, output)
    assert client.calls == []
    assert pilot_status(output)["acceptance_verdict"] == "PASS"


def test_corrupted_canonical_review_workers_fails_closed(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    (output / "pilot_review_workers.json").write_text("[]", encoding="utf-8")
    with pytest.raises(StorageError):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_corrupted_pilot_acceptance_fails_closed(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    (output / "pilot_acceptance.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StorageError):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)
