# 파일럿 live runtime scope와 telemetry 계약을 검증한다.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from arc.contracts import ContractError, PROSE_FORBIDDEN_MARKERS, PROSE_MAX_CHARACTERS, PROSE_MIN_CHARACTERS, PROSE_REPAIRABLE_MIN_CHARACTERS, validate_draft_prose, validate_prose
from arc.mock_model import acceptance_review_response, transition_adapter_response
from arc.live_model import AtomicTelemetryStore, GemmaPoolClient, LiveCallError, LiveConfig, LogicalDesk, MODEL_NAME, RoutingStateStore
from arc.pipeline import PLANNING_ROLES, MockPipeline, WaveCheckpoint, status
from arc.evidence_candidates import EVIDENCE_CANDIDATE_CATALOG_VERSION
from arc.pilot_contracts import ACCEPTANCE_GENERIC_QUESTION_MARKER, ACCEPTANCE_PROVIDER_CONTRACT_VERSION, PILOT_REVIEW_ROLES
from arc.pilot import PilotError, PilotPipeline, _transition_candidate_catalog, classify_episode_projection, episode_projection_document, inspect_pilot_checkpoint, live_telemetry_checkpoint, reconcile_live_telemetry_projections, reconcile_pilot_checkpoint
from arc.storage import StorageError, read_json, write_json


PILOT_FIXTURE = Path("tests/fixtures/pilot_synthetic_work.json")


class _Response:
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = None


class _Models:
    def __init__(self, owner: "_Provider"):
        self.owner = owner

    def generate_content(self, *, model: str, contents: str, config: dict) -> _Response:
        self.owner.prompts.append(contents)
        return _Response(f'{{"ok": true, "prompt": "{contents}"}}')


class _Provider:
    def __init__(self, slot: str):
        self.slot = slot
        self.models = _Models(self)
        self.prompts: list[str] = []
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _PilotModels:
    def __init__(self, owner: "_PilotProvider"):
        self.owner = owner

    def generate_content(self, *, model: str, contents: str, config: dict) -> _Response:
        if "Input JSON:\n" in contents:
            stage = contents.split("Stage: ", 1)[1].split("\n", 1)[0]
            role = contents.split("Role: ", 1)[1].split("\n", 1)[0]
            payload = json.loads(contents.split("Input JSON:\n", 1)[1])
        else:
            payload = json.loads(contents)
            stage = payload.get("stage", "pilot_review")
            role = payload.get("role") or payload["dimension"]
        marker = f"{stage}:{role}"
        self.owner.root.provider_calls.append((self.owner.slot, marker, contents))
        if marker == self.owner.root.fail_once_at and marker not in self.owner.root.failed:
            self.owner.root.failed.add(marker)
            error = RuntimeError("injected transient")
            error.status_code = self.owner.root.fail_status_code
            raise error
        return _Response(self.owner.root.response(stage, role, payload))


class _PilotProvider:
    def __init__(self, root: "_PilotProviderRoot", slot: str):
        self.root = root
        self.slot = slot
        self.models = _PilotModels(self)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _PilotProviderRoot:
    def __init__(self, fail_once_at: str | None = None, hold_episode: str | None = None, fail_status_code: int = 500, hold_dimension: str | None = None, wrong_plan_once: bool = False, wrong_plan_episode: str | None = None, short_writer_once_episode: str | None = None, writer_text_once_episode: str | None = None, writer_text: str | None = None, repairable_writer_once_episode: str | None = None, review_pass_on_repairable: bool = False, short_revision_once_episode: str | None = None, revision_text: str | None = None, transition_malformed_episode: str | None = None):
        self.transition_malformed_episode = transition_malformed_episode
        self.fail_once_at = fail_once_at
        self.hold_episode = hold_episode
        self.fail_status_code = fail_status_code
        self.hold_dimension = hold_dimension
        self.wrong_plan_once = wrong_plan_once
        self.wrong_plan_episode = wrong_plan_episode
        self.short_writer_once_episode = short_writer_once_episode
        self.writer_text_once_episode = writer_text_once_episode
        self.writer_text = writer_text
        self.repairable_writer_once_episode = repairable_writer_once_episode
        self.review_pass_on_repairable = review_pass_on_repairable
        self.short_revision_once_episode = short_revision_once_episode
        self.revision_text = revision_text
        self.malformed_once_at: str | None = None
        self.failed: set[str] = set()
        self.malformed: set[str] = set()
        self.provider_calls: list[tuple[str, str, str]] = []

    def factory(self, key: str) -> _PilotProvider:
        return _PilotProvider(self, key.replace("key-", "K"))

    def response(self, stage: str, role: str, payload: dict) -> str:
        episode_id = payload.get("episode_id") or payload.get("context", {}).get("episode_id") or "SYN001"
        if stage in {"planning", "review", "memory"}:
            evidence = ["final.md"] if stage == "memory" else ["source:current_episode"]
            return json.dumps({"worker_id": f"{stage}-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": evidence, "proposal": {"role": role}})
        if stage == "pilot_review":
            marker = f"{stage}:{role}"
            if marker == self.malformed_once_at and marker not in self.malformed:
                self.malformed.add(marker)
                return "{malformed"
            return json.dumps(acceptance_review_response(payload, hold=role == self.hold_dimension), ensure_ascii=False)
        if stage == "planning_merge":
            if self.wrong_plan_once and "planning_merge:merge" not in self.malformed and (self.wrong_plan_episode is None or episode_id == self.wrong_plan_episode):
                self.malformed.add("planning_merge:merge")
                return json.dumps({"episode_id": "wrong_episode", "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]})
            return json.dumps({"episode_id": episode_id, "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]})
        if stage == "writer":
            marker = f"writer:{episode_id}"
            if self.writer_text_once_episode == episode_id and marker not in self.malformed:
                self.malformed.add(marker)
                return self.writer_text if self.writer_text is not None else ""
            if self.short_writer_once_episode == episode_id and marker not in self.malformed:
                self.malformed.add(marker)
                return "short prose"
            if self.repairable_writer_once_episode == episode_id and marker not in self.malformed:
                self.malformed.add(marker)
                return "A" * 3500
            return ("A synthetic live episode sentence. " * 160)[:4800]
        if stage == "review_merge":
            verdict = "HOLD" if episode_id == self.hold_episode else "PASS"
            if verdict == "HOLD":
                return json.dumps({"verdict": verdict, "strengths_to_preserve": ["synthetic agency"], "required_changes": [], "evidence_refs": ["draft.md"]})
            if payload.get("draft_contract", {}).get("verdict") == "REVISE_REQUIRED" and not self.review_pass_on_repairable:
                return json.dumps({"verdict": "REVISE_ONCE", "strengths_to_preserve": ["synthetic agency"], "required_changes": ["Preserve strengths, events, and causality while rewriting the whole draft.", "Do not add a new central conflict or pad with repeated sentences.", "Produce one coherent 5000 to 7000 character prose passage."], "evidence_refs": ["draft.md"]})
            return json.dumps({"verdict": verdict, "strengths_to_preserve": ["synthetic agency"], "required_changes": [], "evidence_refs": ["draft.md"]})
        if stage == "revision":
            marker = f"revision:{episode_id}"
            if self.revision_text is not None:
                return self.revision_text
            if self.short_revision_once_episode == episode_id and marker not in self.malformed:
                self.malformed.add(marker)
                return "B" * 3500
            return ("A revised synthetic live episode sentence. " * 150)[:4800]
        if stage == "memory_merge":
            return json.dumps({"episode_id": episode_id, "confirmed_facts_added": [f"synthetic fact {episode_id}"], "relationship_changes": [f"synthetic relationship {episode_id}"], "conflict_ids_resolved": [], "conflicts_opened": [f"synthetic opened conflict {episode_id}"], "promises_added": [f"synthetic promise {episode_id}"], "important_excerpts_added": [f"synthetic excerpt {episode_id}"], "episode_summary": f"synthetic episode summary {episode_id}", "required_next_episode_continuity": [f"synthetic continuity {episode_id}"], "evidence_refs": ["final.md"]})
        if stage == "transition":
            if self.transition_malformed_episode == payload["completed_episode_id"]:
                return "{malformed transition"
            return json.dumps(transition_adapter_response(payload), ensure_ascii=False)
        raise RuntimeError(f"unknown live stage: {stage}:{role}")


def _config(key_count: int = 11) -> LiveConfig:
    return LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i:02d}" for i in range(1, key_count + 1)}, launch_interval=0.0)


def test_transition_payload_pins_candidate_selection_contract(tmp_path):
    episode_id = "episode_007"
    next_id = "episode_008"
    episode_dir = tmp_path / "episodes" / episode_id
    episode_dir.mkdir(parents=True)
    (episode_dir / "final.md").write_text("An exact final excerpt for the transition artifact.", encoding="utf-8")
    write_json(episode_dir / "episode_plan.json", {"objective": "A safe JSON excerpt."})
    write_json(episode_dir / "memory_update.json", {"summary": "A memory update excerpt."})
    write_json(episode_dir / "memory_after.json", {"summary": "A memory after excerpt."})

    payload = PilotPipeline(object(), scenario="pass", mode="mock")._transition_payload(
        tmp_path,
        {"pilot_id": "pilot-test", "episode_ids": [episode_id, next_id]},
        episode_id,
        next_id,
        {"rolling_plan": {"immediate_horizon": ["next item"], "near_horizon": []}, "required_next_episode_continuity": []},
        0,
    )
    contract = payload["candidate_selection_contract"]

    assert isinstance(contract, str) and contract
    assert payload["evidence_candidate_catalog_version"] == 1
    assert payload["evidence_candidates"]
    assert "at least one evidence_candidate_ids" in contract
    assert "using only candidate_id values from evidence_candidates" in contract
    assert "do not edit them, create new IDs" in contract
    assert "Select candidate IDs before writing the reason" in contract
    assert "unknown or fabricated candidate ID is a terminal contract failure" in contract
    assert "evidence" not in payload["strict_output_schema"]["adaptation_decisions"][0]
    assert "ref" not in payload["strict_output_schema"]["adaptation_decisions"][0]
    assert "excerpt" not in payload["strict_output_schema"]["adaptation_decisions"][0]


def test_transition_payload_pins_continuity_contract(tmp_path):
    episode_id = "episode_007"
    next_id = "episode_008"
    episode_dir = tmp_path / "episodes" / episode_id
    episode_dir.mkdir(parents=True)
    (episode_dir / "final.md").write_text("An exact final excerpt for the transition artifact.", encoding="utf-8")
    write_json(episode_dir / "episode_plan.json", {"objective": "A safe JSON excerpt."})
    write_json(episode_dir / "memory_update.json", {"summary": "A memory update excerpt."})
    write_json(episode_dir / "memory_after.json", {"summary": "A memory after excerpt."})

    payload = PilotPipeline(object(), scenario="pass", mode="mock")._transition_payload(
        tmp_path,
        {"pilot_id": "pilot-test", "episode_ids": [episode_id, next_id]},
        episode_id,
        next_id,
        {"rolling_plan": {"immediate_horizon": ["next item"], "near_horizon": []}, "required_next_episode_continuity": []},
        0,
    )
    contract = payload["continuity_contract"]

    assert isinstance(contract, str) and contract
    assert "Partition the provided required_next_episode_continuity list exactly" in contract
    assert "every item must appear exactly once" in contract
    assert "copied character-for-character unchanged" in contract
    assert "continuity_satisfied holds items fulfilled by the completed episode's final prose or memory results" in contract
    assert "continuity_deferred holds items still pending, which are carried to the next episode source" in contract
    assert "Never add, rewrite, merge, split, or omit items" in contract
    assert "Never insert memory facts, relationships, or any string that is not in required_next_episode_continuity into either list." in contract
    assert "The two lists must not overlap" in contract
    assert "An empty list is valid when nothing falls in that category" in contract
    assert "adaptation_summary must be a non-blank string" in contract
    assert "Partition only the top-level required_next_episode_continuity field of this payload." in contract
    assert "Do not use memory_update.required_next_episode_continuity or memory_after.required_next_episode_continuity as the partition source" in contract
    assert "code appends the new items to the next episode source automatically" in contract
    assert "An item may appear in continuity_satisfied or continuity_deferred if and only if it appears in the top-level required_next_episode_continuity list." in contract
    assert "Ignore nested-list membership; if an item also appears in a nested list, partition it once based solely on its top-level membership." in contract


def test_transition_rejects_candidate_absent_from_provider_projection(tmp_path):
    episode_id = "episode_007"
    next_id = "episode_008"
    episode_dir = tmp_path / "episodes" / episode_id
    episode_dir.mkdir(parents=True)
    prose = " ".join(f"Exact transition evidence sentence number {index:03d} keeps the completed final artifact long enough." for index in range(220))
    (episode_dir / "final.md").write_text(prose, encoding="utf-8")
    write_json(episode_dir / "episode_plan.json", {"objective": "A safe JSON excerpt."})
    write_json(episode_dir / "memory_update.json", {"summary": "A memory update excerpt."})
    write_json(episode_dir / "memory_after.json", {"summary": "A memory after excerpt."})

    pipeline = PilotPipeline(object(), scenario="pass", mode="mock")
    source = {"rolling_plan": {"immediate_horizon": ["next item"], "near_horizon": []}, "required_next_episode_continuity": []}
    payload = pipeline._transition_payload(tmp_path, {"pilot_id": "pilot-test", "episode_ids": [episode_id, next_id]}, episode_id, next_id, source, 0)
    offered_ids = frozenset(entry["candidate_id"] for entry in payload["evidence_candidates"])
    excluded = [candidate for candidate in _transition_candidate_catalog(tmp_path, episode_id) if candidate.candidate_id not in offered_ids]
    assert excluded

    response = transition_adapter_response(payload)
    response["adaptation_decisions"][0]["evidence_candidate_ids"] = [excluded[0].candidate_id]
    with pytest.raises(ContractError) as error:
        pipeline._transition_from_response(tmp_path, episode_id, next_id, source, "hash", json.dumps(response, ensure_ascii=False), offered_ids)
    assert error.value.contract_code == "EVIDENCE_CANDIDATE_UNKNOWN"


def _client(tmp_path, key_count: int = 11) -> tuple[GemmaPoolClient, dict[str, _Provider]]:
    providers: dict[str, _Provider] = {}

    def factory(key: str) -> _Provider:
        slot = key.replace("key-", "K")
        providers[slot] = _Provider(slot)
        return providers[slot]

    store = AtomicTelemetryStore(tmp_path / "pilot_live_calls.json")
    return GemmaPoolClient(_config(key_count), client_factory=factory, telemetry_sink=store.save), providers


def test_json_prompt_gate_rejects_before_key_lease_and_provider_call(tmp_path):
    client, providers = _client(tmp_path)
    with pytest.raises(LiveCallError) as error:
        client.generate_for_desk(desk=LogicalDesk("transition:adapter", "transition", "adapter", 1), prompt="x" * 16001)
    assert error.value.error_class == "PROMPT_BUDGET_EXCEEDED"
    assert client.calls == []
    assert all(not provider.prompts for provider in providers.values())
    assert client.telemetry()["contract_failures"][0]["contract_code"] == "PROMPT_BUDGET_EXCEEDED"


def _pilot_client(run_dir: Path, root: _PilotProviderRoot | None = None) -> tuple[GemmaPoolClient, _PilotProviderRoot]:
    provider_root = root or _PilotProviderRoot()
    state_store = RoutingStateStore(run_dir / "routing_state.json", list(_config().keys))
    telemetry_store = AtomicTelemetryStore(run_dir / "pilot_live_calls.json")
    return GemmaPoolClient(_config(), client_factory=provider_root.factory, state_store=state_store, telemetry_sink=telemetry_store.save), provider_root


def _worker(stage: str, role: str) -> dict:
    evidence = ["final.md"] if stage == "memory" else ["source:current_episode"]
    return {"worker_id": f"{stage}-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": evidence, "proposal": {"role": role}}


def _file_bytes(output: Path) -> dict[str, bytes]:
    return {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}


def _episode_from_prompt(prompt: str) -> str | None:
    if "Input JSON:\n" not in prompt:
        return None
    payload = json.loads(prompt.split("Input JSON:\n", 1)[1])
    return payload.get("episode_id") or payload.get("context", {}).get("episode_id")


def _make_interrupted_episode_output(tmp_path: Path) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = read_json(output / "pilot_manifest.json")
    episode2 = output / "episodes" / "episode_002"
    transition_001_002 = read_json(output / "transitions" / "episode_001_to_episode_002.json")
    for episode_id in ["episode_003", "episode_004", "episode_005"]:
        shutil.rmtree(output / "episodes" / episode_id, ignore_errors=True)
        (output / "episode_sources" / f"{episode_id}.json").unlink(missing_ok=True)
    shutil.rmtree(output / "transitions", ignore_errors=True)
    transitions = output / "transitions"
    transitions.mkdir()
    write_json(transitions / "episode_001_to_episode_002.json", transition_001_002)
    for name in ["pilot_evidence_packet.json", "pilot_review_workers.json", "pilot_acceptance.json", "pilot_review_workers.partial.json"]:
        (output / name).unlink(missing_ok=True)
    shutil.rmtree(output / "pilot_review_receipts", ignore_errors=True)

    episode_manifest = read_json(episode2 / "manifest.json")
    keep = {"manifest.json", "context_packet.json", "planning_workers.partial.json", "live_calls.json"}
    for path in list(episode2.iterdir()):
        if path.is_file() and path.name not in keep:
            path.unlink()
    episode_manifest.update({"status": "RUNNING", "completed_stages": ["CONTEXT_ASSEMBLED"], "artifact_hashes": {"context_packet.json": episode_manifest["artifact_hashes"]["context_packet.json"]}, "writer_call_count": 0, "revision_count": 0, "review_verdict": None, "last_error": None, "live_call_count": 2, **MockPipeline._initial_writer_state()})

    root_telemetry = read_json(output / "pilot_live_calls.json")
    keep_scopes = {"episode:episode_001"}
    kept_calls = [call for call in root_telemetry["calls"] if call["scope_id"] in keep_scopes or call["desk_id"] in {"episode:episode_002:planning:event", "episode:episode_002:planning:protagonist_action"}]
    root_telemetry["calls"] = kept_calls
    episode2_projection = {**root_telemetry, "calls": [call for call in kept_calls if call["scope_id"] == "episode:episode_002"]}
    episode_manifest["artifact_hashes"]["live_calls.json"] = write_json(episode2 / "live_calls.json", episode2_projection)
    write_json(episode2 / "manifest.json", episode_manifest)

    checkpoint = WaveCheckpoint(episode2 / "planning_workers.partial.json", "planning", read_json(episode2 / "context_packet.json"), PLANNING_ROLES)
    checkpoint.save("event", _worker("planning", "event"))
    checkpoint.save("protagonist_action", _worker("planning", "protagonist_action"))

    root_hash = write_json(output / "pilot_live_calls.json", root_telemetry)
    routing_state = read_json(output / "routing_state.json")
    max_lease = max(call["lease_sequence"] for call in kept_calls)
    routing_state["next_lease_sequence"] = max_lease + 1
    write_json(output / "routing_state.json", routing_state)

    manifest.update({"status": "RUNNING", "completed_episodes": ["episode_001"], "completed_transitions": ["episode_001_to_episode_002"], "active_episode_id": "episode_002", "episode_records": manifest["episode_records"][:1], "acceptance_verdict": None, "last_error": None, "pilot_live_call_count": len(kept_calls)})
    manifest["artifact_hashes"] = {key: value for key, value in manifest["artifact_hashes"].items() if key in {"episode_sources/episode_001.json", "episode_sources/episode_002.json", "transitions/episode_001_to_episode_002.json"}}
    manifest["live_telemetry_checkpoint"] = live_telemetry_checkpoint(root_telemetry)
    write_json(output / "pilot_manifest.json", manifest)
    return output


def _make_legacy_revision_output(tmp_path: Path, *, ambiguous: bool = False) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004", revision_text="B" * 3949))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode_dir = output / "episodes" / "episode_004"
    episode = read_json(episode_dir / "manifest.json")
    for key in ["revision_attempt_state", "revision_exhausted", "revision_response_sha256", "revision_character_count", "revision_contract_code", "revision_response_received_at", "revision_call_id", "revision_lease_sequence"]:
        episode.pop(key)
    episode.update({"status": "ERROR", "revision_count": 0, "last_error": {"error_class": "CONTRACT_ERROR", "stage": "revision", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "character_count": 3949, "call_id": "L317-A004", "message": "sanitized prose contract failure"}})
    telemetry = read_json(output / "pilot_live_calls.json")
    original = next(call for call in telemetry["calls"] if call["scope_id"] == "episode:episode_004" and call["stage"] == "revision")
    telemetry["calls"] = [call for call in telemetry["calls"] if not (call["scope_id"] == "episode:episode_004" and call["stage"] == "revision")]
    max_lease = max(call["lease_sequence"] for call in telemetry["calls"])
    failures = []
    for offset, slot in enumerate(("K10", "K11"), start=1):
        failure = dict(original)
        failure.update({"call_id": f"L317-A00{offset + 1}", "attempt": offset + 1, "lease_sequence": max_lease + offset, "key_slot": slot, "status": "FAIL", "output_characters": 0, "response_sha256": None, "error_class": "PROVIDER_5XX", "http_status": 500})
        failures.append(failure)
    response = dict(original)
    response.update({"call_id": "L317-A004", "attempt": 4, "lease_sequence": max_lease + 3, "key_slot": "K01", "status": "PASS", "output_characters": 3949, "response_sha256": hashlib.sha256(("B" * 3949).encode()).hexdigest(), "error_class": None, "http_status": None})
    telemetry["calls"].extend([*failures, response])
    telemetry["contract_failures"] = [item for item in telemetry.get("contract_failures", []) if not (item.get("scope_id") == "episode:episode_004" and item.get("stage") == "revision")]
    telemetry["contract_failures"].append({"event_id": "CF999", "scope_id": "episode:episode_004", "desk_id": "episode:episode_004:revision:canonical", "stage": "revision", "role": "canonical", "key_slot": "K01", "call_id": "L317-A004", "contract_code": "PROSE_TOO_SHORT", "error_class": "CONTRACT_ERROR", "created_at": response["finished_at"], "character_count": 3949, "message": "sanitized prose contract failure"})
    if ambiguous:
        extra = dict(response)
        extra.update({"call_id": "L317-A005", "attempt": 5, "lease_sequence": max_lease + 4})
        telemetry["calls"].append(extra)
    write_json(output / "pilot_live_calls.json", telemetry)
    projection = {**telemetry, "calls": [call for call in telemetry["calls"] if call["scope_id"] == "episode:episode_004"]}
    episode["artifact_hashes"]["live_calls.json"] = write_json(episode_dir / "live_calls.json", projection)
    write_json(episode_dir / "manifest.json", episode)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = max(call["lease_sequence"] for call in telemetry["calls"]) + 1
    write_json(output / "routing_state.json", routing)
    manifest = read_json(output / "pilot_manifest.json")
    manifest.update({"status": "ERROR", "active_episode_id": "episode_004", "last_error": {"error_class": "CONTRACT_ERROR", "active_episode_id": "episode_004", "stage": "revision", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "message": "sanitized child episode failure"}, "pilot_live_call_count": len(telemetry["calls"]), "live_telemetry_checkpoint": live_telemetry_checkpoint(telemetry)})
    write_json(output / "pilot_manifest.json", manifest)
    return output


def _make_legacy_writer_output(tmp_path: Path, *, ambiguous: str | None = None) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(writer_text_once_episode="episode_001", writer_text="A" * 2858))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode_dir = output / "episodes" / "episode_001"
    episode = read_json(episode_dir / "manifest.json")
    for key in MockPipeline._initial_writer_state():
        episode.pop(key)
    episode.update({"status": "ERROR", "writer_call_count": 0, "last_error": {"error_class": "CONTRACT_ERROR", "stage": "writer", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "character_count": 2858, "call_id": "L008-A001", "message": "sanitized prose contract failure"}})
    telemetry = read_json(output / "pilot_live_calls.json")
    response = next(call for call in telemetry["calls"] if call["scope_id"] == "episode:episode_001" and call["stage"] == "writer")
    response.update({"call_id": "L008-A001", "key_slot": "K04", "lease_sequence": 15})
    failure = next(item for item in telemetry["contract_failures"] if item["scope_id"] == "episode:episode_001" and item["stage"] == "writer")
    failure.update({"call_id": "L008-A001", "key_slot": "K04", "character_count": 2858, "contract_code": "PROSE_TOO_SHORT"})
    if ambiguous == "response":
        extra = dict(response)
        extra.update({"call_id": "L008-A002", "lease_sequence": 16})
        telemetry["calls"].append(extra)
    elif ambiguous == "failure":
        telemetry["contract_failures"].append({**failure, "event_id": "CF999"})
    elif ambiguous == "call_id":
        failure["call_id"] = "mismatch"
    elif ambiguous == "characters":
        failure["character_count"] = 2857
    elif ambiguous == "contract":
        failure["contract_code"] = "PROSE_TOO_LONG"
    elif ambiguous == "duplicate_lease":
        response["lease_sequence"] = telemetry["calls"][0]["lease_sequence"]
    elif ambiguous == "duplicate_call":
        response["call_id"] = telemetry["calls"][0]["call_id"]
        failure["call_id"] = response["call_id"]
    write_json(output / "pilot_live_calls.json", telemetry)
    projection = {**telemetry, "calls": [call for call in telemetry["calls"] if call["scope_id"] == "episode:episode_001"]}
    episode["artifact_hashes"]["live_calls.json"] = write_json(episode_dir / "live_calls.json", projection)
    write_json(episode_dir / "manifest.json", episode)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = max(call["lease_sequence"] for call in telemetry["calls"]) + 1
    routing["keys"]["K04"]["last_lease_sequence"] = response["lease_sequence"]
    write_json(output / "routing_state.json", routing)
    manifest = read_json(output / "pilot_manifest.json")
    manifest.update({"status": "ERROR", "active_episode_id": "episode_001", "last_error": {"error_class": "CONTRACT_ERROR", "active_episode_id": "episode_001", "stage": "writer", "role": "canonical", "contract_code": "PROSE_TOO_SHORT", "message": "sanitized child episode failure"}, "pilot_live_call_count": len(telemetry["calls"]), "live_telemetry_checkpoint": live_telemetry_checkpoint(telemetry)})
    write_json(output / "pilot_manifest.json", manifest)
    if ambiguous == "draft":
        (episode_dir / "draft.md").write_text("A" * 2858, encoding="utf-8")
    elif ambiguous == "review":
        write_json(episode_dir / "review_workers.json", [])
    elif ambiguous == "episode_002":
        write_json(output / "episode_sources" / "episode_002.json", read_json(output / "episode_sources" / "episode_001.json"))
    return output


def _make_acceptance_state(tmp_path: Path, completed_roles: list[str], *, receipt_roles: list[str] | None = None, telemetry_roles: list[str] | None = None, stale_manifest_checkpoint: bool = False) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = read_json(output / "pilot_manifest.json")
    receipt_roles = completed_roles if receipt_roles is None else receipt_roles
    telemetry_roles = completed_roles if telemetry_roles is None else telemetry_roles
    workers = {worker["role"]: worker for worker in read_json(output / "pilot_review_workers.json")}
    (output / "pilot_review_workers.json").unlink(missing_ok=True)
    (output / "pilot_acceptance.json").unlink(missing_ok=True)
    for role in PILOT_REVIEW_ROLES:
        if role not in receipt_roles:
            (output / "pilot_review_receipts" / f"{role}.response.json").unlink(missing_ok=True)

    if completed_roles:
        checkpoint = WaveCheckpoint(
            output / "pilot_review_workers.partial.json",
            "pilot_review",
            {"pilot_id": manifest["pilot_id"], "mode": manifest["mode"], "scenario": manifest["scenario"], "episode_ids": manifest["episode_ids"], "evidence_packet_hash": manifest["artifact_hashes"]["pilot_evidence_packet.json"], "acceptance_rubric_version": 1, "acceptance_provider_contract_version": ACCEPTANCE_PROVIDER_CONTRACT_VERSION, "evidence_candidate_catalog_version": EVIDENCE_CANDIDATE_CATALOG_VERSION},
            PILOT_REVIEW_ROLES,
        )
        for role in completed_roles:
            checkpoint.save(role, workers[role])

    telemetry = read_json(output / "pilot_live_calls.json")
    kept_calls = [call for call in telemetry["calls"] if call["scope_id"] != "pilot:acceptance" or call["role"] in telemetry_roles]
    telemetry["calls"] = kept_calls
    write_json(output / "pilot_live_calls.json", telemetry)
    routing_state = read_json(output / "routing_state.json")
    routing_state["next_lease_sequence"] = max(call["lease_sequence"] for call in kept_calls) + 1
    write_json(output / "routing_state.json", routing_state)

    manifest.update({"status": "RUNNING", "active_episode_id": None, "acceptance_verdict": None, "last_error": None, "pilot_live_call_count": len(kept_calls)})
    manifest["artifact_hashes"].pop("pilot_review_workers.json", None)
    manifest["artifact_hashes"].pop("pilot_acceptance.json", None)
    if stale_manifest_checkpoint:
        pre_acceptance = {**telemetry, "calls": [call for call in kept_calls if call["scope_id"] != "pilot:acceptance"]}
        manifest["pilot_live_call_count"] = len(pre_acceptance["calls"])
        manifest["live_telemetry_checkpoint"] = live_telemetry_checkpoint(pre_acceptance)
    else:
        manifest["live_telemetry_checkpoint"] = live_telemetry_checkpoint(telemetry)
    write_json(output / "pilot_manifest.json", manifest)
    return output


def _make_acceptance_partial_output(tmp_path: Path) -> Path:
    return _make_acceptance_state(tmp_path, ["readability", "character_consistency", "continuity"])


def _run_planning_merge_contract_failure(tmp_path: Path):
    output = tmp_path / "episode"
    root = _PilotProviderRoot(wrong_plan_once=True)
    client, _ = _pilot_client(tmp_path / "runtime", root)
    scoped = client.scope(scope_id="episode:episode_004", logical_order_base=300)
    with pytest.raises(Exception):
        MockPipeline(scoped, mode="live").run(Path("tests/fixtures/synthetic_work.json"), output, None)
    telemetry = read_json(output / "live_calls.json")
    manifest = read_json(output / "manifest.json")
    return output, root, telemetry, manifest


def _make_episode_four_plan_error_output(tmp_path: Path) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(wrong_plan_once=True, wrong_plan_episode="episode_004"))
    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    return output


def _make_reconcilable_pilot_output(tmp_path: Path) -> Path:
    output = _make_episode_four_plan_error_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    telemetry = read_json(output / "pilot_live_calls.json")
    stale_hash = write_json(output / "pilot_live_calls.json", telemetry)
    manifest["active_episode_id"] = "episode_001"
    manifest["completed_episodes"] = ["episode_001"]
    manifest["completed_transitions"] = ["episode_001_to_episode_002"]
    manifest["episode_records"] = manifest["episode_records"][:1]
    manifest["artifact_hashes"]["pilot_live_calls.json"] = "0" * 64
    manifest.pop("live_telemetry_checkpoint", None)
    write_json(output / "pilot_manifest.json", manifest)
    assert stale_hash != manifest["artifact_hashes"]["pilot_live_calls.json"]
    return output


def _make_episode_four_writer_error_output(tmp_path: Path) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(short_writer_once_episode="episode_004"))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    return output


