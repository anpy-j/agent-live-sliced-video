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
        self.assertEqual(settings["engine_path"], "")
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


if __name__ == "__main__":
    unittest.main()
