# transition schema v2의 rolling plan 회계와 evidence 계약을 검증한다.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.contracts import ContractError
from arc.pilot_contracts import rolling_plan_hash, transition_action_counts, validate_rolling_plan, validate_transition, validate_transition_response
from arc.storage import write_json


EPISODE_ID = "episode_001"
NEXT_ID = "episode_002"
FINAL_TEXT = "Synthetic final prose for transition evidence checks."
FINAL_REF = f"episodes/{EPISODE_ID}/final.md"
PLAN_REF = f"episodes/{EPISODE_ID}/episode_plan.json"


def _artifacts(tmp_path: Path) -> Path:
    root = tmp_path / "episodes" / EPISODE_ID
    root.mkdir(parents=True, exist_ok=True)
    (root / "final.md").write_text(FINAL_TEXT, encoding="utf-8")
    write_json(root / "episode_plan.json", {"episode_id": EPISODE_ID, "immediate_objective": "synthetic transition objective"})
    write_json(root / "memory_update.json", {"episode_id": EPISODE_ID, "episode_summary": "synthetic transition summary"})
    write_json(root / "memory_after.json", {"episode_id": EPISODE_ID, "confirmed_facts": ["synthetic transition fact"]})
    return tmp_path


def _evidence(excerpt: str = "Synthetic final prose", ref: str = FINAL_REF) -> list[dict]:
    return [{"ref": ref, "excerpt": excerpt}]


def _decision(action: str, horizon_before, item_before, horizon_after, item_after, evidence: list[dict] | None = None) -> dict:
    return {"action": action, "horizon_before": horizon_before, "item_before": item_before, "horizon_after": horizon_after, "item_after": item_after, "reason": "synthetic decision reason", "evidence": evidence if evidence is not None else _evidence()}


def _source() -> dict:
    return {"current_episode": {"episode_id": EPISODE_ID}, "rolling_plan": {"immediate_horizon": ["goal A"], "near_horizon": ["goal B", "goal C"]}, "required_next_episode_continuity": ["cont 1", "cont 2"]}


def _transition() -> dict:
    decisions = [
        _decision("KEEP", "immediate_horizon", "goal A", "immediate_horizon", "goal A"),
        _decision("DROP", "near_horizon", "goal B", None, None),
        _decision("CHANGE", "near_horizon", "goal C", "near_horizon", "goal C adapted"),
        _decision("ADD", None, None, "near_horizon", "goal D"),
    ]
    return {
        "schema_version": 2,
        "completed_episode_id": EPISODE_ID,
        "next_episode_id": NEXT_ID,
        "transition_input_hash": "x" * 64,
        "next_source_hash": "y" * 64,
        "next_episode": {"episode_id": NEXT_ID, "importance": "ordinary", "required_role": "goal A"},
        "rolling_plan_before_hash": rolling_plan_hash(_source()["rolling_plan"]),
        "rolling_plan_after": {"immediate_horizon": ["goal A"], "near_horizon": ["goal C adapted", "goal D"]},
        "adaptation_decisions": decisions,
        "continuity_satisfied": ["cont 1"],
        "continuity_deferred": ["cont 2"],
        "adaptation_summary": "Plan adapted from completed episode results.",
        "evidence_refs": [FINAL_REF],
    }


def test_valid_schema_v2_transition_passes(tmp_path):
    run_dir = _artifacts(tmp_path)
    value = validate_transition(_transition(), _source(), NEXT_ID, run_dir)
    assert transition_action_counts(value) == {"KEEP": 1, "CHANGE": 1, "DROP": 1, "ADD": 1}


