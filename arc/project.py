# 프로젝트 매니페스트를 생성하고 읽는다.

import json
from pathlib import Path

PROJECT_MANIFEST = {"schema_version": 1, "project": "kingdom_archive", "world_core_gate": "G1_WORLD_CORE", "approvals": []}
SERIES_STATE = {"schema_version": 1, "episodes": {}}
CONTINUITY_LEDGER = {"schema_version": 1, "facts": []}


def write_json_if_missing(path: Path, value: dict) -> bool:
    if path.exists():
        return False
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def write_text_if_missing(path: Path, value: str) -> bool:
    if path.exists():
        return False
    path.write_text(value, encoding="utf-8")
    return True


def initialise_project(project_root: Path) -> list[Path]:
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "episodes").mkdir(exist_ok=True)
    created: list[Path] = []
    files = {
        "project.json": (write_json_if_missing, PROJECT_MANIFEST),
        "SERIES_STATE.json": (write_json_if_missing, SERIES_STATE),
        "CONTINUITY_LEDGER.json": (write_json_if_missing, CONTINUITY_LEDGER),
        "WORLD_CORE.md": (write_text_if_missing, "# WORLD_CORE\n\n> G1 사용자 승인 전에는 내용을 작성하거나 확정하지 않습니다.\n"),
        "ARCHIVE_RULES.md": (write_text_if_missing, "# ARCHIVE_RULES\n\n> 프로젝트별 기록 규칙을 여기에 작성합니다.\n"),
    }
    for name, (writer, value) in files.items():
        path = project_root / name
        if writer(path, value):
            created.append(path)
    return created
