import unittest

from agent_video.ai import WorkBuddyCli


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


if __name__ == "__main__":
    unittest.main()
