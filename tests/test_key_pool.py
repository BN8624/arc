# 동적 key pool의 lease와 cooldown 계약을 검증한다.
from __future__ import annotations

from arc.live_model import DynamicKeyPool, GemmaPoolClient, LaunchPacer, LiveConfig, MODEL_NAME
from arc.pipeline import MockPipeline


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_round_robin_lease_and_release() -> None:
    clock = Clock()
    pool = DynamicKeyPool(["K01", "K02"], clock)
    first, _ = pool.lease()
    pool.release(first)
    second, _ = pool.lease()
    assert (first, second) == ("K01", "K02")


def test_transient_failure_cools_key_and_other_key_is_used() -> None:
    clock = Clock()
    pool = DynamicKeyPool(["K01", "K02"], clock)
    first, _ = pool.lease()
    pool.release(first, "RATE_LIMITED")
    second, _ = pool.lease()
    assert (first, second) == ("K01", "K02")


def test_credential_failure_disables_only_that_key() -> None:
    clock = Clock()
    pool = DynamicKeyPool(["K01", "K02"], clock)
    first, _ = pool.lease()
    pool.release(first, "AUTH_ERROR")
    second, _ = pool.lease()
    assert second == "K02"


def test_memory_desk_rotates_after_429_and_preserves_attempt_sequence(tmp_path) -> None:
    class ProviderError(RuntimeError):
        status_code = 429

    class Provider:
        def __init__(self, key: str) -> None:
            self.key = key

        @property
        def models(self):
            return self

        def generate_content(self, **_: object):
            if self.key == "key-10":
                raise ProviderError()
            return type("Response", (), {"text": '{"worker_id":"memory-important_excerpts","role":"important_excerpts","verdict":"OK","primary_finding":"f","primary_risk":"r","evidence_refs":["source:current_episode"],"proposal":{}}'})()

    config = LiveConfig(MODEL_NAME, {"K10": "key-10", "K11": "key-11"}, max_live=2, launch_interval=1.0)
    client = GemmaPoolClient(config, client_factory=Provider)
    client.pacer = LaunchPacer(0.0)
    pipeline = MockPipeline(client, mode="live")
    payload = {"episode_id": "E001", "final": "final", "memory_before": {"important_excerpts": [], "characters": [], "relationship_state": []}}

    results = pipeline._wave("memory", ["important_excerpts"], payload, tmp_path)

    calls = client.telemetry()["calls"]
    assert results[0]["role"] == "important_excerpts"
    assert [(call["key_slot"], call["status"], call["attempt"], call["lease_sequence"]) for call in calls] == [("K10", "FAIL", 1, 1), ("K11", "PASS", 2, 2)]
    assert calls[0]["http_status"] == 429
    assert (tmp_path / "memory_workers.partial.json").exists()
