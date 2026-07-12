# Routing v2 운영 파일의 exact allowlist 경계를 검증한다.
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc.storage import StorageError, verify_artifacts, write_json


def manifest() -> dict:
    return {"artifact_hashes": {}}


def test_exact_operational_files_are_allowed(tmp_path: Path) -> None:
    write_json(tmp_path / "manifest.json", manifest())
    for name in ("planning_workers.partial.json", "review_workers.partial.json", "memory_workers.partial.json", "routing_state.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    verify_artifacts(tmp_path, manifest(), {"planning_workers.partial.json", "review_workers.partial.json", "memory_workers.partial.json", "routing_state.json"})


@pytest.mark.parametrize("name", ["unknown.partial.json", "planning_workers.partial.backup.json", "routing_state.old.json"])
def test_unknown_operational_file_is_rejected(tmp_path: Path, name: str) -> None:
    write_json(tmp_path / "manifest.json", manifest())
    (tmp_path / name).write_text("{}", encoding="utf-8")
    with pytest.raises(StorageError):
        verify_artifacts(tmp_path, manifest(), {"routing_state.json"})
