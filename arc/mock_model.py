# Phase 1에서 결정론적 합성 모델 응답을 제공한다.
from __future__ import annotations

import json
import threading
import time


def transition_adapter_response(payload: dict) -> dict:
    """Deterministic evidence-grounded transition adaptation for fake providers."""
    completed_episode_id = payload["completed_episode_id"]
    plan = payload["rolling_plan"]
    evidence = [{"ref": f"episodes/{completed_episode_id}/final.md", "excerpt": payload["final"][:64]}]
    decisions = []
    for item in plan["immediate_horizon"]:
        decisions.append({"action": "KEEP", "horizon_before": "immediate_horizon", "item_before": item, "horizon_after": "immediate_horizon", "item_after": item, "reason": f"The {completed_episode_id} outcome still requires this objective.", "evidence": list(evidence)})
    near = plan["near_horizon"]
    for index, item in enumerate(near):
        if len(near) >= 2 and index == 0:
            decisions.append({"action": "DROP", "horizon_before": "near_horizon", "item_before": item, "horizon_after": None, "item_after": None, "reason": f"The {completed_episode_id} outcome resolved this direction.", "evidence": list(evidence)})
        elif index == len(near) - 1:
            decisions.append({"action": "CHANGE", "horizon_before": "near_horizon", "item_before": item, "horizon_after": "near_horizon", "item_after": f"adapted direction after {completed_episode_id}", "reason": f"The {completed_episode_id} outcome redirects this item.", "evidence": list(evidence)})
        else:
            decisions.append({"action": "KEEP", "horizon_before": "near_horizon", "item_before": item, "horizon_after": "near_horizon", "item_after": item, "reason": f"The {completed_episode_id} outcome leaves this direction open.", "evidence": list(evidence)})
    decisions.append({"action": "ADD", "horizon_before": None, "item_before": None, "horizon_after": "near_horizon", "item_after": f"deferred hook from {completed_episode_id}", "reason": f"The {completed_episode_id} ending opened a new deferred hook.", "evidence": list(evidence)})
    plan_after = {"immediate_horizon": [decision["item_after"] for decision in decisions if decision["horizon_after"] == "immediate_horizon"], "near_horizon": [decision["item_after"] for decision in decisions if decision["horizon_after"] == "near_horizon"]}
    return {
        "next_episode": {"episode_id": payload["next_episode_id"], "importance": "ordinary", "required_role": plan_after["immediate_horizon"][0]},
        "rolling_plan_after": plan_after,
        "adaptation_decisions": decisions,
        "continuity_satisfied": [],
        "continuity_deferred": list(payload["required_next_episode_continuity"]),
        "adaptation_summary": f"Adapted the rolling plan from {completed_episode_id} results toward {payload['next_episode_id']}.",
        "evidence_refs": sorted({item["ref"] for decision in decisions for item in decision["evidence"]}),
    }


def acceptance_review_response(payload: dict, hold: bool = False) -> dict:
    """Deterministic rubric-grounded acceptance worker response for fake providers."""
    role = payload["dimension"]
    episode_ids = payload["episode_ids"]
    catalog = payload["evidence_catalog"]
    by_kind: dict[str, list[dict]] = {}
    by_ref: dict[str, dict] = {}
    for entry in catalog:
        by_kind.setdefault(entry["kind"], []).append(entry)
        by_ref[entry["ref"]] = entry
    rule = payload["coverage_rule"]
    coverage: list[str] = []

    def add(entry: dict) -> None:
        if entry["ref"] not in coverage:
            coverage.append(entry["ref"])

    def selected(selector: str) -> list[str]:
        if selector == "all":
            return list(episode_ids)
        if selector == "first":
            return [episode_ids[0]]
        if selector == "last":
            return [episode_ids[-1]]
        return list(episode_ids[1:])

    for kind in sorted(rule["required_kind_episodes"]):
        wanted = set(selected(rule["required_kind_episodes"][kind]))
        for entry in by_kind[kind]:
            if entry["episode_id"] in wanted:
                add(entry)
    if rule["required_transitions"] == "all":
        for entry in by_kind["transition"]:
            add(entry)
    for kind in sorted(rule["minimum_kind_episodes"]):
        for entry in by_kind[kind]:
            covered = {by_ref[ref]["episode_id"] for ref in coverage if by_ref[ref]["kind"] == kind}
            if len(covered) >= rule["minimum_kind_episodes"][kind]:
                break
            add(entry)
    if rule["require_first_and_last_episode"]:
        for target in (episode_ids[0], episode_ids[-1]):
            if not any(by_ref[ref]["episode_id"] == target and by_ref[ref]["kind"] != "transition" for ref in coverage):
                add(by_kind["episode_final"][episode_ids.index(target)])
    for entry in by_kind["episode_final"]:
        if len(coverage) >= rule["minimum_granular_refs"]:
            break
        add(entry)

    criterion_results = []
    for criterion in payload["criteria"]:
        evidence = []
        for kind in criterion["required_evidence_kinds"]:
            entry = next((by_ref[ref] for ref in coverage if by_ref[ref]["kind"] == kind), None)
            if entry is None:
                entry = by_kind[kind][0]
                add(entry)
            evidence.append({"ref": entry["ref"], "excerpt": entry["content"][:80]})
        criterion_results.append({"criterion_id": criterion["criterion_id"], "result": "PASS", "finding": f"The cited artifacts keep {criterion['criterion_id']} consistent across the reviewed episodes.", "evidence": evidence})
    critical = None
    if hold:
        held = criterion_results[0]
        held["result"] = "HOLD"
        held["finding"] = f"The obligation tracked by {held['criterion_id']} is not carried into the cited next episode source."
        critical = {"criterion_id": held["criterion_id"], "finding": held["finding"]}
    pass_results = [result for result in criterion_results if result["result"] == "PASS"]
    strengths = [{"criterion_id": result["criterion_id"], "strength": f"Evidence for {result['criterion_id']} stays traceable to verbatim excerpts of the cited artifacts.", "evidence": [dict(item) for item in result["evidence"]]} for result in pass_results[:1]]
    evidence_refs = sorted({item["ref"] for result in criterion_results for item in result["evidence"]} | {item["ref"] for strength in strengths for item in strength["evidence"]})
    return {
        "worker_id": f"pilot_review-{role}",
        "role": role,
        "verdict": "OK",
        "primary_finding": f"Dimension {role} was reviewed against {len(criterion_results)} rubric criteria with catalog evidence.",
        "primary_risk": f"A later episode could regress {payload['criteria'][0]['criterion_id']} without new evidence.",
        "evidence_refs": evidence_refs,
        "proposal": {"dimension_result": "HOLD" if hold else "PASS", "criterion_results": criterion_results, "critical_finding": critical, "strengths": strengths, "coverage_refs": sorted(set(coverage) | set(evidence_refs))},
    }


