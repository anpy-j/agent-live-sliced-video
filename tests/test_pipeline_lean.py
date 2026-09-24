# -*- coding: utf-8 -*-
"""精简管线单元测试：S1 切分、S2 粗筛、S3/S4 契约校验、AI 原语选择。"""
import json
import os
import unittest
from unittest.mock import patch

from agent_video.pipeline import ai as pipeline_ai
from agent_video.pipeline.ai import DECISION_SCHEMA, ORDER_SCHEMA
from agent_video.pipeline.errors import AIReturnError, PipelineConfigError
from agent_video.pipeline.filter import filter_clauses, normalize
from agent_video.pipeline.run import _judge_batches, _validate_decisions, _validate_order
from agent_video.pipeline.split import split_clauses
from agent_video.pipeline.units import build_units, order_candidates


def word(text, start, end):
    return {"w": text, "s": start, "e": end}


class ProviderExecutableTest(unittest.TestCase):
    def test_workbuddy_fallback_prefers_existing_candidate(self):
        existing = pipeline_ai._FALLBACK_EXECUTABLES["workbuddy"][0]
        with patch.object(pipeline_ai.shutil, "which", return_value=None), \
                patch.object(pipeline_ai.os.path, "isfile",
                             side_effect=lambda path: path == existing), \
                patch.object(pipeline_ai.os, "access", return_value=True):
            self.assertEqual(pipeline_ai._provider_executable("workbuddy"), existing)

    def test_missing_provider_reports_primary_candidate(self):
        primary = pipeline_ai._FALLBACK_EXECUTABLES["workbuddy"][0]
        with patch.object(pipeline_ai.shutil, "which", return_value=None), \
                patch.object(pipeline_ai.os.path, "isfile", return_value=False):
            self.assertEqual(pipeline_ai._provider_executable("workbuddy"), primary)

    def test_path_lookup_wins_over_fallback(self):
        with patch.object(pipeline_ai.shutil, "which", return_value="/usr/bin/codebuddy"):
            self.assertEqual(pipeline_ai._provider_executable("workbuddy"),
                             "/usr/bin/codebuddy")


class SplitTest(unittest.TestCase):
    def test_sentence_punctuation_is_a_hard_boundary(self):
        words = [word("你好", 0.0, 1.2), word("世界。", 1.2, 2.4),
                 word("再来", 2.4, 3.6), word("一句。", 3.6, 4.8)]
        clauses = split_clauses(words)
        self.assertEqual([c["text"] for c in clauses], ["你好世界。", "再来一句。"])
        self.assertEqual([c["id"] for c in clauses], [0, 1])

    def test_silence_gap_splits_a_run_on(self):
        words = [word("前面", 0.0, 1.2), word("后面", 1.6, 2.8)]
        clauses = split_clauses(words)
        self.assertEqual(len(clauses), 2)
        self.assertEqual(clauses[0]["end"], 1.2)
        self.assertEqual(clauses[1]["start"], 1.6)

    def test_hard_cap_forces_a_split_without_sentence_punctuation(self):
        words = [word("一", 0.0, 2.0), word("二", 2.0, 4.0), word("三", 4.0, 6.5),
                 word("四", 6.5, 8.0)]
        clauses = split_clauses(words, max_duration=6.0)
        self.assertGreater(len(clauses), 1)
        self.assertTrue(all(c["end"] - c["start"] <= 6.0 + 1e-6 for c in clauses))

    def test_short_orphan_is_merged_into_the_neighbour(self):
        words = [word("主句内容", 0.0, 2.0), word("短", 2.4, 3.0)]
        clauses = split_clauses(words, min_duration=1.0)
        self.assertEqual(len(clauses), 1)
        self.assertEqual(clauses[0]["text"], "主句内容短")

    def test_two_sentences_are_never_merged_by_min_duration(self):
        words = [word("短。", 0.0, 0.4), word("下一句长内容。", 0.9, 3.0)]
        clauses = split_clauses(words, min_duration=1.0)
        self.assertEqual([c["text"] for c in clauses], ["短。", "下一句长内容。"])

    def test_contract_fields_are_present(self):
        clauses = split_clauses([word("内容。", 0.0, 1.5)])
        self.assertEqual(set(clauses[0]), {"id", "start", "end", "text", "usable",
                                           "reason", "order", "split_from"})
        self.assertIsNone(clauses[0]["usable"])
        self.assertIsNone(clauses[0]["order"])


