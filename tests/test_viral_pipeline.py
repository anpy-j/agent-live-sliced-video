import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_video.server import Application
from agent_video.viral_pipeline.analyzer import analyze_reference_locally


SAMPLE = (
    "我真的没想到这件衣服上身会这么显瘦！"
    "你看这个腰线，整个人的比例一下就出来了。"
    "搭牛仔裤可以通勤，换一条半裙气质又完全不一样。"
    "面料摸起来也很柔软。"
)


class ViralAnalyzerTest(unittest.TestCase):
    def test_extracts_dynamic_dna(self):
        dna = analyze_reference_locally({
            "transcript": SAMPLE, "duration_seconds": 36,
        })
        self.assertIn(dna["hook"]["mechanism"], {"夸张感受", "结果前置"})
        self.assertIn(dna["primary_focus"], {"上身效果", "穿搭"})
        self.assertGreaterEqual(dna["rhythm"]["sentence_count"], 3)
        self.assertTrue(dna["source_hash"])


class ViralServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = Application(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reference_is_analyzed_and_isolated(self):
        reference = self.app.viral.create_reference({
            "title": "爆款一号", "transcript": SAMPLE,
            "likes": 120000, "duration_seconds": 36,
        })
        self.assertEqual(reference["status"], "ready")
        self.assertEqual(reference["model"], "local-baseline")
        self.assertEqual(reference["likes"], 120000)
        self.assertIsNotNone(reference["dna"])

        self.assertTrue((self.root / "data" / "viral_v2" / "viral_v2.db").is_file())
        con = sqlite3.connect(self.root / "data" / "agent.db")
        try:
            legacy_tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertNotIn("viral_references", legacy_tables)
        self.assertEqual(self.app.store.list_jobs(), [])

    def test_v2_storage_is_lazy_and_does_not_affect_legacy_startup(self):
        fresh_root = self.root / "lazy"
        app = Application(fresh_root)
        self.assertFalse((fresh_root / "data" / "viral_v2").exists())
        self.assertEqual(app.store.list_jobs(), [])
        app.viral.list_references()
        self.assertTrue((fresh_root / "data" / "viral_v2" / "viral_v2.db").is_file())

    def test_v2_job_uses_independent_stages_and_workspace(self):
        reference = self.app.viral.create_reference({
            "title": "上身效果爆款", "transcript": SAMPLE,
        })
        job = self.app.viral.create_job({
            "title": "参考驱动剪辑", "source_text": SAMPLE,
            "target_seconds": "70-120", "reference_mode": "hybrid",
            "reference_ids": [reference["id"]],
        })
        self.assertEqual(job["status"], "draft")
        self.assertEqual([stage["stage_id"] for stage in job["stages"]], [
            "prepare", "profile", "retrieve", "blueprint", "map", "review", "render",
        ])
        self.assertIn(str(Path("data") / "viral_v2" / "jobs"), job["workspace"])
        self.assertEqual(job["reference_ids"], [reference["id"]])
        self.assertEqual(job["references"][0]["title"], "上身效果爆款")
        self.assertEqual(self.app.store.list_jobs(), [])

    def test_reference_driven_job_requires_ready_references(self):
        with self.assertRaisesRegex(ValueError, "至少选择一个"):
            self.app.viral.create_job({
                "title": "没有样本", "source_text": SAMPLE,
                "reference_mode": "hybrid",
            })
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.app.viral.create_job({
                "title": "无效样本", "source_text": SAMPLE,
                "reference_mode": "replicate", "reference_ids": ["vr_missing"],
            })

    def test_free_mode_allows_no_reference(self):
        job = self.app.viral.create_job({
            "title": "自由剪辑", "source_text": SAMPLE,
            "reference_mode": "free", "reference_ids": [],
        })
        self.assertEqual(job["reference_ids"], [])

    def test_validates_reference_input(self):
        with self.assertRaisesRegex(ValueError, "至少需要20个字符"):
            self.app.viral.create_reference({"title": "短文本", "transcript": "太短"})


if __name__ == "__main__":
    unittest.main()
