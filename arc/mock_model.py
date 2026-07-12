# Phase 1에서 결정론적 합성 모델 응답을 제공한다.
from __future__ import annotations

import json
import threading
import time


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
        if stage == "memory_merge":
            return {"episode_id": episode_id, "confirmed_facts_added": [f"synthetic fact {episode_id}"], "relationship_changes": [f"synthetic relationship change {episode_id}"], "conflicts_resolved": ["synthetic resolved conflict"] if "synthetic resolved conflict" in payload.get("open_conflicts", []) else [], "conflicts_opened": [f"synthetic opened conflict {episode_id}"], "promises_added": [f"synthetic promise {episode_id}"], "important_excerpts_added": [f"synthetic choice {episode_id}"], "episode_summary": f"synthetic episode summary {episode_id}", "required_next_episode_continuity": [f"synthetic next continuity {episode_id}"], "evidence_refs": ["final.md"]}
        raise RuntimeError(f"unknown mock stage: {stage}")
