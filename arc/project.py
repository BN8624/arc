# 프로젝트 매니페스트를 생성하고 읽는다.

import json
from pathlib import Path

from .states import FactLifecycle
from .validation import ValidationError

PROJECT_MANIFEST = {"schema_version": 1, "project": "kingdom_archive", "world_core_gate": "G1_WORLD_CORE", "approvals": []}
SERIES_STATE = {"schema_version": 1, "episodes": {}}
CONTINUITY_LEDGER = {"schema_version": 1, "facts": []}
WORLD_CORE_SECTIONS = (
    "## 기록 프레임",
    "## 장르와 분위기",
    "## 초자연 규칙",
    "## 역사적 기둥",
    "## 중심 미스터리",
    "## 창작 원칙",
    "## 아직 정하지 않은 항목",
)
ARCHIVE_RULE_MARKERS = (
    "event_time", "record_time", "release_order", "VERIFIED", "FORGED_SUSPECTED", "UNKNOWN",
    "HOLD 및 REJECTED 에피소드의 canon_delta 적용 금지", "HARD_CONFLICT", "SOFT_CONFLICT", "CONTESTED",
)
PILLAR_IDS = ("EV_FOUNDING", "EV_SILENCE", "EV_SCHISM", "EV_FALL")
CLAIM_IDS = ("CL_ARCHIVE_137", "CL_SELF_ERASURE")


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


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"JSON object required: {path}")
    return value


def _require_markers(path: Path, markers: tuple[str, ...]) -> None:
    content = path.read_text(encoding="utf-8")
    missing = [marker for marker in markers if marker not in content]
    if missing:
        raise ValidationError(f"missing required world-core content in {path.name}: {', '.join(missing)}")


def _require_unique_ids(entries: list[dict], expected_ids: tuple[str, ...], kind: str) -> None:
    ids = [entry.get("id") for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValidationError(f"duplicate {kind} ID")
    if set(ids) != set(expected_ids):
        raise ValidationError(f"unexpected {kind} IDs")


def validate_world_core(project_root: Path) -> str:
    _require_markers(project_root / "WORLD_CORE.md", WORLD_CORE_SECTIONS)
    _require_markers(project_root / "ARCHIVE_RULES.md", ARCHIVE_RULE_MARKERS)
    project = _read_json(project_root / "project.json")
    ledger = _read_json(project_root / "CONTINUITY_LEDGER.json")
    version = project.get("world_version")
    approval = project.get("world_core_approval", {})
    if "G1_WORLD_CORE" not in project.get("approvals", []):
        raise ValidationError("G1_WORLD_CORE approval is required")
    if approval.get("approved_by") != "user" or not approval.get("approved_at"):
        raise ValidationError("G1 approval actor and timestamp are required")
    if not version or approval.get("world_version") != version or ledger.get("world_version") != version:
        raise ValidationError("world_version mismatch")
    version_marker = f"World Version: {version}"
    if version_marker not in (project_root / "WORLD_CORE.md").read_text(encoding="utf-8") or version_marker not in (project_root / "ARCHIVE_RULES.md").read_text(encoding="utf-8"):
        raise ValidationError("world_version mismatch")
    events = ledger.get("events")
    claims = ledger.get("claims")
    if not isinstance(events, list) or not isinstance(claims, list):
        raise ValidationError("ledger events and claims are required")
    _require_unique_ids(events, PILLAR_IDS, "event")
    _require_unique_ids(claims, CLAIM_IDS, "claim")
    for event in events:
        if event.get("status") != FactLifecycle.CANON or event.get("date") is not None or event.get("details") != FactLifecycle.OPEN:
            raise ValidationError("historical pillars must be CANON with OPEN details and date")
    allowed_statuses = {state.value for state in FactLifecycle}
    if any(claim.get("status") not in allowed_statuses for claim in claims):
        raise ValidationError("invalid claim status")
    archive_claim = next(claim for claim in claims if claim["id"] == "CL_ARCHIVE_137")
    mystery_claim = next(claim for claim in claims if claim["id"] == "CL_SELF_ERASURE")
    if "137년" not in archive_claim.get("statement", "") or archive_claim.get("status") != FactLifecycle.CANON:
        raise ValidationError("archive frame claim must preserve 137 years as CANON")
    if mystery_claim.get("status") not in {FactLifecycle.OPEN, FactLifecycle.CONTESTED}:
        raise ValidationError("central mystery must remain OPEN or CONTESTED")
    return version


def world_readiness(project_root: Path) -> tuple[bool, str]:
    try:
        return True, validate_world_core(project_root)
    except (OSError, ValidationError) as error:
        return False, str(error)
