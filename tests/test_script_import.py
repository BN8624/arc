# E001 실제 story gate와 대본 초안 import 계약을 검증한다.

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from arc.script import import_script
from arc.validation import ValidationError

ROOT = Path(__file__).parent.parent


class ScriptImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.project = Path(self.temporary.name) / "kingdom_archive"
        shutil.copytree(ROOT / "projects" / "kingdom_archive", self.project); self.episode = self.project / "episodes" / "E001"
        for name in ("story_gate.json", "script_draft.md"): (self.episode / name).unlink(missing_ok=True)
        manifest_path = self.episode / "episode.json"; manifest = json.loads(manifest_path.read_text(encoding="utf-8")); manifest["state"] = "OUTLINE_READY"; manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.gate, self.script = Path(self.temporary.name) / "gate.json", Path(self.temporary.name) / "script.md"; self.write_valid_inputs()
        self.ledger_before = (self.project / "CONTINUITY_LEDGER.json").read_bytes()

    def tearDown(self): self.temporary.cleanup()

    def write_valid_inputs(self):
        checks = {key: {"result": "PASS"} for key in ("standalone_comprehension", "protagonist_choice", "human_conflict", "ending_answers_opening_conflict", "worldbuilding_not_substitute_for_story", "central_mystery_protection", "production_feasibility")}
        self.gate.write_text(json.dumps({"schema_version": 1, "project_id": "kingdom_archive", "world_version": "1.0", "episode_id": "E001", "source": {"batch_id": "KA_20260712_001", "pitch_id": "P001", "outline_artifact": "outline.md"}, "verdict": "PASS", "checks": checks, "mandatory_script_directives": [{"id": "D1"}], "blocking_issues": [], "next_allowed_artifact": "script_draft.md", "canon_effect": "NONE"}), encoding="utf-8")
        scenes = "\n".join(f"## 장면 {index}\n[세라]\n[로엔]\n[베른]\n[탈렌]" for index in range(8))
        self.script.write_text(f"# E001\n## 제작 기준\n{scenes}\n수신자 명부는 비공식 경로로 폐기된다.\n## 초안 자기검사\n", encoding="utf-8")

    def state(self): return json.loads((self.episode / "episode.json").read_text(encoding="utf-8"))["state"]

    def test_import_transitions_and_preserves_input_bytes_and_ledger(self):
        gate_bytes, script_bytes = self.gate.read_bytes(), self.script.read_bytes(); self.assertTrue(import_script(self.project, "E001", self.gate, self.script))
        self.assertEqual(self.state(), "SCRIPT_DRAFT"); self.assertEqual((self.episode / "story_gate.json").read_bytes(), gate_bytes); self.assertEqual((self.episode / "script_draft.md").read_bytes(), script_bytes)
        self.assertEqual((self.project / "CONTINUITY_LEDGER.json").read_bytes(), self.ledger_before); self.assertFalse((self.episode / "review_1.json").exists())

    def test_identity_verdict_checks_and_directives_fail_closed(self):
        for key, value in (("project_id", "bad"), ("episode_id", "E999"), ("world_version", "2.0"), ("verdict", "FAIL")):
            gate = json.loads(self.gate.read_text(encoding="utf-8")); gate[key] = value; self.gate.write_text(json.dumps(gate), encoding="utf-8")
            with self.assertRaises(ValidationError): import_script(self.project, "E001", self.gate, self.script)
            self.assertEqual(self.state(), "OUTLINE_READY"); self.write_valid_inputs()
        gate = json.loads(self.gate.read_text(encoding="utf-8")); gate["blocking_issues"] = ["block"]; self.gate.write_text(json.dumps(gate), encoding="utf-8")
        with self.assertRaises(ValidationError): import_script(self.project, "E001", self.gate, self.script)
        self.write_valid_inputs(); gate = json.loads(self.gate.read_text(encoding="utf-8")); gate["checks"].pop("human_conflict"); self.gate.write_text(json.dumps(gate), encoding="utf-8")
        with self.assertRaises(ValidationError): import_script(self.project, "E001", self.gate, self.script)
        self.write_valid_inputs(); gate = json.loads(self.gate.read_text(encoding="utf-8")); gate["mandatory_script_directives"] = [{"id": "D1"}, {"id": "D1"}]; self.gate.write_text(json.dumps(gate), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "duplicate"): import_script(self.project, "E001", self.gate, self.script)

    def test_script_and_json_failures_leave_no_partial_artifacts(self):
        self.script.write_text("# E001", encoding="utf-8")
        with self.assertRaises(ValidationError): import_script(self.project, "E001", self.gate, self.script)
        self.assertFalse((self.episode / "story_gate.json").exists()); self.assertEqual(self.state(), "OUTLINE_READY")
        self.write_valid_inputs(); self.script.write_text(self.script.read_text(encoding="utf-8") + "\n[기록관]", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "speakers"): import_script(self.project, "E001", self.gate, self.script)
        self.gate.write_text("{bad", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "malformed"): import_script(self.project, "E001", self.gate, self.script)

    def test_same_input_is_noop_and_different_input_is_rejected(self):
        self.assertTrue(import_script(self.project, "E001", self.gate, self.script)); self.assertFalse(import_script(self.project, "E001", self.gate, self.script))
        self.script.write_text(self.script.read_text(encoding="utf-8") + "changed", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "differs"): import_script(self.project, "E001", self.gate, self.script)
