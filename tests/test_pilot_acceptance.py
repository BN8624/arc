# 수용 rubric registry와 criterion 단위 evidence 계약을 검증한다.
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from arc.contracts import ContractError
from arc.evidence_candidates import make_candidate_id
from arc.mock_model import acceptance_review_response
from arc.pilot_contracts import (
    ACCEPTANCE_FORBIDDEN_MARKERS,
    ACCEPTANCE_GENERIC_QUESTION_MARKER,
    ACCEPTANCE_RUBRIC,
    ACCEPTANCE_RUBRIC_VERSION,
    ACCEPTANCE_SCHEMA_VERSION,
    PILOT_REVIEW_ROLES,
    acceptance_catalog_plan,
    aggregate_pilot_acceptance,
    materialize_acceptance_worker_response,
    validate_acceptance_coverage,
    validate_acceptance_rubric,
    validate_acceptance_worker,
    validate_grounded_pilot_acceptance,
)


EPISODE_IDS = ["episode_001", "episode_002", "episode_003", "episode_004", "episode_005"]


def _catalog(episode_ids: list[str] = EPISODE_IDS) -> list[dict]:
    entries = []
    for ref, kind, episode_id in acceptance_catalog_plan(episode_ids):
        content = f"Synthetic {kind} body for {episode_id} referencing {ref} inside the five episode pilot. " * 2
        entries.append({"ref": ref, "kind": kind, "episode_id": episode_id, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "content": content})
    return entries


def _rubric_by_id() -> dict[str, dict]:
    return {item["dimension"]: item for item in ACCEPTANCE_RUBRIC}


def _payload(role: str, catalog: list[dict] | None = None) -> dict:
    dimension = _rubric_by_id()[role]
    catalog = catalog if catalog is not None else _catalog()
    candidates = [{"candidate_id": make_candidate_id(entry["ref"], entry["content"][:80]), "ref": entry["ref"], "kind": entry["kind"], "episode_id": entry["episode_id"], "ordinal": 0, "excerpt": entry["content"][:80]} for entry in catalog]
    return {
        "dimension": role,
        "episode_ids": list(EPISODE_IDS),
        "evidence_catalog": catalog,
        "evidence_candidates": candidates,
        "criteria": json.loads(json.dumps(dimension["criteria"])),
        "coverage_rule": json.loads(json.dumps(dimension["coverage_rule"])),
    }


def _worker(role: str, hold: bool = False, catalog: list[dict] | None = None) -> dict:
    payload = _payload(role, catalog)
    candidates = payload["evidence_candidates"]
    return materialize_acceptance_worker_response(acceptance_review_response(payload, hold=hold), role, candidates, EPISODE_IDS)


def _mutable_rubric() -> list[dict]:
    return json.loads(json.dumps(ACCEPTANCE_RUBRIC))


def _expect(callable_, code: str) -> None:
    with pytest.raises(ContractError) as error:
        callable_()
    assert error.value.contract_code == code


CATALOG = _catalog()


# --- Rubric registry ---


def test_rubric_registry_satisfies_contract() -> None:
    rubric = validate_acceptance_rubric()
    assert list(rubric) == PILOT_REVIEW_ROLES
    assert sum(len(item["criteria"]) for item in rubric.values()) == 21
    assert all(2 <= len(item["criteria"]) <= 4 for item in rubric.values())


def test_rubric_questions_are_unique_and_not_generic() -> None:
    questions = [item["question"] for item in ACCEPTANCE_RUBRIC]
    assert len(questions) == len(set(questions)) == 7
    assert all(ACCEPTANCE_GENERIC_QUESTION_MARKER not in question for question in questions)
    titles = [item["title"] for item in ACCEPTANCE_RUBRIC]
    assert len(titles) == len(set(titles)) == 7


def test_rubric_rejects_wrong_dimension_set() -> None:
    with pytest.raises(ContractError):
        validate_acceptance_rubric(_mutable_rubric()[:-1])


