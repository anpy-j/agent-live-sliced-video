import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from agent_video.smart_v3 import draft, SmartService
from agent_video.smart_v3.draft_timeline import load_virtual_timeline
from agent_video.smart_v3.pipeline import command
from agent_video.server import Application, Handler
from test_smart_v3 import FakeAI


def draft_json(paths):
    # Two sources, constant speed 2 then 1, ten seconds of selected content.
    return {"materials": {"videos": [{"id": f"v{i}", "path": str(p)} for i, p in enumerate(paths)],
                           "speeds": [{"id": "fast", "speed": 2}]},
            "tracks": [{"type": "video", "segments": [
                {"id": "a", "material_id": "v0", "source_timerange": {"start": 2_000_000, "duration": 8_000_000},
                 "target_timerange": {"start": 0, "duration": 4_000_000}, "extra_material_refs": ["fast"]},
                {"id": "b", "material_id": "v1", "source_timerange": {"start": 3_000_000, "duration": 6_000_000},
                 "target_timerange": {"start": 4_000_000, "duration": 6_000_000}}]}]}


class DraftParserTest(unittest.TestCase):
    def test_relative_paths_and_cross_segment_sentence_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "draft_content.json"
            data = draft_json(["first.mp4", "second.mp4"])
            path.write_text(json.dumps(data), encoding="utf-8")
            timeline = load_virtual_timeline(path)
            self.assertEqual(timeline.source_paths[0], str(root / "first.mp4"))
            mapped = timeline.map_timeline_range(3, 6)
            self.assertEqual(len(mapped), 2)
            self.assertEqual((mapped[0]["source_start"], mapped[0]["source_end"]), (8, 10))
            self.assertEqual((mapped[1]["source_start"], mapped[1]["source_end"]), (3, 5))
            self.assertEqual(sum(p["timeline_end"] - p["timeline_start"] for p in mapped), 3)

    def test_encrypted_draft_uses_v3_decoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "draft_content.json"
            path.write_bytes(b"encrypted")
            with patch("agent_video.smart_v3.jianying_crypto.decrypt_jianying_file", return_value=draft_json(["a", "b"])) as decoder:
                timeline = load_virtual_timeline(path)
                self.assertEqual(len(timeline.segments), 2)
                decoder.assert_called_once_with(path.resolve())

    def test_curve_speed_is_rejected(self):
        data = draft_json(["a", "b"])
        data["materials"]["speeds"][0]["curve_speed"] = {"points": [1, 2]}
        with self.assertRaisesRegex(ValueError, "曲线"):
            load_virtual_timeline(data)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class DraftEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = [self.root / "first.mp4", self.root / "second.mp4"]
        for index, path in enumerate(self.paths):
            command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={'red' if index == 0 else 'blue'}:s=160x120:r=30",
                     "-f", "lavfi", "-i", f"sine=frequency={440+index*220}", "-t", "12", "-c:v", "libx264", "-c:a", "aac", str(path)])
        self.file = self.root / "draft_content.json"
        self.file.write_text(json.dumps(draft_json(self.paths)), encoding="utf-8")
        self.transcript = self.root / "sentences.json"
        self.transcript.write_text(json.dumps([{"start": i*2., "end": (i+1)*2., "text": f"完整句{i}。"} for i in range(5)]), encoding="utf-8")
        self.app = Application(self.root)
        self.app.viral.list_jobs()
        self.hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                       [self.root / "data/agent.db", self.root / "data/viral_v2/viral_v2.db", self.file, *self.paths]}
        self.service = SmartService(self.root, ai=FakeAI())

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self):
        return {"source_kind": "draft", "draft_path": str(self.file), "product_name": "毛衣",
                "target_min": 6, "target_max": 8, "transcript_path": str(self.transcript)}

    def assert_isolated(self):
        for path, before in self.hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before, str(path))

    def test_full_draft_pipeline_retry_failure_and_delete(self):
        job = self.service.create_job(self.payload())
        result = self.service.run(job["id"])
        self.assertEqual(result["status"], "completed", result["error"])
        units = result["stages"]["S1"]["result"]
        self.assertEqual(units[0]["source_slices"][0]["source_start"], 2)
        self.assertEqual(units[0]["source_slices"][0]["source_end"], 6)
        self.assertEqual(units[2]["source_slices"][0]["source_path"], str(self.paths[1]))
        self.assertTrue(self.service.artifact(job["id"], "source-timeline.json").is_file())
        self.assertTrue(self.service.artifact(job["id"], "film.mp4").stat().st_size > 1000)
        self.assert_isolated()
        self.service.ai = FakeAI(reject_always=True)
        self.assertEqual(self.service.run(job["id"], retry=True)["status"], "failed")
        self.assert_isolated()
        self.service.delete_job(job["id"])
        self.assert_isolated()

    def test_active_child_timeline_and_snapshot(self):
        child = self.root / "Timelines" / "child"
        child.mkdir(parents=True)
        (child / "draft_content.json").write_text(self.file.read_text(), encoding="utf-8")
        (self.root / "timeline_layout.json").write_text(json.dumps({"activeTimeline": "child", "dockItems": [{"timelineIds": ["child"], "timelineNames": ["精选"]}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "人工选择"):
            draft.snapshot(self.root)
        selected = draft.snapshot(child / "draft_content.json")
        self.assertEqual(selected["timeline"]["timeline_id"], "child")
        self.assertEqual(selected["selected_path"], str(child / "draft_content.json"))
        choices = self.service.list_draft_timelines({"draft_path": str(self.root)})
        self.assertTrue(choices["timelines"][0]["active"])
        payload = self.payload()
        payload["draft_path"] = str(child / "draft_content.json")
        job = self.service.create_job(payload)
        (child / "draft_content.json").write_text("{}", encoding="utf-8")
        # Job snapshot still describes the accepted timeline; it never rewrites the draft.
        self.assertEqual(job["input"]["draft_timeline"]["timeline"]["segment_count"], 2)

    def test_all_timelines_are_listed_and_selection_is_explicit(self):
        for name in ("active", "other", "broken"):
            folder = self.root / "Timelines" / name
            folder.mkdir(parents=True)
            (folder / "draft_content.json").write_text("{}" if name == "broken" else self.file.read_text(), encoding="utf-8")
        (self.root / "timeline_layout.json").write_text(json.dumps({"activeTimeline":"active", "dockItems":[{"timelineIds":["active","other","broken"],"timelineNames":["活动","人工选择","损坏"]}]}), encoding="utf-8")
        choices = self.service.list_draft_timelines({"draft_path": str(self.root)})["timelines"]
        self.assertEqual({t["name"] for t in choices}, {"活动","人工选择","损坏"})
        self.assertTrue(next(t for t in choices if t["name"] == "损坏")["error"])
        payload = self.payload()
        payload.pop("draft_path")
        payload["source_path"] = str(self.root)
        with self.assertRaisesRegex(ValueError, "人工选择"):
            self.service.create_job(payload)
        payload["draft_path"] = str(self.root / "Timelines/other/draft_content.json")
        job = self.service.create_job(payload)
        self.assertEqual(job["input"]["draft_timeline"]["timeline"]["timeline_id"], "other")

    def test_cross_segment_sentence_renders_correct_original_colors(self):
        snapshot = draft.snapshot(self.file)
        units = draft.understand(lambda *args: [{"id": "cross", "start": 3., "end": 6., "text": "跨片段完整句。"}], snapshot, self.root, None)
        self.assertEqual(len(units[0]["source_slices"]), 2)
        film = draft.render(self.root, units)
        def color(at):
            result = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(film), "-frames:v", "1",
                                     "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], capture_output=True, check=True)
            return list(result.stdout[:3])
        red, blue = color(.3), color(1.5)
        self.assertGreater(red[0], 200)
        self.assertLess(red[2], 50)
        self.assertGreater(blue[2], 200)
        self.assertLess(blue[0], 50)
        self.assert_isolated()

    def test_draft_http_list_create_and_run(self):
        self.app._smart = self.service
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        def post(path, payload):
            req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}" + path,
                    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                self.assertIn("application/json", response.headers["Content-Type"])
                return json.load(response)
        try:
            choices = post("/api/smart-v3/drafts/timelines", {"draft_path": str(self.file)})
            self.assertEqual(choices["timelines"][0]["segment_count"], 2)
            job = post("/api/smart-v3/jobs", self.payload())
            result = post(f"/api/smart-v3/jobs/{job['id']}/run", {})
            self.assertEqual(result["status"], "completed", result["error"])
            self.assert_isolated()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_gaps_missing_sources_and_changed_media_fail_clearly(self):
        data = draft_json(self.paths)
        data["tracks"][0]["segments"][1]["target_timerange"]["start"] = 5_000_000
        self.file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "空隙"):
            self.service.create_job(self.payload())
        self.file.write_text(json.dumps(draft_json(self.paths)), encoding="utf-8")
        job = self.service.create_job(self.payload())
        self.paths[0].touch()
        failed = self.service.run(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("素材已变化", failed["error"])
        data = draft_json(["missing.mp4", self.paths[1]])
        self.file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.service.create_job(self.payload())