class MockModelClient:
    def __init__(self, scenario: str, delays: dict[str, float] | None = None, fail_at: str | None = None, malformed_at: str | None = None):
        self.scenario, self.delays, self.fail_at, self.malformed_at = scenario, delays or {}, fail_at, malformed_at
        self.calls: list[tuple[str, str, str]] = []
        self.active = self.max_active = 0
        self.active_by_stage: dict[str, int] = {}
        self.max_active_by_stage: dict[str, int] = {}
        self._lock = threading.Lock()

    def generate(self, *, stage: str, role: str, prompt: str) -> str:
        with self._lock:
            self.calls.append((stage, role, prompt))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.active_by_stage[stage] = self.active_by_stage.get(stage, 0) + 1
            self.max_active_by_stage[stage] = max(self.max_active_by_stage.get(stage, 0), self.active_by_stage[stage])
        try:
            time.sleep(self.delays.get(role, self.delays.get(stage, 0)))
            marker = f"{stage}:{role}"
            if self.fail_at in {stage, marker}:
                raise RuntimeError(f"injected failure at {marker}")
            if self.malformed_at in {stage, marker}:
                return "{malformed"
            return json.dumps(self._response(stage, role, prompt))
        finally:
            with self._lock:
                self.active -= 1
                self.active_by_stage[stage] -= 1

    def _response(self, stage: str, role: str, prompt: str) -> dict:
        payload = json.loads(prompt)
        episode_id = payload.get("episode_id") or payload.get("context", {}).get("episode_id") or "SYN001"
        if stage in {"planning", "review", "memory"}:
            evidence = ["final.md"] if stage == "memory" else ["source:current_episode"]
            return {"worker_id": f"{stage}-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": evidence, "proposal": {"role": role}}
        if stage == "pilot_review":
            hold = payload.get("scenario") == "pilot_hold" and role == "continuity"
            return {"worker_id": f"pilot_review-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": ["pilot_evidence_packet.json"], "proposal": {"dimension_result": "HOLD" if hold else "PASS", "critical_finding": "synthetic cross-episode continuity hold" if hold else None}}
        if stage == "planning_merge":
            return {"episode_id": episode_id, "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]}
        if stage == "writer":
            return {"text": "A synthetic character makes one synthetic choice.\n"}
        if stage == "review_merge":
            verdict = {"pass": "PASS", "revise": "REVISE_ONCE", "hold": "HOLD"}[self.scenario]
            changes = ["tighten synthetic transition"] if verdict == "REVISE_ONCE" else []
            return {"verdict": verdict, "strengths_to_preserve": ["synthetic agency"], "required_changes": changes, "evidence_refs": ["draft.md"]}
        if stage == "revision":
            return {"text": "A synthetic character makes one revised synthetic choice.\n"}
        if stage == "transition":
            return transition_adapter_response(payload)
        if stage == "memory_merge":
            return {"episode_id": episode_id, "confirmed_facts_added": [f"synthetic fact {episode_id}"], "relationship_changes": [f"synthetic relationship change {episode_id}"], "conflicts_resolved": ["synthetic resolved conflict"] if "synthetic resolved conflict" in payload.get("open_conflicts", []) else [], "conflicts_opened": [f"synthetic opened conflict {episode_id}"], "promises_added": [f"synthetic promise {episode_id}"], "important_excerpts_added": [f"synthetic choice {episode_id}"], "episode_summary": f"synthetic episode summary {episode_id}", "required_next_episode_continuity": [f"synthetic next continuity {episode_id}"], "evidence_refs": ["final.md"]}
        raise RuntimeError(f"unknown mock stage: {stage}")