def test_rubric_rejects_generic_question() -> None:
    rubric = _mutable_rubric()
    rubric[0]["question"] = f"{ACCEPTANCE_GENERIC_QUESTION_MARKER} readability."
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_duplicate_criterion_id() -> None:
    rubric = _mutable_rubric()
    rubric[0]["criteria"][1]["criterion_id"] = rubric[0]["criteria"][0]["criterion_id"]
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_missing_criterion_field() -> None:
    rubric = _mutable_rubric()
    del rubric[0]["criteria"][0]["pass_rule"]
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_unknown_evidence_kind() -> None:
    rubric = _mutable_rubric()
    rubric[0]["criteria"][0]["required_evidence_kinds"] = ["unknown_kind"]
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_criteria_count_out_of_range() -> None:
    rubric = _mutable_rubric()
    rubric[0]["criteria"] = rubric[0]["criteria"][:1]
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)
    rubric = _mutable_rubric()
    extra = [dict(criterion, criterion_id=f"readability.extra_{index}") for index, criterion in enumerate(rubric[1]["criteria"][:2])]
    rubric[0]["criteria"] += extra
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_duplicate_question() -> None:
    rubric = _mutable_rubric()
    rubric[1]["question"] = rubric[0]["question"]
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_blank_rule() -> None:
    rubric = _mutable_rubric()
    rubric[2]["criteria"][0]["hold_rule"] = " "
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_unknown_coverage_selector() -> None:
    rubric = _mutable_rubric()
    rubric[0]["coverage_rule"]["required_kind_episodes"]["episode_final"] = "some"
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


def test_rubric_rejects_invalid_coverage_fields() -> None:
    rubric = _mutable_rubric()
    del rubric[0]["coverage_rule"]["minimum_granular_refs"]
    with pytest.raises(ContractError):
        validate_acceptance_rubric(rubric)


# --- Worker contract ---


def test_valid_pass_worker_accepted_for_all_dimensions() -> None:
    for role in PILOT_REVIEW_ROLES:
        worker = _worker(role)
        assert validate_acceptance_worker(worker, role, CATALOG, EPISODE_IDS) is worker
        assert worker["proposal"]["dimension_result"] == "PASS"
        assert worker["proposal"]["strengths"]


def test_valid_hold_worker_accepted() -> None:
    worker = _worker("continuity", hold=True)
    validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS)
    assert worker["proposal"]["dimension_result"] == "HOLD"
    assert worker["proposal"]["critical_finding"]["criterion_id"] == "continuity.required_obligations"


