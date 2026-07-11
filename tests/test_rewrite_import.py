# E001 1차 리뷰와 단일 수정 대본 import 계약을 검증한다.

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from arc.rewrite import import_rewrite
from arc.validation import ValidationError

ROOT = Path(__file__).parent.parent


class RewriteImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.project = Path(self.temp.name) / "kingdom_archive"; shutil.copytree(ROOT / "projects" / "kingdom_archive", self.project)
        self.episode = self.project / "episodes" / "E001"
        for name in ("review_1.json", "script_revised.md"): (self.episode / name).unlink(missing_ok=True)
        manifest_path = self.episode / "episode.json"; manifest = json.loads(manifest_path.read_text(encoding="utf-8")); manifest["state"] = "SCRIPT_DRAFT"; manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.review, self.script = Path(self.temp.name) / "review.json", Path(self.temp.name) / "revised.md"; self.write_valid()
        self.ledger = (self.project / "CONTINUITY_LEDGER.json").read_bytes()

    def tearDown(self): self.temp.cleanup()

    def write_valid(self):
        revisions = [{"id": f"RR0{number}"} for number in range(1, 6)]
        self.review.write_text(json.dumps({"schema_version": 1, "project_id": "kingdom_archive", "world_version": "1.0", "episode_id": "E001", "source": {"batch_id": "KA_20260712_001", "pitch_id": "P001", "script_artifact": "script_draft.md"}, "review_round": 1, "verdict": "REWRITE_ONCE", "required_revisions": revisions, "strengths_to_preserve": ["x"], "continuity_constraints": ["x"], "next_allowed_artifact": "script_revised.md", "canon_effect": "NONE"}), encoding="utf-8")
        scenes = "\n".join(f"## 장면 {index}\n[세라]\n[로엔]\n[베른]\n[탈렌]" for index in range(8))
        self.script.write_text(f"# E001\n## 제작 기준\n수정 상태: REVISED\n반영 리뷰: RR01~RR05\n{scenes}\n수신자 명부와 공식 경로\n## 수정 반영 확인\nRR01 RR02 RR03 RR04 RR05", encoding="utf-8")

    def state(self): return json.loads((self.episode / "episode.json").read_text(encoding="utf-8"))["state"]

    def test_import_transition_noop_and_ledger(self):
        self.assertTrue(import_rewrite(self.project, "E001", self.review, self.script)); self.assertEqual(self.state(), "REVISED")
        self.assertEqual((self.project / "CONTINUITY_LEDGER.json").read_bytes(), self.ledger); self.assertFalse(import_rewrite(self.project, "E001", self.review, self.script))

    def test_invalid_review_or_script_leaves_draft_without_artifacts(self):
        review = json.loads(self.review.read_text(encoding="utf-8")); review["verdict"] = "PASS"; self.review.write_text(json.dumps(review), encoding="utf-8")
        with self.assertRaises(ValidationError): import_rewrite(self.project, "E001", self.review, self.script)
        self.assertEqual(self.state(), "SCRIPT_DRAFT"); self.assertFalse((self.episode / "review_1.json").exists())
        self.write_valid(); self.script.write_text("# E001", encoding="utf-8")
        with self.assertRaises(ValidationError): import_rewrite(self.project, "E001", self.review, self.script)
