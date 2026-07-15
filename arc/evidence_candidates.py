from __future__ import annotations

# 실제 artifact에서 exact-substring evidence 후보를 결정론적으로 생성하고 검증한다.

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


EVIDENCE_CANDIDATE_CATALOG_VERSION = 1
EVIDENCE_CANDIDATE_ID_PREFIX = "EC_"
EVIDENCE_CANDIDATE_ID_HEX_LENGTH = 16
EVIDENCE_CANDIDATE_MIN_CHARACTERS = 8
EVIDENCE_CANDIDATE_TARGET_MAX_CHARACTERS = 120
EVIDENCE_CANDIDATE_MAX_CHARACTERS = 400

_KNOWN_KINDS = {
    "final.md": "episode_final",
    "episode_plan.json": "episode_plan",
    "memory_update.json": "episode_memory_update",
    "memory_after.json": "episode_memory_after",
    "review_decision.json": "episode_review",
}
_SENTENCE_ENDINGS = ".!?。！？"
class EvidenceCandidateCatalogError(ValueError):
    """A candidate catalog violates the exact-evidence contract."""

    contract_code = "EVIDENCE_CANDIDATE_CATALOG_INVALID"


class UnknownEvidenceCandidateError(EvidenceCandidateCatalogError):
    """A provider-selected candidate ID is not in the current catalog."""

    contract_code = "EVIDENCE_CANDIDATE_UNKNOWN"


class EvidenceCandidateProjectionError(EvidenceCandidateCatalogError):
    """A bounded provider projection cannot satisfy its evidence contract."""

    contract_code = "PROMPT_BUDGET_UNSATISFIABLE"


