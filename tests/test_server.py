import base64
import subprocess
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
        self.assertNotIn("/Applications/", settings["workbuddy_cli_path"])

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


    def test_windows_picker_subtitle_returns_path(self):
        srt_file = self.root / "字幕.srt"
        srt_file.write_text("1\n00:00:01,000 --> 00:00:02,000\n测试\n", encoding="utf-8")
        encoded = base64.b64encode(str(srt_file).encode("utf-8")).decode("ascii")
        completed = subprocess.CompletedProcess([], 0, stdout=encoded + "\n", stderr="")
        with patch("agent_video.server.sys.platform", "win32"), \
             patch("agent_video.server.shutil.which", return_value="powershell.exe"), \
             patch("agent_video.server.subprocess.run", return_value=completed) as run:
            result = self.app.pick_file(kind="subtitle")

        self.assertFalse(result["cancelled"])
        self.assertEqual(result["path"], str(srt_file.resolve()))
        self.assertEqual(result["name"], "字幕")
        self.assertIn("选择时间戳字幕文件", run.call_args.args[0][-1])
        self.assertIn("*.srt;*.vtt;*.ass;*.ssa;*.txt", run.call_args.args[0][-1])

    def test_deliverable_folder_opened_when_present(self):
        video = self.root / "demo.mp4"
        video.write_bytes(b"data")
        with patch.object(self.app.runner, "resolve_ai_selection", return_value=("manual", None)), \
             patch.object(self.app.runner, "resolve_visual_ai_selection", return_value=("manual", None)):
            job = self.app.create_job({"title": "成片文件夹", "source_path": str(video),
                                       "products": ["衣服"]})
        job_id = job["id"]
        job = self.app.store.get_job(job_id)
        folder = Path(job["workspace"]) / "deliverables"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "成片.mp4").write_bytes(b"mp4")

        info = self.app.deliverable_info(job)
        self.assertTrue(info["exists"])
        self.assertEqual(Path(info["folder"]), folder)
        with patch("agent_video.server.os.startfile") as startfile:
            result = self.app.open_deliverable_folder(job_id)

        self.assertTrue(result["opened"])
        self.assertEqual(Path(result["folder"]), folder)
        startfile.assert_called_once_with(str(folder))

    def test_open_deliverable_folder_requires_existing_folder(self):
        video = self.root / "demo2.mp4"
        video.write_bytes(b"data")
        with patch.object(self.app.runner, "resolve_ai_selection", return_value=("manual", None)), \
             patch.object(self.app.runner, "resolve_visual_ai_selection", return_value=("manual", None)):
            job = self.app.create_job({"title": "未出片", "source_path": str(video),
                                       "products": ["衣服"]})
        with patch("agent_video.server.os.startfile") as startfile:
            with self.assertRaisesRegex(ValueError, "尚未生成"):
                self.app.open_deliverable_folder(job["id"])
        startfile.assert_not_called()

    def test_server_job_restart_and_delete(self):
        video = self.root / "demo.mp4"
        video.write_bytes(b"data")
        with patch.object(self.app.runner, "resolve_ai_selection", return_value=("manual", None)), \
             patch.object(self.app.runner, "resolve_visual_ai_selection", return_value=("manual", None)):
            job = self.app.create_job({"title": "测试API", "source_path": str(video), "products": ["衣服"]})
        job_id = job["id"]
        
        # Test restart
        res_restart = self.app.invoke_tool("restart_video_job", {"job_id": job_id})
        self.assertTrue(res_restart["restarted"])
        self.assertEqual(self.app.store.get_job(job_id)["status"], "queued")
        
        # Test delete
        res_delete = self.app.invoke_tool("delete_video_job", {"job_id": job_id})
        self.assertTrue(res_delete["deleted"])
        self.assertIsNone(self.app.store.get_job(job_id))


if __name__ == "__main__":
    unittest.main()
