# Phase 1 입력과 모델 응답 계약을 검증한다.
from __future__ import annotations

import json
from typing import Protocol


class ContractError(ValueError):
    """A mock Phase 1 contract was violated."""


class ModelClient(Protocol):
    def generate(self, *, stage: str, role: str, prompt: str) -> str: ...


REQUIRED_FIXTURE_KEYS = {
    "fixture_id", "series_compass", "world_rules", "characters", "confirmed_facts",
    "relationship_state", "open_conflicts", "episode_summaries", "important_excerpts",
    "rolling_plan", "current_episode",
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
    if not isinstance(source["confirmed_facts"], list) or not isinstance(source["episode_summaries"], list):
        raise ContractError("facts and summaries must be separate lists")


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
    if set(value) != required or value["episode_id"] != episode_id or "final.md" not in value["evidence_refs"]:
        raise ContractError("invalid memory update")
    if not value["episode_summary"]:
        raise ContractError("memory summary is required")
    return value
