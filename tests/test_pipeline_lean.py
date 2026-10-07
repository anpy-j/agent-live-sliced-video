# -*- coding: utf-8 -*-
"""精简管线单元测试：S1 切分、S2 粗筛、S3/S4 契约校验、AI 原语选择。"""
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from agent_video.engine.scripts import badvocab
from agent_video.engine.scripts.asr_backend import resolve_cpu_threads
from agent_video.pipeline import ai as pipeline_ai
from agent_video.pipeline.ai import DECISION_SCHEMA, ORDER_SCHEMA
from agent_video.pipeline.errors import AIReturnError, PipelineConfigError
from agent_video.pipeline.filter import (activate_account_vocab, filter_clauses,
                                         normalize)
from agent_video.pipeline import run as pipeline_run
from agent_video.pipeline.run import (_judge_batches, _judge_concurrency,
                                      _judge_prompt, _judge_retries,
                                      _next_render_output, _order_prompt,
                                      _run_judge_batches, _safe_output_stem,
                                      _validate_decisions, _validate_order,
                                      next_available_dir)
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

    def test_brand_names_are_hard_blocked(self):
        rows = filter_clauses([
            self.clause(0, "对这件衣服就是今年香奈儿的最新款"),
            self.clause(1, "专门打迪奥的外套"),
            self.clause(2, "它是做到的一个羊毛小香"),
            self.clause(3, "这件羊毛马甲版型很正"),
            self.clause(4, "这是一件小香风的外套"),
            self.clause(5, "小香家同款"),
        ])
        self.assertFalse(rows[0]["usable"])
        self.assertEqual(rows[0]["reason"], "hard_vocab")
        self.assertFalse(rows[1]["usable"])
        self.assertEqual(rows[1]["reason"], "hard_vocab")
        self.assertFalse(rows[2]["usable"])
        self.assertEqual(rows[2]["reason"], "hard_vocab")
        self.assertTrue(rows[3]["usable"])
        self.assertTrue(rows[4]["usable"])
        self.assertFalse(rows[5]["usable"])
        self.assertEqual(rows[5]["reason"], "hard_vocab")

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


