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
        self.assertEqual(len(job["stages"]), 9)
        self.assertEqual(job["events"][0]["kind"], "job_created")

    def test_stage_transition_is_persisted(self):
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace="/tmp/job")
        self.store.stage_start(job_id, "ingest", "开始")
        self.store.stage_done(job_id, "ingest", "完成", {"duration": 42})
        job = self.store.get_job(job_id)
        stage = job["stages"][0]
        self.assertEqual(stage["status"], "succeeded")
        self.assertEqual(stage["result"]["duration"], 42)


if __name__ == "__main__":
    unittest.main()

