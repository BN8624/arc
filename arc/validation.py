# 상태 전이와 설정 생명주기 제약을 fail-closed로 검증한다.

from .states import ApprovalGate, EpisodeState, FactLifecycle, REQUIRED_GATE, TRANSITIONS


class ValidationError(ValueError):
    """ARC 계약을 위반한 요청이다."""


def validate_transition(
    current: EpisodeState,
    target: EpisodeState,
    approvals: set[ApprovalGate] | frozenset[ApprovalGate] = frozenset(),
) -> None:
    if target not in TRANSITIONS[current]:
        raise ValidationError(f"허용되지 않은 상태 전이: {current} -> {target}")
    required_gate = REQUIRED_GATE.get((current, target))
    if required_gate and required_gate not in approvals:
        raise ValidationError(f"사용자 승인 필요: {required_gate}")


def validate_world_core_finalisation(approvals: set[ApprovalGate] | frozenset[ApprovalGate]) -> None:
    if ApprovalGate.G1_WORLD_CORE not in approvals:
        raise ValidationError("사용자 승인 필요: G1_WORLD_CORE")


def validate_fact_lifecycle(current: FactLifecycle, target: FactLifecycle, episode_state: EpisodeState) -> None:
    if target is FactLifecycle.CANON and episode_state is not EpisodeState.PUBLISHED and episode_state is not EpisodeState.CANON_UPDATED:
        raise ValidationError("PUBLISHED 이전에는 CANON으로 승격할 수 없습니다.")
    if current is FactLifecycle.CANON and target is FactLifecycle.DRAFT:
        raise ValidationError("CANON을 DRAFT로 되돌릴 수 없습니다.")


def validate_canon_delta_application(episode_state: EpisodeState) -> None:
    if episode_state in {EpisodeState.HOLD, EpisodeState.REJECTED}:
        raise ValidationError(f"{episode_state} 에피소드에는 canon_delta를 적용할 수 없습니다.")
    if episode_state is not EpisodeState.PUBLISHED and episode_state is not EpisodeState.CANON_UPDATED:
        raise ValidationError("PUBLISHED 이전에는 canon_delta를 적용할 수 없습니다.")
