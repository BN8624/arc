from __future__ import annotations
# Prose generation profile을 bounded cycle artifact로 검증한다.

import hashlib
import json
import subprocess
from statistics import median
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import PROSE_GENERATION_PROFILE_VERSION, PROSE_MAX_CHARACTERS, PROSE_MIN_CHARACTERS, PROSE_PROVIDER_CONTRACT_VERSION, ContractError, materialize_prose_provider_response, validate_draft_prose, validate_prose
from .prompts import build_prompt
from .storage import read_json, sha256_bytes, sha256_file, write_json


CALIBRATION_SCHEMA_VERSION = 3
CALIBRATION_REQUIRED = "PROSE_CALIBRATION_REQUIRED"
CALIBRATION_INVALID = "PROSE_CALIBRATION_INVALID"
CALIBRATION_STALE = "PROSE_CALIBRATION_STALE"
CALIBRATION_PROFILE_MISMATCH = "PROSE_CALIBRATION_PROFILE_MISMATCH"
PROFILE_IDS = ("profile_v2_high", "profile_v2_minimal")


class ProseCalibrationError(RuntimeError):
    pass


def _digest(value: object) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def prompt_templates_hash() -> str:
    probe_identity = {"context": {}, "plan": {}, "profile_id": "template-hash-probe", "prose_thinking_level": "high", "generation_profile_version": PROSE_GENERATION_PROFILE_VERSION}
    writer_probe = build_prompt("writer", "canonical", probe_identity)
    revision_probe = build_prompt("revision", "canonical", {**probe_identity, "draft": "A" * 3500, "draft_contract": {"character_count": 3500, "verdict": "REVISE_REQUIRED"}, "decision": {"verdict": "REVISE_ONCE"}})
    return _digest({"writer": "writer.v2", "revision": "revision.v2", "writer_probe": writer_probe, "revision_probe": revision_probe})


def quota_config_snapshot(config: object) -> dict:
    mapping = getattr(config, "quota_project_buckets", None) or {}
    return {"active": {"rpm": getattr(config, "provider_rpm_limit", 30), "input_tpm": getattr(config, "provider_input_tpm_limit", 16000), "rpd": getattr(config, "provider_rpd_limit", 14400)}, "safety": {"rpm": getattr(config, "provider_rpm_safety_limit", 27), "input_tpm": getattr(config, "provider_input_tpm_safety_limit", 14000), "rpd": getattr(config, "provider_rpd_safety_limit", 13000)}, "max_input_tokens_per_request": getattr(config, "max_input_tokens_per_request", 14000), "project_buckets": dict(mapping)}


def _profile_id(level: str) -> str:
    if level not in {"high", "minimal"}:
        raise ProseCalibrationError(CALIBRATION_INVALID)
    return f"profile_v2_{level}"


def profile_config(profile_id: str, *, model: str = "gemma-4-31b-it", prose_limit: int = 32768) -> dict:
    if profile_id not in PROFILE_IDS:
        raise ProseCalibrationError(CALIBRATION_INVALID)
    level = profile_id.rsplit("_", 1)[-1]
    return {
        "profile_id": profile_id,
        "generation_profile_version": PROSE_GENERATION_PROFILE_VERSION,
        "provider_contract_version": PROSE_PROVIDER_CONTRACT_VERSION,
        "model": model,
        "thinking_level": level,
        "target_band": [6000, 6800],
        "hard_band": [PROSE_MIN_CHARACTERS, PROSE_MAX_CHARACTERS],
        "prose_max_output_tokens": prose_limit,
        "writer_prompt_template": "writer.v2",
        "revision_prompt_template": "revision.v2",
        "prompt_templates_hash": prompt_templates_hash(),
        "revision_expansion_rule": "safe_final_floor=5200; minimum_meaningful_growth=max(1800,safe_final_floor-current_draft_character_count)",
    }


