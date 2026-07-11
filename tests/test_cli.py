# CLI 초기화와 상태 표시를 검증하는 테스트를 제공한다.

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from arc.cli import main


class CliTests(unittest.TestCase):
    def test_init_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "kingdom_archive"
            self.assertEqual(main(["init", str(project)]), 0)
            original = (project / "WORLD_CORE.md").read_text(encoding="utf-8")
            (project / "WORLD_CORE.md").write_text("user content\n", encoding="utf-8")
            self.assertEqual(main(["init", str(project)]), 0)
            self.assertNotEqual((project / "WORLD_CORE.md").read_text(encoding="utf-8"), original)
            self.assertEqual((project / "WORLD_CORE.md").read_text(encoding="utf-8"), "user content\n")

    def test_status_shows_state_missing_artifacts_and_next_work(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "kingdom_archive"
            main(["init", str(project)])
            episode = project / "episodes" / "E001"
            episode.mkdir()
            (episode / "episode.json").write_text(json.dumps({"episode_id": "E001", "state": "PITCHED"}), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status", str(project)]), 0)
            self.assertIn("E001: PITCHED", output.getvalue())
            self.assertIn("pitch.md", output.getvalue())
            self.assertIn("SELECTED", output.getvalue())
