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

- G1. WORLD_CORE 승인. `kingdom_archive` World Version 1.0은 사용자 승인으로 완료됐으며, `arc status`는 `WORLD_READY`를 표시한다. 다른 프로젝트의 G1 계약은 그대로 사용자 승인을 요구한다.
- G2. 에피소드 후보 선택 또는 폐기.
- G3. 최종 대본 제작 승인.
- G4. 실제 콘텐츠 공개 및 canon 반영 승인.

자동 코드는 승인 기록을 만들거나 이 결정을 대신하지 않는다.

## 설정 생명주기

`DRAFT`, `PROVISIONAL`, `CANON`, `BELIEVED`, `CONTESTED`, `RUMOR`, `HIDDEN`, `OPEN`을 사용한다. `PUBLISHED` 이전의 `CANON` 승격과 `HOLD` 또는 `REJECTED` 에피소드의 canon delta 적용은 거부한다.

## 산출물 계약

에피소드 디렉터리는 `projects/<project>/episodes/<episode-id>/`이며, 상태별 필요 파일은 `arc.artifacts.STATE_ARTIFACTS`가 정의한다. `arc status`는 현재 상태, 누락 파일, 다음 허용 상태를 표시한다.

## ARC-1 E001 fixture 수직 흐름

`tests/fixtures/arc1/`은 《왕국 기록보관소》의 짧은 결정론적 입력이다. `arc episode create E001 --scenario <pass|rewrite|hold|soft|hard>`는 fixture를 작업 산출물과 구분된 `projects/kingdom_archive/episodes/E001/`에 복사해 시작한다.

- PASS. G1, G2 뒤 `REVIEW_1 PASS`, `CONTINUITY_CHECKED(CLEAR)`, G3를 거쳐 `PRODUCTION_READY`에 도달한다.
- REWRITE. `REVIEW_1 REWRITE` 뒤 한 번만 `REVISED → REVIEW_2 PASS`를 허용한다.
- HOLD. 두 번째 리뷰 FAIL 또는 HARD_CONFLICT는 `HOLD`가 되며 자동 진행하지 않는다.
- SOFT_CONFLICT. `continuity_check.json`의 충돌 근거를 보존한 채 `AWAITING_APPROVAL`로 진행한다.

`arc approve G1_WORLD_CORE`, `arc approve E001 G2_EPISODE_SELECTION`, `arc approve E001 G3_FINAL_SCRIPT_PRODUCTION`으로만 승인 기록을 만든다. 같은 승인은 안전하게 재실행되어 `already recorded`를 반환한다. `arc episode advance E001`은 한 단계, `arc episode run E001`은 승인 또는 차단 지점까지 진행하고, `arc episode status E001`은 상태·누락 산출물·차단 이유를 표시한다.

canon delta는 `canon_delta.json` 후보로만 생성한다. ARC-1은 ledger를 수정하지 않는다.

## ARC-3 G2 pitch 선택

G1 완료 뒤 `arc pitch import`는 world version과 ledger 참조를 검증한 정확히 5개의 외부 후보 batch를 `pitches/<batch-id>/`에 보존한다. `arc pitch list`는 간결한 후보 요약과 경고를 표시하며 자동 선택하지 않는다. `arc pitch select <batch-id> <pitch-id> --episode E001`은 사용자 선택으로 G2를 기록하고 E001을 `SELECTED`로 만든다. 이 과정은 pitch source와 선택 기록만 만들며 ledger를 변경하지 않는다.
