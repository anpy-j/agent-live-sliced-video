import subprocess
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_video.engine.scripts.qc import stream_issues
from agent_video.engine.scripts.audit_bounds import issue_level, main as audit_bounds_main
from agent_video.engine.scripts.textnorm import content_rejection, incomplete_ending
from agent_video.engine.scripts.validate_timeline import validate_rows
from agent_video.engine.scripts.visual_mix import apply as apply_visual_mix
from agent_video.engine.scripts import render_dual

GRAPH_OPTIONS = ("-filter_complex_script", "-/filter_complex")


def read_filter_graph(command):
    option = next(name for name in GRAPH_OPTIONS if name in command)
    return Path(command[command.index(option) + 1]).read_text(encoding="utf-8")


class EngineValidationTest(unittest.TestCase):
    def test_secondary_product_detail_is_advisory(self):
        self.assertEqual(issue_level({"type": "secondary_product_detail"}), "warning")
        self.assertEqual(issue_level({"type": "cut_inside_token"}), "error")

    def test_secondary_product_detail_does_not_fail_boundary_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline = root / "timeline.json"
            timeline.write_text(json.dumps([
                {"src": 1, "start": 0, "end": 2, "text": "牛仔裤颜色"},
            ]), encoding="utf-8")
            words = root / "words.json"
            words.write_text(json.dumps([
                {"s": 0, "e": 2, "w": "牛仔裤颜色"},
            ]), encoding="utf-8")
            report = root / "report.json"
            argv = ["audit_bounds.py", str(timeline), str(words),
                    "--main-product", "针织衫", "--secondary-products", "牛仔裤",
                    "--secondary-attributes", "颜色", "--report", str(report)]
            with patch("sys.argv", argv):
                code = audit_bounds_main()
            result = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["error_count"], 0)
            self.assertEqual(result["warning_count"], 1)

    def test_legacy_visual_review_pipeline_stays_removed(self):
        scripts = Path(__file__).parents[1] / "agent_video" / "engine" / "scripts"
        self.assertFalse((scripts / "visual_review.py").exists())
        self.assertFalse((scripts / "find_broll.py").exists())
        production = "\n".join((scripts / name).read_text(encoding="utf-8") for name in
                               ("run_slice.py", "qc.py", "frames.py"))
        for forbidden in ("visual_review", "selected_detail.jpg", "contact_sheet",
                          "replace_with_broll"):
            self.assertNotIn(forbidden, production)

    def test_visual_mix_replaces_only_requested_bad_block(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline = root / "body.json"
            timeline.write_text(json.dumps([
                {"audio": {"src": 1, "start": 1, "end": 4, "text": "显瘦"},
                 "video": [{"src": 1, "start": 1, "end": 4, "kind": "aroll"}]},
            ]), encoding="utf-8")
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"dual_timelines": {"body": str(timeline)}}),
                               encoding="utf-8")
            packet = root / "packet.json"
            packet.write_text(json.dumps({"duration": 30, "replacement_blocks": [
                {"block_id": "body:0", "duration": 3}], "candidates": [
                {"candidate_id": "C001", "start": 10, "end": 13}]}), encoding="utf-8")
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({"replacements": [
                {"block_id": "body:0", "candidate_id": "C001", "reason": "同款全身"}]}),
                encoding="utf-8")
            report = apply_visual_mix(packet, decisions, mapping, root / "report.json")
            result = json.loads(timeline.read_text(encoding="utf-8"))
            self.assertEqual(report["replaced"], 1)
            self.assertEqual(result[0]["audio"]["start"], 1)
            self.assertEqual(result[0]["video"][0]["start"], 10)
            self.assertEqual(result[0]["video"][0]["kind"], "broll")

            decisions.write_text(json.dumps({"replacements": [
                {"block_id": "body:0", "candidate_id": "C001", "reason": "正面",
                 "shot_type": "front_face", "mouth_visibility": "clear"}]}),
                encoding="utf-8")
            report = apply_visual_mix(packet, decisions, mapping, root / "report-2.json")
            result = json.loads(timeline.read_text(encoding="utf-8"))
            self.assertEqual(report["replaced"], 0)
            self.assertEqual(result[0]["video"][0]["start"], 1)
            self.assertEqual(result[0]["video"][0]["kind"], "aroll")

    def test_visual_mix_rejects_reused_shots_across_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline = root / "body.json"
            timeline.write_text(json.dumps([
                {"audio": {"src": 1, "start": 1, "end": 4, "text": "第一句"},
                 "video": [{"src": 1, "start": 1, "end": 4, "kind": "aroll"}]},
                {"audio": {"src": 1, "start": 5, "end": 7, "text": "第二句"},
                 "video": [{"src": 1, "start": 5, "end": 7, "kind": "aroll"}]},
                {"audio": {"src": 1, "start": 8, "end": 10, "text": "第三句"},
                 "video": [{"src": 1, "start": 8, "end": 10, "kind": "aroll"}]},
            ]), encoding="utf-8")
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"dual_timelines": {"body": str(timeline)}}),
                               encoding="utf-8")
            packet = root / "packet.json"
            packet.write_text(json.dumps({"duration": 40, "replacement_blocks": [
                {"block_id": "body:0", "duration": 3},
                {"block_id": "body:1", "duration": 2},
                {"block_id": "body:2", "duration": 2}], "candidates": [
                {"candidate_id": "C001", "start": 10, "end": 13},
                {"candidate_id": "C002", "start": 20, "end": 23},
                {"candidate_id": "C003", "start": 11, "end": 14}]}), encoding="utf-8")
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({"replacements": [
                {"block_id": "body:0", "candidate_id": "C001", "reason": "同款全身"},
                {"block_id": "body:1", "candidate_id": "C001", "reason": "同款全身"},
                {"block_id": "body:2", "candidate_id": "C003", "reason": "同款细节"}]}),
                encoding="utf-8")
            report = apply_visual_mix(packet, decisions, mapping, root / "report.json")
            result = json.loads(timeline.read_text(encoding="utf-8"))
            self.assertEqual(report["replaced"], 1)
            self.assertEqual(result[0]["video"][0]["candidate_id"], "C001")
            self.assertEqual(result[1]["video"][0]["kind"], "aroll")
            self.assertEqual(result[2]["video"][0]["kind"], "aroll")
            ignored_reasons = [str(item.get("reason", "")) for item in report["ignored"]]
            self.assertEqual(len([reason for reason in ignored_reasons if "重复" in reason]), 2)
            self.assertEqual(report["duplicate_shots_rejected"], 2)

    def test_price_vocabulary_is_hard_banned(self):
        import importlib
        import os
        from agent_video.engine.scripts import badvocab
        self.assertTrue(badvocab.hit("这个蓝牛也能搭就配这件外套5,980专柜售价"))
        self.assertIsNone(badvocab.hit("这件毛衣上身显瘦又利落"))
        self.assertIn("专柜", badvocab.review_hits("专柜品质的做工"))
        profile = Path(__file__).parents[1] / "agent_video" / "engine" / "profiles" / "douyin-strict.json"
        with patch.dict(os.environ, {"DOUYIN_VOCAB_PROFILE": str(profile)}):
            strict = importlib.reload(badvocab)
            self.assertTrue(strict.hit("到手价只要三百块"))
            self.assertTrue(strict.hit("这件专柜售价一千二"))
            self.assertTrue(strict.hit("这款只卖5,980"))
            self.assertIsNone(strict.hit("这件毛衣上身显瘦又利落"))
        importlib.reload(badvocab)

    def test_qc_rejects_wrong_resolution(self):
        info = {
            "format": {"duration": "2.0"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 320, "height": 240,
                 "duration": "2.0"},
                {"codec_type": "audio", "codec_name": "aac", "duration": "2.0"},
            ],
        }
        _, _, _, issues = stream_issues(info)
        resolution = next(item for item in issues if item["code"] == "unexpected_resolution")
        self.assertEqual(resolution["expected"], [1440, 2560])

    def test_sentence_rule_is_narrow_and_punctuation_cannot_hide_connector(self):
        self.assertEqual(incomplete_ending("推荐它是因为。"), "因为")
        self.assertIsNone(incomplete_ending("这件衣服是很显瘦的"))

    def test_content_gate_rejects_fragments_stage_chatter_and_malformed_asr(self):
        self.assertEqual(content_rejection("的收腰的感觉很像"),
                         "context_dependent_start")
        self.assertEqual(content_rejection("删掉来整个先删掉好"), "stage_chatter")
        self.assertEqual(content_rejection("轻柔羊毛手手"), "malformed_speech")
        self.assertIsNone(content_rejection("这件毛衣上身显瘦又利落"))

    @patch("agent_video.engine.scripts.validate_timeline.media_duration", return_value=100)
    def test_timeline_validator_blocks_incomplete_close(self, _duration):
        rows = [
            {"src": 1, "start": 0, "end": 2, "text": "直接看效果", "role": "hook"},
            {"src": 1, "start": 10, "end": 12, "text": "上身很显瘦", "role": "proof"},
            {"src": 1, "start": 20, "end": 22, "text": "通勤可以穿", "role": "scene"},
            {"src": 1, "start": 30, "end": 32, "text": "推荐它是因为", "role": "close"},
        ]
        result = validate_rows(rows, {"1": "source.mp4"}, 0, 20, 1, 10, 1.2, 5, 5, True)
        self.assertIn("incomplete_sentence", {item["code"] for item in result["issues"]})

    @patch("agent_video.engine.scripts.validate_timeline.media_duration", return_value=100)
    def test_shared_validator_issues_have_levels_in_final_report(self, _duration):
        rows = [
            {"src": 1, "start": 0, "end": 2, "text": "羊毛面料很舒服", "role": "material"},
            {"src": 1, "start": 10, "end": 12, "text": "这个羊毛材质很舒服", "role": "material"},
        ]
        result = validate_rows(rows, {"1": "source.mp4"}, 0, 20, 1, 10, 1.2, 5, 5, False)
        self.assertTrue(result["issues"])
        self.assertTrue(all(item["level"] in {"error", "warning"}
                            for item in result["issues"]))

    @patch("agent_video.engine.scripts.validate_timeline.media_duration", return_value=100)
    def test_timeline_validator_allows_small_alignment_overrun(self, _duration):
        rows = [
            {"src": 1, "start": 0, "end": 5.3, "text": "完整卖点", "role": "proof"},
        ]
        result = validate_rows(rows, {"1": "source.mp4"}, 0, 20, 1, 10, 1.2, 5, 5, False)
        overruns = [item for item in result["issues"] if item["code"] == "segment_too_long"]
        self.assertTrue(overruns)
        self.assertTrue(all(item["level"] == "warning" for item in overruns))

    def test_render_dual_keeps_crop_position_per_piece(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            timeline = root / "timeline.json"
            timeline.write_text(json.dumps([{
                "audio": {"src": 1, "start": 0, "end": 2},
                "video": [
                    {"src": 1, "start": 0, "end": 1, "crop_x": 0.1},
                    {"src": 1, "start": 1, "end": 2, "crop_x": 0.9},
                ],
            }]), encoding="utf-8")
            output = root / "output.mp4"
            commands = []
            graphs = []

            def fake_run(command):
                graphs.append(read_filter_graph(command))
                commands.append(command)
                Path(str(output.resolve()) + ".partial.mp4").write_bytes(b"video")
                return ""

            argv = ["render_dual.py", str(timeline), str(output), "--src", f"1={source}"]
            with patch("sys.argv", argv), \
                    patch.object(render_dual, "source_fps", return_value=30), \
                    patch.object(render_dual, "output_size", return_value=(1080, 1920)), \
                    patch.object(render_dual, "run", side_effect=fake_run):
                render_dual.main()
            filters = graphs[-1]
            self.assertEqual(filters.count("(iw-ow)*0.100000"), 1)
            self.assertEqual(filters.count("(iw-ow)*0.900000"), 1)

    @patch.object(render_dual, "source_fps", return_value=30)
    @patch.object(render_dual, "output_size", return_value=(1080, 1920))
    def test_render_dual_long_timeline_stays_under_command_line_limit(self, *_):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            rows = [{"audio": {"src": 1, "start": index * 2, "end": index * 2 + 2},
                     "video": [{"src": 1, "start": index * 2, "end": index * 2 + 2}]}
                    for index in range(120)]
            timeline = root / "timeline.json"
            timeline.write_text(json.dumps(rows), encoding="utf-8")
            output = root / "output.mp4"
            commands = []
            graphs = []

            def fake_run(command):
                graphs.append(read_filter_graph(command))
                commands.append(command)
                Path(str(output.resolve()) + ".partial.mp4").write_bytes(b"video")
                return ""

            argv = ["render_dual.py", str(timeline), str(output), "--src", f"1={source}"]
            with patch("sys.argv", argv), \
                    patch.object(render_dual, "run", side_effect=fake_run):
                render_dual.main()
            command = commands[-1]
            self.assertLess(len(subprocess.list2cmdline(command)), 32767)
            filters = graphs[-1]
            self.assertEqual(filters.count("[vcat]"), 1)
            self.assertEqual(len(filters.split(";")), 120 + 120 + 3)


if __name__ == "__main__":
    unittest.main()