def test_worker_rejects_non_object() -> None:
    _expect(lambda: validate_acceptance_worker([], "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_RESPONSE_NOT_OBJECT")


def test_worker_rejects_field_mismatch() -> None:
    worker = _worker("readability")
    worker["extra"] = True
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_FIELDS_MISMATCH")


def test_worker_rejects_missing_criterion() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"].pop()
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITERIA_MISMATCH")


def test_worker_rejects_duplicate_criterion() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"].append(copy.deepcopy(worker["proposal"]["criterion_results"][0]))
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITERIA_MISMATCH")


def test_worker_rejects_unknown_criterion() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["criterion_id"] = "readability.unknown"
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITERIA_MISMATCH")


def test_worker_rejects_foreign_dimension_criterion() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["criterion_id"] = "continuity.required_obligations"
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITERIA_MISMATCH")


def test_worker_rejects_invalid_result_value() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["result"] = "MAYBE"
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITERIA_MISMATCH")


def test_worker_rejects_blank_finding() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["finding"] = "  "
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITERIA_MISMATCH")


def test_worker_rejects_empty_evidence() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["evidence"] = []
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_fabricated_excerpt() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["evidence"][0]["excerpt"] = "fabricated excerpt that never appears"
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_excerpt_from_other_artifact() -> None:
    worker = _worker("readability")
    other = next(entry for entry in CATALOG if entry["kind"] == "episode_final" and entry["episode_id"] == "episode_002")
    worker["proposal"]["criterion_results"][0]["evidence"][0] = {"ref": "episodes/episode_001/final.md", "excerpt": other["content"][:80]}
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_out_of_catalog_ref() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["evidence"][0]["ref"] = "episodes/episode_999/final.md"
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


@pytest.mark.parametrize("ref", ["C:/episodes/episode_001/final.md", "episodes/../episodes/episode_001/final.md"])
def test_worker_rejects_absolute_and_parent_path_refs(ref: str) -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["evidence"][0]["ref"] = ref
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_aggregate_packet_only_evidence() -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["evidence"] = [{"ref": "pilot_evidence_packet.json", "excerpt": "aggregate packet excerpt"}]
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_duplicate_evidence_item() -> None:
    worker = _worker("readability")
    evidence = worker["proposal"]["criterion_results"][0]["evidence"]
    evidence.append(dict(evidence[0]))
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


@pytest.mark.parametrize("excerpt", ["short", "S" + "y" * 500])
def test_worker_rejects_out_of_range_excerpt(excerpt: str) -> None:
    worker = _worker("readability")
    worker["proposal"]["criterion_results"][0]["evidence"][0]["excerpt"] = excerpt
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_wrong_kind_evidence() -> None:
    worker = _worker("readability")
    transition = next(entry for entry in CATALOG if entry["kind"] == "transition")
    worker["proposal"]["criterion_results"][0]["evidence"][0] = {"ref": transition["ref"], "excerpt": transition["content"][:80]}
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_missing_required_kind() -> None:
    worker = _worker("character_consistency")
    final = next(entry for entry in CATALOG if entry["kind"] == "episode_final")
    worker["proposal"]["criterion_results"][0]["evidence"] = [{"ref": final["ref"], "excerpt": final["content"][:80]}]
    _expect(lambda: validate_acceptance_worker(worker, "character_consistency", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_top_level_evidence_refs_mismatch() -> None:
    worker = _worker("readability")
    worker["evidence_refs"] = worker["evidence_refs"] + ["episodes/episode_002/final.md"]
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_worker_rejects_hold_criterion_with_pass_dimension() -> None:
    worker = _worker("continuity", hold=True)
    worker["proposal"]["dimension_result"] = "PASS"
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_RESULT_INCONSISTENT")


def test_worker_rejects_all_pass_with_hold_dimension() -> None:
    worker = _worker("continuity")
    worker["proposal"]["dimension_result"] = "HOLD"
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_RESULT_INCONSISTENT")


def test_worker_rejects_pass_with_critical_finding() -> None:
    worker = _worker("readability")
    worker["proposal"]["critical_finding"] = {"criterion_id": "readability.local_clarity", "finding": "should not exist"}
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITICAL_FINDING_INVALID")


def test_worker_rejects_hold_without_critical_finding() -> None:
    worker = _worker("continuity", hold=True)
    worker["proposal"]["critical_finding"] = None
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITICAL_FINDING_INVALID")


def test_worker_rejects_critical_not_linked_to_hold_criterion() -> None:
    worker = _worker("continuity", hold=True)
    worker["proposal"]["critical_finding"]["criterion_id"] = "continuity.fact_and_conflict_chain"
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_CRITICAL_FINDING_INVALID")


def test_worker_rejects_pass_without_strengths() -> None:
    worker = _worker("readability")
    worker["proposal"]["strengths"] = []
    worker["evidence_refs"] = sorted({item["ref"] for result in worker["proposal"]["criterion_results"] for item in result["evidence"]})
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_STRENGTH_INVALID")


def test_worker_rejects_strength_without_evidence() -> None:
    worker = _worker("readability")
    worker["proposal"]["strengths"][0]["evidence"] = []
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_STRENGTH_INVALID")


def test_worker_rejects_strength_on_hold_criterion() -> None:
    worker = _worker("continuity", hold=True)
    worker["proposal"]["strengths"][0]["criterion_id"] = "continuity.required_obligations"
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_STRENGTH_INVALID")


def test_worker_rejects_duplicate_strengths() -> None:
    worker = _worker("readability")
    worker["proposal"]["strengths"].append(copy.deepcopy(worker["proposal"]["strengths"][0]))
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_STRENGTH_INVALID")


@pytest.mark.parametrize("marker", ["works well", "synthetic continuity evidence"])
def test_worker_rejects_generic_or_forbidden_strength(marker: str) -> None:
    worker = _worker("readability")
    worker["proposal"]["strengths"][0]["strength"] = f"The dimension {marker} across the pilot."
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_STRENGTH_INVALID")


def test_worker_rejects_too_many_strengths() -> None:
    worker = _worker("readability")
    base = worker["proposal"]["strengths"][0]
    worker["proposal"]["strengths"] = [dict(base, strength=f"{base['strength']} Variant {index}.") for index in range(3)]
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_STRENGTH_INVALID")


# --- Coverage ---


def test_worker_rejects_missing_required_episode_coverage() -> None:
    worker = _worker("readability")
    worker["proposal"]["coverage_refs"] = [ref for ref in worker["proposal"]["coverage_refs"] if ref != "episodes/episode_005/final.md"]
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


def test_worker_rejects_missing_transition_coverage() -> None:
    worker = _worker("continuity")
    worker["proposal"]["coverage_refs"] = [ref for ref in worker["proposal"]["coverage_refs"] if ref != "transitions/episode_004_to_episode_005.json"]
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


def test_worker_rejects_missing_first_source_coverage() -> None:
    worker = _worker("character_consistency")
    worker["proposal"]["coverage_refs"] = [ref for ref in worker["proposal"]["coverage_refs"] if ref != "episode_sources/episode_001.json"]
    _expect(lambda: validate_acceptance_worker(worker, "character_consistency", CATALOG, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


def test_worker_rejects_missing_memory_kind_coverage() -> None:
    worker = _worker("memory_correctness")
    worker["proposal"]["coverage_refs"] = [ref for ref in worker["proposal"]["coverage_refs"] if ref != "episodes/episode_003/memory_update.json"]
    _expect(lambda: validate_acceptance_worker(worker, "memory_correctness", CATALOG, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


def test_worker_rejects_unsorted_coverage_refs() -> None:
    worker = _worker("readability")
    worker["proposal"]["coverage_refs"] = list(reversed(worker["proposal"]["coverage_refs"]))
    _expect(lambda: validate_acceptance_worker(worker, "readability", CATALOG, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


def test_worker_rejects_uncovered_evidence_ref() -> None:
    worker = _worker("continuity")
    memory_ref = next(ref for ref in worker["evidence_refs"] if "/memory_after.json" in ref)
    worker["proposal"]["coverage_refs"] = [ref for ref in worker["proposal"]["coverage_refs"] if ref != memory_ref]
    _expect(lambda: validate_acceptance_worker(worker, "continuity", CATALOG, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


def test_coverage_minimum_granular_refs_enforced() -> None:
    dimension = {"coverage_rule": {"required_kind_episodes": {}, "required_transitions": None, "minimum_kind_episodes": {}, "require_first_and_last_episode": False, "minimum_granular_refs": 6}}
    index = {entry["ref"]: entry for entry in CATALOG}
    refs = sorted(entry["ref"] for entry in CATALOG if entry["kind"] == "episode_final")
    _expect(lambda: validate_acceptance_coverage(dimension, refs, index, EPISODE_IDS), "PILOT_REVIEW_COVERAGE_INCOMPLETE")


# --- Evidence catalog ---


def test_catalog_rejects_wrong_order() -> None:
    catalog = _catalog()
    catalog[0], catalog[1] = catalog[1], catalog[0]
    _expect(lambda: validate_acceptance_worker(_worker("readability"), "readability", catalog, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


def test_catalog_rejects_content_hash_mismatch() -> None:
    catalog = _catalog()
    catalog[0]["content"] += " tampered"
    _expect(lambda: validate_acceptance_worker(_worker("readability"), "readability", catalog, EPISODE_IDS), "PILOT_REVIEW_EVIDENCE_INVALID")


# --- Deterministic aggregation ---


def _workers(hold_dimension: str | None = None) -> list[dict]:
    return [_worker(role, hold=role == hold_dimension) for role in PILOT_REVIEW_ROLES]


def test_aggregate_grounded_pass() -> None:
    workers = _workers()
    acceptance = aggregate_pilot_acceptance(workers)
    assert acceptance["schema_version"] == ACCEPTANCE_SCHEMA_VERSION
    assert acceptance["rubric_version"] == ACCEPTANCE_RUBRIC_VERSION
    assert acceptance["verdict"] == "PASS"
    assert set(acceptance["dimension_results"]) == set(PILOT_REVIEW_ROLES)
    assert acceptance["critical_findings"] == []
    assert len(acceptance["strengths_to_preserve"]) == 7
    assert all(set(strength) == {"dimension", "criterion_id", "strength", "evidence"} for strength in acceptance["strengths_to_preserve"])
    refs = {entry["ref"] for entry in CATALOG}
    assert acceptance["evidence_refs"] == sorted(set(acceptance["evidence_refs"]))
    assert set(acceptance["evidence_refs"]) <= refs
    document = json.dumps(acceptance, ensure_ascii=False)
    assert all(marker not in document for marker in ACCEPTANCE_FORBIDDEN_MARKERS)
    assert validate_grounded_pilot_acceptance(acceptance, workers) is acceptance


def test_aggregate_grounded_hold() -> None:
    workers = _workers("continuity")
    acceptance = aggregate_pilot_acceptance(workers)
    assert acceptance["verdict"] == "HOLD"
    assert acceptance["dimension_results"]["continuity"] == "HOLD"
    assert len(acceptance["critical_findings"]) == 1
    finding = acceptance["critical_findings"][0]
    assert finding["dimension"] == "continuity"
    assert finding["criterion_id"] == "continuity.required_obligations"
    assert finding["evidence"]
    assert len(acceptance["strengths_to_preserve"]) == 7
    validate_grounded_pilot_acceptance(acceptance, workers)


def test_aggregate_rejects_wrong_worker_order() -> None:
    workers = _workers()
    workers[0], workers[1] = workers[1], workers[0]
    with pytest.raises(ContractError):
        aggregate_pilot_acceptance(workers)


def test_aggregate_rejects_excess_strengths() -> None:
    workers = _workers()
    base = workers[0]["proposal"]["strengths"][0]
    for worker in workers:
        strength = worker["proposal"]["strengths"][0]
        worker["proposal"]["strengths"] = [strength, dict(strength, strength=f"{strength['strength']} Second angle.")]
    workers[0]["proposal"]["strengths"].append(dict(base, strength=f"{base['strength']} Third angle."))
    with pytest.raises(ContractError):
        aggregate_pilot_acceptance(workers)


def test_grounded_validation_rejects_legacy_acceptance() -> None:
    legacy = {"verdict": "PASS", "dimension_results": {role: "PASS" for role in PILOT_REVIEW_ROLES}, "critical_findings": [], "strengths_to_preserve": ["legacy strength"], "evidence_refs": ["pilot_evidence_packet.json"]}
    _expect(lambda: validate_grounded_pilot_acceptance(legacy, _workers()), "LEGACY_GENERIC_ACCEPTANCE")


def test_grounded_validation_rejects_schema_version_flip() -> None:
    legacy = {"schema_version": 2, "verdict": "PASS", "dimension_results": {role: "PASS" for role in PILOT_REVIEW_ROLES}, "critical_findings": [], "strengths_to_preserve": ["legacy strength"], "evidence_refs": ["pilot_evidence_packet.json"]}
    with pytest.raises(ContractError):
        validate_grounded_pilot_acceptance(legacy, _workers())


def test_grounded_validation_rejects_tampered_dimension_result() -> None:
    workers = _workers()
    acceptance = aggregate_pilot_acceptance(workers)
    acceptance["dimension_results"]["readability"] = "HOLD"
    with pytest.raises(ContractError):
        validate_grounded_pilot_acceptance(acceptance, workers)


def test_grounded_validation_rejects_tampered_strength_and_refs() -> None:
    workers = _workers()
    acceptance = aggregate_pilot_acceptance(workers)
    tampered = json.loads(json.dumps(acceptance))
    tampered["strengths_to_preserve"][0]["strength"] = "A rewritten strength."
    with pytest.raises(ContractError):
        validate_grounded_pilot_acceptance(tampered, workers)
    tampered = json.loads(json.dumps(acceptance))
    tampered["evidence_refs"] = tampered["evidence_refs"][:-1]
    with pytest.raises(ContractError):
        validate_grounded_pilot_acceptance(tampered, workers)


def test_grounded_validation_rejects_tampered_critical_finding() -> None:
    workers = _workers("continuity")
    acceptance = aggregate_pilot_acceptance(workers)
    acceptance["critical_findings"][0]["finding"] = "A rewritten finding."
    with pytest.raises(ContractError):
        validate_grounded_pilot_acceptance(acceptance, workers)


def test_grounded_validation_rejects_forbidden_marker() -> None:
    workers = _workers()
    acceptance = aggregate_pilot_acceptance(workers)
    acceptance["strengths_to_preserve"][0]["strength"] = "synthetic continuity evidence"
    _expect(lambda: validate_grounded_pilot_acceptance(acceptance, workers), "PILOT_REVIEW_STRENGTH_INVALID")
