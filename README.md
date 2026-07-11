# arc

`arc`는 V0 에피소드 제작 워크플로우의 상태·산출물·승인 계약을 검증하는 Python 골격입니다. 모델 호출, 창작 생성, 미디어 제작, 웹 UI, 데이터베이스는 포함하지 않습니다.

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
