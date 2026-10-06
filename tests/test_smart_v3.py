import hashlib
import json
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from agent_video.server import Application, Handler
from agent_video.smart_v3 import SmartService
from agent_video.smart_v3 import pipeline
from agent_video.smart_v3.ai import validate_profiles, validate_review


def fixture():
    return [{"id": f"u{i}", "start": i * 5., "end": (i + 1) * 5., "text": f"商品完整句{i}。",
             "independent_start": True, "previous_dependency": [], "next_dependency": [],
             "visual": {"scene_id": None, "shot_changes": None, "status": "not_analyzed"}}
            for i in range(8)]


class FakeAI:
    def __init__(self, role="result", reject_once=False, reject_always=False):
        self.role = role
        self.reject_once = reject_once
        self.reject_always = reject_always
        self.reviews = 0

    def __call__(self, kind, payload):
        if kind == "profile":
            return {"profiles": [{"id": u["id"], "usable": True, "role": [self.role],
                    "topic": self.role, "claim_cluster": u["id"], "strength": .8,
                    "hook_strength": .9, "independent": True, "dependency": [],
                    "product_relevance": .9, "visual_need": "unknown", "reason": "fixture"}
                    for u in payload["units"]]}
        self.reviews += 1
        rejected = self.reject_always or self.reject_once and self.reviews == 1
        return {"passed": not rejected, "issues": [{"unit_id": payload["timeline"][1]["id"],
                "reason": "abrupt topic change"}] if rejected else [], "visual_assessment": "unknown"}


def fake_render(source, workspace, timeline):
    output = workspace / "film.mp4"
    output.write_bytes(b"test-render")
    return output


class SmartPipelineTest(unittest.TestCase):
    def test_three_content_driven_structures_and_three_distinct_candidates(self):
        names = set()
        for role in ("result", "styling", "material"):
            profiles = pipeline.profile(FakeAI(role), fixture(), "上衣")
            result = pipeline.arrange(profiles, 15, 20)
            self.assertGreaterEqual(len(result["candidates"]), 3)
            self.assertEqual(len({tuple(c["unit_ids"]) for c in result["candidates"]}), len(result["candidates"]))
            names.add(result["candidates"][0]["structure"])
            selected = next(c for c in result["candidates"] if c["id"] == result["selected"])
            self.assertEqual(selected["score"]["total"], max(c["score"]["total"] for c in result["candidates"]))
            self.assertTrue(all(15 <= c["duration"] <= 20 for c in result["candidates"]))
        self.assertEqual(names, {"上身效果型", "穿搭型", "面料品质型"})

    def test_review_replaces_whole_sentence_then_rechecks(self):
        ai = FakeAI(reject_once=True)
        profiles = pipeline.profile(ai, fixture(), "上衣")
        result = pipeline.review(ai, profiles, pipeline.arrange(profiles, 15, 20), 15, 20, "上衣")
        self.assertTrue(result["passed"])
        self.assertEqual(ai.reviews, 2)
        self.assertNotEqual(result["initial_unit_ids"], result["final_unit_ids"])
        self.assertIn("from", result["replacements"][0])
        self.assertTrue(all(u["end"] - u["start"] == 5 for u in result["timeline"]))

    def test_review_is_bounded_and_never_fakes_success(self):
        ai = FakeAI(reject_always=True)
        profiles = pipeline.profile(ai, fixture(), "上衣")
        result = pipeline.review(ai, profiles, pipeline.arrange(profiles, 15, 20), 15, 20, "上衣")
        self.assertFalse(result["passed"])
        self.assertEqual(ai.reviews, 3)

    def test_schema_rejects_missing_duplicate_unknown_and_bad_values(self):
        for mutate in (lambda p: p.pop(), lambda p: p.append(p[0]),
                       lambda p: p[0].update(strength=float("nan")),
                       lambda p: p[0].update(dependency=["missing"]),
                       lambda p: p[0].update(usable="yes")):
            raw = FakeAI()("profile", {"units": fixture()})
            mutate(raw["profiles"])
            with self.assertRaises(ValueError):
                validate_profiles(raw, fixture())
        raw = FakeAI()("profile", {"units": fixture()})
        raw["profiles"][0]["role"] = []
        validate_profiles(raw, fixture())  # absent optional role is legal
        with self.assertRaises(ValueError):
            validate_review({"passed": True, "issues": [{"unit_id": "u0", "reason": "bad"}]}, {"u0"})

    def test_constraints_and_insufficient_material(self):
        profiles = pipeline.profile(FakeAI(), fixture(), "上衣")
        profiles[0]["dependency"] = ["u7"]
        issues = pipeline.audit(profiles[:3], 15, 20)
        self.assertTrue(any("context" in i["reason"] for i in issues))
        profiles[0]["dependency"] = []
        profiles[1]["claim_cluster"] = profiles[0]["claim_cluster"]
        self.assertTrue(any("repeated" in i["reason"] for i in pipeline.audit(profiles[:3], 15, 20)))
        with self.assertRaises(ValueError):
            pipeline.arrange(profiles[:1], 15, 20)

    def test_coarse_filter_only_high_confidence(self):
        units = fixture()
        units[0]["text"] = "库存只剩十件。"
        units[1]["text"] = "搭配裤子很好看。"
        units[2]["text"] = units[1]["text"]
        result = pipeline.coarse_filter(units)
        self.assertEqual([u["id"] for u in result["removed"]], ["u0", "u2"])
        self.assertIn("u1", [u["id"] for u in result["kept"]])
        units[3]["text"] = "接下来介绍裤子。"
        result = pipeline.coarse_filter(units, "毛衣")
        self.assertIn("u3", [u["id"] for u in result["removed"]])


class SmartServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "素材.mp4"
        self.source.write_bytes(b"fixture")
        self.app = Application(self.root)
        self.app.viral.list_jobs()
        self.baseline = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                         (self.root / "data/agent.db", self.root / "data/viral_v2/viral_v2.db")}
        self.service = SmartService(self.root, ai=FakeAI(), understand=lambda *a: fixture(), render=fake_render)
        self.app._smart = self.service

    def tearDown(self):
        self.tmp.cleanup()

    def create(self):
        return self.service.create_job({"source_path": str(self.source), "product_name": "上衣", "target_min": 15, "target_max": 20})

    def unchanged(self):
        for path, digest in self.baseline.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_end_to_end_failure_retry_delete_are_isolated(self):
        job = self.create()
        self.service.ai = FakeAI(reject_always=True)
        failed = self.service.run(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(any(a["name"] == "film.mp4" for a in failed["artifacts"]))
        self.assertFalse(failed["stages"]["S5"]["result"]["passed"])
        self.unchanged()
        self.service.ai = FakeAI(reject_once=True)
        ready = self.service.run(job["id"], retry=True)
        self.assertEqual(ready["status"], "completed")
        self.assertEqual(ready["attempt"], 2)
        self.assertTrue(self.service.artifact(job["id"], "film.mp4").is_file())
        self.assertIsNone(ready["feedback"]["retention_ratio"])
        self.assertEqual(set(ready["stages"]), {"S1", "S2", "S3", "S4", "S5"})
        self.unchanged()
        with self.assertRaises(KeyError):
            self.service.artifact(job["id"], "../../agent.db")
        self.service.delete_job(job["id"])
        self.assertFalse((self.service.jobs_root / job["id"]).exists())
        self.unchanged()
        self.assertEqual(self.source.read_bytes(), b"fixture")

    def test_lazy_initialization_and_invalid_input(self):
        app = Application(self.root / "fresh")
        self.assertFalse((app.root / "data/smart_v3").exists())
        with self.assertRaises(ValueError):
            self.service.create_job({"source_path": str(self.source), "product_name": "x", "target_min": float("nan")})
        self.service.lock.acquire()
        try:
            with self.assertRaises(ValueError):
                self.service.run("unknown")
        finally:
            self.service.lock.release()

    def test_schema_and_render_failures_keep_stage_evidence_and_isolation(self):
        job = self.create()
        self.service.ai = lambda *args: {"profiles": []}
        result = self.service.run(job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stages"]["S3"]["status"], "failed")
        self.assertEqual(result["stages"]["S2"]["status"], "completed")
        self.unchanged()
        self.service.ai = FakeAI()
        def broken_render(*args):
            raise RuntimeError("fixture render failure")
        self.service.render = broken_render
        result = self.service.run(job["id"], retry=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["stages"]["S5"]["status"], "failed")
        self.assertTrue(result["stages"]["S5"]["result"]["passed"])
        self.unchanged()
        self.service.delete_job(job["id"])
        self.unchanged()

    def test_http_routes_and_artifact_access(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        def request(path, method="GET", body=None):
            req = urllib.request.Request(base + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, response.read()
        try:
            status, body = request("/api/smart-v3/jobs", "POST", {"source_path": str(self.source), "product_name": "上衣", "target_min": 15, "target_max": 20})
            self.assertEqual(status, 201)
            job = json.loads(body)
            path = "/api/smart-v3/jobs/" + job["id"]
            self.assertEqual(json.loads(request(path + "/run", "POST", {})[1])["status"], "completed")
            self.assertEqual(request(path + "/artifacts/film.mp4")[1], b"test-render")
            self.assertEqual(json.loads(request(path)[1])["version"], "V3")
            self.assertEqual(len(json.loads(request("/api/smart-v3/jobs")[1])["jobs"]), 1)
            request(path + "/retry", "POST", {})
            request(path, "DELETE")
            with self.assertRaises(urllib.error.HTTPError) as exc:
                request(path)
            self.assertEqual(exc.exception.code, 404)
            self.unchanged()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class SmartRenderTest(unittest.TestCase):
    def test_real_media_transcript_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            source = workspace / "source.mp4"
            pipeline.command(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25",
                              "-f", "lavfi", "-i", "sine=frequency=440", "-t", "40", "-c:v", "libx264", "-c:a", "aac", str(source)])
            transcript = workspace / "transcript.json"
            transcript.write_text(json.dumps(fixture(), ensure_ascii=False), encoding="utf-8")
            units = pipeline.understand(str(source), workspace, str(transcript))
            service = SmartService(workspace, ai=FakeAI(), render=pipeline.render)
            job = service.create_job({"source_path": str(source), "product_name": "上衣", "target_min": 15,
                                      "target_max": 20, "transcript_path": str(transcript)})
            result = service.run(job["id"])
            self.assertEqual(result["status"], "completed", result["error"])
            self.assertTrue(service.artifact(job["id"], "film.mp4").stat().st_size > 1000)
            self.assertEqual(len(units), 8)