class AccountVocabFilterTest(unittest.TestCase):
    """S2 必须应用账号级红线词表：价格/现货/发货/库存/尺码/优惠券等命中即硬禁。"""

    def clause(self, cid, text, start=0.0, end=2.0):
        return {"id": cid, "text": text, "start": start, "end": end,
                "usable": None, "reason": "", "order": None, "split_from": None}

    def setUp(self):
        activate_account_vocab()

    def tearDown(self):
        badvocab.set_profile({}, None)

    def test_account_redline_words_are_hard_blocked(self):
        rows = filter_clauses([
            self.clause(0, "这个价格真的很划算"),
            self.clause(1, "现在都是现货不用等"),
            self.clause(2, "拍下发货很快"),
            self.clause(3, "库存已经不多了"),
            self.clause(4, "尺码做的很正"),
            self.clause(5, "下单再送一张优惠券"),
        ])
        for row in rows:
            self.assertFalse(row["usable"], row["text"])
            self.assertEqual(row["reason"], "hard_vocab", row["text"])

    def test_normal_selling_points_stay_usable(self):
        rows = filter_clauses([
            self.clause(0, "这件马甲上身特别显瘦"),
            self.clause(1, "面料是百分百羊毛很软"),
            self.clause(2, "小香风的设计很高级"),
        ])
        for row in rows:
            self.assertTrue(row["usable"], row["text"])
            self.assertEqual(row["reason"], "", row["text"])

    def test_pipeline_activation_reports_strict_profile(self):
        self.assertTrue(str(activate_account_vocab()).endswith("douyin-strict.json"))

    def test_off_falls_back_to_plain_vocab(self):
        badvocab.set_profile({}, None)
        self.assertIsNone(activate_account_vocab("none"))
        rows = filter_clauses([self.clause(0, "这个价格真的很划算")])
        self.assertTrue(rows[0]["usable"])


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

    @staticmethod
    def order(sections, ordered_ids, main_product="马甲"):
        return {"main_product": main_product, "sections": sections,
                "ordered_ids": ordered_ids}

    def test_valid_order(self):
        main, ids, total, sections = _validate_order(
            self.order([{"role": "hook", "ids": [1]},
                        {"role": "selling_point", "ids": [0]}], [1, 0]),
            self.candidates(), (5.0, 7.0), 1.0)
        self.assertEqual(main, "马甲")
        self.assertEqual(ids, [1, 0])
        self.assertEqual(sections[0]["role"], "hook")
        self.assertAlmostEqual(total, 6.0)

    def test_below_target_is_accepted_when_available_is_short(self):
        candidates = [{"id": 0, "text": "a", "start": 0.0, "end": 3.0}]
        main, ids, total, sections = _validate_order(
            self.order([{"role": "hook", "ids": [0]}], [0]), candidates,
            (5.0, 7.0), 1.0, available=3.0)
        self.assertEqual(ids, [0])
        self.assertAlmostEqual(total, 3.0)

    def test_below_target_is_accepted_with_abundant_candidates(self):
        result = _validate_order(self.order([{"role": "hook", "ids": [0]}], [0]),
                                 self.candidates(), (5.0, 7.0), 1.0)
        self.assertEqual(result[2], 3.0)

    def test_out_of_range_id_is_rejected(self):
        with self.assertRaises(AIReturnError):
            _validate_order(self.order([{"role": "hook", "ids": [9]}], [9]),
                            self.candidates(), (5.0, 7.0), 1.0)

    def test_long_unit_may_push_total_over_the_upper_bound(self):
        candidates = [{"id": 0, "text": "a", "start": 0.0, "end": 12.0}]
        main, ids, total, sections = _validate_order(
            self.order([{"role": "hook", "ids": [0]}], [0]), candidates, (5.0, 7.0), 1.0)
        self.assertEqual(ids, [0])
        self.assertAlmostEqual(total, 12.0)

    def test_overshoot_beyond_long_unit_overflow_is_rejected(self):
        candidates = [{"id": 0, "text": "a", "start": 0.0, "end": 12.0},
                      {"id": 1, "text": "b", "start": 12.0, "end": 15.0}]
        with self.assertRaises(AIReturnError):
            _validate_order(self.order([{"role": "hook", "ids": [0, 1]}], [0, 1]),
                            candidates, (5.0, 7.0), 1.0)

    def test_hook_must_open_the_order(self):
        with self.assertRaises(AIReturnError):
            _validate_order(
                self.order([{"role": "selling_point", "ids": [0, 1]}], [0, 1]),
                self.candidates(), (5.0, 7.0), 1.0)

    def test_cta_must_close_the_order(self):
        candidates = [{"id": i, "text": str(i), "start": float(i), "end": float(i) + 2.0}
                      for i in range(3)]
        with self.assertRaises(AIReturnError):
            _validate_order(
                self.order([{"role": "hook", "ids": [0]},
                            {"role": "cta", "ids": [1]},
                            {"role": "proof", "ids": [2]}], [0, 1, 2]),
                candidates, (4.0, 8.0), 1.0)

    def test_sections_and_ordered_ids_must_agree(self):
        with self.assertRaises(AIReturnError):
            _validate_order(self.order([{"role": "hook", "ids": [0, 1]}], [1, 0]),
                            self.candidates(), (5.0, 7.0), 1.0)

    def test_invalid_role_is_rejected(self):
        with self.assertRaises(AIReturnError):
            _validate_order(self.order([{"role": "intro", "ids": [0, 1]}], [0, 1]),
                            self.candidates(), (5.0, 7.0), 1.0)


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
        self.assertEqual(ORDER_SCHEMA["required"],
                         ["main_product", "sections", "ordered_ids"])

    def test_sections_schema_translates_for_jev(self):
        questions = pipeline_ai._schema_to_questions(ORDER_SCHEMA)
        self.assertIn("sections", questions)
        parsed = pipeline_ai._answers_to_object(ORDER_SCHEMA, {"answers": {
            "main_product": {"text": "毛衣"},
            "sections": {"text": "hook:1,2;scene:3"},
            "ordered_ids": {"text": "1,2,3"},
        }})
        self.assertEqual(parsed["sections"],
                         [{"role": "hook", "ids": [1, 2]},
                          {"role": "scene", "ids": [3]}])
        self.assertEqual(parsed["ordered_ids"], [1, 2, 3])


