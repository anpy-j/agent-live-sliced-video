import tempfile
import unittest
from pathlib import Path

from agent_video.db import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_contains_all_stages_and_events(self):
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace="/tmp/job")
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(len(job["stages"]), 5)
        self.assertEqual([stage["stage_id"] for stage in job["stages"]], [
            "material_index", "edit_plan", "validation", "visual_mix", "delivery",
        ])
        self.assertEqual(job["events"][0]["kind"], "job_created")

    def test_stage_transition_is_persisted(self):
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace="/tmp/job")
        self.store.stage_start(job_id, "material_index", "开始")
        self.store.stage_done(job_id, "material_index", "完成", {"duration": 42})
        job = self.store.get_job(job_id)
        stage = job["stages"][0]
        self.assertEqual(stage["status"], "succeeded")
        self.assertEqual(stage["result"]["duration"], 42)

    def test_job_persists_ai_provider_and_model(self):
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace="/tmp/job",
                                       model_provider="workbuddy", model_name="kimi-k2.5")
        job = self.store.get_job(job_id)
        self.assertEqual(job["model_provider"], "workbuddy")
        self.assertEqual(job["model_name"], "kimi-k2.5")

    def test_job_persists_separate_visual_model(self):
        job_id = self.store.create_job(
            title="视觉模型", source_path="/tmp/source.mp4", brief="", mode="standard",
            workspace="/tmp/work", model_provider="opencode", model_name="text-model",
            visual_model_provider="codex", visual_model_name="gpt-5.6-sol")
        job = self.store.get_job(job_id)
        self.assertEqual(job["visual_model_provider"], "codex")
        self.assertEqual(job["visual_model_name"], "gpt-5.6-sol")

    def test_old_rough_cut_stage_is_removed(self):
        job_id = self.store.create_job(title="旧任务", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace="/tmp/job")
        with self.store.connect() as con:
            con.execute("INSERT INTO stages(job_id,stage_id,name,position) VALUES(?,?,?,?)",
                        (job_id, "rough_cut", "粗剪与审片", 80))
            con.execute("UPDATE jobs SET current_stage='rough_cut' WHERE id=?", (job_id,))
        Store(self.store.path)
        job = self.store.get_job(job_id)
        self.assertEqual(job["current_stage"], "validation")
        self.assertNotIn("rough_cut", {stage["stage_id"] for stage in job["stages"]})

    def test_job_persists_products_materials_colors(self):
        job_id = self.store.create_job(
            title="多商品", source_path="/tmp/source.mp4", brief="", mode="standard",
            workspace="/tmp/job", products=["羊毛衫", "百褶裙"], materials=["羊毛", "混纺"],
            colors=["黑色", "燕麦色"]
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["products"], ["羊毛衫", "百褶裙"])
        self.assertEqual(job["materials"], ["羊毛", "混纺"])
        self.assertEqual(job["colors"], ["黑色", "燕麦色"])

    def test_job_persists_subtitle_and_delivery_mode(self):
        job_id = self.store.create_job(
            title="分段导出", source_path="/tmp/source.mp4", brief="", mode="fast",
            workspace="/tmp/job", subtitle_path="/tmp/source.srt", delivery_mode="segments")
        job = self.store.get_job(job_id)
        self.assertEqual(job["subtitle_path"], "/tmp/source.srt")
        self.assertEqual(job["delivery_mode"], "segments")

    def test_job_persists_creative_strategy(self):
        job_id = self.store.create_job(
            title="人设切片", source_path="/tmp/source.mp4", brief="", mode="fast",
            workspace="/tmp/job", creative_strategy="personality")
        self.assertEqual(self.store.get_job(job_id)["creative_strategy"], "personality")


if __name__ == "__main__":
    unittest.main()
