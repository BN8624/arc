# 상태 전이와 설정 제약을 검증하는 테스트를 제공한다.

import json
import unittest
from pathlib import Path

from arc.states import ApprovalGate, EpisodeState, FactLifecycle, TRANSITIONS
from arc.validation import (
    ValidationError,
    validate_canon_delta_application,
    validate_fact_lifecycle,
    validate_transition,
    validate_world_core_finalisation,
)


class TransitionTests(unittest.TestCase):
    def test_fixture_declared_transitions_are_accepted(self):
        fixture = Path(__file__).parent / "fixtures" / "transitions.json"
        for item in json.loads(fixture.read_text(encoding="utf-8"))["allowed"]:
            approvals = {ApprovalGate(value) for value in item["approvals"]}
            validate_transition(EpisodeState(item["from"]), EpisodeState(item["to"]), approvals)

    def test_undeclared_jump_is_rejected(self):
        for current in EpisodeState:
            for target in EpisodeState:
                if target is current or target in TRANSITIONS[current]:
                    continue
                with self.assertRaises(ValidationError):
                    validate_transition(current, target, set())

    def test_gate_requires_explicit_approval(self):
        with self.assertRaises(ValidationError):
            validate_transition(EpisodeState.AWAITING_APPROVAL, EpisodeState.PRODUCTION_READY)

    def test_world_core_finalisation_requires_g1(self):
        with self.assertRaises(ValidationError):
            validate_world_core_finalisation(set())
        validate_world_core_finalisation({ApprovalGate.G1_WORLD_CORE})


class LifecycleTests(unittest.TestCase):
    def test_canon_before_publication_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_fact_lifecycle(FactLifecycle.PROVISIONAL, FactLifecycle.CANON, EpisodeState.AWAITING_APPROVAL)

    def test_canon_after_publication_is_allowed(self):
        validate_fact_lifecycle(FactLifecycle.PROVISIONAL, FactLifecycle.CANON, EpisodeState.PUBLISHED)

    def test_hold_or_rejected_canon_delta_is_rejected(self):
        for state in (EpisodeState.HOLD, EpisodeState.REJECTED):
            with self.assertRaises(ValidationError):
                validate_canon_delta_application(state)
