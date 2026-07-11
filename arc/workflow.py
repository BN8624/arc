# E001 fixture 기반 수직 워크플로우를 실행한다.

import json
import shutil
from pathlib import Path

from .artifacts import episode_directory, missing_artifacts, required_artifacts
from .states import ApprovalGate, EpisodeState
from .validation import ValidationError, validate_transition

PROJECT_ID = "kingdom_archive"
FIXTURE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "arc1"


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value, dict):
        raise ValidationError(f"JSON object required: {path}")
    return value


def _fixture_json(name: str, episode_id: str | None = None) -> dict:
    value = _read_json(FIXTURE_ROOT / name)
    if value.get("project_id") != PROJECT_ID:
        raise ValidationError(f"fixture project mismatch: {name}")
    if episode_id is not None and value.get("episode_id") not in {None, episode_id}:
        raise ValidationError(f"fixture episode mismatch: {name}")
    return value


def _copy_fixture(name: str, destination: Path) -> None:
    if destination.exists():
        raise ValidationError(f"refusing to overwrite artifact: {destination.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE_ROOT / name, destination)


def _write_json(path: Path, value: dict) -> None:
    if path.exists():
        raise ValidationError(f"refusing to overwrite artifact: {path.name}")
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_episode(project_root: Path, episode_id: str) -> tuple[Path, dict]:
    root = episode_directory(project_root, episode_id)
    manifest = _read_json(root / "episode.json")
    if manifest.get("project_id") != PROJECT_ID or manifest.get("episode_id") != episode_id:
        raise ValidationError("episode manifest project ID or episode ID mismatch")
    try:
        EpisodeState(manifest["state"])
    except (KeyError, ValueError) as error:
        raise ValidationError("episode manifest state is invalid") from error
    return root, manifest


def _save_episode(root: Path, manifest: dict) -> None:
    (root / "episode.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _project_approvals(project_root: Path) -> set[ApprovalGate]:
    project = _read_json(project_root / "project.json")
    try:
        return {ApprovalGate(item) for item in project.get("approvals", [])}
    except ValueError as error:
        raise ValidationError("project approvals are invalid") from error


def create_episode(project_root: Path, episode_id: str, scenario: str) -> None:
    if scenario not in {"pass", "rewrite", "hold", "soft", "hard"}:
        raise ValidationError("scenario must be pass, rewrite, hold, soft, or hard")
    if ApprovalGate.G1_WORLD_CORE not in _project_approvals(project_root):
        raise ValidationError("G1_WORLD_CORE approval is required before episode creation")
    root = episode_directory(project_root, episode_id)
    if root.exists():
        raise ValidationError(f"episode already exists: {episode_id}")
    pitches = _fixture_json("pitches.json")
    if not any(item.get("id") == episode_id for item in pitches.get("pitches", [])):
        raise ValidationError(f"fixture pitch not found: {episode_id}")
    root.mkdir(parents=True)
    _copy_fixture("pitch.md", root / "pitch.md")
    _write_json(root / "episode.json", {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "episode_id": episode_id,
        "state": EpisodeState.PITCHED.value,
        "scenario": scenario,
        "approvals": [],
    })


def approve(project_root: Path, episode_id: str | None, gate: ApprovalGate) -> bool:
    if gate is ApprovalGate.G1_WORLD_CORE:
        if episode_id is not None:
            raise ValidationError("G1 is approved at project scope")
        project_path = project_root / "project.json"
        project = _read_json(project_path)
        approvals = set(project.get("approvals", []))
        if gate.value in approvals:
            return False
        approvals.add(gate.value)
        project["approvals"] = sorted(approvals)
        project_path.write_text(json.dumps(project, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    if episode_id is None:
        raise ValidationError("G2, G3, and G4 require an episode ID")
    root, manifest = _load_episode(project_root, episode_id)
    state = EpisodeState(manifest["state"])
    approvals = set(manifest.get("approvals", []))
    if gate.value in approvals:
        return False
    expected = {
        ApprovalGate.G2_EPISODE_SELECTION: EpisodeState.PITCHED,
        ApprovalGate.G3_FINAL_SCRIPT_PRODUCTION: EpisodeState.AWAITING_APPROVAL,
        ApprovalGate.G4_PUBLICATION_AND_CANON: EpisodeState.PRODUCTION_READY,
    }[gate]
    if state is not expected:
        raise ValidationError(f"{gate.value} cannot be approved in {state.value}")
    approvals.add(gate.value)
    manifest["approvals"] = sorted(approvals)
    _save_episode(root, manifest)
    return True


def _transition(root: Path, manifest: dict, target: EpisodeState) -> None:
    approvals = {ApprovalGate(item) for item in manifest.get("approvals", [])}
    validate_transition(EpisodeState(manifest["state"]), target, approvals)
    manifest["state"] = target.value
    _save_episode(root, manifest)


def _require_current_artifacts(root: Path, state: EpisodeState, episode_id: str) -> None:
    missing = missing_artifacts(root, state)
    if missing:
        raise ValidationError(f"missing required artifacts: {', '.join(missing)}")
    for item in (root / name for name in required_artifacts(state) if name.endswith(".json")):
        value = _read_json(item)
        if item.name != "episode.json" and (value.get("project_id") != PROJECT_ID or value.get("episode_id") != episode_id):
            raise ValidationError(f"artifact identity mismatch: {item.name}")


def advance(project_root: Path, episode_id: str) -> EpisodeState:
    root, manifest = _load_episode(project_root, episode_id)
    state = EpisodeState(manifest["state"])
    if state is EpisodeState.HOLD:
        raise ValidationError("HOLD episodes cannot advance automatically")
    _require_current_artifacts(root, state, manifest["episode_id"])
    scenario = manifest["scenario"]
    if state is EpisodeState.PITCHED:
        if ApprovalGate.G1_WORLD_CORE not in _project_approvals(project_root):
            raise ValidationError("G1_WORLD_CORE approval is required before pitch flow")
        validate_transition(state, EpisodeState.SELECTED, {ApprovalGate(item) for item in manifest.get("approvals", [])})
        _fixture_json("selected_pitch.json", episode_id)
        _copy_fixture("selected_pitch.json", root / "selection.json")
        _transition(root, manifest, EpisodeState.SELECTED)
    elif state is EpisodeState.SELECTED:
        _fixture_json("selected_pitch.json", episode_id)
        _write_json(root / "continuity_plan.json", {"project_id": PROJECT_ID, "episode_id": episode_id, "check": "pending"})
        _copy_fixture("outline.md", root / "outline.md")
        _transition(root, manifest, EpisodeState.OUTLINE_READY)
    elif state is EpisodeState.OUTLINE_READY:
        _fixture_json("story_gate_pass.json", episode_id)
        _copy_fixture("story_gate_pass.json", root / "story_gate.json")
        _copy_fixture("script_draft.md", root / "script_draft.md")
        _transition(root, manifest, EpisodeState.SCRIPT_DRAFT)
    elif state is EpisodeState.SCRIPT_DRAFT:
        review = "review_rewrite.json" if scenario in {"rewrite", "hold"} else "review_pass.json"
        _fixture_json(review, episode_id)
        _copy_fixture(review, root / "review_1.json")
        _transition(root, manifest, EpisodeState.REVIEW_1)
    elif state is EpisodeState.REVIEW_1:
        decision = _read_json(root / "review_1.json").get("decision")
        if decision == "PASS":
            _copy_fixture("continuity_soft_conflict.json" if scenario == "soft" else "continuity_hard_conflict.json" if scenario == "hard" else "continuity_clear.json", root / "continuity_check.json")
            _transition(root, manifest, EpisodeState.CONTINUITY_CHECKED)
        elif decision == "REWRITE":
            _copy_fixture("script_revised.md", root / "script_revised.md")
            _transition(root, manifest, EpisodeState.REVISED)
        else:
            raise ValidationError("review_1 decision must be PASS or REWRITE")
    elif state is EpisodeState.REVISED:
        review = "review_2_fail.json" if scenario == "hold" else "review_2_pass.json"
        _fixture_json(review, episode_id)
        _copy_fixture(review, root / "review_2.json")
        _transition(root, manifest, EpisodeState.REVIEW_2)
    elif state is EpisodeState.REVIEW_2:
        decision = _read_json(root / "review_2.json").get("decision")
        if decision == "PASS":
            _copy_fixture("continuity_clear.json", root / "continuity_check.json")
            _transition(root, manifest, EpisodeState.CONTINUITY_CHECKED)
        elif decision == "FAIL":
            _transition(root, manifest, EpisodeState.HOLD)
        else:
            raise ValidationError("review_2 decision must be PASS or FAIL")
    elif state is EpisodeState.CONTINUITY_CHECKED:
        decision = _read_json(root / "continuity_check.json").get("result")
        if decision in {"CLEAR", "SOFT_CONFLICT"}:
            _copy_fixture("script_revised.md" if scenario in {"rewrite", "hold"} else "script_draft.md", root / "script_final.md")
            _transition(root, manifest, EpisodeState.AWAITING_APPROVAL)
        elif decision == "HARD_CONFLICT":
            _transition(root, manifest, EpisodeState.HOLD)
        else:
            raise ValidationError("continuity result must be CLEAR, SOFT_CONFLICT, or HARD_CONFLICT")
    elif state is EpisodeState.AWAITING_APPROVAL:
        validate_transition(state, EpisodeState.PRODUCTION_READY, {ApprovalGate(item) for item in manifest.get("approvals", [])})
        _fixture_json("canon_delta.json", episode_id)
        _copy_fixture("canon_delta.json", root / "canon_delta.json")
        _fixture_json("production_packet_manifest.json", episode_id)
        _copy_fixture("production_packet_manifest.json", root / "production_packet" / "manifest.json")
        _transition(root, manifest, EpisodeState.PRODUCTION_READY)
    else:
        raise ValidationError(f"cannot advance from {state.value}")
    return EpisodeState(manifest["state"])


def run_until_blocked(project_root: Path, episode_id: str) -> tuple[EpisodeState, str | None]:
    while True:
        try:
            state = advance(project_root, episode_id)
        except ValidationError as error:
            _, manifest = _load_episode(project_root, episode_id)
            return EpisodeState(manifest["state"]), str(error)
        if state in {EpisodeState.PRODUCTION_READY, EpisodeState.HOLD}:
            return state, None


def status(project_root: Path, episode_id: str) -> tuple[EpisodeState, list[str], str | None]:
    root, manifest = _load_episode(project_root, episode_id)
    state = EpisodeState(manifest["state"])
    reason = blocked_reason(project_root, manifest)
    return state, missing_artifacts(root, state), reason


def blocked_reason(project_root: Path, manifest: dict) -> str | None:
    state = EpisodeState(manifest["state"])
    approvals = set(manifest.get("approvals", []))
    if state is EpisodeState.PITCHED and ApprovalGate.G1_WORLD_CORE not in _project_approvals(project_root):
        return "G1_WORLD_CORE approval is required before pitch flow"
    if state is EpisodeState.PITCHED and ApprovalGate.G2_EPISODE_SELECTION.value not in approvals:
        return "G2_EPISODE_SELECTION approval is required"
    if state is EpisodeState.AWAITING_APPROVAL and ApprovalGate.G3_FINAL_SCRIPT_PRODUCTION.value not in approvals:
        return "G3_FINAL_SCRIPT_PRODUCTION approval is required"
    if state is EpisodeState.HOLD:
        return "HOLD episodes cannot advance automatically"
    if state is EpisodeState.PRODUCTION_READY:
        return "production ready"
    return None
