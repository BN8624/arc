# Phase 2 live 단계별 prompt contract를 구성한다.
from __future__ import annotations

import json
from math import ceil

from .contracts import ContractError, PROSE_MAX_CHARACTERS, PROSE_MIN_CHARACTERS


JSON_STAGES = {"planning", "planning_merge", "review", "review_merge", "memory", "memory_merge", "preflight"}
REVISION_SAFE_EXPANSION_FLOOR = 1200
REVISION_EXPANSION_MARGIN = 1000


def prose_target_band(hard_min: int = PROSE_MIN_CHARACTERS, hard_max: int | None = PROSE_MAX_CHARACTERS) -> tuple[int, int]:
    target_min = ceil(hard_min * 1.30)
    target_max = ceil(hard_min * 1.60)
    if hard_max is not None:
        if target_min > hard_max:
            raise ContractError("prose target band is incompatible with hard maximum", "PROSE_TARGET_BAND_INCOMPATIBLE")
        target_max = min(target_max, hard_max)
    return target_min, target_max


def _prose_target_text() -> str:
    target_min, target_max = prose_target_band()
    return f"{target_min} to {target_max} characters"


def revision_expansion_guidance(character_count: int) -> tuple[int, int]:
    if not isinstance(character_count, int) or isinstance(character_count, bool) or character_count < 0:
        raise ContractError("draft character count is invalid", "PROSE_DRAFT_CHARACTER_COUNT_INVALID")
    hard_gap = max(0, PROSE_MIN_CHARACTERS - character_count)
    safe_expansion = max(REVISION_SAFE_EXPANSION_FLOOR, hard_gap + REVISION_EXPANSION_MARGIN)
    return hard_gap, safe_expansion


def _prose_structural_guidance() -> str:
    return (
        "Silently plan roughly 14 to 18 natural prose paragraphs, usually with multiple sentences, without printing "
        "headings or paragraph numbers. Develop, in scene order, the opening state and immediate objective; the obstacle "
        "appearing as concrete interference; the protagonist's active choice and action; specific reactions from people, "
        "the environment, or the conflict; the meaningful consequence and changed relationship or situation; the immediate "
        "emotional and practical aftermath; the episode payoff; and an ending hook or pressure toward the next action. "
        "Use action, dialogue, observation, sensory detail, reaction, counteraction, cost, consequence, and aftermath where "
        "the plan supports them. Do not compress multiple actions into one or two summary sentences, replace important dialogue "
        "with one sentence of indirect speech, reduce a conflict to 'they argued', skip movement, decisions, or results with a "
        "single bridge sentence, narrate an event list, stretch setting exposition as padding, or jump from climax directly to "
        "the ending. Do not end before action, consequence, real change, aftermath, payoff, and the hook are present."
    )


def _writer_plan_guidance() -> str:
    return (
        "Use the supplied context and plan only. Fully realize plan.immediate_objective in the opening, plan.obstacle as a "
        "visible scene-level obstruction, plan.protagonist_action as an active choice followed by counteraction, "
        "plan.meaningful_change as an experienced result, and plan.episode_ending only after consequence and aftermath. "
        "Honor every plan.continuity_constraints item. Before returning the same single response, perform an internal expansion "
        "pass over these nine beats: objective, obstacle, action, counteraction, consequence, meaningful change, aftermath, "
        "payoff, and ending hook. Each beat must contain a concrete action, reaction, or state change rather than merely naming "
        "the plan field. Expand the two or three thinnest beats through multiple connected prose paragraphs using dialogue, "
        "action, counteraction, cost, and result already supported by the plan. Do not advance to the ending until consequence, "
        "meaningful change, aftermath, and payoff have each been dramatized. This expansion happens inside the one canonical "
        "response and never authorizes a second call. Do not invent a new central conflict or unsupported setting."
    )


