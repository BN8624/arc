# ARC-1 E001 fixture 수직 흐름을 검증한다.

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from arc.project import initialise_project
from arc.states import ApprovalGate, EpisodeState
from arc.validation import ValidationError
from arc.workflow import FIXTURE_ROOT, advance, approve, create_episode, run_until_blocked, status


class Arc1WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "kingdom_archive"
        initialise_project(self.project)
        self.ledger_before = (self.project / "CONTINUITY_LEDGER.json").read_text(encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def create_and_open_gates(self, scenario="pass"):
        create_episode(self.project, "E001", scenario)
        self.assertTrue(approve(self.project, None, ApprovalGate.G1_WORLD_CORE))
        self.assertTrue(approve(self.project, "E001", ApprovalGate.G2_EPISODE_SELECTION))

    def finish_to_ready(self, scenario="pass"):
        self.create_and_open_gates(scenario)
        state, reason = run_until_blocked(self.project, "E001")
        self.assertEqual(state, EpisodeState.AWAITING_APPROVAL)
        self.assertIn("G3", reason)
        self.assertTrue(approve(self.project, "E001", ApprovalGate.G3_FINAL_SCRIPT_PRODUCTION))
        state, reason = run_until_blocked(self.project, "E001")
        self.assertEqual(state, EpisodeState.PRODUCTION_READY)
        self.assertIsNone(reason)
        return self.project / "episodes" / "E001"

    def test_direct_pass_path_creates_only_reached_artifacts_and_preserves_ledger(self):
        episode = self.finish_to_ready("pass")
        self.assertTrue((episode / "production_packet" / "manifest.json").exists())
        self.assertFalse((episode / "script_revised.md").exists())
        self.assertFalse((episode / "review_2.json").exists())
        self.assertEqual((self.project / "CONTINUITY_LEDGER.json").read_text(encoding="utf-8"), self.ledger_before)

    def test_rewrite_path_reaches_production_ready(self):
        episode = self.finish_to_ready("rewrite")
        self.assertTrue((episode / "script_revised.md").exists())
        self.assertTrue((episode / "review_2.json").exists())

    def test_second_review_failure_holds_and_cannot_continue(self):
        self.create_and_open_gates("hold")
        state, reason = run_until_blocked(self.project, "E001")
        self.assertEqual(state, EpisodeState.HOLD)
        self.assertIsNone(reason)
        with self.assertRaises(ValidationError):
            advance(self.project, "E001")

    def test_g1_g2_and_g3_are_required(self):
        create_episode(self.project, "E001", "pass")
        self.assertTrue(approve(self.project, "E001", ApprovalGate.G2_EPISODE_SELECTION))
        with self.assertRaisesRegex(ValidationError, "G1"):
            advance(self.project, "E001")
        self.assertTrue(approve(self.project, None, ApprovalGate.G1_WORLD_CORE))
        with tempfile.TemporaryDirectory() as other:
            project = Path(other) / "kingdom_archive"
            initialise_project(project)
            create_episode(project, "E001", "pass")
            approve(project, None, ApprovalGate.G1_WORLD_CORE)
            with self.assertRaisesRegex(ValidationError, "G2"):
                advance(project, "E001")
        state, _ = run_until_blocked(self.project, "E001")
        self.assertEqual(state, EpisodeState.AWAITING_APPROVAL)
        with self.assertRaisesRegex(ValidationError, "G3"):
            advance(self.project, "E001")

    def test_missing_artifact_and_malformed_fixture_are_rejected(self):
        self.create_and_open_gates()
        (self.project / "episodes" / "E001" / "pitch.md").unlink()
        with self.assertRaisesRegex(ValidationError, "missing required artifacts"):
            advance(self.project, "E001")
        with tempfile.TemporaryDirectory() as fixture_directory:
            fixture_root = Path(fixture_directory) / "arc1"
            shutil.copytree(FIXTURE_ROOT, fixture_root)
            (fixture_root / "pitches.json").write_text("{bad", encoding="utf-8")
            with patch("arc.workflow.FIXTURE_ROOT", fixture_root):
                with self.assertRaisesRegex(ValidationError, "malformed JSON"):
                    create_episode(self.project, "E002", "pass")

    def test_soft_and_hard_conflicts_preserve_or_block(self):
        episode = self.finish_to_ready("soft")
        continuity = json.loads((episode / "continuity_check.json").read_text(encoding="utf-8"))
        self.assertEqual(continuity["result"], "SOFT_CONFLICT")
        self.assertIn("구전", continuity["evidence"])
        with tempfile.TemporaryDirectory() as other:
            project = Path(other) / "kingdom_archive"
            initialise_project(project)
            create_episode(project, "E001", "hard")
            approve(project, None, ApprovalGate.G1_WORLD_CORE)
            approve(project, "E001", ApprovalGate.G2_EPISODE_SELECTION)
            state, _ = run_until_blocked(project, "E001")
            self.assertEqual(state, EpisodeState.HOLD)

    def test_approval_is_idempotent_and_create_never_overwrites(self):
        create_episode(self.project, "E001", "pass")
        self.assertTrue(approve(self.project, None, ApprovalGate.G1_WORLD_CORE))
        self.assertFalse(approve(self.project, None, ApprovalGate.G1_WORLD_CORE))
        with self.assertRaisesRegex(ValidationError, "already exists"):
            create_episode(self.project, "E001", "pass")

    def test_status_reports_block_reason_without_mutating(self):
        create_episode(self.project, "E001", "pass")
        state, missing, reason = status(self.project, "E001")
        self.assertEqual(state, EpisodeState.PITCHED)
        self.assertEqual(missing, [])
        self.assertIn("G1", reason)
