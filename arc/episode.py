# 에피소드 매니페스트의 최소 스키마를 제공한다.

from dataclasses import asdict, dataclass, field

from .states import ApprovalGate, EpisodeState


@dataclass(frozen=True)
class EpisodeManifest:
    episode_id: str
    state: EpisodeState = EpisodeState.PITCHED
    approvals: list[ApprovalGate] = field(default_factory=list)
    canon_delta: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self) | {"state": self.state.value, "approvals": [gate.value for gate in self.approvals]}
