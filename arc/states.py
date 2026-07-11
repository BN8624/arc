# 에피소드 상태와 승인 게이트의 허용 전이를 정의한다.

from enum import StrEnum


class EpisodeState(StrEnum):
    PITCHED = "PITCHED"
    SELECTED = "SELECTED"
    OUTLINE_READY = "OUTLINE_READY"
    SCRIPT_DRAFT = "SCRIPT_DRAFT"
    REVIEW_1 = "REVIEW_1"
    REVISED = "REVISED"
    REVIEW_2 = "REVIEW_2"
    CONTINUITY_CHECKED = "CONTINUITY_CHECKED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PRODUCTION_READY = "PRODUCTION_READY"
    PUBLISHED = "PUBLISHED"
    CANON_UPDATED = "CANON_UPDATED"
    HOLD = "HOLD"
    REJECTED = "REJECTED"


class ApprovalGate(StrEnum):
    G1_WORLD_CORE = "G1_WORLD_CORE"
    G2_EPISODE_SELECTION = "G2_EPISODE_SELECTION"
    G3_FINAL_SCRIPT_PRODUCTION = "G3_FINAL_SCRIPT_PRODUCTION"
    G4_PUBLICATION_AND_CANON = "G4_PUBLICATION_AND_CANON"


class FactLifecycle(StrEnum):
    DRAFT = "DRAFT"
    PROVISIONAL = "PROVISIONAL"
    CANON = "CANON"
    BELIEVED = "BELIEVED"
    CONTESTED = "CONTESTED"
    RUMOR = "RUMOR"
    HIDDEN = "HIDDEN"
    OPEN = "OPEN"


TRANSITIONS: dict[EpisodeState, frozenset[EpisodeState]] = {
    EpisodeState.PITCHED: frozenset({EpisodeState.SELECTED, EpisodeState.REJECTED}),
    EpisodeState.SELECTED: frozenset({EpisodeState.OUTLINE_READY}),
    EpisodeState.OUTLINE_READY: frozenset({EpisodeState.SCRIPT_DRAFT}),
    EpisodeState.SCRIPT_DRAFT: frozenset({EpisodeState.REVIEW_1}),
    EpisodeState.REVIEW_1: frozenset({EpisodeState.REVISED, EpisodeState.CONTINUITY_CHECKED, EpisodeState.HOLD}),
    EpisodeState.REVISED: frozenset({EpisodeState.REVIEW_2}),
    EpisodeState.REVIEW_2: frozenset({EpisodeState.CONTINUITY_CHECKED, EpisodeState.HOLD}),
    EpisodeState.CONTINUITY_CHECKED: frozenset({EpisodeState.AWAITING_APPROVAL, EpisodeState.HOLD}),
    EpisodeState.AWAITING_APPROVAL: frozenset({EpisodeState.PRODUCTION_READY, EpisodeState.HOLD, EpisodeState.REJECTED}),
    EpisodeState.PRODUCTION_READY: frozenset({EpisodeState.PUBLISHED}),
    EpisodeState.PUBLISHED: frozenset({EpisodeState.CANON_UPDATED}),
    EpisodeState.CANON_UPDATED: frozenset(),
    EpisodeState.HOLD: frozenset({EpisodeState.SELECTED, EpisodeState.REJECTED}),
    EpisodeState.REJECTED: frozenset(),
}

REQUIRED_GATE: dict[tuple[EpisodeState, EpisodeState], ApprovalGate] = {
    (EpisodeState.PITCHED, EpisodeState.SELECTED): ApprovalGate.G2_EPISODE_SELECTION,
    (EpisodeState.PITCHED, EpisodeState.REJECTED): ApprovalGate.G2_EPISODE_SELECTION,
    (EpisodeState.AWAITING_APPROVAL, EpisodeState.PRODUCTION_READY): ApprovalGate.G3_FINAL_SCRIPT_PRODUCTION,
    (EpisodeState.PRODUCTION_READY, EpisodeState.PUBLISHED): ApprovalGate.G4_PUBLICATION_AND_CANON,
    (EpisodeState.PUBLISHED, EpisodeState.CANON_UPDATED): ApprovalGate.G4_PUBLICATION_AND_CANON,
}
