import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.ai import AntigravityCli, CodexCli, MulticaCli, OpenCodeCli, WorkBuddyCli


class WorkBuddyCliTest(unittest.TestCase):
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

    def test_all_providers_share_structured_plan_parser(self):
        envelope = {"output_text": '{"main_product":"风衣","picks":[]}'}
        self.assertEqual(AntigravityCli._find_plan(envelope)["main_product"], "风衣")
        self.assertEqual(CodexCli._find_plan(envelope)["main_product"], "风衣")

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


if __name__ == "__main__":
    unittest.main()
