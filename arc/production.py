# 실제 E001의 G3 승인과 production packet을 안전하게 가져온다.

import hashlib, json, os, shutil, tempfile, zipfile
from pathlib import Path
from .states import ApprovalGate, EpisodeState
from .validation import ValidationError, validate_transition

ROOT="E001_production_packet/"
ALLOWED={"README.md","g3_approval.json","image_prompts.md","production_manifest.json","shot_list.json","sound_edit_plan.md","subtitles.srt","voice_script.md"}
def _json(path):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,json.JSONDecodeError) as e: raise ValidationError(f"malformed JSON: {path}") from e
def _hash(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def import_production(project,episode,packet):
    root=project/"episodes"/episode; mp=root/"episode.json"; manifest=_json(mp)
    if manifest.get("state")!="AWAITING_APPROVAL" or "G3_FINAL_SCRIPT_PRODUCTION" in manifest.get("approvals",[]): raise ValidationError("production import requires unapproved AWAITING_APPROVAL")
    temp=Path(tempfile.mkdtemp(prefix=".packet-",dir=root))
    try:
        with zipfile.ZipFile(packet) as archive:
            names=archive.namelist()
            if set(names)!={ROOT+name for name in ALLOWED} or any(name.startswith("/") or ".." in Path(name).parts for name in names): raise ValidationError("unsafe or unexpected packet files")
            for info in archive.infolist():
                if info.is_dir() or (info.external_attr>>16)&0o170000==0o120000: raise ValidationError("unsafe packet entry")
            archive.extractall(temp)
        packet_root=temp/ROOT.rstrip("/"); approval=_json(packet_root/"g3_approval.json"); prod=_json(packet_root/"production_manifest.json")
        if any(approval.get(k)!=v for k,v in {"project_id":"kingdom_archive","world_version":"1.0","episode_id":episode,"gate":"G3_FINAL_SCRIPT_PRODUCTION","decision":"APPROVE","approved_by":"user","approved_artifact":"script_final.md"}.items()): raise ValidationError("G3 approval is invalid")
        if not {"publication","G4_CANON_APPROVAL","canon_delta_apply","continuity_ledger_update"}.issubset(set(approval.get("excludes",[]))): raise ValidationError("G3 exclusions are invalid")
        if any(prod.get(k)!=v for k,v in {"project_id":"kingdom_archive","world_version":"1.0","episode_id":episode,"packet_version":"1.0","status":"PRODUCTION_PACKET_DRAFT","source_artifact":"script_final.md","g3_decision":"APPROVE","aspect_ratio":"16:9","narrator_role":None,"planned_images":12,"canon_effect":"NONE","publication_effect":"NONE","ledger_apply":False}.items()): raise ValidationError("production manifest is invalid")
        if not 300<=prod.get("target_duration_seconds",0)<=480 or set(prod.get("speaking_roles",[]))!={"세라","로엔","베른","탈렌"} or prod.get("source_script_sha256","").lower()!=_hash(root/"script_final.md"): raise ValidationError("production source or scale is invalid")
        files={item.get("path"):item for item in prod.get("files",[])}
        if set(files)!=(ALLOWED-{"production_manifest.json"}): raise ValidationError("production manifest file list is invalid")
        for name,item in files.items():
            if _hash(packet_root/name)!=item.get("sha256") or (packet_root/name).stat().st_size!=item.get("size_bytes"): raise ValidationError("production packet hash mismatch")
        target=root/"production_packet"
        if target.exists(): raise ValidationError("production packet already exists")
        validate_transition(EpisodeState.AWAITING_APPROVAL,EpisodeState.PRODUCTION_READY,{ApprovalGate.G3_FINAL_SCRIPT_PRODUCTION})
        os.replace(packet_root,target); manifest["approvals"].append("G3_FINAL_SCRIPT_PRODUCTION"); manifest["g3_approval"]=approval; manifest["state"]="PRODUCTION_READY"; mp.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    finally: shutil.rmtree(temp,ignore_errors=True)
    return True
