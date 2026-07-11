# E001 실제 outline import 계약을 검증한다.

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from arc.outline import import_outline
from arc.validation import ValidationError


ROOT = Path(__file__).parent.parent


class OutlineImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "kingdom_archive"
        shutil.copytree(ROOT / "projects" / "kingdom_archive", self.project)
        self.episode = self.project / "episodes" / "E001"
        for name in ("continuity_plan.json", "outline.md"):
            (self.episode / name).unlink(missing_ok=True)
        self.episode.joinpath("episode.json").write_text(json.dumps({"schema_version": 1, "project_id": "kingdom_archive", "episode_id": "E001", "state": "SELECTED", "scenario": "external", "approvals": ["G2_EPISODE_SELECTION"]}), encoding="utf-8")
        self.plan = Path(self.temporary.name) / "plan.json"
        self.outline = Path(self.temporary.name) / "outline.md"
        self.write_valid_inputs()
        self.ledger_before = (self.project / "CONTINUITY_LEDGER.json").read_bytes()

    def tearDown(self):
        self.temporary.cleanup()

    def write_valid_inputs(self):
        self.plan.write_text(json.dumps({"schema_version": 1, "project_id": "kingdom_archive", "world_version": "1.0", "episode_id": "E001", "source": {"batch_id": "KA_20260712_001", "pitch_id": "P001"}, "status": "DRAFT", "time_model": {"era_anchor": "SCHISM"}, "existing_world_refs": [{"id": "EV_SCHISM"}, {"id": "EV_SILENCE"}], "history_contribution": {"count": 1, "intended_status": "CONTESTED", "canon_effect_now": "NONE"}, "draft_entities": [{"status": "DRAFT"}], "production_constraints": {"target_minutes": 7, "speaking_roles": 4, "primary_locations": 2, "estimated_images": 12}}, ensure_ascii=False), encoding="utf-8")
        scenes = "\n".join(f"### {index}. scene" for index in range(8))
        self.outline.write_text(f"# E001\n## 에피소드 약속\n## 이야기의 중심\n## 장면 구성\n{scenes}\n## 이야기 게이트용 자체 점검\n## 대본 단계에서 금지할 것\n", encoding="utf-8")

    def manifest_state(self):
        return json.loads((self.episode / "episode.json").read_text(encoding="utf-8"))["state"]

    def test_import_transitions_and_preserves_bytes_and_ledger(self):
        plan_bytes, outline_bytes = self.plan.read_bytes(), self.outline.read_bytes()
        self.assertTrue(import_outline(self.project, "E001", self.plan, self.outline))
        self.assertEqual(self.manifest_state(), "OUTLINE_READY")
        self.assertEqual((self.episode / "continuity_plan.json").read_bytes(), plan_bytes)
        self.assertEqual((self.episode / "outline.md").read_bytes(), outline_bytes)
        self.assertEqual((self.project / "CONTINUITY_LEDGER.json").read_bytes(), self.ledger_before)
        self.assertFalse((self.episode / "script_draft.md").exists())
        self.assertFalse((self.episode / "production_packet").exists())

    def test_identity_and_contract_failures_leave_selected_without_artifacts(self):
        cases = [("episode_id", "E999"), ("project_id", "other"), ("world_version", "2.0")]
        for key, value in cases:
            plan = json.loads(self.plan.read_text(encoding="utf-8")); plan[key] = value; self.plan.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(ValidationError): import_outline(self.project, "E001", self.plan, self.outline)
            self.assertEqual(self.manifest_state(), "SELECTED")
            self.assertFalse((self.episode / "continuity_plan.json").exists())
            self.write_valid_inputs()
        for field, value in [("batch_id", "BAD"), ("pitch_id", "BAD")]:
            plan = json.loads(self.plan.read_text(encoding="utf-8")); plan["source"][field] = value; self.plan.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(ValidationError): import_outline(self.project, "E001", self.plan, self.outline)
            self.assertEqual(self.manifest_state(), "SELECTED")
            self.write_valid_inputs()

    def test_refs_contribution_entities_markdown_and_json_fail_closed(self):
        mutations = [
            ("refs", lambda plan: plan.update(existing_world_refs=[{"id": "UNKNOWN"}])),
            ("zero count", lambda plan: plan["history_contribution"].update(count=0)),
            ("count", lambda plan: plan["history_contribution"].update(count=2)),
            ("entity", lambda plan: plan.update(draft_entities=[{"status": "CANON"}])),
        ]
        for _, mutate in mutations:
            plan = json.loads(self.plan.read_text(encoding="utf-8")); mutate(plan); self.plan.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaises(ValidationError): import_outline(self.project, "E001", self.plan, self.outline)
            self.assertFalse((self.episode / "outline.md").exists())
            self.write_valid_inputs()
        self.outline.write_text("# E001", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "required sections"):
            import_outline(self.project, "E001", self.plan, self.outline)
        self.assertEqual(self.manifest_state(), "SELECTED")
        self.plan.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "malformed"):
            import_outline(self.project, "E001", self.plan, self.outline)

    def test_same_input_is_noop_and_different_input_is_rejected(self):
        self.assertTrue(import_outline(self.project, "E001", self.plan, self.outline))
        self.assertFalse(import_outline(self.project, "E001", self.plan, self.outline))
        self.outline.write_text(self.outline.read_text(encoding="utf-8") + "changed", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "differs"):
            import_outline(self.project, "E001", self.plan, self.outline)