def _resume_episode_four_output(output: Path, root: _PilotProviderRoot | None = None):
    fresh_client, provider_root = _pilot_client(output, root)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    return result, provider_root


def _planning_merge_contract_event(telemetry: dict) -> dict:
    return next(item for item in telemetry["contract_failures"] if item["stage"] == "planning_merge")


def test_scoped_clients_share_one_dynamic_key_pool(tmp_path):
    base, _ = _client(tmp_path, 3)
    first = base.scope(scope_id="episode:episode_001", logical_order_base=0)
    second = base.scope(scope_id="episode:episode_002", logical_order_base=100)

    first.generate(stage="planning", role="event", prompt="one")
    second.generate(stage="planning", role="event", prompt="two")

    assert first.pool is base.pool
    assert second.pool is base.pool
    assert [call["key_slot"] for call in base.telemetry()["calls"]] == ["K01", "K02"]


def test_scoped_clients_share_one_launch_pacer(tmp_path):
    base, _ = _client(tmp_path, 3)
    first = base.scope(scope_id="episode:episode_001", logical_order_base=0)
    second = base.scope(scope_id="episode:episode_002", logical_order_base=100)

    first.generate(stage="planning", role="event", prompt="one")
    second.generate(stage="planning", role="event", prompt="two")

    assert first.pacer is base.pacer
    assert second.pacer is base.pacer
    assert [call["launch_sequence"] for call in base.telemetry()["calls"]] == [1, 2]


def test_scoped_clients_do_not_close_base_clients(tmp_path):
    base, providers = _client(tmp_path, 2)
    scoped = base.scope(scope_id="episode:episode_001", logical_order_base=0)

    scoped.close()
    assert all(not provider.closed for provider in providers.values())

    base.close()
    assert all(provider.closed for provider in providers.values())


def test_pilot_logical_orders_are_globally_unique(tmp_path):
    base, _ = _client(tmp_path, 3)

    for index in range(5):
        base.scope(scope_id=f"episode:episode_{index + 1:03d}", logical_order_base=index * 100).generate(stage="planning", role="event", prompt=str(index))

    orders = [call["logical_order"] for call in base.telemetry()["calls"]]
    assert orders == [1, 101, 201, 301, 401]
    assert len(orders) == len(set(orders))


def test_attempt_is_scoped_by_desk_id(tmp_path):
    base, _ = _client(tmp_path, 3)
    first = base.scope(scope_id="episode:episode_001", logical_order_base=0)
    second = base.scope(scope_id="episode:episode_002", logical_order_base=100)

    first.generate(stage="planning", role="event", prompt="one")
    second.generate(stage="planning", role="event", prompt="two")
    first.generate(stage="planning", role="event", prompt="three")

    attempts = {(call["desk_id"], call["attempt"]) for call in base.telemetry()["calls"]}
    assert ("episode:episode_001:planning:event", 1) in attempts
    assert ("episode:episode_002:planning:event", 1) in attempts
    assert ("episode:episode_001:planning:event", 2) in attempts


def test_call_ids_are_unique_across_episodes(tmp_path):
    base, _ = _client(tmp_path, 3)

    for index in range(2):
        base.scope(scope_id=f"episode:episode_{index + 1:03d}", logical_order_base=index * 100).generate(stage="planning", role="event", prompt=str(index))

    call_ids = [call["call_id"] for call in base.telemetry()["calls"]]
    assert call_ids == ["L001-A001", "L101-A001"]
    assert len(call_ids) == len(set(call_ids))


def test_pilot_telemetry_atomic_concurrent_append(tmp_path):
    base, _ = _client(tmp_path, 11)
    scopes = [base.scope(scope_id=f"episode:episode_{index + 1:03d}", logical_order_base=index * 100) for index in range(5)]

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda item: item[1].generate(stage="planning", role="event", prompt=str(item[0])), enumerate(scopes)))

    saved = AtomicTelemetryStore(tmp_path / "pilot_live_calls.json").load()
    call_ids = [call["call_id"] for call in saved["calls"]]
    assert len(saved["calls"]) == 5
    assert len(call_ids) == len(set(call_ids))
    assert {call["scope_id"] for call in saved["calls"]} == {f"episode:episode_{index + 1:03d}" for index in range(5)}


def test_episode_telemetry_is_scope_projection(tmp_path):
    base, _ = _client(tmp_path, 3)
    first = base.scope(scope_id="episode:episode_001", logical_order_base=0)
    second = base.scope(scope_id="episode:episode_002", logical_order_base=100)

    first.generate(stage="planning", role="event", prompt="one")
    second.generate(stage="planning", role="event", prompt="two")

    root = base.telemetry()
    projection = first.telemetry()
    assert len(root["calls"]) == 2
    assert [call["desk_id"] for call in projection["calls"]] == ["episode:episode_001:planning:event"]
    assert projection["calls"][0] in root["calls"]


