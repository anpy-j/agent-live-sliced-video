# -*- coding: utf-8 -*-
"""S2 标注工作台单元测试：词表热加载、补丁翻译、结构性命中不误改词表。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_video import labeling
from agent_video.engine.scripts import badvocab
from agent_video.pipeline.filter import filter_clauses, reject_hit


def clause(cid, text, start, end, usable=True, reason="", hit=None):
    return {"id": cid, "text": text, "start": start, "end": end,
            "usable": usable, "reason": reason, "hit": hit}


class ReloadProfileTest(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.get("DOUYIN_VOCAB_PROFILE")
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "label-overrides.json"
        self._orig = labeling.overrides_path
        labeling.overrides_path = lambda: self.path

    def tearDown(self):
        labeling.overrides_path = self._orig
        if self._env is None:
            os.environ.pop("DOUYIN_VOCAB_PROFILE", None)
        else:
            os.environ["DOUYIN_VOCAB_PROFILE"] = self._env
        badvocab.reload_profile()
        self._tmp.cleanup()

    def test_apply_patch_makes_new_word_reject(self):
        clauses = [clause(1, "这个面料贼舒服", 0.0, 2.0)]
        filter_clauses(clauses)
        self.assertTrue(clauses[0]["usable"])

        labeling.apply_patch({"hard_add": ["贼舒服"], "hard_remove": []})
        clauses2 = [clause(1, "这个面料贼舒服", 0.0, 2.0)]
        filter_clauses(clauses2)
        self.assertFalse(clauses2[0]["usable"])
        self.assertEqual(clauses2[0]["reason"], "hard_vocab")

    def test_remove_word_from_hard_list(self):
        labeling.apply_patch({"hard_add": [], "hard_remove": ["链接"]})
        clauses = [clause(1, "这里有链接", 0.0, 2.0)]
        filter_clauses(clauses)
        self.assertTrue(clauses[0]["usable"])

    def test_overrides_roundtrip(self):
        labeling.apply_patch({"hard_add": ["甲"], "hard_regex_add": [r"\d+次"]})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("甲", data["hard_add"])
        self.assertIn(r"\d+次", data["hard_regex_add"])


class BuildPatchTest(unittest.TestCase):
    def test_false_positive_needs_circled_word(self):
        clauses = [clause(1, "这个面料贼舒服", 0.0, 2.0, usable=True)]
        patch = labeling.build_patch(clauses, {"1": {"label": False, "tokens": ["贼舒服"]}})
        self.assertEqual(patch["hard_add"], ["贼舒服"])
        self.assertEqual(patch["unresolved"], [])

    def test_false_positive_without_token_is_unresolved(self):
        clauses = [clause(1, "这个面料贼舒服", 0.0, 2.0, usable=True)]
        patch = labeling.build_patch(clauses, {"1": {"label": False, "tokens": []}})
        self.assertEqual(patch["hard_add"], [])
        self.assertEqual(len(patch["unresolved"]), 1)

    def test_false_negative_removes_hit_word(self):
        clauses = [clause(1, "点开链接看详情", 0.0, 2.0, usable=False,
                          reason="hard_vocab", hit="链接")]
        patch = labeling.build_patch(clauses, {"1": {"label": True}})
        self.assertEqual(patch["hard_remove"], ["链接"])

    def test_structural_reject_cannot_be_patched(self):
        clauses = [clause(1, "短", 0.0, 0.4, usable=False, reason="duration_gate")]
        patch = labeling.build_patch(clauses, {"1": {"label": True}})
        self.assertEqual(patch["hard_remove"], [])
        self.assertEqual(len(patch["unresolved"]), 1)

    def test_regex_reject_cannot_be_patched(self):
        clauses = [clause(1, "先删掉这一段", 0.0, 2.0, usable=False, reason="stage_chatter")]
        patch = labeling.build_patch(clauses, {"1": {"label": True}})
        self.assertEqual(patch["hard_remove"], [])
        self.assertEqual(len(patch["unresolved"]), 1)

    def test_agreeing_decision_is_ignored(self):
        clauses = [clause(1, "点开链接看详情", 0.0, 2.0, usable=False, reason="hard_vocab",
                          hit="链接")]
        patch = labeling.build_patch(clauses, {"1": {"label": False}})
        self.assertEqual(patch["hard_remove"], [])
        self.assertEqual(patch["unresolved"], [])


class MergePatchTest(unittest.TestCase):
    def test_remove_wins_over_add(self):
        merged = labeling.merge_patch(
            {"hard_add": ["甲"], "hard_remove": []},
            {"hard_add": ["甲", "乙"], "hard_remove": ["甲"]})
        self.assertNotIn("甲", merged["hard_add"])
        self.assertIn("乙", merged["hard_add"])
        self.assertIn("甲", merged["hard_remove"])


class RejectHitTest(unittest.TestCase):
    def test_hard_vocab_returns_literal(self):
        self.assertEqual(reject_hit(clause(1, "点开链接", 0, 2, reason="hard_vocab")), "开链接")

    def test_structural_returns_none(self):
        self.assertIsNone(reject_hit(clause(1, "短", 0, 0.4, reason="duration_gate")))


if __name__ == "__main__":
    unittest.main()
