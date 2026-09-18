import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.db import Store
from agent_video.ai import PLAN_PATCH_SCHEMA, ProviderResponseError
from agent_video.engine.scripts.cuts import expand
from agent_video.engine.scripts.digest_candidates import category, quality
from agent_video.engine.scripts.prep import (cache_key, parse_subtitle,
                                             words_from_subtitles)
from agent_video.engine.scripts import cuts, prep
from agent_video.engine.scripts.visual_mix import (apply as apply_visual_mix,
                                                   prepare as prepare_visual_mix,
                                                   ranked_candidates)
from agent_video.engine.validation_policy import shared_issues
from agent_video.rules import DirectivesManager
from agent_video.runner import JobRunner, PlanRefinementError
from agent_video.server import Application


class PipelineFeatureTest(unittest.TestCase):
    def test_enterprise_console_assets_and_accessibility_contract(self):
        root = Path(__file__).resolve().parents[1]
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        app = (root / "web" / "app.js").read_text(encoding="utf-8")
        theme = (root / "web" / "enterprise.css").read_text(encoding="utf-8")
        self.assertIn('<link rel="stylesheet" href="/enterprise.css">', index)
        self.assertIn('<title>LiveCut Production OS</title>', index)
        self.assertIn('class="skip-link" href="#app"', index)
        self.assertIn("aria-current','page'", app)
        self.assertIn("LIVE PRODUCTION CONTROL", app)
        self.assertIn("@media (max-width: 720px)", theme)
        self.assertIn("@media (prefers-reduced-motion: reduce)", theme)
        self.assertNotIn("color-mix(", theme)

    def test_candidate_scoring_keeps_personality_story_reaction_and_visual_moments(self):
        self.assertEqual(category("我觉得穿衣服最重要的是舒服"), "personality")
        self.assertEqual(category("刚开始我也没想到后来大家都来问我"), "story")
        self.assertEqual(category("哇你们看到了吗真的绝了"), "reaction")
        self.assertEqual(category("转一圈给你们看背面"), "visual")
        greeting = {"start": 0, "end": 2, "text": "姐妹们能听到吗"}
        opinion = {"start": 0, "end": 2, "text": "我觉得这个腰线才是关键。"}
        self.assertGreater(quality(opinion), quality(greeting))

    def test_same_source_creates_separate_edits_under_one_source_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "直播素材.mp4"
            source.write_bytes(b"same-source")
            app = Application(root)
            with patch.object(app.runner, "resolve_ai_selection",
                              return_value=("workbuddy", "auto")), \
                    patch.object(app.runner, "resolve_visual_ai_selection",
                                 return_value=("workbuddy", "vision")), \
                    patch.object(app.runner, "enqueue"):
                first = app.create_job({"source_path": str(source), "title": "成片一",
                                        "products": ["上衣"]})
                second = app.create_job({"source_path": str(source), "title": "成片二",
                                         "products": ["上衣"]})
            first_workspace = Path(first["workspace"])
            second_workspace = Path(second["workspace"])
            self.assertNotEqual(first_workspace, second_workspace)
            self.assertEqual(first_workspace.parent, second_workspace.parent)
            self.assertEqual(first_workspace.parent.name, "edits")
            source_workspace = first_workspace.parent.parent
            self.assertTrue((source_workspace / "source.json").is_file())
            self.assertTrue((source_workspace / "shared" / "indexes").is_dir())
            self.assertIn("成片一", first_workspace.name)
            self.assertIn("成片二", second_workspace.name)

    def test_shared_index_is_stable_per_source_and_subtitle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            source_workspace = root / "workspaces" / "sources" / "source-key"
            workspace_a = source_workspace / "edits" / "edit-a"
            workspace_b = source_workspace / "edits" / "edit-b"
            workspace_a.mkdir(parents=True)
            workspace_b.mkdir(parents=True)
            (source_workspace / "source.json").write_text("{}", encoding="utf-8")
            subtitle_a = root / "a.srt"
            subtitle_b = root / "b.srt"
            subtitle_a.write_text("a", encoding="utf-8")
            subtitle_b.write_text("b", encoding="utf-8")
            first = JobRunner._shared_index_dir(
                {"subtitle_path": str(subtitle_a)}, source, workspace_a)
            second = JobRunner._shared_index_dir(
                {"subtitle_path": str(subtitle_a)}, source, workspace_b)
            different = JobRunner._shared_index_dir(
                {"subtitle_path": str(subtitle_b)}, source, workspace_b)
            self.assertEqual(first, second)
            self.assertNotEqual(first, different)
            self.assertEqual(first.parent, source_workspace / "shared" / "indexes")

    def test_legacy_workspace_keeps_private_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            self.assertIsNone(JobRunner._shared_index_dir({}, source, root / "job-old"))

    def test_segment_delivery_never_creates_merged_video(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            workspace, engine = root / "job", root / "job" / "engine"
            (engine / "dual_timelines").mkdir(parents=True)
            (engine / "timelines").mkdir()
            rows = [{"audio": {"src": 1, "start": 0, "end": 2, "text": "测试"},
                     "video": [{"src": 1, "start": 0, "end": 2}]}]
            dual = engine / "dual_timelines" / "body.json"
            dual.write_text(json.dumps(rows), encoding="utf-8")
            module = engine / "timelines" / "body.json"
            module.write_text(json.dumps([rows[0]["audio"]]), encoding="utf-8")
            (engine / "video_mapping_report.json").write_text(
                json.dumps({"dual_timelines": {"body": str(dual)}}), encoding="utf-8")
            snapshot = runner._delivery_inputs(source, engine)
            (workspace / "timeline_locked.json").write_text(
                json.dumps({"validated_inputs": snapshot}), encoding="utf-8")
            job_id = store.create_job(title="交付", source_path=str(source), brief="",
                                      mode="fast", workspace=str(workspace),
                                      delivery_mode="segments")
            store.update_stage(job_id, "validation", status="succeeded")

            def fake_render(_job_id, label, command, steps, _progress):
                output = Path(command[3])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"segment")
                steps.append({"name": label, "ok": True})

            with patch.object(runner, "_run_delivery_step", side_effect=fake_render):
                runner._deliver(store.get_job(job_id), source, workspace, engine)
            self.assertFalse((workspace / "deliverables" / "交付.mp4").exists())
            segments = list((workspace / "deliverables").glob("交付-segments-*/001.mp4"))
            self.assertEqual(len(segments), 1)

    def test_subtitle_formats_build_word_boundaries_without_asr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            samples = {
                "a.srt": "1\n00:00:01,000 --> 00:00:03,000\n这件衣服很显瘦。\n",
                "a.vtt": "WEBVTT\n\n00:01.000 --> 00:03.000\n这件衣服很显瘦。\n",
                "a.ass": "[Events]\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,这件衣服很显瘦。\n",
                "a.txt": "[00:01.000 --> 00:03.000] 这件衣服很显瘦。\n",
            }
            for name, content in samples.items():
                path = root / name
                path.write_text(content, encoding="utf-8")
                blocks = parse_subtitle(path)
                self.assertEqual(len(blocks), 1, name)
                words = words_from_subtitles(blocks)
                self.assertTrue(words)
                self.assertTrue(all(word["source"] == "subtitle_block" for word in words))
                expanded = expand(words)
                self.assertTrue(expanded)
                self.assertTrue(all(word["s"] == blocks[0]["start"] and
                                    word["e"] == blocks[0]["end"] for word in expanded))

    def test_subtitle_cache_key_invalidates_when_subtitle_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media, subtitle = root / "video.mp4", root / "a.srt"
            media.write_bytes(b"video")
            subtitle.write_text("old", encoding="utf-8")
            first = cache_key(media, subtitle, None, False)
            subtitle.write_text("new content", encoding="utf-8")
            second = cache_key(media, subtitle, None, False)
            self.assertNotEqual(first, second)

    def test_subtitle_prepare_defers_audio_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media, subtitle, work = root / "video.mp4", root / "a.srt", root / "index"
            media.write_bytes(b"video")
            subtitle.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n这件衣服很显瘦。\n", encoding="utf-8")
            argv = ["prep.py", str(media), "--subtitle", str(subtitle), "--workdir", str(work)]
            with patch("sys.argv", argv), \
                    patch.object(prep, "probe", return_value=("probe", 10)), \
                    patch.object(prep, "extract_audio") as extract:
                prep.main()
            extract.assert_not_called()
            self.assertFalse((work / "audio16k.wav").exists())
            self.assertTrue((work / "words.json").is_file())

    def test_subtitle_alignment_does_not_need_audio_when_boundaries_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing_audio = root / "audio16k.wav"
            words = root / "words.json"
            words.write_text(json.dumps([{"s": 1.0, "e": 3.0, "w": "这件衣服很显瘦",
                                          "boundary_only": True}]), encoding="utf-8")
            sentences = root / "sentences.json"
            sentences.write_text(json.dumps([{"start": 1.0, "end": 3.0,
                                               "text": "这件衣服很显瘦"}]), encoding="utf-8")
            picks = root / "picks.json"
            picks.write_text(json.dumps({"main_product": "上衣", "picks": [{
                "src": 1, "start": 1.0, "end": 3.0, "text": "这件衣服很显瘦",
                "role": "proof", "module": "body"}]}), encoding="utf-8")
            output = root / "timeline.json"
            argv = ["cuts.py", str(missing_audio), str(picks), str(output),
                    "--words", str(words), "--sentences", str(sentences)]
            with patch("sys.argv", argv):
                cuts.main()
            self.assertFalse(missing_audio.exists())
            self.assertTrue(json.loads(output.read_text(encoding="utf-8")))

    def test_shared_policy_soft_duration_near_duplicate_fabric_and_demo(self):
        rows = [
            {"start": 0, "end": 2, "text": "羊毛面料非常舒服", "role": "material"},
            {"start": 3, "end": 5, "text": "这个羊毛材质非常舒服", "role": "material"},
            {"start": 6, "end": 8, "text": "上身展示", "role": "demo"},
            {"start": 9, "end": 11, "text": "背面展示", "role": "demo"},
        ]
        codes = {item["code"] for item in shared_issues(rows)}
        self.assertIn("duplicate_text", codes)
        self.assertIn("too_many_material_segments", codes)
        self.assertNotIn("too_many_long_demos", codes)

    def test_incremental_merge_does_not_reselect_existing_candidate(self):
        base = {"main_product": "上衣", "picks": [
            {"_candidate_id": 1, "role": "hook"}, {"_candidate_id": 3, "role": "close"}]}
        addition = {"main_product": "上衣", "picks": [
            {"_candidate_id": 1, "role": "proof"}, {"_candidate_id": 2, "role": "fit"}]}
        merged = JobRunner._merge_incremental_plan(base, addition)
        self.assertEqual([item["_candidate_id"] for item in merged["picks"]], [1, 2, 3])

    def test_incremental_merge_can_remove_and_insert_at_position(self):
        base = {"main_product": "套装", "picks": [
            {"_candidate_id": 1, "role": "hook"},
            {"_candidate_id": 2, "role": "material"},
            {"_candidate_id": 3, "role": "material"},
            {"_candidate_id": 4, "role": "close"}]}
        patch = {"main_product": "套装", "remove_candidate_ids": [3], "picks": [
            {"_candidate_id": 5, "role": "proof", "_insert_after_candidate_id": 1}]}
        merged = JobRunner._merge_incremental_plan(base, patch)
        self.assertEqual([item["_candidate_id"] for item in merged["picks"]], [1, 5, 2, 4])

    def test_model_chain_has_hard_two_provider_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = JobRunner(Store(root / "db.sqlite"), root)
            chain = runner._text_model_chain({"model_provider": "workbuddy", "model_name": "auto"})
            self.assertLessEqual(len(chain), 2)

    def test_refinement_exhaustion_does_not_switch_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            workspace, engine = root / "job", root / "job" / "engine"
            engine.mkdir(parents=True)
            job_id = store.create_job(title="局部修复", source_path="/tmp/source.mp4",
                                      brief="", mode="fast", workspace=str(workspace))
            provider = Mock(display_name="Mock")
            with patch.object(runner, "_text_model_chain", return_value=[
                    ("workbuddy", "auto"), ("codex", "auto")]), \
                    patch.object(runner, "_provider", return_value=provider) as lookup, \
                    patch.object(runner, "_run_ai_plan_attempt",
                                 side_effect=PlanRefinementError("still invalid")) as attempt:
                runner._run_ai_plan(store.get_job(job_id), engine)
            self.assertEqual(lookup.call_count, 1)
            self.assertEqual(attempt.call_count, 1)
            self.assertEqual(store.get_job(job_id)["status"], "failed")

    def test_failed_model_usage_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            (engine / "candidate_digest.json").write_text("[]", encoding="utf-8")
            job_id = store.create_job(title="usage", source_path="/tmp/source.mp4", brief="",
                                      mode="fast", workspace=str(engine.parent))
            provider = Mock(display_name="Mock")
            provider.generate_plan.side_effect = ProviderResponseError(
                "bad", {"usage": {"input_tokens": 123, "output_tokens": 7}})
            with self.assertRaises(ProviderResponseError):
                runner._run_ai_plan_attempt(store.get_job(job_id), engine,
                                            provider, "workbuddy", "auto")
            job = store.get_job(job_id)
            self.assertEqual(job["token_input"], 123)
            self.assertEqual(job["token_output"], 7)

    def test_returned_usage_is_counted_when_candidate_hydration_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            (engine / "candidate_digest.json").write_text(json.dumps([
                {"i": 1, "s": 0, "e": 2, "c": "other", "t": "有效候选"}
            ]), encoding="utf-8")
            job_id = store.create_job(title="usage", source_path="/tmp/source.mp4", brief="",
                                      mode="fast", workspace=str(engine.parent))
            provider = Mock(display_name="Mock")
            invalid = {"main_product": "上衣", "picks": [{
                "candidate_id": 999, "role": "hook", "module": "hook_A",
                "product": "上衣", "color": ""}]}
            provider.generate_plan.return_value = {
                "plan": invalid, "raw": {"result": invalid}, "seconds": 1,
                "usage": {"input_tokens": 123, "output_tokens": 7}}
            with self.assertRaises(ValueError):
                runner._run_ai_plan_attempt(store.get_job(job_id), engine,
                                            provider, "workbuddy", "auto")
            job = store.get_job(job_id)
            self.assertEqual(job["token_input"], 123)
            self.assertEqual(job["token_output"], 7)

    def test_refinement_execution_error_is_locked_to_successful_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            candidates = [{"i": index, "s": index * 3.0, "e": index * 3.0 + 2.0,
                           "c": "other", "t": f"候选{index}"} for index in range(4)]
            (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
            job_id = store.create_job(title="refine", source_path="/tmp/source.mp4", brief="",
                                      mode="fast", workspace=str(engine.parent), products=["上衣"])
            initial = {"main_product": "上衣", "picks": [
                {"candidate_id": index, "role": role,
                 "module": "hook_A" if index == 0 else "body",
                 "product": "上衣", "color": ""}
                for index, role in enumerate(("hook", "proof", "scene", "close"))]}
            provider = Mock(display_name="Mock")
            provider.generate_plan.side_effect = [
                {"plan": initial, "raw": {"result": initial}, "seconds": 1,
                 "usage": {"input_tokens": 10, "output_tokens": 2}},
                ProviderResponseError("bad patch", {
                    "usage": {"input_tokens": 20, "output_tokens": 3}}),
            ]
            with patch.object(runner, "_plan_preflight_issues",
                              return_value=[{"code": "needs_patch", "level": "error"}]), \
                    self.assertRaises(PlanRefinementError):
                runner._run_ai_plan_attempt(store.get_job(job_id), engine,
                                            provider, "workbuddy", "auto")
            job = store.get_job(job_id)
            self.assertEqual(job["token_input"], 30)
            self.assertEqual(job["token_output"], 5)

    def test_incremental_planning_stops_after_two_patches_and_does_not_resend_used_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            workspace, engine = root / "job", root / "job" / "engine"
            engine.mkdir(parents=True)
            candidates = [{"i": index, "s": index * 5.0, "e": index * 5.0 + 2.0,
                           "c": "other", "t": f"候选原文{index}"}
                          for index in range(20)]
            (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
            job_id = store.create_job(title="增量", source_path="/tmp/source.mp4", brief="",
                                      mode="fast", workspace=str(workspace), products=["上衣"])

            def response(ids, roles):
                plan = {"main_product": "上衣", "picks": [
                    {"candidate_id": candidate_id, "role": role,
                     "module": "hook_A" if role == "hook" else "body",
                     "product": "上衣", "color": ""}
                    for candidate_id, role in zip(ids, roles)]}
                return {"plan": plan, "raw": {"result": plan}, "seconds": 1,
                        "usage": {"input_tokens": 10, "output_tokens": 2}}

            provider = Mock(display_name="Mock")
            provider.generate_plan.side_effect = [
                response([0, 1, 2, 3], ["hook", "proof", "styling", "close"]),
                response([4], ["bridge"]), response([5], ["bridge"])]
            with patch.object(runner, "enqueue"):
                runner._run_ai_plan_attempt(store.get_job(job_id), engine, provider, "workbuddy", "auto")
            self.assertEqual(provider.generate_plan.call_count, 3)
            second_prompt = provider.generate_plan.call_args_list[1].kwargs["prompt"]
            third_prompt = provider.generate_plan.call_args_list[2].kwargs["prompt"]
            self.assertNotIn("候选原文0", second_prompt)
            self.assertNotIn("候选原文4", third_prompt)
            self.assertIs(provider.generate_plan.call_args_list[1].kwargs["schema"],
                          PLAN_PATCH_SCHEMA)
            self.assertIn("remove_candidate_ids", PLAN_PATCH_SCHEMA["required"])
            self.assertEqual(store.get_job(job_id)["token_input"], 30)

    def test_visual_candidate_set_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timeline = root / "body.json"
            timeline.write_text(json.dumps([{"audio": {"src": 1, "start": 0, "end": 2},
                                              "video": [{"src": 1, "start": 0, "end": 2}]}]))
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"dual_timelines": {"body": str(timeline)}}))
            packet = root / "packet.json"
            packet.write_text(json.dumps({"duration": 20, "replacement_blocks": [
                {"block_id": "body:0", "duration": 2}], "candidate_sets": {"body:0": ["C1"]},
                "candidates": [{"candidate_id": "C1", "start": 4, "end": 6},
                               {"candidate_id": "C2", "start": 8, "end": 10}]}))
            decisions = root / "decisions.json"
            decisions.write_text(json.dumps({"replacements": [
                {"block_id": "body:0", "candidate_id": "C2"}]}))
            report = apply_visual_mix(packet, decisions, mapping, root / "report.json")
            self.assertEqual(report["replaced"], 0)

    def test_visual_candidates_prioritize_product_then_color(self):
        block = {"start": 100, "end": 102, "product": "上衣", "color": "白色"}
        candidates = [
            {"candidate_id": "near-wrong", "start": 101, "end": 103,
             "product": "裤子", "color": "黑色"},
            {"candidate_id": "same-product", "start": 60, "end": 62,
             "product": "上衣", "color": "黑色"},
            {"candidate_id": "exact", "start": 20, "end": 22,
             "product": "上衣", "color": "白色"},
        ]
        ranked = ranked_candidates(block, candidates)
        self.assertEqual([item["candidate_id"] for item in ranked], ["exact"])

    def test_visual_prepare_exits_before_candidate_search_when_selected_clips_are_good(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            timeline = root / "body.json"
            timeline.write_text(json.dumps([{
                "audio": {"src": 1, "start": 10, "end": 12, "text": "显瘦"},
                "video": [{"src": 1, "start": 10, "end": 12}],
            }]), encoding="utf-8")
            mapping = root / "mapping.json"
            mapping.write_text(json.dumps({"dual_timelines": {"body": str(timeline)}}),
                               encoding="utf-8")
            good = ({"bad": False, "reasons": [], "brightness": 80,
                     "contrast": 20, "sharpness": 8}, 0.5, root / "frame.jpg")
            with patch("agent_video.engine.scripts.visual_mix.media_duration", return_value=982), \
                    patch("agent_video.engine.scripts.visual_mix.inspect_segment",
                          return_value=good) as inspect, \
                    patch("agent_video.engine.scripts.visual_mix.build_overview"), \
                    patch("agent_video.engine.scripts.visual_mix.scene_boundaries") as scenes:
                packet = prepare_visual_mix(source, mapping, root / "visual", 10, 24)
            self.assertEqual(packet["search_strategy"], "selected_only_early_exit")
            self.assertEqual(packet["candidates"], [])
            self.assertEqual(inspect.call_count, 1)
            scenes.assert_not_called()

    def test_feedback_rules_are_injected_only_into_matching_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            runner.directives.add_feedback(problem_type="framing", task="edit",
                                           expected_change="视觉只用全身画面",
                                           scope="vision")
            runner.directives.add_feedback(problem_type="codec", task="edit",
                                           expected_change="渲染质量优先",
                                           scope="render")
            workspace = root / "job"
            workspace.mkdir()
            (workspace / "revision_request.json").write_text(json.dumps({
                "feedback": "只在视觉阶段使用这条意见", "scope": "vision"}),
                encoding="utf-8")
            job = {"title": "测试", "mode": "fast", "brief": "", "workspace": str(workspace)}
            candidate = [{"i": 1, "s": 0, "e": 2, "c": "other", "t": "上身显瘦"}]
            text_prompt = runner._plan_prompt(job, candidate)
            vision_prompt = runner._visual_mix_prompt(job, {
                "replacement_blocks": [], "candidates": []})
            self.assertNotIn("视觉只用全身画面", text_prompt)
            self.assertNotIn("渲染质量优先", text_prompt)
            self.assertIn("视觉只用全身画面", vision_prompt)
            self.assertIn("只在视觉阶段使用这条意见", vision_prompt)
            self.assertNotIn("渲染质量优先", vision_prompt)
            self.assertIn("禁止异时配音", vision_prompt)
            self.assertIn("嘴部不可见", vision_prompt)

    def test_render_defaults_to_hardware_fast_but_honors_quality_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = JobRunner(Store(root / "db.sqlite"), root)
            with patch.object(runner, "_ffmpeg_has_encoder", return_value=True):
                fast = runner._render_options({})
            self.assertEqual(fast["video_codec"], "h264_videotoolbox")
            runner.directives.add_feedback(problem_type="quality", task="edit",
                                           expected_change="渲染质量优先",
                                           scope="render")
            with patch.object(runner, "_ffmpeg_has_encoder", return_value=True):
                quality = runner._render_options({})
            self.assertEqual(quality["video_codec"], "libx264")
            self.assertEqual(quality["preset"], "slow")

    def test_visual_labels_follow_full_transcript_product_color_periods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / "engine"
            (engine / "index").mkdir(parents=True)
            (engine / "index" / "sentences.json").write_text(json.dumps([
                {"start": 0, "end": 2, "text": "先看这件上衣"},
                {"start": 10, "end": 12, "text": "这个白色很清爽"},
                {"start": 40, "end": 42, "text": "接下来是阔腿裤"},
                {"start": 50, "end": 52, "text": "黑色更显瘦"},
            ]), encoding="utf-8")
            anchors = JobRunner._visual_label_anchors(
                {"products": ["上衣", "阔腿裤"], "colors": ["白色", "黑色"]}, engine)
            self.assertEqual([(item["product"], item["color"]) for item in anchors], [
                ("上衣", ""), ("上衣", "白色"), ("阔腿裤", ""), ("阔腿裤", "黑色")])

    def test_rules_dedupe_conflict_disable_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = DirectivesManager(Path(directory))
            first = manager.add_feedback(problem_type="pace", task="edit",
                                         expected_change="禁止快切", scope="text")
            manager.add_feedback(problem_type="pace", task="edit",
                                 expected_change="应该快切", scope="text")
            data = manager.load()
            self.assertEqual(len(data["rules"]), 2)
            self.assertFalse(data["rules"][1]["enabled"])
            manager.set_enabled(data["rules"][0]["id"], False)
            rolled = manager.rollback(first["version"])
            self.assertTrue(rolled["rules"][0]["enabled"])


if __name__ == "__main__":
    unittest.main()
