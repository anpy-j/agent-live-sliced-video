import tempfile
import unittest
from pathlib import Path

from agent_video.db import STAGE_DEFINITIONS, Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "agent.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_job_contains_lean_pipeline_stages(self):
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       workspace="/tmp/job")
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual([stage["stage_id"] for stage in job["stages"]],
                         [stage_id for stage_id, _, _ in STAGE_DEFINITIONS])
        self.assertEqual([stage["stage_id"] for stage in job["stages"]],
                         ["asr", "filter", "judge", "order", "render"])
        self.assertEqual(job["events"][0]["kind"], "job_created")

    def test_stage_transition_is_persisted(self):
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       workspace="/tmp/job")
        self.store.stage_start(job_id, "asr", "开始")
        self.store.stage_done(job_id, "asr", "完成", {"duration": 42})
        job = self.store.get_job(job_id)
        stage = job["stages"][0]
        self.assertEqual(stage["status"], "succeeded")
        self.assertEqual(stage["result"]["duration"], 42)

    def test_delete_job_removes_job_and_cascades(self):
        job_id = self.store.create_job(title="待删除", source_path="/tmp/source.mp4",
                                       workspace="/tmp/job")
        self.store.add_event(job_id, "asr", "info", "test_event", "测试事件")
        artifact_file = Path(self.tmp.name) / "test_artifact.txt"
        artifact_file.write_text("hello", encoding="utf-8")
        self.store.add_artifact(job_id, "render", "log", "测试产物", artifact_file)

        self.assertIsNotNone(self.store.get_job(job_id))
        deleted = self.store.delete_job(job_id)
        self.assertTrue(deleted)
        self.assertIsNone(self.store.get_job(job_id))
        with self.store.connect() as con:
            stages_count = con.execute("SELECT count(*) FROM stages WHERE job_id=?", (job_id,)).fetchone()[0]
            events_count = con.execute("SELECT count(*) FROM events WHERE job_id=?", (job_id,)).fetchone()[0]
            artifacts_count = con.execute("SELECT count(*) FROM artifacts WHERE job_id=?", (job_id,)).fetchone()[0]
        self.assertEqual(stages_count, 0)
        self.assertEqual(events_count, 0)
        self.assertEqual(artifacts_count, 0)

    def test_reset_job_resets_stages_and_artifacts(self):
        job_id = self.store.create_job(title="待重置", source_path="/tmp/source.mp4",
                                       workspace="/tmp/job")
        self.store.stage_start(job_id, "asr", "进行中")
        self.store.stage_done(job_id, "asr", "完成")
        artifact_file = Path(self.tmp.name) / "test_artifact2.txt"
        artifact_file.write_text("hello2", encoding="utf-8")
        self.store.add_artifact(job_id, "asr", "log", "测试产物2", artifact_file)

        self.store.reset_job(job_id)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["current_stage"], "asr")
        self.assertEqual(job["progress"], 0)
        self.assertIsNone(job["error"])
        for stage in job["stages"]:
            self.assertEqual(stage["status"], "pending")
            self.assertEqual(stage["progress"], 0)
        self.assertEqual(len(job["artifacts"]), 0)

    def test_recoverable_jobs_are_requeued_on_restart(self):
        job_id = self.store.create_job(title="中断", source_path="/tmp/source.mp4",
                                       workspace="/tmp/job")
        self.store.update_job(job_id, status="running")
        self.assertEqual([job["id"] for job in self.store.list_recoverable_jobs()], [job_id])

    def test_settings_round_trip(self):
        self.store.set_setting("ai_model", "opencode:gpt-5")
        self.assertEqual(self.store.get_setting("ai_model"), "opencode:gpt-5")

    def test_dashboard_counts_jobs(self):
        self.store.create_job(title="a", source_path="/tmp/a.mp4", workspace="/tmp/a")
        self.store.create_job(title="b", source_path="/tmp/b.mp4", workspace="/tmp/b")
        data = self.store.dashboard()
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["active"], 2)


if __name__ == "__main__":
    unittest.main()
