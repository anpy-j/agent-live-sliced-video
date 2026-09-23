import base64
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
        self.assertEqual([stage["stage_id"] for stage in job["stages"]],
                         ["asr", "filter", "judge", "order", "render"])

    def test_create_job_requires_existing_source(self):
        with self.assertRaisesRegex(ValueError, "素材文件不存在"):
            self.app.create_job({"source_path": str(self.root / "missing.mp4")})

    def test_legacy_fields_are_ignored(self):
        job = self.app.create_job({"source_path": str(self.video), "products": ["衣服"],
                                   "brief": "x", "delivery_mode": "segments"})
        self.assertEqual(job["title"], "demo")

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

    def test_open_deliverable_folder_requires_existing_folder(self):
        job = self.app.create_job({"title": "未出片", "source_path": str(self.video)})
        target_mock = (patch("agent_video.server.os.startfile", create=True)
                       if sys.platform == "win32" else patch("agent_video.server.subprocess.run"))
        with target_mock as opener:
            with self.assertRaisesRegex(ValueError, "尚未生成"):
                self.app.open_deliverable_folder(job["id"])
        opener.assert_not_called()

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

    def test_update_settings_validates_ai_engine(self):
        self.assertEqual(self.app.update_settings({"ai_engine": "jev"})["ai_engine"], "jev")
        with self.assertRaisesRegex(ValueError, "ai_engine"):
            self.app.update_settings({"ai_engine": "gpt"})


if __name__ == "__main__":
    unittest.main()
