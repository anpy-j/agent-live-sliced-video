import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        rendered = workspace / "final-render" / "renders"
        rendered.mkdir(parents=True)
        body = rendered / "body.mp4"
        body.write_bytes(b"complete-body-video")
        job_id = self.store.create_job(title="秋装/显瘦.mp4", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        job = self.store.get_job(job_id)
        output = self.runner._save_final(job, workspace, {"deliverables": {"body": str(body), "hooks": {}}})
        self.assertEqual(output.name, "秋装-显瘦.mp4")
        self.assertEqual(output.read_bytes(), b"complete-body-video")
        self.assertEqual(self.store.get_job(job_id)["artifacts"][0]["stage_id"], "delivery")

    def test_validated_inputs_detect_timeline_changes(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        engine = self.root / "engine"
        (engine / "dual_timelines").mkdir(parents=True)
        (engine / "timelines").mkdir()
        dual = engine / "dual_timelines" / "body.json"
        module = engine / "timelines" / "body.json"
        dual.write_text("[]", encoding="utf-8")
        module.write_text("[]", encoding="utf-8")
        (engine / "visual_report.json").write_text(
            '{"dual_timelines":{"body":"' + str(dual) + '"}}', encoding="utf-8")
        snapshot = self.runner._delivery_inputs(source, engine)
        self.runner._assert_delivery_inputs(snapshot, source)
        dual.write_text("[{}]", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            self.runner._assert_delivery_inputs(snapshot, source)

    def test_only_waiting_stages_accept_payload(self):
        workspace = self.root / "job"
        workspace.mkdir()
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        with self.assertRaisesRegex(ValueError, "不接受外部决策"):
            self.runner.submit(job_id, {"verdict": "approve"})

    def test_edit_plan_requires_hook_body_and_unique_segments(self):
        valid = {
            "main_product": "白山茶",
            "picks": [
                {"src": 1, "start": 0, "end": 3, "text": "开头", "role": "hook", "module": "hook_A"},
                {"src": 1, "start": 4, "end": 8, "text": "正文", "role": "proof", "module": "body"},
            ],
        }
        self.runner._validate_edit_plan(valid)
        no_body = json.loads(json.dumps(valid))
        no_body["picks"][1]["module"] = "hook_A"
        with self.assertRaisesRegex(ValueError, "正文"):
            self.runner._validate_edit_plan(no_body)
        duplicate = json.loads(json.dumps(valid))
        duplicate["picks"][1].update({"start": 0, "end": 3})
        with self.assertRaisesRegex(ValueError, "重复"):
            self.runner._validate_edit_plan(duplicate)

    def test_delivery_reuses_snapshot_without_full_pipeline(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        workspace = self.root / "job"
        engine = workspace / "engine"
        (engine / "dual_timelines").mkdir(parents=True)
        (engine / "timelines").mkdir()
        dual = engine / "dual_timelines" / "body.json"
        module = engine / "timelines" / "body.json"
        dual.write_text("[]", encoding="utf-8")
        module.write_text("[]", encoding="utf-8")
        (engine / "visual_report.json").write_text(
            '{"dual_timelines":{"body":"' + str(dual) + '"}}', encoding="utf-8")
        snapshot = self.runner._delivery_inputs(source, engine)
        (workspace / "proxy_complete.json").write_text(
            json.dumps({"validated_inputs": snapshot}), encoding="utf-8")
        job_id = self.store.create_job(title="完整成片", source_path=str(source), brief="",
                                       mode="standard", workspace=str(workspace))
        self.store.update_stage(job_id, "rough_cut", status="succeeded")
        job = self.store.get_job(job_id)

        def fake_step(_job_id, label, command, steps, _progress):
            if label.startswith("高清渲染"):
                output = Path(command[3])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"full-video")
            else:
                qc_dir = Path(command[3])
                qc_dir.mkdir(parents=True, exist_ok=True)
                (qc_dir / "qc_report.json").write_text('{"ok":true}', encoding="utf-8")
            steps.append({"name": label, "ok": True})

        with patch.object(self.runner, "_run_delivery_step", side_effect=fake_step), \
                patch.object(self.runner, "_execute", side_effect=AssertionError("full pipeline repeated")):
            self.runner._deliver(job, source, workspace, engine)
        completed = self.store.get_job(job_id)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue((workspace / "deliverables" / "完整成片.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
