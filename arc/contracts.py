# Phase 1 입력과 모델 응답 계약을 검증한다.
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Protocol


class ContractError(ValueError):
    """A mock Phase 1 contract was violated."""


class ModelClient(Protocol):
    def generate(self, *, stage: str, role: str, prompt: str) -> str: ...


REQUIRED_FIXTURE_KEYS = {
    "fixture_id", "series_compass", "world_rules", "characters", "confirmed_facts",
    "relationship_state", "open_conflicts", "episode_summaries", "important_excerpts",
    "promises", "required_next_episode_continuity", "rolling_plan", "current_episode",
}


def parse_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ContractError("malformed JSON response") from error
    if not isinstance(value, dict):
        raise ContractError("response must be a JSON object")
    return value


def validate_fixture(source: dict) -> None:
    missing = REQUIRED_FIXTURE_KEYS - source.keys()
    if missing:
        raise ContractError(f"fixture missing: {sorted(missing)}")
    if not source["fixture_id"] or not source["current_episode"].get("episode_id"):
        raise ContractError("fixture identity is required")
    list_fields = REQUIRED_FIXTURE_KEYS & {"world_rules", "characters", "confirmed_facts", "relationship_state", "open_conflicts", "promises", "episode_summaries", "important_excerpts", "required_next_episode_continuity"}
    if any(not isinstance(source[field], list) for field in list_fields):
        raise ContractError("persistent collections must be lists")
    if not isinstance(source["rolling_plan"], dict):
        raise ContractError("rolling plan must be an object")


def validate_worker(value: dict, worker_id: str, role: str) -> dict:
    allowed = {"worker_id", "role", "verdict", "primary_finding", "primary_risk", "evidence_refs", "proposal"}
    if set(value) - allowed or set(value) != allowed:
        raise ContractError("worker fields do not match contract")
    if value["worker_id"] != worker_id or value["role"] != role:
        raise ContractError("worker identity mismatch")
    if not all(isinstance(value[key], str) and value[key] for key in ("verdict", "primary_finding", "primary_risk")):
        raise ContractError("worker finding and risk are required")
    if not isinstance(value["evidence_refs"], list) or not value["evidence_refs"]:
        raise ContractError("worker evidence is required")
    if not isinstance(value["proposal"], dict):
        raise ContractError("worker proposal must be object")
    return value


def validate_plan(value: dict, episode_id: str) -> dict:
    required = {"episode_id", "immediate_objective", "obstacle", "protagonist_action", "meaningful_change", "episode_ending", "selected_worker_ids", "continuity_constraints"}
    if set(value) != required or value["episode_id"] != episode_id:
        raise ContractError("invalid episode plan")
    if any(not value[key] for key in required - {"episode_id", "selected_worker_ids", "continuity_constraints"}):
        raise ContractError("episode plan has empty required value")
    return value


def validate_review(value: dict) -> dict:
    required = {"verdict", "strengths_to_preserve", "required_changes", "evidence_refs"}
    if set(value) != required or value["verdict"] not in {"PASS", "REVISE_ONCE", "HOLD"}:
        raise ContractError("invalid review verdict")
    changes = value["required_changes"]
    if not isinstance(changes, list) or len(changes) > 3 or len(changes) != len(set(changes)):
        raise ContractError("invalid required changes")
    if value["verdict"] == "PASS" and changes:
        raise ContractError("PASS cannot require changes")
    if value["verdict"] == "REVISE_ONCE" and not changes:
        raise ContractError("REVISE_ONCE requires changes")
    return value


def validate_memory(value: dict, episode_id: str) -> dict:
    required = {"episode_id", "confirmed_facts_added", "relationship_changes", "conflicts_resolved", "conflicts_opened", "promises_added", "important_excerpts_added", "episode_summary", "required_next_episode_continuity", "evidence_refs"}
    if set(value) != required or value["episode_id"] != episode_id:
        raise ContractError("invalid memory update")
    list_fields = required - {"episode_id", "episode_summary"}
    if any(not isinstance(value[field], list) for field in list_fields):
        raise ContractError("memory collections must be lists")
    if not all(isinstance(item, str) and item for field in list_fields for item in value[field]):
        raise ContractError("memory collection entries must be non-empty strings")
    if any(len(value[field]) != len(set(value[field])) for field in list_fields):
        raise ContractError("duplicate memory update entries")
    if not isinstance(value["episode_summary"], str) or not value["episode_summary"]:
        raise ContractError("memory summary is required")
    if "final.md" not in value["evidence_refs"]:
        raise ContractError("memory evidence must cite final.md")
    return value


