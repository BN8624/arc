# Routing state의 저장과 재시작 정규화를 검증한다.
from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

import pytest

from arc.live_model import DynamicKeyPool, KeyState, LiveConfigError, RoutingStateStore


def key(state: KeyState, *, cooldown: str | None = None, failures: int = 0, lease: int = 0) -> dict:
    return {"state": state, "consecutive_transient_failures": failures, "cooldown_until": cooldown, "last_lease_sequence": lease}


def test_state_round_trip_normalizes_in_use(tmp_path: Path) -> None:
    store = RoutingStateStore(tmp_path / "routing_state.json", ["K01", "K02"])
    store.save(cursor=1, lease_sequence=3, keys={"K01": key(KeyState.IN_USE), "K02": key(KeyState.DISABLED)})
    state = store.load()
    assert state["keys"]["K01"]["state"] == KeyState.AVAILABLE
    assert state["keys"]["K02"]["state"] == KeyState.DISABLED


def test_state_rejects_invalid_cursor(tmp_path: Path) -> None:
    store = RoutingStateStore(tmp_path / "routing_state.json", ["K01"])
    store.save(cursor=2, lease_sequence=1, keys={"K01": key(KeyState.AVAILABLE)})
    with pytest.raises(LiveConfigError):
        store.load()


def test_pool_lease_persists_routing_state(tmp_path: Path) -> None:
    store = RoutingStateStore(tmp_path / "routing_state.json", ["K01", "K02"])
    pool = DynamicKeyPool(["K01", "K02"], state_store=store)
    slot, _ = pool.lease()
    state = store.load()
    assert state["next_lease_sequence"] == 2
    assert state["keys"][slot]["state"] == KeyState.AVAILABLE


def test_pool_restores_expired_cooldown_as_available(tmp_path: Path) -> None:
    store = RoutingStateStore(tmp_path / "routing_state.json", ["K01"])
    store.save(cursor=0, lease_sequence=2, keys={"K01": key(KeyState.COOLDOWN, cooldown="2000-01-01T00:00:00+00:00", failures=1, lease=1)})
    pool = DynamicKeyPool(["K01"], state_store=store)
    slot, lease = pool.lease()
    assert (slot, lease) == ("K01", 2)


def test_state_rejects_invalid_cooldown_timestamp(tmp_path: Path) -> None:
    store = RoutingStateStore(tmp_path / "routing_state.json", ["K01"])
    store.save(cursor=0, lease_sequence=1, keys={"K01": key(KeyState.COOLDOWN, cooldown="bad")})
    with pytest.raises(LiveConfigError):
        store.load()
