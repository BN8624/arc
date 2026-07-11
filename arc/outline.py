# 실제 E001의 continuity plan과 outline을 안전하게 가져온다.

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .project import validate_world_core
from .states import EpisodeState
from .validation import ValidationError, validate_transition

REQUIRED_OUTLINE_MARKERS = ("E001", "## 에피소드 약속", "## 이야기의 중심", "## 장면 구성", "## 이야기 게이트용 자체 점검", "## 대본 단계에서 금지할 것")
FORBIDDEN_OUTLINE_TOKENS = ("왕국의 고유 이름:", "정확한 연도:")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError("JSON object required")
    return value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_plan(project_root: Path, episode_id: str, plan: dict, source: dict) -> None:
    version = validate_world_core(project_root)
    if plan.get("schema_version") != 1 or plan.get("project_id") != "kingdom_archive" or plan.get("world_version") != version or plan.get("episode_id") != episode_id:
        raise ValidationError("continuity plan identity mismatch")
    plan_source = plan.get("source", {})
    if plan_source.get("batch_id") != source.get("batch_id") or plan_source.get("pitch_id") != source.get("pitch_id"):
        raise ValidationError("continuity plan source mismatch")
    if plan.get("status") != "DRAFT" or plan.get("time_model", {}).get("era_anchor") != "SCHISM":
        raise ValidationError("continuity plan status or era_anchor is invalid")
    ledger = _read_json(project_root / "CONTINUITY_LEDGER.json")
    known = {item["id"] for item in ledger["events"] + ledger["claims"]}
    refs = plan.get("existing_world_refs", [])
    if {item.get("id") for item in refs} != {"EV_SCHISM", "EV_SILENCE"} or any(item.get("id") not in known for item in refs):
        raise ValidationError("continuity plan world references are invalid")
    contribution = plan.get("history_contribution", {})
    if contribution.get("count") != 1 or contribution.get("intended_status") != "CONTESTED" or contribution.get("canon_effect_now") != "NONE":
        raise ValidationError("continuity plan history contribution is invalid")
    entities = plan.get("draft_entities", [])
    if not isinstance(entities, list) or any(item.get("status") != "DRAFT" for item in entities):
        raise ValidationError("continuity plan draft entities must be DRAFT")
    shape = plan.get("production_constraints", {})
    if not (5 <= shape.get("target_minutes", 0) <= 8 and shape.get("speaking_roles", 99) <= 4 and shape.get("primary_locations", 99) <= 3 and 8 <= shape.get("estimated_images", 0) <= 15):
        raise ValidationError("continuity plan production constraints are invalid")


def _validate_outline(path: Path) -> None:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("outline must be UTF-8") from error
    if not content.strip() or any(marker not in content for marker in REQUIRED_OUTLINE_MARKERS):
        raise ValidationError("outline required sections are missing")
    if any(f"### {index}." not in content for index in range(8)):
        raise ValidationError("outline scene markers 0 through 7 are required")
    if any(token in content for token in FORBIDDEN_OUTLINE_TOKENS):
        raise ValidationError("outline contains a forbidden fixed setting")


def import_outline(project_root: Path, episode_id: str, plan_path: Path, outline_path: Path) -> bool:
    root = project_root / "episodes" / episode_id
    manifest_path = root / "episode.json"
    manifest = _read_json(manifest_path)
    source = _read_json(root / "pitch_source.json")
    if manifest.get("project_id") != "kingdom_archive" or manifest.get("episode_id") != episode_id:
        raise ValidationError("episode manifest identity mismatch")
    plan = _read_json(plan_path)
    _validate_plan(project_root, episode_id, plan, source)
    _validate_outline(outline_path)
    plan_target, outline_target = root / "continuity_plan.json", root / "outline.md"
    state = EpisodeState(manifest.get("state"))
    if state is EpisodeState.OUTLINE_READY:
        if plan_target.exists() and outline_target.exists() and _hash(plan_target) == _hash(plan_path) and _hash(outline_target) == _hash(outline_path):
            return False
        raise ValidationError("OUTLINE_READY input differs from existing artifacts")
    if state is not EpisodeState.SELECTED:
        raise ValidationError("outline import requires SELECTED state")
    if plan_target.exists() or outline_target.exists():
        raise ValidationError("outline artifacts already exist")
    prohibited = ("story_gate.json", "script_draft.md", "review_1.json", "script_revised.md", "review_2.json", "continuity_check.json", "script_final.md", "canon_delta.json", "production_packet")
    if any((root / item).exists() for item in prohibited):
        raise ValidationError("later episode artifacts already exist")
    validate_transition(EpisodeState.SELECTED, EpisodeState.OUTLINE_READY)
    temporary = Path(tempfile.mkdtemp(prefix=".outline-", dir=root))
    try:
        shutil.copyfile(plan_path, temporary / "continuity_plan.json")
        shutil.copyfile(outline_path, temporary / "outline.md")
        os.replace(temporary / "continuity_plan.json", plan_target)
        os.replace(temporary / "outline.md", outline_target)
        manifest["state"] = EpisodeState.OUTLINE_READY.value
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return True
