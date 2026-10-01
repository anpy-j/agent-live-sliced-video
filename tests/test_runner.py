from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video import runner as runner_module
from agent_video.db import Store
from agent_video.runner import JobRunner


class JobRunnerCancellationTest(unittest.TestCase):
    def test_cancel_running_job_terminates_active_process_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "workspace"
            job_id = store.create_job(title="running", source_path=str(root / "source.mp4"),
                                      workspace=str(workspace))
            store.update_job(job_id, status="running")

            runner = JobRunner(store, root)
            runner._current = job_id
            runner._process = Mock(pid=123, is_alive=Mock(return_value=True))

            with patch.object(runner, "_terminate_process_tree") as terminate:
                self.assertTrue(runner.cancel(job_id))

            terminate.assert_called_once_with(runner._process)
            self.assertEqual(store.get_job(job_id)["status"], "cancelled")


@unittest.skipUnless(os.name == "nt", "子进程 job 处理是 Windows 专用路径")
class PipelineChildTest(unittest.TestCase):
    def test_child_forwards_output_stem_to_run_pipeline(self):
        class FakeQueue:
            def __init__(self):
                self.items = []

            def put(self, item):
                self.items.append(item)

        queue = FakeQueue()
        with patch.object(runner_module, "run_pipeline",
                          return_value={"output": "deliverables/x.mp4"}) as call:
            runner_module._run_pipeline_child(queue, "src.mp4", "ws", (8.0, 9.0),
                                              None, "伯恩夫人0926")

        self.assertEqual(call.call_args.kwargs["output_stem"], "伯恩夫人0926")
        self.assertEqual(queue.items[0][0], "success")

    def test_child_forwards_virtual_timeline_to_stage_rerun(self):
        class FakeQueue:
            def __init__(self):
                self.items = []

            def put(self, item):
                self.items.append(item)

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            (workspace / "virtual_timeline.json").write_text(
                '{"timeline_id":"vt-1","title":"虚拟时间线","segments":['
                '{"segment_id":"s1","timeline_start":0,"timeline_end":2,'
                '"source_path":"source.ts","source_start":10,"source_end":12}]}'
                , encoding="utf-8")
            queue = FakeQueue()
            with patch.object(runner_module, "run_pipeline_stage",
                              return_value={"output": "deliverables/x.mp4"}) as call:
                runner_module._run_pipeline_child(
                    queue, "display-name", str(workspace), (8.0, 9.0), "render", "成片")

        timeline = call.call_args.kwargs["virtual_timeline"]
        self.assertIsNotNone(timeline)
        self.assertEqual(timeline.timeline_id, "vt-1")
        self.assertEqual(queue.items[0][0], "success")


