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
            return json.dumps(self._response(stage, role))
        finally:
            with self._lock:
                self.active -= 1
                self.active_by_stage[stage] -= 1

    def _response(self, stage: str, role: str) -> dict:
        if stage in {"planning", "review", "memory"}:
            evidence = ["final.md"] if stage == "memory" else ["source:current_episode"]
            return {"worker_id": f"{stage}-{role}", "role": role, "verdict": "OK", "primary_finding": f"synthetic {role} finding", "primary_risk": f"synthetic {role} risk", "evidence_refs": evidence, "proposal": {"role": role}}
        if stage == "planning_merge":
            return {"episode_id": "SYN001", "immediate_objective": "synthetic objective", "obstacle": "synthetic obstacle", "protagonist_action": "synthetic action", "meaningful_change": "synthetic change", "episode_ending": "synthetic ending", "selected_worker_ids": ["planning-event"], "continuity_constraints": ["synthetic constraint"]}
        if stage == "writer":
            return {"text": "A synthetic character makes one synthetic choice.\n"}
        if stage == "review_merge":
            verdict = {"pass": "PASS", "revise": "REVISE_ONCE", "hold": "HOLD"}[self.scenario]
            changes = ["tighten synthetic transition"] if verdict == "REVISE_ONCE" else []
            return {"verdict": verdict, "strengths_to_preserve": ["synthetic agency"], "required_changes": changes, "evidence_refs": ["draft.md"]}
        if stage == "revision":
            return {"text": "A synthetic character makes one revised synthetic choice.\n"}
        if stage == "memory_merge":
            return {"episode_id": "SYN001", "confirmed_facts_added": ["synthetic fact"], "relationship_changes": ["synthetic relationship change"], "conflicts_resolved": ["synthetic resolved conflict"], "conflicts_opened": ["synthetic opened conflict"], "promises_added": ["synthetic promise"], "important_excerpts_added": ["synthetic choice"], "episode_summary": "synthetic episode summary", "required_next_episode_continuity": ["synthetic next continuity"], "evidence_refs": ["final.md"]}
        raise RuntimeError(f"unknown mock stage: {stage}")
