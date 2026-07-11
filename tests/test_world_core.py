# ARC-2 세계관 정본과 G1 승인 메타데이터를 검증한다.

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from arc.project import validate_world_core, world_readiness
from arc.validation import ValidationError
from arc.workflow import create_episode


REPOSITORY_ROOT = Path(__file__).parent.parent
ACTUAL_PROJECT = REPOSITORY_ROOT / "projects" / "kingdom_archive"


class WorldCoreTests(unittest.TestCase):
    def copy_project(self):
        temporary = tempfile.TemporaryDirectory()
        project = Path(temporary.name) / "kingdom_archive"
        shutil.copytree(ACTUAL_PROJECT, project)
        return temporary, project

    def test_actual_world_core_and_rules_are_ready(self):
        self.assertEqual(validate_world_core(ACTUAL_PROJECT), "1.0")
        self.assertEqual(world_readiness(ACTUAL_PROJECT), (True, "1.0"))
        core = (ACTUAL_PROJECT / "WORLD_CORE.md").read_text(encoding="utf-8")
        self.assertIn("왕국의 고유 이름", core)
        self.assertIn("왕국 멸망의 진실은 아직 정하지 않았다", core)

    def test_initial_ledger_has_four_open_detail_pillars_and_open_mystery(self):
        ledger = json.loads((ACTUAL_PROJECT / "CONTINUITY_LEDGER.json").read_text(encoding="utf-8"))
        self.assertEqual({event["id"] for event in ledger["events"]}, {"EV_FOUNDING", "EV_SILENCE", "EV_SCHISM", "EV_FALL"})
        self.assertTrue(all(event["status"] == "CANON" and event["date"] is None and event["details"] == "OPEN" for event in ledger["events"]))
        mystery = next(claim for claim in ledger["claims"] if claim["id"] == "CL_SELF_ERASURE")
        self.assertEqual(mystery["status"], "OPEN")

    def test_g1_approved_world_can_start_fixture_episode(self):
        temporary, project = self.copy_project()
        try:
            shutil.rmtree(project / "episodes" / "E001")
            create_episode(project, "E001", "pass")
            self.assertTrue((project / "episodes" / "E001" / "episode.json").exists())
        finally:
            temporary.cleanup()

    def test_world_version_mismatch_is_rejected(self):
        temporary, project = self.copy_project()
        try:
            ledger_path = project / "CONTINUITY_LEDGER.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["world_version"] = "2.0"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "world_version mismatch"):
                validate_world_core(project)
        finally:
            temporary.cleanup()

    def test_document_world_version_mismatch_is_rejected(self):
        temporary, project = self.copy_project()
        try:
            core_path = project / "WORLD_CORE.md"
            core_path.write_text(core_path.read_text(encoding="utf-8").replace("World Version: 1.0", "World Version: 2.0"), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "world_version mismatch"):
                validate_world_core(project)
        finally:
            temporary.cleanup()

    def test_duplicate_ids_and_invalid_status_are_rejected(self):
        temporary, project = self.copy_project()
        try:
            ledger_path = project / "CONTINUITY_LEDGER.json"
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["events"].append(ledger["events"][0].copy())
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "duplicate event ID"):
                validate_world_core(project)
            ledger["events"] = ledger["events"][:4]
            ledger["claims"][0]["status"] = "NOT_A_STATUS"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "invalid claim status"):
                validate_world_core(project)
        finally:
            temporary.cleanup()
