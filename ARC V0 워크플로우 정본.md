# ARC V0 워크플로우 정본

## 1. 사람과 ARC의 역할

사람이 결정하는 지점은 네 번만 둔다.

| 승인 지점    | 사람이 결정하는 것             |
| -------- | ---------------------- |
| G1 세계 승인 | 세계관의 분위기·핵심 규칙·중심 미스터리 |
| G2 소재 선택 | 후보 5개 중 실제로 보고 싶은 이야기  |
| G3 대본 승인 | 제작할 가치가 있는 최종 대본인지     |
| G4 공개 확정 | 실제 콘텐츠가 완성됐고 역사에 반영할지  |

그 사이 작업은 ARC가 진행한다.

```text
세계관 최소 정본
→ 소재 5개 제안
→ 사용자 선택
→ 기존 역사와 연결 계획
→ 장면 개요
→ 이야기 게이트
→ 대본 초안
→ 비판적 리뷰
→ 필요 시 한 번만 재작성
→ 연속성 검사
→ 사용자 최종 승인
→ 제작 패킷
→ 콘텐츠 완성 확인
→ 역사 장부 반영
```

## 2. 에피소드 상태 흐름

```text
PITCHED
  ↓ 사용자 선택
SELECTED
  ↓
OUTLINE_READY
  ↓ 이야기 게이트
SCRIPT_DRAFT
  ↓
REVIEW_1
  ├─ PASS ───────────────┐
  ├─ REWRITE → REVISED → REVIEW_2
  └─ HOLD                │
                         ↓
CONTINUITY_CHECKED
  ├─ CLEAR
  ├─ SOFT_CONFLICT
  └─ HARD_CONFLICT → HOLD
                         ↓
AWAITING_APPROVAL
  ├─ 제작
  ├─ 보류
  └─ 폐기
       ↓
PRODUCTION_READY
       ↓ 사용자가 실제 완성 확인
PUBLISHED
       ↓
CANON_UPDATED
```

무한 수정 루프는 금지한다.

* 1차 리뷰에서 한 번만 전면 재작성 가능
* 2차 리뷰도 실패하면 `HOLD`
* `HOLD`는 실패작이 아니라 나중에 재활용 가능한 보관 상태
* 폐기된 이야기의 설정은 세계관에 반영하지 않음

## 3. 설정의 생명주기

대본에 등장했다고 바로 정본이 되면 안 된다.

```text
초안에서 새로 생긴 설정
→ DRAFT

최종 대본에 남은 설정
→ PROVISIONAL

실제 콘텐츠가 완성·공개됨
→ CANON / CONTESTED / RUMOR
```

확정된 사실과 등장인물의 주장을 분리한다.

* `CANON`: 세계에서 실제로 확정된 사실
* `BELIEVED`: 당대 사람들이 믿는 내용
* `CONTESTED`: 기록끼리 충돌하는 내용
* `RUMOR`: 소문·전설
* `HIDDEN`: 제작자만 아는 진실
* `OPEN`: 아직 결정하지 않은 영역

## 4. 프로젝트 파일 구조

사람이 읽는 내용은 Markdown, 상태와 관계는 JSON으로 나눈다.

```text
arc/
├── README.md
├── AGENTS.md
├── CLAUDE.md
├── docs/
│   ├── ARC_V0_WORKFLOW.md
│   └── ARC_V0_SCOPE.md
├── arc/
│   ├── project.py
│   ├── episode.py
│   ├── states.py
│   ├── artifacts.py
│   └── validation.py
├── projects/
│   └── kingdom_archive/
│       ├── project.json
│       ├── WORLD_CORE.md
│       ├── ARCHIVE_RULES.md
│       ├── CONTINUITY_LEDGER.json
│       ├── SERIES_STATE.json
│       └── episodes/
│           └── E001/
│               ├── episode.json
│               ├── pitch.md
│               ├── continuity_plan.json
│               ├── outline.md
│               ├── story_gate.json
│               ├── script_draft.md
│               ├── review_1.json
│               ├── script_revised.md
│               ├── review_2.json
│               ├── continuity_check.json
│               ├── script_final.md
│               ├── canon_delta.json
│               └── production_packet/
└── tests/
```

모든 파일을 항상 만들지는 않는다. 해당 단계에 도달했을 때만 생성한다.

## 5. V0 명령 구조

최종적으로는 이 정도만 있으면 된다.

```bash
arc init
arc pitch
arc select <pitch-id>
arc build <episode-id>
arc status
arc approve <episode-id>
arc publish <episode-id>
```

* `init`: 세계관 프로젝트 생성
* `pitch`: 후보 5개 생성
* `select`: 사용자 선택 기록
* `build`: 개요부터 연속성 검사까지 진행
* `status`: 현재 상태와 다음 승인 지점 표시
* `approve`: 최종 대본을 제작 대상으로 승인
* `publish`: 실제 완성된 콘텐츠의 설정을 정본에 반영