def test_phase2_unscoped_telemetry_remains_compatible(tmp_path):
    base, _ = _client(tmp_path, 3)

    base.generate(stage="planning", role="event", prompt="one")
    base.generate(stage="planning", role="event", prompt="two")

    calls = base.telemetry()["calls"]
    assert [call["call_id"] for call in calls] == ["L001-A001", "L001-A002"]
    assert [call["desk_id"] for call in calls] == ["planning:event", "planning:event"]
    assert [call["scope_id"] for call in calls] == [None, None]
    assert [call["attempt"] for call in calls] == [1, 2]


def test_live_pilot_runs_five_episodes_with_one_base_client(tmp_path):
    output = tmp_path / "pilot-live"
    client, provider_root = _pilot_client(output)

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = result["manifest"]
    assert manifest["status"] == "COMPLETE"
    assert manifest["completed_episodes"] == manifest["episode_ids"]
    assert manifest["pilot_live_call_count"] == len(read_json(output / "pilot_live_calls.json")["calls"])
    episode_scopes = {call["scope_id"] for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"].startswith("episode:")}
    assert len(episode_scopes) == 5
    assert provider_root.provider_calls


def test_pilot_live_telemetry_is_operational_not_immutable_artifact(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = read_json(output / "pilot_manifest.json")
    assert "pilot_live_calls.json" not in manifest["artifact_hashes"]
    assert (output / "pilot_live_calls.json").exists()


def test_live_checkpoint_writes_telemetry_before_manifest(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = read_json(output / "pilot_manifest.json")
    telemetry = read_json(output / "pilot_live_calls.json")
    assert manifest["live_telemetry_checkpoint"] == live_telemetry_checkpoint(telemetry)


def test_live_checkpoint_records_telemetry_prefix(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    checkpoint = read_json(output / "pilot_manifest.json")["live_telemetry_checkpoint"]
    telemetry = read_json(output / "pilot_live_calls.json")
    assert checkpoint["call_count"] == len(telemetry["calls"])
    assert checkpoint["contract_failure_count"] == len(telemetry["contract_failures"])
    assert checkpoint["last_call_id"] == telemetry["calls"][-1]["call_id"]


def test_live_checkpoint_accepts_append_only_telemetry(tmp_path):
    from arc.pilot import pilot_status

    output = _make_episode_four_plan_error_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    telemetry = read_json(output / "pilot_live_calls.json")
    checkpoint = manifest["live_telemetry_checkpoint"]
    assert len(telemetry["calls"]) == checkpoint["call_count"]
    extra = dict(telemetry["calls"][-1])
    extra["call_id"] = "L999-A999"
    extra["lease_sequence"] = max(call["lease_sequence"] for call in telemetry["calls"]) + 1
    extra["scope_id"] = "pilot:acceptance"
    extra["desk_id"] = "pilot:acceptance:pilot_review:readability"
    telemetry["calls"].append(extra)
    write_json(output / "pilot_live_calls.json", telemetry)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = extra["lease_sequence"] + 1
    write_json(output / "routing_state.json", routing)

    current = pilot_status(output)

    assert current["pilot_live_call_count"] == checkpoint["call_count"] + 1


def test_live_checkpoint_rejects_changed_call_prefix(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    telemetry = read_json(output / "pilot_live_calls.json")
    telemetry["calls"][0]["stage"] = "tampered"
    write_json(output / "pilot_live_calls.json", telemetry)

    with pytest.raises(StorageError, match="checkpoint call prefix mismatch"):
        pilot_status(output)


def test_live_checkpoint_rejects_shortened_telemetry(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    telemetry = read_json(output / "pilot_live_calls.json")
    telemetry["calls"].pop()
    write_json(output / "pilot_live_calls.json", telemetry)

    with pytest.raises(StorageError, match="shorter than checkpoint"):
        pilot_status(output)


def test_live_exception_saves_error_manifest_after_telemetry(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    telemetry = read_json(output / "pilot_live_calls.json")
    assert manifest["status"] == "ERROR"
    assert manifest["live_telemetry_checkpoint"] == live_telemetry_checkpoint(telemetry)


def test_live_terminal_checkpoint_covers_complete_telemetry(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = read_json(output / "pilot_manifest.json")
    telemetry = read_json(output / "pilot_live_calls.json")
    assert manifest["status"] == "COMPLETE"
    assert manifest["live_telemetry_checkpoint"] == live_telemetry_checkpoint(telemetry)


def test_phase2_live_telemetry_contract_remains_compatible(tmp_path):
    test_phase2_unscoped_telemetry_remains_compatible(tmp_path)


def test_validate_prose_accepts_normal_range():
    assert validate_prose("A" * 4000) == "A" * 4000
    assert PROSE_MIN_CHARACTERS == 4000
    assert PROSE_MAX_CHARACTERS == 8000
    assert PROSE_REPAIRABLE_MIN_CHARACTERS == 3000


def test_validate_prose_rejects_short_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose("A" * 3999)
    assert error.value.contract_code == "PROSE_TOO_SHORT"
    assert error.value.character_count == 3999


def test_validate_prose_rejects_3709_revision_as_short():
    with pytest.raises(ContractError) as error:
        validate_prose("A" * 3709)
    assert error.value.contract_code == "PROSE_TOO_SHORT"
    assert error.value.character_count == 3709


def test_validate_prose_rejects_long_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose("A" * 8001)
    assert error.value.contract_code == "PROSE_TOO_LONG"
    assert error.value.character_count == 8001


def test_validate_prose_rejects_forbidden_marker_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose(("A" * 4100) + "SCENE 1")
    assert error.value.contract_code == "PROSE_FORBIDDEN_MARKER"


def test_prose_forbidden_markers_are_utf8_canonical():
    assert PROSE_FORBIDDEN_MARKERS == ("[화면]", "[음향]", "[카메라]", "장면 1", "장면 2", "SCENE 1", "CUT TO:", "```")


@pytest.mark.parametrize("marker", ["[화면]", "[음향]", "[카메라]", "장면 1", "장면 2"])
def test_validate_prose_rejects_korean_forbidden_markers(marker):
    with pytest.raises(ContractError) as error:
        validate_prose(("A" * 4100) + marker)
    assert error.value.contract_code == "PROSE_FORBIDDEN_MARKER"


@pytest.mark.parametrize("marker", ["SCENE 1", "CUT TO:", "```"])
def test_validate_prose_rejects_english_forbidden_markers(marker):
    with pytest.raises(ContractError) as error:
        validate_prose(("A" * 4100) + marker)
    assert error.value.contract_code == "PROSE_FORBIDDEN_MARKER"


def test_prose_contract_sources_do_not_contain_mojibake_markers():
    assert "?붾㈃" not in Path("arc/contracts.py").read_text(encoding="utf-8")


def test_validate_prose_rejects_json_shape_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose('{"text":"bad"}')
    assert error.value.contract_code == "PROSE_INVALID_SHAPE"


def test_validate_draft_prose_rejects_2999_as_terminal_short():
    with pytest.raises(ContractError) as error:
        validate_draft_prose("A" * 2999)
    assert error.value.contract_code == "PROSE_TOO_SHORT"


@pytest.mark.parametrize("count", [3000, 3999])
def test_validate_draft_prose_accepts_repairable_underlength(count):
    text, contract = validate_draft_prose("A" * count)
    assert text == "A" * count
    assert contract["verdict"] == "REVISE_REQUIRED"
    assert contract["contract_code"] == "PROSE_UNDERLENGTH_REPAIRABLE"


def test_validate_draft_prose_accepts_4000_as_pass():
    _, contract = validate_draft_prose("A" * 4000)
    assert contract["verdict"] == "PASS"
    assert contract["contract_code"] is None


def test_validate_draft_prose_rejects_8001_as_too_long():
    with pytest.raises(ContractError) as error:
        validate_draft_prose("A" * 8001)
    assert error.value.contract_code == "PROSE_TOO_LONG"


def test_writer_contract_failure_keeps_provider_pass_and_records_code(tmp_path):
    output = _make_episode_four_writer_error_output(tmp_path)
    telemetry = read_json(output / "pilot_live_calls.json")
    event = next(item for item in telemetry["contract_failures"] if item["stage"] == "writer")
    writer_call = next(call for call in telemetry["calls"] if call["desk_id"] == "episode:episode_004:writer:canonical")
    manifest = read_json(output / "episodes" / "episode_004" / "manifest.json")

    assert writer_call["status"] == "PASS"
    assert event["contract_code"] == "PROSE_TOO_SHORT"
    assert event["character_count"] == len("short prose")
    assert event["call_id"] == writer_call["call_id"]
    assert manifest["status"] == "HOLD"
    assert manifest["writer_call_count"] == 1
    assert manifest["writer_attempt_state"] == "REJECTED"
    assert manifest["writer_exhausted"] is True
    assert manifest["last_error"]["contract_code"] == "PROSE_TOO_SHORT"
    assert manifest["last_error"]["character_count"] == len("short prose")
    assert "short prose" not in json.dumps(telemetry, ensure_ascii=False)
    assert "short prose" not in json.dumps(manifest, ensure_ascii=False)


@pytest.mark.parametrize("writer_text", ["A" * 2999, "", "   ", "A" * 8001, '{"text":"bad"}', '["bad"]', "```\n" + "A" * 4100, "A" * 4100 + "SCENE 1"])
def test_invalid_writer_response_is_consumed_and_holds(tmp_path, writer_text):
    output = tmp_path / "pilot-live"
    client, root = _pilot_client(output, _PilotProviderRoot(writer_text_once_episode="episode_001", writer_text=writer_text))

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")

    assert result["manifest"]["status"] == "HOLD"
    assert episode["status"] == "HOLD"
    assert episode["writer_call_count"] == 1
    assert episode["writer_attempt_state"] == "REJECTED"
    assert episode["writer_exhausted"] is True
    assert episode["writer_contract_code"]
    assert not (output / "episodes" / "episode_001" / "draft.md").exists()
    assert not any(marker.startswith(("review:", "revision:")) for _, marker, _ in root.provider_calls)
    assert not (output / "episode_sources" / "episode_002.json").exists()
    assert not (output / "pilot_acceptance.json").exists()


def test_writer_response_receipt_is_persisted_before_validation(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    def inspect_receipt(value):
        episode = read_json(output / "episodes" / "episode_001" / "manifest.json")
        assert episode["writer_call_count"] == 1
        assert episode["writer_attempt_state"] == "RESPONSE_RECEIVED"
        assert episode["writer_exhausted"] is True
        assert episode["writer_response_sha256"] == hashlib.sha256(value.encode()).hexdigest()
        assert episode["writer_character_count"] == len(value)
        assert episode["writer_call_id"]
        assert episode["writer_lease_sequence"] > 0
        assert episode["writer_response_received_at"]
        raise KeyboardInterrupt

    monkeypatch.setattr("arc.pipeline.validate_draft_prose", inspect_receipt)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)


def test_response_received_resume_holds_without_second_writer_call(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    monkeypatch.setattr("arc.pipeline.validate_draft_prose", lambda value: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    monkeypatch.undo()
    resumed_client, resumed_root = _pilot_client(output)

    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")

    assert result["manifest"]["status"] == "HOLD"
    assert episode["writer_attempt_state"] == "REJECTED"
    assert episode["writer_contract_code"] == "WRITER_RESPONSE_ALREADY_CONSUMED"
    assert resumed_root.provider_calls == []


def test_transport_failure_does_not_consume_writer_and_resume_can_retry(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    original = client.generate_for_desk

    def fail_writer(*, desk, prompt):
        if desk.stage == "writer":
            raise LiveCallError("PROVIDER_5XX", "writer", "canonical", "K10", "injected", 500)
        return original(desk=desk, prompt=prompt)

    monkeypatch.setattr(client, "generate_for_desk", fail_writer)
    with pytest.raises(LiveCallError):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")
    assert episode["status"] == "ERROR"
    assert episode["writer_call_count"] == 0
    assert episode["writer_attempt_state"] == "NOT_STARTED"
    assert episode["writer_exhausted"] is False
    monkeypatch.undo()
    resumed_client, resumed_root = _pilot_client(output)

    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    assert sum(marker == "writer:canonical" and _episode_from_prompt(prompt) == "episode_001" for _, marker, prompt in resumed_root.provider_calls) == 1


def test_transport_retries_count_as_one_logical_writer_response(tmp_path):
    output = tmp_path / "pilot-live"
    root = _PilotProviderRoot(fail_once_at="writer:canonical")
    client, _ = _pilot_client(output, root)

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")
    calls = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "episode:episode_001" and call["stage"] == "writer"]

    assert result["manifest"]["status"] == "COMPLETE"
    assert [call["status"] for call in calls] == ["FAIL", "PASS"]
    assert episode["writer_call_count"] == 1
    assert episode["writer_attempt_state"] == "COMPLETED"


def test_rejected_writer_checkpoint_is_terminal_noop(tmp_path):
    output = _make_episode_four_writer_error_output(tmp_path)
    before = _file_bytes(output)

    result, provider_root = _resume_episode_four_output(output)

    assert result["no_op"] is True
    assert result["manifest"]["status"] == "HOLD"
    assert provider_root.provider_calls == []
    assert _file_bytes(output) == before


@pytest.mark.parametrize("field,value", [("writer_call_count", 2), ("writer_attempt_state", "NOT_STARTED"), ("writer_response_sha256", "bad"), ("writer_character_count", 1), ("writer_call_id", "missing"), ("writer_lease_sequence", 999)])
def test_invalid_writer_state_fails_closed_before_provider_call(tmp_path, field, value):
    output = _make_episode_four_writer_error_output(tmp_path)
    episode_path = output / "episodes" / "episode_004" / "manifest.json"
    episode = read_json(episode_path)
    episode[field] = value
    write_json(episode_path, episode)
    resumed_client, resumed_root = _pilot_client(output)

    with pytest.raises(Exception, match="invalid writer"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []


def test_writer_prompt_reinforces_safe_character_band():
    from arc.prompts import build_prompt, prose_target_band

    prompt = build_prompt("writer", "canonical", {"context": {}, "plan": {}})

    assert prose_target_band() == (6000, 6800)
    assert "6000 to 6800 characters" in prompt
    assert "barely clearing the validation floor" in prompt
    assert "Expand causes, actions, dialogue" in prompt
    assert "never mention the character count" in prompt


def test_prose_target_band_uses_contract_minimum_and_hard_maximum():
    from arc.prompts import prose_target_band

    assert prose_target_band(5000, None) == (7500, 8500)
    assert prose_target_band(4000, 6000) == (6000, 6000)


def test_writer_prompt_requires_structured_plan_development_without_changing_target_band():
    from arc.prompts import build_prompt

    payload = {"context": {"episode_id": "episode_002"}, "plan": {"immediate_objective": "protect evidence", "obstacle": "active scan", "protagonist_action": "camouflage evidence", "meaningful_change": "become a trusted insider", "episode_ending": "prepare the next move", "continuity_constraints": ["preserve the hidden variable"]}}
    prompt = build_prompt("writer", "canonical", payload)
    for meaning in ("20 to 24", "at least three complete sentences", "plan.immediate_objective", "plan.obstacle", "plan.protagonist_action", "counteraction", "plan.meaningful_change", "consequence", "aftermath", "episode payoff", "plan.episode_ending", "plan.continuity_constraints", "6000 to 6800 characters", "internal expansion pass", "nine beats", "two or three thinnest beats", "Do not advance to the ending", "one canonical response", "20~24개의 자연스러운 소설 문단", "완결된 세 문장 이상", "아홉 전개 단위"):
        assert meaning in prompt
    for forbidden in ("headings or paragraph numbers", "Do not compress multiple actions", "Do not invent a new central conflict"):
        assert forbidden in prompt
    assert "If this is revision" not in prompt
    assert "Do not perform revision work" in prompt


@pytest.mark.parametrize("character_count,expected", [(3000, (1000, 2000)), (3474, (526, 1526)), (3834, (166, 1200)), (3999, (1, 1200)), (4000, (0, 1200)), (4514, (0, 1200))])
def test_revision_expansion_guidance_is_deterministic(character_count, expected):
    from arc.prompts import revision_expansion_guidance

    assert revision_expansion_guidance(character_count) == expected
    assert revision_expansion_guidance(character_count) == expected


def test_revision_prompt_uses_actual_length_and_full_replacement_expansion():
    from arc.prompts import build_prompt

    payload = {"context": {}, "plan": {}, "draft": "A" * 3474, "draft_contract": {"character_count": 3474, "verdict": "REVISE_REQUIRED"}, "decision": {"required_changes": ["preserve evidence"]}}
    prompt = build_prompt("revision", "canonical", payload)
    for meaning in ("3474 characters", "526 characters below", "roughly 1526 or more", "full replacement", "review required changes", "event order", "point of view", "ending", "consequences", "aftermath", "repetition, padding, fragments"):
        assert meaning in prompt


def test_non_prose_prompt_is_not_given_prose_structure_guidance():
    from arc.prompts import build_prompt

    prompt = build_prompt("planning", "event", {})
    assert "20 to 24 natural prose paragraphs" not in prompt
    assert "current draft is" not in prompt


def test_underlength_revision_prompt_requires_full_replacement():
    from arc.prompts import build_prompt

    prompt = build_prompt("revision", "canonical", {"draft_contract": {"verdict": "REVISE_REQUIRED"}, "draft": "A" * 3500})

    assert "6000 to 6800 characters" in prompt
    assert "one full replacement from beginning to end" in prompt
    assert "do not append fragments" in prompt
    assert "Do not change canon outside review requirements" in prompt


def test_repairable_draft_is_saved_and_revised_once(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004"))

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    ep4 = output / "episodes" / "episode_004"
    manifest = read_json(ep4 / "manifest.json")
    draft_contract = read_json(ep4 / "draft_contract.json")

    assert result["manifest"]["status"] == "COMPLETE"
    assert draft_contract["verdict"] == "REVISE_REQUIRED"
    assert draft_contract["contract_code"] == "PROSE_UNDERLENGTH_REPAIRABLE"
    assert draft_contract["character_count"] == 3500
    assert manifest["writer_call_count"] == 1
    assert manifest["writer_attempt_state"] == "COMPLETED"
    assert manifest["writer_exhausted"] is True
    assert manifest["writer_contract_code"] == "PROSE_UNDERLENGTH_REPAIRABLE"
    assert manifest["writer_response_sha256"] == hashlib.sha256((ep4 / "draft.md").read_bytes()).hexdigest()
    assert manifest["revision_count"] == 1
    assert (ep4 / "final.md").read_text(encoding="utf-8") == (ep4 / "revised.md").read_text(encoding="utf-8")


def test_repairable_draft_review_merge_pass_is_rejected(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004", review_pass_on_repairable=True))

    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    ep4 = read_json(output / "episodes" / "episode_004" / "manifest.json")
    assert ep4["last_error"]["contract_code"] == "PROSE_REPAIRABLE_PASS_INVALID"
    assert "draft.md" in ep4["artifact_hashes"]


def test_repairable_draft_review_hold_skips_revision(tmp_path):
    output = tmp_path / "pilot-live"
    client, provider_root = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004", hold_episode="episode_004"))

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "HOLD"
    assert not any(marker == "revision:canonical" and _episode_from_prompt(prompt) == "episode_004" for _, marker, prompt in provider_root.provider_calls)


def test_repairable_draft_revision_failure_is_terminal(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004", short_revision_once_episode="episode_004"))

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    ep4 = read_json(output / "episodes" / "episode_004" / "manifest.json")
    assert result["manifest"]["status"] == "HOLD"
    assert ep4["status"] == "HOLD"
    assert ep4["revision_count"] == 1
    assert ep4["revision_attempt_state"] == "REJECTED"
    assert ep4["revision_exhausted"] is True
    assert ep4["last_error"]["stage"] == "revision"
    assert ep4["last_error"]["contract_code"] == "PROSE_TOO_SHORT"


@pytest.mark.parametrize("revision_text", ["B" * 3999, "B" * 8001, "", "   ", '{"text":"bad"}', '["bad"]', "```\n" + "B" * 4100, "B" * 4100 + "SCENE 1"])
def test_invalid_revision_response_is_consumed_and_holds(tmp_path, revision_text):
    output = tmp_path / "pilot-live"
    root = _PilotProviderRoot(repairable_writer_once_episode="episode_004", revision_text=revision_text)
    client, root = _pilot_client(output, root)

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_004" / "manifest.json")
    revision_calls = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "episode:episode_004" and call["stage"] == "revision"]

    assert result["manifest"]["status"] == "HOLD"
    assert episode["revision_count"] == 1
    assert episode["revision_attempt_state"] == "REJECTED"
    assert episode["revision_character_count"] == len(revision_text)
    assert len(revision_calls) == 1
    assert not (output / "episodes" / "episode_004" / "revised.md").exists()


def test_revision_response_receipt_is_persisted_before_validation(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004"))

    def inspect_receipt(value):
        episode = read_json(output / "episodes" / "episode_004" / "manifest.json")
        assert episode["revision_count"] == 1
        assert episode["revision_attempt_state"] == "RESPONSE_RECEIVED"
        assert episode["revision_response_sha256"]
        assert episode["revision_character_count"] == len(value)
        assert episode["revision_exhausted"] is True
        raise KeyboardInterrupt

    monkeypatch.setattr("arc.pipeline.validate_prose", inspect_receipt)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)


def test_response_received_resume_holds_without_second_revision_call(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004"))
    monkeypatch.setattr("arc.pipeline.validate_prose", lambda value: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    monkeypatch.undo()
    resumed_client, resumed_root = _pilot_client(output)

    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_004" / "manifest.json")

    assert result["manifest"]["status"] == "HOLD"
    assert episode["revision_attempt_state"] == "REJECTED"
    assert episode["revision_contract_code"] == "REVISION_RESPONSE_ALREADY_CONSUMED"
    assert not any(marker == "revision:canonical" for _, marker, _ in resumed_root.provider_calls)


def test_transport_failure_does_not_consume_revision_and_resume_can_retry(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004"))
    original = client.generate_for_desk

    def fail_revision(*, desk, prompt):
        if desk.stage == "revision":
            raise LiveCallError("PROVIDER_5XX", "revision", "canonical", "K10", "injected", 500)
        return original(desk=desk, prompt=prompt)

    monkeypatch.setattr(client, "generate_for_desk", fail_revision)
    with pytest.raises(LiveCallError):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_004" / "manifest.json")
    assert episode["status"] == "ERROR"
    assert episode["revision_count"] == 0
    assert episode["revision_attempt_state"] == "NOT_STARTED"
    assert episode["revision_exhausted"] is False
    monkeypatch.undo()
    resumed_client, resumed_root = _pilot_client(output)

    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    assert sum(marker == "revision:canonical" for _, marker, _ in resumed_root.provider_calls) == 1


def test_transport_retry_attempts_count_as_one_logical_revision(tmp_path):
    output = tmp_path / "pilot-live"
    root = _PilotProviderRoot(fail_once_at="revision:canonical", repairable_writer_once_episode="episode_004")
    client, _ = _pilot_client(output, root)

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_004" / "manifest.json")
    calls = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "episode:episode_004" and call["stage"] == "revision"]

    assert result["manifest"]["status"] == "COMPLETE"
    assert [call["status"] for call in calls] == ["FAIL", "PASS"]
    assert episode["revision_count"] == 1
    assert episode["revision_attempt_state"] == "COMPLETED"


def test_rejected_revision_is_terminal_noop(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004", revision_text="B" * 3999))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = _file_bytes(output)
    resumed_client, resumed_root = _pilot_client(output)

    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert resumed_root.provider_calls == []
    assert _file_bytes(output) == before


@pytest.mark.parametrize("field,value", [("revision_count", 2), ("revision_attempt_state", "NOT_STARTED"), ("revision_response_sha256", "bad")])
def test_invalid_revision_state_fails_closed_before_provider_call(tmp_path, field, value):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_004", revision_text="B" * 3999))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode_path = output / "episodes" / "episode_004" / "manifest.json"
    episode = read_json(episode_path)
    episode[field] = value
    write_json(episode_path, episode)
    resumed_client, resumed_root = _pilot_client(output)

    with pytest.raises(Exception, match="invalid revision evidence"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []


def test_exact_legacy_writer_checkpoint_reconciles_to_hold_without_provider(tmp_path):
    output = _make_legacy_writer_output(tmp_path)
    before_telemetry = (output / "pilot_live_calls.json").read_bytes()
    before_routing = (output / "routing_state.json").read_bytes()
    client, root = _pilot_client(output)

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")

    assert result["manifest"]["status"] == "HOLD"
    assert episode["status"] == "HOLD"
    assert episode["writer_call_count"] == 1
    assert episode["writer_attempt_state"] == "REJECTED"
    assert episode["writer_character_count"] == 2858
    assert episode["writer_call_id"] == "L008-A001"
    assert episode["writer_lease_sequence"] == 15
    assert root.provider_calls == []
    assert (output / "pilot_live_calls.json").read_bytes() == before_telemetry
    assert (output / "routing_state.json").read_bytes() == before_routing
    assert not (output / "episode_sources" / "episode_002.json").exists()
    assert not (output / "pilot_acceptance.json").exists()


@pytest.mark.parametrize("ambiguity", ["response", "failure", "call_id", "characters", "contract", "duplicate_lease", "duplicate_call", "draft", "review", "episode_002"])
def test_ambiguous_legacy_writer_checkpoint_is_blocked_without_provider(tmp_path, ambiguity):
    output = _make_legacy_writer_output(tmp_path, ambiguous=ambiguity)
    client, root = _pilot_client(output)

    with pytest.raises(Exception, match="WRITER_RECONCILIATION_BLOCKED|artifact|checkpoint|duplicate"):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert root.provider_calls == []
    assert read_json(output / "pilot_manifest.json")["status"] == "ERROR"


def test_exact_legacy_revision_checkpoint_reconciles_to_hold_without_provider(tmp_path):
    output = _make_legacy_revision_output(tmp_path)
    client, root = _pilot_client(output)

    result = PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_004" / "manifest.json")

    assert result["manifest"]["status"] == "HOLD"
    assert episode["status"] == "HOLD"
    assert episode["revision_count"] == 1
    assert episode["revision_attempt_state"] == "REJECTED"
    assert episode["revision_character_count"] == 3949
    assert root.provider_calls == []
    assert not (output / "episodes" / "episode_005").exists()
    assert not (output / "pilot_acceptance.json").exists()


def test_ambiguous_legacy_revision_checkpoint_is_blocked_without_provider(tmp_path):
    output = _make_legacy_revision_output(tmp_path, ambiguous=True)
    client, root = _pilot_client(output)

    with pytest.raises(Exception, match="REVISION_RECONCILIATION_BLOCKED"):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert root.provider_calls == []
    assert read_json(output / "pilot_manifest.json")["status"] == "ERROR"


def test_normal_writer_path_does_not_force_revision(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    ep4 = read_json(output / "episodes" / "episode_004" / "manifest.json")
    assert read_json(output / "episodes" / "episode_004" / "draft_contract.json")["verdict"] == "PASS"
    assert ep4["writer_call_count"] == 1
    assert ep4["writer_attempt_state"] == "COMPLETED"
    assert ep4["writer_exhausted"] is True
    assert ep4["writer_contract_code"] is None
    assert ep4["revision_count"] == 0


def test_pilot_live_status_reports_valid_checkpoint_without_writes(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = _file_bytes(output)

    current = pilot_status(output)

    assert current["checkpoint_integrity"] == "VALID"
    assert current["reconciliation_required"] is False
    assert _file_bytes(output) == before


def test_pilot_live_status_reports_reconcilable_checkpoint_without_writes(tmp_path):
    from arc.pilot import pilot_status

    output = _make_reconcilable_pilot_output(tmp_path)
    before = _file_bytes(output)

    current = pilot_status(output)

    assert current["checkpoint_integrity"] == "RECONCILABLE"
    assert current["reconciliation_required"] is True
    assert current["derived"]["active_episode_id"] == "episode_004"
    assert current["derived"]["completed_episodes"] == ["episode_001", "episode_002", "episode_003"]
    assert current["derived"]["completed_transitions"] == ["episode_001_to_episode_002", "episode_002_to_episode_003", "episode_003_to_episode_004"]
    assert _file_bytes(output) == before


def test_pilot_live_status_rejects_corrupt_checkpoint_without_writes(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = _file_bytes(output)
    (output / "transitions" / "episode_001_to_episode_002.json").write_text("{}", encoding="utf-8")

    with pytest.raises(StorageError):
        pilot_status(output)
    assert (output / "pilot_live_calls.json").read_bytes() == before["pilot_live_calls.json"]


def test_reconcile_repairs_stale_active_episode_forward_only(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    result = reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")

    assert result["checkpoint_integrity"] == "VALID"
    assert manifest["active_episode_id"] == "episode_004"
    assert "ACTIVE_EPISODE_STALE" in manifest["checkpoint_reconciliation"]["reason_codes"]


def test_reconcile_repairs_completed_episode_prefix_forward_only(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")

    assert manifest["completed_episodes"] == ["episode_001", "episode_002", "episode_003"]


def test_reconcile_repairs_completed_transition_prefix_forward_only(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")

    assert manifest["completed_transitions"] == ["episode_001_to_episode_002", "episode_002_to_episode_003", "episode_003_to_episode_004"]


def test_reconcile_rebuilds_missing_episode_records(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    manifest["episode_records"] = []
    write_json(output / "pilot_manifest.json", manifest)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    assert len(read_json(output / "pilot_manifest.json")["episode_records"]) == 3


def test_reconcile_migrates_legacy_telemetry_hash(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")

    assert "pilot_live_calls.json" not in manifest["artifact_hashes"]
    assert manifest["live_telemetry_checkpoint"] == live_telemetry_checkpoint(read_json(output / "pilot_live_calls.json"))


def test_reconcile_writes_only_pilot_manifest(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    before = _file_bytes(output)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    after = _file_bytes(output)
    changed = {name for name in before if before[name] != after.get(name)}

    assert changed == {"pilot_manifest.json"}


def test_reconcile_is_noop_after_success(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    before = _file_bytes(output)

    result = reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert _file_bytes(output) == before


def test_reconcile_rejects_manifest_ahead_of_child_evidence(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    manifest["completed_episodes"] = manifest["episode_ids"]
    write_json(output / "pilot_manifest.json", manifest)

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"
    with pytest.raises(StorageError):
        reconcile_pilot_checkpoint(PILOT_FIXTURE, output)


def test_reconcile_rejects_non_contiguous_episode_prefix(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    shutil.copytree(output / "episodes" / "episode_003", output / "episodes" / "episode_005")
    manifest = read_json(output / "episodes" / "episode_005" / "manifest.json")
    manifest["episode_id"] = "episode_005"
    write_json(output / "episodes" / "episode_005" / "manifest.json", manifest)

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


def test_reconcile_rejects_non_contiguous_transition_prefix(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    (output / "transitions" / "episode_002_to_episode_003.json").unlink()

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


def test_reconcile_rejects_immutable_artifact_mismatch(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    (output / "episode_sources" / "episode_001.json").write_text("{}", encoding="utf-8")

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


def test_reconcile_rejects_telemetry_prefix_tamper(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    telemetry = read_json(output / "pilot_live_calls.json")
    telemetry["calls"][0]["call_id"] = "tampered"
    write_json(output / "pilot_live_calls.json", telemetry)

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


def test_reconcile_rejects_projection_conflict(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    episode_live = output / "episodes" / "episode_003" / "live_calls.json"
    value = read_json(episode_live)
    value["calls"][0]["key_slot"] = "tampered"
    write_json(episode_live, value)

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"
    with pytest.raises(StorageError):
        reconcile_pilot_checkpoint(PILOT_FIXTURE, output)


def test_reconcile_repairs_stale_prefix_projection(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    episode_live = output / "episodes" / "episode_003" / "live_calls.json"
    value = read_json(episode_live)
    value["calls"] = value["calls"][:2]
    write_json(episode_live, value)

    inspection = inspect_pilot_checkpoint(output)
    assert inspection["checkpoint_integrity"] == "RECONCILABLE"
    assert "EPISODE_PROJECTION_STALE" in inspection["reason_codes"]

    result = reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    assert result["checkpoint_integrity"] == "VALID"
    assert "episodes/episode_003/live_calls.json" in result["changed_files"]
    root_calls = read_json(output / "pilot_live_calls.json")["calls"]
    projection = read_json(episode_live)
    assert projection["calls"] == [call for call in root_calls if call["scope_id"] == "episode:episode_003"]


def test_reconcile_rejects_routing_state_behind_telemetry(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = 1
    write_json(output / "routing_state.json", routing)

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


def test_pilot_live_run_refuses_reconcilable_output_before_provider_call(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    fresh_client, provider_root = _pilot_client(output)

    with pytest.raises(Exception, match="pilot checkpoint reconciliation required"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert provider_root.provider_calls == []


def test_pilot_live_run_refuses_corrupt_output_before_provider_call(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    (output / "episode_sources" / "episode_001.json").write_text("{}", encoding="utf-8")
    fresh_client, provider_root = _pilot_client(output)

    with pytest.raises(StorageError):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert provider_root.provider_calls == []


def test_pilot_live_reconcile_does_not_load_provider_client(tmp_path, monkeypatch):
    output = _make_reconcilable_pilot_output(tmp_path)
    monkeypatch.setenv("GOOGLE_API_KEY_1", "")

    result = reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    assert result["checkpoint_integrity"] == "VALID"


def test_legacy_frozen_checkpoint_is_reconcilable(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    inspection = inspect_pilot_checkpoint(output)

    assert inspection["checkpoint_integrity"] == "RECONCILABLE"
    assert "LEGACY_TELEMETRY_HASH_STALE" in inspection["reason_codes"]


def test_legacy_frozen_status_is_read_only(tmp_path):
    from arc.pilot import pilot_status

    output = _make_reconcilable_pilot_output(tmp_path)
    before = _file_bytes(output)

    current = pilot_status(output)

    assert current["checkpoint_integrity"] == "RECONCILABLE"
    assert _file_bytes(output) == before


def test_legacy_frozen_reconcile_changes_only_manifest(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    before = _file_bytes(output)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    after = _file_bytes(output)

    assert {name for name in before if before[name] != after.get(name)} == {"pilot_manifest.json"}


def test_legacy_frozen_reconcile_derives_episode_four(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    assert read_json(output / "pilot_manifest.json")["active_episode_id"] == "episode_004"


def test_legacy_frozen_reconcile_preserves_error_status(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    assert read_json(output / "pilot_manifest.json")["status"] == "ERROR"


def test_legacy_frozen_reconcile_migrates_telemetry_checkpoint(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)

    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")

    assert "pilot_live_calls.json" not in manifest["artifact_hashes"]
    assert manifest["live_telemetry_checkpoint"]["call_count"] == len(read_json(output / "pilot_live_calls.json")["calls"])


def test_legacy_frozen_reconciled_output_resumes_from_planning_merge(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    fresh_client, provider_root = _pilot_client(output)

    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    resumed = {(_episode_from_prompt(prompt), marker) for _, marker, prompt in provider_root.provider_calls}
    assert ("episode_004", "planning_merge:merge") in resumed
    assert not any(episode in {"episode_001", "episode_002", "episode_003"} for episode, _ in resumed)
    assert not any(episode == "episode_004" and marker.startswith("planning:") for episode, marker in resumed)


def test_crash_after_telemetry_before_manifest_is_reconcilable(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    telemetry = read_json(output / "pilot_live_calls.json")
    extra = dict(telemetry["calls"][-1])
    extra["call_id"] = "L999-A999"
    extra["lease_sequence"] = max(call["lease_sequence"] for call in telemetry["calls"]) + 1
    extra["scope_id"] = "pilot:acceptance"
    extra["desk_id"] = "pilot:acceptance:pilot_review:readability"
    telemetry["calls"].append(extra)
    write_json(output / "pilot_live_calls.json", telemetry)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = extra["lease_sequence"] + 1
    write_json(output / "routing_state.json", routing)
    write_json(output / "pilot_manifest.json", manifest)

    inspection = inspect_pilot_checkpoint(output)

    assert inspection["checkpoint_integrity"] == "RECONCILABLE"
    assert "TELEMETRY_APPEND_AFTER_CHECKPOINT" in inspection["reason_codes"]


def test_crash_after_child_complete_before_root_checkpoint_is_reconcilable(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    manifest["completed_episodes"] = ["episode_001", "episode_002"]
    manifest["episode_records"] = manifest["episode_records"][:2]
    write_json(output / "pilot_manifest.json", manifest)

    inspection = inspect_pilot_checkpoint(output)

    assert inspection["checkpoint_integrity"] == "RECONCILABLE"
    assert inspection["derived"]["completed_episodes"] == ["episode_001", "episode_002", "episode_003"]


def test_crash_after_transition_before_root_checkpoint_is_reconcilable(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    manifest = read_json(output / "pilot_manifest.json")
    manifest["completed_transitions"] = ["episode_001_to_episode_002", "episode_002_to_episode_003"]
    write_json(output / "pilot_manifest.json", manifest)

    inspection = inspect_pilot_checkpoint(output)

    assert inspection["checkpoint_integrity"] == "RECONCILABLE"
    assert inspection["derived"]["completed_transitions"] == ["episode_001_to_episode_002", "episode_002_to_episode_003", "episode_003_to_episode_004"]


def test_half_written_transition_checkpoint_is_corrupt(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    (output / "episode_sources" / "episode_004.json").unlink()

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


def test_live_pilot_uses_one_root_routing_state(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert (output / "routing_state.json").exists()
    assert read_json(output / "routing_state.json")["routing_mode"] == "dynamic_key_pool"


def test_live_pilot_does_not_create_episode_routing_states(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert not list((output / "episodes").glob("*/routing_state.json"))


def test_live_pilot_preserves_pool_cursor_between_episodes(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    calls = read_json(output / "pilot_live_calls.json")["calls"]
    lease_sequences = [call["lease_sequence"] for call in calls]
    assert sorted(lease_sequences) == list(range(1, len(calls) + 1))
    assert max(lease_sequences) == len(calls)


def test_live_pilot_resumes_interrupted_episode_without_recalling_completed_desks(tmp_path):
    output = _make_interrupted_episode_output(tmp_path)
    before_calls = read_json(output / "pilot_live_calls.json")["calls"]
    before_call_ids = {call["call_id"] for call in before_calls}

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    resumed = read_json(output / "pilot_live_calls.json")["calls"]
    resumed_prompts = {(_episode_from_prompt(prompt), marker) for _, marker, prompt in provider_root.provider_calls}

    assert result["no_op"] is False
    assert result["manifest"]["status"] == "COMPLETE"
    assert ("episode_001", "writer:canonical") not in resumed_prompts
    assert ("episode_002", "planning:event") not in resumed_prompts
    assert ("episode_002", "planning:protagonist_action") not in resumed_prompts
    assert {role for episode, marker in resumed_prompts if episode == "episode_002" and marker.startswith("planning:") for role in [marker.split(":", 1)[1]]} == {"relationship", "continuity", "readability_weight", "reader_payoff"}
    assert {"episode_003", "episode_004", "episode_005"}.issubset({episode for episode, _ in resumed_prompts})
    assert before_call_ids.issubset({call["call_id"] for call in resumed})
    assert len({call["call_id"] for call in resumed}) == len(resumed)
    assert len({call["lease_sequence"] for call in resumed}) == len(resumed)
    assert all(call["error_class"] is None for call in resumed if call["call_id"] in before_call_ids)
    episode2_projection = read_json(output / "episodes" / "episode_002" / "live_calls.json")["calls"]
    assert episode2_projection == [call for call in resumed if call["scope_id"] == "episode:episode_002"]


def test_live_pilot_interrupted_episode_preserves_global_attempt_and_lease_sequence(tmp_path):
    output = _make_interrupted_episode_output(tmp_path)
    before = read_json(output / "pilot_live_calls.json")["calls"]
    max_lease = max(call["lease_sequence"] for call in before)

    fresh_client, _ = _pilot_client(output)
    PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after = read_json(output / "pilot_live_calls.json")["calls"]
    new_calls = [call for call in after if call["lease_sequence"] > max_lease]

    assert min(call["lease_sequence"] for call in new_calls) == max_lease + 1
    assert [call["attempt"] for call in after if call["desk_id"] == "episode:episode_002:planning:event"] == [1]
    assert [call["attempt"] for call in after if call["desk_id"] == "episode:episode_002:planning:relationship"][0] == 1
    assert sorted(call["lease_sequence"] for call in after) == list(range(1, len(after) + 1))


def test_live_pilot_interrupted_episode_rejects_root_projection_conflict(tmp_path):
    output = _make_interrupted_episode_output(tmp_path)
    episode_path = output / "episodes" / "episode_002" / "live_calls.json"
    episode_calls = read_json(episode_path)
    episode_calls["calls"][0]["key_slot"] = "tampered"
    write_json(episode_path, episode_calls)
    before = episode_path.read_bytes()

    fresh_client, provider_root = _pilot_client(output)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert provider_root.provider_calls == []
    assert episode_path.read_bytes() == before


def test_live_pilot_interrupted_episode_recovers_stale_prefix_projection(tmp_path):
    output = _make_interrupted_episode_output(tmp_path)
    episode_path = output / "episodes" / "episode_002" / "live_calls.json"
    episode_calls = read_json(episode_path)
    episode_calls["calls"] = []
    write_json(episode_path, episode_calls)

    fresh_client, _ = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    root_calls = read_json(output / "pilot_live_calls.json")["calls"]
    projection = read_json(episode_path)
    assert projection["calls"] == [call for call in root_calls if call["scope_id"] == "episode:episode_002"]
    assert len({call["call_id"] for call in root_calls}) == len(root_calls)
    assert len({call["lease_sequence"] for call in root_calls}) == len(root_calls)


def test_planning_merge_contract_failure_keeps_provider_call_pass(tmp_path):
    _, _, telemetry, _ = _run_planning_merge_contract_failure(tmp_path)
    call = next(call for call in telemetry["calls"] if call["stage"] == "planning_merge")
    assert call["status"] == "PASS"
    assert call["error_class"] is None


def test_planning_merge_contract_failure_records_contract_event(tmp_path):
    _, _, telemetry, _ = _run_planning_merge_contract_failure(tmp_path)
    event = _planning_merge_contract_event(telemetry)
    assert event["error_class"] == "CONTRACT_ERROR"
    assert event["stage"] == "planning_merge"
    assert event["role"] == "merge"


def test_planning_merge_contract_failure_uses_actual_key_slot(tmp_path):
    _, _, telemetry, _ = _run_planning_merge_contract_failure(tmp_path)
    call = next(call for call in telemetry["calls"] if call["stage"] == "planning_merge")
    event = _planning_merge_contract_event(telemetry)
    assert event["key_slot"] == call["key_slot"]
    assert event["key_slot"] != "UNKNOWN"


def test_planning_merge_contract_failure_records_contract_code(tmp_path):
    _, _, telemetry, manifest = _run_planning_merge_contract_failure(tmp_path)
    event = _planning_merge_contract_event(telemetry)
    assert event["contract_code"] == "PLAN_EPISODE_ID_MISMATCH"
    assert manifest["last_error"]["contract_code"] == "PLAN_EPISODE_ID_MISMATCH"


def test_planning_merge_contract_failure_has_no_raw_response(tmp_path):
    output, _, telemetry, _ = _run_planning_merge_contract_failure(tmp_path)
    serialized = json.dumps(telemetry, ensure_ascii=False)
    assert "wrong_episode" not in serialized
    assert "raw_response" not in serialized
    assert not (output / "episode_plan.json").exists()


def test_planning_merge_contract_failure_writes_structured_episode_error(tmp_path):
    _, _, _, manifest = _run_planning_merge_contract_failure(tmp_path)
    assert manifest["status"] == "ERROR"
    assert manifest["last_error"] == {"error_class": "CONTRACT_ERROR", "stage": "planning_merge", "role": "merge", "contract_code": "PLAN_EPISODE_ID_MISMATCH", "key_slot": manifest["last_error"]["key_slot"], "http_status": None, "provider_code": None, "message": "sanitized planning merge contract failure"}


def test_planning_merge_contract_failure_writes_structured_pilot_error(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(wrong_plan_once=True))
    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")
    assert manifest["status"] == "ERROR"
    assert manifest["last_error"]["error_class"] == "CONTRACT_ERROR"
    assert manifest["last_error"]["stage"] == "planning_merge"
    assert manifest["last_error"]["contract_code"] == "PLAN_EPISODE_ID_MISMATCH"


def test_status_does_not_duplicate_contract_failure(tmp_path):
    output, _, telemetry, _ = _run_planning_merge_contract_failure(tmp_path)
    before = len(telemetry["contract_failures"])
    status(output)
    after = len(read_json(output / "live_calls.json")["contract_failures"])
    assert after == before


def test_restored_telemetry_keeps_contract_failure_event_ids_unique(tmp_path):
    _, _, telemetry, _ = _run_planning_merge_contract_failure(tmp_path)
    client, _ = _client(tmp_path / "restore")
    client.restore_telemetry(telemetry)
    client.record_contract_failure("planning_merge", "merge", contract_code="PLAN_FIELDS_MISMATCH")
    event_ids = [event["event_id"] for event in client.telemetry()["contract_failures"]]
    assert len(event_ids) == len(set(event_ids))


def test_pilot_resumes_episode_four_from_planning_merge_contract_error(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    result, provider_root = _resume_episode_four_output(output)
    manifest = result["manifest"]
    assert manifest["status"] == "COMPLETE"
    assert manifest["completed_episodes"] == manifest["episode_ids"]
    assert len(manifest["completed_transitions"]) == 4
    assert any(marker == "planning_merge:merge" and _episode_from_prompt(prompt) == "episode_004" for _, marker, prompt in provider_root.provider_calls)


def test_planning_merge_resume_does_not_recall_planning_workers(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    _, provider_root = _resume_episode_four_output(output)
    resumed = {(_episode_from_prompt(prompt), marker) for _, marker, prompt in provider_root.provider_calls}
    assert not any(episode in {"episode_001", "episode_002", "episode_003"} for episode, _ in resumed)
    assert not any(episode == "episode_004" and marker.startswith("planning:") for episode, marker in resumed)


def test_planning_merge_resume_preserves_contract_failure_and_attempt_sequence(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    before = read_json(output / "pilot_live_calls.json")
    before_events = list(before["contract_failures"])
    _resume_episode_four_output(output)
    after = read_json(output / "pilot_live_calls.json")
    merge_calls = [call for call in after["calls"] if call["desk_id"] == "episode:episode_004:planning_merge:merge"]
    assert before_events[0] in after["contract_failures"]
    assert [call["attempt"] for call in merge_calls] == [1, 2]
    assert [call["status"] for call in merge_calls] == ["PASS", "PASS"]
    assert len({call["call_id"] for call in after["calls"]}) == len(after["calls"])
    assert len({call["lease_sequence"] for call in after["calls"]}) == len(after["calls"])


def test_planning_merge_resume_does_not_rebuild_completed_pilot_prefix(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    before = {name: (output / name).read_bytes() for name in ["transitions/episode_001_to_episode_002.json", "transitions/episode_002_to_episode_003.json", "transitions/episode_003_to_episode_004.json"]}
    _resume_episode_four_output(output)
    after = {name: (output / name).read_bytes() for name in before}
    assert after == before


def test_planning_merge_resume_keeps_key_available_after_contract_failure(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    before_state = read_json(output / "routing_state.json")
    event = read_json(output / "pilot_live_calls.json")["contract_failures"][0]
    assert before_state["keys"][event["key_slot"]]["state"] == "AVAILABLE"
    _resume_episode_four_output(output)
    after_state = read_json(output / "routing_state.json")
    assert after_state["keys"][event["key_slot"]]["state"] in {"AVAILABLE", "COOLDOWN"}


def test_planning_merge_resume_rejects_tampered_planning_workers(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    planning = output / "episodes" / "episode_004" / "planning_workers.json"
    planning.write_text("[]\n", encoding="utf-8")
    with pytest.raises(StorageError):
        _resume_episode_four_output(output)


def test_planning_merge_resume_rejects_root_projection_conflict(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    episode_live = output / "episodes" / "episode_004" / "live_calls.json"
    value = read_json(episode_live)
    value["calls"][0]["key_slot"] = "tampered"
    write_json(episode_live, value)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        _resume_episode_four_output(output)


def test_planning_merge_resume_recovers_stale_prefix_projection(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    episode_live = output / "episodes" / "episode_004" / "live_calls.json"
    value = read_json(episode_live)
    value["calls"] = value["calls"][:1]
    write_json(episode_live, value)

    result, _ = _resume_episode_four_output(output)

    assert result["manifest"]["status"] == "COMPLETE"
    root_calls = read_json(output / "pilot_live_calls.json")["calls"]
    projection = read_json(episode_live)
    assert projection["calls"] == [call for call in root_calls if call["scope_id"] == "episode:episode_004"]


def test_planning_merge_resume_rejects_stale_planning_partial(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    write_json(output / "episodes" / "episode_004" / "planning_workers.partial.json", {"stale": True})
    with pytest.raises(Exception, match="stale planning partial"):
        _resume_episode_four_output(output)


def test_planning_merge_resume_rejects_routing_sequence_behind_telemetry(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = 1
    write_json(output / "routing_state.json", routing)
    with pytest.raises(StorageError, match="routing lease sequence behind telemetry"):
        _resume_episode_four_output(output)


def test_planning_merge_resume_rejects_episode_source_hash_mismatch(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    source = read_json(output / "episode_sources" / "episode_004.json")
    source["current_episode"]["required_role"] = "tampered"
    write_json(output / "episode_sources" / "episode_004.json", source)
    with pytest.raises(StorageError):
        _resume_episode_four_output(output)


def test_planning_merge_resume_rejects_plan_merged_without_artifact(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    manifest_path = output / "episodes" / "episode_004" / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["completed_stages"].append("PLAN_MERGED")
    write_json(manifest_path, manifest)
    with pytest.raises(Exception, match="PLAN_MERGED without episode plan"):
        _resume_episode_four_output(output)


def test_live_pilot_rejects_root_and_episode_telemetry_conflict(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode_path = output / "episodes" / "episode_001" / "live_calls.json"
    episode_calls = read_json(episode_path)
    episode_calls["calls"][0]["key_slot"] = "tampered"
    write_json(episode_path, episode_calls)

    fresh_client, provider_root = _pilot_client(output)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert provider_root.provider_calls == []


def test_live_pilot_recovers_missing_completed_episode_projection(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode_path = output / "episodes" / "episode_001" / "live_calls.json"
    episode_path.unlink()

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert provider_root.provider_calls == []
    root = read_json(output / "pilot_live_calls.json")
    projection = read_json(episode_path)
    assert projection["calls"] == [call for call in root["calls"] if call["scope_id"] == "episode:episode_001"]
    assert all(item["scope_id"] == "episode:episode_001" for item in projection["contract_failures"])


def test_live_pilot_transient_rotation_continues_same_desk(tmp_path):
    output = tmp_path / "pilot-live"
    provider_root = _PilotProviderRoot(fail_once_at="planning:event")
    client, _ = _pilot_client(output, provider_root)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    calls = read_json(output / "pilot_live_calls.json")["calls"]
    first_desk = [call for call in calls if call["desk_id"] == "episode:episode_001:planning:event"]
    assert [call["attempt"] for call in first_desk[:2]] == [1, 2]
    assert [call["status"] for call in first_desk[:2]] == ["FAIL", "PASS"]
    assert first_desk[0]["key_slot"] != first_desk[1]["key_slot"]


def test_live_pilot_complete_rerun_is_noop(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = _file_bytes(output)
    before_routing = (output / "routing_state.json").read_bytes()
    before_telemetry = (output / "pilot_live_calls.json").read_bytes()
    before_manifest = (output / "pilot_manifest.json").read_bytes()

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after = _file_bytes(output)

    assert result["no_op"] is True
    assert provider_root.provider_calls == []
    assert (output / "routing_state.json").read_bytes() == before_routing
    assert (output / "pilot_live_calls.json").read_bytes() == before_telemetry
    assert (output / "pilot_manifest.json").read_bytes() == before_manifest
    assert after == before


def test_live_episode_hold_rerun_is_noop(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(hold_episode="episode_003"))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = _file_bytes(output)
    before_routing = (output / "routing_state.json").read_bytes()
    before_telemetry = (output / "pilot_live_calls.json").read_bytes()
    before_manifest = (output / "pilot_manifest.json").read_bytes()
    before_calls = len(read_json(output / "pilot_live_calls.json")["calls"])

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert provider_root.provider_calls == []
    assert len(read_json(output / "pilot_live_calls.json")["calls"]) == before_calls
    assert (output / "routing_state.json").read_bytes() == before_routing
    assert (output / "pilot_live_calls.json").read_bytes() == before_telemetry
    assert (output / "pilot_manifest.json").read_bytes() == before_manifest
    assert _file_bytes(output) == before
    assert not (output / "episodes" / "episode_004").exists()
    assert not (output / "pilot_review_workers.partial.json").exists()


def test_live_pilot_hold_rerun_is_noop(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(hold_dimension="continuity"))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = _file_bytes(output)
    before_routing = (output / "routing_state.json").read_bytes()
    before_telemetry = (output / "pilot_live_calls.json").read_bytes()
    before_manifest = (output / "pilot_manifest.json").read_bytes()
    before_calls = len(read_json(output / "pilot_live_calls.json")["calls"])

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert read_json(output / "pilot_manifest.json")["status"] == "HOLD"
    assert result["no_op"] is True
    assert provider_root.provider_calls == []
    assert len(read_json(output / "pilot_live_calls.json")["calls"]) == before_calls
    assert (output / "routing_state.json").read_bytes() == before_routing
    assert (output / "pilot_live_calls.json").read_bytes() == before_telemetry
    assert (output / "pilot_manifest.json").read_bytes() == before_manifest
    assert _file_bytes(output) == before


def _acceptance_prompts(provider_root: _PilotProviderRoot) -> list[str]:
    return [prompt for _, marker, prompt in provider_root.provider_calls if marker.startswith("pilot_review:")]


def test_live_acceptance_prompt_contains_dimension_rubric_and_catalog(tmp_path):
    output = tmp_path / "pilot-live"
    client, provider_root = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    payloads = [json.loads(prompt) for prompt in _acceptance_prompts(provider_root)]
    manifest = read_json(output / "pilot_manifest.json")
    assert len(payloads) == 7
    questions = {payload["dimension_question"] for payload in payloads}
    assert len(questions) == 7
    assert all(ACCEPTANCE_GENERIC_QUESTION_MARKER not in question for question in questions)
    for payload in payloads:
        assert payload["pilot_id"] == manifest["pilot_id"]
        assert payload["acceptance_rubric_version"] == 1
        assert payload["acceptance_provider_contract_version"] == ACCEPTANCE_PROVIDER_CONTRACT_VERSION
        assert payload["evidence_candidate_catalog_version"] == EVIDENCE_CANDIDATE_CATALOG_VERSION
        assert all(criterion["criterion_id"].startswith(f"{payload['dimension']}.") for criterion in payload["criteria"])
        assert 2 <= len(payload["criteria"]) <= 4
        assert len(payload["artifact_metadata"]) == 34
        assert {entry["kind"] for entry in payload["artifact_metadata"]} == {"episode_final", "episode_plan", "episode_review", "episode_memory_update", "episode_memory_after", "episode_source", "transition"}
        refs = {entry["ref_id"]: entry for entry in payload["evidence_ref_catalog"]}
        assert payload["evidence_candidates"] == sorted(payload["evidence_candidates"], key=lambda entry: (refs[entry["ref_id"]]["ref"], entry["ordinal"]))
        assert all(set(entry) == {"candidate_id", "ref_id", "ordinal", "excerpt"} for entry in payload["evidence_candidates"])
        assert all("content" not in entry for entry in payload["evidence_candidates"])
        assert set(payload["strict_output_schema"]["proposal"]) == {"dimension_result", "criterion_results", "critical_finding", "strengths"}
        assert "evidence_refs" not in payload["strict_output_schema"]
        assert "coverage_refs" not in payload["strict_output_schema"]["proposal"]
        assert "coverage_rule" in payload and "evidence_contract" in payload


def test_live_acceptance_prompt_is_deterministic(tmp_path):
    first_output = tmp_path / "first"
    first_client, first_root = _pilot_client(first_output)
    PilotPipeline(first_client, scenario=None, mode="live").run(PILOT_FIXTURE, first_output)

    second_output = tmp_path / "second"
    second_client, second_root = _pilot_client(second_output)
    PilotPipeline(second_client, scenario=None, mode="live").run(PILOT_FIXTURE, second_output)

    assert sorted(_acceptance_prompts(first_root)) == sorted(_acceptance_prompts(second_root))


def test_live_acceptance_receipts_preserve_candidate_only_provider_responses(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    for role in PILOT_REVIEW_ROLES:
        response = json.loads(read_json(output / "pilot_review_receipts" / f"{role}.response.json")["raw_response"])
        assert "evidence_refs" not in response
        assert "coverage_refs" not in response["proposal"]
        assert all("evidence" not in result and "evidence_candidate_ids" in result for result in response["proposal"]["criterion_results"])
        assert all("evidence" not in strength and "evidence_candidate_ids" in strength for strength in response["proposal"]["strengths"])


def test_live_acceptance_uses_shared_base_client(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    calls = read_json(output / "pilot_live_calls.json")["calls"]
    assert {call["scope_id"] for call in calls if call["scope_id"] == "pilot:acceptance"} == {"pilot:acceptance"}
    assert client.scope(scope_id="pilot:acceptance", logical_order_base=500).pool is client.pool


def test_live_acceptance_calls_seven_scoped_desks(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    calls = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "pilot:acceptance" and call["status"] == "PASS"]
    assert [call["role"] for call in calls] == ["readability", "character_consistency", "continuity", "rolling_plan_adaptation", "memory_correctness", "narrative_weight", "episode_to_episode_interest"]


def test_live_acceptance_resume_calls_only_missing_dimensions(tmp_path):
    output = _make_acceptance_partial_output(tmp_path)
    before_calls = read_json(output / "pilot_live_calls.json")["calls"]
    before_acceptance = [call for call in before_calls if call["scope_id"] == "pilot:acceptance"]

    fresh_client, fresh_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    markers = [marker for _, marker, _ in fresh_root.provider_calls]

    assert result["no_op"] is False
    assert result["manifest"]["status"] == "COMPLETE"
    assert not any(_episode_from_prompt(prompt) for _, _, prompt in fresh_root.provider_calls)
    assert not {"pilot_review:readability", "pilot_review:character_consistency", "pilot_review:continuity"} & set(markers)
    assert {marker for marker in markers if marker.startswith("pilot_review:")} == {"pilot_review:rolling_plan_adaptation", "pilot_review:memory_correctness", "pilot_review:narrative_weight", "pilot_review:episode_to_episode_interest"}
    after_acceptance = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "pilot:acceptance"]
    assert {call["call_id"] for call in before_acceptance}.issubset({call["call_id"] for call in after_acceptance})
    assert not (output / "pilot_review_workers.partial.json").exists()
    assert (output / "pilot_review_workers.json").exists()
    assert (output / "pilot_acceptance.json").exists()


def test_live_acceptance_partial_resume_preserves_existing_telemetry(tmp_path):
    output = _make_acceptance_partial_output(tmp_path)
    before_calls = read_json(output / "pilot_live_calls.json")["calls"]
    before_completed = [call for call in before_calls if call["scope_id"] == "pilot:acceptance"]

    fresh_client, _ = _pilot_client(output)
    PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after_calls = read_json(output / "pilot_live_calls.json")["calls"]

    for call in before_completed:
        assert call in after_calls
    assert len({call["call_id"] for call in after_calls}) == len(after_calls)
    assert len({call["lease_sequence"] for call in after_calls}) == len(after_calls)


def test_live_acceptance_partial_resume_rotates_key_for_missing_dimension(tmp_path):
    output = _make_acceptance_partial_output(tmp_path)
    provider_root = _PilotProviderRoot(fail_once_at="pilot_review:memory_correctness", fail_status_code=429)
    fresh_client, _ = _pilot_client(output, provider_root)

    PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    calls = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["desk_id"] == "pilot:acceptance:pilot_review:memory_correctness"]
    prompts = [prompt for _, marker, prompt in provider_root.provider_calls if marker == "pilot_review:memory_correctness"]
    assert [call["status"] for call in calls] == ["FAIL", "PASS"]
    assert [call["attempt"] for call in calls] == [1, 2]
    assert calls[0]["error_class"] == "RATE_LIMITED"
    assert calls[0]["key_slot"] != calls[1]["key_slot"]
    assert prompts[0] == prompts[1]
    assert read_json(output / "pilot_manifest.json")["status"] == "COMPLETE"


def test_live_acceptance_partial_resume_rejects_evidence_hash_mismatch(tmp_path):
    output = _make_acceptance_partial_output(tmp_path)
    partial = read_json(output / "pilot_review_workers.partial.json")
    partial["wave_input_hash"] = "0" * 64
    write_json(output / "pilot_review_workers.partial.json", partial)

    fresh_client, _ = _pilot_client(output)
    with pytest.raises(Exception, match="invalid wave checkpoint"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)


def test_live_acceptance_429_rotates_key_and_preserves_prompt(tmp_path):
    output = tmp_path / "pilot-live"
    provider_root = _PilotProviderRoot(fail_once_at="pilot_review:continuity")
    client, _ = _pilot_client(output, provider_root)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    calls = [call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["desk_id"] == "pilot:acceptance:pilot_review:continuity"]
    prompts = [prompt for _, marker, prompt in provider_root.provider_calls if marker == "pilot_review:continuity"]
    assert [call["attempt"] for call in calls[:2]] == [1, 2]
    assert [call["status"] for call in calls[:2]] == ["FAIL", "PASS"]
    assert calls[0]["key_slot"] != calls[1]["key_slot"]
    assert prompts[0] == prompts[1]


def test_live_acceptance_terminal_error_preserves_other_successes(tmp_path):
    output = tmp_path / "pilot-live"
    provider_root = _PilotProviderRoot()
    provider_root.malformed_once_at = "pilot_review:continuity"
    client, _ = _pilot_client(output, provider_root)

    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    partial = read_json(output / "pilot_review_workers.partial.json")
    assert partial["completed_desks"]
    assert "pilot_acceptance.json" not in read_json(output / "pilot_manifest.json")["artifact_hashes"]
    assert read_json(output / "pilot_live_calls.json")["contract_failures"]


@pytest.mark.parametrize("completed_count", [0, 1, 6, 7])
def test_live_acceptance_interruption_resume_calls_only_missing(tmp_path, completed_count):
    completed = PILOT_REVIEW_ROLES[:completed_count]
    output = _make_acceptance_state(tmp_path, list(completed))
    fresh_client, fresh_root = _pilot_client(output)

    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    markers = {marker for _, marker, _ in fresh_root.provider_calls if marker.startswith("pilot_review:")}
    assert markers == {f"pilot_review:{role}" for role in PILOT_REVIEW_ROLES if role not in completed}
    assert not any(_episode_from_prompt(prompt) for _, _, prompt in fresh_root.provider_calls)
    assert result["manifest"]["status"] == "COMPLETE"
    assert (output / "pilot_acceptance.json").exists()
    assert not (output / "pilot_review_workers.partial.json").exists()
    for role in PILOT_REVIEW_ROLES:
        assert read_json(output / "pilot_review_receipts" / f"{role}.response.json")["state"] == "COMPLETED"


def test_live_acceptance_receipt_reuses_stored_response_without_call(tmp_path):
    completed = [role for role in PILOT_REVIEW_ROLES if role != "episode_to_episode_interest"]
    output = _make_acceptance_state(tmp_path, completed, receipt_roles=list(PILOT_REVIEW_ROLES), telemetry_roles=list(PILOT_REVIEW_ROLES))
    receipt_path = output / "pilot_review_receipts" / "episode_to_episode_interest.response.json"
    receipt = read_json(receipt_path)
    receipt.update({"state": "RESPONSE_RECEIVED", "contract_code": None})
    write_json(receipt_path, receipt)

    fresh_client, fresh_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert fresh_root.provider_calls == []
    assert result["manifest"]["status"] == "COMPLETE"
    assert read_json(receipt_path)["state"] == "COMPLETED"


def test_live_acceptance_pass_without_receipt_fails_closed(tmp_path):
    completed = [role for role in PILOT_REVIEW_ROLES if role != "memory_correctness"]
    output = _make_acceptance_state(tmp_path, completed, receipt_roles=completed, telemetry_roles=list(PILOT_REVIEW_ROLES))

    fresh_client, fresh_root = _pilot_client(output)
    with pytest.raises(PilotError, match="PILOT_REVIEW_RECONCILIATION_REQUIRED"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert fresh_root.provider_calls == []


def test_live_acceptance_rejected_receipt_fails_closed(tmp_path):
    completed = [role for role in PILOT_REVIEW_ROLES if role != "narrative_weight"]
    output = _make_acceptance_state(tmp_path, completed, receipt_roles=list(PILOT_REVIEW_ROLES), telemetry_roles=list(PILOT_REVIEW_ROLES))
    receipt_path = output / "pilot_review_receipts" / "narrative_weight.response.json"
    receipt = read_json(receipt_path)
    receipt.update({"state": "REJECTED", "contract_code": "PILOT_REVIEW_EVIDENCE_INVALID"})
    write_json(receipt_path, receipt)

    fresh_client, fresh_root = _pilot_client(output)
    with pytest.raises(PilotError, match="PILOT_REVIEW_RESPONSE_ALREADY_CONSUMED"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert fresh_root.provider_calls == []


def test_live_acceptance_completed_receipt_without_worker_fails_closed(tmp_path):
    completed = [role for role in PILOT_REVIEW_ROLES if role != "narrative_weight"]
    output = _make_acceptance_state(tmp_path, completed, receipt_roles=list(PILOT_REVIEW_ROLES), telemetry_roles=list(PILOT_REVIEW_ROLES))

    fresh_client, fresh_root = _pilot_client(output)
    with pytest.raises(PilotError, match="completed acceptance receipt without checkpointed worker"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert fresh_root.provider_calls == []


@pytest.mark.parametrize("field", ["raw_response", "response_sha256", "evidence_packet_hash"])
def test_live_tampered_acceptance_receipt_fails_closed(tmp_path, field):
    output = _make_acceptance_partial_output(tmp_path)
    receipt_path = output / "pilot_review_receipts" / "readability.response.json"
    receipt = read_json(receipt_path)
    receipt[field] = receipt[field] + " " if field == "raw_response" else "0" * 64
    write_json(receipt_path, receipt)

    fresh_client, fresh_root = _pilot_client(output)
    with pytest.raises(Exception):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert fresh_root.provider_calls == []


def test_live_acceptance_malformed_response_records_code_and_is_not_recalled(tmp_path):
    output = tmp_path / "pilot-live"
    provider_root = _PilotProviderRoot()
    provider_root.malformed_once_at = "pilot_review:continuity"
    client, _ = _pilot_client(output, provider_root)

    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    receipt = read_json(output / "pilot_review_receipts" / "continuity.response.json")
    assert receipt["state"] == "REJECTED"
    assert receipt["contract_code"] == "PILOT_REVIEW_RESPONSE_NOT_OBJECT"
    failures = read_json(output / "pilot_live_calls.json")["contract_failures"]
    assert any(item["contract_code"] == "PILOT_REVIEW_RESPONSE_NOT_OBJECT" and item["role"] == "continuity" for item in failures)

    inspection = inspect_pilot_checkpoint(output)
    assert inspection["checkpoint_integrity"] != "CORRUPT", inspection["reason_codes"]
    if inspection["checkpoint_integrity"] == "RECONCILABLE":
        reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    fresh_client, fresh_root = _pilot_client(output)
    with pytest.raises(PilotError, match="PILOT_REVIEW_RESPONSE_ALREADY_CONSUMED"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert not [marker for _, marker, _ in fresh_root.provider_calls if marker == "pilot_review:continuity"]


def test_live_acceptance_mid_wave_manifest_stale_requires_reconcile_then_resumes(tmp_path):
    output = _make_acceptance_state(tmp_path, ["readability", "character_consistency", "continuity"], stale_manifest_checkpoint=True)

    fresh_client, fresh_root = _pilot_client(output)
    with pytest.raises(PilotError, match="pilot checkpoint reconciliation required"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert fresh_root.provider_calls == []

    result = reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    assert result["checkpoint_integrity"] == "VALID"

    resume_client, resume_root = _pilot_client(output)
    PilotPipeline(resume_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    markers = {marker for _, marker, _ in resume_root.provider_calls if marker.startswith("pilot_review:")}
    assert markers == {"pilot_review:rolling_plan_adaptation", "pilot_review:memory_correctness", "pilot_review:narrative_weight", "pilot_review:episode_to_episode_interest"}
    assert read_json(output / "pilot_manifest.json")["status"] == "COMPLETE"


def test_live_status_reports_grounded_acceptance(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    from arc.pilot import pilot_status

    current = pilot_status(output)
    assert current["acceptance_grounded"] is True
    assert current["acceptance_schema_version"] == 2
    assert current["acceptance_rubric_version"] == 1
    assert current["acceptance_dimension_count"] == 7
    assert current["acceptance_criterion_count"] == 21
    assert current["acceptance_hold_criterion_count"] == 0
    assert current["acceptance_strength_count"] == 7
    assert current["acceptance_grounding_reason"] is None
    assert current["acceptance_call_count"] == 7
    assert set(current["acceptance_prompt_character_counts"]) == set(PILOT_REVIEW_ROLES)
    assert all(count > 0 for count in current["acceptance_prompt_character_counts"].values())


def test_live_hold_dimension_produces_grounded_hold(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(hold_dimension="continuity"))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    from arc.pilot import pilot_status

    acceptance = read_json(output / "pilot_acceptance.json")
    assert acceptance["verdict"] == "HOLD"
    assert acceptance["dimension_results"]["continuity"] == "HOLD"
    assert acceptance["critical_findings"][0]["criterion_id"] == "continuity.required_obligations"
    current = pilot_status(output)
    assert current["status"] == "HOLD"
    assert current["acceptance_grounded"] is True
    assert current["acceptance_hold_criterion_count"] == 1


def test_acceptance_calls_exist_only_in_pilot_root_telemetry(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    root_calls = read_json(output / "pilot_live_calls.json")["calls"]
    assert any(call["scope_id"] == "pilot:acceptance" for call in root_calls)
    for episode_file in (output / "episodes").glob("*/live_calls.json"):
        assert all(call["scope_id"] != "pilot:acceptance" for call in read_json(episode_file)["calls"])


def _accepted_preflight(path: Path) -> Path:
    preflight = path / "preflight.json"
    write_json(preflight, {"status": "PASS", "live_run_allowed": True})
    return preflight


def _replace_root_telemetry(output: Path, telemetry: dict) -> None:
    digest = write_json(output / "pilot_live_calls.json", telemetry)
    manifest = read_json(output / "pilot_manifest.json")
    manifest["artifact_hashes"]["pilot_live_calls.json"] = digest
    write_json(output / "pilot_manifest.json", manifest)


def test_pilot_live_run_requires_accepted_preflight(tmp_path, monkeypatch):
    from arc.cli import _load_live_client

    monkeypatch.setenv("MODEL", MODEL_NAME)
    monkeypatch.setenv("ARC_LAUNCH_INTERVAL_SECONDS", "1")
    for index in range(1, 12):
        monkeypatch.setenv(f"GOOGLE_API_KEY_{index}", f"key-{index:02d}")
    preflight = tmp_path / "preflight.json"
    write_json(preflight, {"status": "FAIL", "live_run_allowed": False})

    with pytest.raises(ValueError, match="preflight does not allow"):
        _load_live_client(preflight, tmp_path / "run", tmp_path / "run" / "pilot_live_calls.json")


def test_pilot_live_run_builds_one_root_runtime(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert (output / "routing_state.json").exists()
    assert (output / "pilot_live_calls.json").exists()
    assert not list((output / "episodes").glob("*/routing_state.json"))


def test_pilot_live_status_validates_telemetry_projections(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    current = pilot_status(output)
    assert current["pilot_live_call_count"] == len(read_json(output / "pilot_live_calls.json")["calls"])
    assert current["acceptance_call_count"] == 7


def test_pilot_live_status_detects_duplicate_call_ids(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    telemetry = read_json(output / "pilot_live_calls.json")
    telemetry["calls"][1]["call_id"] = telemetry["calls"][0]["call_id"]
    _replace_root_telemetry(output, telemetry)

    with pytest.raises(StorageError, match="duplicate pilot live call id"):
        pilot_status(output)


def test_pilot_live_status_detects_duplicate_lease_sequences(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    telemetry = read_json(output / "pilot_live_calls.json")
    telemetry["calls"][1]["lease_sequence"] = telemetry["calls"][0]["lease_sequence"]
    _replace_root_telemetry(output, telemetry)

    with pytest.raises(StorageError, match="duplicate pilot live lease sequence"):
        pilot_status(output)


def test_pilot_live_status_rejects_unknown_operational_file(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    write_json(output / "unexpected.partial.json", {"bad": True})

    with pytest.raises(StorageError, match="unknown pilot artifact"):
        pilot_status(output)


def test_mock_and_live_outputs_cannot_be_reused(tmp_path):
    output = tmp_path / "pilot"
    PilotPipeline(__import__("arc.mock_model", fromlist=["MockModelClient"]).MockModelClient("pass"), "pass").run(PILOT_FIXTURE, output)
    client, _ = _pilot_client(output)

    with pytest.raises(Exception, match="pilot input changed"):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)


def _interrupting_pilot_client(run_dir: Path, provider_root: _PilotProviderRoot | None = None, *, stop_scope: str, stop_stage: str, stop_count: int) -> tuple[GemmaPoolClient, _PilotProviderRoot]:
    root = provider_root or _PilotProviderRoot()
    state_store = RoutingStateStore(run_dir / "routing_state.json", list(_config().keys))
    store = AtomicTelemetryStore(run_dir / "pilot_live_calls.json")

    def sink(telemetry: dict) -> None:
        store.save(telemetry)
        matched = [call for call in telemetry.get("calls", []) if call.get("scope_id") == stop_scope and call.get("stage") == stop_stage and call.get("status") == "PASS"]
        if len(matched) >= stop_count:
            raise KeyboardInterrupt

    return GemmaPoolClient(_config(), client_factory=root.factory, state_store=state_store, telemetry_sink=sink), root


def _checkpointed_desks(output: Path) -> set[tuple[str | None, str]]:
    desks: set[tuple[str | None, str]] = set()
    for partial in output.glob("episodes/*/*_workers.partial.json"):
        for desk in read_json(partial).get("completed_desks", {}):
            desks.add((partial.parent.name, desk))
    root_partial = output / "pilot_review_workers.partial.json"
    if root_partial.exists():
        for desk in read_json(root_partial).get("completed_desks", {}):
            desks.add((None, desk))
    return desks


INTERRUPTION_POINTS = [
    ("episode:episode_001", "planning", 1),
    ("episode:episode_001", "planning", 4),
    ("episode:episode_001", "planning", 6),
    ("episode:episode_001", "planning_merge", 1),
    ("episode:episode_002", "planning_merge", 1),
    ("episode:episode_002", "review", 3),
    ("episode:episode_002", "review", 7),
    ("episode:episode_003", "review_merge", 1),
    ("episode:episode_003", "memory", 2),
    ("episode:episode_004", "memory", 4),
    ("episode:episode_004", "memory_merge", 1),
    ("episode:episode_005", "memory_merge", 1),
]


@pytest.mark.parametrize("stop_scope,stop_stage,stop_count", INTERRUPTION_POINTS)
def test_arbitrary_stage_interruption_recovers_without_duplicate_calls(tmp_path, stop_scope, stop_stage, stop_count):
    output = tmp_path / "pilot-live"
    client, _ = _interrupting_pilot_client(output, stop_scope=stop_scope, stop_stage=stop_stage, stop_count=stop_count)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    before = read_json(output / "pilot_live_calls.json")
    completed_before = set(read_json(output / "pilot_manifest.json")["completed_episodes"])
    checkpointed = _checkpointed_desks(output)

    inspection = inspect_pilot_checkpoint(output)
    assert inspection["checkpoint_integrity"] != "CORRUPT", inspection["reason_codes"]
    if inspection["checkpoint_integrity"] == "RECONCILABLE":
        reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    resumed_client, resumed_root = _pilot_client(output)
    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    after = read_json(output / "pilot_live_calls.json")
    after_by_id = {call["call_id"]: call for call in after["calls"]}
    for call in before["calls"]:
        assert after_by_id[call["call_id"]] == call
    call_ids = [call["call_id"] for call in after["calls"]]
    lease_sequences = [call["lease_sequence"] for call in after["calls"]]
    assert len(call_ids) == len(set(call_ids))
    assert len(lease_sequences) == len(set(lease_sequences))
    resumed_markers = {(_episode_from_prompt(prompt), marker) for _, marker, prompt in resumed_root.provider_calls}
    for episode_id, desk in checkpointed:
        short_episode = episode_id.replace("episode:", "") if episode_id else None
        assert (short_episode, desk) not in resumed_markers
    assert not any(episode in {f"episode_{index:03d}" for index in range(1, 6)} and episode in completed_before for episode, _ in resumed_markers)
    for episode_id in result["manifest"]["episode_ids"]:
        projection = read_json(output / "episodes" / episode_id / "live_calls.json")
        scope = f"episode:{episode_id}"
        assert projection["calls"] == [call for call in after["calls"] if call["scope_id"] == scope]
        assert all(item["scope_id"] == scope for item in projection["contract_failures"])
        assert all(call["scope_id"] != "pilot:acceptance" for call in projection["calls"])


def test_interrupt_after_acceptance_content_response_fails_closed_without_second_call(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _interrupting_pilot_client(output, stop_scope="pilot:acceptance", stop_stage="pilot_review", stop_count=2)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    telemetry = read_json(output / "pilot_live_calls.json")
    passed_roles = [call["role"] for call in telemetry["calls"] if call["scope_id"] == "pilot:acceptance" and call["status"] == "PASS"]
    missing_receipt_roles = [role for role in passed_roles if not (output / "pilot_review_receipts" / f"{role}.response.json").exists()]
    assert missing_receipt_roles

    inspection = inspect_pilot_checkpoint(output)
    assert inspection["checkpoint_integrity"] != "CORRUPT", inspection["reason_codes"]
    if inspection["checkpoint_integrity"] == "RECONCILABLE":
        reconcile_pilot_checkpoint(PILOT_FIXTURE, output)

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises(PilotError, match="PILOT_REVIEW_RECONCILIATION_REQUIRED"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    resumed_markers = [marker for _, marker, _ in resumed_root.provider_calls if marker.startswith("pilot_review:")]
    assert all(f"pilot_review:{role}" not in resumed_markers for role in missing_receipt_roles)


def test_interrupt_after_writer_content_response_fails_closed_without_second_call(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _interrupting_pilot_client(output, stop_scope="episode:episode_001", stop_stage="writer", stop_count=1)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")
    assert episode["writer_attempt_state"] == "NOT_STARTED"

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises(Exception, match="WRITER_RECONCILIATION_REQUIRED"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []
    projection = read_json(output / "episodes" / "episode_001" / "live_calls.json")
    assert any(call["stage"] == "writer" and call["status"] == "PASS" for call in projection["calls"])
    assert read_json(output / "episodes" / "episode_001" / "manifest.json")["writer_attempt_state"] == "NOT_STARTED"


def test_interrupt_after_revision_content_response_fails_closed_without_second_call(tmp_path):
    output = tmp_path / "pilot-live"
    provider_root = _PilotProviderRoot(repairable_writer_once_episode="episode_004")
    client, _ = _interrupting_pilot_client(output, provider_root, stop_scope="episode:episode_004", stop_stage="revision", stop_count=1)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_004" / "manifest.json")
    assert episode["revision_attempt_state"] == "NOT_STARTED"

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises(Exception, match="REVISION_RECONCILIATION_REQUIRED"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []
    assert read_json(output / "episodes" / "episode_004" / "manifest.json")["revision_attempt_state"] == "NOT_STARTED"


def test_writer_receipt_with_stale_projection_recovers_and_holds_consumed(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    monkeypatch.setattr("arc.pipeline.validate_draft_prose", lambda value: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    monkeypatch.undo()
    episode_path = output / "episodes" / "episode_001" / "live_calls.json"
    stale = read_json(episode_path)
    stale["calls"] = stale["calls"][:2]
    write_json(episode_path, stale)

    resumed_client, resumed_root = _pilot_client(output)
    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode = read_json(output / "episodes" / "episode_001" / "manifest.json")

    assert result["manifest"]["status"] == "HOLD"
    assert episode["writer_attempt_state"] == "REJECTED"
    assert episode["writer_contract_code"] == "WRITER_RESPONSE_ALREADY_CONSUMED"
    assert resumed_root.provider_calls == []
    root_calls = read_json(output / "pilot_live_calls.json")["calls"]
    assert read_json(episode_path)["calls"] == [call for call in root_calls if call["scope_id"] == "episode:episode_001"]


def test_stale_projection_recovery_is_byte_identical_noop_on_second_run(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    stale_path = output / "episodes" / "episode_002" / "live_calls.json"
    stale = read_json(stale_path)
    stale["calls"] = stale["calls"][:3]
    write_json(stale_path, stale)
    (output / "episodes" / "episode_004" / "live_calls.json").unlink()
    before = _file_bytes(output)

    current = pilot_status(output)
    assert current["checkpoint_integrity"] == "RECONCILABLE"
    assert "EPISODE_PROJECTION_STALE" in current["reason_codes"]
    assert _file_bytes(output) == before

    first_client, first_root = _pilot_client(output)
    result = PilotPipeline(first_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after_first = _file_bytes(output)
    changed = {name for name in after_first if before.get(name) != after_first[name]}

    assert result["no_op"] is True
    assert first_root.provider_calls == []
    assert changed == {"episodes/episode_002/live_calls.json", "episodes/episode_004/live_calls.json"}

    second_client, second_root = _pilot_client(output)
    second = PilotPipeline(second_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert second["no_op"] is True
    assert second_root.provider_calls == []
    assert _file_bytes(output) == after_first
    assert pilot_status(output)["checkpoint_integrity"] == "VALID"


def test_unknown_scope_in_root_telemetry_is_corrupt(tmp_path):
    from arc.pilot import pilot_status

    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    telemetry = read_json(output / "pilot_live_calls.json")
    extra = dict(telemetry["calls"][-1])
    extra.update({"call_id": "L999-A999", "lease_sequence": max(call["lease_sequence"] for call in telemetry["calls"]) + 1, "scope_id": "episode:unknown_episode", "desk_id": "episode:unknown_episode:planning:event"})
    telemetry["calls"].append(extra)
    write_json(output / "pilot_live_calls.json", telemetry)
    routing = read_json(output / "routing_state.json")
    routing["next_lease_sequence"] = extra["lease_sequence"] + 1
    write_json(output / "routing_state.json", routing)

    with pytest.raises(StorageError, match="unknown pilot live telemetry scope"):
        pilot_status(output)


def test_episode_projection_contains_only_episode_scope_contract_failures(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(wrong_plan_once=True, wrong_plan_episode="episode_002"))
    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    resumed_client, _ = _pilot_client(output)
    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert result["manifest"]["status"] == "COMPLETE"

    root = read_json(output / "pilot_live_calls.json")
    assert any(item["scope_id"] == "episode:episode_002" for item in root["contract_failures"])
    projection = read_json(output / "episodes" / "episode_002" / "live_calls.json")
    assert [item["scope_id"] for item in projection["contract_failures"]] == ["episode:episode_002"]
    for episode_id in ["episode_001", "episode_003", "episode_004", "episode_005"]:
        assert read_json(output / "episodes" / episode_id / "live_calls.json")["contract_failures"] == []


def test_live_transition_adapter_calls_once_per_boundary(tmp_path):
    output = tmp_path / "pilot-live"
    client, root = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    telemetry = read_json(output / "pilot_live_calls.json")
    transition_calls = [call for call in telemetry["calls"] if call["stage"] == "transition"]
    assert len(transition_calls) == 4
    assert all(call["role"] == "adapter" and call["status"] == "PASS" for call in transition_calls)
    assert sorted(call["scope_id"] for call in transition_calls) == [f"episode:episode_00{index}" for index in range(1, 5)]
    assert not any(call["stage"] == "transition" for call in telemetry["calls"] if call["scope_id"] == "pilot:acceptance")
    call_ids = [call["call_id"] for call in telemetry["calls"]]
    lease_sequences = [call["lease_sequence"] for call in telemetry["calls"]]
    assert len(call_ids) == len(set(call_ids)) and len(lease_sequences) == len(set(lease_sequences))
    for index in range(1, 5):
        projection = read_json(output / "episodes" / f"episode_00{index}" / "live_calls.json")
        assert sum(call["stage"] == "transition" for call in projection["calls"]) == 1
    assert not any(call["stage"] == "transition" for call in read_json(output / "episodes" / "episode_005" / "live_calls.json")["calls"])
    episode2_planning = [prompt for _, marker, prompt in root.provider_calls if marker.startswith("planning:") and _episode_from_prompt(prompt) == "episode_002"]
    assert episode2_planning and all("adapted direction after episode_001" in prompt for prompt in episode2_planning)


TRANSITION_RECEIPT_PATH = "transitions/episode_001_to_episode_002.response.json"
TRANSITION_ARTIFACT_PATH = "transitions/episode_001_to_episode_002.json"


def _interrupt_pilot_on_write(monkeypatch, fragment: str, skip: int = 0):
    import arc.pilot as pilot_module

    original = pilot_module.write_json
    state = {"remaining": skip}

    def wrapper(path, value):
        if fragment in Path(path).as_posix():
            if state["remaining"] == 0:
                monkeypatch.setattr(pilot_module, "write_json", original)
                raise KeyboardInterrupt()
            state["remaining"] -= 1
        return original(path, value)

    monkeypatch.setattr(pilot_module, "write_json", wrapper)


def _resume_pilot(output: Path):
    inspection = inspect_pilot_checkpoint(output)
    assert inspection["checkpoint_integrity"] != "CORRUPT", inspection["reason_codes"]
    if inspection["checkpoint_integrity"] == "RECONCILABLE":
        reconcile_pilot_checkpoint(PILOT_FIXTURE, output)
    client, root = _pilot_client(output)
    return PilotPipeline(client, scenario=None, mode="live"), root


def _transition_call_episodes(provider_root: _PilotProviderRoot) -> list[str]:
    return [json.loads(prompt)["completed_episode_id"] for _, marker, prompt in provider_root.provider_calls if marker == "transition:adapter"]


def test_transition_pass_without_receipt_fails_closed_without_recall(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    _interrupt_pilot_on_write(monkeypatch, TRANSITION_RECEIPT_PATH)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert not (output / TRANSITION_RECEIPT_PATH).exists()
    telemetry_before = read_json(output / "pilot_live_calls.json")
    pipeline, resumed_root = _resume_pilot(output)
    with pytest.raises(PilotError, match="TRANSITION_RECONCILIATION_REQUIRED"):
        pipeline.run(PILOT_FIXTURE, output)

    assert resumed_root.provider_calls == []
    assert read_json(output / "pilot_live_calls.json") == telemetry_before
    assert not (output / TRANSITION_RECEIPT_PATH).exists()
    assert not (output / TRANSITION_ARTIFACT_PATH).exists()


def test_transition_receipt_resume_completes_without_recall(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    _interrupt_pilot_on_write(monkeypatch, TRANSITION_ARTIFACT_PATH)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    receipt_before = read_json(output / TRANSITION_RECEIPT_PATH)
    assert receipt_before["state"] == "RESPONSE_RECEIVED"
    pipeline, resumed_root = _resume_pilot(output)
    result = pipeline.run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    assert _transition_call_episodes(resumed_root) == ["episode_002", "episode_003", "episode_004"]
    telemetry = read_json(output / "pilot_live_calls.json")
    episode1_calls = [call for call in telemetry["calls"] if call["stage"] == "transition" and call["scope_id"] == "episode:episode_001"]
    assert len(episode1_calls) == 1
    receipt_after = read_json(output / TRANSITION_RECEIPT_PATH)
    assert receipt_after["state"] == "COMPLETED"
    assert receipt_after["response_sha256"] == receipt_before["response_sha256"] == episode1_calls[0]["response_sha256"]


def test_transition_artifact_without_source_regenerates_deterministically(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    _interrupt_pilot_on_write(monkeypatch, "episode_sources/episode_002.json")
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert (output / TRANSITION_ARTIFACT_PATH).exists()
    assert not (output / "episode_sources" / "episode_002.json").exists()
    resumed_client, resumed_root = _pilot_client(output)
    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    assert "episode_001" not in _transition_call_episodes(resumed_root)
    transition = read_json(output / TRANSITION_ARTIFACT_PATH)
    from arc.storage import sha256_file
    assert sha256_file(output / "episode_sources" / "episode_002.json") == transition["next_source_hash"]
    assert read_json(output / TRANSITION_RECEIPT_PATH)["state"] == "COMPLETED"


def test_transition_receipt_completion_interruption_resumes_to_noop(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    _interrupt_pilot_on_write(monkeypatch, TRANSITION_RECEIPT_PATH, skip=1)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert read_json(output / TRANSITION_RECEIPT_PATH)["state"] == "RESPONSE_RECEIVED"
    assert (output / "episode_sources" / "episode_002.json").exists()
    pipeline, resumed_root = _resume_pilot(output)
    result = pipeline.run(PILOT_FIXTURE, output)

    assert result["manifest"]["status"] == "COMPLETE"
    assert "episode_001" not in _transition_call_episodes(resumed_root)
    assert read_json(output / TRANSITION_RECEIPT_PATH)["state"] == "COMPLETED"
    after_first = _file_bytes(output)
    noop_client, noop_root = _pilot_client(output)
    assert PilotPipeline(noop_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)["no_op"] is True
    assert noop_root.provider_calls == []
    assert _file_bytes(output) == after_first


def test_rejected_transition_receipt_fails_closed_without_recall(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(transition_malformed_episode="episode_001"))
    with pytest.raises(ContractError):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    receipt = read_json(output / TRANSITION_RECEIPT_PATH)
    assert receipt["state"] == "REJECTED"
    assert receipt["contract_code"] == "TRANSITION_RESPONSE_NOT_OBJECT"
    telemetry = read_json(output / "pilot_live_calls.json")
    failures = [item for item in telemetry["contract_failures"] if item["stage"] == "transition"]
    assert len(failures) == 1 and failures[0]["scope_id"] == "episode:episode_001" and failures[0]["role"] == "adapter"
    assert not (output / TRANSITION_ARTIFACT_PATH).exists()
    assert not (output / "episode_sources" / "episode_002.json").exists()

    receipt_bytes = (output / TRANSITION_RECEIPT_PATH).read_bytes()
    pipeline, resumed_root = _resume_pilot(output)
    with pytest.raises(PilotError, match="TRANSITION_RESPONSE_ALREADY_CONSUMED"):
        pipeline.run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []
    assert (output / TRANSITION_RECEIPT_PATH).read_bytes() == receipt_bytes
    assert not (output / TRANSITION_ARTIFACT_PATH).exists()


def _pending_receipt_state(tmp_path, monkeypatch) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    _interrupt_pilot_on_write(monkeypatch, TRANSITION_ARTIFACT_PATH)
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    return output


def test_tampered_receipt_input_hash_fails_closed_before_provider_call(tmp_path, monkeypatch):
    output = _pending_receipt_state(tmp_path, monkeypatch)
    receipt = read_json(output / TRANSITION_RECEIPT_PATH)
    receipt["transition_input_hash"] = "0" * 64
    write_json(output / TRANSITION_RECEIPT_PATH, receipt)

    pipeline, resumed_root = _resume_pilot(output)
    with pytest.raises(PilotError, match="transition receipt input hash mismatch"):
        pipeline.run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []
    assert not (output / TRANSITION_ARTIFACT_PATH).exists()


@pytest.mark.parametrize("tamper", [
    lambda receipt: receipt.update(raw_response=receipt["raw_response"].replace("adapted direction", "tampered direction")),
    lambda receipt: receipt.update(response_sha256="0" * 64),
])
def test_tampered_receipt_response_fails_closed_before_provider_call(tmp_path, monkeypatch, tamper):
    output = _pending_receipt_state(tmp_path, monkeypatch)
    receipt = read_json(output / TRANSITION_RECEIPT_PATH)
    tamper(receipt)
    write_json(output / TRANSITION_RECEIPT_PATH, receipt)

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises(PilotError, match="transition receipt response hash mismatch"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []


def test_completed_receipt_without_artifact_fails_closed(tmp_path, monkeypatch):
    output = _pending_receipt_state(tmp_path, monkeypatch)
    receipt = read_json(output / TRANSITION_RECEIPT_PATH)
    receipt["state"] = "COMPLETED"
    write_json(output / TRANSITION_RECEIPT_PATH, receipt)

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises(PilotError, match="completed transition receipt without canonical transition"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []


@pytest.mark.parametrize("tamper_transition", [
    lambda transition: transition["adaptation_decisions"][0].update(reason="tampered reason variant"),
    lambda transition: transition["adaptation_decisions"][0]["evidence"][0].update(excerpt="tampered excerpt that is long enough"),
    lambda transition: transition["rolling_plan_after"]["near_horizon"].append("tampered unexplained item"),
])
def test_tampered_pending_transition_artifact_fails_closed(tmp_path, monkeypatch, tamper_transition):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    _interrupt_pilot_on_write(monkeypatch, "episode_sources/episode_002.json")
    with pytest.raises(KeyboardInterrupt):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    transition = read_json(output / TRANSITION_ARTIFACT_PATH)
    tamper_transition(transition)
    write_json(output / TRANSITION_ARTIFACT_PATH, transition)

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises((ContractError, PilotError, StorageError)):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert "episode_001" not in _transition_call_episodes(resumed_root)
    assert not (output / "episode_sources" / "episode_002.json").exists()


def test_completed_pilot_receipt_transition_mismatch_fails_closed(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    receipt = read_json(output / TRANSITION_RECEIPT_PATH)
    response = json.loads(receipt["raw_response"])
    response["adaptation_summary"] = "Tampered summary that no longer matches the canonical transition."
    receipt["raw_response"] = json.dumps(response, ensure_ascii=False)
    receipt["response_sha256"] = hashlib.sha256(receipt["raw_response"].encode("utf-8")).hexdigest()
    write_json(output / TRANSITION_RECEIPT_PATH, receipt)

    resumed_client, resumed_root = _pilot_client(output)
    with pytest.raises(PilotError, match="transition receipt response does not match canonical transition"):
        PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resumed_root.provider_calls == []


def test_transition_receipts_match_provider_output(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    telemetry = read_json(output / "pilot_live_calls.json")
    ids = read_json(output / "pilot_manifest.json")["episode_ids"]
    for episode_id, next_id in zip(ids, ids[1:]):
        receipt = read_json(output / "transitions" / f"{episode_id}_to_{next_id}.response.json")
        assert receipt["state"] == "COMPLETED" and receipt["contract_code"] is None
        calls = [call for call in telemetry["calls"] if call["stage"] == "transition" and call["scope_id"] == f"episode:{episode_id}"]
        assert len(calls) == 1
        assert receipt["response_sha256"] == calls[0]["response_sha256"]
        transition = read_json(output / "transitions" / f"{episode_id}_to_{next_id}.json")
        response = json.loads(receipt["raw_response"])
        assert "evidence_refs" not in response
        assert all("evidence" not in decision and "evidence_candidate_ids" in decision for decision in response["adaptation_decisions"])
        assert all(response[field] == transition[field] for field in ("next_episode", "rolling_plan_after", "continuity_satisfied", "continuity_deferred", "adaptation_summary"))


def _projection_root_doc() -> dict:
    def call(order: int, scope: str) -> dict:
        return {"call_id": f"L{order:03d}-A001", "scope_id": scope, "desk_id": f"{scope}:planning:event", "logical_order": order, "attempt": 1, "lease_sequence": order, "stage": "planning", "role": "event", "key_slot": f"K{order:02d}", "status": "PASS"}

    return {
        "schema_version": 2,
        "provider": "gemini_developer_api",
        "model": MODEL_NAME,
        "calls": [call(1, "episode:episode_001"), call(2, "episode:episode_001"), call(3, "episode:episode_002"), call(4, "pilot:acceptance")],
        "contract_failures": [
            {"event_id": "CF001", "scope_id": "episode:episode_001", "stage": "planning_merge", "role": "merge", "contract_code": "PLAN_FIELDS_MISMATCH"},
            {"event_id": "CF002", "scope_id": "pilot:acceptance", "stage": "pilot_review", "role": "readability", "contract_code": "UNKNOWN"},
            {"event_id": "CF003", "scope_id": None, "stage": "planning_merge", "role": "merge", "contract_code": "UNKNOWN"},
        ],
        "max_active_by_stage": {"planning": 2},
    }


def test_projection_document_is_deterministic_scope_filter():
    root = _projection_root_doc()

    projection = episode_projection_document(root, "episode_001")

    assert [call["call_id"] for call in projection["calls"]] == ["L001-A001", "L002-A001"]
    assert [item["event_id"] for item in projection["contract_failures"]] == ["CF001"]
    assert "max_active_by_stage" not in projection
    assert projection == episode_projection_document(root, "episode_001")


def test_projection_document_excludes_acceptance_and_unscoped_failures():
    root = _projection_root_doc()

    projection = episode_projection_document(root, "episode_002")

    assert [call["scope_id"] for call in projection["calls"]] == ["episode:episode_002"]
    assert projection["contract_failures"] == []


def test_classify_projection_missing_and_current():
    root = _projection_root_doc()
    canonical = episode_projection_document(root, "episode_001")

    assert classify_episode_projection(root, "episode_001", None) == "MISSING"
    assert classify_episode_projection(root, "episode_001", canonical) == "CURRENT"


def _projection_failure_root_doc() -> dict:
    root = _projection_root_doc()
    root["contract_failures"] = [
        {"event_id": "CF001", "scope_id": "episode:episode_001", "stage": "planning_merge", "role": "merge", "contract_code": "PLAN_FIELDS_MISMATCH"},
        {"event_id": "CF004", "scope_id": "episode:episode_001", "stage": "writer", "role": "canonical", "contract_code": "PROSE_LENGTH"},
        {"event_id": "CF005", "scope_id": "episode:episode_002", "stage": "planning_merge", "role": "merge", "contract_code": "PLAN_FIELDS_MISMATCH"},
        {"event_id": "CF002", "scope_id": "pilot:acceptance", "stage": "pilot_review", "role": "readability", "contract_code": "UNKNOWN"},
        {"event_id": "CF003", "scope_id": None, "stage": "planning_merge", "role": "merge", "contract_code": "UNKNOWN"},
    ]
    return root


def test_classify_projection_stale_prefix():
    root = _projection_failure_root_doc()
    canonical = episode_projection_document(root, "episode_001")

    calls_prefix = {**canonical, "calls": canonical["calls"][:1]}
    assert classify_episode_projection(root, "episode_001", calls_prefix) == "STALE_PREFIX"
    failures_prefix = {**canonical, "contract_failures": canonical["contract_failures"][:1]}
    assert classify_episode_projection(root, "episode_001", failures_prefix) == "STALE_PREFIX"
    both_prefix = {**canonical, "calls": canonical["calls"][:1], "contract_failures": canonical["contract_failures"][:1]}
    assert classify_episode_projection(root, "episode_001", both_prefix) == "STALE_PREFIX"
    empty = {**canonical, "calls": [], "contract_failures": []}
    assert classify_episode_projection(root, "episode_001", empty) == "STALE_PREFIX"


def test_classify_projection_legacy_unfiltered_failures_is_conflict():
    root = _projection_root_doc()
    canonical = episode_projection_document(root, "episode_001")

    legacy_unfiltered = {**canonical, "contract_failures": list(root["contract_failures"]), "max_active_by_stage": {"planning": 2}}
    assert classify_episode_projection(root, "episode_001", legacy_unfiltered) == "CONFLICT"


@pytest.mark.parametrize("mutate", [
    lambda canonical, root: {**canonical, "calls": [dict(canonical["calls"][0], key_slot="tampered"), canonical["calls"][1]]},
    lambda canonical, root: {**canonical, "calls": [canonical["calls"][1]]},
    lambda canonical, root: {**canonical, "calls": list(reversed(canonical["calls"]))},
    lambda canonical, root: {**canonical, "calls": canonical["calls"] + [root["calls"][2]]},
    lambda canonical, root: {**canonical, "calls": canonical["calls"] + [root["calls"][3]]},
    lambda canonical, root: {**canonical, "calls": canonical["calls"] + [dict(canonical["calls"][0], call_id="L999-A999", lease_sequence=99)]},
    lambda canonical, root: {**canonical, "calls": canonical["calls"] + [canonical["calls"][0]]},
    lambda canonical, root: {**canonical, "contract_failures": canonical["contract_failures"] + [{"event_id": "CF999", "scope_id": "episode:episode_001", "contract_code": "FABRICATED"}]},
    lambda canonical, root: {**canonical, "calls": "tampered"},
    lambda canonical, root: [],
])
def test_classify_projection_conflicts(mutate):
    root = _projection_root_doc()
    canonical = episode_projection_document(root, "episode_001")

    assert classify_episode_projection(root, "episode_001", mutate(canonical, root)) == "CONFLICT"


@pytest.mark.parametrize("mutate", [
    lambda canonical, root: {**canonical, "contract_failures": canonical["contract_failures"] + [root["contract_failures"][2]]},
    lambda canonical, root: {**canonical, "contract_failures": canonical["contract_failures"] + [root["contract_failures"][3]]},
    lambda canonical, root: {**canonical, "contract_failures": canonical["contract_failures"] + [root["contract_failures"][4]]},
    lambda canonical, root: {**canonical, "contract_failures": list(reversed(canonical["contract_failures"]))},
    lambda canonical, root: {**canonical, "contract_failures": canonical["contract_failures"][1:]},
    lambda canonical, root: {**canonical, "contract_failures": [canonical["contract_failures"][0], canonical["contract_failures"][0]]},
    lambda canonical, root: {**canonical, "contract_failures": [dict(canonical["contract_failures"][0], contract_code="tampered"), canonical["contract_failures"][1]]},
    lambda canonical, root: {**canonical, "contract_failures": canonical["contract_failures"] + [{"event_id": "CF999", "scope_id": "episode:episode_001", "contract_code": "FABRICATED"}]},
    lambda canonical, root: {**canonical, "contract_failures": "tampered"},
    lambda canonical, root: {key: value for key, value in canonical.items() if key != "contract_failures"},
])
def test_classify_projection_failure_conflicts(mutate):
    root = _projection_failure_root_doc()
    canonical = episode_projection_document(root, "episode_001")

    assert classify_episode_projection(root, "episode_001", mutate(canonical, root)) == "CONFLICT"


def test_classify_projection_ahead_of_shortened_root_is_conflict():
    root = _projection_root_doc()
    canonical = episode_projection_document(root, "episode_001")
    shortened_root = {**root, "calls": root["calls"][:1]}

    assert classify_episode_projection(shortened_root, "episode_001", canonical) == "CONFLICT"


def test_classify_projection_ahead_of_shortened_root_failures_is_conflict():
    root = _projection_failure_root_doc()
    canonical = episode_projection_document(root, "episode_001")
    shortened_root = {**root, "contract_failures": root["contract_failures"][:1]}

    assert classify_episode_projection(shortened_root, "episode_001", canonical) == "CONFLICT"


def test_reconcile_projections_regenerates_then_noops(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")
    stale_path = output / "episodes" / "episode_002" / "live_calls.json"
    stale = read_json(stale_path)
    stale["calls"] = stale["calls"][:3]
    write_json(stale_path, stale)
    (output / "episodes" / "episode_004" / "live_calls.json").unlink()

    states = reconcile_live_telemetry_projections(output, manifest)

    assert states["episode_002"] == "STALE_PREFIX"
    assert states["episode_004"] == "MISSING"
    assert states["episode_001"] == "CURRENT"
    after_first = _file_bytes(output)
    assert reconcile_live_telemetry_projections(output, manifest) == {episode_id: "CURRENT" for episode_id in manifest["episode_ids"]}
    assert _file_bytes(output) == after_first


def test_reconcile_projections_fails_closed_on_conflict_without_overwrite(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")
    conflict_path = output / "episodes" / "episode_003" / "live_calls.json"
    conflict = read_json(conflict_path)
    conflict["calls"][0]["status"] = "tampered"
    write_json(conflict_path, conflict)
    before = conflict_path.read_bytes()

    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        reconcile_live_telemetry_projections(output, manifest)
    assert conflict_path.read_bytes() == before


def _completed_pilot_with_episode_002_failures(output):
    client, _ = _pilot_client(output, _PilotProviderRoot(wrong_plan_once=True, wrong_plan_episode="episode_002"))
    with pytest.raises(Exception):
        PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    resumed_client, _ = _pilot_client(output)
    result = PilotPipeline(resumed_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert result["manifest"]["status"] == "COMPLETE"
    return read_json(output / "pilot_manifest.json")


def test_reconcile_projections_regenerates_truncated_failure_prefix(tmp_path):
    output = tmp_path / "pilot-live"
    manifest = _completed_pilot_with_episode_002_failures(output)
    stale_path = output / "episodes" / "episode_002" / "live_calls.json"
    canonical_bytes_before = stale_path.read_bytes()
    stale = read_json(stale_path)
    assert stale["contract_failures"]
    stale["contract_failures"] = []
    write_json(stale_path, stale)

    states = reconcile_live_telemetry_projections(output, manifest)

    assert states["episode_002"] == "STALE_PREFIX"
    assert stale_path.read_bytes() == canonical_bytes_before
    after_first = _file_bytes(output)
    assert reconcile_live_telemetry_projections(output, manifest) == {episode_id: "CURRENT" for episode_id in manifest["episode_ids"]}
    assert _file_bytes(output) == after_first


def test_reconcile_projections_fails_closed_on_foreign_failure_without_overwrite(tmp_path):
    output = tmp_path / "pilot-live"
    manifest = _completed_pilot_with_episode_002_failures(output)
    root = read_json(output / "pilot_live_calls.json")
    foreign = next(item for item in root["contract_failures"] if item["scope_id"] == "episode:episode_002")
    conflict_path = output / "episodes" / "episode_003" / "live_calls.json"
    conflict = read_json(conflict_path)
    conflict["contract_failures"] = [foreign]
    write_json(conflict_path, conflict)
    before = _file_bytes(output)

    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        reconcile_live_telemetry_projections(output, manifest)
    assert _file_bytes(output) == before

    resume_client, resume_root = _pilot_client(output)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        PilotPipeline(resume_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    assert resume_root.provider_calls == []


def test_projection_regeneration_replace_failure_preserves_original(tmp_path, monkeypatch):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    manifest = read_json(output / "pilot_manifest.json")
    stale_path = output / "episodes" / "episode_002" / "live_calls.json"
    stale = read_json(stale_path)
    stale["calls"] = stale["calls"][:2]
    write_json(stale_path, stale)
    before = stale_path.read_bytes()

    def broken_replace(source, target):
        raise OSError("injected replace failure")

    monkeypatch.setattr("arc.storage.os.replace", broken_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        reconcile_live_telemetry_projections(output, manifest)
    assert stale_path.read_bytes() == before
    monkeypatch.undo()

    reconcile_live_telemetry_projections(output, manifest)
    root_calls = read_json(output / "pilot_live_calls.json")["calls"]
    assert read_json(stale_path)["calls"] == [call for call in root_calls if call["scope_id"] == "episode:episode_002"]


def test_pilot_live_complete_noop_preserves_all_hashes(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    fresh_client, _ = _pilot_client(output)
    PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    assert after == before
