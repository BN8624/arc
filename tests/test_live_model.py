# Phase 2 live 프롬프트와 key-slot 계약을 네트워크 없이 검증한다.
from __future__ import annotations

import pytest

from arc.contracts import ContractError, apply_conflict_selectors, conflict_options, validate_memory
from arc.live_model import LiveConfig, LiveConfigError, SLOT_MAP
from arc.prompts import build_prompt


def test_memory_merge_prompt_exposes_deterministic_conflict_options() -> None:
    conflict = "exact open conflict"
    prompt = build_prompt("memory_merge", "merge", {"episode_id": "LIVE001", "open_conflicts": [conflict]})
    assert "CURRENT_OPEN_CONFLICT_OPTIONS" in prompt
    assert "OC001:" in prompt
    assert conflict in prompt
    assert "conflict_ids_resolved" in prompt
    assert "Never write or paraphrase" in prompt
    assert "empty list" in prompt
    assert "conflicts_opened" in prompt


def test_memory_merge_prompt_uses_empty_conflict_options() -> None:
    prompt = build_prompt("memory_merge", "merge", {"episode_id": "LIVE001", "open_conflicts": []})
    assert "CURRENT_OPEN_CONFLICT_OPTIONS\n[]" in prompt


def _provider_update(ids: list[str]) -> dict:
    return {"episode_id": "LIVE001", "confirmed_facts_added": [], "relationship_changes": [], "conflict_ids_resolved": ids, "conflicts_opened": [], "promises_added": [], "important_excerpts_added": [], "episode_summary": "summary", "required_next_episode_continuity": [], "evidence_refs": ["final.md"]}


def test_conflict_selector_mapping_is_deterministic_and_canonical() -> None:
    open_conflicts = ["first", "second"]
    assert conflict_options(open_conflicts) == {"OC001": "first", "OC002": "second"}
    canonical = apply_conflict_selectors(_provider_update(["OC002", "OC001"]), open_conflicts)
    assert canonical["conflicts_resolved"] == ["first", "second"]
    assert "conflict_ids_resolved" not in canonical
    assert validate_memory(canonical, "LIVE001") == canonical


@pytest.mark.parametrize("ids", [["OC003"], ["OC001", "OC001"], ["OC1"], "OC001"])
def test_conflict_selector_rejects_invalid_ids(ids: object) -> None:
    with pytest.raises(ContractError):
        apply_conflict_selectors(_provider_update(ids), ["first", "second"])


def test_conflict_selector_rejects_direct_conflict_text() -> None:
    value = _provider_update([])
    value["conflicts_resolved"] = ["first"]
    with pytest.raises(ContractError):
        apply_conflict_selectors(value, ["first"])


def test_live_config_requires_distinct_slots() -> None:
    env = {"MODEL": "gemma-4-31b-it", **{f"GOOGLE_API_KEY_{index}": f"key-{index}" for index in range(1, 12)}}
    assert LiveConfig.from_environment(env).keys["K11"] == "key-11"
    env["GOOGLE_API_KEY_11"] = "key-10"
    with pytest.raises(LiveConfigError):
        LiveConfig.from_environment(env)


def test_slot_mapping_has_all_live_slots() -> None:
    assert SLOT_MAP[("planning", "event")] == "K01"
    assert SLOT_MAP[("writer", "canonical")] == "K08"
    assert SLOT_MAP[("memory_merge", "merge")] == "K11"