def test_json_artifact_excerpt_passes(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["adaptation_decisions"][0]["evidence"] = _evidence("synthetic transition objective", PLAN_REF)
    transition["evidence_refs"] = sorted({PLAN_REF, FINAL_REF})
    validate_transition(transition, _source(), NEXT_ID, run_dir)


def test_keep_only_reconstruction_passes(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["adaptation_decisions"] = [
        _decision("KEEP", "immediate_horizon", "goal A", "immediate_horizon", "goal A"),
        _decision("KEEP", "near_horizon", "goal B", "near_horizon", "goal B"),
        _decision("KEEP", "near_horizon", "goal C", "near_horizon", "goal C"),
    ]
    transition["rolling_plan_after"] = {"immediate_horizon": ["goal A"], "near_horizon": ["goal B", "goal C"]}
    value = validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert transition_action_counts(value) == {"KEEP": 3, "CHANGE": 0, "DROP": 0, "ADD": 0}


def test_explicit_horizon_move_is_reconstructed(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["adaptation_decisions"] = [
        _decision("KEEP", "immediate_horizon", "goal A", "immediate_horizon", "goal A"),
        _decision("KEEP", "near_horizon", "goal B", "immediate_horizon", "goal B"),
        _decision("DROP", "near_horizon", "goal C", None, None),
    ]
    transition["rolling_plan_after"] = {"immediate_horizon": ["goal A", "goal B"], "near_horizon": []}
    validate_transition(transition, _source(), NEXT_ID, run_dir)


def _mutate_unexplained_after_item(transition: dict) -> None:
    transition["rolling_plan_after"]["near_horizon"].append("goal E")


def _mutate_unconsumed_before_item(transition: dict) -> None:
    del transition["adaptation_decisions"][1]


def _mutate_duplicate_before_consumption(transition: dict) -> None:
    transition["adaptation_decisions"].append(_decision("DROP", "near_horizon", "goal B", None, None))


def _mutate_dropped_item_survives(transition: dict) -> None:
    transition["adaptation_decisions"][1] = _decision("KEEP", "near_horizon", "goal B", "near_horizon", "goal B")


def _mutate_add_existing_item(transition: dict) -> None:
    transition["adaptation_decisions"][3]["item_after"] = "goal B"


def _mutate_keep_changes_text(transition: dict) -> None:
    transition["adaptation_decisions"][0]["item_after"] = "goal A rewritten"


def _mutate_change_same_text(transition: dict) -> None:
    transition["adaptation_decisions"][2]["item_after"] = "goal C"


def _mutate_unknown_action(transition: dict) -> None:
    transition["adaptation_decisions"][0]["action"] = "REPLACE"


def _mutate_unknown_horizon(transition: dict) -> None:
    transition["adaptation_decisions"][3]["horizon_after"] = "far_horizon"


def _mutate_extra_decision_field(transition: dict) -> None:
    transition["adaptation_decisions"][0]["note"] = "extra"


def _mutate_missing_decision_field(transition: dict) -> None:
    del transition["adaptation_decisions"][0]["reason"]


def _mutate_blank_reason(transition: dict) -> None:
    transition["adaptation_decisions"][0]["reason"] = "  "


def _mutate_out_of_order_consumption(transition: dict) -> None:
    decisions = transition["adaptation_decisions"]
    decisions[0], decisions[1] = decisions[1], decisions[0]


def _mutate_reordered_after_plan(transition: dict) -> None:
    transition["rolling_plan_after"]["near_horizon"] = list(reversed(transition["rolling_plan_after"]["near_horizon"]))


@pytest.mark.parametrize("mutate", [
    _mutate_unexplained_after_item,
    _mutate_unconsumed_before_item,
    _mutate_duplicate_before_consumption,
    _mutate_dropped_item_survives,
    _mutate_add_existing_item,
    _mutate_keep_changes_text,
    _mutate_change_same_text,
    _mutate_unknown_action,
    _mutate_unknown_horizon,
    _mutate_extra_decision_field,
    _mutate_missing_decision_field,
    _mutate_blank_reason,
    _mutate_out_of_order_consumption,
    _mutate_reordered_after_plan,
])
def test_decision_accounting_failures(tmp_path, mutate):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    mutate(transition)
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "TRANSITION_DECISION_ACCOUNTING_INVALID"


def test_empty_immediate_horizon_fails(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["adaptation_decisions"][0] = _decision("DROP", "immediate_horizon", "goal A", None, None)
    transition["rolling_plan_after"]["immediate_horizon"] = []
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "TRANSITION_PLAN_INVALID"


def test_duplicate_after_plan_item_fails(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["adaptation_decisions"][3]["item_after"] = "goal C adapted"
    transition["rolling_plan_after"]["near_horizon"] = ["goal C adapted", "goal C adapted"]
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "TRANSITION_PLAN_INVALID"


def test_horizon_limits_are_enforced():
    with pytest.raises(ContractError):
        validate_rolling_plan({"immediate_horizon": [f"item {index}" for index in range(5)], "near_horizon": []}, require_immediate=True)
    with pytest.raises(ContractError):
        validate_rolling_plan({"immediate_horizon": ["item"], "near_horizon": [f"item {index}" for index in range(9)]}, require_immediate=True)


@pytest.mark.parametrize("mutate", [
    lambda transition: transition["adaptation_decisions"][0].update(evidence=[]),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("absent excerpt text")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("Synthetic final prose", "episodes/episode_002/final.md")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("Synthetic final prose", "pilot_evidence_packet.json")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("Synthetic final prose", "/episodes/episode_001/final.md")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("Synthetic final prose", "episodes/episode_001/../episode_001/final.md")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("   ")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("ab")),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=_evidence("A" * 401)),
    lambda transition: transition["adaptation_decisions"][0].update(evidence=[{"ref": FINAL_REF, "excerpt": "Synthetic final prose", "extra": True}]),
    lambda transition: transition.update(evidence_refs=[FINAL_REF, PLAN_REF]),
    lambda transition: transition.update(evidence_refs=[]),
])
def test_evidence_failures(tmp_path, mutate):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    mutate(transition)
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "TRANSITION_EVIDENCE_INVALID"


def test_next_episode_failures(tmp_path):
    run_dir = _artifacts(tmp_path)
    for mutate, code in [
        (lambda t: t["next_episode"].update(episode_id="episode_003"), "TRANSITION_NEXT_ROLE_INVALID"),
        (lambda t: t["next_episode"].update(importance="legendary"), "TRANSITION_NEXT_ROLE_INVALID"),
        (lambda t: t["next_episode"].update(required_role=""), "TRANSITION_NEXT_ROLE_INVALID"),
        (lambda t: t["next_episode"].update(required_role="goal C adapted"), "TRANSITION_NEXT_ROLE_INVALID"),
    ]:
        transition = _transition()
        mutate(transition)
        with pytest.raises(ContractError) as error:
            validate_transition(transition, _source(), NEXT_ID, run_dir)
        assert error.value.contract_code == code


def test_forbidden_synthetic_markers_fail(tmp_path):
    run_dir = _artifacts(tmp_path)
    for marker in ("synthetic transition toward", "synthetic pilot role", "Synthetic plan adapts"):
        transition = _transition()
        transition["adaptation_summary"] = f"{marker} {NEXT_ID}."
        with pytest.raises(ContractError) as error:
            validate_transition(transition, _source(), NEXT_ID, run_dir)
        assert error.value.contract_code == "TRANSITION_SYNTHETIC_MARKER"


@pytest.mark.parametrize("mutate", [
    lambda transition: transition.update(continuity_satisfied=[], continuity_deferred=["cont 1"]),
    lambda transition: transition.update(continuity_satisfied=["cont 1", "cont 1"], continuity_deferred=["cont 2"]),
    lambda transition: transition.update(continuity_satisfied=["cont 1"], continuity_deferred=["cont 1", "cont 2"]),
    lambda transition: transition.update(continuity_satisfied=["cont 1", "unknown"], continuity_deferred=["cont 2"]),
])
def test_continuity_partition_failures(tmp_path, mutate):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    mutate(transition)
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "TRANSITION_CONTINUITY_INVALID"


def test_before_hash_mismatch_fails(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["rolling_plan_before_hash"] = "0" * 64
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "TRANSITION_FIELDS_MISMATCH"


def test_legacy_schema_v1_is_diagnosed_not_validated(tmp_path):
    run_dir = _artifacts(tmp_path)
    legacy = {"schema_version": 1, "completed_episode_id": EPISODE_ID, "next_episode_id": NEXT_ID, "transition_input_hash": "x" * 64, "next_source_hash": "y" * 64, "next_episode": {"episode_id": NEXT_ID, "importance": "ordinary", "required_role": f"synthetic pilot role for {NEXT_ID}"}, "rolling_plan_after": {"immediate_horizon": [], "near_horizon": [f"synthetic transition toward {NEXT_ID}"]}, "continuity_satisfied": [], "continuity_deferred": ["cont 1", "cont 2"], "adaptation_summary": f"Synthetic plan adapts after {EPISODE_ID} toward {NEXT_ID}.", "evidence_refs": [FINAL_REF]}
    with pytest.raises(ContractError) as error:
        validate_transition(legacy, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "LEGACY_SYNTHETIC_TRANSITION"


def test_legacy_v1_with_partial_v2_fields_is_not_v2(tmp_path):
    run_dir = _artifacts(tmp_path)
    transition = _transition()
    transition["schema_version"] = 1
    with pytest.raises(ContractError) as error:
        validate_transition(transition, _source(), NEXT_ID, run_dir)
    assert error.value.contract_code == "LEGACY_SYNTHETIC_TRANSITION"


def test_schema_field_mismatch_fails(tmp_path):
    run_dir = _artifacts(tmp_path)
    for mutate in (lambda t: t.pop("rolling_plan_before_hash"), lambda t: t.update(unexpected=True), lambda t: t.update(schema_version=3)):
        transition = _transition()
        mutate(transition)
        with pytest.raises(ContractError) as error:
            validate_transition(transition, _source(), NEXT_ID, run_dir)
        assert error.value.contract_code == "TRANSITION_FIELDS_MISMATCH"


def test_transition_response_shape():
    response = {name: None for name in ("next_episode", "rolling_plan_after", "adaptation_decisions", "continuity_satisfied", "continuity_deferred", "adaptation_summary", "evidence_refs")}
    assert validate_transition_response(response) is response
    with pytest.raises(ContractError) as error:
        validate_transition_response([])
    assert error.value.contract_code == "TRANSITION_RESPONSE_NOT_OBJECT"
    for mutate in (lambda value: value.pop("evidence_refs"), lambda value: value.update(transition_input_hash="x")):
        value = dict(response)
        mutate(value)
        with pytest.raises(ContractError) as fields_error:
            validate_transition_response(value)
        assert fields_error.value.contract_code == "TRANSITION_FIELDS_MISMATCH"
