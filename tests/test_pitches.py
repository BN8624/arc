# ARC-3 pitch import와 사용자 선택 계약을 검증한다.

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from arc.pitches import import_pitch_set, list_pitches, select_pitch
from arc.validation import ValidationError


ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "tests" / "fixtures" / "arc3" / "valid_pitch_set.json"


class PitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "kingdom_archive"
        shutil.copytree(ROOT / "projects" / "kingdom_archive", self.project)
        shutil.rmtree(self.project / "episodes" / "E001")
        self.source = Path(self.temporary.name) / "pitch_set.json"
        shutil.copyfile(SOURCE, self.source)
        self.ledger_before = (self.project / "CONTINUITY_LEDGER.json").read_bytes()

    def tearDown(self):
        self.temporary.cleanup()

    def read_source(self):
        return json.loads(self.source.read_text(encoding="utf-8"))

    def write_source(self, value):
        self.source.write_text(json.dumps(value), encoding="utf-8")

    def test_valid_import_creates_markdown_and_warnings_without_ledger_change(self):
        warnings = import_pitch_set(self.project, self.source)
        batch = self.project / "pitches" / "fixture_batch_01"
        self.assertTrue((batch / "pitch_set.md").exists())
        self.assertTrue(any("production_shape" in warning for warning in warnings))
        self.assertEqual((self.project / "CONTINUITY_LEDGER.json").read_bytes(), self.ledger_before)
        self.assertEqual(len(list_pitches(self.project, "fixture_batch_01")), 5)

    def test_candidate_count_duplicate_and_unknown_reference_are_rejected(self):
        value = self.read_source(); value["candidates"] = value["candidates"][:4]; self.write_source(value)
        with self.assertRaisesRegex(ValidationError, "exactly 5"):
            import_pitch_set(self.project, self.source)
        value = json.loads(SOURCE.read_text(encoding="utf-8")); value["candidates"].append(value["candidates"][0].copy()); value["candidates"][-1]["pitch_id"] = "P006"; value["candidates"][-1]["working_title"] = "Fixture Six"; self.write_source(value)
        with self.assertRaisesRegex(ValidationError, "exactly 5"):
            import_pitch_set(self.project, self.source)
        value = self.read_source(); value["candidates"] = self.read_source()["candidates"] + []
        value = json.loads(SOURCE.read_text(encoding="utf-8")); value["candidates"][1]["pitch_id"] = "P001"; self.write_source(value)
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            import_pitch_set(self.project, self.source)
        value = json.loads(SOURCE.read_text(encoding="utf-8")); value["candidates"][0]["world_refs"] = ["UNKNOWN"]; self.write_source(value)
        with self.assertRaisesRegex(ValidationError, "world_refs"):
            import_pitch_set(self.project, self.source)

    def test_forbidden_status_direct_answer_and_malformed_json_are_rejected(self):
        value = self.read_source(); value["candidates"][0]["history_contribution"]["intended_status"] = "CANON"; self.write_source(value)
        with self.assertRaises(ValidationError): import_pitch_set(self.project, self.source)
        value = json.loads(SOURCE.read_text(encoding="utf-8")); value["candidates"][0]["central_mystery_relation"] = "DIRECT_ANSWER"; self.write_source(value)
        with self.assertRaisesRegex(ValidationError, "direct answer"):
            import_pitch_set(self.project, self.source)
        self.source.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "malformed"):
            import_pitch_set(self.project, self.source)

    def test_select_records_g2_selected_episode_and_preserves_other_candidates_and_ledger(self):
        import_pitch_set(self.project, self.source)
        self.assertTrue(select_pitch(self.project, "fixture_batch_01", "P001", "E001"))
        episode = self.project / "episodes" / "E001"
        manifest = json.loads((episode / "episode.json").read_text(encoding="utf-8"))
        source = json.loads((episode / "pitch_source.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "SELECTED")
        self.assertIn("G2_EPISODE_SELECTION", manifest["approvals"])
        self.assertEqual(source["selected_by"], "user")
        self.assertTrue(source["source_hash"])
        self.assertEqual(source["candidate"]["pitch_id"], "P001")
        self.assertTrue((episode / "selection.json").exists())
        self.assertEqual((self.project / "CONTINUITY_LEDGER.json").read_bytes(), self.ledger_before)
        self.assertTrue((self.project / "pitches" / "fixture_batch_01" / "pitch_set.json").exists())
        self.assertFalse((episode / "outline.md").exists())
        self.assertFalse((episode / "script_draft.md").exists())
        self.assertFalse(select_pitch(self.project, "fixture_batch_01", "P001", "E001"))
        with self.assertRaisesRegex(ValidationError, "different selection"):
            select_pitch(self.project, "fixture_batch_01", "P002", "E001")

    def test_existing_batch_and_failed_selection_leave_no_episode(self):
        import_pitch_set(self.project, self.source)
        with self.assertRaisesRegex(ValidationError, "already exists"):
            import_pitch_set(self.project, self.source)
        with self.assertRaisesRegex(ValidationError, "not found"):
            select_pitch(self.project, "fixture_batch_01", "BAD", "E001")
        self.assertFalse((self.project / "episodes" / "E001").exists())

    def test_diversity_warning_does_not_block_import_or_selection(self):
        value = self.read_source()
        for candidate in value["candidates"]:
            candidate["record_form"] = "letter"
        self.write_source(value)
        warnings = import_pitch_set(self.project, self.source)
        self.assertIn("diversity: fewer than 3 record forms", warnings)
        self.assertTrue(select_pitch(self.project, "fixture_batch_01", "P001", "E001"))
