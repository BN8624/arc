# Phase 1 산출물을 원자적으로 저장하고 무결성을 확인한다.
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


class StorageError(RuntimeError):
    """Run artifact integrity failed."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_json(path: Path, value: dict | list) -> str:
    data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write(path, data)
    return sha256_bytes(data)


def write_text(path: Path, text: str) -> str:
    data = text.encode("utf-8")
    atomic_write(path, data)
    return sha256_bytes(data)


def read_json(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_artifacts(run_dir: Path, manifest: dict, allowed_operational_files: set[str] | None = None) -> None:
    for name, expected in manifest["artifact_hashes"].items():
        path = run_dir / name
        if not path.exists() or sha256_file(path) != expected:
            raise StorageError(f"artifact hash mismatch: {name}")
    known = set(manifest["artifact_hashes"]) | {"manifest.json"} | (allowed_operational_files or set())
    actual = {path.name for path in run_dir.iterdir() if path.is_file() and not path.name.startswith(".")}
    if actual - known:
        raise StorageError(f"orphan artifact: {sorted(actual - known)}")
