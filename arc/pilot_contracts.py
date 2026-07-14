# 다섯 회차 pilot 입력과 전환 및 수용 판정을 검증한다.
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import ContractError, validate_fixture


PILOT_REVIEW_ROLES = ["readability", "character_consistency", "continuity", "rolling_plan_adaptation", "memory_correctness", "narrative_weight", "episode_to_episode_interest"]
STABLE_MEMORY_FIELDS = ("series_compass", "world_rules", "characters", "confirmed_facts", "relationship_state", "open_conflicts", "promises", "episode_summaries", "important_excerpts")
TRANSITION_SCHEMA_VERSION = 2
TRANSITION_CONTRACT_VERSION = 1
TRANSITION_ACTIONS = ("KEEP", "CHANGE", "DROP", "ADD")
ROLLING_PLAN_HORIZONS = ("immediate_horizon", "near_horizon")
ROLLING_PLAN_HORIZON_LIMITS = {"immediate_horizon": 4, "near_horizon": 8}
TRANSITION_EVIDENCE_FILES = ("episode_plan.json", "final.md", "memory_after.json", "memory_update.json")
TRANSITION_EXCERPT_MIN_CHARACTERS = 8
TRANSITION_EXCERPT_MAX_CHARACTERS = 400
TRANSITION_FORBIDDEN_MARKERS = ("synthetic transition toward", "synthetic pilot role", "Synthetic plan adapts")
TRANSITION_RESPONSE_FIELDS = ("next_episode", "rolling_plan_after", "adaptation_decisions", "continuity_satisfied", "continuity_deferred", "adaptation_summary", "evidence_refs")
EPISODE_IMPORTANCE_VALUES = ("ordinary", "major", "pivot")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_pilot_fixture(value: dict) -> dict:
    if set(value) != {"pilot_id", "episode_ids", "initial_source"} or not isinstance(value["pilot_id"], str) or not value["pilot_id"]:
        raise ContractError("invalid pilot fixture")
    episode_ids = value["episode_ids"]
    if not isinstance(episode_ids, list) or len(episode_ids) != 5 or len(episode_ids) != len(set(episode_ids)) or any(not isinstance(item, str) or not item for item in episode_ids):
        raise ContractError("pilot requires five unique episode IDs")
    validate_fixture(value["initial_source"])
    if value["initial_source"]["current_episode"]["episode_id"] != episode_ids[0]:
        raise ContractError("initial episode ID does not match pilot")
    return value


def rolling_plan_hash(plan: dict) -> str:
    return hashlib.sha256(canonical_bytes(plan)).hexdigest()


def validate_rolling_plan(plan: object, *, require_immediate: bool, code: str = "TRANSITION_PLAN_INVALID") -> dict:
    if not isinstance(plan, dict) or set(plan) != set(ROLLING_PLAN_HORIZONS):
        raise ContractError("rolling plan must have exactly immediate_horizon and near_horizon", code)
    for horizon in ROLLING_PLAN_HORIZONS:
        items = plan[horizon]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ContractError("rolling plan items must be non-blank strings", code)
        if len(items) > ROLLING_PLAN_HORIZON_LIMITS[horizon]:
            raise ContractError(f"rolling plan {horizon} exceeds {ROLLING_PLAN_HORIZON_LIMITS[horizon]} items", code)
    combined = plan["immediate_horizon"] + plan["near_horizon"]
    if len(combined) != len(set(combined)):
        raise ContractError("rolling plan items must be unique across horizons", code)
    if require_immediate and not plan["immediate_horizon"]:
        raise ContractError("adapted immediate horizon requires at least one item", code)
    return plan


