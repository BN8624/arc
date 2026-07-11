# 에피소드 상태별 산출물 경로 계약을 제공한다.

from pathlib import Path

from .states import EpisodeState


STATE_ARTIFACTS: dict[EpisodeState, tuple[str, ...]] = {
    EpisodeState.PITCHED: ("episode.json", "pitch.md"),
    EpisodeState.SELECTED: ("episode.json", "pitch.md", "selection.json"),
    EpisodeState.OUTLINE_READY: ("episode.json", "pitch.md", "selection.json", "continuity_plan.json", "outline.md"),
    EpisodeState.SCRIPT_DRAFT: ("episode.json", "pitch.md", "selection.json", "continuity_plan.json", "outline.md", "script_draft.md"),
    EpisodeState.REVIEW_1: ("episode.json", "script_draft.md", "review_1.json"),
    EpisodeState.REVISED: ("episode.json", "script_revised.md"),
    EpisodeState.REVIEW_2: ("episode.json", "script_revised.md", "review_2.json"),
    EpisodeState.CONTINUITY_CHECKED: ("episode.json", "continuity_check.json"),
    EpisodeState.AWAITING_APPROVAL: ("episode.json", "script_final.md", "continuity_check.json"),
    EpisodeState.PRODUCTION_READY: ("episode.json", "script_final.md", "production_packet/"),
    EpisodeState.PUBLISHED: ("episode.json", "production_packet/", "publication.json"),
    EpisodeState.CANON_UPDATED: ("episode.json", "canon_delta.json", "publication.json"),
    EpisodeState.HOLD: ("episode.json",),
    EpisodeState.REJECTED: ("episode.json",),
}


def episode_directory(project_root: Path, episode_id: str) -> Path:
    return project_root / "episodes" / episode_id


def required_artifacts(state: EpisodeState) -> tuple[str, ...]:
    return STATE_ARTIFACTS[state]


def missing_artifacts(episode_root: Path, state: EpisodeState) -> list[str]:
    return [item for item in required_artifacts(state) if not (episode_root / item).exists()]
