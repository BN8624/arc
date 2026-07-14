# 다섯 회차 pilot 입력과 전환 및 수용 판정을 검증한다.
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import ContractError, validate_fixture


PILOT_REVIEW_ROLES = ["readability", "character_consistency", "continuity", "rolling_plan_adaptation", "memory_correctness", "narrative_weight", "episode_to_episode_interest"]
STABLE_MEMORY_FIELDS = ("series_compass", "world_rules", "characters", "confirmed_facts", "relationship_state", "open_conflicts", "promises", "episode_summaries", "important_excerpts")
TRANSITION_SCHEMA_VERSION = 2
TRANSITION_CONTRACT_VERSION = 1
TRANSITION_ACTIONS = ("KEEP", "CHANGE", "DROP", "ADD")
ROLLING_PLAN_HORIZONS = ("immediate_horizon", "near_horizon")
ROLLING_PLAN_HORIZON_LIMITS = {"immediate_horizon": 4, "near_horizon": 8}
TRANSITION_EVIDENCE_FILES = ("episode_plan.json", "final.md", "memory_after.json", "memory_update.json")
TRANSITION_EXCERPT_MIN_CHARACTERS = 8
TRANSITION_EXCERPT_MAX_CHARACTERS = 400
TRANSITION_FORBIDDEN_MARKERS = ("synthetic transition toward", "synthetic pilot role", "Synthetic plan adapts")
TRANSITION_RESPONSE_FIELDS = ("next_episode", "rolling_plan_after", "adaptation_decisions", "continuity_satisfied", "continuity_deferred", "adaptation_summary", "evidence_refs")
EPISODE_IMPORTANCE_VALUES = ("ordinary", "major", "pivot")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_pilot_fixture(value: dict) -> dict:
    if set(value) != {"pilot_id", "episode_ids", "initial_source"} or not isinstance(value["pilot_id"], str) or not value["pilot_id"]:
        raise ContractError("invalid pilot fixture")
    episode_ids = value["episode_ids"]
    if not isinstance(episode_ids, list) or len(episode_ids) != 5 or len(episode_ids) != len(set(episode_ids)) or any(not isinstance(item, str) or not item for item in episode_ids):
        raise ContractError("pilot requires five unique episode IDs")
    validate_fixture(value["initial_source"])
    if value["initial_source"]["current_episode"]["episode_id"] != episode_ids[0]:
        raise ContractError("initial episode ID does not match pilot")
    return value


def rolling_plan_hash(plan: dict) -> str:
    return hashlib.sha256(canonical_bytes(plan)).hexdigest()


def validate_rolling_plan(plan: object, *, require_immediate: bool, code: str = "TRANSITION_PLAN_INVALID") -> dict:
    if not isinstance(plan, dict) or set(plan) != set(ROLLING_PLAN_HORIZONS):
        raise ContractError("rolling plan must have exactly immediate_horizon and near_horizon", code)
    for horizon in ROLLING_PLAN_HORIZONS:
        items = plan[horizon]
        if not isinstance(items, list) or any(not isinstance(item, str) or not item.strip() for item in items):
            raise ContractError("rolling plan items must be non-blank strings", code)
        if len(items) > ROLLING_PLAN_HORIZON_LIMITS[horizon]:
            raise ContractError(f"rolling plan {horizon} exceeds {ROLLING_PLAN_HORIZON_LIMITS[horizon]} items", code)
    combined = plan["immediate_horizon"] + plan["near_horizon"]
    if len(combined) != len(set(combined)):
        raise ContractError("rolling plan items must be unique across horizons", code)
    if require_immediate and not plan["immediate_horizon"]:
        raise ContractError("adapted immediate horizon requires at least one item", code)
    return plan


def validate_adaptation_decisions(decisions: object, plan_before: dict, plan_after: dict) -> dict[str, int]:
    """Enforce complete before/after accounting and return per-action counts."""
    code = "TRANSITION_DECISION_ACCOUNTING_INVALID"
    if not isinstance(decisions, list) or not decisions:
        raise ContractError("adaptation decisions are required", code)
    required = {"action", "horizon_before", "item_before", "horizon_after", "item_after", "reason", "evidence"}
    before_sequence = [(horizon, item) for horizon in ROLLING_PLAN_HORIZONS for item in plan_before[horizon]]
    before_items = {item for _, item in before_sequence}
    consumed: list[tuple[str, str]] = []
    rebuilt: dict[str, list[str]] = {horizon: [] for horizon in ROLLING_PLAN_HORIZONS}
    counts = {action: 0 for action in TRANSITION_ACTIONS}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != required or decision["action"] not in TRANSITION_ACTIONS:
            raise ContractError("invalid adaptation decision shape", code)
        if not isinstance(decision["reason"], str) or not decision["reason"].strip():
            raise ContractError("adaptation decision requires a reason", code)
        action = decision["action"]
        counts[action] += 1
        if action == "ADD":
            if decision["horizon_before"] is not None or decision["item_before"] is not None:
                raise ContractError("ADD cannot reference a before item", code)
            if decision["item_after"] in before_items:
                raise ContractError("ADD item already exists in the before plan", code)
        else:
            if (decision["horizon_before"], decision["item_before"]) not in before_sequence:
                raise ContractError("decision consumes an unknown before item", code)
            consumed.append((decision["horizon_before"], decision["item_before"]))
        if action == "DROP":
            if decision["horizon_after"] is not None or decision["item_after"] is not None:
                raise ContractError("DROP cannot produce an after item", code)
            continue
        if decision["horizon_after"] not in ROLLING_PLAN_HORIZONS or not isinstance(decision["item_after"], str) or not decision["item_after"].strip():
            raise ContractError("decision after target is invalid", code)
        if action == "KEEP" and decision["item_after"] != decision["item_before"]:
            raise ContractError("KEEP cannot change the item text", code)
        if action == "CHANGE" and decision["item_after"] == decision["item_before"]:
            raise ContractError("CHANGE requires a different item text", code)
        rebuilt[decision["horizon_after"]].append(decision["item_after"])
    if len(consumed) != len(set(consumed)):
        raise ContractError("before plan item consumed by two decisions", code)
    if consumed != before_sequence:
        if set(consumed) != set(before_sequence):
            raise ContractError("before plan items are not fully accounted", code)
        raise ContractError("decisions must consume the before plan in canonical order", code)
    if rebuilt != {horizon: list(plan_after[horizon]) for horizon in ROLLING_PLAN_HORIZONS}:
        raise ContractError("decisions do not reconstruct the adapted plan", code)
    return counts