class RenderOutputNameTest(unittest.TestCase):
    def test_default_stem_is_final(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(os.path.basename(_next_render_output(temp)), "final.mp4")

    def test_custom_stem_versions_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            os.makedirs(os.path.join(temp, "deliverables"))
            first = _next_render_output(temp, "伯恩夫人0926")
            self.assertEqual(os.path.basename(first), "伯恩夫人0926.mp4")
            open(first, "w", encoding="utf-8").close()
            second = _next_render_output(temp, "伯恩夫人0926")
            self.assertEqual(os.path.basename(second), "伯恩夫人0926-1.mp4")
            open(second, "w", encoding="utf-8").close()
            third = _next_render_output(temp, "伯恩夫人0926")
            self.assertEqual(os.path.basename(third), "伯恩夫人0926-2.mp4")

    def test_unsafe_stem_is_sanitized(self):
        self.assertEqual(_safe_output_stem('a/b:c*?"<>|'), "a-b-c")
        self.assertEqual(_safe_output_stem("  ... "), "final")
        self.assertEqual(_safe_output_stem(None), "final")

    def test_next_available_dir_increments(self):
        with tempfile.TemporaryDirectory() as temp:
            first = next_available_dir(temp, "伯恩夫人0926")
            self.assertEqual(os.path.basename(first), "伯恩夫人0926")
            os.makedirs(first)
            second = next_available_dir(temp, "伯恩夫人0926")
            self.assertEqual(os.path.basename(second), "伯恩夫人0926-1")


class ProductPromptTest(unittest.TestCase):
    def test_judge_prompt_carries_target_product(self):
        units = [{"id": 0, "members": [{"id": 1, "text": "这条裤子很好看"}]}]
        self.assertIn("主商品是「毛衣」", _judge_prompt(units, "毛衣"))
        self.assertIn("主商品未知", _judge_prompt(units, None))

    def test_order_prompt_carries_target_product(self):
        cands = [{"id": 0, "text": "a", "start": 0.0, "end": 3.0}]
        self.assertIn("固定为「毛衣」", _order_prompt(cands, (5.0, 7.0), "毛衣"))
        self.assertIn("main_product 给出本片主商品", _order_prompt(cands, (5.0, 7.0), None))

    def test_order_prompt_warns_when_material_below_target(self):
        cands = [{"id": 0, "text": "a", "start": 0.0, "end": 3.0}]
        self.assertIn("已低于目标下限", _order_prompt(cands, (5.0, 7.0), None, 3.0))
        self.assertNotIn("已低于目标下限", _order_prompt(cands, (5.0, 7.0), None, 30.0))


class AsrThreadsTest(unittest.TestCase):
    def test_default_uses_all_cores_minus_two(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOUYIN_WHISPER_THREADS", None)
            self.assertEqual(resolve_cpu_threads(20), 18)
            self.assertEqual(resolve_cpu_threads(2), 1)

    def test_env_override_wins(self):
        with patch.dict(os.environ, {"DOUYIN_WHISPER_THREADS": "12"}):
            self.assertEqual(resolve_cpu_threads(20), 12)

    def test_invalid_env_falls_back(self):
        with patch.dict(os.environ, {"DOUYIN_WHISPER_THREADS": "abc"}):
            self.assertEqual(resolve_cpu_threads(20), 18)


class JudgeConcurrencyTest(unittest.TestCase):
    def unit(self, uid, size):
        return {"id": uid,
                "members": [{"id": uid * 100 + i, "text": f"句{uid * 100 + i}"}
                            for i in range(size)]}

    def fake_ai_call(self, model, prompt, schema, timeout):
        ids = [int(value) for value in re.findall(r'"id":\s*(\d+)', prompt)]
        return {"decisions": [{"id": cid, "usable": cid % 2 == 0, "reason": "fake"}
                              for cid in ids]}

    def test_concurrency_config_falls_back_to_serial(self):
        self.assertEqual(_judge_concurrency(None), 1)
        self.assertEqual(_judge_concurrency("abc"), 1)
        self.assertEqual(_judge_concurrency(0), 1)
        self.assertEqual(_judge_concurrency(-3), 1)
        self.assertEqual(_judge_concurrency(4), 4)
        self.assertEqual(_judge_concurrency("8"), 8)

    def test_concurrent_merges_every_id_exactly_once(self):
        batches = [list(_judge_batches([self.unit(i, 30)], 120))[0] for i in range(10)]
        with patch.object(pipeline_run, "ai_call", side_effect=self.fake_ai_call):
            serial, serial_calls = _run_judge_batches("auto", batches, None, 5, 1)
            concurrent, concurrent_calls = _run_judge_batches("auto", batches, None, 5, 4)
        expected = {uid * 100 + i for uid in range(10) for i in range(30)}
        self.assertEqual(set(serial), expected)
        self.assertEqual(set(concurrent), expected)
        self.assertEqual(serial, concurrent)
        self.assertEqual((serial_calls, concurrent_calls), (10, 10))

    def test_concurrent_propagates_batch_failure(self):
        batches = [list(_judge_batches([self.unit(i, 10)], 120))[0] for i in range(4)]

        def flaky(model, prompt, schema, timeout):
            if '"id": 100' in prompt:
                raise AIReturnError("S3 返回缺少 decisions 数组")
            return self.fake_ai_call(model, prompt, schema, timeout)

        with patch.object(pipeline_run, "ai_call", side_effect=flaky):
            with self.assertRaises(AIReturnError):
                _run_judge_batches("auto", batches, None, 5, 4)

    def test_judge_retries_config_falls_back(self):
        self.assertEqual(_judge_retries(None), 1)
        self.assertEqual(_judge_retries("abc"), 1)
        self.assertEqual(_judge_retries(-1), 1)
        self.assertEqual(_judge_retries(0), 0)
        self.assertEqual(_judge_retries(2), 2)

    def test_retry_recovers_transient_missing_id(self):
        batches = [list(_judge_batches([self.unit(i, 5)], 120))[0] for i in range(3)]
        seen = {}

        def flaky(model, prompt, schema, timeout):
            unit = re.search(r'"unit":\s*(\d+)', prompt).group(1)
            seen[unit] = seen.get(unit, 0) + 1
            if unit == "1" and seen[unit] == 1:
                ids = [int(value) for value in re.findall(r'"id":\s*(\d+)', prompt)]
                return {"decisions": [{"id": cid, "usable": True, "reason": "x"}
                                      for cid in ids[:-1]]}  # 首次漏掉一个 id
            return self.fake_ai_call(model, prompt, schema, timeout)

        with patch.object(pipeline_run, "ai_call", side_effect=flaky):
            decisions, calls = _run_judge_batches("auto", batches, None, 5, 2, 1)
        self.assertEqual(len(decisions), 15)
        self.assertEqual(calls, 4)  # 3 批 + 1 次重试

    def test_retry_zero_still_raises_and_counts_attempt(self):
        batches = [list(_judge_batches([self.unit(0, 5)], 120))[0]]

        def bad(model, prompt, schema, timeout):
            return {"decisions": []}

        with patch.object(pipeline_run, "ai_call", side_effect=bad):
            with self.assertRaises(AIReturnError):
                _run_judge_batches("auto", batches, None, 5, 1, 0)

    def test_env_defaults_to_serial(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PIPELINE_JUDGE_CONCURRENCY", None)
            self.assertEqual(_judge_concurrency(os.environ.get("PIPELINE_JUDGE_CONCURRENCY")), 1)
        with patch.dict(os.environ, {"PIPELINE_JUDGE_CONCURRENCY": "4"}):
            self.assertEqual(_judge_concurrency(os.environ["PIPELINE_JUDGE_CONCURRENCY"]), 4)


if __name__ == "__main__":
    unittest.main()
