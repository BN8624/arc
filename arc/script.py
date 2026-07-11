# 실제 E001의 story gate와 대본 초안을 안전하게 가져온다.

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from .project import validate_world_core
from .states import EpisodeState
from .validation import ValidationError, validate_transition

REQUIRED_CHECKS = {"standalone_comprehension", "protagonist_choice", "human_conflict", "ending_answers_opening_conflict", "worldbuilding_not_substitute_for_story", "central_mystery_protection", "production_feasibility"}
ALLOWED_SPEAKERS = {"세라", "로엔", "베른", "탈렌"}
NON_SPEAKER_LABELS = {"화면", "화면 자막", "음향"}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value, dict): raise ValidationError("JSON object required")
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_gate(project_root: Path, episode_id: str, gate: dict, source: dict) -> None:
    version = validate_world_core(project_root)
    if gate.get("schema_version") != 1 or gate.get("project_id") != "kingdom_archive" or gate.get("world_version") != version or gate.get("episode_id") != episode_id:
        raise ValidationError("story gate identity mismatch")
    gate_source = gate.get("source", {})
    if gate_source.get("batch_id") != source.get("batch_id") or gate_source.get("pitch_id") != source.get("pitch_id") or gate_source.get("outline_artifact") != "outline.md":
        raise ValidationError("story gate source mismatch")
    if gate.get("verdict") != "PASS" or gate.get("blocking_issues") != [] or gate.get("next_allowed_artifact") != "script_draft.md" or gate.get("canon_effect") != "NONE":
        raise ValidationError("story gate verdict or effects are invalid")
    checks = gate.get("checks", {})
    if not REQUIRED_CHECKS.issubset(checks) or any(checks[key].get("result") != "PASS" for key in REQUIRED_CHECKS):
        raise ValidationError("story gate checks are invalid")
    directives = gate.get("mandatory_script_directives", [])
    ids = [item.get("id") for item in directives]
    if len(ids) != len(set(ids)):
        raise ValidationError("duplicate mandatory script directive ID")


def _validate_script(path: Path) -> None:
    try: content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error: raise ValidationError("script must be UTF-8") from error
    required = ("E001", "## 제작 기준", "## 초안 자기검사", "수신자 명부", "비공식")
    if not content.strip() or any(marker not in content for marker in required) or any(f"## 장면 {index}" not in content for index in range(8)):
        raise ValidationError("script required sections are missing")
    labels = set(re.findall(r"^\[([^\]]+)\]", content, flags=re.MULTILINE))
    speakers = labels - NON_SPEAKER_LABELS
    if not labels.issubset(ALLOWED_SPEAKERS | NON_SPEAKER_LABELS) or speakers != ALLOWED_SPEAKERS:
        raise ValidationError("script speakers are invalid")
    forbidden = ("왕국의 고유 이름:", "정확한 연도:", "CANON으로 승격", "ledger에 반영 지시")
    if any(token in content for token in forbidden):
        raise ValidationError("script contains a forbidden fixed setting or canon instruction")


def import_script(project_root: Path, episode_id: str, gate_path: Path, script_path: Path) -> bool:
    root = project_root / "episodes" / episode_id
    manifest_path = root / "episode.json"; manifest = _read_json(manifest_path); source = _read_json(root / "pitch_source.json")
    if manifest.get("project_id") != "kingdom_archive" or manifest.get("episode_id") != episode_id: raise ValidationError("episode manifest identity mismatch")
    gate = _read_json(gate_path); _validate_gate(project_root, episode_id, gate, source); _validate_script(script_path)
    gate_target, script_target = root / "story_gate.json", root / "script_draft.md"; state = EpisodeState(manifest.get("state"))
    if state is EpisodeState.SCRIPT_DRAFT:
        if gate_target.exists() and script_target.exists() and _hash(gate_target) == _hash(gate_path) and _hash(script_target) == _hash(script_path): return False
        raise ValidationError("SCRIPT_DRAFT input differs from existing artifacts")
    if state is not EpisodeState.OUTLINE_READY: raise ValidationError("script import requires OUTLINE_READY state")
    if gate_target.exists() or script_target.exists(): raise ValidationError("script artifacts already exist")
    prohibited = ("review_1.json", "script_revised.md", "review_2.json", "continuity_check.json", "script_final.md", "canon_delta.json", "production_packet")
    if any((root / item).exists() for item in prohibited): raise ValidationError("later episode artifacts already exist")
    validate_transition(EpisodeState.OUTLINE_READY, EpisodeState.SCRIPT_DRAFT)
    temporary = Path(tempfile.mkdtemp(prefix=".script-", dir=root))
    try:
        shutil.copyfile(gate_path, temporary / "story_gate.json"); shutil.copyfile(script_path, temporary / "script_draft.md")
        os.replace(temporary / "story_gate.json", gate_target); os.replace(temporary / "script_draft.md", script_target)
        manifest["state"] = EpisodeState.SCRIPT_DRAFT.value; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return True
