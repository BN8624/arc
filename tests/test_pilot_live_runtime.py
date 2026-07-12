# 파일럿 live runtime scope와 telemetry 계약을 검증한다.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from arc.live_model import AtomicTelemetryStore, GemmaPoolClient, LiveConfig, MODEL_NAME, RoutingStateStore
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
        stage = contents.split("Stage: ", 1)[1].split("\n", 1)[0]
        role = contents.split("Role: ", 1)[1].split("\n", 1)[0]
        payload = json.loads(contents.split("Input JSON:\n", 1)[1])
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
        self.failed: set[str] = set()
        self.provider_calls: list[tuple[str, str, str]] = []

    def factory(self, key: str) -> _PilotProvider:
        return _PilotProvider(self, key.replace("key-", "K"))

    def response(self, stage: str, role: str, payload: dict) -> str:
        episode_id = payload.get("episode_id") or payload.get("context", {}).get("episode_id") or "SYN001"
        if stage in {"planning", "review", "memory"}:
            evidence = ["final.md"] if stage == "memory" else ["source:current_episode"]
            return json.dumps({"worker_id": f"{stage}-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": evidence, "proposal": {"role": role}})
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
    assert len({call["scope_id"] for call in read_json(output / "pilot_live_calls.json")["calls"]}) == 5
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
    assert lease_sequences == sorted(lease_sequences)
    assert max(lease_sequences) == len(calls)


def test_live_pilot_resumes_interrupted_episode_without_recalling_completed_desks(tmp_path):
    output = tmp_path / "pilot-live"
    client, _ = _pilot_client(output)
    PilotPipeline(client, scenario=None, mode="live").run(PILOT_FIXTURE, output)
    before = len(read_json(output / "pilot_live_calls.json")["calls"])

    fresh_client, _ = _pilot_client(output)
    result = PilotPipeline(fresh_client, scenario=None, mode="live").run(PILOT_FIXTURE, output)

    assert result["no_op"] is True
    assert len(read_json(output / "pilot_live_calls.json")["calls"]) == before


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