def conflict_options(open_conflicts: list[str]) -> dict[str, str]:
    return {f"OC{index:03d}": conflict for index, conflict in enumerate(open_conflicts, start=1)}


def apply_conflict_selectors(value: dict, open_conflicts: list[str]) -> dict:
    canonical_fields = {"episode_id", "confirmed_facts_added", "relationship_changes", "conflicts_resolved", "conflicts_opened", "promises_added", "important_excerpts_added", "episode_summary", "required_next_episode_continuity", "evidence_refs"}
    provider_fields = canonical_fields - {"conflicts_resolved"} | {"conflict_ids_resolved"}
    if set(value) != provider_fields:
        raise ContractError("memory selector response fields are invalid")
    selected = value["conflict_ids_resolved"]
    if not isinstance(selected, list) or any(not isinstance(item, str) or not re.fullmatch(r"OC\d{3}", item) for item in selected):
        raise ContractError("memory selector IDs are invalid")
    if len(selected) != len(set(selected)):
        raise ContractError("memory selector IDs are duplicated")
    options = conflict_options(open_conflicts)
    if set(selected) - options.keys():
        raise ContractError("memory selector ID is unknown")
    resolved = [conflict for identifier, conflict in options.items() if identifier in selected]
    canonical = dict(value)
    canonical.pop("conflict_ids_resolved")
    canonical["conflicts_resolved"] = resolved
    return canonical


def apply_memory_update(source: dict, update: dict) -> dict:
    """Return a complete, deterministic memory state without mutating source."""
    validate_fixture(source)
    validate_memory(update, source["current_episode"]["episode_id"])
    additions = {
        "confirmed_facts": update["confirmed_facts_added"],
        "relationship_state": update["relationship_changes"],
        "promises": update["promises_added"],
        "important_excerpts": update["important_excerpts_added"],
        "required_next_episode_continuity": update["required_next_episode_continuity"],
    }
    for field, values in additions.items():
        if set(source[field]) & set(values):
            raise ContractError(f"memory update duplicates existing {field}")
    if update["episode_summary"] in source["episode_summaries"]:
        raise ContractError("memory update duplicates existing episode summary")
    future_values = _strings_in(source["rolling_plan"])
    if set(update["confirmed_facts_added"]) & future_values:
        raise ContractError("future plan cannot become a confirmed fact")
    resolved, opened = update["conflicts_resolved"], update["conflicts_opened"]
    if set(resolved) - set(source["open_conflicts"]):
        raise ContractError("cannot resolve missing conflict")
    if set(resolved) & set(opened) or set(source["open_conflicts"]) & set(opened):
        raise ContractError("invalid conflict update")
    result = deepcopy(source)
    result["confirmed_facts"] += additions["confirmed_facts"]
    result["relationship_state"] += additions["relationship_state"]
    result["open_conflicts"] = [item for item in source["open_conflicts"] if item not in resolved] + opened
    result["promises"] += additions["promises"]
    result["important_excerpts"] += additions["important_excerpts"]
    result["episode_summaries"] += [update["episode_summary"]]
    result["required_next_episode_continuity"] += additions["required_next_episode_continuity"]
    result["last_completed_episode_id"] = update["episode_id"]
    _validate_memory_after(source, result)
    return result


def _strings_in(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(_strings_in(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_strings_in(item) for item in value)) if value else set()
    return set()


def _validate_memory_after(source: dict, result: dict) -> None:
    for field in ("fixture_id", "series_compass", "world_rules", "characters", "rolling_plan"):
        if result[field] != source[field]:
            raise ContractError(f"stable memory field changed: {field}")
    for field in ("confirmed_facts", "relationship_state", "open_conflicts", "promises", "episode_summaries", "important_excerpts", "required_next_episode_continuity"):
        if not isinstance(result[field], list) or len(result[field]) != len(set(result[field])):
            raise ContractError(f"invalid memory state: {field}")
