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
