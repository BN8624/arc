# 합성 episode에서 내용 비의존적 prose mock loop를 검증한다.
import tempfile,unittest,json
from pathlib import Path
from arc.prose import run,status
class ProseTests(unittest.TestCase):
 def test_mock_final_and_noop(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d); (p/'episodes'/'X001').mkdir(parents=True); (p/'WORLD_CORE.md').write_text('world'); (p/'ARCHIVE_RULES.md').write_text('rules')
   self.assertTrue(run(p,'X001')); q=p/'episodes'/'X001'/'prose'; self.assertEqual((q/'draft.md').read_bytes(),(q/'final.md').read_bytes()); self.assertEqual(status(p,'X001')['state'],'FINAL'); self.assertFalse(run(p,'X001'))
