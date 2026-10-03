import unittest

from agent_video.pipeline.errors import AIReturnError
from agent_video.pipeline.remix import (build_clip_units, literal_deduplicate,
                                         validate_remix_plan)
from agent_video.timeline import TimelineSegment, VirtualTimeline


class RemixPipelineTest(unittest.TestCase):
    def timeline(self):
        return VirtualTimeline("collection", "集合时间线", [
            TimelineSegment("a", 0, 2, "/tmp/source.mp4", 10, 12),
            TimelineSegment("b", 3, 5, "/tmp/source.mp4", 20, 22),
        ])

    def test_build_clip_units_uses_existing_clip_boundaries(self):
        words = [
            {"w": "第一段", "s": 0.1, "e": 1.0},
            {"w": "第二段", "s": 2.1, "e": 3.0},
        ]
        clips = build_clip_units(self.timeline(), words)

        self.assertEqual([(clip["start"], clip["end"]) for clip in clips], [(0, 2), (3, 5)])
        self.assertEqual([clip["text"] for clip in clips], ["第一段", "第二段"])

    def test_literal_deduplicate_keeps_more_complete_text(self):
        clips = [
            {"id": 0, "text": "这个面料摸起来非常柔软", "seconds": 3.0,
             "usable": True, "reason": ""},
            {"id": 1, "text": "这个面料摸起来非常柔软舒服", "seconds": 4.0,
             "usable": True, "reason": ""},
            {"id": 2, "text": "可以搭配牛仔裤", "seconds": 2.0,
             "usable": True, "reason": ""},
        ]
        kept, duplicates = literal_deduplicate(clips, "strict")

        self.assertEqual({item["id"] for item in kept}, {1, 2})
        self.assertEqual([item["id"] for item in duplicates], [0])
        self.assertEqual(duplicates[0]["duplicate_of"], 1)
        self.assertEqual(duplicates[0]["reason"], "literal_duplicate")

    def test_validate_remix_plan_accepts_dynamic_categories(self):
        clips = [{"id": 1}, {"id": 2}, {"id": 3}]
        sections, ordered, duplicates = validate_remix_plan({
            "sections": [
                {"role": "穿着感受", "ids": [2]},
                {"role": "材质", "ids": [1]},
            ],
            "ordered_ids": [2, 1],
            "duplicate_ids": [3],
        }, clips)

        self.assertEqual([item["role"] for item in sections], ["穿着感受", "材质"])
        self.assertEqual(ordered, [2, 1])
        self.assertEqual(duplicates, [3])

    def test_validate_remix_plan_rejects_missing_clip(self):
        with self.assertRaises(AIReturnError):
            validate_remix_plan({
                "sections": [{"role": "材质", "ids": [1]}],
                "ordered_ids": [1],
                "duplicate_ids": [],
            }, [{"id": 1}, {"id": 2}])


if __name__ == "__main__":
    unittest.main()