class SucceedArtifactsTest(unittest.TestCase):
    def test_succeed_registers_custom_and_legacy_video_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "workspace"
            deliverables = workspace / "deliverables"
            deliverables.mkdir(parents=True)
            (deliverables / "final.mp4").write_bytes(b"old")
            (deliverables / "伯恩夫人0926.mp4").write_bytes(b"new")
            job_id = store.create_job(title="伯恩夫人0926",
                                      source_path=str(root / "source.mp4"),
                                      workspace=str(workspace))
            store.update_job(job_id, status="running")

            JobRunner(store, root)._succeed(
                job_id, workspace, {"output": "deliverables/伯恩夫人0926.mp4"})

            names = {Path(item["path"]).name
                     for item in store.get_job(job_id)["artifacts"]
                     if item["kind"] == "video"}
            self.assertIn("伯恩夫人0926.mp4", names)
            self.assertIn("final.mp4", names)

    def test_export_video_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "rendered" / "伯恩夫人0926.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"new")
            export_dir = root / "exports"
            export_dir.mkdir()
            (export_dir / "伯恩夫人0926.mp4").write_bytes(b"existing")

            target = JobRunner._export_video(source, str(export_dir))

            self.assertEqual(target.name, "伯恩夫人0926-1.mp4")
            self.assertEqual((export_dir / "伯恩夫人0926.mp4").read_bytes(), b"existing")
            self.assertEqual(target.read_bytes(), b"new")

    def test_succeed_exports_to_chosen_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "workspace"
            deliverables = workspace / "deliverables"
            deliverables.mkdir(parents=True)
            (deliverables / "伯恩夫人0926.mp4").write_bytes(b"video")
            export_dir = root / "exports"
            job_id = store.create_job(title="伯恩夫人0926",
                                      source_path=str(root / "source.mp4"),
                                      workspace=str(workspace),
                                      export_dir=str(export_dir))
            store.update_job(job_id, status="running")

            JobRunner(store, root)._succeed(
                job_id, workspace, {"output": "deliverables/伯恩夫人0926.mp4"},
                export_dir=str(export_dir))

            self.assertTrue((export_dir / "伯恩夫人0926.mp4").is_file())
            names = {Path(item["path"]).name
                     for item in store.get_job(job_id)["artifacts"]}
            self.assertIn("伯恩夫人0926.mp4", names)

    def test_export_outputs_segments_into_named_subfolder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir(parents=True)
            export_dir = root / "exports"
            manifest = {
                "output": "deliverables/成片.mp4",
                "source": str(root / "media.mp4"),
                "segments": [{"id": 0, "start": 0.0, "end": 3.0},
                             {"id": 1, "start": 10.0, "end": 13.0}],
            }

            def fake_render(media, segment, output, workdir, **kwargs):
                Path(output).write_bytes(b"clip")
                return output

            with patch.object(runner_module, "render_segment", side_effect=fake_render):
                exported = runner_module.JobRunner._export_outputs(
                    None, workspace, manifest, str(export_dir), "segments",
                    "伯恩夫人0926", str(root / "media.mp4"))

            self.assertEqual([path.name for path in exported], ["01.mp4", "02.mp4"])
            self.assertEqual(exported[0].parent.name, "伯恩夫人0926")
            self.assertTrue((export_dir / "伯恩夫人0926" / "01.mp4").is_file())

    def test_export_outputs_segments_subfolder_increments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            workspace.mkdir(parents=True)
            export_dir = root / "exports"
            (export_dir / "伯恩夫人0926").mkdir(parents=True)
            manifest = {"output": "deliverables/成片.mp4",
                        "source": str(root / "media.mp4"),
                        "segments": [{"id": 0, "start": 0.0, "end": 3.0}]}

            def fake_render(media, segment, output, workdir, **kwargs):
                Path(output).write_bytes(b"clip")
                return output

            with patch.object(runner_module, "render_segment", side_effect=fake_render):
                exported = runner_module.JobRunner._export_outputs(
                    None, workspace, manifest, str(export_dir), "segments",
                    "伯恩夫人0926", str(root / "media.mp4"))

            self.assertEqual(exported[0].parent.name, "伯恩夫人0926-1")


class EditCountTest(unittest.TestCase):
    def test_succeed_increments_edit_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "workspace"
            (workspace / "deliverables").mkdir(parents=True)
            (workspace / "deliverables" / "成片.mp4").write_bytes(b"v")
            job_id = store.create_job(title="成片", source_path=str(root / "s.mp4"),
                                      workspace=str(workspace))
            store.update_job(job_id, status="running")

            JobRunner(store, root)._succeed(job_id, workspace,
                                            {"output": "deliverables/成片.mp4"})

            self.assertEqual(store.get_job(job_id)["edit_count"], 1)

    def test_mark_delivered_requires_existing_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = JobRunner(Store(root / "agent.db"), root)
            with self.assertRaises(KeyError):
                runner.mark_delivered("missing")


