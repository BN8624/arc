# 다섯 회차 mock pilot의 순차 실행과 복구 계약을 검증한다.
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from arc.mock_model import MockModelClient
from arc.pilot import PilotError, PilotPipeline, pilot_status
from arc.evidence_candidates import EVIDENCE_CANDIDATE_CATALOG_VERSION
from arc.pilot_contracts import ACCEPTANCE_PROVIDER_CONTRACT_VERSION, validate_pilot_fixture
from arc.storage import StorageError
from arc.pipeline import WaveCheckpoint
from arc.pilot_contracts import PILOT_REVIEW_ROLES


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


def test_mock_transitions_are_schema_v2_with_accounted_adaptation(tmp_path: Path) -> None:
    client, output = run(tmp_path)
    assert sum(stage == "transition" for stage, _, _ in client.calls) == 4
    ids = json.loads(FIXTURE.read_text(encoding="utf-8"))["episode_ids"]
    counts = {"KEEP": 0, "CHANGE": 0, "DROP": 0, "ADD": 0}
    for episode_id, next_id in zip(ids, ids[1:]):
        transition = json.loads((output / "transitions" / f"{episode_id}_to_{next_id}.json").read_text(encoding="utf-8"))
        assert transition["schema_version"] == 2
        for decision in transition["adaptation_decisions"]:
            counts[decision["action"]] += 1
        next_source = json.loads((output / "episode_sources" / f"{next_id}.json").read_text(encoding="utf-8"))
        assert next_source["rolling_plan"] == transition["rolling_plan_after"]
        assert next_source["current_episode"] == transition["next_episode"]
        assert next_source["current_episode"]["required_role"] == transition["rolling_plan_after"]["immediate_horizon"][0]
    assert counts["CHANGE"] + counts["DROP"] + counts["ADD"] >= 1
    assert counts["CHANGE"] >= 1 and counts["DROP"] >= 1 and counts["ADD"] >= 1
    current = pilot_status(output)
    assert current["rolling_plan_adapted"] is True
    assert current["rolling_plan_adaptation_action_counts"] == counts
    assert current["legacy_transition_count"] == 0
    packet = json.loads((output / "pilot_evidence_packet.json").read_text(encoding="utf-8"))
    summary = packet["rolling_plan_adaptation"]
    assert summary["adaptation_proven"] is True
    assert summary["transition_count"] == summary["validated_transition_count"] == 4
    assert summary["action_counts"] == counts
    assert summary["non_keep_action_count"] == counts["CHANGE"] + counts["DROP"] + counts["ADD"]


class KeepOnlyTransitionClient(MockModelClient):
    def _response(self, stage: str, role: str, prompt: str) -> dict:
        if stage != "transition":
            return super()._response(stage, role, prompt)
        payload = json.loads(prompt)
        completed_id = payload["completed_episode_id"]
        plan = payload["rolling_plan"]
        candidate_id = payload["evidence_candidates"][0]["candidate_id"]
        decisions = [{"action": "KEEP", "horizon_before": horizon, "item_before": item, "horizon_after": horizon, "item_after": item, "reason": f"The {completed_id} outcome confirms this item.", "evidence_candidate_ids": [candidate_id]} for horizon in ("immediate_horizon", "near_horizon") for item in plan[horizon]]
        return {"next_episode": {"episode_id": payload["next_episode_id"], "importance": "ordinary", "required_role": plan["immediate_horizon"][0]}, "rolling_plan_after": {"immediate_horizon": list(plan["immediate_horizon"]), "near_horizon": list(plan["near_horizon"])}, "adaptation_decisions": decisions, "continuity_satisfied": [], "continuity_deferred": list(payload["required_next_episode_continuity"]), "adaptation_summary": f"Completed {completed_id} confirmed the existing plan without changes."}


def test_keep_only_pilot_completes_but_is_not_adaptation_proven(tmp_path: Path) -> None:
    client = KeepOnlyTransitionClient("pass")
    output = tmp_path / "keep-only"
    PilotPipeline(client, "pass").run(FIXTURE, output)

    current = pilot_status(output)
    assert current["status"] == "COMPLETE"
    assert current["completed_transition_count"] == 4
    assert current["rolling_plan_adapted"] is False
    counts = current["rolling_plan_adaptation_action_counts"]
    assert counts["CHANGE"] == counts["DROP"] == counts["ADD"] == 0 and counts["KEEP"] == 12
    packet = json.loads((output / "pilot_evidence_packet.json").read_text(encoding="utf-8"))
    assert packet["rolling_plan_adaptation"]["adaptation_proven"] is False
    assert packet["rolling_plan_adaptation"]["non_keep_action_count"] == 0


