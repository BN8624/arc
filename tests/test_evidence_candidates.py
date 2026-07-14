from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.evidence_candidates import (
    EVIDENCE_CANDIDATE_CATALOG_VERSION,
    EvidenceCandidateCatalogError,
    UnknownEvidenceCandidateError,
    build_evidence_candidate_catalog,
    catalog_bytes,
    lookup_candidate,
    make_candidate_id,
    materialize_candidate_ids,
    generate_json_candidates,
    generate_prose_candidates,
    validate_catalog,
)


FINAL_REF = "episodes/episode_004/final.md"
PLAN_REF = "episodes/episode_004/episode_plan.json"


def _candidate_map(catalog):
    return {candidate.candidate_id: candidate for candidate in catalog}


def test_run_4_failure_shape_never_enters_catalog():
    original = "그는 탁자 위에 흩어진 설계도들을. 그것은 그가 단순한 오류가 아니라는 사실을 깨닫게 했다."
    edited = "그는 탁자 위에 흩어진 설계도들… 자신이 단순한 오류가 아니라는 사실을 깨닫게 했다."
    catalog = build_evidence_candidate_catalog({FINAL_REF: original})

    assert catalog
    assert all(candidate.excerpt in original for candidate in catalog)
    assert edited not in {candidate.excerpt for candidate in catalog}


def test_valid_candidate_id_materializes_to_exact_canonical_evidence():
    original = "그는 탁자 위에 흩어진 설계도들을. 그것은 그가 단순한 오류가 아니라는 사실을 깨닫게 했다."
    catalog = build_evidence_candidate_catalog({FINAL_REF: original})
    candidate = catalog[0]

    assert materialize_candidate_ids([candidate.candidate_id], catalog) == [{"ref": FINAL_REF, "excerpt": candidate.excerpt}]
    assert candidate.candidate_id == make_candidate_id(FINAL_REF, candidate.excerpt)


def test_unknown_tampered_and_other_ref_ids_fail_at_lookup():
    first = build_evidence_candidate_catalog({FINAL_REF: "첫 번째 artifact에는 충분히 긴 정확한 문장이 있습니다."})
    other_ref = "episodes/episode_004/memory_after.json"
    other = build_evidence_candidate_catalog({other_ref: json.dumps({"summary": "두 번째 artifact에도 충분히 긴 정확한 문장이 있습니다."}, ensure_ascii=False)})

    with pytest.raises(UnknownEvidenceCandidateError):
        lookup_candidate(first, "EC_000000000000")
    with pytest.raises(UnknownEvidenceCandidateError):
        lookup_candidate(first, other[0].candidate_id)
    with pytest.raises(UnknownEvidenceCandidateError):
        lookup_candidate(first, first[0].candidate_id[:-1] + ("0" if first[0].candidate_id[-1] != "0" else "1"))


def test_same_bytes_are_byte_deterministic_and_ids_include_ref():
    text = "앞부분의 충분히 긴 문장입니다. 중간 부분도 exact candidate가 됩니다. 마지막 부분까지 순회합니다."
    artifacts = {FINAL_REF: text, PLAN_REF: json.dumps({"objective": "계획에 있는 충분히 긴 문자열입니다."}, ensure_ascii=False, indent=2)}
    first = build_evidence_candidate_catalog(artifacts)
    second = build_evidence_candidate_catalog(dict(reversed(list(artifacts.items()))))

    assert catalog_bytes(first) == catalog_bytes(second)
    assert [candidate.candidate_id for candidate in first] == [candidate.candidate_id for candidate in second]
    assert make_candidate_id(FINAL_REF, "같은 excerpt") != make_candidate_id("episodes/episode_005/final.md", "같은 excerpt")
    assert EVIDENCE_CANDIDATE_CATALOG_VERSION == 1


def test_prose_covers_front_middle_back_and_exact_long_sentence_splits():
    text = "앞부분의 문장에는 한국어 구두점이 있습니다. " + ("중간에 있는 긴 문장도 원문을 바꾸지 않고 정확하게 분할해야 합니다. " * 18) + "마지막 부분의 문장도 후보가 됩니다!"
    catalog = generate_prose_candidates(text, ref=FINAL_REF)

    assert len(catalog) >= 3
    assert all(candidate.excerpt in text and 8 <= len(candidate.excerpt) <= 400 for candidate in catalog)
    assert any(candidate.excerpt.startswith("앞부분") for candidate in catalog)
    assert any("중간에" in candidate.excerpt for candidate in catalog)
    assert any(candidate.excerpt.startswith("마지막") for candidate in catalog)
    assert all(candidate.ordinal == index for index, candidate in enumerate(catalog))


