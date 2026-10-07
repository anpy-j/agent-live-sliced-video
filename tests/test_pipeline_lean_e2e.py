# -*- coding: utf-8 -*-
"""精简管线端到端测试。

AI 全部 mock，但 S1 之后的真实 ffmpeg 渲染必须跑通一个小样（合成素材）。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_video.pipeline import run as pipeline_run
from agent_video.pipeline.ai import DECISION_SCHEMA
from agent_video.pipeline.editorial import INVENTORY_SCHEMA, PLAN_SCHEMA, REVIEW_SCHEMA
from agent_video.pipeline.errors import (AIReturnError, AsrError, RenderError,
                                         RuleFilterEmpty)
from agent_video.pipeline.render import build_segments

FFMPEG = shutil.which("ffmpeg")
WORDS = [
    {"w": "这件马甲", "s": 0.0, "e": 0.5},
    {"w": "很显瘦。", "s": 0.5, "e": 1.4},
    {"w": "面料", "s": 1.8, "e": 2.6},
    {"w": "很舒服", "s": 2.6, "e": 3.6},
    {"w": "弹力也大。", "s": 4.0, "e": 4.6},
    {"w": "白色", "s": 4.6, "e": 5.2},
    {"w": "很百搭。", "s": 5.2, "e": 6.0},
    {"w": "配牛仔裤", "s": 6.5, "e": 7.3},
    {"w": "特别好看。", "s": 7.3, "e": 8.3},
    {"w": "减龄又显气质。", "s": 8.8, "e": 9.8},
]


def make_media(path):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         path],
        check=True, capture_output=True)


def fake_ai(model, prompt, schema, timeout):
    ids = [int(value) for value in re.findall(r'"id": (\d+)', prompt)]
    if schema is INVENTORY_SCHEMA:
        return {"candidates": [{"id": cid, "topic": "fabric", "facts": [str(cid)],
                "subject": "马甲", "eligible": True, "requires": [], "reason": "独立信息"} for cid in ids]}
    if schema is REVIEW_SCHEMA:
        return {"passed": True, "issues": []}
    if schema is DECISION_SCHEMA:
        return {"decisions": [{"id": cid, "usable": True, "reason": "mock 可用"} for cid in ids]}
    data = json.loads(prompt.splitlines()[-1])
    ids = [c["id"] for c in data["candidates"]]
    return {"main_product": "马甲", "opening_topic": "fabric",
            "sections": [{"role": "hook", "topic": "fabric", "ids": ids}], "ordered_ids": ids}


@unittest.skipUnless(FFMPEG, "ffmpeg 不可用")
class LeanPipelineEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = os.path.join(self.temp.name, "media.mp4")
        make_media(self.media)
        self.workdir = os.path.join(self.temp.name, "out")

    def run_pipeline(self, **kwargs):
        kwargs.setdefault("transcript", ([], WORDS))
        kwargs.setdefault("target_seconds", (8.0, 9.0))
        return pipeline_run.run_pipeline(self.media, self.workdir, **kwargs)

    def test_semantic_selection_removes_duplicate_from_render_and_keeps_new_fact(self):
        words = [{"w": "百分百羊毛。", "s": 0., "e": 2.},
                 {"w": "纯羊毛材质。", "s": 3., "e": 5.},
                 {"w": "贴身不会扎皮肤。", "s": 6., "e": 8.}]
        def semantic_ai(model, prompt, schema, timeout):
            if schema is INVENTORY_SCHEMA:
                data = fake_ai(model, prompt, schema, timeout)
                data["candidates"][1]["eligible"] = False
                return data
            return fake_ai(model, prompt, schema, timeout)
        with patch.object(pipeline_run, "ai_call", side_effect=semantic_ai):
            manifest = self.run_pipeline(transcript=([], words), target_seconds=(120., 180.))
        self.assertEqual([s["id"] for s in manifest["segments"]], [0, 2])
        self.assertEqual(manifest["total_seconds"], 4.)
        self.assertEqual(manifest["semantic_summary"]["selection_removed"], 1)
        self.assertTrue(Path(self.workdir, manifest["output"]).is_file())
        timeline = json.loads(Path(self.workdir, "timeline.json").read_text("utf8"))
        self.assertIsNone(timeline["clauses"][1]["order"])

    def test_golden_path_renders_final_mp4_with_semantic_selection_and_review(self):
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai) as mocked:
            manifest = self.run_pipeline()
        self.assertEqual(manifest["ai_calls"], 4)
        self.assertEqual(mocked.call_count, 4)
        self.assertEqual(manifest["judge_concurrency"], 1)
        self.assertEqual(manifest["judge_retries"], 1)
        self.assertEqual(manifest["main_product"], "马甲")
        self.assertEqual(len(manifest["segments"]), 5)
        self.assertAlmostEqual(manifest["total_seconds"], 8.4, places=2)

        output = os.path.join(self.workdir, manifest["output"])
        self.assertTrue(os.path.isfile(output), output)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", output],
            check=True, capture_output=True, text=True).stdout.strip()
        self.assertAlmostEqual(float(probe), 8.4, delta=0.5)

        with open(os.path.join(self.workdir, "timeline.json"), encoding="utf-8") as handle:
            timeline = json.load(handle)
        self.assertEqual(timeline["source"], os.path.abspath(self.media))
        self.assertTrue(all(clause["usable"] for clause in timeline["clauses"]))
        self.assertEqual([clause["order"] for clause in timeline["clauses"]],
                         list(range(5)))

        with open(os.path.join(self.workdir, "clauses.judged.json"), encoding="utf-8") as handle:
            judged = json.load(handle)
        self.assertTrue(all(clause["usable"] for clause in judged["clauses"]))
        self.assertEqual(len(judged["clauses"]), len(timeline["clauses"]))

    def test_judge_batch_retry_recovers_and_counts_calls(self):
        state = {"failed": False}

        def flaky(model, prompt, schema, timeout):
            if schema is DECISION_SCHEMA and not state["failed"]:
                state["failed"] = True
                raise AIReturnError("S3 返回缺少 decisions 数组")
            return fake_ai(model, prompt, schema, timeout)

        with patch.object(pipeline_run, "ai_call", side_effect=flaky):
            manifest = self.run_pipeline(judge_retries=1)
        self.assertEqual(manifest["judge_retries"], 1)
        self.assertEqual(manifest["ai_calls"], 5)  # 判定失败/重试 + 分组/排序/复审
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, manifest["output"])))

    def test_contiguous_fragments_merge_and_long_unit_extends_duration(self):
        words = [
            {"w": "同样的是T恤", "s": 0.0, "e": 1.5},
            {"w": "我们会给到你三到五年", "s": 1.5, "e": 3.5},
            {"w": "没有任何变化因为", "s": 3.5, "e": 5.5},
            {"w": "整个领子袖口全部做罗纹", "s": 5.5, "e": 8.5},
            {"w": "而且它非常好搭", "s": 8.5, "e": 11.0},
        ]
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            manifest = self.run_pipeline(transcript=([], words),
                                         target_seconds=(8.0, 9.0), merge_max=13.0)
        self.assertEqual(manifest["sentence_units"], 1)
        self.assertEqual(manifest["clauses"], 2)
        self.assertEqual(len(manifest["segments"]), 1)
        self.assertGreater(manifest["total_seconds"], manifest["target_seconds"]["max"])

    def test_select_visual_seam_changes_segment_bounds(self):
        def offset(clause):
            return clause["start"] + 0.05, clause["end"]

        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            manifest = self.run_pipeline(select_visual_fn=offset)
        first = manifest["segments"][0]
        self.assertNotAlmostEqual(first["start"], first["source_start"])

    def test_rule_filter_empty_is_reported_before_ai(self):
        words = [{"w": "拍它。", "s": 0.0, "e": 1.5},
                 {"w": "上链接。", "s": 1.5, "e": 3.0}]
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai) as mocked:
            with self.assertRaises(RuleFilterEmpty):
                self.run_pipeline(transcript=([], words))
        mocked.assert_not_called()

    def test_bad_ai_coverage_stops_before_render(self):
        def broken(model, prompt, schema, timeout):
            return {"decisions": []}

        with patch.object(pipeline_run, "ai_call", side_effect=broken):
            with self.assertRaises(AIReturnError):
                self.run_pipeline()
        self.assertFalse(os.path.exists(os.path.join(self.workdir, "deliverables",
                                                     "final.mp4")))

    def test_empty_transcript_is_an_asr_error(self):
        with self.assertRaises(AsrError):
            self.run_pipeline(transcript=([], []))

    def test_below_target_material_still_renders_without_abort(self):
        def only_first(model, prompt, schema, timeout):
            ids = [int(value) for value in re.findall(r'"id": (\d+)', prompt)]
            if schema is DECISION_SCHEMA:
                return {"decisions": [{"id": cid, "usable": cid == ids[0], "reason": "keep one"}
                                      for cid in ids]}
            return fake_ai(model, prompt, schema, timeout)

        with patch.object(pipeline_run, "ai_call", side_effect=only_first):
            manifest = self.run_pipeline()
        self.assertLess(manifest["total_seconds"], manifest["target_seconds"]["min"])
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, manifest["output"])))

    def test_pipeline_reuses_cached_clauses_without_asr(self):
        workdir = os.path.join(self.temp.name, "cached_workdir")
        os.makedirs(workdir, exist_ok=True)
        cached_timeline = {
            "source": os.path.abspath(self.media),
            "duration": 10.0,
            "clauses": [
                {"id": 0, "start": 0.0, "end": 2.0, "text": "这件衣服面料很好。"},
                {"id": 1, "start": 2.5, "end": 5.0, "text": "穿上特别显瘦。"},
                {"id": 2, "start": 5.5, "end": 8.0, "text": "颜色也非常百搭。"},
            ],
        }
        with open(os.path.join(workdir, "clauses.json"), "w", encoding="utf-8") as f:
            json.dump(cached_timeline, f)

        stages = []
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai), \
                patch.object(pipeline_run, "_default_asr") as mock_asr:
            manifest = pipeline_run.run_pipeline(
                self.media, workdir, target_seconds=(4.0, 8.0),
                on_stage=lambda s, st, msg: stages.append((s, st, msg)))

        mock_asr.assert_not_called()
        self.assertTrue(any("复用已有" in msg for s, st, msg in stages if s == "asr"))
        output = os.path.join(workdir, manifest["output"])
        self.assertTrue(os.path.isfile(output))

    def test_repeated_render_creates_versioned_video_without_overwrite(self):
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            first = self.run_pipeline()
            first_path = os.path.join(self.workdir, first["output"])
            first_bytes = Path(first_path).read_bytes()
            second = self.run_pipeline()

        self.assertEqual(first["output"], os.path.join("deliverables", "final.mp4"))
        self.assertEqual(second["output"], os.path.join("deliverables", "final-1.mp4"))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, second["output"])))
        self.assertEqual(Path(first_path).read_bytes(), first_bytes)

    def test_output_stem_names_deliverable_and_versions(self):
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            first = self.run_pipeline(output_stem="伯恩夫人0926")
        self.assertEqual(first["output"], os.path.join("deliverables", "伯恩夫人0926.mp4"))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, first["output"])))

        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            second = self.run_pipeline(output_stem="伯恩夫人0926")
        self.assertEqual(second["output"],
                         os.path.join("deliverables", "伯恩夫人0926-1.mp4"))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, second["output"])))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, first["output"])))

    def test_single_stage_reruns_preserve_upstream_files(self):
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            self.run_pipeline()
        s1_path = Path(self.workdir) / "clauses.json"
        s2_path = Path(self.workdir) / "clauses.filtered.json"
        s3_path = Path(self.workdir) / "clauses.judged.json"
        s1_before = s1_path.read_bytes()

        with patch.object(pipeline_run, "ai_call") as ai:
            result2 = pipeline_run.run_pipeline_stage(
                self.media, self.workdir, "filter", target_seconds=(8.0, 9.0))
        ai.assert_not_called()
        self.assertEqual(result2["stage"], "filter")
        self.assertEqual(s1_path.read_bytes(), s1_before)
        s2_before = s2_path.read_bytes()

        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            result3 = pipeline_run.run_pipeline_stage(
                self.media, self.workdir, "judge", target_seconds=(8.0, 9.0))
        self.assertEqual(result3["stage"], "judge")
        self.assertEqual(s1_path.read_bytes(), s1_before)
        self.assertEqual(s2_path.read_bytes(), s2_before)
        s3_before = s3_path.read_bytes()

        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            result4 = pipeline_run.run_pipeline_stage(
                self.media, self.workdir, "order", target_seconds=(8.0, 9.0))
        self.assertEqual(result4["stage"], "order")
        self.assertEqual(s1_path.read_bytes(), s1_before)
        self.assertEqual(s2_path.read_bytes(), s2_before)
        self.assertEqual(s3_path.read_bytes(), s3_before)

        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            pipeline_run.run_pipeline_stage(self.media, self.workdir, "review", target_seconds=(8.0, 9.0))
        order_path = Path(self.workdir) / "order.json"
        order_before = order_path.read_bytes()
        with patch.object(pipeline_run, "ai_call") as ai:
            result5 = pipeline_run.run_pipeline_stage(
                self.media, self.workdir, "render", target_seconds=(8.0, 9.0),
                output_stem="重渲染")
        ai.assert_not_called()
        self.assertEqual(result5["output"], os.path.join("deliverables", "重渲染.mp4"))
        self.assertTrue(os.path.isfile(os.path.join(self.workdir, result5["output"])))
        self.assertEqual(s1_path.read_bytes(), s1_before)
        self.assertEqual(s2_path.read_bytes(), s2_before)
        self.assertEqual(s3_path.read_bytes(), s3_before)
        self.assertEqual(order_path.read_bytes(), order_before)

    def test_render_stage_rerun_uses_virtual_timeline_sources(self):
        class FakeTimeline:
            title = "虚拟时间线"
            timeline_id = "vt-1"

            def map_timeline_range(self, start, end):
                return [{"timeline_id": self.timeline_id,
                         "timeline_start": start, "timeline_end": end,
                         "segment_id": "s1", "segment_offset": 0.0,
                         "source_path": "real-source.ts",
                         "source_start": start + 10, "source_end": end + 10,
                         "speed": 1.0}]

            def to_dict(self):
                return {"timeline_id": self.timeline_id, "title": self.title,
                        "segments": []}

        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai):
            self.run_pipeline()
        timeline = FakeTimeline()
        with patch.object(pipeline_run, "render_video") as render:
            result = pipeline_run.run_pipeline_stage(
                self.media, self.workdir, "render", target_seconds=(8.0, 9.0),
                output_stem="重渲染", virtual_timeline=timeline)

        segments = render.call_args.args[1]
        self.assertTrue(segments)
        self.assertTrue(all(row["source_path"] == "real-source.ts" for row in segments))
        self.assertIs(render.call_args.kwargs["virtual_timeline"], timeline)
        self.assertEqual(result["timeline_id"], "vt-1")


class RenderSeamTest(unittest.TestCase):
    def test_invalid_bounds_raise_render_error(self):
        with self.assertRaises(RenderError):
            build_segments([{"id": 0, "start": 3.0, "end": 3.0, "text": "x"}])


if __name__ == "__main__":
    unittest.main()
