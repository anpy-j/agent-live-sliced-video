import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.db import Store
from agent_video.ai import PLAN_PATCH_SCHEMA, ProviderResponseError
from agent_video.engine.scripts.cuts import expand
from agent_video.engine.scripts.digest_candidates import category, quality
from agent_video.engine.scripts.global_quality import review_copy
from agent_video.engine.scripts.prep import (cache_key, parse_subtitle,
                                             merge_blocks, usable,
                                             transcript_cache_key,
                                             words_from_subtitles)
from agent_video.engine.scripts import cuts, prep
from agent_video.engine.scripts.visual_mix import (apply as apply_visual_mix,
                                                   prepare as prepare_visual_mix,
                                                   ranked_candidates)
from agent_video.engine.validation_policy import shared_issues
from agent_video.rules import DirectivesManager
from agent_video.runner import (FALLBACK_MODELS, JobRunner,
                                MAX_TEXT_MODEL_ATTEMPTS, PlanRefinementError,
                                SEMANTIC_AUDIT_BATCH_SIZE,
                                SEMANTIC_AUDIT_RETRY_LIMIT)
from agent_video.server import Application


def semantic_audit_response(candidates, main_product="上衣", reject_ids=()):
    reject_ids = set(reject_ids)
    decisions = [{
        "candidate_id": int(item["i"]),
        "verdict": "reject" if int(item["i"]) in reject_ids else "keep",
        "standalone": int(item["i"]) not in reject_ids,
        "main_product_relevant": int(item["i"]) not in reject_ids,
        "content_type": "stage_chatter" if int(item["i"]) in reject_ids else "selling_point",
        "selling_value": 0 if int(item["i"]) in reject_ids else 80,
        "reason": "测试审核",
    } for item in candidates]
    plan = {"main_product": main_product, "picks": decisions}
    return {"plan": plan, "raw": {"result": plan}, "seconds": 0.5,
            "usage": {"input_tokens": 0, "output_tokens": 0}}