def test_prose_preserves_punctuation_and_existing_ellipsis_only():
    text = "ASCII punctuation stays exact! 한국어 문장도 그대로 남습니다? 원문에만 …가 있습니다."
    catalog = generate_prose_candidates(text, ref=FINAL_REF)
    excerpts = {candidate.excerpt for candidate in catalog}

    assert any("!" in excerpt for excerpt in excerpts)
    assert any("?" in excerpt for excerpt in excerpts)
    assert any("…" in excerpt for excerpt in excerpts)
    assert all("..." not in excerpt for excerpt in excerpts)


def test_prose_does_not_reassemble_across_newlines():
    text = "첫 줄에 충분한 exact candidate가 있습니다.\n둘째 줄에도 충분한 exact candidate가 있습니다."
    catalog = generate_prose_candidates(text, ref=FINAL_REF)

    assert catalog
    assert all("\n" not in candidate.excerpt for candidate in catalog)


def test_json_walks_nested_lists_and_dicts_in_stable_order():
    raw = json.dumps(
        {"outer": {"z": ["한국어 문자열 leaf가 충분히 깁니다.", {"inner": "nested list leaf도 충분히 깁니다."}], "a": "앞선 키가 아니어도 순서는 안정적입니다."}},
        ensure_ascii=False,
        indent=2,
    )
    catalog = generate_json_candidates(raw, ref=PLAN_REF)

    assert [candidate.ordinal for candidate in catalog] == list(range(len(catalog)))
    assert any("한국어 문자열" in candidate.excerpt for candidate in catalog)
    assert any("nested list" in candidate.excerpt for candidate in catalog)
    assert all(candidate.excerpt in raw for candidate in catalog)


def test_json_excludes_escaped_leaves_and_key_names():
    raw = json.dumps(
        {
            "a_key_name_that_is_not_evidence": "평범한 문자열 leaf는 raw JSON에 그대로 존재합니다.",
            "quote_value": '이 문자열에는 "따옴표"가 있습니다.',
            "backslash_value": r"이 문자열에는 backslash\\가 있습니다.",
            "newline_value": "첫 줄\n둘째 줄",
        },
        ensure_ascii=False,
        indent=2,
    )
    catalog = generate_json_candidates(raw, ref=PLAN_REF)
    excerpts = {candidate.excerpt for candidate in catalog}

    assert any("평범한 문자열" in excerpt for excerpt in excerpts)
    assert not any("a_key_name_that_is_not_evidence" == excerpt for excerpt in excerpts)
    assert not any("따옴표" in excerpt for excerpt in excerpts)
    assert not any("backslash" in excerpt for excerpt in excerpts)
    assert not any("첫 줄" in excerpt for excerpt in excerpts)


def test_duplicate_json_values_are_one_pair_and_empty_leaves_are_skipped():
    raw = json.dumps({"first": "동일한 문자열 값이 두 번 나옵니다.", "second": "동일한 문자열 값이 두 번 나옵니다.", "short": "x"}, ensure_ascii=False)
    catalog = generate_json_candidates(raw, ref=PLAN_REF)

    assert len([candidate for candidate in catalog if candidate.excerpt == "동일한 문자열 값이 두 번 나옵니다."]) == 1
    assert all(candidate.excerpt != "x" for candidate in catalog)


def test_catalog_validation_rejects_tampering_duplicate_order_and_bounds():
    raw = "충분히 긴 원문 exact candidate 문장이 있습니다."
    catalog = build_evidence_candidate_catalog({FINAL_REF: raw})

    tampered = list(catalog)
    tampered[0] = type(catalog[0])(tampered[0].candidate_id[:-1] + "0", tampered[0].ref, tampered[0].kind, tampered[0].episode_id, tampered[0].ordinal, tampered[0].excerpt)
    if tampered[0].candidate_id == catalog[0].candidate_id:
        tampered[0] = type(catalog[0])(tampered[0].candidate_id[:-1] + "1", tampered[0].ref, tampered[0].kind, tampered[0].episode_id, tampered[0].ordinal, tampered[0].excerpt)
    with pytest.raises(EvidenceCandidateCatalogError):
        validate_catalog(tampered, {FINAL_REF: raw})

    duplicate = catalog + [catalog[0]]
    with pytest.raises(EvidenceCandidateCatalogError):
        validate_catalog(duplicate, {FINAL_REF: raw})

    invalid_ref = type(catalog[0])(catalog[0].candidate_id, "../secret.txt", catalog[0].kind, catalog[0].episode_id, 0, catalog[0].excerpt)
    with pytest.raises(EvidenceCandidateCatalogError):
        validate_catalog([invalid_ref], {"../secret.txt": raw})


def test_catalog_validation_rejects_unknown_refs_and_requires_candidates_when_requested():
    with pytest.raises(EvidenceCandidateCatalogError):
        build_evidence_candidate_catalog({"secrets/api_key.txt": "not an allowed artifact"})
    with pytest.raises(EvidenceCandidateCatalogError):
        build_evidence_candidate_catalog({FINAL_REF: "short"})

    raw = "short"
    validate_catalog([], {FINAL_REF: raw}, require_candidate_per_ref=False)

