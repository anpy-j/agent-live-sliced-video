import tempfile
import unittest
from pathlib import Path

from agent_video.db import Store
from agent_video.runner import JobRunner


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "agent.db")
        self.runner = JobRunner(self.store, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_final_video_uses_job_title(self):
        workspace = self.root / "job"
        preview_dir = workspace / "engine" / "deliverables" / "previews"
        preview_dir.mkdir(parents=True)
        preview = preview_dir / "preview_A+body.mp4"
        preview.write_bytes(b"video")
        job_id = self.store.create_job(title="秋装/显瘦.mp4", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        job = self.store.get_job(job_id)
        output = self.runner._save_final(job, workspace, {"deliverables": {"previews": str(preview_dir)}})
        self.assertEqual(output.name, "秋装-显瘦.mp4")
        self.assertEqual(output.read_bytes(), b"video")
        self.assertEqual(self.store.get_job(job_id)["artifacts"][0]["stage_id"], "delivery")

    def test_only_waiting_stages_accept_payload(self):
        workspace = self.root / "job"
        workspace.mkdir()
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        with self.assertRaisesRegex(ValueError, "不接受外部决策"):
            self.runner.submit(job_id, {"verdict": "approve"})


if __name__ == "__main__":
    unittest.main()