class PipelineFeatureTest(unittest.TestCase):
    def test_generic_color_never_creates_a_cross_back_jump(self):
        rows = [
            {"text": "这件白色很显气质。", "product": "T恤", "color": "白色"},
            {"text": "通用色谁穿都合适。", "product": "T恤", "color": "通用"},
            {"text": "白色搭牛仔裤很好看。", "product": "T恤", "color": "白色"},
        ]
        codes = {item["code"] for item in review_copy(rows)["issues"]}
        self.assertNotIn("color_jump", codes)

    def test_generic_color_still_flags_a_real_cross_back_jump(self):
        rows = [
            {"text": "这件白色很显气质。", "product": "T恤", "color": "白色"},
            {"text": "黑色更耐脏一些。", "product": "T恤", "color": "黑色"},
            {"text": "通用色谁穿都合适。", "product": "T恤", "color": "通用"},
            {"text": "白色搭牛仔裤很好看。", "product": "T恤", "color": "白色"},
        ]
        codes = {item["code"] for item in review_copy(rows)["issues"]}
        self.assertIn("color_jump", codes)

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

    def test_transcript_cache_key_tracks_glossary_changes(self):
        # glossary 的术语纠错直接改写转写文本：改词表必须让转写缓存失效，
        # 否则旧转写（裸口/长蹄）会被静默复用，成片字幕照旧。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "video.mp4"
            media.write_bytes(b"video")
            asr = {"backend": "mlx", "model": "m"}
            before = transcript_cache_key(media, None, asr, False)
            with patch.object(prep, "fingerprint",
                              side_effect=lambda path: ("changed" if "glossary" in str(path)
                                                        else "same")):
                after = transcript_cache_key(media, None, asr, False)
            self.assertNotEqual(before["vocab"], after["vocab"])

    def test_cache_complete_rejects_index_built_from_stale_transcript(self):
        # 索引目录依赖清单和文件都在，但里面的转写是旧词表/旧代码产出的：必须判为
        # 未命中，否则旧转写（裸口/长蹄）会被追认后长期复用。
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            media = work / "video.mp4"
            media.write_bytes(b"video")
            key = cache_key(media, None, {"backend": "mlx", "model": "m"}, False)
            for name in ("probe.txt", "sentences.json", "candidates.json", "words.json"):
                (work / name).write_text("[]", encoding="utf-8")
            (work / "audio16k.wav").write_bytes(b"wav")
            (work / "audio16k.wav.manifest.json").write_text(
                json.dumps({"key": {"media": key["media"]}}), encoding="utf-8")
            stale = {name: value for name, value in key.items() if name != "version"}
            stale.pop("vocab")
            (work / "transcript_manifest.json").write_text(
                json.dumps({"key": dict(stale, version=4)}), encoding="utf-8")
            (work / "cache_manifest.json").write_text(
                json.dumps({"key": key}), encoding="utf-8")
            self.assertFalse(prep.cache_complete(work, key))
            (work / "transcript_manifest.json").write_text(
                json.dumps({"key": dict(stale, vocab=key["vocab"], version=5)}),
                encoding="utf-8")
            self.assertTrue(prep.cache_complete(work, key))

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
        material = next(item for item in shared_issues(rows)
                        if item["code"] == "too_many_material_segments")
        self.assertEqual(material["level"], "error")

    def test_subtitle_merge_extends_past_soft_limit_for_dependent_continuation(self):
        blocks = [
            {"start": 0.0, "end": 1.0, "text": "带一点点腰身的"},
            {"start": 1.0, "end": 2.0, "text": "肚子特别大的"},
            {"start": 2.0, "end": 3.0, "text": "它会给你很好的腰部"},
            {"start": 3.0, "end": 4.0, "text": "的一个线条的修饰。"},
        ]
        merged = merge_blocks(blocks)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"],
                         "带一点点腰身的肚子特别大的它会给你很好的腰部的一个线条的修饰。")

    def test_candidate_filter_rejects_production_chatter(self):
        self.assertFalse(usable({"start": 0, "end": 3,
                                 "text": "删掉来整个先删掉好"}))
        self.assertFalse(usable({"start": 0, "end": 3,
                                 "text": "轻柔羊毛手手"}))
        self.assertTrue(usable({"start": 0, "end": 3,
                                "text": "这件毛衣上身显瘦又利落"}))

    def test_shared_policy_tolerates_alignment_margin_as_warning(self):
        pre_alignment = shared_issues([
            {"start": 0, "end": 5.0, "text": "完整卖点", "role": "proof"}],
            pre_alignment=True)
        self.assertEqual(pre_alignment[0]["code"], "segment_too_long")
        self.assertEqual(pre_alignment[0]["level"], "warning")

        final = shared_issues([
            {"start": 0, "end": 5.3, "text": "完整卖点", "role": "proof"}])
        self.assertEqual(final[0]["code"], "segment_too_long")
        self.assertEqual(final[0]["level"], "warning")

    def test_long_complete_utterance_is_capped_at_eight_seconds(self):
        tolerable = shared_issues([
            {"start": 0, "end": 7.0, "text": "不可切分的长句", "role": "proof",
             "long_complete_utterance": True}])
        self.assertNotIn("segment_too_long", {item["code"] for item in tolerable})
        blocked = shared_issues([
            {"start": 0, "end": 12.2, "text": "被合并出来的长段", "role": "proof",
             "long_complete_utterance": True}])
        self.assertEqual(blocked[0]["code"], "segment_too_long")
        self.assertEqual(blocked[0]["level"], "error")

    def test_segment_floor_is_a_blocking_issue(self):
        plan = {"main_product": "上衣", "picks": [
            {"start": index * 12.0, "end": index * 12.0 + 12.0, "text": f"长段{index}",
             "role": "proof", "module": "body"}
            for index in range(10)]}
        issues = JobRunner._plan_preflight_issues(plan, {
            "min_total": 70, "max_total": 120, "min_segments": 18, "max_segments": 32})
        floor = [item for item in issues if item["code"] == "too_few_segments"]
        self.assertTrue(floor)
        self.assertEqual(floor[0]["level"], "error")
        self.assertIn("segment_too_long", {item["code"] for item in issues})

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

    def test_model_chain_is_bounded_and_never_uses_auto_for_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = JobRunner(Store(root / "db.sqlite"), root)

            def provider(provider_id):
                item = Mock()
                item.info.return_value = {"available": True}
                return item

            with patch.object(runner, "_provider", side_effect=provider):
                chain = runner._text_model_chain(
                    {"model_provider": "workbuddy", "model_name": "auto"})
            self.assertLessEqual(len(chain), MAX_TEXT_MODEL_ATTEMPTS)
            # 首选模型保持用户显式选择，备用 provider 一律落到具体模型，不再用 auto。
            self.assertEqual(chain[0], ("workbuddy", "auto"))
            fallbacks = chain[1:]
            self.assertTrue(fallbacks)
            self.assertTrue(all(model != "auto" for _pid, model in fallbacks))
            for provider_id, model in fallbacks:
                self.assertIn(model, FALLBACK_MODELS[provider_id])

    def test_model_chain_skips_unavailable_provider_before_applying_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = JobRunner(Store(root / "db.sqlite"), root)
            availability = {
                "opencode": False,
                "workbuddy": True,
                "codex": True,
                "antigravity": True,
            }

            def provider(provider_id):
                item = Mock()
                item.info.return_value = {"available": availability.get(provider_id, False)}
                return item

            with patch.object(runner, "_provider", side_effect=provider):
                chain = runner._text_model_chain(
                    {"model_provider": "opencode", "model_name": "auto"})
            self.assertEqual([provider_id for provider_id, _ in chain],
                             ["workbuddy", "codex", "antigravity"])
            self.assertLessEqual(len(chain), MAX_TEXT_MODEL_ATTEMPTS)
            self.assertTrue(all(model != "auto" for _pid, model in chain))

    def test_opencode_fallback_uses_concrete_model_instead_of_auto(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = JobRunner(Store(root / "db.sqlite"), root)
            availability = {"workbuddy": True, "opencode": True}
            supported = {
                "workbuddy": ["auto", "glm-5.1"],
                "opencode": ["auto", "opencode-go/gpt-5.6-luna", "openai/gpt-5.6-sol"],
            }

            def provider(provider_id):
                item = Mock()
                models = supported.get(provider_id, [])
                item.info.return_value = {"available": availability.get(provider_id, False)}
                item.models.return_value = [(model, model) for model in models]

                def validate(value, allowed=models):
                    if value not in allowed:
                        raise ValueError(f"unsupported: {value}")

                item.validate_model.side_effect = validate
                return item

            with patch.object(runner, "_provider", side_effect=provider):
                chain = runner._text_model_chain(
                    {"model_provider": "workbuddy", "model_name": "auto"})
            self.assertEqual(chain[0], ("workbuddy", "auto"))
            self.assertIn(("opencode", "opencode-go/gpt-5.6-luna"), chain)

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
            self.assertEqual(store.get_job(job_id)["status"], "blocked")
            self.assertFalse((engine / "picks.json").is_file())
            self.assertTrue((engine / "picks.local-draft.json").is_file())

    def test_failed_model_usage_is_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            (engine / "candidate_digest.json").write_text(
                json.dumps([{"i": 0, "s": 0, "e": 2, "c": "other", "t": "候选一句"}]),
                encoding="utf-8")
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
            candidates = [
                {"i": 1, "s": 0, "e": 2, "c": "other", "t": "有效候选一"},
                {"i": 2, "s": 3, "e": 5, "c": "other", "t": "有效候选二"},
            ]
            (engine / "candidate_digest.json").write_text(
                json.dumps(candidates), encoding="utf-8")
            job_id = store.create_job(title="usage", source_path="/tmp/source.mp4", brief="",
                                      mode="fast", workspace=str(engine.parent))
            provider = Mock(display_name="Mock")
            invalid = {"main_product": "上衣", "picks": [{
                "candidate_id": 999, "role": "hook", "module": "hook_A",
                "product": "上衣", "color": ""}]}
            invalid_response = {
                "plan": invalid, "raw": {"result": invalid}, "seconds": 1,
                "usage": {"input_tokens": 123, "output_tokens": 7}}
            provider.generate_plan.side_effect = [
                semantic_audit_response(candidates), invalid_response]
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
                semantic_audit_response(candidates),
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
                semantic_audit_response(candidates),
                response([0, 1, 2, 3], ["hook", "proof", "styling", "close"]),
                response([4, 5], ["bridge", "bridge"]), response([6, 7], ["bridge", "bridge"])]
            with patch.object(runner, "enqueue"):
                runner._run_ai_plan_attempt(store.get_job(job_id), engine, provider, "workbuddy", "auto")
            self.assertEqual(provider.generate_plan.call_count, 4)
            second_prompt = provider.generate_plan.call_args_list[2].kwargs["prompt"]
            third_prompt = provider.generate_plan.call_args_list[3].kwargs["prompt"]
            self.assertNotIn("候选原文0", second_prompt)
            self.assertNotIn("候选原文4", third_prompt)
            self.assertIs(provider.generate_plan.call_args_list[2].kwargs["schema"],
                          PLAN_PATCH_SCHEMA)
            self.assertIn("remove_candidate_ids", PLAN_PATCH_SCHEMA["required"])
            self.assertEqual(store.get_job(job_id)["token_input"], 30)

    def test_invalid_refinement_round_keeps_last_valid_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            workspace, engine = root / "job", root / "job" / "engine"
            engine.mkdir(parents=True)
            candidates = [
                {"i": 0, "s": 0, "e": 3, "c": "other", "t": "候选零",
                 "atom_id": "a0"},
                {"i": 4, "s": 4, "e": 7, "c": "other", "t": "候选四",
                 "atom_id": "a4"},
                {"i": 1, "s": 8, "e": 11, "c": "other", "t": "候选一",
                 "atom_id": "a1", "requires_previous": True,
                 "required_atom_ids": ["a4"]},
                {"i": 2, "s": 12, "e": 15, "c": "other", "t": "候选二",
                 "atom_id": "a2"},
                {"i": 3, "s": 16, "e": 19, "c": "other", "t": "候选三",
                 "atom_id": "a3"},
                {"i": 5, "s": 20, "e": 23, "c": "other", "t": "候选五",
                 "atom_id": "a5"},
                {"i": 6, "s": 24, "e": 27, "c": "other", "t": "候选六",
                 "atom_id": "a6"},
            ]
            (engine / "candidate_digest.json").write_text(
                json.dumps(candidates), encoding="utf-8")
            job_id = store.create_job(title="回退", source_path="/tmp/source.mp4",
                                      brief="", mode="fast", workspace=str(workspace),
                                      products=["上衣"])
            initial = {"main_product": "上衣", "picks": [
                {"candidate_id": 0, "role": "hook", "module": "hook_A",
                 "product": "上衣", "color": ""},
                {"candidate_id": 4, "role": "scene", "module": "body",
                 "product": "上衣", "color": ""},
                {"candidate_id": 1, "role": "proof", "module": "body",
                 "product": "上衣", "color": ""},
                {"candidate_id": 2, "role": "styling", "module": "body",
                 "product": "上衣", "color": ""},
                {"candidate_id": 3, "role": "close", "module": "body",
                 "product": "上衣", "color": ""}]}
            round_one = {"main_product": "上衣", "remove_candidate_ids": [],
                         "picks": []}
            round_two = {"main_product": "上衣", "remove_candidate_ids": [4],
                         "picks": []}
            provider = Mock(display_name="Mock")
            provider.generate_plan.side_effect = [
                semantic_audit_response(candidates),
                {"plan": initial, "raw": {"result": initial}, "seconds": 1,
                 "usage": {"input_tokens": 10, "output_tokens": 2}},
                {"plan": round_one, "raw": {"result": round_one}, "seconds": 1,
                 "usage": {"input_tokens": 10, "output_tokens": 2}},
                {"plan": round_two, "raw": {"result": round_two}, "seconds": 1,
                 "usage": {"input_tokens": 10, "output_tokens": 2}},
            ]
            preflight = [
                [{"code": "needs_patch", "level": "error"}],
                [{"code": "soft_duration_short", "level": "warning"}],
            ]
            with patch.object(runner, "_plan_preflight_issues",
                              side_effect=preflight), \
                    patch.object(runner, "enqueue"):
                runner._run_ai_plan_attempt(store.get_job(job_id), engine,
                                            provider, "workbuddy", "auto")
            self.assertEqual(provider.generate_plan.call_count, 4)
            written = json.loads((engine / "picks.json").read_text(encoding="utf-8"))
            selected = [pick.get("_candidate_id") for pick in written["picks"]]
            self.assertIn(4, selected)
            self.assertEqual(selected.index(1), selected.index(4) + 1)
            review = json.loads((engine / "global_plan_review.json").read_text(
                encoding="utf-8"))
            self.assertEqual(review["variants"][0]["pick_count"], len(written["picks"]))

    @staticmethod
    def _audit_decision(candidate_id):
        return {"candidate_id": candidate_id, "verdict": "keep", "standalone": True,
                "subject_explicit": True, "referent": "", "requires_previous": False,
                "requires_next": False, "opening_suitability": 60, "information_gain": 60,
                "content_function": "benefit", "main_product_relevant": True,
                "content_type": "selling_point", "selling_value": 80, "reason": "测试"}

    def test_semantic_audit_retries_uncovered_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            candidates = [{"i": index, "s": index * 3.0, "e": index * 3.0 + 2.0,
                           "c": "other", "t": f"候选{index}"} for index in range(3)]
            job_id = store.create_job(title="审核", source_path="/tmp/source.mp4",
                                      brief="", mode="fast",
                                      workspace=str(engine.parent), products=["上衣"])
            first = {"main_product": "上衣",
                     "picks": [self._audit_decision(0), self._audit_decision(1)]}
            second = {"main_product": "上衣", "picks": [self._audit_decision(2)]}
            provider = Mock(display_name="Mock")
            provider.generate_plan.side_effect = [
                {"plan": first, "raw": {"result": first}, "seconds": 1,
                 "usage": {"input_tokens": 5, "output_tokens": 1}},
                {"plan": second, "raw": {"result": second}, "seconds": 1,
                 "usage": {"input_tokens": 5, "output_tokens": 1}},
            ]
            runner._semantic_review_candidates(
                store.get_job(job_id), engine, provider, "workbuddy", "auto", candidates)
            self.assertEqual(provider.generate_plan.call_count, 2)
            report = json.loads((engine / "semantic_audit.json").read_text(encoding="utf-8"))
            audited = sorted(item["candidate_id"] for item in report["decisions"])
            self.assertEqual(audited, [0, 1, 2])

    def test_semantic_audit_missing_candidates_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            candidates = [{"i": index, "s": index * 3.0, "e": index * 3.0 + 2.0,
                           "c": "other", "t": f"候选{index}"} for index in range(5)]
            job_id = store.create_job(title="审核", source_path="/tmp/source.mp4",
                                      brief="", mode="fast",
                                      workspace=str(engine.parent), products=["上衣"])
            partial = {"main_product": "上衣", "picks": [self._audit_decision(0)]}
            provider = Mock(display_name="Mock")
            provider.generate_plan.side_effect = [
                {"plan": partial, "raw": {"result": partial}, "seconds": 1,
                 "usage": {"input_tokens": 5, "output_tokens": 1}}
                for _ in range(SEMANTIC_AUDIT_RETRY_LIMIT + 1)]
            runner._semantic_review_candidates(
                store.get_job(job_id), engine, provider, "workbuddy", "auto", candidates)
            self.assertEqual(provider.generate_plan.call_count,
                             SEMANTIC_AUDIT_RETRY_LIMIT + 1)
            report = json.loads((engine / "semantic_audit.json").read_text(encoding="utf-8"))
            verdicts = {item["candidate_id"]: item["verdict"]
                        for item in report["decisions"]}
            for candidate_id in (1, 2, 3, 4):
                self.assertEqual(verdicts[candidate_id], "reject")

    def test_semantic_audit_resumes_from_completed_batch_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "db.sqlite")
            runner = JobRunner(store, root)
            engine = root / "job" / "engine"
            engine.mkdir(parents=True)
            candidates = [
                {"i": index, "s": index * 3.0, "e": index * 3.0 + 2.0,
                 "c": "other", "t": f"第{index}条独立完整卖点"}
                for index in range(SEMANTIC_AUDIT_BATCH_SIZE + 1)
            ]
            job_id = store.create_job(title="断点审核", source_path="/tmp/source.mp4",
                                      brief="", mode="fast", workspace=str(engine.parent),
                                      products=["上衣"])
            first_batch = {"main_product": "上衣", "picks": [
                self._audit_decision(index) for index in range(SEMANTIC_AUDIT_BATCH_SIZE)
            ]}
            first_provider = Mock(display_name="Mock")
            first_provider.generate_plan.side_effect = [
                {"plan": first_batch, "raw": {"result": first_batch}, "seconds": 1,
                 "usage": {"input_tokens": 5, "output_tokens": 1}},
                RuntimeError("模拟第二批进程中断"),
            ]
            with self.assertRaisesRegex(RuntimeError, "模拟第二批"):
                runner._semantic_review_candidates(
                    store.get_job(job_id), engine, first_provider,
                    "workbuddy", "auto", candidates)
            progress = json.loads(
                (engine / "semantic_audit.progress.json").read_text(encoding="utf-8"))
            self.assertEqual(len(progress["decisions"]), SEMANTIC_AUDIT_BATCH_SIZE)

            final_batch = {"main_product": "上衣", "picks": [
                self._audit_decision(SEMANTIC_AUDIT_BATCH_SIZE)
            ]}
            resumed_provider = Mock(display_name="Mock")
            resumed_provider.generate_plan.return_value = {
                "plan": final_batch, "raw": {"result": final_batch}, "seconds": 1,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            }
            runner._semantic_review_candidates(
                store.get_job(job_id), engine, resumed_provider,
                "workbuddy", "auto", candidates)
            self.assertEqual(resumed_provider.generate_plan.call_count, 1)
            report = json.loads((engine / "semantic_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["decisions"]), len(candidates))
            self.assertFalse((engine / "semantic_audit.progress.json").exists())

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
            self.assertEqual(packet["search_strategy"], "full_selected_visual_audit_nearby_windows")
            self.assertEqual(len(packet["replacement_blocks"]), 1)
            self.assertGreater(len(packet["candidates"]), 0)
            self.assertGreater(inspect.call_count, 1)
            scenes.assert_not_called()

    def test_long_material_defaults_to_selective_short_video_target(self):
        candidates = [{"i": index, "s": index * 4.0, "e": index * 4.0 + 4.0,
                       "c": "proof", "t": f"主商品卖点{index}"}
                      for index in range(75)]
        limits = JobRunner._editing_constraints(candidates, {})
        self.assertEqual((limits["min_total"], limits["max_total"]), (70, 120))
        self.assertEqual(limits["min_segments"], 18)
        self.assertEqual(limits["max_segments"], 32)

    def test_local_repair_removes_secondary_products_and_fills_duration(self):
        candidates = [
            {"i": 0, "s": 0, "e": 4, "c": "hook", "t": "这件针织衫很显气质"},
            {"i": 1, "s": 5, "e": 9, "c": "styling", "t": "里面搭一件小打底"},
            {"i": 2, "s": 10, "e": 14, "c": "proof", "t": "纹理细节很高级"},
            {"i": 3, "s": 15, "e": 19, "c": "fit", "t": "上身不会压个子"},
        ]
        plan = {"main_product": "针织衫", "creative_strategy": "selling", "picks": [
            {"src": 1, "start": row["s"], "end": row["e"], "text": row["t"],
             "role": row["c"], "module": "hook_A" if row["i"] == 0 else "body",
             "product": "针织衫", "color": "", "_candidate_id": row["i"]}
            for row in candidates[:2]
        ]}
        repaired, report = JobRunner._repair_plan_locally(
            plan, candidates, {"min_total": 12, "max_total": 20, "max_segments": 8},
            {"products": ["针织衫"]})
        text = "".join(item["text"] for item in repaired["picks"])
        self.assertNotIn("打底", text)
        self.assertGreaterEqual(report["duration"], 12)
        self.assertEqual(report["removed_secondary_candidate_ids"], [1])

    def test_local_repair_never_uses_garbage_to_reach_duration(self):
        candidates = [
            {"i": 0, "s": 0, "e": 4, "c": "proof", "q": 9,
             "t": "这件毛衣上身显瘦又利落"},
            {"i": 1, "s": 10, "e": 14, "c": "scene", "q": 20,
             "t": "删掉来整个先删掉好"},
            {"i": 2, "s": 20, "e": 24, "c": "material", "q": 9,
             "t": "羊毛面料柔软亲肤"},
            {"i": 3, "s": 30, "e": 34, "c": "material", "q": 9,
             "t": "羊绒材质摸起来很软"},
        ]
        plan = {"main_product": "毛衣", "picks": [{
            "src": 1, "start": 0, "end": 4, "text": candidates[0]["t"],
            "role": "proof", "module": "hook_A", "_candidate_id": 0,
        }]}
        repaired, report = JobRunner._repair_plan_locally(
            plan, candidates,
            {"min_total": 16, "max_total": 20, "min_segments": 4, "max_segments": 8},
            {"products": ["毛衣"]},
        )
        texts = [item["text"] for item in repaired["picks"]]
        self.assertNotIn("删掉来整个先删掉好", texts)
        self.assertEqual(sum("羊毛" in text or "羊绒" in text for text in texts), 1)
        self.assertLess(report["duration"], 16)

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
