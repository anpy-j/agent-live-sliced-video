# -*- coding: utf-8 -*-
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_video.engine.scripts.digest_candidates import (
    merge_short_units, should_merge_speech_units, dependency_metadata,
    TARGET_SPEECH, MAX_COMPLETE_SPEECH,
)
from agent_video.runner import JobRunner


class CandidateRecallAndReplenishTest(unittest.TestCase):
    def test_short_sentence_merging_into_speech_units(self):
        # 3 short chunks < 1.5s that form one natural selling point
        rows = [
            {"start": 10.0, "end": 10.8, "text": "同样的是T恤"},
            {"start": 10.8, "end": 12.0, "text": "我们会给到你三到五年没有任何变化"},
            {"start": 12.0, "end": 13.2, "text": "是因为我们整个的领子跟袖口"},
        ]
        merged = merge_short_units(rows, min_duration=1.5, max_duration=18.0, max_gap=0.40)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0]["start"], 10.0)
        self.assertAlmostEqual(merged[0]["end"], 13.2)
        self.assertIn("同样的是T恤", merged[0]["text"])
        self.assertIn("是因为我们整个的领子跟袖口", merged[0]["text"])
        self.assertGreaterEqual(merged[0]["end"] - merged[0]["start"], 1.5)

    def test_incomplete_ending_and_continuation_merging(self):
        rows = [
            {"start": 20.0, "end": 22.0, "text": "而且你会发现这种T恤，"},
            {"start": 22.1, "end": 23.5, "text": "在你整个的秋冬当中很好搭"},
        ]
        merged = merge_short_units(rows, min_duration=1.5, max_duration=18.0, max_gap=0.40)
        self.assertEqual(len(merged), 1)
        self.assertAlmostEqual(merged[0]["start"], 20.0)
        self.assertAlmostEqual(merged[0]["end"], 23.5)

    def test_isolated_short_fragment_without_neighbors_not_merged(self):
        rows = [
            {"start": 0.0, "end": 1.0, "text": "单个短句"},
            {"start": 10.0, "end": 14.0, "text": "这是很远之后的另一个正常完整句子"},
        ]
        merged = merge_short_units(rows, min_duration=1.5, max_duration=18.0, max_gap=0.40)
        self.assertEqual(len(merged), 2)
        self.assertAlmostEqual(merged[0]["end"] - merged[0]["start"], 1.0)

    def test_merge_target_keeps_units_inside_five_second_budget(self):
        self.assertLessEqual(TARGET_SPEECH, 5.0)
        self.assertLessEqual(MAX_COMPLETE_SPEECH, 8.0)
        # 三句各自 1.4 秒、彼此紧邻：旧上限 18 秒会把它们并成一个 4.2 秒长段；
        # 目标上限下仍允许并成一段（因为 4.2 <= 4.7），但不会跨过 5 秒门禁。
        rows = [{"start": index * 1.4, "end": index * 1.4 + 1.4,
                 "text": f"短句{index}"} for index in range(6)]
        merged = merge_short_units(rows, min_duration=1.5,
                                   max_duration=TARGET_SPEECH, max_gap=0.40)
        durations = [row["end"] - row["start"] for row in merged]
        self.assertTrue(durations)
        self.assertLessEqual(max(durations), TARGET_SPEECH + 1e-6)
        self.assertGreater(len(merged), 1)

    def test_anchored_editing_constraints_prevent_target_shrinkage(self):
        with TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            engine_work = workspace / "engine"
            engine_work.mkdir(parents=True)
            # Create a full candidate digest with 200s total duration
            full_digest = [
                {"i": 0, "s": 0.0, "e": 100.0, "t": "素材前半部分超长优质内容"},
                {"i": 1, "s": 100.0, "e": 200.0, "t": "素材后半部分超长优质内容"},
            ]
            (engine_work / "candidate_digest.json").write_text(
                json.dumps(full_digest, ensure_ascii=False), encoding="utf-8"
            )
            job = {"id": "test-job", "workspace": str(workspace), "target_min_seconds": 0, "target_max_seconds": 0}

            # 1. First calculation from full candidates
            initial_limits = JobRunner._editing_constraints(full_digest, job)
            self.assertEqual(initial_limits["min_total"], 70)
            self.assertEqual(initial_limits["max_total"], 120)

            # 2. Later mid-pipeline call with small filtered candidates (e.g. only 35s)
            small_subset = [
                {"i": 0, "s": 0.0, "e": 35.0, "t": "残余候选池只有35秒"},
            ]
            later_limits = JobRunner._editing_constraints(small_subset, job)
            # Target must NOT shrink to 35s or 22s!
            self.assertEqual(later_limits["min_total"], 70)
            self.assertEqual(later_limits["max_total"], 120)

    def test_tiered_candidate_pools_and_context_recall(self):
        candidates = [
            {"i": 0, "s": 10.0, "e": 15.0, "t": "这是高分主打卖点纯羊毛保暖不闷汗", "q": 10, "utterance_id": "u0", "atom_id": "u0:0"},
            {"i": 1, "s": 15.2, "e": 20.0, "t": "而且我们在领口这里做了细腻的双层包边", "q": 8, "utterance_id": "u1", "atom_id": "u1:0", "requires_previous": True, "required_atom_ids": ["u0:0"]},
            {"i": 2, "s": 30.0, "e": 35.0, "t": "喜欢的朋友点点关注进粉丝群领券", "q": 1, "utterance_id": "u2", "atom_id": "u2:0"},
            {"i": 3, "s": 40.0, "e": 48.0, "t": "早秋的时候你单穿或者搭配大衣都很有型", "q": 7, "utterance_id": "u3", "atom_id": "u3:0"},
        ]
        decisions = [
            {"candidate_id": 0, "verdict": "keep", "main_product_relevant": True, "selling_value": 85, "content_type": "selling_point", "content_function": "benefit"},
            {"candidate_id": 1, "verdict": "keep", "main_product_relevant": True, "selling_value": 40, "content_type": "craft", "content_function": "context"},  # recallable!
            {"candidate_id": 2, "verdict": "reject", "main_product_relevant": False, "selling_value": 0, "content_type": "stage_chatter", "content_function": "discard"},  # hard reject!
            {"candidate_id": 3, "verdict": "keep", "main_product_relevant": True, "selling_value": 45, "content_type": "styling", "content_function": "demonstration"},  # recallable!
        ]

        pools = JobRunner._partition_candidate_pools(decisions, candidates, target_min_duration=70.0)
        self.assertIn(0, pools["preferred_ids"])
        self.assertIn(2, pools["hard_rejected_ids"])
        self.assertNotIn(2, pools["kept_ids"])
        # Because preferred pool (candidate 0, 5s) < target_min_duration (70s),
        # recallable candidate 1 and 3 should be recalled!
        self.assertIn(1, pools["recalled_ids"])
        self.assertIn(3, pools["recalled_ids"])
        self.assertIn(1, pools["kept_ids"])
        self.assertIn(3, pools["kept_ids"])

    def test_validation_backfill_when_segment_deleted_or_duration_short(self):
        limits = {"min_total": 70, "max_total": 120, "min_segments": 3, "max_segments": 20}
        job = {"products": ["羊毛大衣"]}
        plan = {
            "main_product": "羊毛大衣",
            "picks": [
                {"src": 1, "start": 0.0, "end": 15.0, "text": "开头展示版型显瘦", "role": "hook", "module": "body", "_candidate_id": 10},
                {"src": 1, "start": 20.0, "end": 40.0, "text": "澳洲美丽奴纯羊毛", "role": "material", "module": "body", "_candidate_id": 11},
                {"src": 1, "start": 90.0, "end": 100.0, "text": "收尾闭眼入手", "role": "close", "module": "body", "_candidate_id": 12},
            ]
        }
        # Current duration is 15 + 20 + 10 = 45s < 70s
        candidates = [
            {"i": 10, "s": 0.0, "e": 8.0, "t": "开头展示版型显瘦", "q": 8},
            {"i": 11, "s": 20.0, "e": 28.0, "t": "澳洲美丽奴纯羊毛", "q": 9},
            {"i": 12, "s": 90.0, "e": 98.0, "t": "收尾闭眼入手", "q": 7},
            {"i": 13, "s": 50.0, "e": 58.0, "t": "手工双面呢暗线缝制工艺", "q": 10, "c": "craft"},
            {"i": 14, "s": 70.0, "e": 78.0, "t": "搭配阔腿裤或者半身裙都好看", "q": 9, "c": "styling"},
            {"i": 15, "s": 110.0, "e": 118.0, "t": "版型立体挺阔不挑身材", "q": 9, "c": "fit"},
            {"i": 16, "s": 130.0, "e": 138.0, "t": "小个子微胖都能轻松驾驭", "q": 9, "c": "fit"},
            {"i": 17, "s": 150.0, "e": 158.0, "t": "通勤约会都很提气质", "q": 8, "c": "scene"},
            {"i": 18, "s": 170.0, "e": 178.0, "t": "经典两色燕麦色与咖色", "q": 8, "c": "color"},
        ]
        quarantined_ids = set()
        repaired_plan, backfilled_ids = JobRunner._backfill_validation_plan(
            plan, candidates, limits, quarantined_ids, job
        )
        self.assertGreater(len(backfilled_ids), 0)
        total_duration = sum(float(p["end"]) - float(p["start"]) for p in repaired_plan["picks"])
        self.assertGreaterEqual(total_duration, 70.0)
        # Verify close remains the final segment
        self.assertEqual(repaired_plan["picks"][-1]["role"], "close")
        self.assertEqual(repaired_plan["picks"][-1]["_candidate_id"], 12)


if __name__ == "__main__":
    unittest.main()