def validate_adaptation_decisions(decisions: object, plan_before: dict, plan_after: dict) -> dict[str, int]:
    """Enforce complete before/after accounting and return per-action counts."""
    code = "TRANSITION_DECISION_ACCOUNTING_INVALID"
    if not isinstance(decisions, list) or not decisions:
        raise ContractError("adaptation decisions are required", code)
    required = {"action", "horizon_before", "item_before", "horizon_after", "item_after", "reason", "evidence"}
    before_sequence = [(horizon, item) for horizon in ROLLING_PLAN_HORIZONS for item in plan_before[horizon]]
    before_items = {item for _, item in before_sequence}
    consumed: list[tuple[str, str]] = []
    rebuilt: dict[str, list[str]] = {horizon: [] for horizon in ROLLING_PLAN_HORIZONS}
    counts = {action: 0 for action in TRANSITION_ACTIONS}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != required or decision["action"] not in TRANSITION_ACTIONS:
            raise ContractError("invalid adaptation decision shape", code)
        if not isinstance(decision["reason"], str) or not decision["reason"].strip():
            raise ContractError("adaptation decision requires a reason", code)
        action = decision["action"]
        counts[action] += 1
        if action == "ADD":
            if decision["horizon_before"] is not None or decision["item_before"] is not None:
                raise ContractError("ADD cannot reference a before item", code)
            if decision["item_after"] in before_items:
                raise ContractError("ADD item already exists in the before plan", code)
        else:
            if (decision["horizon_before"], decision["item_before"]) not in before_sequence:
                raise ContractError("decision consumes an unknown before item", code)
            consumed.append((decision["horizon_before"], decision["item_before"]))
        if action == "DROP":
            if decision["horizon_after"] is not None or decision["item_after"] is not None:
                raise ContractError("DROP cannot produce an after item", code)
            continue
        if decision["horizon_after"] not in ROLLING_PLAN_HORIZONS or not isinstance(decision["item_after"], str) or not decision["item_after"].strip():
            raise ContractError("decision after target is invalid", code)
        if action == "KEEP" and decision["item_after"] != decision["item_before"]:
            raise ContractError("KEEP cannot change the item text", code)
        if action == "CHANGE" and decision["item_after"] == decision["item_before"]:
            raise ContractError("CHANGE requires a different item text", code)
        rebuilt[decision["horizon_after"]].append(decision["item_after"])
    if len(consumed) != len(set(consumed)):
        raise ContractError("before plan item consumed by two decisions", code)
    if consumed != before_sequence:
        if set(consumed) != set(before_sequence):
            raise ContractError("before plan items are not fully accounted", code)
        raise ContractError("decisions must consume the before plan in canonical order", code)
    if rebuilt != {horizon: list(plan_after[horizon]) for horizon in ROLLING_PLAN_HORIZONS}:
        raise ContractError("decisions do not reconstruct the adapted plan", code)
    return counts


def validate_transition_evidence(decisions: list[dict], evidence_refs: object, completed_episode_id: str, run_dir: Path) -> None:
    code = "TRANSITION_EVIDENCE_INVALID"
    allowed = {f"episodes/{completed_episode_id}/{name}" for name in TRANSITION_EVIDENCE_FILES}
    texts: dict[str, str] = {}
    seen: set[str] = set()
    for decision in decisions:
        evidence = decision["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ContractError("adaptation decision requires evidence", code)
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"ref", "excerpt"}:
                raise ContractError("invalid evidence item shape", code)
            ref, excerpt = item["ref"], item["excerpt"]
            if ref not in allowed:
                raise ContractError("evidence ref outside the completed episode artifacts", code)
            if not isinstance(excerpt, str) or not excerpt.strip() or not TRANSITION_EXCERPT_MIN_CHARACTERS <= len(excerpt) <= TRANSITION_EXCERPT_MAX_CHARACTERS:
                raise ContractError("evidence excerpt length is invalid", code)
            if ref not in texts:
                path = run_dir / ref
                if not path.exists():
                    raise ContractError("evidence artifact does not exist", code)
                texts[ref] = path.read_text(encoding="utf-8")
            if excerpt not in texts[ref]:
                raise ContractError("evidence excerpt not found in the artifact", code)
            seen.add(ref)
    if not isinstance(evidence_refs, list) or evidence_refs != sorted(seen):
        raise ContractError("evidence refs must equal the sorted unique decision refs", code)


def validate_transition_response(value: object) -> dict:
    if not isinstance(value, dict):
        raise ContractError("transition response must be a JSON object", "TRANSITION_RESPONSE_NOT_OBJECT")
    if set(value) != set(TRANSITION_RESPONSE_FIELDS):
        raise ContractError("transition response fields mismatch", "TRANSITION_FIELDS_MISMATCH")
    return value


def transition_action_counts(value: dict) -> dict[str, int]:
    counts = {action: 0 for action in TRANSITION_ACTIONS}
    for decision in value.get("adaptation_decisions", []):
        if isinstance(decision, dict) and decision.get("action") in counts:
            counts[decision["action"]] += 1
    return counts