def test_legacy_v1_transition_is_diagnosed_and_not_adaptation_evidence(tmp_path: Path) -> None:
    from arc.storage import write_json

    _, output = run(tmp_path)
    transition_path = output / "transitions" / "episode_001_to_episode_002.json"
    transition = json.loads(transition_path.read_text(encoding="utf-8"))
    legacy = {"schema_version": 1, "completed_episode_id": "episode_001", "next_episode_id": "episode_002", "transition_input_hash": transition["transition_input_hash"], "next_source_hash": transition["next_source_hash"], "next_episode": transition["next_episode"], "rolling_plan_after": transition["rolling_plan_after"], "continuity_satisfied": [], "continuity_deferred": list(transition["continuity_deferred"]), "adaptation_summary": "legacy synthetic summary", "evidence_refs": list(transition["evidence_refs"])}
    digest = write_json(transition_path, legacy)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_hashes"]["transitions/episode_001_to_episode_002.json"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    current = pilot_status(output)
    assert current["legacy_transition_count"] == 1
    assert current["rolling_plan_adapted"] is False

    manifest["status"] = "RUNNING"
    manifest["completed_transitions"].remove("episode_001_to_episode_002")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(Exception, match="LEGACY_SYNTHETIC_TRANSITION"):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_mock_pilot_output_contains_no_synthetic_markers(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    for path in output.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for marker in ("synthetic transition toward", "synthetic pilot role", "Synthetic plan adapts", "synthetic continuity evidence", "Evaluate pilot dimension:"):
            assert marker not in text, f"{marker} in {path}"


def test_mock_pass_acceptance_is_grounded_schema_v2(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    acceptance = json.loads((output / "pilot_acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["schema_version"] == 2
    assert acceptance["rubric_version"] == 1
    assert acceptance["verdict"] == "PASS"
    assert acceptance["critical_findings"] == []
    assert len(acceptance["strengths_to_preserve"]) == 7
    workers = json.loads((output / "pilot_review_workers.json").read_text(encoding="utf-8"))
    derived = [{"dimension": worker["role"], **strength} for worker in workers for strength in worker["proposal"]["strengths"]]
    assert acceptance["strengths_to_preserve"] == derived
    assert acceptance["evidence_refs"] == sorted(set(acceptance["evidence_refs"]))
    assert "pilot_evidence_packet.json" not in acceptance["evidence_refs"]
    packet = json.loads((output / "pilot_evidence_packet.json").read_text(encoding="utf-8"))
    assert packet["acceptance_rubric_version"] == 1
    assert packet["acceptance_provider_contract_version"] == ACCEPTANCE_PROVIDER_CONTRACT_VERSION
    assert packet["evidence_candidate_catalog_version"] == EVIDENCE_CANDIDATE_CATALOG_VERSION
    catalog_refs = {entry["ref"] for entry in packet["acceptance_evidence_catalog"]}
    assert len(catalog_refs) == 34
    assert set(acceptance["evidence_refs"]) <= catalog_refs
    for worker in workers:
        assert worker["proposal"]["coverage_refs"]
        assert all(result["evidence"] for result in worker["proposal"]["criterion_results"])


def test_mock_hold_acceptance_is_grounded(tmp_path: Path) -> None:
    _, output = run(tmp_path, "pilot_hold")
    acceptance = json.loads((output / "pilot_acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["schema_version"] == 2
    assert acceptance["verdict"] == "HOLD"
    assert acceptance["dimension_results"]["continuity"] == "HOLD"
    assert len(acceptance["critical_findings"]) == 1
    finding = acceptance["critical_findings"][0]
    assert finding["dimension"] == "continuity"
    assert finding["criterion_id"] == "continuity.required_obligations"
    assert finding["evidence"]
    assert len(acceptance["strengths_to_preserve"]) == 7
    assert all(strength["evidence"] for strength in acceptance["strengths_to_preserve"])


def test_status_reports_grounded_acceptance_counts(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    current = pilot_status(output)
    assert current["acceptance_grounded"] is True
    assert current["acceptance_schema_version"] == 2
    assert current["acceptance_rubric_version"] == 1
    assert current["acceptance_dimension_count"] == 7
    assert current["acceptance_criterion_count"] == 21
    assert current["acceptance_hold_criterion_count"] == 0
    assert current["acceptance_strength_count"] == 7
    assert current["acceptance_evidence_ref_count"] >= 1
    assert current["acceptance_grounding_reason"] is None


def test_status_reports_grounded_hold_acceptance(tmp_path: Path) -> None:
    _, output = run(tmp_path, "pilot_hold")
    current = pilot_status(output)
    assert current["status"] == "HOLD"
    assert current["acceptance_grounded"] is True
    assert current["acceptance_hold_criterion_count"] == 1


def test_status_query_does_not_mutate_artifacts(tmp_path: Path) -> None:
    _, output = run(tmp_path)
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    pilot_status(output)
    after = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    assert before == after


def _legacy_acceptance() -> dict:
    return {"verdict": "PASS", "dimension_results": {role: "PASS" for role in PILOT_REVIEW_ROLES}, "critical_findings": [], "strengths_to_preserve": ["legacy strength"], "evidence_refs": ["pilot_evidence_packet.json"]}


def test_legacy_generic_acceptance_is_diagnosed_not_upgraded(tmp_path: Path) -> None:
    from arc.storage import write_json

    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_hashes"]["pilot_acceptance.json"] = write_json(output / "pilot_acceptance.json", _legacy_acceptance())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    current = pilot_status(output)
    assert current["checkpoint_integrity"] != "CORRUPT"
    assert current["acceptance_grounded"] is False
    assert current["acceptance_grounding_reason"] == "LEGACY_GENERIC_ACCEPTANCE"
    assert current["acceptance_schema_version"] is None

    manifest["status"] = "RUNNING"
    manifest["acceptance_verdict"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = (output / "pilot_acceptance.json").read_bytes()
    with pytest.raises(Exception, match="legacy generic acceptance"):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)
    assert (output / "pilot_acceptance.json").read_bytes() == before


def _tampered_running_resume(tmp_path: Path, scenario: str, name: str, mutate) -> Path:
    from arc.storage import write_json

    _, output = run(tmp_path, scenario)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    value = json.loads((output / name).read_text(encoding="utf-8"))
    mutate(value)
    manifest["artifact_hashes"][name] = write_json(output / name, value)
    manifest["status"] = "RUNNING"
    manifest["acceptance_verdict"] = None
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return output


def _flip_first_criterion(workers: list) -> None:
    workers[0]["proposal"]["criterion_results"][0]["result"] = "HOLD"


def _fabricate_excerpt(workers: list) -> None:
    workers[0]["proposal"]["criterion_results"][0]["evidence"][0]["excerpt"] = "fabricated excerpt that never appears"


def _drop_coverage_ref(workers: list) -> None:
    workers[0]["proposal"]["coverage_refs"] = workers[0]["proposal"]["coverage_refs"][:-1]


def _drop_criterion(workers: list) -> None:
    workers[0]["proposal"]["criterion_results"].pop()


def _swap_worker_role(workers: list) -> None:
    workers[0]["role"], workers[1]["role"] = workers[1]["role"], workers[0]["role"]


@pytest.mark.parametrize("mutate", [_flip_first_criterion, _fabricate_excerpt, _drop_coverage_ref, _drop_criterion, _swap_worker_role])
def test_tampered_worker_artifacts_fail_closed(tmp_path: Path, mutate) -> None:
    output = _tampered_running_resume(tmp_path, "pass", "pilot_review_workers.json", mutate)
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def _flip_dimension_result(acceptance: dict) -> None:
    acceptance["dimension_results"]["readability"] = "HOLD"


def _rewrite_strength(acceptance: dict) -> None:
    acceptance["strengths_to_preserve"][0]["strength"] = "A rewritten strength statement."


def _drop_evidence_ref(acceptance: dict) -> None:
    acceptance["evidence_refs"] = acceptance["evidence_refs"][:-1]


def _flip_schema_only(acceptance: dict) -> None:
    legacy = _legacy_acceptance()
    acceptance.clear()
    acceptance.update(legacy, schema_version=2)


@pytest.mark.parametrize("mutate", [_flip_dimension_result, _rewrite_strength, _drop_evidence_ref, _flip_schema_only])
def test_tampered_acceptance_artifacts_fail_closed(tmp_path: Path, mutate) -> None:
    output = _tampered_running_resume(tmp_path, "pass", "pilot_acceptance.json", mutate)
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def _drop_critical_finding(acceptance: dict) -> None:
    acceptance["critical_findings"] = []


def test_tampered_hold_acceptance_without_finding_fails_closed(tmp_path: Path) -> None:
    output = _tampered_running_resume(tmp_path, "pilot_hold", "pilot_acceptance.json", _drop_critical_finding)
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pilot_hold").run(FIXTURE, output)


def _bump_rubric_version(packet: dict) -> None:
    packet["acceptance_rubric_version"] = 999


def test_tampered_packet_rubric_version_fails_closed(tmp_path: Path) -> None:
    output = _tampered_running_resume(tmp_path, "pass", "pilot_evidence_packet.json", _bump_rubric_version)
    with pytest.raises(Exception, match="pilot evidence packet"):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)
    current = pilot_status(output)
    assert current["acceptance_grounded"] is False
    assert current["acceptance_grounding_reason"] == "ACCEPTANCE_VALIDATION_FAILED"


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


def _acceptance_restart(tmp_path: Path) -> tuple[Path, dict]:
    _, output = run(tmp_path)
    manifest_path = output / "pilot_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "RUNNING"
    manifest["acceptance_verdict"] = None
    for name in ("pilot_review_workers.json", "pilot_acceptance.json"):
        manifest["artifact_hashes"].pop(name)
        (output / name).unlink()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return output, manifest


def _review_input(manifest: dict) -> dict:
    return {"pilot_id": manifest["pilot_id"], "mode": manifest["mode"], "scenario": manifest["scenario"], "episode_ids": manifest["episode_ids"], "evidence_packet_hash": manifest["artifact_hashes"]["pilot_evidence_packet.json"], "acceptance_rubric_version": 1, "acceptance_provider_contract_version": ACCEPTANCE_PROVIDER_CONTRACT_VERSION, "evidence_candidate_catalog_version": EVIDENCE_CANDIDATE_CATALOG_VERSION}


def _review_worker(role: str) -> dict:
    return {"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": "f", "primary_risk": "r", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"dimension_result": "PASS", "critical_finding": None}}


def test_malformed_acceptance_partial_fails_closed(tmp_path: Path) -> None:
    output, _ = _acceptance_restart(tmp_path)
    (output / "pilot_review_workers.partial.json").write_text("{", encoding="utf-8")
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_acceptance_partial_result_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    output, manifest = _acceptance_restart(tmp_path)
    checkpoint = WaveCheckpoint(output / "pilot_review_workers.partial.json", "pilot_review", _review_input(manifest), PILOT_REVIEW_ROLES)
    checkpoint.save("readability", _review_worker("readability"))
    partial = json.loads((output / "pilot_review_workers.partial.json").read_text(encoding="utf-8"))
    partial["completed_desks"]["pilot_review:readability"]["result_sha256"] = "bad"
    (output / "pilot_review_workers.partial.json").write_text(json.dumps(partial), encoding="utf-8")
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_acceptance_partial_unknown_dimension_fails_closed(tmp_path: Path) -> None:
    output, manifest = _acceptance_restart(tmp_path)
    checkpoint = WaveCheckpoint(output / "pilot_review_workers.partial.json", "pilot_review", _review_input(manifest), PILOT_REVIEW_ROLES)
    checkpoint.save("readability", _review_worker("readability"))
    partial = json.loads((output / "pilot_review_workers.partial.json").read_text(encoding="utf-8"))
    partial["completed_desks"]["pilot_review:unknown"] = partial["completed_desks"].pop("pilot_review:readability")
    (output / "pilot_review_workers.partial.json").write_text(json.dumps(partial), encoding="utf-8")
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


def test_acceptance_partial_duplicate_dimension_fails_closed(tmp_path: Path) -> None:
    output, manifest = _acceptance_restart(tmp_path)
    checkpoint = WaveCheckpoint(output / "pilot_review_workers.partial.json", "pilot_review", _review_input(manifest), PILOT_REVIEW_ROLES)
    checkpoint.save("readability", _review_worker("readability"))
    partial = json.loads((output / "pilot_review_workers.partial.json").read_text(encoding="utf-8"))
    partial["expected_desks"].append("pilot_review:readability")
    (output / "pilot_review_workers.partial.json").write_text(json.dumps(partial), encoding="utf-8")
    with pytest.raises(Exception):
        PilotPipeline(MockModelClient("pass"), "pass").run(FIXTURE, output)


@pytest.mark.parametrize("scenario", ["pass", "episode_hold", "pilot_hold"])
def test_complete_rerun_is_noop(tmp_path: Path, scenario: str) -> None:
    client, output = run(tmp_path, scenario)
    before = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in output.rglob("*") if path.is_file()}
    calls = len(client.calls)

    class Spy(PilotPipeline):
        def _transition(self, *args, **kwargs):
            raise AssertionError("transition rerun")

    result = Spy(client, scenario).run(FIXTURE, output)
    after = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in output.rglob("*") if path.is_file()}
    assert result["no_op"] is True
    assert len(client.calls) == calls
    assert before == after


def test_episode_hold_rerun_is_noop(tmp_path: Path) -> None:
    test_complete_rerun_is_noop(tmp_path, "episode_hold")


def test_pilot_hold_rerun_is_noop(tmp_path: Path) -> None:
    test_complete_rerun_is_noop(tmp_path, "pilot_hold")