def validate_transition_evidence(decisions: list[dict], evidence_refs: object, completed_episode_id: str, run_dir: Path) -> None:
    code = "TRANSITION_EVIDENCE_INVALID"
    allowed = {f"episodes/{completed_episode_id}/{name}" for name in TRANSITION_EVIDENCE_FILES}
    texts: dict[str, str] = {}
    seen: set[str] = set()
    for decision in decisions:
        evidence = decision["evidence"]
        if not isinstance(evidence, list) or not evidence:
            raise ContractError("adaptation decision requires evidence", code)
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"ref", "excerpt"}:
                raise ContractError("invalid evidence item shape", code)
            ref, excerpt = item["ref"], item["excerpt"]
            if ref not in allowed:
                raise ContractError("evidence ref outside the completed episode artifacts", code)
            if not isinstance(excerpt, str) or not excerpt.strip() or not TRANSITION_EXCERPT_MIN_CHARACTERS <= len(excerpt) <= TRANSITION_EXCERPT_MAX_CHARACTERS:
                raise ContractError("evidence excerpt length is invalid", code)
            if ref not in texts:
                path = run_dir / ref
                if not path.exists():
                    raise ContractError("evidence artifact does not exist", code)
                texts[ref] = path.read_text(encoding="utf-8")
            if excerpt not in texts[ref]:
                raise ContractError("evidence excerpt not found in the artifact", code)
            seen.add(ref)
    if not isinstance(evidence_refs, list) or evidence_refs != sorted(seen):
        raise ContractError("evidence refs must equal the sorted unique decision refs", code)


def validate_transition_response(value: object) -> dict:
    if not isinstance(value, dict):
        raise ContractError("transition response must be a JSON object", "TRANSITION_RESPONSE_NOT_OBJECT")
    if set(value) != set(TRANSITION_RESPONSE_FIELDS):
        raise ContractError("transition response fields mismatch", "TRANSITION_FIELDS_MISMATCH")
    return value


def transition_action_counts(value: dict) -> dict[str, int]:
    counts = {action: 0 for action in TRANSITION_ACTIONS}
    for decision in value.get("adaptation_decisions", []):
        if isinstance(decision, dict) and decision.get("action") in counts:
            counts[decision["action"]] += 1
    return counts


def validate_transition(value: dict, source: dict, next_episode_id: str, run_dir: Path) -> dict:
    if not isinstance(value, dict):
        raise ContractError("transition must be a JSON object", "TRANSITION_RESPONSE_NOT_OBJECT")
    if value.get("schema_version") == 1:
        raise ContractError("LEGACY_SYNTHETIC_TRANSITION", "LEGACY_SYNTHETIC_TRANSITION")
    required = {"schema_version", "completed_episode_id", "next_episode_id", "transition_input_hash", "next_source_hash", "next_episode", "rolling_plan_before_hash", "rolling_plan_after", "adaptation_decisions", "continuity_satisfied", "continuity_deferred", "adaptation_summary", "evidence_refs"}
    completed_episode_id = source["current_episode"]["episode_id"]
    if set(value) != required or value["schema_version"] != TRANSITION_SCHEMA_VERSION or not isinstance(value["transition_input_hash"], str) or not isinstance(value["next_source_hash"], str) or value["completed_episode_id"] != completed_episode_id or value["next_episode_id"] != next_episode_id:
        raise ContractError("invalid pilot transition identity", "TRANSITION_FIELDS_MISMATCH")
    if any(marker in json.dumps(value, ensure_ascii=False) for marker in TRANSITION_FORBIDDEN_MARKERS):
        raise ContractError("forbidden synthetic transition marker", "TRANSITION_SYNTHETIC_MARKER")
    if not isinstance(value["adaptation_summary"], str) or not value["adaptation_summary"].strip():
        raise ContractError("invalid adaptation summary", "TRANSITION_FIELDS_MISMATCH")
    plan_before = validate_rolling_plan(source["rolling_plan"], require_immediate=False)
    if value["rolling_plan_before_hash"] != rolling_plan_hash(plan_before):
        raise ContractError("rolling plan before hash mismatch", "TRANSITION_FIELDS_MISMATCH")
    plan_after = validate_rolling_plan(value["rolling_plan_after"], require_immediate=True)
    validate_adaptation_decisions(value["adaptation_decisions"], plan_before, plan_after)
    validate_transition_evidence(value["adaptation_decisions"], value["evidence_refs"], completed_episode_id, Path(run_dir))
    next_episode = value["next_episode"]
    if not isinstance(next_episode, dict) or set(next_episode) != {"episode_id", "importance", "required_role"} or next_episode["episode_id"] != next_episode_id or next_episode["importance"] not in EPISODE_IMPORTANCE_VALUES:
        raise ContractError("invalid transition next episode", "TRANSITION_NEXT_ROLE_INVALID")
    if not isinstance(next_episode["required_role"], str) or next_episode["required_role"] != plan_after["immediate_horizon"][0]:
        raise ContractError("next episode role must be the first adapted immediate item", "TRANSITION_NEXT_ROLE_INVALID")
    expected = source["required_next_episode_continuity"]
    satisfied, deferred = value["continuity_satisfied"], value["continuity_deferred"]
    if not all(isinstance(items, list) and len(items) == len(set(items)) for items in (satisfied, deferred)) or set(satisfied) & set(deferred) or set(satisfied) | set(deferred) != set(expected):
        raise ContractError("invalid continuity partition", "TRANSITION_CONTINUITY_INVALID")
    return value