@dataclass(frozen=True, slots=True)
class EvidenceCandidate:
    candidate_id: str
    ref: str
    kind: str
    episode_id: str
    ordinal: int
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        """Return the stable wire-shaped representation of this candidate."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvidenceCandidate":
        if set(value) != {"candidate_id", "ref", "kind", "episode_id", "ordinal", "excerpt"}:
            raise EvidenceCandidateCatalogError("candidate has invalid fields")
        return cls(
            candidate_id=value["candidate_id"],
            ref=value["ref"],
            kind=value["kind"],
            episode_id=value["episode_id"],
            ordinal=value["ordinal"],
            excerpt=value["excerpt"],
        )


def make_candidate_id(
    ref: str,
    excerpt: str,
    *,
    catalog_version: int = EVIDENCE_CANDIDATE_CATALOG_VERSION,
) -> str:
    """Create the deterministic ID for a ref/excerpt pair."""
    material = f"{catalog_version}\0{ref}\0{excerpt}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:EVIDENCE_CANDIDATE_ID_HEX_LENGTH]
    return f"{EVIDENCE_CANDIDATE_ID_PREFIX}{digest}"


def _decode_utf8(value: bytes | str, *, ref: str) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EvidenceCandidateCatalogError(f"artifact is not valid UTF-8: {ref}") from error
    if isinstance(value, str):
        return value
    raise TypeError(f"raw artifact must be bytes or str: {ref}")


def _raw_text(value: bytes | str | Path, *, ref: str) -> str:
    if isinstance(value, Path):
        return _decode_utf8(value.read_bytes(), ref=ref)
    return _decode_utf8(value, ref=ref)


def _safe_ref(ref: object) -> bool:
    if not isinstance(ref, str) or not ref or "\x00" in ref:
        return False
    if PurePosixPath(ref).is_absolute() or PureWindowsPath(ref).is_absolute():
        return False
    if re.match(r"^[A-Za-z]:", ref) or ref.startswith(("/", "\\")):
        return False
    normalized_parts = ref.replace("\\", "/").split("/")
    return all(part not in {"", ".", ".."} for part in normalized_parts)


def _metadata_for_ref(ref: str) -> tuple[str, str] | None:
    """Return (kind, episode_id) for a canonical ARC artifact ref."""
    parts = ref.split("/")
    if len(parts) == 3 and parts[0] == "episodes" and parts[1] and parts[2] in _KNOWN_KINDS:
        return _KNOWN_KINDS[parts[2]], parts[1]
    if len(parts) == 2 and parts[0] == "episode_sources" and parts[1].endswith(".json"):
        episode_id = parts[1][:-5]
        if episode_id:
            return "episode_source", episode_id
    if len(parts) == 2 and parts[0] == "transitions" and parts[1].endswith(".json"):
        transition_id = parts[1][:-5]
        source_id, separator, _next_id = transition_id.partition("_to_")
        if separator and source_id and _next_id:
            return "transition", source_id
    return None


def _validate_ref(ref: object, *, allowed_refs: set[str] | None) -> None:
    if not _safe_ref(ref):
        raise EvidenceCandidateCatalogError("candidate ref must be a safe relative path")
    assert isinstance(ref, str)
    if allowed_refs is not None:
        if ref not in allowed_refs:
            raise EvidenceCandidateCatalogError(f"candidate ref is not allowed: {ref}")
    elif _metadata_for_ref(ref) is None:
        raise EvidenceCandidateCatalogError(f"candidate ref is not a known ARC artifact: {ref}")


def _episode_metadata(ref: str, kind: str | None, episode_id: str | None) -> tuple[str, str]:
    known = _metadata_for_ref(ref)
    if known is not None:
        known_kind, known_episode_id = known
        if kind is not None and kind != known_kind:
            raise EvidenceCandidateCatalogError(f"kind does not match ref: {ref}")
        if episode_id is not None and episode_id != known_episode_id:
            raise EvidenceCandidateCatalogError(f"episode_id does not match ref: {ref}")
        return known_kind, known_episode_id
    if not isinstance(kind, str) or not kind.strip() or not isinstance(episode_id, str) or not episode_id.strip():
        raise EvidenceCandidateCatalogError("custom refs require kind and episode_id")
    return kind, episode_id


def _sentence_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split one non-newline span without changing any source characters."""
    spans: list[tuple[int, int]] = []
    sentence_start = start
    index = start
    while index < end:
        character = text[index]
        if character in _SENTENCE_ENDINGS:
            if character == "." and ((index + 1 < end and text[index + 1] == ".") or (index > start and text[index - 1] == ".")):
                index += 1
                continue
            next_index = index + 1
            if next_index == end or text[next_index].isspace():
                spans.append((sentence_start, next_index))
                sentence_start = next_index
        index += 1
    if sentence_start < end:
        spans.append((sentence_start, end))
    return spans


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _split_long_span(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split a span at source word boundaries, with a hard maximum."""
    if end - start <= EVIDENCE_CANDIDATE_TARGET_MAX_CHARACTERS:
        return [(start, end)]

    result: list[tuple[int, int]] = []
    current = start
    while current < end:
        remaining = end - current
        if remaining <= EVIDENCE_CANDIDATE_TARGET_MAX_CHARACTERS:
            result.append((current, end))
            break

        limit = min(current + EVIDENCE_CANDIDATE_TARGET_MAX_CHARACTERS, end)
        boundary = None
        for match in re.finditer(r"\S+", text[current:limit]):
            candidate_end = current + match.end()
            if candidate_end - current <= EVIDENCE_CANDIDATE_TARGET_MAX_CHARACTERS:
                boundary = candidate_end
        if boundary is None or boundary <= current:
            boundary = limit
        result.append((current, boundary))
        current = boundary
        while current < end and text[current].isspace():
            current += 1

    if len(result) > 1 and result[-1][1] - result[-1][0] < EVIDENCE_CANDIDATE_MIN_CHARACTERS:
        previous_start, _previous_end = result[-2]
        result[-2] = (previous_start, result[-1][1])
        result.pop()
    return result


def _prose_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    # Process each non-newline run independently so generation never reassembles a quote across lines.
    for match in re.finditer(r"[^\r\n]+", text):
        line_start, line_end = match.span()
        for sentence_start, sentence_end in _sentence_spans(text, line_start, line_end):
            trimmed = _trimmed_span(text, sentence_start, sentence_end)
            if trimmed is None:
                continue
            spans.extend(_split_long_span(text, *trimmed))
    return spans


def _make_candidates(text: str, ref: str, kind: str, episode_id: str, spans: Sequence[tuple[int, int]]) -> list[EvidenceCandidate]:
    excerpts: list[str] = []
    for start, end in spans:
        excerpt = text[start:end]
        if excerpt.strip() and EVIDENCE_CANDIDATE_MIN_CHARACTERS <= len(excerpt) <= EVIDENCE_CANDIDATE_MAX_CHARACTERS and excerpt in text:
            if excerpt not in excerpts:
                excerpts.append(excerpt)
    return [
        EvidenceCandidate(make_candidate_id(ref, excerpt), ref, kind, episode_id, ordinal, excerpt)
        for ordinal, excerpt in enumerate(excerpts)
    ]


def generate_prose_candidates(
    raw_text: bytes | str,
    *,
    ref: str,
    kind: str | None = None,
    episode_id: str | None = None,
) -> list[EvidenceCandidate]:
    """Generate candidates from raw prose without normalizing its characters."""
    _validate_ref(ref, allowed_refs=None)
    resolved_kind, resolved_episode_id = _episode_metadata(ref, kind, episode_id)
    text = _decode_utf8(raw_text, ref=ref)
    return _make_candidates(text, ref, resolved_kind, resolved_episode_id, _prose_spans(text))


def _walk_string_leaves(value: Any) -> list[str]:
    leaves: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            leaves.extend(_walk_string_leaves(value[key]))
    elif isinstance(value, list):
        for item in value:
            leaves.extend(_walk_string_leaves(item))
    elif isinstance(value, str):
        leaves.append(value)
    return leaves


def generate_json_candidates(
    raw_json: bytes | str,
    *,
    ref: str,
    kind: str | None = None,
    episode_id: str | None = None,
) -> list[EvidenceCandidate]:
    """Generate candidates only from decoded string leaves present verbatim in raw JSON text."""
    _validate_ref(ref, allowed_refs=None)
    resolved_kind, resolved_episode_id = _episode_metadata(ref, kind, episode_id)
    raw_text = _decode_utf8(raw_json, ref=ref)
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise EvidenceCandidateCatalogError(f"invalid JSON artifact: {ref}") from error

    candidates: list[EvidenceCandidate] = []
    seen: set[str] = set()
    for leaf in _walk_string_leaves(value):
        if not leaf.strip() or leaf in seen or leaf not in raw_text:
            continue
        seen.add(leaf)
        for candidate in _make_candidates(leaf, ref, resolved_kind, resolved_episode_id, _prose_spans(leaf)):
            if candidate.excerpt in raw_text:
                candidates.append(candidate)
    return _reordinal(candidates)


def _reordinal(candidates: Sequence[EvidenceCandidate]) -> list[EvidenceCandidate]:
    result: list[EvidenceCandidate] = []
    seen: set[tuple[str, str]] = set()
    by_ref: dict[str, list[EvidenceCandidate]] = {}
    for candidate in candidates:
        key = (candidate.ref, candidate.excerpt)
        if key not in seen:
            seen.add(key)
            by_ref.setdefault(candidate.ref, []).append(candidate)
    for ref in sorted(by_ref):
        for ordinal, candidate in enumerate(by_ref[ref]):
            result.append(EvidenceCandidate(candidate.candidate_id, candidate.ref, candidate.kind, candidate.episode_id, ordinal, candidate.excerpt))
    return result


def _artifact_input(value: bytes | str | Path, *, ref: str) -> tuple[str, str]:
    raw_text = _raw_text(value, ref=ref)
    if ref.endswith(".json"):
        kind, episode_id = _metadata_for_ref(ref) or (None, None)
        if kind is None:
            raise EvidenceCandidateCatalogError(f"JSON ref is not a known ARC artifact: {ref}")
        return raw_text, "json"
    return raw_text, "prose"


def build_evidence_candidate_catalog(
    artifacts: Mapping[str, bytes | str | Path],
    *,
    allowed_refs: Sequence[str] | None = None,
    require_candidate_per_ref: bool = True,
) -> list[EvidenceCandidate]:
    """Build and validate one deterministic catalog from raw artifact inputs."""
    allowed = set(allowed_refs) if allowed_refs is not None else None
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise EvidenceCandidateCatalogError("candidate catalog requires artifacts")
    for ref in artifacts:
        _validate_ref(ref, allowed_refs=allowed)

    all_candidates: list[EvidenceCandidate] = []
    raw_texts: dict[str, str] = {}
    for ref in sorted(artifacts):
        raw_text, artifact_type = _artifact_input(artifacts[ref], ref=ref)
        raw_texts[ref] = raw_text
        kind, episode_id = _metadata_for_ref(ref) or (None, None)
        if kind is None:
            raise EvidenceCandidateCatalogError(f"custom refs require an explicit generator call: {ref}")
        if artifact_type == "json":
            all_candidates.extend(generate_json_candidates(raw_text, ref=ref, kind=kind, episode_id=episode_id))
        else:
            all_candidates.extend(generate_prose_candidates(raw_text, ref=ref, kind=kind, episode_id=episode_id))

    catalog = _reordinal(all_candidates)
    validate_catalog(catalog, raw_texts, allowed_refs=allowed, require_candidate_per_ref=require_candidate_per_ref)
    return catalog


def generate_candidate_catalog(
    artifacts: Mapping[str, bytes | str | Path],
    *,
    allowed_refs: Sequence[str] | None = None,
    require_candidate_per_ref: bool = True,
) -> list[EvidenceCandidate]:
    """Public alias for build_evidence_candidate_catalog."""
    return build_evidence_candidate_catalog(artifacts, allowed_refs=allowed_refs, require_candidate_per_ref=require_candidate_per_ref)


def _projection_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_bounded_candidate_projection(
    full_catalog: Sequence[EvidenceCandidate | Mapping[str, Any]],
    required_refs: Sequence[str],
    eligible_kinds: Sequence[str] | None,
    available_character_budget: int,
) -> dict[str, list[dict[str, Any]]]:
    """Build a deterministic compact candidate view for one provider prompt."""
    if not isinstance(available_character_budget, int) or isinstance(available_character_budget, bool) or available_character_budget < 0:
        raise EvidenceCandidateProjectionError("candidate projection budget is invalid")
    normalized = [_candidate(value) for value in full_catalog]
    allowed_kinds = set(eligible_kinds) if eligible_kinds is not None else None
    required = list(dict.fromkeys(required_refs))
    by_ref: dict[str, list[EvidenceCandidate]] = {}
    for candidate in sorted(normalized, key=lambda item: (item.ref, item.ordinal)):
        if allowed_kinds is None or candidate.kind in allowed_kinds:
            by_ref.setdefault(candidate.ref, []).append(candidate)
    missing = [ref for ref in required if ref not in by_ref]
    if missing:
        raise EvidenceCandidateProjectionError(f"required evidence refs have no eligible candidates: {missing}")

    def priority(items: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        positions = []
        for position in (0, len(items) // 2, len(items) - 1):
            if position not in positions:
                positions.append(position)
        # After first/middle/last, keep every selection prefix evenly spaced over
        # the full ordinal range by bisecting the widest remaining gap.
        chosen = sorted(positions)
        while len(positions) < len(items):
            gap = max(((right - left, -left) for left, right in zip(chosen, chosen[1:]) if right - left > 1), default=None)
            if gap is None:
                break
            middle = -gap[1] + gap[0] // 2
            positions.append(middle)
            chosen.append(middle)
            chosen.sort()
        return [items[position] for position in positions]

    # Only refs required by the dimension/stage enter the provider view. This keeps
    # the canonical full catalog available for later materialization and audit.
    ordered_refs = sorted(required)
    ordered_candidates = {ref: priority(by_ref[ref]) for ref in ordered_refs}
    selected: list[EvidenceCandidate] = [ordered_candidates[ref][0] for ref in ordered_refs]

    def projection(items: Sequence[EvidenceCandidate]) -> dict[str, list[dict[str, Any]]]:
        refs = sorted({candidate.ref for candidate in items})
        ref_ids = {ref: f"R{index:02d}" for index, ref in enumerate(refs)}
        return {
            "evidence_ref_catalog": [
                {"ref_id": ref_ids[ref], "ref": ref, "kind": by_ref[ref][0].kind, "episode_id": by_ref[ref][0].episode_id}
                for ref in refs
            ],
            "evidence_candidates": [
                {"candidate_id": candidate.candidate_id, "ref_id": ref_ids[candidate.ref], "ordinal": candidate.ordinal, "excerpt": candidate.excerpt}
                for candidate in sorted(items, key=lambda item: (item.ref, item.ordinal))
            ],
        }

    if len(_projection_json(projection(selected))) > available_character_budget:
        raise EvidenceCandidateProjectionError("required candidate projection exceeds its prompt budget")

    selected_ids = {candidate.candidate_id for candidate in selected}
    # Add candidates in round-robin order, with first/middle/last positions first.
    for position in range(1, max((len(items) for items in ordered_candidates.values()), default=0)):
        for ref in ordered_refs:
            candidates = ordered_candidates[ref]
            if position >= len(candidates):
                continue
            candidate = candidates[position]
            if candidate.candidate_id in selected_ids:
                continue
            proposed = selected + [candidate]
            if len(_projection_json(projection(proposed))) > available_character_budget:
                continue
            selected.append(candidate)
            selected_ids.add(candidate.candidate_id)
    return projection(selected)


def catalog_bytes(catalog: Sequence[EvidenceCandidate | Mapping[str, Any]]) -> bytes:
    """Serialize a catalog using stable JSON bytes for determinism checks."""
    values = [_candidate(value).to_dict() for value in catalog]
    return (json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _candidate(value: EvidenceCandidate | Mapping[str, Any]) -> EvidenceCandidate:
    if isinstance(value, EvidenceCandidate):
        return value
    if isinstance(value, Mapping):
        return EvidenceCandidate.from_dict(value)
    raise EvidenceCandidateCatalogError("catalog item must be an EvidenceCandidate")


def validate_catalog(
    catalog: Sequence[EvidenceCandidate | Mapping[str, Any]],
    raw_artifacts: Mapping[str, bytes | str | Path],
    *,
    allowed_refs: Sequence[str] | None = None,
    catalog_version: int = EVIDENCE_CANDIDATE_CATALOG_VERSION,
    require_candidate_per_ref: bool = True,
) -> list[EvidenceCandidate]:
    """Enforce every catalog invariant against raw UTF-8 artifact bytes."""
    if not isinstance(catalog, Sequence) or isinstance(catalog, (str, bytes)):
        raise EvidenceCandidateCatalogError("catalog must be a sequence")
    allowed = set(allowed_refs) if allowed_refs is not None else None
    raw_texts = {ref: _raw_text(value, ref=ref) for ref, value in raw_artifacts.items()}
    normalized = [_candidate(value) for value in catalog]
    ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    by_ref: dict[str, list[EvidenceCandidate]] = {}
    for candidate in normalized:
        _validate_ref(candidate.ref, allowed_refs=allowed)
        if candidate.ref not in raw_texts:
            raise EvidenceCandidateCatalogError(f"candidate ref has no raw artifact: {candidate.ref}")
        if not isinstance(candidate.candidate_id, str) or not re.fullmatch(r"EC_[0-9a-f]{12,16}", candidate.candidate_id):
            raise EvidenceCandidateCatalogError("candidate ID has invalid shape")
        if candidate.candidate_id in ids:
            raise EvidenceCandidateCatalogError("candidate IDs must be unique")
        ids.add(candidate.candidate_id)
        pair = (candidate.ref, candidate.excerpt)
        if pair in pairs:
            raise EvidenceCandidateCatalogError("(ref, excerpt) pairs must be unique")
        pairs.add(pair)
        if not isinstance(candidate.kind, str) or not candidate.kind.strip() or not isinstance(candidate.episode_id, str) or not candidate.episode_id.strip():
            raise EvidenceCandidateCatalogError("candidate metadata must be non-blank strings")
        expected_metadata = _metadata_for_ref(candidate.ref)
        if expected_metadata is not None and (candidate.kind, candidate.episode_id) != expected_metadata:
            raise EvidenceCandidateCatalogError("candidate metadata does not match ref")
        if not isinstance(candidate.ordinal, int) or isinstance(candidate.ordinal, bool) or candidate.ordinal < 0:
            raise EvidenceCandidateCatalogError("candidate ordinal must be a non-negative integer")
        if not isinstance(candidate.excerpt, str) or not candidate.excerpt.strip():
            raise EvidenceCandidateCatalogError("candidate excerpt must be non-blank")
        if not EVIDENCE_CANDIDATE_MIN_CHARACTERS <= len(candidate.excerpt) <= EVIDENCE_CANDIDATE_MAX_CHARACTERS:
            raise EvidenceCandidateCatalogError("candidate excerpt length is outside 8-400")
        if candidate.excerpt not in raw_texts[candidate.ref]:
            raise EvidenceCandidateCatalogError("candidate excerpt is not an exact raw substring")
        if make_candidate_id(candidate.ref, candidate.excerpt, catalog_version=catalog_version) != candidate.candidate_id:
            raise EvidenceCandidateCatalogError("candidate ID does not match ref and excerpt")
        by_ref.setdefault(candidate.ref, []).append(candidate)

    for ref, items in by_ref.items():
        ordinals = sorted(item.ordinal for item in items)
        if ordinals not in (list(range(len(items))), list(range(1, len(items) + 1))):
            raise EvidenceCandidateCatalogError(f"ordinals are not continuous for {ref}")
    if require_candidate_per_ref:
        missing = set(raw_texts) - set(by_ref)
        if missing:
            raise EvidenceCandidateCatalogError(f"artifact has no evidence candidate: {sorted(missing)}")
    expected_order = sorted(normalized, key=lambda item: (item.ref, item.ordinal))
    if normalized != expected_order:
        raise EvidenceCandidateCatalogError("catalog order is not deterministic")
    return normalized


def lookup_candidate(
    catalog: Sequence[EvidenceCandidate | Mapping[str, Any]],
    candidate_id: str,
) -> EvidenceCandidate:
    """Look up an ID only in the supplied catalog and reject unknown IDs."""
    candidates = [_candidate(value) for value in catalog]
    matches = [candidate for candidate in candidates if candidate.candidate_id == candidate_id]
    if len(matches) != 1:
        raise UnknownEvidenceCandidateError(f"unknown evidence candidate ID: {candidate_id}")
    return matches[0]


def materialize_candidate_ids(
    candidate_ids: Sequence[str],
    catalog: Sequence[EvidenceCandidate | Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Materialize selected IDs to canonical ref/excerpt evidence in catalog order, deduplicating deterministically."""
    selected = {candidate_id for candidate_id in candidate_ids}
    candidates = [_candidate(value) for value in catalog]
    found = [candidate for candidate in candidates if candidate.candidate_id in selected]
    if len(found) != len(selected):
        unknown = sorted(selected - {candidate.candidate_id for candidate in candidates})
        raise UnknownEvidenceCandidateError(f"unknown evidence candidate ID: {unknown[0] if unknown else ''}")
    return [{"ref": candidate.ref, "excerpt": candidate.excerpt} for candidate in found]