class AiEnvironmentDefaultsTest(unittest.TestCase):
    """生产环境默认注入 S3 并发/重试，且 DB 设置与环境变量可覆盖/回退。"""

    def test_production_defaults_injected_when_unset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = JobRunner(Store(root / "agent.db"), root)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PIPELINE_JUDGE_CONCURRENCY", None)
                os.environ.pop("PIPELINE_JUDGE_RETRIES", None)
                with runner._ai_environment():
                    self.assertEqual(os.environ["PIPELINE_JUDGE_CONCURRENCY"], "4")
                    self.assertEqual(os.environ["PIPELINE_JUDGE_RETRIES"], "1")
                self.assertIsNone(os.environ.get("PIPELINE_JUDGE_CONCURRENCY"))
                self.assertIsNone(os.environ.get("PIPELINE_JUDGE_RETRIES"))

    def test_db_setting_overrides_production_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            store.set_setting("judge_concurrency", "1")
            runner = JobRunner(store, root)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PIPELINE_JUDGE_CONCURRENCY", None)
                with runner._ai_environment():
                    self.assertEqual(os.environ["PIPELINE_JUDGE_CONCURRENCY"], "1")

    def test_existing_env_wins_over_production_default(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = JobRunner(Store(root / "agent.db"), root)
            with patch.dict(os.environ, {"PIPELINE_JUDGE_CONCURRENCY": "2"},
                            clear=False):
                with runner._ai_environment():
                    self.assertEqual(os.environ["PIPELINE_JUDGE_CONCURRENCY"], "2")
                self.assertEqual(os.environ["PIPELINE_JUDGE_CONCURRENCY"], "2")


class StageCascadeTest(unittest.TestCase):
    """单节点重跑完成后自动继续下游节点，渲染完成后按出片收尾。"""

    def _job(self, store, workspace):
        job_id = store.create_job(title="级联", source_path=str(workspace / "s.mp4"),
                                  workspace=str(workspace))
        store.update_job(job_id, status="running")
        return job_id

    def test_succeed_stage_cascades_to_next_node(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "ws"
            workspace.mkdir()
            (workspace / "clauses.judged.json").write_text("{}", encoding="utf-8")
            job_id = self._job(store, workspace)
            runner = JobRunner(store, root)
            runner._current = job_id
            with patch.object(runner, "_enqueue") as enqueue:
                runner._succeed_stage(job_id, workspace, "judge",
                                      {"stage": "judge", "usable": 1},
                                      store.get_job(job_id))
            enqueue.assert_called_once()
            updated = store.get_job(job_id)
            self.assertEqual(updated["run_stage"], "order")
            self.assertEqual(updated["status"], "queued")

    def test_render_stage_finalizes_job_without_waiting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "ws"
            (workspace / "deliverables").mkdir(parents=True)
            (workspace / "deliverables" / "级联.mp4").write_bytes(b"v")
            job_id = self._job(store, workspace)
            runner = JobRunner(store, root)
            job = store.get_job(job_id)
            runner._succeed_stage(job_id, workspace, "render",
                                  {"output": "deliverables/级联.mp4",
                                   "segments": [], "source": str(workspace / "s.mp4")},
                                  job)
            updated = store.get_job(job_id)
            self.assertEqual(updated["status"], "completed")
            names = {Path(item["path"]).name
                     for item in updated["artifacts"] if item["kind"] == "video"}
            self.assertIn("级联.mp4", names)

    def test_rerun_render_requires_order_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = Store(root / "agent.db")
            workspace = root / "ws"
            workspace.mkdir()
            job_id = store.create_job(title="渲染", source_path=str(workspace / "s.mp4"),
                                      workspace=str(workspace))
            store.update_job(job_id, status="completed")
            runner = JobRunner(store, root)
            with self.assertRaises(ValueError):
                runner.rerun_stage(job_id, "render")
            (workspace / "order.json").write_text("{}", encoding="utf-8")
            runner.rerun_stage(job_id, "render")
            updated = store.get_job(job_id)
            self.assertEqual(updated["run_stage"], "render")
            self.assertEqual(updated["status"], "queued")


if __name__ == "__main__":
    unittest.main()