def _revision_guidance(payload: dict) -> str:
    draft_contract = payload.get("draft_contract", {})
    character_count = draft_contract.get("character_count")
    if not isinstance(character_count, int):
        draft = payload.get("draft")
        if not isinstance(draft, str):
            raise ContractError("revision draft character count is unavailable", "PROSE_DRAFT_CHARACTER_COUNT_INVALID")
        character_count = len(draft)
    hard_gap, safe_expansion = revision_expansion_guidance(character_count)
    return (
        f"The current draft is {character_count} characters and is {hard_gap} characters below the {PROSE_MIN_CHARACTERS}-character "
        f"hard minimum. Produce a substantially expanded full replacement with roughly {safe_expansion} or more characters of "
        "meaningful new prose development, while targeting the stated final band; this expansion amount is guidance, not a "
        "validator. Preserve the existing facts, event order, point of view, ending, and strengths, and apply only the supplied "
        "review required changes. Expand rushed action, compressed dialogue, missing counteraction, causes of emotional change, "
        "costs of choices, consequences, aftermath, and abrupt compression before the ending through coherent scenes rather than "
        "repetition, padding, fragments, or a new central conflict."
    )


def _planning_merge_rule(payload: dict) -> str:
    explicit_id = payload.get("episode_id")
    context_id = payload.get("context", {}).get("episode_id")
    expected_id = explicit_id or context_id
    if explicit_id and context_id and explicit_id != context_id:
        raise ContractError("planning merge episode id mismatch", "PLAN_EPISODE_ID_MISMATCH")
    allowed_worker_ids = sorted(worker.get("worker_id", "") for worker in payload.get("workers", []) if isinstance(worker, dict))
    return (
        "Return exactly these eight keys and no others: episode_id, immediate_objective, obstacle, "
        "protagonist_action, meaningful_change, episode_ending, selected_worker_ids, continuity_constraints. "
        f'episode_id must be exactly "{expected_id}". '
        "immediate_objective, obstacle, protagonist_action, meaningful_change, and episode_ending must be non-empty strings. "
        "selected_worker_ids must be a unique non-empty string array, and every item must be one of these allowed worker IDs: "
        f"{json.dumps(allowed_worker_ids, ensure_ascii=False, separators=(',', ':'))}. "
        "continuity_constraints must be a unique string array; it may be empty, but entries must be non-empty strings. "
        "Do not return Markdown, explanation, or extra keys."
    )