V0에서는 자동 업로드, 이미지 생성, 음성 생성, 영상 편집을 하지 않는다.

# 첫 구현 단계: ARC-0

처음부터 모델을 연결하지 않는다. 먼저 **상태·파일·검증 계약만 작동하는 결정론적 골격**을 만든다.

아래가 첫 구현 주문서다.

```md
# ARC-0 — Workflow Skeleton

## 목적

기존 Atelier를 사용하거나 이전하지 않고, 신규 프로젝트 `arc`의
V0 에피소드 제작 워크플로우 골격을 구현한다.

이번 단계에서는 LLM 호출, 이야기 생성, 이미지·음성·영상 제작을 하지 않는다.
상태 전이, 산출물 계약, 검증, CLI 골격만 만든다.

## 절대 조건

- 기존 Atelier 코드·문서·스키마를 복사하거나 참조하지 않는다.
- 프로젝트명, 저장소명, 패키지명, CLI 이름은 모두 `arc`로 통일한다.
- 범용 창작 플랫폼으로 확장하지 않는다.
- 데이터베이스, 웹 UI, 멀티에이전트, 자동 업로드를 추가하지 않는다.
- 구현 전에 아래 상태 흐름을 코드와 문서의 단일 정본으로 고정한다.

## 에피소드 상태

- PITCHED
- SELECTED
- OUTLINE_READY
- SCRIPT_DRAFT
- REVIEW_1
- REVISED
- REVIEW_2
- CONTINUITY_CHECKED
- AWAITING_APPROVAL
- PRODUCTION_READY
- PUBLISHED
- CANON_UPDATED
- HOLD
- REJECTED

허용되지 않은 상태 점프는 fail-closed로 거부한다.

## 사용자 승인 지점

- G1: WORLD_CORE 승인
- G2: 에피소드 후보 선택
- G3: 최종 대본 제작 승인
- G4: 실제 콘텐츠 공개 및 canon 반영 승인

자동 코드가 이 네 결정을 임의로 대신해서는 안 된다.

## 설정 생명주기

- DRAFT
- PROVISIONAL
- CANON
- BELIEVED
- CONTESTED
- RUMOR
- HIDDEN
- OPEN

PUBLISHED 이전에는 CANON으로 승격할 수 없다.

## 이번 구현 범위

1. 신규 Python 패키지 `arc`
2. 에피소드 상태 enum 및 전이 규칙
3. 프로젝트·에피소드 manifest 스키마
4. 산출물 경로 생성 규칙
5. 상태 전이 검증기
6. 사용자 승인 없이 다음 gate를 넘지 못하게 하는 검증
7. `arc init`, `arc status` CLI
8. fixture 기반 상태 전이 테스트
9. README 및 ARC_V0_WORKFLOW 문서
10. AGENTS.md와 CLAUDE.md를 byte-identical로 작성

## 생성할 첫 프로젝트

`projects/kingdom_archive/`

초기 파일:
- project.json
- WORLD_CORE.md
- ARCHIVE_RULES.md
- CONTINUITY_LEDGER.json
- SERIES_STATE.json
- episodes/

WORLD_CORE 내용은 이번 단계에서 작성하지 말고 템플릿만 만든다.

## 완료 조건

- 정상 상태 전이가 모두 테스트된다.
- 잘못된 상태 점프가 모두 거부된다.
- 승인 없는 gate 통과가 거부된다.
- PUBLISHED 이전 CANON 승격이 거부된다.
- 폐기·보류 에피소드의 canon_delta 적용이 거부된다.
- `arc init`이 동일 경로에서 재실행되어도 데이터를 손상하지 않는다.
- `arc status`가 현재 상태, 누락 산출물, 다음 허용 작업을 표시한다.
- 전체 테스트가 통과한다.

## 금지

- 실제 모델 API 연동
- 프롬프트 작성
- 이야기 후보 생성
- 영상 제작 기능
- 웹 서버 또는 대시보드
- SQLite 등 영속 DB
- 플러그인 시스템
- 기존 Atelier 마이그레이션
- 미래 확장을 위한 추상화

## 완료 보고

- 시작 HEAD / 종료 HEAD
- 변경 파일
- 구현된 상태 전이 목록
- 실행한 테스트와 결과
- 미구현·보류 사항
- git status
- commit / push 여부
```

ARC-0이 끝난 다음 단계는 **ARC-1: mock 데이터로 에피소드 한 편을 `PITCHED → PRODUCTION_READY`까지 실제 통과시키는 수직 검증**이다. 모델 연결은 그 수직 흐름이 안정된 뒤에 한다.
