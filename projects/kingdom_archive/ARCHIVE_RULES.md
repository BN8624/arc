# ARCHIVE_RULES

World Version: 1.0

## 세 개의 시간

- event_time. 사건이 실제로 일어난 시점.
- record_time. 기록이 작성되거나 남겨진 시점.
- release_order. 시청자에게 공개되는 순서.

세 값은 서로 일치할 필요가 없다.

## 기록 신뢰도

- VERIFIED. 복수의 독립 기록으로 확인.
- PLAUSIBLE. 상당한 근거가 있으나 확정 불가.
- DISPUTED. 기록들이 서로 충돌.
- PROPAGANDA. 선전 목적이 명백.
- FORGED_SUSPECTED. 위조 가능성이 큼.
- LEGEND. 민담·전승.
- UNKNOWN. 평가 불가.

## 설정 상태

`DRAFT`, `PROVISIONAL`, `CANON`, `BELIEVED`, `CONTESTED`, `RUMOR`, `HIDDEN`, `OPEN`을 사용한다.

## 정본 승격 규칙

초안 설정은 DRAFT, 최종 대본에 남은 설정은 PROVISIONAL이다. 실제 콘텐츠가 PUBLISHED되고 G4가 승인된 뒤에만 정본 반영이 가능하다. 공개된 기록이 거짓이더라도 기록의 존재 자체는 CANON이 될 수 있고, 내용의 진실 여부는 BELIEVED·CONTESTED·RUMOR로 별도 관리한다. HOLD 및 REJECTED 에피소드의 canon_delta 적용 금지.

## 모순 처리

모순을 자동 삭제하거나 하나의 정답으로 합치지 않는다. 실수로 생긴 모순은 HARD_CONFLICT, 관점·기록 차이로 설명 가능한 모순은 SOFT_CONFLICT, 의도적으로 유지하는 역사적 논쟁은 CONTESTED로 관리한다.