class SentenceUnitTest(unittest.TestCase):
    def clause(self, cid, text, start=0.0, end=2.0, usable=True, unit=None):
        return {"id": cid, "text": text, "start": start, "end": end,
                "usable": usable, "reason": "", "order": None, "split_from": None,
                "unit": unit}

    def test_contiguous_fragments_form_one_unit(self):
        clauses = [self.clause(0, "同样的是T恤", 0.0, 2.0),
                   self.clause(1, "我们会给到三到五年", 2.0, 4.0)]
        units = build_units(clauses, merge_min=4.0, merge_max=8.0, silence_gap=0.30)
        self.assertEqual(len(units), 1)
        self.assertEqual([m["id"] for m in units[0]["members"]], [0, 1])
        self.assertEqual(units[0]["text"], "同样的是T恤我们会给到三到五年")
        self.assertEqual([clause["unit"] for clause in clauses], [0, 0])

    def test_sentence_final_punctuation_hard_stops(self):
        clauses = [self.clause(0, "这句已经完整。", 0.0, 2.0),
                   self.clause(1, "下一句继续说", 2.0, 4.0)]
        units = build_units(clauses, merge_min=4.0)
        self.assertEqual(len(units), 2)

    def test_silence_gap_hard_stops(self):
        clauses = [self.clause(0, "前半句在这里", 0.0, 2.0),
                   self.clause(1, "后半句在别处", 2.5, 4.5)]
        units = build_units(clauses, merge_min=4.0, silence_gap=0.30)
        self.assertEqual(len(units), 2)

    def test_unit_never_exceeds_merge_max(self):
        clauses = [self.clause(0, "一", 0.0, 3.5),
                   self.clause(1, "二", 3.5, 6.5),
                   self.clause(2, "三", 6.5, 9.5)]
        units = build_units(clauses, merge_min=4.0, merge_max=8.0)
        self.assertEqual([m["id"] for m in units[0]["members"]], [0, 1])
        self.assertEqual([m["id"] for m in units[1]["members"]], [2])

    def test_order_candidates_split_around_unusable_member(self):
        clauses = [self.clause(0, "a", 0.0, 2.0, usable=True, unit=0),
                   self.clause(1, "b", 2.0, 3.0, usable=False, unit=0),
                   self.clause(2, "c", 3.0, 5.0, usable=True, unit=0),
                   self.clause(3, "d", 6.0, 8.0, usable=True, unit=1)]
        candidates = order_candidates(clauses)
        self.assertEqual([c["id"] for c in candidates], [0, 2, 3])
        self.assertEqual(candidates[0]["members"], [0])
        self.assertEqual(candidates[1]["members"], [2])


class JudgeBatchTest(unittest.TestCase):
    def unit(self, uid, size):
        return {"id": uid, "members": [{"id": uid * 100 + i} for i in range(size)]}

    def test_batches_are_capped_by_clause_count(self):
        units = [self.unit(0, 50), self.unit(1, 50), self.unit(2, 50)]
        batches = list(_judge_batches(units, 120))
        self.assertEqual([sum(len(u["members"]) for u in b) for b in batches], [100, 50])

    def test_oversized_unit_still_gets_its_own_batch(self):
        units = [self.unit(0, 10), self.unit(1, 200), self.unit(2, 10)]
        batches = list(_judge_batches(units, 120))
        self.assertEqual([sum(len(u["members"]) for u in b) for b in batches], [10, 200, 10])

    def test_every_clause_covered_exactly_once(self):
        units = [self.unit(i, 30) for i in range(10)]
        ids = [m["id"] for b in _judge_batches(units, 120) for u in b for m in u["members"]]
        self.assertEqual(sorted(ids), sorted(m["id"] for u in units for m in u["members"]))


class FilterTest(unittest.TestCase):
    def clause(self, cid, text, start=0.0, end=2.0):
        return {"id": cid, "text": text, "start": start, "end": end,
                "usable": None, "reason": "", "order": None, "split_from": None}

    def test_bad_vocab_is_removed(self):
        rows = filter_clauses([self.clause(0, "这条马甲的版型很正")])
        self.assertTrue(rows[0]["usable"])

    def test_price_and_live_chatter_blocked(self):
        rows = filter_clauses([
            self.clause(0, "拍它上链接"),
            self.clause(1, "专柜价要卖多少钱"),
            self.clause(2, "好"),
        ])
        self.assertFalse(rows[0]["usable"])
        self.assertIn(rows[0]["reason"], {"hard_vocab", "stage_chatter"})
        self.assertFalse(rows[1]["usable"])
        self.assertFalse(rows[2]["usable"])

    def test_duplicate_after_normalize_is_dropped(self):
        rows = filter_clauses([
            self.clause(0, "这件马甲很显瘦。"),
            self.clause(1, "这件马甲很显瘦！"),
        ])
        self.assertTrue(rows[0]["usable"])
        self.assertFalse(rows[1]["usable"])
        self.assertEqual(rows[1]["reason"], "duplicate")

    def test_duration_gate(self):
        rows = filter_clauses([self.clause(0, "短内容。", 0.0, 0.5)])
        self.assertFalse(rows[0]["usable"])
        self.assertEqual(rows[0]["reason"], "duration_gate")

    def test_normalize_strips_punctuation(self):
        self.assertEqual(normalize("马甲，很显瘦！"), normalize("马甲很显瘦"))