ACCEPTANCE_SCHEMA_VERSION = 2
ACCEPTANCE_RUBRIC_VERSION = 1
ACCEPTANCE_EXCERPT_MIN_CHARACTERS = 8
ACCEPTANCE_EXCERPT_MAX_CHARACTERS = 400
ACCEPTANCE_EVIDENCE_KINDS = ("episode_final", "episode_plan", "episode_review", "episode_memory_update", "episode_memory_after", "episode_source", "transition")
ACCEPTANCE_EPISODE_FILES = (("final.md", "episode_final"), ("episode_plan.json", "episode_plan"), ("review_decision.json", "episode_review"), ("memory_update.json", "episode_memory_update"), ("memory_after.json", "episode_memory_after"))
ACCEPTANCE_GENERIC_QUESTION_MARKER = "Evaluate pilot dimension:"
ACCEPTANCE_FORBIDDEN_MARKERS = ("synthetic continuity evidence", ACCEPTANCE_GENERIC_QUESTION_MARKER)
ACCEPTANCE_FORBIDDEN_STRENGTH_MARKERS = ("synthetic strength", "good continuity", "works well")
ACCEPTANCE_COVERAGE_SELECTORS = ("all", "first", "last", "after_first")
ACCEPTANCE_COVERAGE_RULE_FIELDS = ("required_kind_episodes", "required_transitions", "minimum_kind_episodes", "require_first_and_last_episode", "minimum_granular_refs")
ACCEPTANCE_WORKER_PROPOSAL_FIELDS = ("dimension_result", "criterion_results", "critical_finding", "strengths", "coverage_refs")
ACCEPTANCE_MAX_STRENGTHS_PER_DIMENSION = 2
ACCEPTANCE_MAX_STRENGTHS = 14

