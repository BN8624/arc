# Phase 2 live 단계별 최소 프롬프트를 구성한다.
from __future__ import annotations

import json


JSON_STAGES = {"planning", "planning_merge", "review", "review_merge", "memory", "memory_merge", "preflight"}


def build_prompt(stage: str, role: str, payload: dict) -> str:
    if stage in JSON_STAGES:
        instruction = "Return exactly one JSON object. Do not use Markdown fences or add explanation."
    elif stage == "writer":
        instruction = "한국어 산문 한 회차 본문만 작성하세요. 공백 포함 5000~7000자로 작성해 4000~8000자 검증 범위를 안전하게 만족하세요. 설명, 표식, JSON, Markdown, 생성 과정 언급을 포함하지 마세요."
    else:
        instruction = "한국어 수정본 본문만 작성하세요. 공백 포함 5000~7000자로 작성해 4000~8000자 검증 범위를 안전하게 만족하세요. 설명, 표식, JSON, Markdown을 포함하지 마세요."
    worker_id = f"{stage}-{role}"
    worker_rule = f'Use exactly these keys and no others: {{"worker_id":"{worker_id}","role":"{role}","verdict":"OK","primary_finding":"one concise finding","primary_risk":"one concise risk","evidence_refs":["source:current_episode"],"proposal":{{"role":"{role}"}}}}.'
    conflicts = payload.get("open_conflicts", [])
    option_lines = "\n".join(f"OC{index:03d}: {conflict}" for index, conflict in enumerate(conflicts, start=1)) if conflicts else "[]"
    memory_merge_rule = f'''Return exactly these keys and no others: episode_id, confirmed_facts_added, relationship_changes, conflict_ids_resolved, conflicts_opened, promises_added, important_excerpts_added, episode_summary, required_next_episode_continuity, evidence_refs. episode_id must be "{payload.get("episode_id", "")}". Every collection is an array of unique non-empty strings. evidence_refs must be ["final.md"].

CURRENT_OPEN_CONFLICT_OPTIONS
{option_lines}

Select resolved existing conflicts only by ID from CURRENT_OPEN_CONFLICT_OPTIONS. Return selected IDs in conflict_ids_resolved. Never write or paraphrase an existing conflict text in the response. An unknown ID is invalid. If none were resolved, return an empty list. conflicts_opened is only for new conflicts created by canonical final prose. Do not move or duplicate an existing open conflict into conflicts_opened.'''
    role_rule = {"planning": f"Analyze only the assigned planning role. Do not write prose. {worker_rule}", "review": f"Inspect only the assigned review role. {worker_rule}", "memory": f'Canonical final prose is the only evidence for new memory. Existing memory only prevents duplicates and conflicts. Do not restate existing items or convert plans into facts. Return only the assigned role. Use exactly these keys and no others: {{"worker_id":"{worker_id}","role":"{role}","verdict":"OK","primary_finding":"one concise finding","primary_risk":"one concise risk","evidence_refs":["final.md"],"proposal":{{"role":"{role}"}}}}.', "planning_merge": 'Return exactly these keys: episode_id, immediate_objective, obstacle, protagonist_action, meaningful_change, episode_ending, selected_worker_ids, continuity_constraints. Every text value must be non-empty.', "review_merge": 'Return exactly these keys: verdict, strengths_to_preserve, required_changes, evidence_refs. verdict is PASS, REVISE_ONCE, or HOLD. PASS has no required_changes; REVISE_ONCE has one to three.', "memory_merge": memory_merge_rule + ' Do not add an item already present in memory_before. Existing memory is not evidence; final.md is the only evidence for new memory.'}.get(stage, "Perform only the assigned task.")
    return f"{instruction}\n{role_rule}\nStage: {stage}\nRole: {role}\nInput JSON:\n{json.dumps(payload, ensure_ascii=False)}"
