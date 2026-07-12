# 파일럿 live runtime scope와 telemetry 계약을 검증한다.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from arc.live_model import AtomicTelemetryStore, GemmaPoolClient, LiveConfig, MODEL_NAME


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