class DecisionContractTest(unittest.TestCase):
    def candidates(self):
        return [{"id": 0, "text": "a"}, {"id": 1, "text": "b"}]

    def test_valid_decisions_are_indexed_by_id(self):
        data = {"decisions": [{"id": 0, "usable": True, "reason": "ok"},
                              {"id": 1, "usable": False, "reason": "残句"}]}
        result = _validate_decisions(data, self.candidates())
        self.assertTrue(result[0]["usable"])
        self.assertFalse(result[1]["usable"])

    def test_missing_id_is_rejected(self):
        data = {"decisions": [{"id": 0, "usable": True, "reason": "ok"}]}
        with self.assertRaises(AIReturnError):
            _validate_decisions(data, self.candidates())

    def test_extra_id_is_ignored(self):
        data = {"decisions": [{"id": 0, "usable": True, "reason": "ok"},
                              {"id": 1, "usable": True, "reason": "ok"},
                              {"id": 2, "usable": True, "reason": "ok"}]}
        result = _validate_decisions(data, self.candidates())
        self.assertEqual(sorted(result), [0, 1])

    def test_gap_filling_extra_ids_are_ignored_but_missing_still_fatal(self):
        candidates = [{"id": 10, "text": "a"}, {"id": 12, "text": "b"}]
        ok = {"decisions": [{"id": 10, "usable": True, "reason": "ok"},
                            {"id": 11, "usable": True, "reason": "ok"},
                            {"id": 12, "usable": False, "reason": "ok"}]}
        self.assertEqual(sorted(_validate_decisions(ok, candidates)), [10, 12])
        bad = {"decisions": [{"id": 10, "usable": True, "reason": "ok"},
                             {"id": 11, "usable": True, "reason": "ok"}]}
        with self.assertRaises(AIReturnError):
            _validate_decisions(bad, candidates)

    def test_non_boolean_usable_is_rejected(self):
        data = {"decisions": [{"id": 0, "usable": "yes", "reason": "ok"},
                              {"id": 1, "usable": True, "reason": "ok"}]}
        with self.assertRaises(AIReturnError):
            _validate_decisions(data, self.candidates())


class OrderContractTest(unittest.TestCase):
    def candidates(self):
        return [{"id": 0, "text": "a", "start": 0.0, "end": 3.0},
                {"id": 1, "text": "b", "start": 3.0, "end": 6.0}]

    def test_valid_order(self):
        main, ids, total = _validate_order(
            {"main_product": "马甲", "ordered_ids": [1, 0]}, self.candidates(),
            (5.0, 7.0), 1.0)
        self.assertEqual(main, "马甲")
        self.assertEqual(ids, [1, 0])
        self.assertAlmostEqual(total, 6.0)

    def test_out_of_range_total_is_rejected(self):
        with self.assertRaises(AIReturnError):
            _validate_order({"main_product": "马甲", "ordered_ids": [0]},
                            self.candidates(), (5.0, 7.0), 1.0)

    def test_out_of_range_id_is_rejected(self):
        with self.assertRaises(AIReturnError):
            _validate_order({"main_product": "马甲", "ordered_ids": [9]},
                            self.candidates(), (5.0, 7.0), 1.0)

    def test_long_unit_may_push_total_over_the_upper_bound(self):
        candidates = [{"id": 0, "text": "a", "start": 0.0, "end": 12.0}]
        main, ids, total = _validate_order(
            {"main_product": "马甲", "ordered_ids": [0]}, candidates, (5.0, 7.0), 1.0)
        self.assertEqual(ids, [0])
        self.assertAlmostEqual(total, 12.0)

    def test_overshoot_beyond_long_unit_overflow_is_rejected(self):
        candidates = [{"id": 0, "text": "a", "start": 0.0, "end": 12.0},
                      {"id": 1, "text": "b", "start": 12.0, "end": 15.0}]
        with self.assertRaises(AIReturnError):
            _validate_order({"main_product": "马甲", "ordered_ids": [0, 1]},
                            candidates, (5.0, 7.0), 1.0)


class AiPrimitiveTest(unittest.TestCase):
    def test_unknown_engine_is_a_config_error(self):
        with patch.dict(os.environ, {"PIPELINE_AI_ENGINE": "nope"}):
            with self.assertRaises(PipelineConfigError):
                pipeline_ai.ai_call("auto", "p", DECISION_SCHEMA, 5)

    def test_llm_engine_routes_through_provider(self):
        with patch.object(pipeline_ai, "_call_llm",
                          return_value={"decisions": []}) as mocked:
            with patch.dict(os.environ, {"PIPELINE_AI_ENGINE": "llm"}):
                data = pipeline_ai.ai_call("auto", "p", DECISION_SCHEMA, 5)
        self.assertEqual(data, {"decisions": []})
        mocked.assert_called_once()

    def test_find_object_requires_all_keys(self):
        from agent_video.ai import CliProvider
        envelope = {"result": {"note": "x", "decisions": []}}
        self.assertIsNone(CliProvider._find_object(envelope, ("main_product", "ordered_ids")))
        self.assertEqual(CliProvider._find_object(envelope, ("decisions",))["decisions"], [])

    def test_schema_shapes_are_stable(self):
        self.assertEqual(DECISION_SCHEMA["required"], ["decisions"])
        self.assertEqual(ORDER_SCHEMA["required"], ["main_product", "ordered_ids"])


if __name__ == "__main__":
    unittest.main()
