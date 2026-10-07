import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_video.pipeline import run
from agent_video.pipeline.ai import ORDER_SCHEMA
from agent_video.pipeline.errors import AIReturnError
from agent_video.pipeline.semantic import GROUP_SCHEMA, group_prompt, validate_groups


def candidate(cid, text):
    return {"id": cid, "text": text, "start": cid * 5., "end": cid * 5. + 4.,
            "members": [cid]}


def group(ids, keep, fact="羊毛含量"):
    return {"ids": ids, "keep_id": keep, "fact": fact, "reason": "完整具体"}


class SemanticSelectionTest(unittest.TestCase):
    def setUp(self):
        self.candidates = [candidate(1, "百分百羊毛"), candidate(2, "纯羊毛"),
                           candidate(3, "贴身不扎皮肤"), candidate(4, "领子能拆"),
                           candidate(5, "袖口能拆")]

    def test_equivalent_fact_removed_complementary_facts_retained(self):
        selected, audit = validate_groups({"groups": [group([1, 2], 2),
            group([3], 3, "不扎"), group([4], 4, "领子可拆"),
            group([5], 5, "袖口可拆")]}, self.candidates)
        self.assertEqual([c["id"] for c in selected], [2, 3, 4, 5])
        self.assertEqual(audit["deletions"][0]["keep_id"], 2)

    def test_malformed_grouping_fails_closed(self):
        valid = [group([1, 2], 2), group([3], 3), group([4], 4), group([5], 5)]
        variants = [valid[:-1], [group([1, 2], 99)] + valid[1:],
                    valid + [group([1], 1)], [group([True, 2], 2)] + valid[1:],
                    [group([1, 2, 99], 2)] + valid[1:]]
        for groups in variants:
            with self.subTest(groups=groups), self.assertRaises(AIReturnError):
                validate_groups({"groups": groups}, self.candidates)

    def test_short_output_accepted_even_when_material_is_abundant(self):
        order = {"main_product": "毛衣", "sections": [{"role": "hook", "ids": [1]}],
                 "ordered_ids": [1]}
        self.assertEqual(run._validate_order(order, self.candidates, (120., 180.),
                                             1., 500.)[2], 4.)

    def test_full_order_applies_selection_and_cross_section_review(self):
        selection = {"groups": [group([1, 2], 2), group([3], 3),
                                  group([4], 4), group([5], 5)]}
        order = {"main_product": "毛衣", "sections": [
            {"role": "hook", "ids": [2]}, {"role": "proof", "ids": [3]},
            {"role": "styling", "ids": [4, 5]}], "ordered_ids": [2, 3, 4, 5]}
        # Force a second-pass duplicate to verify actual removal across roles.
        review = {"groups": [group([2, 3], 3), group([4], 4), group([5], 5)]}
        before = copy.deepcopy(self.candidates)
        def call(model, prompt, schema, timeout):
            if schema is ORDER_SCHEMA:
                payload = json.loads(prompt.split("JSON）：\n")[-1])
                self.assertEqual([c["id"] for c in payload], [2, 3, 4, 5])
                return copy.deepcopy(order)
            self.assertIs(schema, GROUP_SCHEMA)
            return review if "跨段落" in prompt else selection
        with tempfile.TemporaryDirectory() as workdir, patch.object(run, "ai_call", side_effect=call):
            final = run._semantic_order(self.candidates, "auto", (120., 180.), None,
                                         1., 10, workdir)
            self.assertEqual(final["ordered_ids"], [3, 4, 5])
            self.assertEqual(final["sections"][0], {"role": "hook", "ids": [3]})
            self.assertEqual(run._validate_order(final, self.candidates, (120., 180.), 1.)[2], 12.)
            audit = json.loads(Path(workdir, "semantic-selection.json").read_text("utf8"))
            self.assertEqual(audit["review"]["deletions"][0]["id"], 2)
        self.assertEqual(self.candidates, before)

    def test_prompt_protects_new_information_and_numeric_conflicts(self):
        prompt = group_prompt(self.candidates)
        self.assertIn("数值冲突", prompt)
        self.assertIn("新增信息", prompt)
        self.assertIn("不预设或固定商品名称", prompt)


if __name__ == "__main__":
    unittest.main()
