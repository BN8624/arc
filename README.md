# arc

`arc`는 멸망 후 137년의 기록보관소가 공개하는 다크 판타지 역사 미스터리 앤솔러지의 V0 워크플로우 골격입니다. 모델 호출, 창작 생성, 미디어 제작, 웹 UI, 데이터베이스는 포함하지 않습니다.

## 사용법

```bash
python -m pip install -e .
arc init
arc status
python -m unittest discover -s tests -v
```

`arc init`은 기본으로 `projects/kingdom_archive/`를 만들며 기존 파일을 덮어쓰지 않습니다.

## 범위

- 에피소드 상태 전이와 fail-closed 검증.
- G1~G4 사용자 승인 지점의 계약.
- JSON 매니페스트 및 상태별 산출물 경로 계약.
- 프로젝트 초기화와 상태 조회 CLI.

상세한 단일 정본은 [ARC_V0_WORKFLOW.md](docs/ARC_V0_WORKFLOW.md)입니다.

## E001 fixture 실행

아래는 결정론적 fixture로 직접 PASS 경로를 `PRODUCTION_READY`까지 진행한다.

```bash
arc init
arc approve G1_WORLD_CORE
arc episode create E001 --scenario pass
arc approve E001 G2_EPISODE_SELECTION
arc episode run E001
arc approve E001 G3_FINAL_SCRIPT_PRODUCTION
arc episode run E001
arc episode status E001
```

`--scenario rewrite`, `hold`, `soft`, `hard`는 각각 한 번 재작성, 두 번째 리뷰 실패, soft conflict, hard conflict 경로를 검증한다. `arc episode run`은 승인 또는 HOLD에서 멈추며, 기존 산출물을 덮어쓰지 않는다.

## 외부 pitch batch

외부에서 사람이 작성한 5개 후보 JSON을 검증·가져온 뒤 사용자가 하나를 고른다.

```bash
arc pitch import /path/to/pitch_set.json
arc pitch list <batch-id>
arc pitch select <batch-id> <pitch-id> --episode E001
```

import와 selection은 ledger를 변경하지 않는다. 후보의 이름과 설정은 DRAFT이며, 실제 첫 후보는 ARC 밖에서 작성해 가져온다.

## 실제 outline import

선택된 실제 에피소드에는 외부 continuity plan과 outline을 함께 가져온다.

```bash
arc episode outline-import E001 --plan /path/to/E001_continuity_plan.json --outline /path/to/E001_outline.md
```

명령은 `SELECTED` 상태와 batch·pitch identity를 검증하고, 두 원본을 그대로 저장한 뒤 `OUTLINE_READY`로 전환한다.

## 실제 story gate·대본 import

```bash
arc episode script-import E001 --gate /path/to/E001_story_gate.json --script /path/to/E001_script_draft.md
```

명령은 PASS gate와 대본의 최소 계약을 검증한 뒤 두 원본을 보존하고 `SCRIPT_DRAFT`로 전환한다.
