# 파일럿 live runtime scope와 telemetry 계약을 검증한다.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shutil
from pathlib import Path

import pytest

from arc.live_model import AtomicTelemetryStore, GemmaPoolClient, LiveConfig, MODEL_NAME, RoutingStateStore
from arc.pipeline import PLANNING_ROLES, WaveCheckpoint
from arc.pilot import PilotPipeline
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
            error.status_code = 500
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
    def __init__(self, fail_once_at: str | None = None, hold_episode: str | None = None):
        self.fail_once_at = fail_once_at
        self.hold_episode = hold_episode
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
            return json.dumps({"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"dimension_result": "PASS", "critical_finding": None}})
        if stage == "planning_merge":
            return json.dumps({"episode_id": episode_id, "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]})
        if stage == "writer":
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
    manifest["artifact_hashes"]["pilot_live_calls.json"] = root_hash
    write_json(output / "pilot_manifest.json", manifest)
    return output


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
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    after = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}

    assert result["no_op"] is True
    assert provider_root.provider_calls == []
    assert after == before


def test_live_pilot_hold_rerun_is_noop(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output, _PilotProviderRoot(hold_episode="episode_003"))
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = len(read_json(output / "pilot_live_calls.json")["calls"])

    fresh_client, provider_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert provider_root.provider_calls == []
    assert len(read_json(output / "pilot_live_calls.json")["calls"]) == before


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
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = len([call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "pilot:acceptance"])

    fresh_client, fresh_root = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert fresh_root.provider_calls == []
    assert len([call for call in read_json(output / "pilot_live_calls.json")["calls"] if call["scope_id"] == "pilot:acceptance"]) == before


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
