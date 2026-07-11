# ARC V0 Workflow

이 문서는 ARC-0 상태 흐름의 단일 정본입니다.

## 에피소드 상태와 허용 전이

| 현재 상태 | 허용 다음 상태 |
| --- | --- |
| PITCHED | SELECTED(G2), REJECTED(G2) |
| SELECTED | OUTLINE_READY |
| OUTLINE_READY | SCRIPT_DRAFT |
| SCRIPT_DRAFT | REVIEW_1 |
| REVIEW_1 | REVISED, CONTINUITY_CHECKED, HOLD |
| REVISED | REVIEW_2 |
| REVIEW_2 | CONTINUITY_CHECKED, HOLD |
| CONTINUITY_CHECKED | AWAITING_APPROVAL, HOLD |
| AWAITING_APPROVAL | PRODUCTION_READY(G3), HOLD, REJECTED |
| PRODUCTION_READY | PUBLISHED(G4) |
| PUBLISHED | CANON_UPDATED(G4) |
| HOLD | SELECTED, REJECTED |
| CANON_UPDATED / REJECTED | 없음 |

나열되지 않은 전이는 거부한다.

## 사용자 승인

- G1. WORLD_CORE 승인. `validate_world_core_finalisation`은 이 승인 없이 WORLD_CORE 확정을 거부한다.
- G2. 에피소드 후보 선택 또는 폐기.
- G3. 최종 대본 제작 승인.
- G4. 실제 콘텐츠 공개 및 canon 반영 승인.

자동 코드는 승인 기록을 만들거나 이 결정을 대신하지 않는다.

## 설정 생명주기

`DRAFT`, `PROVISIONAL`, `CANON`, `BELIEVED`, `CONTESTED`, `RUMOR`, `HIDDEN`, `OPEN`을 사용한다. `PUBLISHED` 이전의 `CANON` 승격과 `HOLD` 또는 `REJECTED` 에피소드의 canon delta 적용은 거부한다.

## 산출물 계약

에피소드 디렉터리는 `projects/<project>/episodes/<episode-id>/`이며, 상태별 필요 파일은 `arc.artifacts.STATE_ARTIFACTS`가 정의한다. `arc status`는 현재 상태, 누락 파일, 다음 허용 상태를 표시한다.
