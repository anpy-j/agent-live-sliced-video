import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from agent_video.jianying import draft_title_base, list_jianying_drafts, next_available_title


class JianyingDraftsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.draft = self.root / "米兰"
        self.draft.mkdir()
        (self.draft / "cover.png").write_bytes(b"png")
        (self.root / "root_meta_info.json").write_text(json.dumps({
            "all_draft_store": [{
                "draft_name": "米兰",
                "draft_fold_path": str(self.draft),
                "draft_cover": "cover.png",
                "tm_draft_modified": 1_700_000_000_000_000,
            }]
        }, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    @patch("agent_video.timeline.discover_virtual_timelines")
    def test_lists_drafts_and_recommends_longest_timeline(self, discover):
        discover.return_value = {"timelines": [
            {"timeline_id": "one", "name": "时间线01", "timeline_duration": 20, "path": "one"},
            {"timeline_id": "two", "name": "主时间线", "timeline_duration": 80, "path": "two"},
        ]}

        drafts = list_jianying_drafts(self.root)

        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["name"], "米兰")
        self.assertEqual(drafts[0]["timeline_count"], 2)
        self.assertEqual(drafts[0]["recommended_timeline"]["timeline_id"], "two")
        self.assertEqual(Path(drafts[0]["cover_path"]), self.draft / "cover.png")

    def test_unique_title_checks_jobs_and_exported_files(self):
        export_dir = self.root / "exports"
        export_dir.mkdir()
        (export_dir / "米兰1001-1").mkdir()

        title = next_available_title(
            "米兰", ["米兰1001"], export_dir, datetime(2026, 10, 1, 9, 0)
        )

        self.assertEqual(title, "米兰1001-2")

    def test_title_base_uses_draft_name_and_current_date(self):
        self.assertEqual(draft_title_base("米兰", datetime(2026, 10, 2)), "米兰1002")


if __name__ == "__main__":
    unittest.main()
