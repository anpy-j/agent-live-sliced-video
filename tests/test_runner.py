import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.db import Store
from agent_video.runner import JobCancelled, JobRunner


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "agent.db")
        self.runner = JobRunner(self.store, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_final_video_uses_job_title(self):
        workspace = self.root / "job"
        rendered = workspace / "final-render" / "renders"
        rendered.mkdir(parents=True)
        body = rendered / "body.mp4"
        body.write_bytes(b"complete-body-video")
        job_id = self.store.create_job(title="秋装/显瘦.mp4", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        job = self.store.get_job(job_id)
        output = self.runner._save_final(job, workspace, {"deliverables": {"body": str(body), "hooks": {}}})
        self.assertEqual(output.name, "秋装-显瘦.mp4")
        self.assertEqual(output.read_bytes(), b"complete-body-video")
        self.assertEqual(self.store.get_job(job_id)["artifacts"][0]["stage_id"], "delivery")

    def test_srt_uses_edited_playback_clock(self):
        target = self.root / "captions.srt"
        self.runner._write_srt([
            {"audio": {"start": 100, "end": 102.5, "text": "第一句"}},
            {"audio": {"start": 8, "end": 10, "text": "第二句"}},
        ], target)
        text = target.read_text(encoding="utf-8")
        self.assertIn("00:00:00,000 --> 00:00:02,500", text)
        self.assertIn("00:00:02,500 --> 00:00:04,500", text)

    def test_source_edit_history_marks_previously_used_candidates(self):
        source_root = self.root / "sources" / "source-abc"
        workspace = source_root / "edits" / "job-1"
        (source_root / "shared").mkdir(parents=True)
        workspace.mkdir(parents=True)
        (source_root / "source.json").write_text("{}", encoding="utf-8")
        job = {"id": "job-1", "title": "第一版", "workspace": str(workspace),
               "creative_strategy": "personality"}
        rows = [{"start": 10, "end": 12, "text": "这件上身特别显瘦", "role": "result"}]
        self.runner._record_edit_history(
            job, rows, {"main_product": "上衣", "creative_strategy": "personality"})
        next_job = {"id": "job-2", "workspace": str(source_root / "edits" / "job-2")}
        annotated = self.runner._annotate_candidate_history(next_job, [
            {"i": 1, "s": 10, "e": 12, "t": "这件上身特别显瘦"},
            {"i": 2, "s": 20, "e": 22, "t": "另外一句新内容"},
        ])
        self.assertEqual(annotated[0]["u"], 1)
        self.assertNotIn("u", annotated[1])

    def test_only_hard_or_duration_issues_trigger_refinement(self):
        warnings = [
            {"code": "missing_close", "level": "warning"},
            {"code": "role_cluster", "level": "warning"},
        ]
        self.assertEqual(self.runner._refinement_issues(warnings), [])
        self.assertEqual(len(self.runner._refinement_issues([
            *warnings, {"code": "soft_duration_short", "level": "warning"}
        ])), 1)

    def test_validated_inputs_detect_timeline_changes(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        engine = self.root / "engine"
        (engine / "dual_timelines").mkdir(parents=True)
        (engine / "timelines").mkdir()
        dual = engine / "dual_timelines" / "body.json"
        module = engine / "timelines" / "body.json"
        dual.write_text("[]", encoding="utf-8")
        module.write_text("[]", encoding="utf-8")
        (engine / "video_mapping_report.json").write_text(
            json.dumps({"dual_timelines": {"body": str(dual)}}), encoding="utf-8")
        snapshot = self.runner._delivery_inputs(source, engine)
        self.runner._assert_delivery_inputs(snapshot, source)
        dual.write_text("[{}]", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            self.runner._assert_delivery_inputs(snapshot, source)

    def test_audio_lock_ignores_visual_replacements_but_tracks_text_timeline(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        engine = self.root / "engine-audio-lock"
        (engine / "dual_timelines").mkdir(parents=True)
        (engine / "timelines").mkdir()
        dual = engine / "dual_timelines" / "body.json"
        module = engine / "timelines" / "body.json"
        dual.write_text("[]", encoding="utf-8")
        module.write_text("[]", encoding="utf-8")
        (engine / "video_mapping_report.json").write_text(
            json.dumps({"dual_timelines": {"body": str(dual)}}), encoding="utf-8")
        snapshot = self.runner._audio_inputs(source, engine)
        dual.write_text('[{"video":[]}]', encoding="utf-8")
        self.runner._assert_audio_inputs(snapshot, source)
        module.write_text("[{}]", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.runner._assert_audio_inputs(snapshot, source)

    def test_only_waiting_stages_accept_payload(self):
        workspace = self.root / "job"
        workspace.mkdir()
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        with self.assertRaisesRegex(ValueError, "不接受外部决策"):
            self.runner.submit(job_id, {"verdict": "approve"})

    def test_running_edit_plan_rejects_concurrent_manual_submission(self):
        workspace = self.root / "job"
        workspace.mkdir()
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        self.store.stage_start(job_id, "edit_plan", "AI 正在编排")
        with self.assertRaisesRegex(ValueError, "等待决策"):
            self.runner.submit(job_id, {"main_product": "测试", "picks": []})

    def test_cancelled_engine_failure_does_not_replace_status(self):
        workspace = self.root / "job"
        workspace.mkdir()
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="standard", workspace=str(workspace))
        self.store.stage_start(job_id, "validation", "执行中")
        self.runner.cancel(job_id)
        with self.assertRaises(JobCancelled):
            self.runner._fail_engine(job_id, "validation", 143, {"state": "failed"})
        self.assertEqual(self.store.get_job(job_id)["status"], "cancelled")
        stage = next(item for item in self.store.get_job(job_id)["stages"]
                     if item["stage_id"] == "validation")
        self.assertEqual(stage["status"], "cancelled")

    def test_edit_plan_requires_hook_body_and_unique_segments(self):
        valid = {
            "main_product": "白山茶",
            "picks": [
                {"src": 1, "start": 0, "end": 3, "text": "开头", "role": "hook", "module": "hook_A"},
                {"src": 1, "start": 4, "end": 8, "text": "正文", "role": "proof", "module": "body"},
            ],
        }
        self.runner._validate_edit_plan(valid)
        no_body = json.loads(json.dumps(valid))
        no_body["picks"][1]["module"] = "hook_A"
        with self.assertRaisesRegex(ValueError, "正文"):
            self.runner._validate_edit_plan(no_body)
        duplicate = json.loads(json.dumps(valid))
        duplicate["picks"][1].update({"start": 0, "end": 3})
        with self.assertRaisesRegex(ValueError, "重复"):
            self.runner._validate_edit_plan(duplicate)

    def test_workbuddy_plan_is_validated_saved_and_requeued(self):
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        candidates = [
            {"i": 1, "s": 1.0, "e": 3.0, "c": "hook", "t": "开头"},
            {"i": 2, "s": 4.0, "e": 8.0, "c": "proof", "t": "正文"},
            {"i": 3, "s": 10.0, "e": 12.0, "c": "styling", "t": "搭配"},
            {"i": 4, "s": 14.0, "e": 16.0, "c": "close", "t": "收尾"},
        ]
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        job_id = self.store.create_job(title="白山茶", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace=str(workspace),
                                       model_provider="workbuddy", model_name="auto")
        self.store.stage_wait(job_id, "edit_plan", "等待")
        plan = {
            "main_product": "白山茶",
            "picks": [
                {"src": 1, "start": 1.0, "end": 3.0, "text": "开头", "role": "hook", "module": "hook_A"},
                {"src": 1, "start": 4.0, "end": 8.0, "text": "正文", "role": "proof", "module": "body"},
                {"src": 1, "start": 10.0, "end": 12.0, "text": "搭配", "role": "styling", "module": "body"},
                {"src": 1, "start": 14.0, "end": 16.0, "text": "收尾", "role": "close", "module": "body"},
            ],
        }
        provider = Mock()
        provider.display_name = "WorkBuddy CLI"
        provider.generate_plan.return_value = {
            "plan": plan, "raw": {"result": plan}, "stderr": "", "seconds": 1.2,
            "usage": {"input_tokens": 100, "output_tokens": 30},
        }
        with patch.object(self.runner, "_provider", return_value=provider):
            self.runner._run_ai_plan(self.store.get_job(job_id), engine)
        job = self.store.get_job(job_id)
        stage = next(item for item in job["stages"] if item["stage_id"] == "edit_plan")
        self.assertEqual(stage["status"], "succeeded")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["token_input"], 100)
        self.assertEqual(json.loads((engine / "picks.json").read_text(encoding="utf-8"))["main_product"],
                         "白山茶")

    def test_soft_structure_warnings_do_not_trigger_another_model_call(self):
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        candidates = [
            {"i": 1, "s": 1.0, "e": 3.0, "c": "hook", "t": "开头"},
            {"i": 2, "s": 4.0, "e": 8.0, "c": "proof", "t": "证明"},
            {"i": 3, "s": 10.0, "e": 12.0, "c": "styling", "t": "搭配"},
            {"i": 4, "s": 14.0, "e": 16.0, "c": "close", "t": "收尾"},
        ]
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        job_id = self.store.create_job(title="前置预检", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace=str(workspace),
                                       model_provider="opencode", model_name="test")
        invalid = {"main_product": "测试", "picks": [
            {"src": 1, "start": 1.0, "end": 3.0, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 4.0, "end": 8.0, "text": "证明", "role": "proof", "module": "body"},
        ]}
        valid = {"main_product": "测试", "picks": [
            {"src": 1, "start": 1.0, "end": 3.0, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 4.0, "end": 8.0, "text": "证明", "role": "proof", "module": "body"},
            {"src": 1, "start": 10.0, "end": 12.0, "text": "搭配", "role": "styling", "module": "body"},
            {"src": 1, "start": 14.0, "end": 16.0, "text": "收尾", "role": "close", "module": "body"},
        ]}
        provider = Mock(display_name="OpenCode CLI")
        provider.generate_plan.side_effect = [
            {"plan": invalid, "raw": {"result": invalid}, "seconds": 1,
             "usage": {"input_tokens": 10, "output_tokens": 5}},
            {"plan": valid, "raw": {"result": valid}, "seconds": 2,
             "usage": {"input_tokens": 20, "output_tokens": 8}},
        ]
        with patch.object(self.runner, "_provider", return_value=provider), \
                patch.object(self.runner, "enqueue") as enqueue:
            self.runner._run_ai_plan(self.store.get_job(job_id), engine)
        self.assertEqual(provider.generate_plan.call_count, 1)
        self.assertEqual(json.loads((engine / "picks.json").read_text(encoding="utf-8"))["picks"], invalid["picks"])
        self.assertEqual(self.store.get_job(job_id)["token_input"], 10)
        enqueue.assert_called_once_with(job_id)

    def test_ai_provider_selection_accepts_all_automatic_providers(self):
        for provider_id, model in (("codex", "gpt-5.6-sol"),
                                   ("antigravity", "gemini-3.1-pro-high"),
                                   ("opencode", "openai/gpt-5.6-sol"),
                                   ("multica", "agent-1")):
            provider = Mock()
            provider.display_name = provider_id
            provider.info.return_value = {"available": True}
            provider.models.return_value = [(model, model)]
            provider.validate_model.side_effect = lambda value, expected=model: (
                None if value == expected else (_ for _ in ()).throw(ValueError("unsupported")))
            with patch.object(self.runner, "_provider", return_value=provider):
                self.assertEqual(self.runner.resolve_ai_selection(f"{provider_id}:{model}"),
                                 (provider_id, model))

    def test_waiting_opencode_job_runs_ai_plan_instead_of_returning_to_manual(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        (engine / "candidate_digest.json").write_text("[]", encoding="utf-8")
        job_id = self.store.create_job(
            title="OpenCode 编排", source_path=str(source), brief="", mode="fast",
            workspace=str(workspace), model_provider="opencode", model_name="jysd/glm-5.3-flash",
        )
        self.store.stage_wait(job_id, "edit_plan", "等待编排")
        with patch.object(self.runner, "_run_ai_plan") as run_ai_plan:
            self.runner._run(self.store.get_job(job_id))
        run_ai_plan.assert_called_once()
        self.assertEqual(run_ai_plan.call_args.args[0]["model_provider"], "opencode")

    def _auto_plan_candidates(self) -> tuple[Path, list[dict[str, object]]]:
        engine = self.root / "job" / "engine"
        engine.mkdir(parents=True)
        candidates = [
            {"i": 1, "s": 1.0, "e": 3.0, "c": "hook", "t": "开头"},
            {"i": 2, "s": 4.0, "e": 8.0, "c": "proof", "t": "正文"},
            {"i": 3, "s": 10.0, "e": 12.0, "c": "styling", "t": "搭配"},
            {"i": 4, "s": 14.0, "e": 16.0, "c": "close", "t": "收尾"},
        ]
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        return engine, candidates

    @staticmethod
    def _plan_provider(name: str, plan: dict[str, object] | None = None,
                       error: Exception | None = None) -> Mock:
        provider = Mock(display_name=name)
        if error is not None:
            provider.generate_plan.side_effect = error
        else:
            provider.generate_plan.return_value = {
                "plan": plan, "raw": {"result": plan}, "seconds": 1,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        return provider

    @staticmethod
    def _auto_plan() -> dict[str, object]:
        return {"main_product": "白山茶", "picks": [
            {"src": 1, "start": 1.0, "end": 3.0, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 4.0, "end": 8.0, "text": "正文", "role": "proof", "module": "body"},
            {"src": 1, "start": 10.0, "end": 12.0, "text": "搭配", "role": "styling", "module": "body"},
            {"src": 1, "start": 14.0, "end": 16.0, "text": "收尾", "role": "close", "module": "body"},
        ]}

    def test_ai_plan_failure_falls_back_to_next_provider_without_waiting(self):
        engine = self._plan_setup_engine()
        job_id = self.store.create_job(title="自动降级", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace=str(engine.parent),
                                       model_provider="workbuddy", model_name="auto")
        failing = self._plan_provider("WorkBuddy CLI", error=RuntimeError("CLI 崩溃"))
        passing = self._plan_provider("OpenCode CLI", plan=self._auto_plan())
        providers = {"workbuddy": failing, "opencode": passing}
        def dispatch(pid: str) -> Mock:
            if pid not in providers:
                raise ValueError(f"不支持的 AI 提供方: {pid}")
            return providers[pid]
        with patch.object(self.runner, "_provider", side_effect=dispatch):
            self.runner._run_ai_plan(self.store.get_job(job_id), engine)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["model_provider"], "opencode")
        self.assertEqual(json.loads((engine / "picks.json").read_text(encoding="utf-8"))["main_product"], "白山茶")
        kinds = [event["kind"] for event in job["events"]]
        self.assertIn("ai_plan_fallback", kinds)
        self.assertNotIn("input_required", kinds)
        stage = next(item for item in job["stages"] if item["stage_id"] == "edit_plan")
        self.assertEqual(stage["status"], "succeeded")

    def test_all_providers_failing_fails_job_instead_of_waiting(self):
        engine = self._plan_setup_engine()
        job_id = self.store.create_job(title="全部失败", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace=str(self.root / "job"),
                                       model_provider="workbuddy", model_name="auto")
        failing = self._plan_provider("WorkBuddy CLI", error=RuntimeError("CLI 崩溃"))
        with patch.object(self.runner, "_provider", return_value=failing):
            self.runner._run_ai_plan(self.store.get_job(job_id), engine)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("自动编排失败", str(job["error"]))
        kinds = [event["kind"] for event in job["events"]]
        self.assertNotIn("input_required", kinds)

    def test_manual_provider_job_automatically_resolves_default_model(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        engine = self._plan_setup_engine()
        self.store.set_setting("ai_default_selection", "opencode:test")
        job_id = self.store.create_job(title="旧任务", source_path=str(source),
                                       brief="", mode="fast", workspace=str(self.root / "job"),
                                       model_provider="manual", model_name=None)
        self.store.stage_wait(job_id, "edit_plan", "等待编排")
        provider = self._plan_provider("OpenCode CLI", plan=self._auto_plan())
        seen: list[str] = []
        def dispatch(provider_id: str):
            seen.append(provider_id)
            return provider
        with patch.object(self.runner, "_provider", side_effect=dispatch), \
                patch.object(self.runner, "enqueue"):
            self.runner._run(self.store.get_job(job_id))
        job = self.store.get_job(job_id)
        self.assertEqual(job["model_provider"], "opencode")
        self.assertEqual(seen[0], "opencode")
        stage = next(item for item in job["stages"] if item["stage_id"] == "edit_plan")
        self.assertEqual(stage["status"], "succeeded")

    def _plan_setup_engine(self) -> Path:
        engine = self.root / "job" / "engine"
        engine.mkdir(parents=True, exist_ok=True)
        candidates = [
            {"i": 1, "s": 1.0, "e": 3.0, "c": "hook", "t": "开头"},
            {"i": 2, "s": 4.0, "e": 8.0, "c": "proof", "t": "正文"},
            {"i": 3, "s": 10.0, "e": 12.0, "c": "styling", "t": "搭配"},
            {"i": 4, "s": 14.0, "e": 16.0, "c": "close", "t": "收尾"},
        ]
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        return engine

    def test_editing_constraints_adapt_to_available_spoken_material(self):
        candidates = [
            {"s": 0, "e": 4}, {"s": 5, "e": 9}, {"s": 10, "e": 14},
            {"s": 15, "e": 19}, {"s": 20, "e": 24}, {"s": 25, "e": 29},
            {"s": 30, "e": 34}, {"s": 35, "e": 39}, {"s": 40, "e": 44},
            {"s": 45, "e": 49}, {"s": 50, "e": 54}, {"s": 55, "e": 59},
        ]
        limits = self.runner._editing_constraints(candidates)
        self.assertEqual(limits["min_total"], 31)
        self.assertEqual(limits["max_total"], 48)
        self.assertEqual(limits["min_segments"], 9)
        prompt = self.runner._plan_prompt({"title": "测试", "mode": "fast", "brief": ""}, candidates)
        self.assertIn("31-48 秒", prompt)
        self.assertIn("不强制 proof、close", prompt)
        self.assertIn("顶层只包含 main_product、creative_strategy 和 picks", prompt)
        self.assertIn("禁止省略", prompt)

    def test_plan_preflight_catches_long_continuous_run_before_engine(self):
        plan = {"main_product": "白山茶", "picks": [
            {"src": 1, "start": 7.16, "end": 11.22, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 11.5, "end": 15.28, "text": "面料", "role": "proof", "module": "body"},
            {"src": 1, "start": 15.6, "end": 19.26, "text": "展示", "role": "demo", "module": "body"},
            {"src": 1, "start": 35.76, "end": 37.06, "text": "搭配", "role": "styling", "module": "body"},
            {"src": 1, "start": 41.7, "end": 43.32, "text": "收尾", "role": "close", "module": "body"},
        ]}
        issues = self.runner._plan_preflight_issues(
            plan, {"min_total": 10, "max_total": 30, "min_segments": 4, "max_segments": 12})
        continuous = [item for item in issues if item["code"] == "continuous_source_run"]
        self.assertEqual(len(continuous), 1)
        self.assertIn("11.50s", continuous[0]["detail"])

    def test_plan_preflight_enforces_five_second_pick_limit(self):
        plan = {"main_product": "测试", "picks": [
            {"src": 1, "start": 0, "end": 5.2, "text": "过长", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 8, "end": 10, "text": "证明", "role": "proof", "module": "body"},
            {"src": 1, "start": 12, "end": 14, "text": "搭配", "role": "styling", "module": "body"},
            {"src": 1, "start": 16, "end": 18, "text": "收尾", "role": "close", "module": "body"},
        ]}
        issues = self.runner._plan_preflight_issues(
            plan, {"min_total": 1, "max_total": 30, "min_segments": 2, "max_segments": 12})
        self.assertIn("segment_too_long", {item["code"] for item in issues})

    def test_plan_preflight_rejects_three_consecutive_roles(self):
        plan = {"main_product": "测试", "picks": [
            {"src": 1, "start": 0, "end": 2, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 10, "end": 13, "text": "颜色一", "role": "color", "module": "body"},
            {"src": 1, "start": 20, "end": 23, "text": "颜色二", "role": "color", "module": "body"},
            {"src": 1, "start": 30, "end": 33, "text": "颜色三", "role": "color", "module": "body"},
            {"src": 1, "start": 40, "end": 42, "text": "证明", "role": "proof", "module": "body"},
            {"src": 1, "start": 50, "end": 52, "text": "收尾", "role": "close", "module": "body"},
        ]}
        issues = self.runner._plan_preflight_issues(
            plan, {"min_total": 1, "max_total": 30, "min_segments": 2, "max_segments": 12})
        clusters = [item for item in issues if item["code"] == "role_cluster"]
        self.assertEqual(len(clusters), 1)
        self.assertIn("超过 8s", clusters[0]["detail"])

    def test_plan_preflight_rejects_obviously_incomplete_sentence(self):
        plan = {"main_product": "测试", "picks": [
            {"src": 1, "start": 0, "end": 2, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 10, "end": 12, "text": "效果很好", "role": "proof", "module": "body"},
            {"src": 1, "start": 20, "end": 22, "text": "适合通勤", "role": "scene", "module": "body"},
            {"src": 1, "start": 30, "end": 32, "text": "推荐它是因为。", "role": "close", "module": "body"},
        ]}
        issues = self.runner._plan_preflight_issues(
            plan, {"min_total": 1, "max_total": 30, "min_segments": 2, "max_segments": 12})
        self.assertIn("incomplete_sentence", {item["code"] for item in issues})

    def test_retry_returns_invalid_validation_plan_to_edit_stage(self):
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        candidates = [
            {"i": 1, "s": 0, "e": 4, "c": "hook", "t": "开头"},
            {"i": 2, "s": 4.2, "e": 8.2, "c": "material", "t": "面料"},
        ]
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        (engine / "picks.json").write_text(json.dumps({"main_product": "测试", "picks": [
            {"src": 1, "start": 0, "end": 4, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 4.2, "end": 8.2, "text": "面料", "role": "material", "module": "body"},
        ]}), encoding="utf-8")
        job_id = self.store.create_job(title="退回编排", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace=str(workspace),
                                       model_provider="opencode", model_name="jysd/glm-5.3-flash")
        self.store.stage_fail(job_id, "validation", "失败")
        with patch.object(self.runner, "enqueue") as enqueue:
            self.runner.retry(job_id)
        self.assertFalse((engine / "picks.json").exists())
        self.assertEqual(self.store.get_job(job_id)["current_stage"], "edit_plan")
        enqueue.assert_called_once_with(job_id)

    def test_retry_restores_last_preflight_plan_without_calling_ai(self):
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        candidates = [
            {"i": 1, "s": 0, "e": 1.5, "c": "hook", "t": "开头"},
            {"i": 2, "s": 10, "e": 11.5, "c": "proof", "t": "证明"},
            {"i": 3, "s": 20, "e": 21.5, "c": "fit", "t": "显瘦"},
            {"i": 4, "s": 30, "e": 31.5, "c": "close", "t": "收尾"},
        ]
        accepted = {"main_product": "测试", "picks": [
            {"src": 1, "start": 0, "end": 1.5, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 10, "end": 11.5, "text": "证明", "role": "proof", "module": "body"},
            {"src": 1, "start": 20, "end": 21.5, "text": "显瘦", "role": "fit", "module": "body"},
            {"src": 1, "start": 30, "end": 31.5, "text": "收尾", "role": "close", "module": "body"},
        ]}
        invalid = {"main_product": "错误覆盖", "picks": [
            {"src": 1, "start": 0, "end": 6, "text": "过长", "role": "hook", "module": "hook_A"},
        ]}
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        (engine / "picks.json").write_text(json.dumps(invalid), encoding="utf-8")
        (workspace / "opencode-plan-response.json").write_text(json.dumps({
            "attempts": [{"raw": {"result": accepted}, "issues": []}],
        }), encoding="utf-8")
        job_id = self.store.create_job(
            title="恢复旧方案", source_path="/tmp/source.mp4", brief="", mode="fast",
            workspace=str(workspace), model_provider="opencode", model_name="test",
        )
        self.store.stage_fail(job_id, "validation", "失败")
        with patch.object(self.runner, "enqueue") as enqueue:
            self.runner.retry(job_id)
        self.assertEqual(json.loads((engine / "picks.json").read_text(encoding="utf-8")), accepted)
        self.assertEqual(self.store.get_job(job_id)["current_stage"], "validation")
        enqueue.assert_called_once_with(job_id)

    def test_retry_invalidates_corrupt_timeline_lock_and_returns_to_validation(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        (engine / "candidate_digest.json").write_text("[]", encoding="utf-8")
        (engine / "picks.json").write_text(json.dumps({"main_product": "测试", "picks": [
            {"src": 1, "start": 0, "end": 2, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 10, "end": 12, "text": "证明", "role": "proof", "module": "body"},
            {"src": 1, "start": 20, "end": 22, "text": "通勤", "role": "scene", "module": "body"},
            {"src": 1, "start": 30, "end": 32, "text": "收尾", "role": "close", "module": "body"},
        ]}), encoding="utf-8")
        marker = workspace / "timeline_locked.json"
        marker.write_text("{", encoding="utf-8")
        job_id = self.store.create_job(title="损坏锁", source_path=str(source), brief="",
                                       mode="fast", workspace=str(workspace))
        self.store.stage_fail(job_id, "delivery", "锁损坏")
        with patch.object(self.runner, "enqueue") as enqueue:
            self.runner.retry(job_id)
        job = self.store.get_job(job_id)
        self.assertFalse(marker.exists())
        self.assertEqual(job["current_stage"], "validation")
        self.assertEqual(next(x for x in job["stages"] if x["stage_id"] == "validation")["status"],
                         "pending")
        enqueue.assert_called_once_with(job_id)

    def test_recovery_revalidates_changed_timeline_instead_of_delivering(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        workspace = self.root / "job"
        engine = workspace / "engine"
        (engine / "dual_timelines").mkdir(parents=True)
        (engine / "timelines").mkdir()
        (engine / "picks.json").write_text("{}", encoding="utf-8")
        dual = engine / "dual_timelines" / "body.json"
        module = engine / "timelines" / "body.json"
        dual.write_text("[]", encoding="utf-8")
        module.write_text("[]", encoding="utf-8")
        (engine / "video_mapping_report.json").write_text(
            json.dumps({"dual_timelines": {"body": str(dual)}}), encoding="utf-8")
        snapshot = self.runner._delivery_inputs(source, engine)
        marker = workspace / "timeline_locked.json"
        marker.write_text(json.dumps({"validated_inputs": snapshot}), encoding="utf-8")
        dual.write_text("[{}]", encoding="utf-8")
        job_id = self.store.create_job(title="恢复校验", source_path=str(source), brief="",
                                       mode="fast", workspace=str(workspace))
        with patch.object(self.runner, "_prepare_render") as prepare, \
                patch.object(self.runner, "_deliver") as deliver:
            self.runner._run(self.store.get_job(job_id))
        prepare.assert_called_once()
        deliver.assert_not_called()
        self.assertFalse(marker.exists())

    def test_validation_failure_message_lists_actionable_issues(self):
        workspace = self.root / "job"
        workspace.mkdir()
        job_id = self.store.create_job(title="测试", source_path="/tmp/source.mp4",
                                       brief="", mode="fast", workspace=str(workspace))
        summary = {"state": "failed", "issues": [
            {"code": "too_few_segments", "detail": "9 segments; minimum 14"},
            {"code": "missing_proof", "detail": "timeline needs proof or demo"},
        ]}
        self.runner._fail_engine(job_id, "validation", 1, summary)
        error = self.store.get_job(job_id)["error"]
        self.assertIn("入选片段数量不足", error)
        self.assertIn("缺少效果佐证或展示", error)

    def test_validation_failure_never_calls_ai_or_overwrites_plan(self):
        workspace = self.root / "job"
        engine = workspace / "engine"
        engine.mkdir(parents=True)
        candidates = [
            {"i": 1, "s": 1.0, "e": 3.0, "c": "hook", "t": "开头"},
            {"i": 2, "s": 4.0, "e": 8.0, "c": "proof", "t": "证明"},
        ]
        (engine / "candidate_digest.json").write_text(json.dumps(candidates), encoding="utf-8")
        old = {"main_product": "原方案", "picks": [
            {"src": 1, "start": 1.0, "end": 3.0, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 4.0, "end": 8.0, "text": "证明", "role": "proof", "module": "body"},
        ]}
        picks = engine / "picks.json"
        picks.write_text(json.dumps(old), encoding="utf-8")
        job_id = self.store.create_job(
            title="禁止校验旁路", source_path="/tmp/source.mp4", brief="", mode="fast",
            workspace=str(workspace), model_provider="opencode", model_name="jysd/glm-5.3-flash",
        )
        job = self.store.get_job(job_id)
        summary = {"state": "failed", "publish_ready": False,
                   "issues": [{"code": "continuous_source_run", "detail": "too long"}]}
        with patch.object(self.runner, "_execute", return_value=(1, summary)), \
                patch.object(self.runner, "_provider") as provider_lookup:
            self.runner._prepare_render(job, Path("/tmp/source.mp4"), workspace, engine,
                                        workspace / "timeline_locked.json")
        provider_lookup.assert_not_called()
        self.assertEqual(json.loads(picks.read_text(encoding="utf-8")), old)
        self.assertFalse((workspace / "validation-repair.json").exists())

    def test_delivery_reuses_snapshot_without_full_pipeline(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        workspace = self.root / "job"
        engine = workspace / "engine"
        (engine / "dual_timelines").mkdir(parents=True)
        (engine / "timelines").mkdir()
        dual = engine / "dual_timelines" / "body.json"
        module = engine / "timelines" / "body.json"
        dual.write_text("[]", encoding="utf-8")
        module.write_text("[]", encoding="utf-8")
        (engine / "video_mapping_report.json").write_text(
            json.dumps({"dual_timelines": {"body": str(dual)}}), encoding="utf-8")
        snapshot = self.runner._delivery_inputs(source, engine)
        (workspace / "timeline_locked.json").write_text(
            json.dumps({"validated_inputs": snapshot}), encoding="utf-8")
        job_id = self.store.create_job(title="完整成片", source_path=str(source), brief="",
                                       mode="standard", workspace=str(workspace))
        self.store.update_stage(job_id, "validation", status="succeeded")
        job = self.store.get_job(job_id)
        stale_output = workspace / "deliverables" / "完整成片.mp4"
        stale_output.parent.mkdir(parents=True)
        stale_output.write_bytes(b"stale-video-without-matching-manifest")

        labels = []
        def fake_step(_job_id, label, command, steps, _progress):
            labels.append(label)
            if label.startswith("高清渲染"):
                output = Path(command[3])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"full-video")
            else:
                qc_dir = Path(command[3])
                qc_dir.mkdir(parents=True, exist_ok=True)
                (qc_dir / "qc_report.json").write_text('{"ok":true}', encoding="utf-8")
            steps.append({"name": label, "ok": True})

        with patch.object(self.runner, "_run_delivery_step", side_effect=fake_step), \
                patch.object(self.runner, "_execute", side_effect=AssertionError("full pipeline repeated")):
            self.runner._deliver(job, source, workspace, engine)
        completed = self.store.get_job(job_id)
        self.assertEqual(completed["status"], "completed")
        self.assertTrue((workspace / "deliverables" / "完整成片.mp4").is_file())
        self.assertEqual(stale_output.read_bytes(), b"full-video")
        self.assertTrue(labels)
        self.assertFalse(any("技术验收" in label or "QC" in label for label in labels))
        with patch.object(self.runner, "_run_delivery_step",
                          side_effect=AssertionError("matching render cache was not reused")):
            self.runner._deliver(self.store.get_job(job_id), source, workspace, engine)

    def test_plan_preflight_detects_product_interleaving(self):
        plan = {"main_product": "套装", "picks": [
            {"src": 1, "start": 0, "end": 2, "text": "开头", "role": "hook", "module": "hook_A", "product": "上衣"},
            {"src": 1, "start": 10, "end": 12, "text": "裤子展示", "role": "proof", "module": "body", "product": "裤子"},
            {"src": 1, "start": 20, "end": 22, "text": "上衣细节", "role": "fit", "module": "body", "product": "上衣"},
            {"src": 1, "start": 30, "end": 32, "text": "收尾", "role": "close", "module": "body", "product": "上衣"},
        ]}
        issues = self.runner._plan_preflight_issues(
            plan, {"min_total": 1, "max_total": 30, "min_segments": 2, "max_segments": 12})
        interleaving = [item for item in issues if item["code"] == "product_interleaving"]
        self.assertEqual(len(interleaving), 1)
        self.assertIn("禁止交叉穿插", interleaving[0]["detail"])

    def test_plan_preflight_warns_too_many_material_segments(self):
        plan = {"main_product": "上衣", "picks": [
            {"src": 1, "start": 0, "end": 2, "text": "开头", "role": "hook", "module": "hook_A"},
            {"src": 1, "start": 10, "end": 12, "text": "面料一", "role": "material", "module": "body"},
            {"src": 1, "start": 20, "end": 22, "text": "面料二", "role": "material", "module": "body"},
            {"src": 1, "start": 30, "end": 32, "text": "面料三", "role": "material", "module": "body"},
            {"src": 1, "start": 40, "end": 42, "text": "版型", "role": "fit", "module": "body"},
            {"src": 1, "start": 50, "end": 52, "text": "收尾", "role": "close", "module": "body"},
        ]}
        issues = self.runner._plan_preflight_issues(
            plan, {"min_total": 1, "max_total": 30, "min_segments": 2, "max_segments": 12})
        mat_issues = [item for item in issues if item["code"] == "too_many_material_segments"]
        self.assertEqual(len(mat_issues), 1)
        self.assertIn("讲面料不要过多", mat_issues[0]["detail"])

    def test_directives_manager_learned_rules(self):
        self.runner.directives.add_learned_directive("主播背身展示不要切断", source_job_id="job-1")
        prompt = self.runner._plan_prompt({"title": "测试", "mode": "fast", "brief": "",
                                           "products": ["上衣", "阔腿裤"], "materials": ["羊毛"]}, [])
        self.assertIn("指定商品（必须按商品集中讲解，严禁交叉穿插）：上衣、阔腿裤", prompt)
        self.assertIn("指定面料（篇幅精简适中）：羊毛", prompt)
        self.assertIn("主播背身展示不要切断", prompt)


if __name__ == "__main__":
    unittest.main()
