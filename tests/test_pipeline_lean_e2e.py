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
from unittest.mock import patch

from agent_video.pipeline import run as pipeline_run
from agent_video.pipeline.ai import DECISION_SCHEMA
from agent_video.pipeline.errors import (AIReturnError, AsrError, RenderError,
                                         RuleFilterEmpty, TargetUnreachable)
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
    import re
    ids = [int(value) for value in re.findall(r'"id": (\d+)', prompt)]
    if schema is DECISION_SCHEMA:
        return {"decisions": [{"id": cid, "usable": True, "reason": "mock 可用"}
                              for cid in ids]}
    return {"main_product": "马甲", "ordered_ids": ids}


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

    def test_golden_path_renders_final_mp4_with_two_ai_calls(self):
        with patch.object(pipeline_run, "ai_call", side_effect=fake_ai) as mocked:
            manifest = self.run_pipeline()
        self.assertEqual(manifest["ai_calls"], 2)
        self.assertEqual(mocked.call_count, 2)
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

    def test_too_few_usable_clauses_fail_before_ordering_call(self):
        def stingy(model, prompt, schema, timeout):
            ids = [int(value) for value in re.findall(r'"id": (\d+)', prompt)]
            return {"decisions": [{"id": cid, "usable": cid == ids[0], "reason": "only one"}
                                  for cid in ids]}

        with patch.object(pipeline_run, "ai_call", side_effect=stingy) as mocked:
            with self.assertRaises(TargetUnreachable):
                self.run_pipeline()
        self.assertEqual(mocked.call_count, 1)


class RenderSeamTest(unittest.TestCase):
    def test_invalid_bounds_raise_render_error(self):
        with self.assertRaises(RenderError):
            build_segments([{"id": 0, "start": 3.0, "end": 3.0, "text": "x"}])


if __name__ == "__main__":
    unittest.main()
