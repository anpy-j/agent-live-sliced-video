import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.ai import (AntigravityCli, CodexCli, MulticaCli, OpenCodeCli,
                            ProviderResponseError, WorkBuddyCli,
                            _workbuddy_catalog_cache, workbuddy_model_catalog)


class WorkBuddyCliTest(unittest.TestCase):
    def setUp(self):
        _workbuddy_catalog_cache.clear()

    def test_standalone_cli_shared_by_catalog_and_pipeline(self):
        from agent_video.ai import _workbuddy_cli_entries
        from agent_video.pipeline.ai import _provider_executable
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            entry = (home / "AppData/Roaming/npm/node_modules/@tencent-ai"
                     / "codebuddy-code/bin/codebuddy")
            entry.parent.mkdir(parents=True)
            entry.write_text("", encoding="utf-8")
            with patch("agent_video.ai.Path.home", return_value=home), \
                    patch("agent_video.ai.sys.platform", "win32"), \
                    patch("agent_video.ai._workbuddy_live_models", return_value=[
                        ("glm-5.3-flash", "glm-5.3-flash")]) as models:
                self.assertEqual(_workbuddy_cli_entries(), [entry])
                self.assertEqual(_provider_executable("workbuddy"), str(entry))
                self.assertIn(("glm-5.3-flash", "glm-5.3-flash"), workbuddy_model_catalog())
                models.assert_called_once_with(entry)

    def test_workbuddy_catalog_reads_all_tool_call_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli_root = Path(tmp) / "cli"
            executable = cli_root / "bin" / "codebuddy"
            executable.parent.mkdir(parents=True)
            executable.write_text("", encoding="utf-8")
            (cli_root / "product.json").write_text(json.dumps({"models": [
                {"id": "legacy", "name": "Legacy", "supportsToolCall": True},
                {"id": "auto", "name": "Auto", "supportsToolCall": True},
                {"id": "image", "name": "Image only", "supportsToolCall": False},
            ]}), encoding="utf-8")

            with patch("agent_video.ai._workbuddy_live_models", return_value=[]):
                self.assertEqual(workbuddy_model_catalog(executable), [
                    ("auto", "Auto"), ("legacy", "Legacy")])
                self.assertEqual(WorkBuddyCli(executable).models(), [
                    ("auto", "Auto"), ("legacy", "Legacy")])

    def test_workbuddy_catalog_prefers_live_cli_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            cli_root = Path(tmp) / "cli"
            executable = cli_root / "bin" / "codebuddy"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (cli_root / "product.json").write_text(json.dumps({"models": [
                {"id": "glm-5.2", "name": "GLM-5.2", "supportsToolCall": True},
                {"id": "image", "name": "Image only", "supportsToolCall": False},
            ]}), encoding="utf-8")
            result = Mock(stdout=(
                "  --model <model>   Model for the current session. "
                "Currently supported: (deepseek-v4.1-flash, glm-5.3, glm-5.3-flash, "
                "glm-5.2)\n  --text-to-image-model <model>\n"))
            with patch("agent_video.ai.subprocess.run", return_value=result):
                catalog = workbuddy_model_catalog(executable)
        self.assertEqual(catalog, [
            ("auto", "自动选择"),
            ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
            ("glm-5.3", "glm-5.3"),
            ("glm-5.3-flash", "glm-5.3-flash"),
            ("glm-5.2", "GLM-5.2"),
        ])

    def test_workbuddy_windows_extensionless_cli_uses_node(self):
        provider = WorkBuddyCli(Path("C:/WorkBuddy/cli/bin/codebuddy"))
        with patch("agent_video.ai.sys.platform", "win32"):
            self.assertEqual(provider._command_prefix(),
                             ["node", "C:\\WorkBuddy\\cli\\bin\\codebuddy"])

    def test_windows_timeout_kills_entire_provider_process_tree(self):
        provider = WorkBuddyCli(Path("C:/workbuddy.cmd"))
        process = Mock(pid=4321, stdin=None, stdout=None, stderr=None, returncode=1)
        process.poll.return_value = None
        expired = subprocess.TimeoutExpired("workbuddy", 2, output="partial", stderr="waiting")
        process.communicate.side_effect = [expired, ("tail", "closed")]
        with patch("agent_video.ai.subprocess.Popen", return_value=process), \
                patch("agent_video.ai.subprocess.run") as run, \
                patch("agent_video.ai.sys.platform", "win32"):
            with self.assertRaisesRegex(ProviderResponseError, "已停止"):
                provider._complete(["workbuddy"], cwd=Path("."), on_process=None,
                                   timeout=2, started=time.monotonic(), stdin_text="prompt")
        self.assertEqual(run.call_args.args[0],
                         ["taskkill", "/PID", "4321", "/T", "/F"])
        process.communicate.assert_called_with(timeout=10)

    def test_extracts_nested_structured_plan_and_usage(self):
        plan = {
            "main_product": "白山茶",
            "picks": [
                {"src": 1, "start": 1, "end": 3, "text": "开头", "role": "hook", "module": "hook_A"},
                {"src": 1, "start": 4, "end": 7, "text": "正文", "role": "proof", "module": "body"},
            ],
        }
        envelope = {"type": "result", "result": {"structured_output": plan},
                    "usage": {"input_tokens": 120, "output_tokens": 45}}
        self.assertEqual(WorkBuddyCli._find_plan(envelope), plan)
        self.assertEqual(WorkBuddyCli._find_usage(envelope),
                         {"input_tokens": 120, "output_tokens": 45})

    def test_extracts_plan_from_json_string(self):
        envelope = {"result": '{"main_product":"针织衫","picks":[]}'}
        self.assertEqual(WorkBuddyCli._find_plan(envelope)["main_product"], "针织衫")

    def test_extracts_plan_from_text_with_markdown_block(self):
        envelope = {"result": '这是编排结果：\n\n```json\n{"main_product":"风衣","picks":[]}\n```\n请查收。'}
        self.assertEqual(WorkBuddyCli._find_plan(envelope)["main_product"], "风衣")

    def test_workbuddy_command_includes_structured_output_tool(self):
        provider = WorkBuddyCli(Path("/tmp/workbuddy"))
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "validate_model"), \
                patch.object(provider, "_complete", return_value=('{"result": {"structured_output": {"main_product": "T恤", "picks": []}}}', "", 1)) as complete:
            provider.generate_plan(model="auto", prompt="test", cwd=Path("/tmp"))
        cmd = complete.call_args.args[0]
        tools_idx = cmd.index("--tools")
        self.assertEqual(cmd[tools_idx + 1], "StructuredOutput")
        self.assertNotIn("test", cmd)
        self.assertEqual(complete.call_args.kwargs["stdin_text"], "test")

    def test_all_providers_share_structured_plan_parser(self):
        envelope = {"output_text": '{"main_product":"风衣","picks":[]}'}
        self.assertEqual(AntigravityCli._find_plan(envelope)["main_product"], "风衣")
        self.assertEqual(CodexCli._find_plan(envelope)["main_product"], "风衣")

    def test_visual_plan_parser_and_capability_lists(self):
        plan = {"replacements": [
            {"block_id": "body:1", "candidate_id": "C003", "reason": "同款全身"}]}
        self.assertEqual(WorkBuddyCli._find_visual_plan({"result": json.dumps(plan)}), plan)
        with patch("agent_video.ai._workbuddy_live_models", return_value=[]):
            self.assertEqual(
                [model_id for model_id, _ in
                 WorkBuddyCli(Path("/tmp/workbuddy")).vision_models()],
                ["glm-5v-turbo"])
        self.assertIn(("gpt-5.6-sol", "GPT-5.6 Sol"),
                      CodexCli(Path("/tmp/codex")).vision_models())
        self.assertEqual(AntigravityCli(Path("/tmp/agy")).vision_models(), [])

    def test_antigravity_models_are_discovered_from_cli(self):
        provider = AntigravityCli(Path("/tmp/agy"))
        AntigravityCli._model_cache = None
        result = Mock(stdout="Fetching available models...\ngemini-test\tGemini Test\n")
        with patch.object(Path, "is_file", return_value=True), \
                patch("agent_video.ai.os.access", return_value=True), \
                patch("agent_video.ai.subprocess.run", return_value=result):
            self.assertIn(("gemini-test", "Gemini Test"), provider.models())

    def test_opencode_models_include_every_installed_model(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        OpenCodeCli._model_cache = None
        result = Mock(stdout="openai/gpt-5.6-sol\njysd/glm-5.2-reasoning\nopencode-go/glm-5.3\n")
        with patch.object(Path, "is_file", return_value=True), \
                patch("agent_video.ai.os.access", return_value=True), \
                patch("agent_video.ai.subprocess.run", return_value=result):
            models = provider.models()
        self.assertIn(("openai/gpt-5.6-sol", "gpt-5.6-sol"), models)
        self.assertIn(("jysd/glm-5.2-reasoning", "glm-5.2-reasoning"), models)
        self.assertIn(("opencode-go/glm-5.3", "glm-5.3"), models)

    def test_opencode_parses_jsonl_text_event(self):
        plan = {"main_product": "风衣", "picks": []}
        stdout = json.dumps({"type": "text", "part": {"text": json.dumps(plan)}})
        events, parsed = OpenCodeCli._parse_events(stdout)
        self.assertEqual(len(events), 1)
        self.assertEqual(parsed, plan)

    def test_opencode_runtime_agent_denies_all_tools(self):
        config = json.loads(OpenCodeCli._runtime_config())
        agent = config["agent"]["livecut"]
        self.assertEqual(agent["permission"]["*"], "deny")
        self.assertFalse(agent["tools"]["*"])

    def test_opencode_usage_reads_jsonl_token_shape(self):
        usage = OpenCodeCli._find_usage({"part": {"tokens": {"input": 321, "output": 87}}})
        self.assertEqual(usage, {"input_tokens": 321, "output_tokens": 87})

    def test_opencode_prompt_puts_required_output_contract_first_and_last(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        valid = json.dumps({"type": "text", "part": {"text": json.dumps({
            "main_product": "白山茶", "picks": [{
                "src": 1, "start": 1, "end": 3, "text": "开头",
                "role": "hook", "module": "hook_A",
            }],
        }, ensure_ascii=False)}}, ensure_ascii=False)
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "validate_model"), \
                patch.object(provider, "_complete", return_value=(valid, "", 1)) as complete:
            provider.generate_plan(model="jysd/test", prompt="业务规则", cwd=Path("/tmp"))
        sent_prompt = complete.call_args.kwargs.get("stdin_text") or complete.call_args.args[0][-1]
        self.assertTrue(sent_prompt.startswith("你现在是一个只返回 JSON 的编排接口"))
        self.assertIn("顶层必须同时包含非空字符串 main_product 和非空数组 picks", sent_prompt)
        self.assertTrue(sent_prompt.endswith("必须返回 main_product 和 picks；只输出 JSON 对象。"))

    def test_opencode_invalid_response_keeps_raw_diagnostic(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        stdout = json.dumps({"type": "text", "part": {"text": "我建议先分析素材"}},
                            ensure_ascii=False)
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "validate_model"), \
                patch.object(provider, "_complete", return_value=(stdout, "warning", 2)):
            with self.assertRaises(ProviderResponseError) as raised:
                provider.generate_plan(model="jysd/test", prompt="业务规则", cwd=Path("/tmp"))
        self.assertEqual(raised.exception.raw["stdout"], stdout)
        self.assertEqual(raised.exception.raw["stderr"], "warning")

    def test_opencode_normalize_model(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        with patch.object(provider, "models", return_value=[
            ("jysd/deepseek-v4.1-flash", "deepseek-v4.1-flash"),
            ("openai/gpt-5.6-sol", "gpt-5.6-sol"),
            ("jysd/glm-5.2-reasoning", "glm-5.2-reasoning"),
        ]):
            self.assertEqual(provider.normalize_model("auto"), "auto")
            self.assertEqual(provider.normalize_model("jysd/deepseek-v4.1-flash"), "jysd/deepseek-v4.1-flash")
            self.assertEqual(provider.normalize_model("deepseek-v4.1-flash"), "jysd/deepseek-v4.1-flash")
            self.assertEqual(provider.normalize_model("gpt-5.6-sol"), "openai/gpt-5.6-sol")
            self.assertEqual(provider.normalize_model("glm-5.2-reasoning"), "jysd/glm-5.2-reasoning")

    def test_opencode_validate_model(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        with patch.object(provider, "models", return_value=[
            ("jysd/deepseek-v4.1-flash", "deepseek-v4.1-flash"),
        ]):
            # Both full name and alias should succeed without error
            provider.validate_model("jysd/deepseek-v4.1-flash")
            provider.validate_model("deepseek-v4.1-flash")
            with self.assertRaisesRegex(ValueError, "不支持模型: nonexistent"):
                provider.validate_model("nonexistent")

    def test_opencode_generate_json_assembled_text(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        events_stdout = "\n".join([
            json.dumps({"type": "step_start"}),
            json.dumps({"type": "text", "part": {"text": '{"decisions": [{"id": 0, '}}),
            json.dumps({"type": "text", "part": {"text": '"usable": true, "reason": "ok"}]}'}}),
            json.dumps({"type": "step_finish"}),
        ])
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "validate_model"), \
                patch.object(provider, "_complete", return_value=(events_stdout, "", 1)):
            res = provider.generate_json(
                model="jysd/test", prompt="test",
                schema={"type": "object", "required": ["decisions"]},
                cwd=Path("/tmp"),
            )
        self.assertEqual(res["data"], {"decisions": [{"id": 0, "usable": True, "reason": "ok"}]})

    def test_multica_agents_are_exposed_as_model_choices(self):
        provider = MulticaCli(Path("/tmp/multica"), profile="desktop", workspace_id="workspace-1")
        agents = {"agents": [
            {"id": "agent-1", "name": "剪辑师", "model": "gpt-5.6-sol",
             "runtime_id": "runtime-1"},
            {"id": "agent-2", "name": "审片师", "runtime": {"name": "Codex"},
             "runtime_id": "runtime-1"},
            {"id": "agent-offline", "name": "离线 Agent", "runtime_id": "runtime-old"},
            {"id": "agent-old", "name": "旧 Agent", "runtime_id": "runtime-1",
             "archived_at": "2026-01-01"},
        ]}
        runtimes = [{"id": "runtime-1", "status": "online"},
                    {"id": "runtime-old", "status": "offline"}]
        with patch.object(Path, "is_file", return_value=True), \
                patch("agent_video.ai.os.access", return_value=True), \
                patch.object(provider, "_run_json",
                             side_effect=[(agents, ""), (runtimes, "")]):
            models = provider.models()
        self.assertEqual(models, [
            ("agent-1", "剪辑师 · gpt-5.6-sol"),
            ("agent-2", "审片师 · Codex"),
        ])
        self.assertEqual(provider._command_prefix(), [
            str(Path("/tmp/multica")), "--profile", "desktop", "--workspace-id", "workspace-1",
        ])

    def test_multica_creates_run_and_extracts_structured_plan(self):
        provider = MulticaCli(Path("/tmp/multica"))
        provider._model_cache = (time.monotonic(), [("agent-1", "剪辑师")])
        plan = {
            "main_product": "风衣",
            "picks": [
                {"src": 1, "start": 0, "end": 3, "text": "开头", "role": "hook",
                 "module": "hook_A"},
                {"src": 1, "start": 4, "end": 8, "text": "正文", "role": "proof",
                 "module": "body"},
            ],
        }
        responses = [
            ({"issue": {"id": "issue-1"}}, ""),
            ({"runs": [{"task_id": "task-1", "status": "running"}]}, ""),
            ({"runs": [{"task_id": "task-1", "status": "completed"}]}, ""),
            ({"messages": [{"content": json.dumps(plan, ensure_ascii=False)}]}, ""),
            ({"input_tokens": 90, "output_tokens": 30}, ""),
        ]
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "_run_json", side_effect=responses) as run, \
                patch("agent_video.ai.time.sleep"):
            result = provider.generate_plan(model="agent-1", prompt="编排", cwd=Path("/tmp"))

        self.assertEqual(result["plan"], plan)
        self.assertEqual(result["usage"], {"input_tokens": 90, "output_tokens": 30})
        create_args = run.call_args_list[0].args[0]
        self.assertIn("--description-stdin", create_args)
        self.assertIn("--assignee-id", create_args)

    def test_multica_cancels_remote_run_when_local_job_is_cancelled(self):
        provider = MulticaCli(Path("/tmp/multica"))
        provider._model_cache = (time.monotonic(), [("agent-1", "剪辑师")])
        responses = [
            ({"issue": {"id": "issue-1"}}, ""),
            ({"runs": [{"task_id": "task-1", "status": "running"}]}, ""),
        ]
        cancelled = iter([False, True])
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "_run_json", side_effect=responses), \
                patch.object(provider, "_cancel_task") as cancel, \
                patch("agent_video.ai.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "已取消"):
                provider.generate_plan(
                    model="agent-1", prompt="编排", cwd=Path("/tmp"),
                    should_cancel=lambda: next(cancelled),
                )
        cancel.assert_called_once_with("task-1", "issue-1", Path("/tmp"))


class FindObjectExtractionTest(unittest.TestCase):
    """_find_object 必须挑出模型答案，而不是被回显的请求 schema 骗到。"""

    SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"decisions": {"type": "array", "items": {"type": "object"}}},
        "required": ["decisions"],
    }

    def test_finds_answer_nested_in_response_string(self):
        envelope = {"status": "SUCCESS",
                    "response": '{"decisions":[{"id":0,"usable":true,"reason":"ok"}]}',
                    "json_schema": self.SCHEMA}
        found = AntigravityCli._find_object(envelope, ("decisions",))
        self.assertEqual(found, {"decisions": [{"id": 0, "usable": True, "reason": "ok"}]})

    def test_schema_echo_alone_is_not_an_answer(self):
        envelope = {"conversation_id": "x", "status": "SUCCESS", "response": "",
                    "json_schema": self.SCHEMA}
        self.assertIsNone(AntigravityCli._find_object(envelope, ("decisions",)))

    def test_prefers_real_answer_over_schema_echo(self):
        envelope = {"json_schema": self.SCHEMA,
                    "result": {"decisions": [{"id": 1, "usable": False, "reason": "no"}]}}
        found = AntigravityCli._find_object(envelope, ("decisions",))
        self.assertEqual(found, {"decisions": [{"id": 1, "usable": False, "reason": "no"}]})

    def test_finds_answer_in_multi_json_response_string(self):
        text = '{"decisions":[{"id":0,"usable":true,"reason":"ok"}]}\n{"toolAction":"done"}'
        envelope = {"status": "SUCCESS", "response": text, "json_schema": self.SCHEMA}
        found = AntigravityCli._find_object(envelope, ("decisions",))
        self.assertEqual(found, {"decisions": [{"id": 0, "usable": True, "reason": "ok"}]})

    def test_antigravity_command_includes_dangerously_skip_permissions(self):
        provider = AntigravityCli(Path("/tmp/agy"))
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "validate_model"), \
                patch.object(provider, "_complete", return_value=(
                    '{"structured_output":{"decisions":[{"id":1,"usable":true,"reason":"ok"}]}}', "", 1
                )) as complete:
            res = provider.generate_json(model="auto", prompt="test", schema=self.SCHEMA, cwd=Path("/tmp"))
        cmd = complete.call_args.args[0]
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(res["data"], {"decisions": [{"id": 1, "usable": True, "reason": "ok"}]})

    def test_antigravity_denied_actions_reported_in_error(self):
        from agent_video.ai import ProviderResponseError
        provider = AntigravityCli(Path("/tmp/agy"))
        with patch.object(provider, "_ensure_available"), \
                patch.object(provider, "validate_model"), \
                patch.object(provider, "_complete", return_value=(
                    json.dumps({"status": "SUCCESS", "response": "", "denied_actions": [{"action": "command"}]}), "", 1
                )):
            with self.assertRaises(ProviderResponseError) as ctx:
                provider.generate_json(model="auto", prompt="test", schema=self.SCHEMA, cwd=Path("/tmp"))
            self.assertIn("工具权限被拒绝", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