def build_prompt(stage: str, role: str, payload: dict) -> str:
    target_band = _prose_target_text()
    if stage in JSON_STAGES:
        instruction = "Return exactly one JSON object. Do not use Markdown fences or add explanation."
    elif stage == "writer":
        instruction = (
            f"Write only the complete Korean novel prose for this episode. Target {target_band} so the draft is fully developed "
            "instead of barely clearing the validation floor. Expand causes, actions, dialogue, sensory details, reactions, "
            "transitions, and consequences naturally within the existing plan and canon. Do not add unsupported new settings, "
            "repeat sentences, pad with filler modifiers, summarize yourself, report a character count, or include headings, "
            "scene numbers, SCENE 1, Markdown fences, JSON, or process notes."
        )
        instruction += " " + _prose_structural_guidance() + " " + _writer_plan_guidance()
    elif stage == "revision":
        instruction = (
            f"Write only the complete revised Korean novel prose. Target {target_band} while preserving the draft's facts, "
            "characters, order, point of view, and ending. Return one full replacement from beginning to end; do not append "
            "fragments to the original. Expand rushed actions, dialogue, transitions, and aftermath inside the prose. Do not "
            "change canon outside review requirements, repeat sentences, pad with filler, report a character count, or include "
            "headings, scene numbers, Markdown fences, JSON, or process notes."
        )
        instruction += " " + _prose_structural_guidance() + " " + _revision_guidance(payload)
    else:
        instruction = "Perform only the assigned task."
    if stage == "writer":
        instruction += f" Before answering, silently verify the prose is safely within {target_band}; the validator checks the real length, so never mention the character count or this self-check."
    elif stage == "revision":
        instruction += f" Before answering, silently verify the replacement is safely within {target_band}; the validator checks the real length, so never mention the character count or this self-check."
    worker_id = f"{stage}-{role}"
    worker_rule = f'Use exactly these keys and no others: {{"worker_id":"{worker_id}","role":"{role}","verdict":"OK","primary_finding":"one concise finding","primary_risk":"one concise risk","evidence_refs":["source:current_episode"],"proposal":{{"role":"{role}"}}}}.'
    conflicts = payload.get("open_conflicts", [])
    option_lines = "\n".join(f"OC{index:03d}: {conflict}" for index, conflict in enumerate(conflicts, start=1)) if conflicts else "[]"
    memory_merge_rule = f'''Return exactly these keys and no others: episode_id, confirmed_facts_added, relationship_changes, conflict_ids_resolved, conflicts_opened, promises_added, important_excerpts_added, episode_summary, required_next_episode_continuity, evidence_refs. episode_id must be "{payload.get("episode_id", "")}". Every collection is an array of unique non-empty strings. evidence_refs must be ["final.md"].

CURRENT_OPEN_CONFLICT_OPTIONS
{option_lines}

Select resolved existing conflicts only by ID from CURRENT_OPEN_CONFLICT_OPTIONS. Return selected IDs in conflict_ids_resolved. Never write or paraphrase an existing conflict text in the response. An unknown ID is invalid. If none were resolved, return an empty list. conflicts_opened is only for new conflicts created by canonical final prose. Do not move or duplicate an existing open conflict into conflicts_opened.'''
    if stage == "planning":
        role_rule = f"Analyze only the assigned planning role. Do not write prose. {worker_rule}"
    elif stage == "review":
        role_rule = f"Inspect only the assigned review role. If draft_contract.verdict is REVISE_REQUIRED, evaluate the existing draft as a repairable underlength draft and preserve strengths for one full rewrite. {worker_rule}"
    elif stage == "memory":
        role_rule = f'Canonical final prose is the only evidence for new memory. Existing memory only prevents duplicates and conflicts. Do not restate existing items or convert plans into facts. Return only the assigned role. Use exactly these keys and no others: {{"worker_id":"{worker_id}","role":"{role}","verdict":"OK","primary_finding":"one concise finding","primary_risk":"one concise risk","evidence_refs":["final.md"],"proposal":{{"role":"{role}"}}}}.'
    elif stage == "planning_merge":
        role_rule = _planning_merge_rule(payload)
    elif stage == "review_merge":
        role_rule = f"Return exactly these keys: verdict, strengths_to_preserve, required_changes, evidence_refs. verdict is PASS, REVISE_ONCE, or HOLD. PASS has no required_changes; REVISE_ONCE has one to three. If draft_contract.verdict is REVISE_REQUIRED and there is no HOLD-level defect, verdict must be REVISE_ONCE. Required changes must preserve the draft's strengths, events, and causality; forbid adding a new central conflict; forbid padding with repeated sentences; and require a coherent full rewrite targeting {target_band}."
    elif stage == "memory_merge":
        role_rule = memory_merge_rule + " Do not add an item already present in memory_before. Existing memory is not evidence; final.md is the only evidence for new memory."
    elif stage == "writer":
        role_rule = "Write the supplied episode plan as one coherent canonical prose passage. Do not perform revision work or discuss a draft."
    elif stage == "revision":
        role_rule = f"Rewrite the whole draft as one coherent {target_band} canonical prose passage. Do not append fragments to the original draft."
    else:
        role_rule = "Perform only the assigned task."
    return f"{instruction}\n{role_rule}\nStage: {stage}\nRole: {role}\nInput JSON:\n{json.dumps(payload, ensure_ascii=False)}"
