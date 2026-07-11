# 실제 E001의 최종 리뷰와 연속성 검사 결과를 안전하게 가져온다.

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from .project import validate_world_core
from .states import EpisodeState
from .validation import ValidationError, validate_transition

REVIEW_CHECKS = {"RR01_evidence_logic", "RR02_receiver_consistency", "RR03_scene_function", "RR04_voice_distinction", "RR05_human_stakes", "standalone_story", "archive_identity", "central_mystery_protection", "production_feasibility"}
CONTINUITY_CHECKS = {"world_version_match", "episode_identity_match", "approved_world_refs_only", "unknown_exact_year_not_introduced", "kingdom_and_king_name_remain_open", "receiver_identity_remains_open", "sealed_order_content_remains_open", "king_death_cause_remains_open", "supernatural_truth_remains_open", "central_mystery_not_answered", "draft_entities_not_in_ledger", "production_constraints_respected"}

def _read(path):
    try: value=json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as error: raise ValidationError(f"malformed JSON: {path}") from error
    if not isinstance(value,dict): raise ValidationError("JSON object required")
    return value
def _hash(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def _identity(value, source, episode_id, version):
    if value.get("project_id")!="kingdom_archive" or value.get("world_version")!=version or value.get("episode_id")!=episode_id: raise ValidationError("final input identity mismatch")
    actual=value.get("source",{})
    if actual.get("batch_id")!=source.get("batch_id") or actual.get("pitch_id")!=source.get("pitch_id") or actual.get("script_artifact")!="script_revised.md": raise ValidationError("final input source mismatch")
def import_final(project_root, episode_id, review_path, continuity_path, script_path):
    root=project_root/"episodes"/episode_id; manifest_path=root/"episode.json"; manifest=_read(manifest_path); source=_read(root/"pitch_source.json"); version=validate_world_core(project_root)
    review=_read(review_path); continuity=_read(continuity_path)
    _identity(review,source,episode_id,version); _identity(continuity,source,episode_id,version)
    revised=root/"script_revised.md"
    if review.get("review_round")!=2 or review.get("verdict")!="PASS" or review.get("blocking_issues")!=[] or review.get("next_allowed_artifact")!="continuity_check.json" or review.get("canon_effect")!="NONE" or review.get("source",{}).get("script_sha256","").lower()!=_hash(revised): raise ValidationError("review 2 is invalid")
    checks=review.get("checks",{});
    if not REVIEW_CHECKS.issubset(checks) or any(checks[key].get("result")!="PASS" for key in REVIEW_CHECKS): raise ValidationError("review 2 checks are invalid")
    if continuity.get("verdict")!="SOFT_CONFLICT" or continuity.get("can_advance") is not True or continuity.get("hard_conflicts")!=[] or continuity.get("next_allowed_artifact")!="script_final.md" or continuity.get("canon_effect")!="NONE": raise ValidationError("continuity result is invalid")
    cchecks=continuity.get("checks",{});
    if not CONTINUITY_CHECKS.issubset(cchecks) or any(cchecks[key]!="PASS" for key in CONTINUITY_CHECKS): raise ValidationError("continuity checks are invalid")
    conflicts=continuity.get("soft_conflicts",[]); ids={item.get("id") for item in conflicts}
    if not {"SC01","SC02"}.issubset(ids) or any(item.get("preserve") is not True or item.get("treatment")!="CONTESTED" for item in conflicts): raise ValidationError("soft conflicts are invalid")
    if any(item.get("apply_to_ledger_now") is not False for item in continuity.get("provisional_claims",[])): raise ValidationError("ledger application is forbidden")
    if _hash(script_path)!=_hash(revised): raise ValidationError("final script must match revised script")
    targets=[root/"review_2.json",root/"continuity_check.json",root/"script_final.md"]; state=EpisodeState(manifest.get("state"))
    if state is EpisodeState.AWAITING_APPROVAL:
        if all(path.exists() for path in targets) and [_hash(path) for path in targets]==[_hash(review_path),_hash(continuity_path),_hash(script_path)]: return False
        raise ValidationError("AWAITING_APPROVAL input differs from existing artifacts")
    if state is not EpisodeState.REVISED or any(path.exists() for path in targets): raise ValidationError("final import requires clean REVISED state")
    if any((root/item).exists() for item in ("canon_delta.json","production_packet")): raise ValidationError("later artifacts already exist")
    validate_transition(EpisodeState.REVISED,EpisodeState.REVIEW_2); validate_transition(EpisodeState.REVIEW_2,EpisodeState.CONTINUITY_CHECKED); validate_transition(EpisodeState.CONTINUITY_CHECKED,EpisodeState.AWAITING_APPROVAL)
    temp=Path(tempfile.mkdtemp(prefix=".final-",dir=root))
    try:
        for source_path,name in ((review_path,"review_2.json"),(continuity_path,"continuity_check.json"),(script_path,"script_final.md")): shutil.copyfile(source_path,temp/name); os.replace(temp/name,root/name)
        manifest["state"]=EpisodeState.AWAITING_APPROVAL.value; manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    finally: shutil.rmtree(temp,ignore_errors=True)
    return True
