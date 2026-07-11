# 외부 pitch batch의 검증·저장·사용자 선택을 처리한다.

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .project import validate_world_core
from .states import ApprovalGate, EpisodeState
from .validation import ValidationError, validate_transition
from .workflow import approve

PROJECT_ID = "kingdom_archive"
REQUIRED_CANDIDATE_FIELDS = (
    "pitch_id", "working_title", "logline", "era_anchor", "location_scope", "record_form", "protagonist",
    "protagonist_class", "human_conflict", "domain", "ending_shape", "world_refs", "history_contribution",
    "open_question", "central_mystery_relation", "production_shape", "draft_entities",
)
ERA_ANCHORS = {"FOUNDING", "BEFORE_SILENCE", "AFTER_SILENCE", "SCHISM", "BEFORE_FALL", "FALL", "AFTER_FALL", "UNDATED"}
PROTAGONIST_CLASSES = {"COMMONER", "CLERGY", "SOLDIER", "SCHOLAR", "CRIMINAL", "NOBILITY", "ROYALTY", "OUTSIDER", "OTHER"}
DOMAINS = {"ORDINARY_LIFE", "FAMILY", "LOVE", "FAITH", "LAW", "CRIME", "EXPLORATION", "FOLKLORE", "POLITICS", "WAR"}
CONTRIBUTION_STATUSES = {"DRAFT", "PROVISIONAL", "BELIEVED", "CONTESTED", "RUMOR", "OPEN"}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError("JSON object required")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ledger_ids(project_root: Path) -> set[str]:
    ledger = _read_json(project_root / "CONTINUITY_LEDGER.json")
    return {item["id"] for item in ledger["events"] + ledger["claims"]}


