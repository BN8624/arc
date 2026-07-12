# 내용 비의존적 prose authoring loop의 mock 실행을 제공한다.

import hashlib,json
from pathlib import Path
from .validation import ValidationError

def _hash(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def run(project,episode):
 root=project/'episodes'/episode; prose=root/'prose'; prose.mkdir(exist_ok=True)
 sources=['WORLD_CORE.md','ARCHIVE_RULES.md']; packet={'episode_id':episode,'sources':{name:_hash(project/name) for name in sources}}
 if (prose/'prose_manifest.json').exists():
  old=json.loads((prose/'source_packet.json').read_text());
  if old!=packet: raise ValidationError('prose source changed')
  return False
 (prose/'source_packet.json').write_text(json.dumps(packet,indent=2),encoding='utf-8')
 files={'prompt_plan.md':'Mock prose plan prompt.','prose_plan.md':'# Prose plan\n- conflict\n- choice\n- open question\n','prompt_draft.md':'Mock draft prompt.','draft.md':'가상의 인물이 선택의 대가를 마주한다.\n'*400,'prompt_review.md':'Mock review prompt.','review.json':json.dumps({'verdict':'PASS','summary':'mock','scores':{'standalone_story':8},'strengths_to_preserve':[],'required_revisions':[],'continuity_violations':[],'forbidden_revelations':[]}), 'prompt_final_check.md':'Mock final check prompt.','final_check.json':json.dumps({'verdict':'PASS','review_requirements_satisfied':[],'continuity_checks':{},'remaining_issues':[],'canon_effect':'NONE'}),'run.json':json.dumps({'model':'mock','calls':4,'ledger_effect':'NONE','publication':'NOT_PUBLISHED'})}
 for n,v in files.items(): (prose/n).write_text(v,encoding='utf-8')
 (prose/'final.md').write_bytes((prose/'draft.md').read_bytes())
 (prose/'prose_manifest.json').write_text(json.dumps({'state':'FINAL','model':'mock'},indent=2),encoding='utf-8')
 return True
def status(project,episode):
 p=project/'episodes'/episode/'prose'/'prose_manifest.json'
 return json.loads(p.read_text()) if p.exists() else {'state':'NOT_STARTED'}