def validate_transition(value: dict, source: dict, next_episode_id: str, run_dir: Path) -> dict:
    if not isinstance(value, dict):
        raise ContractError("transition must be a JSON object", "TRANSITION_RESPONSE_NOT_OBJECT")
    if value.get("schema_version") == 1:
        raise ContractError("LEGACY_SYNTHETIC_TRANSITION", "LEGACY_SYNTHETIC_TRANSITION")
    required = {"schema_version", "completed_episode_id", "next_episode_id", "transition_input_hash", "next_source_hash", "next_episode", "rolling_plan_before_hash", "rolling_plan_after", "adaptation_decisions", "continuity_satisfied", "continuity_deferred", "adaptation_summary", "evidence_refs"}
    completed_episode_id = source["current_episode"]["episode_id"]
    if set(value) != required or value["schema_version"] != TRANSITION_SCHEMA_VERSION or not isinstance(value["transition_input_hash"], str) or not isinstance(value["next_source_hash"], str) or value["completed_episode_id"] != completed_episode_id or value["next_episode_id"] != next_episode_id:
        raise ContractError("invalid pilot transition identity", "TRANSITION_FIELDS_MISMATCH")
    if any(marker in json.dumps(value, ensure_ascii=False) for marker in TRANSITION_FORBIDDEN_MARKERS):
        raise ContractError("forbidden synthetic transition marker", "TRANSITION_SYNTHETIC_MARKER")
    if not isinstance(value["adaptation_summary"], str) or not value["adaptation_summary"].strip():
        raise ContractError("invalid adaptation summary", "TRANSITION_FIELDS_MISMATCH")
    plan_before = validate_rolling_plan(source["rolling_plan"], require_immediate=False)
    if value["rolling_plan_before_hash"] != rolling_plan_hash(plan_before):
        raise ContractError("rolling plan before hash mismatch", "TRANSITION_FIELDS_MISMATCH")
    plan_after = validate_rolling_plan(value["rolling_plan_after"], require_immediate=True)
    validate_adaptation_decisions(value["adaptation_decisions"], plan_before, plan_after)
    validate_transition_evidence(value["adaptation_decisions"], value["evidence_refs"], completed_episode_id, Path(run_dir))
    next_episode = value["next_episode"]
    if not isinstance(next_episode, dict) or set(next_episode) != {"episode_id", "importance", "required_role"} or next_episode["episode_id"] != next_episode_id or next_episode["importance"] not in EPISODE_IMPORTANCE_VALUES:
        raise ContractError("invalid transition next episode", "TRANSITION_NEXT_ROLE_INVALID")
    if not isinstance(next_episode["required_role"], str) or next_episode["required_role"] != plan_after["immediate_horizon"][0]:
        raise ContractError("next episode role must be the first adapted immediate item", "TRANSITION_NEXT_ROLE_INVALID")
    expected = source["required_next_episode_continuity"]
    satisfied, deferred = value["continuity_satisfied"], value["continuity_deferred"]
    if not all(isinstance(items, list) and len(items) == len(set(items)) for items in (satisfied, deferred)) or set(satisfied) & set(deferred) or set(satisfied) | set(deferred) != set(expected):
        raise ContractError("invalid continuity partition", "TRANSITION_CONTINUITY_INVALID")
    return value


def validate_pilot_acceptance(value: dict, evidence_refs: list[str]) -> dict:
    required = {"verdict", "dimension_results", "critical_findings", "strengths_to_preserve", "evidence_refs"}
    if set(value) != required or value["verdict"] not in {"PASS", "HOLD"} or set(value["dimension_results"]) != set(PILOT_REVIEW_ROLES):
        raise ContractError("invalid pilot acceptance")
    dimensions = value["dimension_results"]
    if any(result not in {"PASS", "HOLD"} for result in dimensions.values()) or not isinstance(value["critical_findings"], list) or len(value["critical_findings"]) > 7 or len(value["critical_findings"]) != len(set(value["critical_findings"])):
        raise ContractError("invalid pilot acceptance findings")
    if value["verdict"] == "PASS" and (any(result != "PASS" for result in dimensions.values()) or value["critical_findings"]):
        raise ContractError("pilot PASS has unresolved findings")
    if value["verdict"] == "HOLD" and (all(result == "PASS" for result in dimensions.values()) or not value["critical_findings"]):
        raise ContractError("pilot HOLD lacks critical finding")
    if not isinstance(value["evidence_refs"], list) or not value["evidence_refs"] or set(value["evidence_refs"]) - set(evidence_refs):
        raise ContractError("invalid pilot acceptance evidence")
    return value