def validate_pitch_set(project_root: Path, pitch_set: dict) -> list[str]:
    version = validate_world_core(project_root)
    required = {"schema_version", "project_id", "world_version", "batch_id", "created_by", "candidates"}
    if not required.issubset(pitch_set):
        raise ValidationError("pitch set required fields are missing")
    if pitch_set["project_id"] != PROJECT_ID or pitch_set["world_version"] != version:
        raise ValidationError("pitch set project_id or world_version mismatch")
    if not isinstance(pitch_set["batch_id"], str) or not pitch_set["batch_id"] or not isinstance(pitch_set["created_by"], str) or not pitch_set["created_by"]:
        raise ValidationError("batch_id and created_by are required")
    candidates = pitch_set["candidates"]
    if not isinstance(candidates, list) or len(candidates) != 5:
        raise ValidationError("pitch set must contain exactly 5 candidates")
    ids, titles, ledger_ids = set(), set(), _ledger_ids(project_root)
    warnings: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or not set(REQUIRED_CANDIDATE_FIELDS).issubset(candidate):
            raise ValidationError("candidate required fields are missing")
        pitch_id, title = candidate["pitch_id"], candidate["working_title"]
        if not isinstance(pitch_id, str) or pitch_id in ids or not isinstance(title, str) or title in titles:
            raise ValidationError("duplicate or invalid pitch_id or working_title")
        ids.add(pitch_id); titles.add(title)
        if candidate["era_anchor"] not in ERA_ANCHORS or candidate["protagonist_class"] not in PROTAGONIST_CLASSES or candidate["domain"] not in DOMAINS:
            raise ValidationError("candidate enum is invalid")
        if candidate["central_mystery_relation"] not in {"NONE", "ECHO", "INDIRECT"}:
            raise ValidationError("central mystery direct answer is forbidden")
        refs = candidate["world_refs"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 3 or any(ref not in ledger_ids for ref in refs):
            raise ValidationError("world_refs must contain 1 to 3 known ledger IDs")
        contribution = candidate["history_contribution"]
        if not isinstance(contribution, dict) or contribution.get("type") not in {"FACT", "CLAIM", "CONTRADICTION"} or not contribution.get("summary") or contribution.get("intended_status") not in CONTRIBUTION_STATUSES:
            raise ValidationError("history_contribution is invalid or forbidden")
        shape = candidate["production_shape"]
        if not isinstance(shape, dict):
            raise ValidationError("production_shape is required")
        if not (5 <= shape.get("target_minutes", 0) <= 8 and shape.get("speaking_roles", 99) <= 4 and shape.get("primary_locations", 99) <= 3 and 8 <= shape.get("estimated_images", 0) <= 15):
            warnings.append(f"{pitch_id}: production_shape exceeds preferred range")
        if not isinstance(candidate["draft_entities"], list) or any(not isinstance(item, dict) or not item.get("type") or not item.get("working_name") or item.get("status") != "DRAFT" for item in candidate["draft_entities"]):
            raise ValidationError("draft_entities must all be DRAFT")
    if len({item["record_form"] for item in candidates}) < 3: warnings.append("diversity: fewer than 3 record forms")
    if len({item["era_anchor"] for item in candidates}) < 3: warnings.append("diversity: fewer than 3 era anchors")
    if len({item["domain"] for item in candidates}) < 3: warnings.append("diversity: fewer than 3 domains")
    if not any(item["protagonist_class"] in {"COMMONER", "OUTSIDER"} for item in candidates): warnings.append("diversity: no COMMONER or OUTSIDER protagonist")
    if sum(item["domain"] in {"POLITICS", "WAR"} for item in candidates) >= 3: warnings.append("diversity: POLITICS and WAR dominate")
    if all("CL_SELF_ERASURE" in item["world_refs"] for item in candidates): warnings.append("diversity: every pitch references CL_SELF_ERASURE")
    return warnings


def _batch_root(project_root: Path, batch_id: str) -> Path:
    return project_root / "pitches" / batch_id


def import_pitch_set(project_root: Path, source: Path) -> list[str]:
    pitch_set = _read_json(source)
    warnings = validate_pitch_set(project_root, pitch_set)
    root = _batch_root(project_root, pitch_set["batch_id"])
    if root.exists():
        raise ValidationError(f"pitch batch already exists: {pitch_set['batch_id']}")
    root.mkdir(parents=True)
    _write_json(root / "pitch_set.json", pitch_set)
    _write_json(root / "validation.json", {"valid": True, "warnings": warnings})
    lines = [f"# Pitch batch {pitch_set['batch_id']}", ""]
    for index, item in enumerate(pitch_set["candidates"], 1):
        lines.extend([f"## {index}. {item['working_title']}", item["logline"], f"- Era: {item['era_anchor']}", f"- Record: {item['record_form']}", f"- Protagonist: {item['protagonist']}", f"- Conflict: {item['human_conflict']}", f"- History: {item['history_contribution']['summary']}", ""])
    (root / "pitch_set.md").write_text("\n".join(lines), encoding="utf-8")
    return warnings


def list_pitches(project_root: Path, batch_id: str | None = None) -> list[dict]:
    batches = [_batch_root(project_root, batch_id)] if batch_id else sorted((project_root / "pitches").glob("*/"))
    result = []
    for root in batches:
        pitch_set = _read_json(root / "pitch_set.json")
        warnings = _read_json(root / "validation.json").get("warnings", [])
        for item in pitch_set["candidates"]:
            result.append({"batch_id": pitch_set["batch_id"], "candidate": item, "warnings": [warning for warning in warnings if item["pitch_id"] in warning or warning.startswith("diversity:")]})
    return result


def select_pitch(project_root: Path, batch_id: str, pitch_id: str, episode_id: str) -> bool:
    batch = _batch_root(project_root, batch_id)
    pitch_set = _read_json(batch / "pitch_set.json")
    validate_pitch_set(project_root, pitch_set)
    candidate = next((item for item in pitch_set["candidates"] if item["pitch_id"] == pitch_id), None)
    if candidate is None: raise ValidationError("pitch_id not found in batch")
    selection_path = batch / "selection.json"
    episode_root = project_root / "episodes" / episode_id
    if selection_path.exists():
        prior = _read_json(selection_path)
        if prior.get("pitch_id") == pitch_id and prior.get("episode_id") == episode_id and episode_root.exists(): return False
        raise ValidationError("batch already has a different selection")
    if episode_root.exists(): raise ValidationError(f"episode already exists: {episode_id}")
    validate_transition(EpisodeState.PITCHED, EpisodeState.SELECTED, {ApprovalGate.G2_EPISODE_SELECTION})
    source_hash = hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    source = {"batch_id": batch_id, "pitch_id": pitch_id, "selected_by": "user", "selected_at": datetime.now(timezone.utc).isoformat(), "world_version": pitch_set["world_version"], "source_hash": source_hash, "candidate": candidate}
    episodes = project_root / "episodes"; episodes.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".select-", dir=episodes))
    try:
        _write_json(temporary / "episode.json", {"schema_version": 1, "project_id": PROJECT_ID, "episode_id": episode_id, "state": EpisodeState.PITCHED.value, "scenario": "external", "approvals": []})
        (temporary / "pitch.md").write_text(f"# {candidate['working_title']}\n\n{candidate['logline']}\n", encoding="utf-8")
        _write_json(temporary / "pitch_source.json", source)
        os.replace(temporary, episode_root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    approve(project_root, episode_id, ApprovalGate.G2_EPISODE_SELECTION)
    manifest_path = episode_root / "episode.json"; manifest = _read_json(manifest_path); manifest["state"] = EpisodeState.SELECTED.value; _write_json(manifest_path, manifest)
    _write_json(episode_root / "selection.json", source | {"project_id": PROJECT_ID, "episode_id": episode_id})
    _write_json(selection_path, source | {"episode_id": episode_id})
    return True