ACCEPTANCE_RUBRIC = (
    {
        "dimension": "readability",
        "title": "Readability",
        "question": "Can a reader follow each episode's events and the flow between episodes as readable narrative prose?",
        "criteria": (
            {"criterion_id": "readability.local_clarity", "question": "Are each episode's events, acting subject, objective, obstacle, and outcome expressed so a reader can follow them?", "pass_rule": "Every cited final expresses who acts, what they pursue, what blocks them, and what results, with intact causal links between scenes.", "hold_rule": "Hold when actors or actions are unclear, key scenes are elided into summary, causal links between scenes break, or plan or memo wording leaks into the prose.", "required_evidence_kinds": ("episode_final",)},
            {"criterion_id": "readability.cross_episode_flow", "question": "Can a reader connect the previous episode's outcome to the current situation when the episode changes?", "pass_rule": "Each cited episode opening stays connected to the previous episode's outcome and situation.", "hold_rule": "Hold on unexplained time, place, or objective shifts, previous outcomes that disappear in the next episode, or an episode that restarts as an independent new story.", "required_evidence_kinds": ("episode_final",)},
            {"criterion_id": "readability.prose_delivery", "question": "Does the final prose function as a readable narrative rather than an outline, report, or script?", "pass_rule": "The cited finals deliver the core conflict through scenes with enough density and varied sentences.", "hold_rule": "Hold when the core conflict is only summarized in narration, scene density is insufficient, or repetitive sentences and structures block understanding.", "required_evidence_kinds": ("episode_final",)},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_final": "all"}, "required_transitions": None, "minimum_kind_episodes": {}, "require_first_and_last_episode": True, "minimum_granular_refs": 5},
    },
    {
        "dimension": "character_consistency",
        "title": "Character consistency",
        "question": "Do characters keep stable identities, motivated actions, and grounded relationship changes across the five episodes?",
        "criteria": (
            {"criterion_id": "character_consistency.stable_identity", "question": "Do stable traits, motivations, and already confirmed facts about each character stay consistent between episodes?", "pass_rule": "Cited finals and stored character state agree on stable traits, motivations, and confirmed facts across episodes.", "hold_rule": "Hold when a stable trait, motivation, or already confirmed fact is contradicted between episodes.", "required_evidence_kinds": ("episode_final", "episode_source")},
            {"criterion_id": "character_consistency.agency_and_motivation", "question": "Do major actions arise from each character's goals, pressures, and previous choices?", "pass_rule": "Cited major actions trace back to the character's goals, pressures, or earlier choices.", "hold_rule": "Hold when a major action appears without grounding in the character's goals, pressures, or previous choices.", "required_evidence_kinds": ("episode_final",)},
            {"criterion_id": "character_consistency.relationship_progression", "question": "Do relationship changes build on the previous relationship state and actual scenes without appearing or vanishing abruptly?", "pass_rule": "Cited relationship changes follow from the stored relationship state and on-page scenes.", "hold_rule": "Hold when a relationship changes, appears, or disappears abruptly without grounding in prior state and actual scenes.", "required_evidence_kinds": ("episode_final", "episode_memory_after")},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_source": "first", "episode_memory_after": "last"}, "required_transitions": None, "minimum_kind_episodes": {"episode_final": 3}, "require_first_and_last_episode": True, "minimum_granular_refs": 5},
    },
    {
        "dimension": "continuity",
        "title": "Continuity",
        "question": "Are continuity obligations, confirmed facts, and open conflicts carried across every episode boundary without loss or contradiction?",
        "criteria": (
            {"criterion_id": "continuity.required_obligations", "question": "Is each source's required next-episode continuity handled as satisfied or deferred exactly in the following transition?", "pass_rule": "Every cited required continuity item is partitioned into satisfied or deferred by the following transition.", "hold_rule": "Hold when a required continuity item is dropped, duplicated, or mishandled; cite the transition and the next episode source together.", "required_evidence_kinds": ("transition", "episode_source")},
            {"criterion_id": "continuity.fact_and_conflict_chain", "question": "Do confirmed facts, open conflicts, promises, and their outcomes persist between episodes without omission or contradiction?", "pass_rule": "Cited facts, conflicts, and promises stay consistent between the finals and the stored memory chain.", "hold_rule": "Hold when a confirmed fact, open conflict, or promise is lost or contradicted between episodes.", "required_evidence_kinds": ("episode_final", "episode_memory_after")},
            {"criterion_id": "continuity.transition_to_source", "question": "Are each transition's deferred continuity and memory update actually reflected in the next episode source?", "pass_rule": "Cited deferred continuity and memory updates appear in the next episode source.", "hold_rule": "Hold when deferred continuity or a memory update is missing from the next episode source; cite the transition and the next source together.", "required_evidence_kinds": ("transition", "episode_source")},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_source": "after_first"}, "required_transitions": "all", "minimum_kind_episodes": {}, "require_first_and_last_episode": False, "minimum_granular_refs": 8},
    },
    {
        "dimension": "rolling_plan_adaptation",
        "title": "Rolling-plan adaptation",
        "question": "Do the four transitions adapt the rolling plan through fully accounted decisions grounded in episode evidence and applied to the next episode?",
        "criteria": (
            {"criterion_id": "rolling_plan_adaptation.accounting_validity", "question": "Do the KEEP, CHANGE, DROP, and ADD decisions of all four transitions fully account for the before and after plans?", "pass_rule": "Every cited transition consumes each before-plan item exactly once and its decisions rebuild the after plan exactly.", "hold_rule": "Hold when a before-plan item is unconsumed, consumed twice, or the decisions do not rebuild the after plan.", "required_evidence_kinds": ("transition",)},
            {"criterion_id": "rolling_plan_adaptation.evidence_grounding", "question": "Are the non-KEEP actions grounded in actual episode artifact evidence rather than plan hash changes?", "pass_rule": "Cited CHANGE, DROP, or ADD decisions carry verbatim excerpts from the completed episode's artifacts; a mere plan hash change is not PASS grounds.", "hold_rule": "Hold when a non-KEEP decision lacks grounding in the completed episode's artifacts.", "required_evidence_kinds": ("transition", "episode_final")},
            {"criterion_id": "rolling_plan_adaptation.next_episode_application", "question": "Are the adapted immediate plan and the next episode's required role actually reflected in the next source and planning?", "pass_rule": "Cited next sources carry the adapted rolling plan and the next episode role equals the first adapted immediate item.", "hold_rule": "Hold when the adapted plan or the required role is not reflected in the next episode source.", "required_evidence_kinds": ("transition", "episode_source")},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_source": "after_first"}, "required_transitions": "all", "minimum_kind_episodes": {}, "require_first_and_last_episode": False, "minimum_granular_refs": 8},
    },
    {
        "dimension": "memory_correctness",
        "title": "Memory correctness",
        "question": "Are the memory updates grounded in the final prose, free of hallucinated entries, and preserving of stable state?",
        "criteria": (
            {"criterion_id": "memory_correctness.grounded_updates", "question": "Are confirmed facts, relationships, conflicts, promises, excerpts, and the episode summary grounded in the actual final?", "pass_rule": "Cited memory update entries correspond to events present in the final prose.", "hold_rule": "Hold when a memory update entry has no support in the final prose.", "required_evidence_kinds": ("episode_memory_update", "episode_final")},
            {"criterion_id": "memory_correctness.no_hallucinated_memory", "question": "Were any facts, relationships, conflicts, or promises absent from the prose added to the memory update?", "pass_rule": "No cited memory update entry introduces content absent from the final prose.", "hold_rule": "Hold when the memory update adds a fact, relationship, conflict, or promise that does not appear in the prose.", "required_evidence_kinds": ("episode_memory_update", "episode_final")},
            {"criterion_id": "memory_correctness.stable_state_preservation", "question": "Do the stable memory fields that transitions may not modify remain byte-equivalent to the previous memory state?", "pass_rule": "Cited memory-after states keep the stable fields identical to the previous episode's memory.", "hold_rule": "Hold when a stable memory field changes across a transition.", "required_evidence_kinds": ("episode_memory_after",)},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_memory_update": "all", "episode_memory_after": "all"}, "required_transitions": None, "minimum_kind_episodes": {"episode_final": 1}, "require_first_and_last_episode": True, "minimum_granular_refs": 11},
    },
    {
        "dimension": "narrative_weight",
        "title": "Narrative weight",
        "question": "Does each episode deliver meaningful, dramatized change with causal consequences instead of static repetition?",
        "criteria": (
            {"criterion_id": "narrative_weight.meaningful_change", "question": "Does at least one of character, relationship, conflict, or situation actually change in each episode?", "pass_rule": "Each cited episode shows a concrete change in character, relationship, conflict, or situation.", "hold_rule": "Hold when an episode ends with no meaningful change in character, relationship, conflict, or situation.", "required_evidence_kinds": ("episode_plan", "episode_final")},
            {"criterion_id": "narrative_weight.causal_consequence", "question": "Do the key events influence later choices or states?", "pass_rule": "Cited key events produce visible consequences in later choices or states.", "hold_rule": "Hold when a key event leaves no trace on later choices or states.", "required_evidence_kinds": ("episode_final",)},
            {"criterion_id": "narrative_weight.dramatized_weight", "question": "Are important events delivered through sufficient scenes and reactions rather than bare summary?", "pass_rule": "Cited important events unfold in scenes with actions and reactions.", "hold_rule": "Hold when an important event is delivered only as a summary without scene or reaction.", "required_evidence_kinds": ("episode_final",)},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_plan": "all", "episode_final": "all"}, "required_transitions": None, "minimum_kind_episodes": {"episode_final": 3}, "require_first_and_last_episode": True, "minimum_granular_refs": 10},
    },
    {
        "dimension": "episode_to_episode_interest",
        "title": "Episode-to-episode interest",
        "question": "Does each episode pay off earlier promises while creating a concrete reason to read the next episode?",
        "criteria": (
            {"criterion_id": "episode_to_episode_interest.payoff_and_hook", "question": "Does each episode repay part of an earlier promise or conflict while creating the next point of interest?", "pass_rule": "Cited episodes both repay an earlier promise or conflict and open a new point of interest.", "hold_rule": "Hold when an episode neither repays an earlier promise nor creates a next point of interest.", "required_evidence_kinds": ("episode_final",)},
            {"criterion_id": "episode_to_episode_interest.progression", "question": "Do pressure, choices, and consequences progress instead of repeating the same situation or conflict?", "pass_rule": "Cited adjacent episodes escalate or shift pressure, choices, and consequences.", "hold_rule": "Hold when adjacent episodes repeat the same situation or conflict without progression.", "required_evidence_kinds": ("episode_final",)},
            {"criterion_id": "episode_to_episode_interest.next_episode_pull", "question": "Does a reason to keep reading exist between each episode ending and the next episode's role?", "pass_rule": "Cited endings connect to the next episode's role with earlier payoff as grounds; quoting only a closing sentence is not PASS grounds.", "hold_rule": "Hold when an ending and the next episode's role give the reader no reason to continue.", "required_evidence_kinds": ("episode_final", "transition")},
        ),
        "coverage_rule": {"required_kind_episodes": {"episode_final": "all"}, "required_transitions": "all", "minimum_kind_episodes": {}, "require_first_and_last_episode": True, "minimum_granular_refs": 9},
    },
)


def validate_acceptance_rubric(rubric: object = ACCEPTANCE_RUBRIC) -> dict[str, dict]:
    """Enforce the canonical rubric registry contract and index it by dimension."""
    if not isinstance(rubric, (list, tuple)) or [item.get("dimension") if isinstance(item, dict) else None for item in rubric] != PILOT_REVIEW_ROLES:
        raise ContractError("acceptance rubric must define exactly the seven pilot review dimensions in order")
    titles, questions, criterion_ids = set(), set(), set()
    for item in rubric:
        if set(item) != {"dimension", "title", "question", "criteria", "coverage_rule"}:
            raise ContractError("acceptance rubric dimension fields mismatch")
        title, question = item["title"], item["question"]
        if not isinstance(title, str) or not title.strip() or not isinstance(question, str) or not question.strip():
            raise ContractError("acceptance rubric title and question are required")
        if ACCEPTANCE_GENERIC_QUESTION_MARKER in question:
            raise ContractError("acceptance rubric question is generic")
        if title in titles or question in questions:
            raise ContractError("acceptance rubric title and question must be unique")
        titles.add(title)
        questions.add(question)
        criteria = item["criteria"]
        if not isinstance(criteria, (list, tuple)) or not 2 <= len(criteria) <= 4:
            raise ContractError("acceptance rubric dimension requires two to four criteria")
        for criterion in criteria:
            if not isinstance(criterion, dict) or set(criterion) != {"criterion_id", "question", "pass_rule", "hold_rule", "required_evidence_kinds"}:
                raise ContractError("acceptance rubric criterion fields mismatch")
            identifier = criterion["criterion_id"]
            if not isinstance(identifier, str) or not identifier.startswith(f"{item['dimension']}.") or identifier in criterion_ids:
                raise ContractError("acceptance rubric criterion id must be unique and dimension-prefixed")
            criterion_ids.add(identifier)
            if any(not isinstance(criterion[key], str) or not criterion[key].strip() for key in ("question", "pass_rule", "hold_rule")):
                raise ContractError("acceptance rubric criterion rules are required")
            kinds = criterion["required_evidence_kinds"]
            if not isinstance(kinds, (list, tuple)) or not kinds or len(kinds) != len(set(kinds)) or set(kinds) - set(ACCEPTANCE_EVIDENCE_KINDS):
                raise ContractError("acceptance rubric criterion evidence kinds are unknown")
        rule = item["coverage_rule"]
        if not isinstance(rule, dict) or set(rule) != set(ACCEPTANCE_COVERAGE_RULE_FIELDS):
            raise ContractError("acceptance rubric coverage rule fields mismatch")
        for mapping in (rule["required_kind_episodes"], rule["minimum_kind_episodes"]):
            if not isinstance(mapping, dict) or set(mapping) - set(ACCEPTANCE_EVIDENCE_KINDS):
                raise ContractError("acceptance rubric coverage kinds are unknown")
        if any(selector not in ACCEPTANCE_COVERAGE_SELECTORS for selector in rule["required_kind_episodes"].values()):
            raise ContractError("acceptance rubric coverage selector is unknown")
        if any(not isinstance(minimum, int) or minimum < 1 for minimum in rule["minimum_kind_episodes"].values()):
            raise ContractError("acceptance rubric coverage minimum is invalid")
        if rule["required_transitions"] not in {None, "all"} or not isinstance(rule["require_first_and_last_episode"], bool) or not isinstance(rule["minimum_granular_refs"], int) or rule["minimum_granular_refs"] < 1:
            raise ContractError("acceptance rubric coverage rule is invalid")
    return {item["dimension"]: item for item in rubric}


def acceptance_catalog_plan(episode_ids: list[str]) -> list[tuple[str, str, str]]:
    """Deterministic (ref, kind, episode_id) order for the acceptance evidence catalog."""
    entries = [(f"episodes/{episode_id}/{name}", kind, episode_id) for episode_id in episode_ids for name, kind in ACCEPTANCE_EPISODE_FILES]
    entries += [(f"episode_sources/{episode_id}.json", "episode_source", episode_id) for episode_id in episode_ids]
    entries += [(f"transitions/{episode_id}_to_{next_id}.json", "transition", episode_id) for episode_id, next_id in zip(episode_ids, episode_ids[1:])]
    return entries


def validate_acceptance_catalog(catalog: object, episode_ids: list[str]) -> dict[str, dict]:
    """Validate catalog shape, order, and content hashes; index entries by ref."""
    code = "PILOT_REVIEW_EVIDENCE_INVALID"
    expected = acceptance_catalog_plan(episode_ids)
    if not isinstance(catalog, list) or len(catalog) != len(expected):
        raise ContractError("acceptance evidence catalog refs mismatch", code)
    index: dict[str, dict] = {}
    for entry, (ref, kind, episode_id) in zip(catalog, expected):
        if not isinstance(entry, dict) or set(entry) != {"ref", "kind", "episode_id", "sha256", "content"} or entry["ref"] != ref or entry["kind"] != kind or entry["episode_id"] != episode_id:
            raise ContractError("acceptance evidence catalog entry mismatch", code)
        content = entry["content"]
        if not isinstance(content, str) or not content or entry["sha256"] != hashlib.sha256(content.encode("utf-8")).hexdigest():
            raise ContractError("acceptance evidence catalog content hash mismatch", code)
        index[ref] = entry
    return index


def _selector_episode_ids(selector: str, episode_ids: list[str]) -> list[str]:
    if selector == "all":
        return list(episode_ids)
    if selector == "first":
        return [episode_ids[0]]
    if selector == "last":
        return [episode_ids[-1]]
    return list(episode_ids[1:])


def validate_acceptance_coverage(dimension_def: dict, coverage_refs: object, catalog_index: dict[str, dict], episode_ids: list[str]) -> None:
    code = "PILOT_REVIEW_COVERAGE_INCOMPLETE"
    if not isinstance(coverage_refs, list) or not coverage_refs or coverage_refs != sorted(set(coverage_refs)):
        raise ContractError("coverage refs must be a sorted unique list", code)
    if any(ref not in catalog_index for ref in coverage_refs):
        raise ContractError("coverage ref outside the evidence catalog", code)
    rule = dimension_def["coverage_rule"]
    covered_by_kind: dict[str, set[str]] = {}
    episode_coverage: set[str] = set()
    transition_refs: set[str] = set()
    for ref in coverage_refs:
        entry = catalog_index[ref]
        covered_by_kind.setdefault(entry["kind"], set()).add(entry["episode_id"])
        if entry["kind"] == "transition":
            transition_refs.add(ref)
        else:
            episode_coverage.add(entry["episode_id"])
    for kind, selector in rule["required_kind_episodes"].items():
        if set(_selector_episode_ids(selector, episode_ids)) - covered_by_kind.get(kind, set()):
            raise ContractError(f"coverage misses required {kind} episodes", code)
    if rule["required_transitions"] == "all":
        expected = {f"transitions/{episode_id}_to_{next_id}.json" for episode_id, next_id in zip(episode_ids, episode_ids[1:])}
        if expected - transition_refs:
            raise ContractError("coverage misses required transitions", code)
    for kind, minimum in rule["minimum_kind_episodes"].items():
        if len(covered_by_kind.get(kind, set())) < minimum:
            raise ContractError(f"coverage misses minimum distinct {kind} episodes", code)
    if rule["require_first_and_last_episode"] and not {episode_ids[0], episode_ids[-1]} <= episode_coverage:
        raise ContractError("coverage must include the first and last episodes", code)
    if len(coverage_refs) < rule["minimum_granular_refs"]:
        raise ContractError("coverage granular ref count is insufficient", code)


def _validate_acceptance_evidence_items(evidence: object, allowed_kinds: list[str] | tuple[str, ...], require_all_kinds: bool, catalog_index: dict[str, dict], code: str = "PILOT_REVIEW_EVIDENCE_INVALID") -> list[str]:
    """Validate evidence items against the catalog and return their refs."""
    if not isinstance(evidence, list) or not evidence:
        raise ContractError("acceptance evidence items are required", code)
    seen: set[tuple[str, str]] = set()
    kinds_seen: set[str] = set()
    refs: list[str] = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"ref", "excerpt"}:
            raise ContractError("invalid acceptance evidence item shape", code)
        ref, excerpt = item["ref"], item["excerpt"]
        entry = catalog_index.get(ref)
        if entry is None:
            raise ContractError("acceptance evidence ref outside the evidence catalog", code)
        if entry["kind"] not in allowed_kinds:
            raise ContractError("acceptance evidence kind is not allowed for this criterion", code)
        if not isinstance(excerpt, str) or not excerpt.strip() or not ACCEPTANCE_EXCERPT_MIN_CHARACTERS <= len(excerpt) <= ACCEPTANCE_EXCERPT_MAX_CHARACTERS:
            raise ContractError("acceptance evidence excerpt length is invalid", code)
        if excerpt not in entry["content"]:
            raise ContractError("acceptance evidence excerpt not found in the artifact", code)
        if (ref, excerpt) in seen:
            raise ContractError("duplicate acceptance evidence item", code)
        seen.add((ref, excerpt))
        kinds_seen.add(entry["kind"])
        refs.append(ref)
    if require_all_kinds and set(allowed_kinds) - kinds_seen:
        raise ContractError("acceptance evidence misses a required artifact kind", code)
    return refs


def validate_acceptance_criterion_results(dimension_def: dict, results: object, catalog_index: dict[str, dict]) -> dict[str, dict]:
    code = "PILOT_REVIEW_CRITERIA_MISMATCH"
    criteria = list(dimension_def["criteria"])
    if not isinstance(results, list) or [result.get("criterion_id") if isinstance(result, dict) else None for result in results] != [criterion["criterion_id"] for criterion in criteria]:
        raise ContractError("criterion results must cover the dimension rubric exactly once each in order", code)
    by_id: dict[str, dict] = {}
    for criterion, result in zip(criteria, results):
        if set(result) != {"criterion_id", "result", "finding", "evidence"} or result["result"] not in {"PASS", "HOLD"}:
            raise ContractError("invalid criterion result shape", code)
        if not isinstance(result["finding"], str) or not result["finding"].strip():
            raise ContractError("criterion finding is required", code)
        _validate_acceptance_evidence_items(result["evidence"], criterion["required_evidence_kinds"], True, catalog_index)
        by_id[criterion["criterion_id"]] = result
    return by_id


def validate_acceptance_strengths(dimension_def: dict, strengths: object, criterion_results: dict[str, dict], catalog_index: dict[str, dict]) -> None:
    code = "PILOT_REVIEW_STRENGTH_INVALID"
    if not isinstance(strengths, list) or len(strengths) > ACCEPTANCE_MAX_STRENGTHS_PER_DIMENSION:
        raise ContractError("dimension strengths must be a list of at most two entries", code)
    criteria = {criterion["criterion_id"]: criterion for criterion in dimension_def["criteria"]}
    texts: set[str] = set()
    for strength in strengths:
        if not isinstance(strength, dict) or set(strength) != {"criterion_id", "strength", "evidence"}:
            raise ContractError("invalid strength shape", code)
        criterion = criteria.get(strength["criterion_id"])
        if criterion is None or criterion_results[strength["criterion_id"]]["result"] != "PASS":
            raise ContractError("strength must be linked to a PASS criterion of this dimension", code)
        text = strength["strength"]
        if not isinstance(text, str) or not text.strip() or any(marker in text for marker in ACCEPTANCE_FORBIDDEN_STRENGTH_MARKERS + ACCEPTANCE_FORBIDDEN_MARKERS):
            raise ContractError("strength requires a concrete non-generic statement", code)
        if text in texts:
            raise ContractError("duplicate strength statement", code)
        texts.add(text)
        _validate_acceptance_evidence_items(strength["evidence"], criterion["required_evidence_kinds"], False, catalog_index, code)


def validate_acceptance_worker(value: object, role: str, catalog: list[dict], episode_ids: list[str]) -> dict:
    """Validate one dimension worker against the canonical rubric and evidence catalog."""
    if not isinstance(value, dict):
        raise ContractError("pilot review response must be a JSON object", "PILOT_REVIEW_RESPONSE_NOT_OBJECT")
    rubric = validate_acceptance_rubric()
    dimension_def = rubric.get(role)
    if dimension_def is None:
        raise ContractError("unknown pilot review dimension", "PILOT_REVIEW_FIELDS_MISMATCH")
    catalog_index = validate_acceptance_catalog(catalog, episode_ids)
    fields = "PILOT_REVIEW_FIELDS_MISMATCH"
    if set(value) != {"worker_id", "role", "verdict", "primary_finding", "primary_risk", "evidence_refs", "proposal"} or value["worker_id"] != f"pilot_review-{role}" or value["role"] != role:
        raise ContractError("pilot review worker fields mismatch", fields)
    if any(not isinstance(value[key], str) or not value[key].strip() for key in ("verdict", "primary_finding", "primary_risk")):
        raise ContractError("pilot review worker finding and risk are required", fields)
    if any(marker in json.dumps(value, ensure_ascii=False) for marker in ACCEPTANCE_FORBIDDEN_MARKERS):
        raise ContractError("forbidden acceptance marker", "PILOT_REVIEW_STRENGTH_INVALID")
    proposal = value["proposal"]
    if not isinstance(proposal, dict) or set(proposal) != set(ACCEPTANCE_WORKER_PROPOSAL_FIELDS):
        raise ContractError("pilot review proposal fields mismatch", fields)
    if proposal["dimension_result"] not in {"PASS", "HOLD"}:
        raise ContractError("invalid dimension result", "PILOT_REVIEW_RESULT_INCONSISTENT")
    criterion_results = validate_acceptance_criterion_results(dimension_def, proposal["criterion_results"], catalog_index)
    hold_ids = [criterion_id for criterion_id, result in criterion_results.items() if result["result"] == "HOLD"]
    if (proposal["dimension_result"] == "PASS") != (not hold_ids):
        raise ContractError("dimension result contradicts its criterion results", "PILOT_REVIEW_RESULT_INCONSISTENT")
    critical = proposal["critical_finding"]
    if proposal["dimension_result"] == "PASS":
        if critical is not None:
            raise ContractError("PASS dimension cannot carry a critical finding", "PILOT_REVIEW_CRITICAL_FINDING_INVALID")
    elif not isinstance(critical, dict) or set(critical) != {"criterion_id", "finding"} or critical["criterion_id"] not in hold_ids or not isinstance(critical["finding"], str) or not critical["finding"].strip():
        raise ContractError("HOLD dimension requires a critical finding linked to a HOLD criterion", "PILOT_REVIEW_CRITICAL_FINDING_INVALID")
    validate_acceptance_strengths(dimension_def, proposal["strengths"], criterion_results, catalog_index)
    if proposal["dimension_result"] == "PASS" and not proposal["strengths"]:
        raise ContractError("PASS dimension requires at least one grounded strength", "PILOT_REVIEW_STRENGTH_INVALID")
    evidence_refs = sorted({item["ref"] for result in criterion_results.values() for item in result["evidence"]} | {item["ref"] for strength in proposal["strengths"] for item in strength["evidence"]})
    if value["evidence_refs"] != evidence_refs:
        raise ContractError("worker evidence refs must equal the sorted unique criterion and strength refs", "PILOT_REVIEW_EVIDENCE_INVALID")
    validate_acceptance_coverage(dimension_def, proposal["coverage_refs"], catalog_index, episode_ids)
    if set(evidence_refs) - set(proposal["coverage_refs"]):
        raise ContractError("coverage refs must include every cited evidence ref", "PILOT_REVIEW_COVERAGE_INCOMPLETE")
    return value


def aggregate_pilot_acceptance(workers: list[dict]) -> dict:
    """Deterministically aggregate validated dimension workers into acceptance schema v2."""
    if [worker.get("role") for worker in workers] != PILOT_REVIEW_ROLES:
        raise ContractError("acceptance aggregation requires the seven dimension workers in rubric order")
    rubric = {item["dimension"]: item for item in ACCEPTANCE_RUBRIC}
    dimension_results = {worker["role"]: worker["proposal"]["dimension_result"] for worker in workers}
    verdict = "PASS" if all(result == "PASS" for result in dimension_results.values()) else "HOLD"
    critical_findings: list[dict] = []
    strengths: list[dict] = []
    for worker in workers:
        role, proposal = worker["role"], worker["proposal"]
        criterion_order = [criterion["criterion_id"] for criterion in rubric[role]["criteria"]]
        results_by_id = {result["criterion_id"]: result for result in proposal["criterion_results"]}
        if proposal["dimension_result"] == "HOLD":
            critical = proposal["critical_finding"]
            finding = {"dimension": role, "criterion_id": critical["criterion_id"], "finding": critical["finding"], "evidence": results_by_id[critical["criterion_id"]]["evidence"]}
            if finding not in critical_findings:
                critical_findings.append(finding)
        for strength in sorted(proposal["strengths"], key=lambda item: criterion_order.index(item["criterion_id"])):
            entry = {"dimension": role, **strength}
            if entry not in strengths:
                strengths.append(entry)
    if (verdict == "PASS") != (not critical_findings):
        raise ContractError("acceptance verdict contradicts its critical findings")
    if verdict == "PASS" and not strengths:
        raise ContractError("acceptance PASS requires at least one grounded strength")
    if len(strengths) > ACCEPTANCE_MAX_STRENGTHS:
        raise ContractError("acceptance strengths exceed the aggregation limit")
    evidence_refs = sorted({item["ref"] for finding in critical_findings for item in finding["evidence"]} | {item["ref"] for strength in strengths for item in strength["evidence"]})
    return {"schema_version": ACCEPTANCE_SCHEMA_VERSION, "rubric_version": ACCEPTANCE_RUBRIC_VERSION, "verdict": verdict, "dimension_results": dimension_results, "critical_findings": critical_findings, "strengths_to_preserve": strengths, "evidence_refs": evidence_refs}


def validate_grounded_pilot_acceptance(value: object, workers: list[dict]) -> dict:
    """Fail closed unless the stored acceptance equals the deterministic aggregation."""
    if not isinstance(value, dict):
        raise ContractError("invalid pilot acceptance")
    if value.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        raise ContractError("legacy generic acceptance is not grounded acceptance evidence", "LEGACY_GENERIC_ACCEPTANCE")
    if any(marker in json.dumps(value, ensure_ascii=False) for marker in ACCEPTANCE_FORBIDDEN_MARKERS):
        raise ContractError("forbidden acceptance marker", "PILOT_REVIEW_STRENGTH_INVALID")
    if value != aggregate_pilot_acceptance(workers):
        raise ContractError("pilot acceptance does not match its deterministic aggregation")
    return value


def validate_pilot_acceptance(value: dict, evidence_refs: list[str]) -> dict:
    required = {"verdict", "dimension_results", "critical_findings", "strengths_to_preserve", "evidence_refs"}
    if set(value) != required or value["verdict"] not in {"PASS", "HOLD"} or set(value["dimension_results"]) != set(PILOT_REVIEW_ROLES):
        raise ContractError("invalid pilot acceptance")
    dimensions = value["dimension_results"]
    if any(result not in {"PASS", "HOLD"} for result in dimensions.values()) or not isinstance(value["critical_findings"], list) or len(value["critical_findings"]) > 7 or len(value["critical_findings"]) != len(set(value["critical_findings"])):
        raise ContractError("invalid pilot acceptance findings")
    if value["verdict"] == "PASS" and (any(result != "PASS" for result in dimensions.values()) or value["critical_findings"]):
        raise ContractError("pilot PASS has unresolved findings")
    if value["verdict"] == "HOLD" and (all(result == "PASS" for result in dimensions.values()) or not value["critical_findings"]):
        raise ContractError("pilot HOLD lacks critical finding")
    if not isinstance(value["evidence_refs"], list) or not value["evidence_refs"] or set(value["evidence_refs"]) - set(evidence_refs):
        raise ContractError("invalid pilot acceptance evidence")
    return value
