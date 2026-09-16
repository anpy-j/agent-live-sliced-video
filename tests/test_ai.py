import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_video.ai import AntigravityCli, CodexCli, OpenCodeCli, WorkBuddyCli


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

    def test_opencode_models_are_curated_from_installed_models(self):
        provider = OpenCodeCli(Path("/tmp/opencode"))
        OpenCodeCli._model_cache = None
        result = Mock(stdout="openai/gpt-5.6-sol\nunknown/example\nopencode-go/glm-5.3\n")
        with patch.object(Path, "is_file", return_value=True), \
                patch("agent_video.ai.os.access", return_value=True), \
                patch("agent_video.ai.subprocess.run", return_value=result):
            models = provider.models()
        self.assertIn(("openai/gpt-5.6-sol", "OpenAI · GPT-5.6 Sol"), models)
        self.assertIn(("opencode-go/glm-5.3", "OpenCode Go · GLM 5.3"), models)
        self.assertNotIn(("unknown/example", "unknown/example"), models)

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


if __name__ == "__main__":
    unittest.main()