def deterministic_challenge_draft(profile_id: str, cycle: int) -> str:
    if not 1 <= cycle <= 3:
        raise ProseCalibrationError(CALIBRATION_INVALID)
    seed = f"{profile_id}:cycle:{cycle}:challenge-v2"
    sentence = f"Calibration challenge {seed} preserves a distinct deterministic context and event identity. "
    return (sentence * ((3200 // len(sentence)) + 1))[:3200]


def _telemetry(client: object) -> dict:
    return client.telemetry() if hasattr(client, "telemetry") else {"calls": [], "contract_failures": []}


def _matching_call(telemetry: dict, stage: str, prompt_hash: str) -> dict:
    calls = [call for call in telemetry.get("calls", []) if call.get("stage") == stage and call.get("status") == "PASS" and call.get("input_hash") == prompt_hash]
    if len(calls) == 1:
        return calls[0]
    return {}


def _receipt(profile_id: str, cycle: int, stage: str, prompt: str, raw: str, client: object) -> dict:
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    receipt = {
        "schema_version": 1,
        "stage": stage,
        "cycle_id": f"cycle_{cycle:02d}",
        "profile_id": profile_id,
        "input_hash": prompt_hash,
        "state": "RECEIVED",
        "raw_response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "raw_response": raw,
        "materialized_prose_sha256": None,
        "character_count": None,
        "contract_code": None,
        "call_id": None,
        "lease_sequence": None,
        "provider_contract_version": PROSE_PROVIDER_CONTRACT_VERSION,
        "generation_profile_version": PROSE_GENERATION_PROFILE_VERSION,
    }
    try:
        text = materialize_prose_provider_response(raw, stage=stage)
        receipt["materialized_prose_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        receipt["character_count"] = len(text)
        if stage == "writer":
            _, contract = validate_draft_prose(text)
            receipt["verdict"] = contract["verdict"]
            receipt["contract_code"] = contract["contract_code"]
        else:
            validate_prose(text)
            receipt["verdict"] = "PASS"
    except ContractError as error:
        receipt["state"] = "REJECTED"
        receipt["contract_code"] = error.contract_code
        receipt["character_count"] = getattr(error, "character_count", receipt["character_count"])
        receipt["verdict"] = "FAIL"
    telemetry = _telemetry(client)
    call = _matching_call(telemetry, stage, prompt_hash)
    if call:
        receipt.update({"call_id": call.get("call_id"), "lease_sequence": call.get("lease_sequence"), "reasoning_tokens": call.get("reasoning_tokens", 0) or 0, "quota_admission_evidence": call.get("quota_admission_evidence", {"mode": "offline"})})
        if call.get("response_sha256") not in {None, receipt["raw_response_sha256"]}:
            receipt["state"], receipt["verdict"], receipt["contract_code"] = "REJECTED", "FAIL", "CALIBRATION_TELEMETRY_HASH_MISMATCH"
    else:
        receipt["quota_admission_evidence"] = {"mode": "offline"} if not telemetry.get("calls") else {"mode": "invalid", "reason": "CALIBRATION_TELEMETRY_INPUT_HASH_MISMATCH"}
        if telemetry.get("calls"):
            receipt["state"], receipt["verdict"], receipt["contract_code"] = "REJECTED", "FAIL", "CALIBRATION_TELEMETRY_INPUT_HASH_MISMATCH"
    return receipt


def _cycle(profile_id: str, cycle: int, client: object, context: dict) -> dict:
    identity = {"profile_id": profile_id, "cycle": cycle, "context": context}
    level = profile_id.rsplit("_", 1)[-1]
    if hasattr(client, "set_prose_thinking_level"):
        client.set_prose_thinking_level(level)
    writer_prompt = build_prompt("writer", "canonical", {"context": context, "plan": identity, "profile_id": profile_id, "prose_thinking_level": level, "generation_profile_version": PROSE_GENERATION_PROFILE_VERSION})
    raw_writer = client.generate(stage="writer", role="canonical", prompt=writer_prompt)
    writer = _receipt(profile_id, cycle, "writer", writer_prompt, raw_writer, client)
    if writer.get("verdict") == "FAIL":
        return {"cycle_id": f"cycle_{cycle:02d}", "writer": writer, "revision": None, "verdict": "FAIL", "content_response_count": 1}
    writer_text = materialize_prose_provider_response(raw_writer, stage="writer")
    draft = writer_text if writer.get("verdict") == "REVISE_REQUIRED" else deterministic_challenge_draft(profile_id, cycle)
    draft_contract = {"verdict": "REVISE_REQUIRED", "contract_code": "PROSE_UNDERLENGTH_REPAIRABLE", "character_count": len(draft)}
    revision_prompt = build_prompt("revision", "canonical", {"context": context, "plan": identity, "draft": draft, "draft_contract": draft_contract, "decision": {"verdict": "REVISE_ONCE"}, "profile_id": profile_id, "prose_thinking_level": level, "generation_profile_version": PROSE_GENERATION_PROFILE_VERSION})
    raw_revision = client.generate(stage="revision", role="canonical", prompt=revision_prompt)
    revision = _receipt(profile_id, cycle, "revision", revision_prompt, raw_revision, client)
    return {"cycle_id": f"cycle_{cycle:02d}", "writer": writer, "revision": revision, "verdict": "PASS" if revision.get("verdict") == "PASS" else "FAIL", "content_response_count": 2, "revision_input_hash": hashlib.sha256(draft.encode()).hexdigest(), "challenge": writer.get("verdict") == "PASS"}


def _aggregate(document: dict) -> dict:
    profiles = document.get("profiles", {})
    for profile_id, profile in profiles.items():
        cycles = profile.get("cycles", [])
        config = profile.get("config", {})
        if config.get("prompt_templates_hash") != document.get("prompt_templates_hash") or config.get("provider_contract_version") != document.get("provider_contract_version") or config.get("generation_profile_version") != document.get("generation_profile_version"):
            raise ProseCalibrationError(CALIBRATION_INVALID)
        reasoning = [receipt.get("reasoning_tokens", 0) or 0 for cycle in cycles for receipt in (cycle.get("writer") or {}, cycle.get("revision") or {}) if receipt]
        final_counts = [cycle.get("revision", {}).get("character_count") for cycle in cycles if cycle.get("revision", {}).get("character_count") is not None]
        profile["reasoning_tokens"] = sum(reasoning)
        profile["median_final_character_count"] = median(final_counts) if final_counts else None
        profile["median_distance_to_target"] = abs(profile["median_final_character_count"] - 6400) if final_counts else 10**9
        profile["pass_count"] = sum(cycle.get("verdict") == "PASS" for cycle in cycles)
        profile["verdict"] = "PASS" if len(cycles) == 3 and profile["pass_count"] == 3 and all(cycle.get("content_response_count") == 2 for cycle in cycles) else "FAIL"
    passed = [profile_id for profile_id, profile in profiles.items() if profile.get("verdict") == "PASS"]
    if not passed:
        document["overall_status"] = "PHASE_3_PROSE_CALIBRATION_NOT_PROVEN"
        document["selected_profile"] = None
        document["selection_reason"] = None
    else:
        selected = min(passed, key=lambda item: (profiles[item].get("reasoning_tokens", 0), profiles[item].get("median_distance_to_target", 10**9), 0 if item.endswith("minimal") else 1))
        document["overall_status"] = "PASS"
        document["selected_profile"] = selected
        document["selection_reason"] = "reasoning_tokens_then_target_center_then_minimal"
    if document.get("selected_profile"):
        selected = document["selected_profile"]
        document["selected_profile_hash"] = _digest(document["profiles"][selected].get("config"))
    return document


def run_prose_calibration(output: Path, preflight: Path, profiles: list[str], cycles: int, client: object) -> dict:
    if output.exists():
        raise ProseCalibrationError("calibration output already exists")
    if cycles != 3 or set(profiles) != set(PROFILE_IDS):
        raise ProseCalibrationError(CALIBRATION_INVALID)
    preflight_doc = read_json(preflight)
    if preflight_doc.get("status") not in {"PASS", "DEGRADED_PASS"} or preflight_doc.get("live_run_allowed") is not True:
        raise ProseCalibrationError(CALIBRATION_REQUIRED)
    output.mkdir(parents=True)
    try:
        execution_head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        execution_head = None
    root = {"schema_version": CALIBRATION_SCHEMA_VERSION, "provider_contract_version": PROSE_PROVIDER_CONTRACT_VERSION, "generation_profile_version": PROSE_GENERATION_PROFILE_VERSION, "model": getattr(getattr(client, "config", None), "model", preflight_doc.get("model")), "prompt_templates_hash": prompt_templates_hash(), "quota_config_snapshot": quota_config_snapshot(getattr(client, "config", object())), "source_artifact_hashes": {}, "preflight_sha256": sha256_file(preflight), "execution_head": execution_head, "created_at": datetime.now(timezone.utc).isoformat(), "profiles": {}, "selected_profile": None, "selected_profile_hash": None, "selection_reason": None, "overall_status": "RUNNING"}
    write_json(output / "prose_calibration.json", root)
    all_calls = {"schema_version": 1, "calls": [], "contract_failures": []}
    for profile_id in profiles:
        config = profile_config(profile_id, model=root["model"], prose_limit=getattr(getattr(client, "config", None), "prose_limit", 32768))
        profile = {"profile_id": profile_id, "config": config, "profile_hash": _digest(config), "cycles": [], "verdict": "FAIL", "pass_count": 0}
        for cycle in range(1, cycles + 1):
            cycle_dir = output / "cycles" / profile_id / f"cycle_{cycle:02d}"
            cycle_dir.mkdir(parents=True)
            result = _cycle(profile_id, cycle, client, {"calibration": "synthetic", "cycle": cycle})
            write_json(cycle_dir / "writer.response.json", result["writer"])
            if result.get("revision") is not None:
                write_json(cycle_dir / "revision.response.json", result["revision"])
            profile["cycles"].append(result)
        root["profiles"][profile_id] = profile
    root = _aggregate(root)
    write_json(output / "prose_calibration.json", root)
    write_json(output / "prose_calibration_calls.json", _telemetry(client))
    write_json(output / "routing_state.json", {"schema_version": 1, "profile_count": len(profiles)})
    return root


def _recompute(root: dict, output: Path) -> dict:
    expected = {"prose_calibration.json", "prose_calibration_calls.json", "routing_state.json"}
    for profile_id in PROFILE_IDS:
        for cycle in range(1, 4):
            expected.update({f"cycles/{profile_id}/cycle_{cycle:02d}/writer.response.json", f"cycles/{profile_id}/cycle_{cycle:02d}/revision.response.json"})
    actual = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()}
    if actual - expected:
        raise ProseCalibrationError(CALIBRATION_INVALID)
    recomputed = json.loads(json.dumps(root))
    telemetry = read_json(output / "prose_calibration_calls.json")
    profiles = recomputed.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_IDS):
        raise ProseCalibrationError(CALIBRATION_INVALID)
    for profile_id, profile in recomputed.get("profiles", {}).items():
        if profile_id not in PROFILE_IDS or profile.get("profile_hash") != _digest(profile.get("config")):
            raise ProseCalibrationError(CALIBRATION_INVALID)
        if not isinstance(profile.get("cycles"), list) or len(profile["cycles"]) != 3:
            raise ProseCalibrationError(CALIBRATION_INVALID)
        cycles = []
        for cycle in range(1, 4):
            cycle_dir = output / "cycles" / profile_id / f"cycle_{cycle:02d}"
            writer = read_json(cycle_dir / "writer.response.json")
            revision_path = cycle_dir / "revision.response.json"
            revision = read_json(revision_path) if revision_path.is_file() else {"stage": "revision", "profile_id": profile_id, "cycle_id": f"cycle_{cycle:02d}", "verdict": "FAIL", "contract_code": "WRITER_REJECTED"}
            for receipt, stage in ((writer, "writer"), (revision, "revision")):
                if stage == "revision" and not revision_path.is_file():
                    continue
                if receipt.get("stage") != stage or receipt.get("profile_id") != profile_id or receipt.get("cycle_id") != f"cycle_{cycle:02d}":
                    raise ProseCalibrationError(CALIBRATION_INVALID)
                raw = receipt.get("raw_response")
                if not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != receipt.get("raw_response_sha256"):
                    raise ProseCalibrationError(CALIBRATION_INVALID)
                if receipt.get("call_id") is not None:
                    calls = [call for call in telemetry.get("calls", []) if call.get("call_id") == receipt.get("call_id") and call.get("stage") == stage]
                    if len(calls) != 1 or calls[0].get("lease_sequence") != receipt.get("lease_sequence") or calls[0].get("response_sha256") not in {None, receipt.get("raw_response_sha256")}:
                        raise ProseCalibrationError(CALIBRATION_INVALID)
                try:
                    text = materialize_prose_provider_response(raw, stage=stage)
                except ContractError as error:
                    if receipt.get("contract_code") != error.contract_code:
                        raise ProseCalibrationError(CALIBRATION_INVALID)
                    if receipt.get("verdict") != "FAIL":
                        raise ProseCalibrationError(CALIBRATION_INVALID)
                else:
                    if receipt.get("materialized_prose_sha256") != hashlib.sha256(text.encode()).hexdigest() or receipt.get("character_count") != len(text):
                        raise ProseCalibrationError(CALIBRATION_INVALID)
                    try:
                        if stage == "writer":
                            _, contract = validate_draft_prose(text)
                            expected_verdict, expected_code = contract["verdict"], contract["contract_code"]
                        else:
                            validate_prose(text)
                            expected_verdict, expected_code = "PASS", None
                    except ContractError as error:
                        expected_verdict, expected_code = "FAIL", error.contract_code
                    if receipt.get("verdict") != expected_verdict or receipt.get("contract_code") != expected_code:
                        raise ProseCalibrationError(CALIBRATION_INVALID)
                calls = [call for call in telemetry.get("calls", []) if call.get("call_id") == receipt.get("call_id") and call.get("stage") == stage and call.get("input_hash") == receipt.get("input_hash")]
                if len(calls) != 1:
                    raise ProseCalibrationError(CALIBRATION_INVALID)
                if (calls[0].get("reasoning_tokens") or 0) != (receipt.get("reasoning_tokens") or 0):
                    raise ProseCalibrationError(CALIBRATION_INVALID)
                if calls[0].get("quota_admission_evidence", {"mode": "offline"}) != receipt.get("quota_admission_evidence", {"mode": "offline"}):
                    raise ProseCalibrationError(CALIBRATION_INVALID)
            original = profile.get("cycles", [])[cycle - 1] if len(profile.get("cycles", [])) >= cycle else {}
            cycles.append({**original, "cycle_id": f"cycle_{cycle:02d}", "writer": writer, "revision": revision, "verdict": "PASS" if revision.get("verdict") == "PASS" and writer.get("verdict") in {"PASS", "REVISE_REQUIRED"} else "FAIL", "content_response_count": 2})
        profile["cycles"] = cycles
    return _aggregate(recomputed)


def prose_calibration_status(output: Path) -> dict:
    path = output / "prose_calibration.json"
    if not path.is_file():
        raise ProseCalibrationError(CALIBRATION_REQUIRED)
    root = read_json(path)
    current = _recompute(root, output)
    if current != root:
        raise ProseCalibrationError(CALIBRATION_INVALID)
    return root


def validate_pilot_calibration(path: Path, *, model: str, prompt_profile_version: int = PROSE_GENERATION_PROFILE_VERSION, output_token_limit: int | None = None, current_head: str | None = None, prompt_templates_hash_value: str | None = None, quota_config_snapshot_value: dict | None = None, provider_contract_version: int = PROSE_PROVIDER_CONTRACT_VERSION) -> dict:
    if not path.is_file():
        raise ProseCalibrationError(CALIBRATION_REQUIRED)
    try:
        if path.resolve() != (path.parent / "prose_calibration.json").resolve():
            raise ProseCalibrationError(CALIBRATION_INVALID)
    except OSError as error:
        raise ProseCalibrationError(CALIBRATION_INVALID) from error
    try:
        root = prose_calibration_status(path.parent)
    except ProseCalibrationError as error:
        raise ProseCalibrationError(CALIBRATION_INVALID) from error
    if root.get("overall_status") != "PASS" or not root.get("selected_profile"):
        raise ProseCalibrationError(CALIBRATION_INVALID)
    selected = root["profiles"].get(root["selected_profile"], {})
    if root.get("selected_profile_hash") != _digest(selected.get("config")):
        raise ProseCalibrationError(CALIBRATION_INVALID)
    if selected.get("config", {}).get("prompt_templates_hash") != root.get("prompt_templates_hash") or selected.get("config", {}).get("provider_contract_version") != root.get("provider_contract_version"):
        raise ProseCalibrationError(CALIBRATION_PROFILE_MISMATCH)
    if root.get("model") != model or root.get("generation_profile_version") != prompt_profile_version:
        raise ProseCalibrationError(CALIBRATION_PROFILE_MISMATCH)
    if root.get("provider_contract_version") != provider_contract_version:
        raise ProseCalibrationError(CALIBRATION_PROFILE_MISMATCH)
    if prompt_templates_hash_value is not None and root.get("prompt_templates_hash") != prompt_templates_hash_value:
        raise ProseCalibrationError(CALIBRATION_PROFILE_MISMATCH)
    if quota_config_snapshot_value is not None and root.get("quota_config_snapshot") != quota_config_snapshot_value:
        raise ProseCalibrationError(CALIBRATION_PROFILE_MISMATCH)
    if output_token_limit is not None and selected.get("config", {}).get("prose_max_output_tokens") != output_token_limit:
        raise ProseCalibrationError(CALIBRATION_PROFILE_MISMATCH)
    created = datetime.fromisoformat(root["created_at"])
    age = datetime.now(timezone.utc) - created
    if age < timedelta(0) or age > timedelta(hours=24):
        raise ProseCalibrationError(CALIBRATION_STALE)
    if root.get("execution_head") is None:
        raise ProseCalibrationError(CALIBRATION_STALE)
    if current_head is not None and root.get("execution_head") != current_head:
        raise ProseCalibrationError(CALIBRATION_STALE)
    return root
