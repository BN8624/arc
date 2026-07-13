# 파일럿 live runtime scope와 telemetry 계약을 검증한다.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shutil
from pathlib import Path

import pytest

from arc.contracts import ContractError, validate_prose
from arc.live_model import AtomicTelemetryStore, GemmaPoolClient, LiveConfig, MODEL_NAME, RoutingStateStore
from arc.pipeline import PLANNING_ROLES, MockPipeline, WaveCheckpoint, status
from arc.pilot_contracts import PILOT_REVIEW_ROLES
from arc.pilot import PilotPipeline, inspect_pilot_checkpoint, live_telemetry_checkpoint, reconcile_pilot_checkpoint
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
            stage = "pilot_review"
            role = payload["dimension"]
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
    def __init__(self, fail_once_at: str | None = None, hold_episode: str | None = None, fail_status_code: int = 500, hold_dimension: str | None = None, wrong_plan_once: bool = False, wrong_plan_episode: str | None = None, short_writer_once_episode: str | None = None):
        self.fail_once_at = fail_once_at
        self.hold_episode = hold_episode
        self.fail_status_code = fail_status_code
        self.hold_dimension = hold_dimension
        self.wrong_plan_once = wrong_plan_once
        self.wrong_plan_episode = wrong_plan_episode
        self.short_writer_once_episode = short_writer_once_episode
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
            hold = role == self.hold_dimension
            return json.dumps({"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"dimension_result": "HOLD" if hold else "PASS", "critical_finding": f"synthetic {role} hold" if hold else None}})
        if stage == "planning_merge":
            if self.wrong_plan_once and "planning_merge:merge" not in self.malformed and (self.wrong_plan_episode is None or episode_id == self.wrong_plan_episode):
                self.malformed.add("planning_merge:merge")
                return json.dumps({"episode_id": "wrong_episode", "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]})
            return json.dumps({"episode_id": episode_id, "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]})
        if stage == "writer":
            marker = f"writer:{episode_id}"
            if self.short_writer_once_episode == episode_id and marker not in self.malformed:
                self.malformed.add(marker)
                return "short prose"
            return ("A synthetic live episode sentence. " * 160)[:4800]
        if stage == "review_merge":
            verdict = "HOLD" if episode_id == self.hold_episode else "PASS"
            return json.dumps({"verdict": verdict, "strengths_to_preserve": ["synthetic agency"], "required_changes": [], "evidence_refs": ["draft.md"]})
        if stage == "revision":
            return ("A revised synthetic live episode sentence. " * 150)[:4800]
        if stage == "memory_merge":
            return json.dumps({"episode_id": episode_id, "confirmed_facts_added": [f"synthetic fact {episode_id}"], "relationship_changes": [f"synthetic relationship {episode_id}"], "conflict_ids_resolved": [], "conflicts_opened": [f"synthetic opened conflict {episode_id}"], "promises_added": [f"synthetic promise {episode_id}"], "important_excerpts_added": [f"synthetic excerpt {episode_id}"], "episode_summary": f"synthetic episode summary {episode_id}", "required_next_episode_continuity": [f"synthetic continuity {episode_id}"], "evidence_refs": ["final.md"]})
        raise RuntimeError(f"unknown live stage: {stage}:{role}")


def _config(key_count: int = 11) -> LiveConfig:
    return LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i:02d}" for i in range(1, key_count + 1)}, launch_interval=0.0)


def _client(tmp_path, key_count: int = 11) -> tuple[GemmaPoolClient, dict[str, _Provider]]:
    providers: dict[str, _Provider] = {}

    def factory(key: str) -> _Provider:
        slot = key.replace("key-", "K")
        providers[slot] = _Provider(slot)
        return providers[slot]

    store = AtomicTelemetryStore(tmp_path / "pilot_live_calls.json")
    return GemmaPoolClient(_config(key_count), client_factory=factory, telemetry_sink=store.save), providers


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

    episode_manifest = read_json(episode2 / "manifest.json")
    keep = {"manifest.json", "context_packet.json", "planning_workers.partial.json", "live_calls.json"}
    for path in list(episode2.iterdir()):
        if path.is_file() and path.name not in keep:
            path.unlink()
    episode_manifest.update({"status": "RUNNING", "completed_stages": ["CONTEXT_ASSEMBLED"], "artifact_hashes": {"context_packet.json": episode_manifest["artifact_hashes"]["context_packet.json"]}, "writer_call_count": 0, "revision_count": 0, "review_verdict": None, "last_error": None, "live_call_count": 2})

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


def _acceptance_worker(role: str) -> dict:
    return {"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"dimension_result": "PASS", "critical_finding": None}}


def _make_acceptance_partial_output(tmp_path: Path) -> Path:
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    manifest = read_json(output / "pilot_manifest.json")
    completed_roles = ["readability", "character_consistency", "continuity"]
    (output / "pilot_review_workers.json").unlink(missing_ok=True)
    (output / "pilot_acceptance.json").unlink(missing_ok=True)

    checkpoint = WaveCheckpoint(
        output / "pilot_review_workers.partial.json",
        "pilot_review",
        {"pilot_id": manifest["pilot_id"], "mode": manifest["mode"], "scenario": manifest["scenario"], "episode_ids": manifest["episode_ids"], "evidence_packet_hash": manifest["artifact_hashes"]["pilot_evidence_packet.json"]},
        PILOT_REVIEW_ROLES,
    )
    for role in completed_roles:
        checkpoint.save(role, _acceptance_worker(role))

    telemetry = read_json(output / "pilot_live_calls.json")
    kept_calls = [call for call in telemetry["calls"] if call["scope_id"] != "pilot:acceptance" or call["role"] in completed_roles]
    telemetry["calls"] = kept_calls
    root_hash = write_json(output / "pilot_live_calls.json", telemetry)
    routing_state = read_json(output / "routing_state.json")
    routing_state["next_lease_sequence"] = max(call["lease_sequence"] for call in kept_calls) + 1
    write_json(output / "routing_state.json", routing_state)

    manifest.update({"status": "RUNNING", "active_episode_id": None, "acceptance_verdict": None, "last_error": None, "pilot_live_call_count": len(kept_calls)})
    manifest["artifact_hashes"].pop("pilot_review_workers.json", None)
    manifest["artifact_hashes"].pop("pilot_acceptance.json", None)
    manifest["live_telemetry_checkpoint"] = live_telemetry_checkpoint(telemetry)
    write_json(output / "pilot_manifest.json", manifest)
    return output


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
    with pytest.raises(Exception):
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


def test_validate_prose_rejects_short_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose("A" * 3999)
    assert error.value.contract_code == "PROSE_TOO_SHORT"
    assert error.value.character_count == 3999


def test_validate_prose_rejects_long_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose("A" * 8001)
    assert error.value.contract_code == "PROSE_TOO_LONG"
    assert error.value.character_count == 8001


def test_validate_prose_rejects_forbidden_marker_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose(("A" * 4100) + "SCENE 1")
    assert error.value.contract_code == "PROSE_FORBIDDEN_MARKER"


def test_validate_prose_rejects_json_shape_with_code():
    with pytest.raises(ContractError) as error:
        validate_prose('{"text":"bad"}')
    assert error.value.contract_code == "PROSE_INVALID_SHAPE"


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
    assert manifest["last_error"]["contract_code"] == "PROSE_TOO_SHORT"
    assert manifest["last_error"]["character_count"] == len("short prose")
    assert "short prose" not in json.dumps(telemetry, ensure_ascii=False)
    assert "short prose" not in json.dumps(manifest, ensure_ascii=False)


def test_writer_failure_checkpoint_resumes_from_writer_without_recalling_prefix(tmp_path):
    output = _make_episode_four_writer_error_output(tmp_path)
    before = read_json(output / "pilot_live_calls.json")
    before_episode_counts = {scope: sum(call["scope_id"] == scope for call in before["calls"]) for scope in ["episode:episode_001", "episode:episode_002", "episode:episode_003"]}
    before_planning = sum(call["desk_id"].startswith("episode:episode_004:planning:") for call in before["calls"])
    before_merge = [call["attempt"] for call in before["calls"] if call["desk_id"] == "episode:episode_004:planning_merge:merge"]

    result, provider_root = _resume_episode_four_output(output)
    after = read_json(output / "pilot_live_calls.json")
    resumed = [(_episode_from_prompt(prompt), marker) for _, marker, prompt in provider_root.provider_calls]
    writer_attempts = [call["attempt"] for call in after["calls"] if call["desk_id"] == "episode:episode_004:writer:canonical"]

    assert result["manifest"]["status"] == "COMPLETE"
    assert resumed[0] == ("episode_004", "writer:canonical")
    assert {scope: sum(call["scope_id"] == scope for call in after["calls"]) for scope in before_episode_counts} == before_episode_counts
    assert sum(call["desk_id"].startswith("episode:episode_004:planning:") for call in after["calls"]) == before_planning
    assert [call["attempt"] for call in after["calls"] if call["desk_id"] == "episode:episode_004:planning_merge:merge"] == before_merge
    assert writer_attempts == [1, 2]
    assert read_json(output / "episodes" / "episode_004" / "manifest.json")["writer_call_count"] == 1


def test_writer_prompt_reinforces_safe_character_band():
    from arc.prompts import build_prompt

    prompt = build_prompt("writer", "canonical", {"context": {}, "plan": {}})

    assert "5000 and 7000 characters" in prompt
    assert "never mention the character count" in prompt


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


def test_reconcile_rejects_projection_mismatch(tmp_path):
    output = _make_reconcilable_pilot_output(tmp_path)
    episode_live = output / "episodes" / "episode_003" / "live_calls.json"
    value = read_json(episode_live)
    value["calls"] = []
    digest = write_json(episode_live, value)
    manifest = read_json(output / "episodes" / "episode_003" / "manifest.json")
    manifest["artifact_hashes"]["live_calls.json"] = digest
    write_json(output / "episodes" / "episode_003" / "manifest.json", manifest)

    assert inspect_pilot_checkpoint(output)["checkpoint_integrity"] == "CORRUPT"


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


def test_live_pilot_interrupted_episode_rejects_root_projection_mismatch(tmp_path):
    output = _make_interrupted_episode_output(tmp_path)
    episode_path = output / "episodes" / "episode_002" / "live_calls.json"
    episode_calls = read_json(episode_path)
    episode_calls["calls"] = []
    digest = write_json(episode_path, episode_calls)
    episode_manifest = read_json(output / "episodes" / "episode_002" / "manifest.json")
    episode_manifest["artifact_hashes"]["live_calls.json"] = digest
    write_json(output / "episodes" / "episode_002" / "manifest.json", episode_manifest)

    fresh_client, _ = _pilot_client(output)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)


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


def test_planning_merge_resume_rejects_root_projection_mismatch(tmp_path):
    output = _make_episode_four_plan_error_output(tmp_path)
    episode_live = output / "episodes" / "episode_004" / "live_calls.json"
    value = read_json(episode_live)
    value["calls"] = []
    digest = write_json(episode_live, value)
    episode_manifest = read_json(output / "episodes" / "episode_004" / "manifest.json")
    episode_manifest["artifact_hashes"]["live_calls.json"] = digest
    write_json(output / "episodes" / "episode_004" / "manifest.json", episode_manifest)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        _resume_episode_four_output(output)


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


def test_live_pilot_rejects_root_and_episode_telemetry_mismatch(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    episode_path = output / "episodes" / "episode_001" / "live_calls.json"
    episode_calls = read_json(episode_path)
    episode_calls["calls"] = []
    write_json(episode_path, episode_calls)

    fresh_client, _ = _pilot_client(output)
    with pytest.raises(StorageError, match="telemetry projection mismatch"):
        PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)


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


def test_live_acceptance_prompt_contains_canonical_evidence(tmp_path):
    output = tmp_path / "pilot-live"
    client, provider_root = _pilot_client(output)

    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    prompt = json.loads(_acceptance_prompts(provider_root)[0])
    assert prompt["pilot_id"] == read_json(output / "pilot_manifest.json")["pilot_id"]
    assert len(prompt["pilot_evidence_packet"]["episodes"]) == 5
    assert "final" in prompt["pilot_evidence_packet"]["episodes"][0]


def test_live_acceptance_prompt_is_deterministic(tmp_path):
    first_output = tmp_path / "first"
    first_client, first_root = _pilot_client(first_output)
    PilotPipeline(first_client, scenario=None, mode="live").run(PILOT_FIXTURE, first_output)

    second_output = tmp_path / "second"
    second_client, second_root = _pilot_client(second_output)
    PilotPipeline(second_client, scenario=None, mode="live").run(PILOT_FIXTURE, second_output)

    assert sorted(_acceptance_prompts(first_root)) == sorted(_acceptance_prompts(second_root))


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


def test_pilot_live_complete_noop_preserves_all_hashes(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    fresh_client, _ = _pilot_client(output)
    PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    assert after == before
