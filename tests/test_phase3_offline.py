from __future__ import annotations
# Phase 3 prose integrity, calibration, quota, and thinking 설정의 offline 계약을 검증한다.

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from arc.calibration import ProseCalibrationError, _cycle, prose_calibration_status, run_prose_calibration, validate_pilot_calibration
from arc.contracts import ContractError, materialize_prose_provider_response, validate_draft_prose
from arc.legacy import LegacyProseEvidenceError, validate_legacy_terminal_evidence
from arc.live_model import GemmaPoolClient, LiveCallError, LiveConfig, LogicalDesk, MODEL_NAME
from arc.prompts import build_prompt
from arc.quota import PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED, PROJECT_QUOTA_ADMISSION_EXHAUSTED, ProjectQuotaAdmission, ProjectQuotaError, ProjectQuotaLedger, QuotaLimits
from arc.storage import read_json, write_json, write_text
from arc.usage import TokenAdmissionError, TokenGate, UsageLedger


def _wire(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False, separators=(",", ":"))


class _CalibrationClient:
    def __init__(self, *, writer: str = "A" * 5200, revision: str = "B" * 5200):
        self.writer, self.revision, self.calls = writer, revision, []
        self.config = type("Config", (), {"model": MODEL_NAME, "prose_limit": 32768, "quota_project_buckets": None})()

    def set_prose_thinking_level(self, level: str) -> None:
        self.level = level

    def generate(self, *, stage: str, role: str, prompt: str) -> str:
        raw = _wire(self.writer if stage == "writer" else self.revision)
        self.calls.append({"stage": stage, "role": role, "status": "PASS", "call_id": f"{stage}-{len(self.calls) + 1}", "lease_sequence": len(self.calls) + 1, "input_hash": hashlib.sha256(prompt.encode()).hexdigest(), "prompt": prompt, "response_sha256": hashlib.sha256(raw.encode()).hexdigest(), "reasoning_tokens": 10})
        return raw

    def telemetry(self) -> dict:
        return {"calls": self.calls, "contract_failures": []}


def _calibration(tmp_path: Path) -> Path:
    preflight = tmp_path / "preflight.json"
    write_json(preflight, {"status": "PASS", "live_run_allowed": True, "model": MODEL_NAME})
    output = tmp_path / "calibration"
    run_prose_calibration(output, preflight, ["profile_v2_high", "profile_v2_minimal"], 3, _CalibrationClient())
    return output


# Duplicate envelope: exact single field PASS, duplicate, extra, malformed, and fence cases.
def test_duplicate_envelope_rejects_duplicate_text() -> None:
    with pytest.raises(ContractError) as error:
        materialize_prose_provider_response('{"text":"첫 값","text":"둘째 값"}', stage="writer")
    assert error.value.contract_code == "PROSE_PROVIDER_DUPLICATE_FIELD"


def test_duplicate_envelope_accepts_exact_single_field() -> None:
    assert materialize_prose_provider_response(_wire("본문"), stage="writer") == "본문"


def test_duplicate_envelope_rejects_extra_field() -> None:
    with pytest.raises(ContractError) as error:
        materialize_prose_provider_response('{"text":"본문","extra":1}', stage="writer")
    assert error.value.contract_code == "PROSE_PROVIDER_FIELDS_MISMATCH"


def test_duplicate_envelope_rejects_malformed_json() -> None:
    with pytest.raises(ContractError) as error:
        materialize_prose_provider_response('{"text":', stage="writer")
    assert error.value.contract_code == "PROSE_PROVIDER_RESPONSE_MALFORMED"


def test_duplicate_envelope_rejects_code_fence() -> None:
    with pytest.raises(ContractError) as error:
        materialize_prose_provider_response("```json\n{\"text\":\"본문\"}\n```", stage="writer")
    assert error.value.contract_code == "PROSE_PROVIDER_RESPONSE_MALFORMED"


