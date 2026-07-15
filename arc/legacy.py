from __future__ import annotations
# Legacy prose terminal evidence를 읽기 전용으로 검증한다.

import re
from pathlib import Path

from .storage import read_json, sha256_file


LEGACY_PROSE_TERMINAL_EVIDENCE_INVALID = "LEGACY_PROSE_TERMINAL_EVIDENCE_INVALID"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class LegacyProseEvidenceError(ValueError):
    def __init__(self, message: str = LEGACY_PROSE_TERMINAL_EVIDENCE_INVALID):
        super().__init__(message)
        self.error_code = LEGACY_PROSE_TERMINAL_EVIDENCE_INVALID


def _require(condition: bool) -> None:
    if not condition:
        raise LegacyProseEvidenceError()


def validate_legacy_terminal_evidence(run_dir: Path, manifest: dict, stage: str, *, telemetry_path: Path | None = None) -> dict:
    """Validate a frozen v1 terminal state without writing or dispatching."""
    if stage not in {"writer", "revision"}:
        raise LegacyProseEvidenceError()
    prefix = f"{stage}_"
    _require(manifest.get("status") in {"COMPLETE", "HOLD"})
    count = manifest.get(f"{prefix}call_count")
    state = manifest.get(f"{prefix}attempt_state")
    _require(type(count) is int and count in {0, 1})
    _require(state in {"NOT_STARTED", "COMPLETED", "REJECTED"})
    _require(manifest.get(f"{prefix}exhausted") is (count == 1))
    _require((count == 0) == (state == "NOT_STARTED"))

    response_hash = manifest.get(f"{prefix}response_sha256")
    character_count = manifest.get(f"{prefix}character_count")
    call_id = manifest.get(f"{prefix}call_id")
    lease_sequence = manifest.get(f"{prefix}lease_sequence")

    telemetry_file = telemetry_path or run_dir / "live_calls.json"
    _require(telemetry_file.is_file())
    telemetry = read_json(telemetry_file)
    calls = [
        call for call in telemetry.get("calls", [])
        if call.get("stage") == stage and call.get("role") == "canonical"
    ]
    failures_for_stage = [
        item for item in telemetry.get("contract_failures", [])
        if item.get("stage") == stage and item.get("role") == "canonical"
    ]

    if count == 0:
        # NOT_STARTED terminal state: no content response, so every evidence field must be empty.
        for key in ("response_sha256", "character_count", "contract_code", "response_received_at", "call_id", "lease_sequence"):
            _require(manifest.get(f"{prefix}{key}") is None)
        _require(not [call for call in calls if call.get("status") == "PASS"])
        _require(not failures_for_stage)
        completed_name = "DRAFT_COMPLETED" if stage == "writer" else "REVISION_COMPLETED"
        artifact_name = "draft.md" if stage == "writer" else "revised.md"
        _require(completed_name not in manifest.get("completed_stages", []))
        _require(not (run_dir / artifact_name).exists())
        if stage == "writer":
            _require(not (run_dir / "draft_contract.json").exists())
        return {"valid": True, "no_op": True, "stage": stage, "call_id": None, "lease_sequence": None}

    _require(isinstance(response_hash, str) and _DIGEST.fullmatch(response_hash) is not None)
    _require(type(character_count) is int and character_count >= 0)
    _require(isinstance(manifest.get(f"{prefix}response_received_at"), str) and bool(manifest[f"{prefix}response_received_at"]))
    _require(isinstance(call_id, str) and bool(call_id))
    _require(type(lease_sequence) is int and lease_sequence >= 0)

    matching = [call for call in calls if call.get("call_id") == call_id]
    _require(len(matching) == 1)
    call = matching[0]
    _require(call.get("status") == "PASS")
    _require(call.get("response_sha256") == response_hash)
    _require(call.get("lease_sequence") == lease_sequence)
    _require(call.get("output_characters") == character_count)
    _require(len({item.get("call_id") for item in calls}) == len(calls))
    _require(len({item.get("lease_sequence") for item in calls}) == len(calls))

    failures = failures_for_stage
    contract_code = manifest.get(f"{prefix}contract_code")
    if state == "REJECTED":
        _require(isinstance(contract_code, str) and bool(contract_code))
        matched_failures = [item for item in failures if item.get("call_id") == call_id]
        _require(len(matched_failures) == 1)
        failure = matched_failures[0]
        _require(failure.get("contract_code") == contract_code)
        _require(failure.get("character_count") == character_count)
    else:
        _require(contract_code is None)
        _require(not failures)

    artifact_name = "draft.md" if stage == "writer" else "revised.md"
    contract_name = "draft_contract.json" if stage == "writer" else None
    completed_name = "DRAFT_COMPLETED" if stage == "writer" else "REVISION_COMPLETED"
    artifact = run_dir / artifact_name
    if state == "COMPLETED":
        _require(completed_name in manifest.get("completed_stages", []))
        _require(artifact.exists())
    elif state == "REJECTED":
        _require(completed_name not in manifest.get("completed_stages", []))
        _require(not artifact.exists())
    _require((completed_name in manifest.get("completed_stages", [])) == artifact.exists())
    if artifact.exists():
        _require(manifest.get("artifact_hashes", {}).get(artifact_name) == sha256_file(artifact))
        _require(sha256_file(artifact) == response_hash)
        _require(len(artifact.read_text(encoding="utf-8")) == character_count)
    if contract_name:
        contract = run_dir / contract_name
        _require((completed_name in manifest.get("completed_stages", [])) == contract.exists())
        if contract.exists():
            value = read_json(contract)
            _require(value.get("character_count") == character_count)
            _require(value.get("contract_code") == contract_code)
    return {"valid": True, "no_op": True, "stage": stage, "call_id": call_id, "lease_sequence": lease_sequence}
