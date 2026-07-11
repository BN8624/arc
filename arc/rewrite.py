# 실제 E001의 1차 리뷰와 단일 수정 대본을 안전하게 가져온다.

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

REVISION_IDS = {"RR01", "RR02", "RR03", "RR04", "RR05"}
ALLOWED_SPEAKERS = {"세라", "로엔", "베른", "탈렌"}
NON_SPEAKER_LABELS = {"화면", "화면 자막", "음향"}


def _read_json(path: Path) -> dict:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error: raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value, dict): raise ValidationError("JSON object required")
    return value


def _hash(path: Path) -> str: return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_review(project_root: Path, episode_id: str, review: dict, source: dict) -> None:
    version = validate_world_core(project_root)
    if review.get("schema_version") != 1 or review.get("project_id") != "kingdom_archive" or review.get("world_version") != version or review.get("episode_id") != episode_id: raise ValidationError("review identity mismatch")
    review_source = review.get("source", {})
    if review_source.get("batch_id") != source.get("batch_id") or review_source.get("pitch_id") != source.get("pitch_id") or review_source.get("script_artifact") != "script_draft.md": raise ValidationError("review source mismatch")
    if review.get("review_round") != 1 or review.get("verdict") != "REWRITE_ONCE" or review.get("next_allowed_artifact") != "script_revised.md" or review.get("canon_effect") != "NONE": raise ValidationError("review verdict or routing is invalid")
    revisions = review.get("required_revisions", []); ids = [item.get("id") for item in revisions]
    if set(ids) != REVISION_IDS or len(ids) != len(set(ids)): raise ValidationError("review required revision IDs are invalid")
    if not review.get("strengths_to_preserve") or not review.get("continuity_constraints"): raise ValidationError("review strengths and continuity constraints are required")


def _validate_script(path: Path) -> None:
    try: content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error: raise ValidationError("revised script must be UTF-8") from error
    required = ("E001", "## 제작 기준", "수정 상태: REVISED", "반영 리뷰: RR01~RR05", "## 수정 반영 확인", "수신자 명부", "공식 경로")
    if not content.strip() or any(marker not in content for marker in required) or any(f"## 장면 {index}" not in content for index in range(8)): raise ValidationError("revised script required sections are missing")
    if any(f"{revision}" not in content for revision in REVISION_IDS): raise ValidationError("revised script revision confirmation is missing")
    labels = set(re.findall(r"^\[([^\]]+)\]", content, flags=re.MULTILINE)); speakers = labels - NON_SPEAKER_LABELS
    if not labels.issubset(ALLOWED_SPEAKERS | NON_SPEAKER_LABELS) or speakers != ALLOWED_SPEAKERS: raise ValidationError("revised script speakers are invalid")
    forbidden = ("수신자를 바꿨다", "왕국의 고유 이름:", "정확한 연도:", "CANON으로 승격", "ledger에 반영 지시")
    if any(token in content for token in forbidden): raise ValidationError("revised script contains a forbidden setting or canon instruction")


def import_rewrite(project_root: Path, episode_id: str, review_path: Path, script_path: Path) -> bool:
    root = project_root / "episodes" / episode_id; manifest_path = root / "episode.json"; manifest = _read_json(manifest_path); source = _read_json(root / "pitch_source.json")
    if manifest.get("project_id") != "kingdom_archive" or manifest.get("episode_id") != episode_id: raise ValidationError("episode manifest identity mismatch")
    review = _read_json(review_path); _validate_review(project_root, episode_id, review, source); _validate_script(script_path)
    review_target, script_target = root / "review_1.json", root / "script_revised.md"; state = EpisodeState(manifest.get("state"))
    if state is EpisodeState.REVISED:
        if review_target.exists() and script_target.exists() and _hash(review_target) == _hash(review_path) and _hash(script_target) == _hash(script_path): return False
        raise ValidationError("REVISED input differs from existing artifacts")
    if state is not EpisodeState.SCRIPT_DRAFT: raise ValidationError("rewrite import requires SCRIPT_DRAFT state")
    if review_target.exists() or script_target.exists(): raise ValidationError("rewrite artifacts already exist")
    prohibited = ("review_2.json", "continuity_check.json", "script_final.md", "canon_delta.json", "production_packet")
    if any((root / item).exists() for item in prohibited): raise ValidationError("later episode artifacts already exist")
    validate_transition(EpisodeState.SCRIPT_DRAFT, EpisodeState.REVIEW_1); validate_transition(EpisodeState.REVIEW_1, EpisodeState.REVISED)
    temporary = Path(tempfile.mkdtemp(prefix=".rewrite-", dir=root))
    try:
        shutil.copyfile(review_path, temporary / "review_1.json"); shutil.copyfile(script_path, temporary / "script_revised.md")
        os.replace(temporary / "review_1.json", review_target); os.replace(temporary / "script_revised.md", script_target)
        manifest["state"] = EpisodeState.REVISED.value; manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally: shutil.rmtree(temporary, ignore_errors=True)
    return True
