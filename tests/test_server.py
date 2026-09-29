import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_video.server import Application


class FilePickerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "中文素材.mp4"
        self.video.write_bytes(b"video")
        self.app = Application(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_windows_picker_returns_unicode_video_path(self):
        encoded = base64.b64encode(str(self.video).encode("utf-8")).decode("ascii")
        completed = subprocess.CompletedProcess([], 0, stdout=encoded + "\n", stderr="")
        with patch("agent_video.server.sys.platform", "win32"), \
             patch("agent_video.server.shutil.which", return_value="powershell.exe"), \
             patch("agent_video.server.subprocess.run", return_value=completed) as run:
            result = self.app.pick_video_file()

        self.assertFalse(result["cancelled"])
        self.assertEqual(result["path"], str(self.video.resolve()))
        self.assertEqual(result["name"], "中文素材")
        self.assertIn("-STA", run.call_args.args[0])
        self.assertIn("$owner.TopMost = $true", run.call_args.args[0][-1])
        self.assertIn("ShowDialog($owner)", run.call_args.args[0][-1])

    def test_windows_defaults_use_windows_virtualenv_layout(self):
        with patch("agent_video.server.sys.platform", "win32"), \
             patch("agent_video.server.shutil.which", return_value=None):
            app = Application(self.root / "windows-defaults")

        settings = app.settings()
        self.assertEqual(Path(settings["engine_python"]).name, "python.exe")
        self.assertEqual(Path(settings["engine_python"]).parent.name, "Scripts")
        self.assertTrue(settings["engine_bundled"])
        self.assertTrue(settings["engine_path"].endswith("engine"))

    def test_windows_picker_can_be_cancelled(self):
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch("agent_video.server.sys.platform", "win32"), \
             patch("agent_video.server.shutil.which", return_value="powershell.exe"), \
             patch("agent_video.server.subprocess.run", return_value=completed):
            self.assertEqual(self.app.pick_video_file(), {"cancelled": True})

    def test_windows_folder_picker_returns_directory(self):
        encoded = base64.b64encode(str(self.root).encode("utf-8")).decode("ascii")
        completed = subprocess.CompletedProcess([], 0, stdout=encoded + "\n", stderr="")
        with patch("agent_video.server.sys.platform", "win32"), \
             patch("agent_video.server.shutil.which", return_value="powershell.exe"), \
             patch("agent_video.server.subprocess.run", return_value=completed) as run:
            result = self.app.pick_file("dir")

        self.assertFalse(result["cancelled"])
        self.assertEqual(Path(result["path"]), self.root.resolve())
        self.assertIn("FolderBrowserDialog", run.call_args.args[0][-1])

    def test_macos_picker_still_returns_video_path(self):
        completed = subprocess.CompletedProcess([], 0, stdout=str(self.video) + "\n", stderr="")
        with patch("agent_video.server.sys.platform", "darwin"), \
             patch("agent_video.server.subprocess.run", return_value=completed):
            result = self.app.pick_video_file()

        self.assertEqual(result["path"], str(self.video.resolve()))
        self.assertEqual(result["name"], "中文素材")

    def test_other_platforms_get_a_clear_error(self):
        with patch("agent_video.server.sys.platform", "linux"):
            with self.assertRaisesRegex(ValueError, "Windows 和 macOS"):
                self.app.pick_video_file()


class JobApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "demo.mp4"
        self.video.write_bytes(b"data")
        self.app = Application(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_job_defaults_title_and_enqueues(self):
        job = self.app.create_job({"source_path": str(self.video)})
        self.assertEqual(job["title"], "demo")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["target_seconds"], "70-90")
        self.assertEqual([stage["stage_id"] for stage in job["stages"]],
                         ["asr", "filter", "judge", "order", "render"])

    def test_create_job_stores_custom_target_seconds(self):
        job = self.app.create_job({"source_path": str(self.video), "target_min": 60, "target_max": 80})
        self.assertEqual(job["target_seconds"], "60-80")

    def test_create_job_validates_target_seconds(self):
        with self.assertRaisesRegex(ValueError, "最长时长不能小于最短时长"):
            self.app.create_job({"source_path": str(self.video), "target_min": 100, "target_max": 50})

    def test_create_job_requires_existing_source(self):
        with self.assertRaisesRegex(ValueError, "素材文件不存在"):
            self.app.create_job({"source_path": str(self.root / "missing.mp4")})

    def test_legacy_fields_are_ignored(self):
        job = self.app.create_job({"source_path": str(self.video), "products": ["衣服"],
                                   "brief": "x", "delivery_mode": "segments"})
        self.assertEqual(job["title"], "demo")
        self.assertEqual(job["target_seconds"], "70-90")

    def test_deliverable_folder_opened_when_present(self):
        job = self.app.create_job({"title": "成片文件夹", "source_path": str(self.video)})
        job_id = job["id"]
        stored = self.app.store.get_job(job_id)
        folder = Path(stored["workspace"]) / "deliverables"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "final.mp4").write_bytes(b"mp4")

        info = self.app.deliverable_info(stored)
        self.assertTrue(info["exists"])
        self.assertEqual(Path(info["folder"]), folder)
        target_mock = (patch("agent_video.server.os.startfile", create=True)
                       if sys.platform == "win32" else patch("agent_video.server.subprocess.run"))
        with target_mock as opener:
            result = self.app.open_deliverable_folder(job_id)

        self.assertTrue(result["opened"])
        self.assertEqual(Path(result["folder"]), folder)
        if sys.platform == "win32":
            opener.assert_called_once_with(str(folder))
        else:
            opener.assert_called_once()

    def test_deliverable_info_matches_custom_and_legacy_names(self):
        job = self.app.create_job({"title": "伯恩夫人0926", "source_path": str(self.video)})
        stored = self.app.store.get_job(job["id"])
        folder = Path(stored["workspace"]) / "deliverables"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "伯恩夫人0926.mp4").write_bytes(b"new")
        (folder / "final.mp4").write_bytes(b"old")

        info = self.app.deliverable_info(stored)
        self.assertTrue(info["exists"])
        names = {Path(item["path"]).name for item in info["deliverables"]}
        self.assertIn("伯恩夫人0926.mp4", names)
        self.assertIn("final.mp4", names)

    def test_create_job_persists_export_dir_and_remembers_default(self):
        export_dir = self.root / "exports"
        job = self.app.create_job({"source_path": str(self.video), "title": "导出位置",
                                   "export_dir": str(export_dir)})
        stored = self.app.store.get_job(job["id"])
        self.assertEqual(stored["export_dir"], str(export_dir.resolve()))
        self.assertTrue(export_dir.is_dir())
        self.assertEqual(self.app.store.get_setting("export_dir"),
                         str(export_dir.resolve()))
        self.assertEqual(self.app.settings()["export_dir"], str(export_dir.resolve()))

    def test_create_job_uses_remembered_export_dir_when_unspecified(self):
        export_dir = self.root / "remembered"
        self.app.create_job({"source_path": str(self.video), "export_dir": str(export_dir)})
        job = self.app.create_job({"source_path": str(self.video)})
        stored = self.app.store.get_job(job["id"])
        self.assertEqual(stored["export_dir"], str(export_dir.resolve()))

    def test_create_job_rejects_export_path_that_is_a_file(self):
        with self.assertRaisesRegex(ValueError, "不是文件夹"):
            self.app.create_job({"source_path": str(self.video),
                                 "export_dir": str(self.video)})

    def test_deliverable_info_reports_export_folder(self):
        export_dir = self.root / "exports"
        job = self.app.create_job({"source_path": str(self.video), "title": "导出",
                                   "export_dir": str(export_dir)})
        stored = self.app.store.get_job(job["id"])
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "导出.mp4").write_bytes(b"mp4")

        info = self.app.deliverable_info(stored)
        self.assertEqual(Path(info["export_folder"]), export_dir.resolve())
        self.assertIn("导出.mp4",
                      {Path(item["path"]).name for item in info["exported"]})

    def test_create_job_stores_product_name_and_export_mode(self):
        job = self.app.create_job({"source_path": str(self.video), "title": "商品",
                                   "product_name": "毛衣", "export_mode": "segments"})
        stored = self.app.store.get_job(job["id"])
        self.assertEqual(stored["product_name"], "毛衣")
        self.assertEqual(stored["export_mode"], "segments")

    def test_create_job_defaults_export_mode_merge(self):
        job = self.app.create_job({"source_path": str(self.video)})
        stored = self.app.store.get_job(job["id"])
        self.assertEqual(stored["export_mode"], "merge")
        self.assertIsNone(stored["product_name"])

    def test_create_job_rejects_unknown_export_mode(self):
        with self.assertRaisesRegex(ValueError, "输出形态"):
            self.app.create_job({"source_path": str(self.video), "export_mode": "both"})

    def test_open_deliverable_folder_requires_existing_folder(self):
        job = self.app.create_job({"title": "未出片", "source_path": str(self.video)})
        target_mock = (patch("agent_video.server.os.startfile", create=True)
                       if sys.platform == "win32" else patch("agent_video.server.subprocess.run"))
        with target_mock as opener:
            with self.assertRaisesRegex(ValueError, "尚未生成"):
                self.app.open_deliverable_folder(job["id"])
        opener.assert_not_called()

    def test_job_clauses_merges_s2_and_s3_verdicts(self):
        job_id = self.app.create_job({"title": "核验", "source_path": str(self.video)})["id"]
        workspace = Path(self.app.store.get_job(job_id)["workspace"])
        timeline = [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "第一句", "usable": False,
             "reason": "场控引导语", "order": None},
            {"id": 1, "start": 1.0, "end": 2.0, "text": "第二句", "usable": True,
             "reason": "mock", "order": 0},
            {"id": 2, "start": 2.0, "end": 3.0, "text": "上链接", "usable": False,
             "reason": "hard_vocab", "order": None},
        ]
        filtered = [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "第一句", "usable": True, "reason": ""},
            {"id": 1, "start": 1.0, "end": 2.0, "text": "第二句", "usable": True, "reason": ""},
            {"id": 2, "start": 2.0, "end": 3.0, "text": "上链接", "usable": False,
             "reason": "hard_vocab"},
        ]
        (workspace / "timeline.json").write_text(
            json.dumps({"clauses": timeline}, ensure_ascii=False), encoding="utf-8")
        (workspace / "clauses.filtered.json").write_text(
            json.dumps({"clauses": filtered}, ensure_ascii=False), encoding="utf-8")

        data = self.app.job_clauses(job_id)
        self.assertTrue(data["ready"])
        self.assertEqual(data["counts"], {"total": 3, "s2_passed": 2, "s2_rejected": 1,
                                          "usable": 1, "rejected_by_s3": 1})
        by_id = {c["id"]: c for c in data["clauses"]}
        self.assertTrue(by_id[0]["s2_usable"])
        self.assertFalse(by_id[0]["usable"])
        self.assertEqual(by_id[0]["reason"], "场控引导语")
        self.assertEqual(by_id[2]["s2_reason"], "hard_vocab")
        self.assertEqual(by_id[1]["order"], 0)

    def test_job_clauses_handles_missing_job_or_artifacts(self):
        with self.assertRaises(KeyError):
            self.app.job_clauses("does-not-exist")
        job_id = self.app.create_job({"title": "空", "source_path": str(self.video)})["id"]
        self.assertFalse(self.app.job_clauses(job_id)["ready"])

    def test_server_job_restart_and_delete(self):
        job_id = self.app.create_job({"title": "测试API", "source_path": str(self.video)})["id"]

        res_restart = self.app.invoke_tool("restart_video_job", {"job_id": job_id})
        self.assertTrue(res_restart["restarted"])
        self.assertEqual(self.app.store.get_job(job_id)["status"], "queued")

        res_delete = self.app.invoke_tool("delete_video_job", {"job_id": job_id})
        self.assertTrue(res_delete["deleted"])
        self.assertIsNone(self.app.store.get_job(job_id))

    def test_retry_tool_requeues_job(self):
        job_id = self.app.create_job({"title": "重试", "source_path": str(self.video)})["id"]
        result = self.app.invoke_tool("retry_video_job", {"job_id": job_id})
        self.assertTrue(result["queued"])
        self.assertEqual(self.app.store.get_job(job_id)["status"], "queued")

    def test_retry_preserves_s1_cache_while_restart_cleans_all(self):
        job = self.app.create_job({"title": "缓存测试", "source_path": str(self.video)})
        job_id = job["id"]
        workspace = Path(job["workspace"])
        clauses_file = workspace / "clauses.json"
        clauses_file.write_text("{}", encoding="utf-8")
        extra_file = workspace / "final.mp4"
        extra_file.write_text("dummy", encoding="utf-8")
        historical_video = workspace / "deliverables" / "final.mp4"
        historical_video.parent.mkdir(parents=True)
        historical_video.write_bytes(b"history")

        # retry: 保留 clauses.json，清理 final.mp4
        self.app.invoke_tool("retry_video_job", {"job_id": job_id})
        self.assertTrue(clauses_file.exists())
        self.assertFalse(extra_file.exists())
        self.assertEqual(historical_video.read_bytes(), b"history")

        # restart: 完全清空目录
        self.app.invoke_tool("restart_video_job", {"job_id": job_id})
        self.assertFalse(clauses_file.exists())
        self.assertFalse(extra_file.exists())

    def test_rerun_s2_preserves_s1_and_historical_video_but_clears_downstream(self):
        job = self.app.create_job({"title": "单节点", "source_path": str(self.video)})
        job_id = job["id"]
        workspace = Path(job["workspace"])
        files = {
            "clauses.json": "s1",
            "clauses.filtered.json": "s2",
            "clauses.judged.json": "s3",
            "order.json": "s4",
        }
        for name, value in files.items():
            (workspace / name).write_text(value, encoding="utf-8")
        video = workspace / "deliverables" / "final.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(b"old-video")
        for stage_id in ("asr", "filter", "judge", "order", "render"):
            self.app.store.update_stage(job_id, stage_id, status="succeeded")
        self.app.store.update_job(job_id, status="completed")

        self.app.runner.rerun_stage(job_id, "filter")

        updated = self.app.store.get_job(job_id)
        self.assertEqual(updated["run_stage"], "filter")
        self.assertEqual(updated["status"], "queued")
        self.assertEqual(updated["stages"][0]["status"], "succeeded")
        self.assertTrue(all(stage["status"] == "pending" for stage in updated["stages"][1:]))
        self.assertEqual((workspace / "clauses.json").read_text(encoding="utf-8"), "s1")
        self.assertFalse((workspace / "clauses.filtered.json").exists())
        self.assertFalse((workspace / "clauses.judged.json").exists())
        self.assertFalse((workspace / "order.json").exists())
        self.assertEqual(video.read_bytes(), b"old-video")

    def test_update_settings_validates_ai_engine(self):
        self.assertEqual(self.app.update_settings({"ai_engine": "jev"})["ai_engine"], "jev")
        with self.assertRaisesRegex(ValueError, "ai_engine"):
            self.app.update_settings({"ai_engine": "gpt"})

    def test_inspect_and_create_timeline_job(self):
        draft_file = self.root / "draft_content.json"
        draft_content = {
            "materials": {"videos": [{"id": "v1", "path": str(self.video)}]},
            "tracks": [{
                "type": "video",
                "segments": [{
                    "id": "s1",
                    "material_id": "v1",
                    "source_timerange": {"start": 0, "duration": 60000000},
                    "target_timerange": {"start": 0, "duration": 60000000},
                }]
            }]
        }
        draft_file.write_text(json.dumps(draft_content, ensure_ascii=False), encoding="utf-8")

        # Test inspect
        inspect_res = self.app.inspect_timeline(str(draft_file))
        self.assertEqual(inspect_res["segment_count"], 1)
        self.assertEqual(inspect_res["timeline_duration"], 60.0)
        self.assertEqual(inspect_res["source_duration"], 60.0)

        # Test create timeline job
        saved_export = self.root / "saved-default-exports"
        self.app.store.set_setting("export_dir", str(saved_export))
        job = self.app.create_job({
            "job_type": "timeline",
            "timeline_path": str(draft_file),
            "title": "F家限定 / 时间线01 · 约 1 分钟",
            "target_min": 90,
            "target_max": 120,
            "export_dir": "",
            "product_name": "限定款",
            "export_mode": "segments",
        })
        self.assertEqual(job["job_type"], "timeline")
        self.assertEqual(job["title"], "F家限定 / 时间线01 · 约 1 分钟")
        self.assertIsNotNone(job.get("timeline_meta"))
        self.assertEqual(job["timeline_meta"]["segment_count"], 1)
        self.assertEqual(job["target_seconds"], "90-120")
        self.assertEqual(job["export_dir"], str(saved_export.resolve()))
        self.assertEqual(job["product_name"], "限定款")
        self.assertEqual(job["export_mode"], "segments")

        # Verify virtual_timeline.json in workspace
        ws = Path(job["workspace"])
        vt_path = ws / "virtual_timeline.json"
        self.assertTrue(vt_path.is_file())
        vt_data = json.loads(vt_path.read_text(encoding="utf-8"))
        self.assertEqual(vt_data["segments"][0]["segment_id"], "片段1")

    def test_create_direct_job_backward_compatibility(self):
        job = self.app.create_job({"source_path": str(self.video)})
        self.assertEqual(job["job_type"], "direct")
        self.assertEqual(job["source_path"], str(self.video))
        self.assertIsNone(job.get("timeline_meta"))


if __name__ == "__main__":
    unittest.main()

