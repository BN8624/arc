# 다섯 회차 pilot 입력과 전환 및 수용 판정을 검증한다.
from __future__ import annotations

import json

from .contracts import ContractError, validate_fixture


PILOT_REVIEW_ROLES = ["readability", "character_consistency", "continuity", "rolling_plan_adaptation", "memory_correctness", "narrative_weight", "episode_to_episode_interest"]
STABLE_MEMORY_FIELDS = ("series_compass", "world_rules", "characters", "confirmed_facts", "relationship_state", "open_conflicts", "promises", "episode_summaries", "important_excerpts")


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


def validate_transition(value: dict, source: dict, next_episode_id: str, root: str) -> dict:
    required = {"schema_version", "completed_episode_id", "next_episode_id", "transition_input_hash", "next_source_hash", "next_episode", "rolling_plan_after", "continuity_satisfied", "continuity_deferred", "adaptation_summary", "evidence_refs"}
    if set(value) != required or value["schema_version"] != 1 or not isinstance(value["transition_input_hash"], str) or not isinstance(value["next_source_hash"], str) or value["completed_episode_id"] != source["current_episode"]["episode_id"] or value["next_episode_id"] != next_episode_id or value["next_episode"].get("episode_id") != next_episode_id:
        raise ContractError("invalid pilot transition identity")
    if not isinstance(value["rolling_plan_after"], dict) or not value["rolling_plan_after"] or not isinstance(value["adaptation_summary"], str) or not value["adaptation_summary"]:
        raise ContractError("invalid pilot transition plan")
    expected = source["required_next_episode_continuity"]
    satisfied, deferred = value["continuity_satisfied"], value["continuity_deferred"]
    if not all(isinstance(items, list) and len(items) == len(set(items)) for items in (satisfied, deferred)) or set(satisfied) & set(deferred) or set(satisfied) | set(deferred) != set(expected):
        raise ContractError("invalid continuity partition")
    allowed_prefix = f"episodes/{source['current_episode']['episode_id']}/"
    allowed = {allowed_prefix + name for name in ("final.md", "memory_update.json", "memory_after.json", "episode_plan.json")}
    if not isinstance(value["evidence_refs"], list) or not value["evidence_refs"] or set(value["evidence_refs"]) - allowed:
        raise ContractError("invalid transition evidence")
    if any(ref.startswith(("/", ".", "..")) or root in ref for ref in value["evidence_refs"]):
        raise ContractError("invalid transition evidence path")
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
