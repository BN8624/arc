from __future__ import annotations
# 기존 HOLD 에피소드로 bounded prose live probe를 안전하게 실행한다.

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ContractError, PROSE_MAX_CHARACTERS, PROSE_MIN_CHARACTERS, PROSE_PROVIDER_CONTRACT_VERSION, PROSE_REPAIRABLE_MIN_CHARACTERS, materialize_prose_provider_response, validate_prose
from .prompts import build_prompt, revision_expansion_guidance
from .storage import read_json, sha256_bytes, sha256_file, verify_artifacts, write_json


PROBE_ARTIFACT = "prose_probe.json"
PROBE_TELEMETRY = "prose_probe_calls.json"
PROBE_OPERATIONAL_FILES = {"routing_state.json", PROBE_TELEMETRY}
SOURCE_FILES = ("context_packet.json", "episode_plan.json", "draft.md", "draft_contract.json", "review_decision.json")


class ProseProbeError(RuntimeError):
    """A bounded prose probe could not be executed safely."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_probe_source(source_episode: Path) -> dict:
    if source_episode.name != "episode_002" or source_episode.parent.name != "episodes":
        raise ProseProbeError("source episode must be episode_002")
    pilot_root = source_episode.parent.parent
    pilot_manifest_path = pilot_root / "pilot_manifest.json"
    if not pilot_manifest_path.exists():
        raise ProseProbeError("source pilot manifest is missing")
    pilot_manifest = read_json(pilot_manifest_path)
    if pilot_manifest.get("status") != "HOLD" or pilot_manifest.get("active_episode_id") != "episode_002":
        raise ProseProbeError("source pilot is not the expected HOLD")
    manifest_path = source_episode / "manifest.json"
    if not manifest_path.exists():
        raise ProseProbeError("source episode manifest is missing")
    manifest = read_json(manifest_path)
    if manifest.get("episode_id") != "episode_002" or manifest.get("status") != "HOLD":
        raise ProseProbeError("source episode is not the expected HOLD")
    if manifest.get("revision_attempt_state") != "REJECTED" or manifest.get("revision_contract_code") != "PROSE_TOO_SHORT":
        raise ProseProbeError("source revision rejection evidence is invalid")
    for name in SOURCE_FILES:
        if not (source_episode / name).is_file() or name not in manifest.get("artifact_hashes", {}):
            raise ProseProbeError(f"required source artifact is missing: {name}")
    try:
        verify_artifacts(source_episode, manifest)
    except Exception as error:
        raise ProseProbeError("source artifact integrity failed") from error
    draft_contract = read_json(source_episode / "draft_contract.json")
    decision = read_json(source_episode / "review_decision.json")
    if draft_contract.get("verdict") != "REVISE_REQUIRED" or draft_contract.get("contract_code") != "PROSE_UNDERLENGTH_REPAIRABLE":
        raise ProseProbeError("source draft is not repairable")
    count = draft_contract.get("character_count")
    if not isinstance(count, int) or not PROSE_REPAIRABLE_MIN_CHARACTERS <= count < PROSE_MIN_CHARACTERS:
        raise ProseProbeError("source draft character count is not repairable")
    if decision.get("verdict") != "REVISE_ONCE":
        raise ProseProbeError("source review does not require one revision")
    return {
        "pilot_root": pilot_root,
        "pilot_manifest": pilot_manifest,
        "episode_manifest": manifest,
        "context": read_json(source_episode / "context_packet.json"),
        "plan": read_json(source_episode / "episode_plan.json"),
        "draft": (source_episode / "draft.md").read_text(encoding="utf-8"),
        "draft_contract": draft_contract,
        "decision": decision,
        "hashes": {name: sha256_file(source_episode / name) for name in SOURCE_FILES},
    }


def _load_preflight(preflight: Path) -> tuple[dict, str]:
    if not preflight.is_file():
        raise ProseProbeError("preflight artifact is missing")
    document = read_json(preflight)
    if document.get("status") not in {"PASS", "DEGRADED_PASS"} or document.get("live_run_allowed") is not True:
        raise ProseProbeError("preflight does not admit a live probe")
    if document.get("global_blocker_slots") != 0 or document.get("unknown_slots") != 0 or document.get("pass_slots", 0) < 1:
        raise ProseProbeError("preflight admission counts are invalid")
    return document, sha256_file(preflight)


def _stage_result(client: object, stage: str, prompt: str, text: str) -> dict:
    calls = [call for call in client.telemetry().get("calls", []) if call.get("stage") == stage and call.get("role") == "canonical"]
    content_calls = [call for call in calls if call.get("status") == "PASS"]
    raw_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    matching_content_calls = [call for call in content_calls if call.get("response_sha256") == raw_digest]
    call = matching_content_calls[-1] if matching_content_calls else {}
    materialized_digest, character_count = None, None
    envelope_valid, verdict, contract_code = False, "FAIL", None
    try:
        materialized = materialize_prose_provider_response(text, stage=stage)
        envelope_valid = True
        materialized_digest = hashlib.sha256(materialized.encode("utf-8")).hexdigest()
        character_count = len(materialized)
        validate_prose(materialized)
        verdict = "PASS"
    except ContractError as error:
        contract_code = error.contract_code
        client.record_contract_failure(stage, "canonical", contract_code=contract_code, character_count=character_count)
    return {
        "stage": stage,
        "prose_provider_contract_version": PROSE_PROVIDER_CONTRACT_VERSION,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "raw_response_sha256": raw_digest,
        "materialized_prose_sha256": materialized_digest,
        "envelope_valid": envelope_valid,
        "character_count": character_count,
        "validation_verdict": verdict,
        "prose_contract_code": contract_code,
        "contract_code": contract_code,
        "call_id": call.get("call_id"),
        "key_slot": call.get("key_slot"),
        "lease_sequence": call.get("lease_sequence"),
        "transport_attempt_count": len(calls),
        "actual_content_response_count": len(content_calls),
    }


def _telemetry_checkpoint(telemetry: dict) -> dict:
    calls = telemetry.get("calls", [])
    return {
        "schema_version": 1,
        "call_count": len(calls),
        "calls_sha256": sha256_bytes(json.dumps(calls, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
        "contract_failure_count": len(telemetry.get("contract_failures", [])),
        "last_call_id": calls[-1].get("call_id") if calls else None,
        "max_lease_sequence": max((call.get("lease_sequence") or 0 for call in calls), default=0),
    }


def _usage_summary(client: object) -> dict | None:
    gate = getattr(client, "usage_gate", None)
    ledger = getattr(gate, "ledger", None)
    usage_run_id = getattr(gate, "usage_run_id", None)
    path = getattr(ledger, "path", None)
    if not usage_run_id or not isinstance(path, Path) or not path.exists():
        return None
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM usage_events WHERE usage_run_id=? ORDER BY event_id", (usage_run_id,))]
    finally:
        connection.close()
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["request_group_id"], []).append(row)
    pairing_valid = bool(rows) and all(
        group_id and len(group) == 2 and {row["request_kind"] for row in group} == {"count_tokens", "generate_content"}
        and len({row["usage_attempt_id"] for row in group}) == 1
        for group_id, group in groups.items()
    )
    return {
        "row_count": len(rows),
        "provider_requests": sum(row["provider_dispatched"] == 1 for row in rows),
        "count_token_requests": sum(row["request_kind"] == "count_tokens" for row in rows),
        "generation_requests": sum(row["request_kind"] == "generate_content" for row in rows),
        "success": sum(row["status"] == "SUCCEEDED" for row in rows),
        "failed": sum(row["status"] == "FAILED" for row in rows),
        "usage_unknown": sum(row["usage_metadata_status"] == "UNKNOWN" for row in rows),
        "input_tokens": sum(row["actual_input_tokens"] or 0 for row in rows),
        "candidate_tokens": sum(row["candidate_tokens"] or 0 for row in rows),
        "reasoning_tokens": sum(row["reasoning_tokens"] or 0 for row in rows),
        "combined_output_tokens": sum(row["combined_output_tokens"] or 0 for row in rows),
        "provider_total_tokens": sum(row["provider_total_tokens"] or 0 for row in rows),
        "pairing_valid": pairing_valid,
        "event_ids_unique": len(rows) == len({row["event_id"] for row in rows}),
        "ownership_valid": all(row["usage_run_id"] == usage_run_id and row["usage_attempt_id"] and row["request_group_id"] for row in rows),
    }


def run_prose_live_probe(source_episode: Path, output: Path, preflight: Path, client: object) -> dict:
    if output.exists():
        raise ProseProbeError("probe output already exists")
    source = validate_probe_source(source_episode)
    preflight_document, preflight_hash = _load_preflight(preflight)
    output.mkdir(parents=True)
    started_at = _utcnow()
    results: list[dict] = []
    terminal_error = None
    payloads = {
        "writer": {"context": source["context"], "plan": source["plan"]},
        "revision": {"context": source["context"], "plan": source["plan"], "draft": source["draft"], "draft_contract": source["draft_contract"], "decision": source["decision"]},
    }
    for stage in ("writer", "revision"):
        prompt = build_prompt(stage, "canonical", payloads[stage])
        try:
            text = client.generate(stage=stage, role="canonical", prompt=prompt)
        except Exception as error:
            terminal_error = {"stage": stage, "error_class": getattr(error, "error_class", error.__class__.__name__), "message": "sanitized provider failure"}
            break
        results.append(_stage_result(client, stage, prompt, text))
    telemetry = client.telemetry()
    usage = _usage_summary(client)
    calls = telemetry.get("calls", [])
    call_ids = [call.get("call_id") for call in calls]
    leases = [call.get("lease_sequence") for call in calls]
    duplicate_content = any(result["actual_content_response_count"] > 1 for result in results)
    identity_valid = (
        all(call_ids)
        and len(call_ids) == len(set(call_ids))
        and all(isinstance(value, int) for value in leases)
        and len(leases) == len(set(leases))
        and all(result.get("call_id") and result.get("key_slot") and isinstance(result.get("lease_sequence"), int) for result in results)
    )
    source_unchanged = source["hashes"] == {name: sha256_file(source_episode / name) for name in SOURCE_FILES}
    usage_valid = usage is None or (usage["pairing_valid"] and usage["event_ids_unique"] and usage["ownership_valid"])
    if duplicate_content or not identity_valid or not source_unchanged or not usage_valid:
        overall_status = "SAFETY_BLOCKED"
    elif terminal_error:
        overall_status = "INCOMPLETE"
    elif len(results) == 2 and all(result["actual_content_response_count"] == 1 and result["validation_verdict"] == "PASS" for result in results):
        overall_status = "PASS"
    else:
        overall_status = "NOT_PROVEN"
    _, safe_expansion = revision_expansion_guidance(source["draft_contract"]["character_count"])
    document = {
        "schema_version": 2,
        "model": getattr(getattr(client, "config", None), "model", preflight_document.get("model")),
        "source_episode_path": str(source_episode),
        "source_artifact_hashes": source["hashes"],
        "preflight_sha256": preflight_hash,
        "usage_run_id": getattr(getattr(client, "usage_gate", None), "usage_run_id", None),
        "source_draft_character_count": source["draft_contract"]["character_count"],
        "revision_safe_expansion": safe_expansion,
        "stages": results,
        "telemetry_checkpoint": _telemetry_checkpoint(telemetry),
        "usage": usage,
        "usage_identity_valid": usage_valid,
        "source_artifacts_unchanged": source_unchanged,
        "call_ids_unique": len(call_ids) == len(set(call_ids)),
        "lease_sequences_unique": len(leases) == len(set(leases)),
        "terminal_error": terminal_error,
        "started_at": started_at,
        "finished_at": _utcnow(),
        "overall_status": overall_status,
    }
    write_json(output / PROBE_ARTIFACT, document)
    return document


def prose_live_probe_status(output: Path) -> dict:
    manifest_path = output / PROBE_ARTIFACT
    if not manifest_path.is_file():
        raise ProseProbeError("probe artifact is missing")
    document = read_json(manifest_path)
    allowed = {PROBE_ARTIFACT, *PROBE_OPERATIONAL_FILES}
    unknown = {path.name for path in output.iterdir() if path.is_file()} - allowed
    if unknown:
        raise ProseProbeError(f"unknown probe artifact: {sorted(unknown)}")
    telemetry_path = output / PROBE_TELEMETRY
    if not telemetry_path.is_file():
        raise ProseProbeError("probe telemetry is missing")
    telemetry = read_json(telemetry_path)
    if document.get("telemetry_checkpoint") != _telemetry_checkpoint(telemetry):
        raise ProseProbeError("probe telemetry checkpoint mismatch")
    forbidden = ("raw_prompt", "raw_response", "provider_response", "api_key", "request_headers")
    def has_forbidden_key(value: object) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = key.lower().replace("-", "_") if isinstance(key, str) else str(key).lower().replace("-", "_")
                if normalized_key == "raw_response_sha256":
                    valid_digest = isinstance(item, str) and len(item) == 64 and all(character in "0123456789abcdef" for character in item)
                    if not valid_digest:
                        return True
                elif any(token in normalized_key for token in forbidden):
                    return True
                if has_forbidden_key(item):
                    return True
            return False
        if isinstance(value, list):
            return any(has_forbidden_key(item) for item in value)
        return False

    if has_forbidden_key({"manifest": document, "telemetry": telemetry}):
        raise ProseProbeError("forbidden probe data detected")
    return document