def _legacy_fixture(tmp_path: Path) -> tuple[Path, dict, dict, str]:
    text = "A" * 4000
    digest = hashlib.sha256(text.encode()).hexdigest()
    write_text(tmp_path / "draft.md", text)
    write_json(tmp_path / "draft_contract.json", {"character_count": 4000, "contract_code": None})
    call = {"stage": "writer", "role": "canonical", "status": "PASS", "call_id": "legacy-1", "lease_sequence": 1, "response_sha256": digest, "output_characters": 4000}
    telemetry = {"calls": [call], "contract_failures": []}
    manifest = {"status": "HOLD", "writer_call_count": 1, "writer_attempt_state": "COMPLETED", "writer_exhausted": True, "writer_response_sha256": digest, "writer_character_count": 4000, "writer_contract_code": None, "writer_response_received_at": "2026-01-01T00:00:00+00:00", "writer_call_id": "legacy-1", "writer_lease_sequence": 1, "completed_stages": ["DRAFT_COMPLETED"], "artifact_hashes": {"draft.md": digest}}
    write_json(tmp_path / "live_calls.json", telemetry)
    return tmp_path, manifest, telemetry, digest


def test_legacy_terminal_exact_evidence_is_read_only_noop(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    assert validate_legacy_terminal_evidence(path, manifest, "writer")["no_op"] is True


def test_legacy_terminal_response_hash_mismatch_is_blocked(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    manifest["writer_response_sha256"] = "a" * 64
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_terminal_missing_telemetry_is_blocked(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    (path / "live_calls.json").unlink()
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_terminal_duplicate_matching_call_is_blocked(tmp_path: Path) -> None:
    path, manifest, telemetry, _ = _legacy_fixture(tmp_path)
    telemetry["calls"].append(dict(telemetry["calls"][0]))
    write_json(path / "live_calls.json", telemetry)
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_terminal_call_id_mismatch_is_blocked(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    manifest["writer_call_id"] = "wrong"
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_terminal_lease_mismatch_is_blocked(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    manifest["writer_lease_sequence"] = 2
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_terminal_artifact_state_contradiction_is_blocked(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    manifest["completed_stages"] = []
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_terminal_contract_failure_mismatch_is_blocked(tmp_path: Path) -> None:
    path, manifest, telemetry, digest = _legacy_fixture(tmp_path)
    manifest.update({"writer_attempt_state": "REJECTED", "writer_contract_code": "PROSE_TOO_SHORT"})
    telemetry["contract_failures"] = [{"stage": "writer", "role": "canonical", "call_id": "legacy-1", "contract_code": "PROSE_TOO_LONG", "character_count": 4000}]
    write_json(path / "live_calls.json", telemetry)
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_completed_requires_stage_and_artifact(tmp_path: Path) -> None:
    path, manifest, _, _ = _legacy_fixture(tmp_path)
    manifest["completed_stages"] = []
    (path / "draft.md").unlink()
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


def test_legacy_rejected_requires_no_completion_artifact(tmp_path: Path) -> None:
    path, manifest, telemetry, digest = _legacy_fixture(tmp_path)
    manifest.update({"writer_attempt_state": "REJECTED", "writer_contract_code": "PROSE_TOO_SHORT"})
    telemetry["contract_failures"] = [{"stage": "writer", "role": "canonical", "call_id": "legacy-1", "contract_code": "PROSE_TOO_SHORT", "character_count": 4000}]
    write_json(path / "live_calls.json", telemetry)
    with pytest.raises(LegacyProseEvidenceError):
        validate_legacy_terminal_evidence(path, manifest, "writer")


@pytest.mark.parametrize(("count", "verdict"), [(3905, "REVISE_REQUIRED"), (4000, "PASS"), (8000, "PASS")])
def test_writer_calibration_contract_admits_expected_lengths(count: int, verdict: str) -> None:
    _, contract = validate_draft_prose("A" * count)
    assert contract["verdict"] == verdict


@pytest.mark.parametrize("count", [2999, 8001])
def test_writer_calibration_contract_rejects_outside_band(count: int) -> None:
    with pytest.raises(ContractError):
        validate_draft_prose("A" * count)


def test_end_to_end_repairable_writer_is_revision_input() -> None:
    client = _CalibrationClient(writer="A" * 3500, revision="B" * 4000)
    result = _cycle("profile_v2_high", 1, client, {"identity": "repair"})
    assert result["verdict"] == "PASS"
    assert "A" * 3500 in client.calls[1].get("prompt", "") or client.calls[1]["input_hash"] == hashlib.sha256(("A" * 3500).encode()).hexdigest()


def test_end_to_end_direct_writer_uses_deterministic_challenge() -> None:
    client = _CalibrationClient(writer="A" * 5200, revision="B" * 4000)
    result = _cycle("profile_v2_high", 1, client, {"identity": "direct"})
    assert result["challenge"] is True
    assert result["revision_input_hash"] != hashlib.sha256(("A" * 5200).encode()).hexdigest()


def test_end_to_end_revision_3999_fails() -> None:
    assert _cycle("profile_v2_high", 1, _CalibrationClient(writer="A" * 3500, revision="B" * 3999), {})["verdict"] == "FAIL"


def test_end_to_end_revision_4000_passes() -> None:
    assert _cycle("profile_v2_high", 1, _CalibrationClient(writer="A" * 3500, revision="B" * 4000), {})["verdict"] == "PASS"


@pytest.mark.parametrize("mutation", ["overall", "profile", "cycle", "character", "contract", "selected", "raw", "materialized"])
def test_calibration_status_rejects_verdict_and_receipt_mutations(tmp_path: Path, mutation: str) -> None:
    output = _calibration(tmp_path)
    root_path = output / "prose_calibration.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    if mutation == "overall":
        root["overall_status"] = "FAIL"
    elif mutation == "profile":
        root["profiles"]["profile_v2_high"]["verdict"] = "FAIL"
    elif mutation == "cycle":
        root["profiles"]["profile_v2_high"]["cycles"][0]["verdict"] = "FAIL"
    elif mutation == "character":
        root["profiles"]["profile_v2_high"]["cycles"][0]["revision"]["character_count"] += 1
    elif mutation == "contract":
        root["profiles"]["profile_v2_high"]["cycles"][0]["writer"]["contract_code"] = "MUTATED"
    elif mutation == "selected":
        root["selected_profile"] = "profile_v2_high" if root["selected_profile"] == "profile_v2_minimal" else "profile_v2_minimal"
    elif mutation == "raw":
        root["profiles"]["profile_v2_high"]["cycles"][0]["writer"]["raw_response"] += "mutated"
    else:
        root["profiles"]["profile_v2_high"]["cycles"][0]["writer"]["materialized_prose_sha256"] = "0" * 64
    write_json(root_path, root)
    with pytest.raises(ProseCalibrationError):
        prose_calibration_status(output)


def test_calibration_status_rejects_telemetry_hash_mismatch(tmp_path: Path) -> None:
    output = _calibration(tmp_path)
    telemetry = json.loads((output / "prose_calibration_calls.json").read_text(encoding="utf-8"))
    telemetry["calls"][0]["response_sha256"] = "0" * 64
    write_json(output / "prose_calibration_calls.json", telemetry)
    with pytest.raises(ProseCalibrationError):
        prose_calibration_status(output)


def test_calibration_status_rejects_unknown_artifact(tmp_path: Path) -> None:
    output = _calibration(tmp_path)
    write_text(output / "unknown.json", "{}")
    with pytest.raises(ProseCalibrationError):
        prose_calibration_status(output)


def test_calibration_status_rejects_missing_profile(tmp_path: Path) -> None:
    output = _calibration(tmp_path)
    root_path = output / "prose_calibration.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    del root["profiles"]["profile_v2_minimal"]
    write_json(root_path, root)
    with pytest.raises(ProseCalibrationError):
        prose_calibration_status(output)


def test_pilot_calibration_rejects_future_and_sibling_paths(tmp_path: Path) -> None:
    output = _calibration(tmp_path)
    root_path = output / "prose_calibration.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    from datetime import datetime, timedelta, timezone
    root["created_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    write_json(root_path, root)
    with pytest.raises(ProseCalibrationError):
        validate_pilot_calibration(root_path, model=MODEL_NAME)
    sibling = output / "copy.json"
    write_json(sibling, root)
    with pytest.raises(ProseCalibrationError):
        validate_pilot_calibration(sibling, model=MODEL_NAME)


def test_token_gate_preserves_single_request_error_and_finishes_count_once(tmp_path: Path) -> None:
    ledger = UsageLedger(tmp_path / "usage.sqlite3")
    gate = TokenGate(ledger, counter=lambda *_: 14001, usage_run_id="run", id_factory=lambda: "attempt")
    with pytest.raises(TokenAdmissionError, match=PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED):
        gate.admit(client=object(), model=MODEL_NAME, prompt="prompt", config={}, key_slot_id="K01", call={"stage": "writer", "role": "canonical"}, max_output_tokens=32768, max_input_tokens=14000)
    with ledger._connect() as connection:
        rows = connection.execute("SELECT status, error_code FROM usage_events WHERE request_kind='count_tokens'").fetchall()
    assert len(rows) == 1 and rows[0]["status"] == "SUCCEEDED" and rows[0]["error_code"] is None


def test_calibration_selection_uses_reasoning_and_target_distance(tmp_path: Path) -> None:
    root = prose_calibration_status(_calibration(tmp_path))
    assert root["profiles"]["profile_v2_high"]["reasoning_tokens"] == 60
    assert root["profiles"]["profile_v2_minimal"]["median_final_character_count"] == 5200
    assert root["selected_profile"] == "profile_v2_minimal"
    assert root["selection_reason"] == "reasoning_tokens_then_target_center_then_minimal"


def test_pilot_calibration_gate_checks_profile_inputs(tmp_path: Path) -> None:
    output = _calibration(tmp_path)
    with pytest.raises(ProseCalibrationError):
        validate_pilot_calibration(output / "prose_calibration.json", model=MODEL_NAME, prompt_templates_hash_value="0" * 64)
    with pytest.raises(ProseCalibrationError):
        validate_pilot_calibration(output / "prose_calibration.json", model=MODEL_NAME, provider_contract_version=999)


def test_fake_live_full_pilot_requires_calibration_and_second_run_is_noop(tmp_path: Path) -> None:
    from tests.test_pilot_live_runtime import PILOT_FIXTURE, _PilotProviderRoot, _pilot_client
    from arc.pilot import PilotPipeline

    missing_client, missing_root = _pilot_client(tmp_path / "missing")
    with pytest.raises(ProseCalibrationError):
        PilotPipeline(missing_client, scenario=None, mode="live", require_prose_calibration=True).run(PILOT_FIXTURE, tmp_path / "missing-run")
    assert missing_root.provider_calls == []

    calibration_client, _ = _pilot_client(tmp_path / "calibration", _PilotProviderRoot())
    preflight = tmp_path / "preflight.json"
    write_json(preflight, {"status": "PASS", "live_run_allowed": True, "model": MODEL_NAME})
    calibration_root = tmp_path / "calibration-root"
    run_prose_calibration(calibration_root, preflight, ["profile_v2_high", "profile_v2_minimal"], 3, calibration_client)
    calibration = calibration_root / "prose_calibration.json"

    invalid = json.loads(calibration.read_text(encoding="utf-8"))
    invalid["overall_status"] = "FAIL"
    invalid_path = tmp_path / "invalid.json"
    write_json(invalid_path, invalid)
    invalid_client, invalid_root = _pilot_client(tmp_path / "invalid-run", _PilotProviderRoot())
    with pytest.raises(ProseCalibrationError):
        PilotPipeline(invalid_client, scenario=None, mode="live", prose_calibration=invalid_path, require_prose_calibration=True).run(PILOT_FIXTURE, tmp_path / "invalid-pilot")
    assert invalid_root.provider_calls == []

    output = tmp_path / "pilot"
    live_client, live_root = _pilot_client(output, _PilotProviderRoot(repairable_writer_once_episode="episode_001"))
    result = PilotPipeline(live_client, scenario=None, mode="live", prose_calibration=calibration, require_prose_calibration=True).run(PILOT_FIXTURE, output)
    assert result["manifest"]["status"] == "COMPLETE"
    assert len(result["manifest"]["completed_episodes"]) == 5
    assert len(result["manifest"]["completed_transitions"]) == 4
    assert result["manifest"]["prose_generation_profile_version"] == 2
    assert result["manifest"]["quota_project_bucket_count"] == 11
    assert live_client._prose_thinking_level == "minimal"
    assert {stage for stage, config in live_root.generation_configs if stage in {"writer", "revision"}} == {"writer", "revision"}
    assert all(config["thinkingConfig"]["thinkingLevel"] == "minimal" for stage, config in live_root.generation_configs if stage in {"writer", "revision"})
    acceptance = read_json(output / "pilot_acceptance.json")
    assert acceptance["verdict"] == "PASS"
    assert len(acceptance["dimension_results"]) == 7
    assert all(value == "PASS" for value in acceptance["dimension_results"].values())
    assert len(live_root.provider_calls) <= 300
    before = {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()}
    resumed_client, resumed_root = _pilot_client(output, _PilotProviderRoot())
    assert PilotPipeline(resumed_client, scenario=None, mode="live", prose_calibration=calibration, require_prose_calibration=True).run(PILOT_FIXTURE, output)["no_op"] is True
    assert resumed_root.provider_calls == []
    assert {path.relative_to(output).as_posix(): path.read_bytes() for path in output.rglob("*") if path.is_file()} == before


def test_quota_single_request_over_limit_blocks() -> None:
    with pytest.raises(ProjectQuotaError) as error:
        ProjectQuotaLedger(QuotaLimits(max_input_tokens_per_request=10)).reserve("Q01", 11)
    assert error.value.error_code == PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED


def test_quota_single_request_over_limit_has_zero_generation_calls() -> None:
    class FixedTokenGate:
        def admit(self, **_: object) -> tuple[str, int]:
            return "event-1", 14001

        def finish(self, **_: object) -> None:
            return None

    config = LiveConfig(MODEL_NAME, {"K01": "key-01"}, launch_interval=0)
    provider_calls: list[str] = []
    client = GemmaPoolClient(config, client_factory=lambda _: type("Provider", (), {"models": type("Models", (), {"generate_content": lambda *_args, **_kwargs: provider_calls.append("generate")})()})(), usage_gate=FixedTokenGate())
    with pytest.raises(LiveCallError) as error:
        client.generate_for_desk(desk=LogicalDesk("desk", "planning", "event", 1), prompt="one")
    assert error.value.error_class == PROJECT_INPUT_TPM_SINGLE_REQUEST_EXCEEDED
    assert provider_calls == [] and client.calls == []


def test_quota_tpm_exact_safety_limit_passes() -> None:
    ledger = ProjectQuotaLedger(QuotaLimits(safety_input_tpm=10, max_input_tokens_per_request=10))
    ledger.reserve("Q01", 10)
    assert ledger.snapshot()["Q01"]["remaining_tpm_headroom"] == 0


def test_quota_tpm_one_token_over_blocks() -> None:
    ledger = ProjectQuotaLedger(QuotaLimits(safety_input_tpm=10, max_input_tokens_per_request=10))
    ledger.reserve("Q01", 10)
    with pytest.raises(ProjectQuotaError):
        ledger.reserve("Q01", 1)


def test_quota_rpm_safety_limit_is_27() -> None:
    assert QuotaLimits().safety_rpm == 27


def test_quota_rpd_safety_limit_is_13000() -> None:
    assert QuotaLimits().safety_rpd == 13000


def test_quota_shared_project_bucket_aggregates_slots() -> None:
    admission = ProjectQuotaAdmission(["K01", "K02"], QuotaLimits(safety_rpm=2), {"K01": "Q01", "K02": "Q01"})
    admission.reserve_for_slot("K01", 1)
    admission.reserve_for_slot("K02", 1)
    assert admission.telemetry()["buckets"]["Q01"]["rolling_request_count"] == 2


def test_quota_separate_buckets_do_not_share() -> None:
    admission = ProjectQuotaAdmission(["K01", "K02"], QuotaLimits(safety_rpm=1), {"K01": "Q01", "K02": "Q02"})
    admission.reserve_for_slot("K01", 1)
    admission.reserve_for_slot("K02", 1)
    assert set(admission.telemetry()["buckets"]) == {"Q01", "Q02"}


def test_quota_concurrent_reservation_allows_only_one() -> None:
    admission = ProjectQuotaAdmission(["K01"], QuotaLimits(safety_rpm=1), {"K01": "Q01"})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: _reserve_once(admission), range(2)))
    assert sum(results) == 1


def _reserve_once(admission: ProjectQuotaAdmission) -> int:
    try:
        admission.reserve_for_slot("K01", 1)
        return 1
    except ProjectQuotaError:
        return 0


def test_quota_blocked_first_bucket_reroutes_and_counts() -> None:
    admission = ProjectQuotaAdmission(["K01", "K02"], QuotaLimits(safety_rpm=1), {"K01": "Q01", "K02": "Q02"})
    admission.reserve_for_slot("K01", 1)
    reservation = admission.reserve_for_candidates(["K01", "K02"], 1)
    assert reservation.bucket_id == "Q02" and admission.ledger.reroute_count == 1


def test_quota_count_token_result_is_reused_across_reroute() -> None:
    count_calls = 0
    counted_tokens = 9
    def count_once() -> int:
        nonlocal count_calls
        count_calls += 1
        return counted_tokens
    admission = ProjectQuotaAdmission(["K01", "K02"], QuotaLimits(safety_rpm=1), {"K01": "Q01", "K02": "Q02"})
    admission.reserve_for_slot("K01", counted_tokens)
    admission.reserve_for_candidates(["K01", "K02"], count_once())
    assert count_calls == 1


def test_quota_bounded_wait_uses_earliest_release() -> None:
    now = [100.0]
    waits = []
    def waiter(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds + 0.001
    admission = ProjectQuotaAdmission(["K01"], QuotaLimits(safety_rpm=1), {"K01": "Q01"}, clock=lambda: now[0], waiter=waiter)
    admission.reserve_for_slot("K01", 1)
    admission.reserve_for_candidates(["K01"], 1, deadline=200)
    assert waits and admission.ledger.wait_count == 1


def test_quota_deadline_after_release_blocks_without_provider() -> None:
    now = [100.0]
    admission = ProjectQuotaAdmission(["K01"], QuotaLimits(safety_rpm=1), {"K01": "Q01"}, clock=lambda: now[0], waiter=lambda _: None)
    admission.reserve_for_slot("K01", 1)
    with pytest.raises(ProjectQuotaError, match=PROJECT_QUOTA_ADMISSION_EXHAUSTED):
        admission.reserve_for_candidates(["K01"], 1, deadline=101)


def test_quota_block_does_not_consume_provider_attempt() -> None:
    admission = ProjectQuotaAdmission(["K01"], QuotaLimits(safety_rpm=1), {"K01": "Q01"})
    admission.reserve_for_slot("K01", 1)
    with pytest.raises(ProjectQuotaError):
        admission.reserve_for_candidates(["K01"], 1, deadline=admission.ledger._now())
    assert admission.ledger.snapshot()["Q01"]["rolling_request_count"] == 1


def test_quota_dispatch_is_the_state_transition() -> None:
    ledger = ProjectQuotaLedger(QuotaLimits())
    reservation = ledger.reserve("Q01", 1)
    assert reservation.state == "RESERVED"
    ledger.dispatch(reservation.reservation_id)
    assert reservation.state == "DISPATCHED"


def test_quota_restart_restores_rolling_usage() -> None:
    ledger = ProjectQuotaLedger(QuotaLimits())
    ledger.reserve("Q01", 7)
    restored = ProjectQuotaLedger(QuotaLimits())
    restored.restore_state(ledger.export_state())
    assert restored.snapshot()["Q01"]["rolling_reserved_input_tokens"] == 7


def test_quota_sqlite_backend_allows_only_one_cross_instance_reservation(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    limits = QuotaLimits(safety_rpm=1)
    first = ProjectQuotaAdmission(["K01"], limits, {"K01": "Q01"}, usage_ledger=UsageLedger(path))
    second = ProjectQuotaAdmission(["K01"], limits, {"K01": "Q01"}, usage_ledger=UsageLedger(path))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda admission: _reserve_backend_once(admission), (first, second)))
    assert sum(results) == 1


def _reserve_backend_once(admission: ProjectQuotaAdmission) -> int:
    try:
        admission.reserve_for_slot("K01", 1)
        return 1
    except ProjectQuotaError:
        return 0


def test_quota_sqlite_backend_preserves_dispatched_usage_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "usage.sqlite3"
    limits = QuotaLimits(safety_rpm=1)
    first = ProjectQuotaAdmission(["K01"], limits, {"K01": "Q01"}, usage_ledger=UsageLedger(path))
    reservation = first.reserve_for_slot("K01", 1)
    first.ledger.dispatch(reservation.reservation_id)
    restarted = ProjectQuotaAdmission(["K01"], limits, {"K01": "Q01"}, usage_ledger=UsageLedger(path))
    assert restarted.ledger.snapshot()["Q01"]["rolling_request_count"] == 1
    with pytest.raises(ProjectQuotaError):
        restarted.reserve_for_slot("K01", 1)


def test_bookkeeping_failure_returns_provider_attempt_before_generate() -> None:
    class FailingDispatchGate:
        def admit(self, **_: object) -> tuple[str, int]:
            return "event-1", 0

        def mark_dispatched(self, _: str) -> None:
            raise RuntimeError("injected bookkeeping failure")

        def cancel(self, *_: object) -> None:
            return None

    provider_calls: list[str] = []
    provider = type("Provider", (), {"models": type("Models", (), {"generate_content": lambda *_args, **_kwargs: provider_calls.append("generate")})()})()
    config = LiveConfig(MODEL_NAME, {"K01": "key-01"}, launch_interval=0)
    client = GemmaPoolClient(config, client_factory=lambda _: provider, usage_gate=FailingDispatchGate())
    with pytest.raises(LiveCallError) as error:
        client.generate_for_desk(desk=LogicalDesk("desk", "planning", "event", 1), prompt="one")
    assert error.value.error_class == "DISPATCH_BOOKKEEPING_FAILED"
    assert provider_calls == []
    assert client.telemetry()["retry_budget"]["provider_attempts_used"] == 0


def test_quota_pacific_date_rollover_changes_rpd_window() -> None:
    now = [1704095999.0]
    ledger = ProjectQuotaLedger(QuotaLimits(safety_rpd=1), clock=lambda: now[0])
    ledger.reserve("Q01", 1)
    now[0] += 2
    ledger.reserve("Q01", 1)
    assert ledger.snapshot()["Q01"]["daily_request_count"] == 1


def test_quota_telemetry_never_contains_key_values() -> None:
    admission = ProjectQuotaAdmission(["K01"], QuotaLimits(), {"K01": "Q01"})
    assert "key" not in json.dumps(admission.telemetry()).lower()


@pytest.mark.parametrize("prose_level", ["high", "minimal"])
def test_thinking_config_prose_levels(prose_level: str) -> None:
    config = LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i}" for i in range(1, 12)}, launch_interval=1, prose_thinking_level=prose_level)
    client = GemmaPoolClient(config, client_factory=lambda _: object())
    assert client._generation_config("writer")["thinkingConfig"]["thinkingLevel"] == prose_level


def test_thinking_config_other_stages_use_global_high() -> None:
    config = LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i}" for i in range(1, 12)}, launch_interval=1, thinking_level="high", prose_thinking_level="minimal")
    assert GemmaPoolClient(config, client_factory=lambda _: object())._generation_config("planning")["thinkingConfig"]["thinkingLevel"] == "high"


def test_thinking_config_rejects_prose_low() -> None:
    config = LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i}" for i in range(1, 12)}, launch_interval=1, prose_thinking_level="low")
    with pytest.raises(Exception, match="prose thinking level"):
        config.validate()


def test_thinking_config_rejects_prose_medium() -> None:
    config = LiveConfig(MODEL_NAME, {f"K{i:02d}": f"key-{i}" for i in range(1, 12)}, launch_interval=1, prose_thinking_level="medium")
    with pytest.raises(Exception, match="prose thinking level"):
        config.validate()
