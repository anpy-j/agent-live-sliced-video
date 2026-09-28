import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent_video.timeline import (
    TimelineSegment,
    VirtualTimeline,
    load_virtual_timeline,
    map_timeline_range,
    map_clause,
    extract_virtual_timeline_audio,
)


def test_timeline_segment_and_virtual_timeline():
    seg1 = TimelineSegment(
        segment_id="seg_1",
        source_path="video1.mp4",
        source_start=100.0,
        source_end=200.0,
        timeline_start=0.0,
        timeline_end=100.0,
        speed=1.0,
    )
    seg2 = TimelineSegment(
        segment_id="seg_2",
        source_path="video2.mp4",
        source_start=50.0,
        source_end=180.0,
        timeline_start=100.0,
        timeline_end=200.0,
        speed=1.3,
    )
    vt = VirtualTimeline(
        timeline_id="vt_test",
        title="Test Timeline",
        segments=[seg1, seg2],
    )

    assert vt.timeline_duration == 200.0
    assert vt.source_duration == 230.0  # 100 + 130
    assert len(vt.segments) == 2

    # to_dict & from_dict
    d = vt.to_dict()
    vt2 = VirtualTimeline.from_dict(d)
    assert vt2.timeline_id == "vt_test"
    assert vt2.title == "Test Timeline"
    assert len(vt2.segments) == 2
    assert vt2.segments[1].speed == 1.3


def test_jianying_draft_parsing_and_speed_calculation(tmp_path):
    draft_file = tmp_path / "draft_content.json"
    draft_data = {
        "materials": {
            "videos": [
                {
                    "id": "mat_v1",
                    "path": str(tmp_path / "raw_stream.mp4"),
                    "duration": 28800000000,  # 8 hours in microseconds
                }
            ]
        },
        "tracks": [
            {
                "type": "video",
                "segments": [
                    {
                        "id": "jianying_seg_1",
                        "material_id": "mat_v1",
                        "source_timerange": {
                            "start": 1938000000,  # 00:32:18 = 1938s in us
                            "duration": 390000000,  # 390s in us (1.3x speed of 300s)
                        },
                        "target_timerange": {
                            "start": 0,
                            "duration": 300000000,  # 300s (5 mins) in us
                        },
                    },
                    {
                        "id": "jianying_seg_2",
                        "material_id": "mat_v1",
                        "source_timerange": {
                            "start": 5000000000,  # 5000s
                            "duration": 600000000,  # 600s
                        },
                        "target_timerange": {
                            "start": 300000000,  # 300s
                            "duration": 600000000,  # 600s (1.0x speed)
                        },
                    },
                ],
            }
        ],
    }
    draft_file.write_text(json.dumps(draft_data, ensure_ascii=False), encoding="utf-8")

    vt = load_virtual_timeline(draft_file)
    assert len(vt.segments) == 2
    seg1 = vt.segments[0]
    assert seg1.segment_id == "片段1"
    assert seg1.source_start == 1938.0
    assert seg1.source_end == 2328.0  # 1938 + 390
    assert seg1.timeline_start == 0.0
    assert seg1.timeline_end == 300.0
    assert pytest.approx(seg1.speed, 0.001) == 1.3

    seg2 = vt.segments[1]
    assert seg2.source_start == 5000.0
    assert seg2.source_end == 5600.0
    assert seg2.timeline_start == 300.0
    assert seg2.timeline_end == 900.0
    assert pytest.approx(seg2.speed, 0.001) == 1.0


def test_user_specified_mapping_formula():
    """
    User scenario:
    - JianYing segment 1: timeline 0 - 300s, source_start 00:32:18 (1938s), speed 1.3x
    - AI selects 01:40 - 01:50 on timeline (100s - 110s)
    - Mapping should find:
      segment_id = 'seg-1'
      segment_offset = 100.0
      source_start = 1938 + 100 * 1.3 = 2068s (00:34:28)
      source_end = 2068 + (110 - 100) * 1.3 = 2081s (00:34:41)
    """
    seg1 = TimelineSegment(
        segment_id="seg-1",
        source_path="/path/to/raw_8h.mp4",
        source_start=1938.0,
        source_end=1938.0 + 390.0,
        timeline_start=0.0,
        timeline_end=300.0,
        speed=1.3,
    )
    vt = VirtualTimeline(
        timeline_id="vt_jianying_01",
        title="F家限定 / 时间线01 · 约 40 分钟",
        segments=[seg1],
    )

    slices = map_timeline_range(vt, 100.0, 110.0)
    assert len(slices) == 1
    mapping = slices[0]
    assert mapping["segment_id"] == "seg-1"
    assert pytest.approx(mapping["segment_offset"], 0.001) == 100.0
    assert pytest.approx(mapping["source_start"], 0.001) == 2068.0
    assert pytest.approx(mapping["source_end"], 0.001) == 2081.0
    assert pytest.approx(mapping["speed"], 0.001) == 1.3
    assert mapping["source_path"] == "/path/to/raw_8h.mp4"

    # Test map_clause injection
    clause = {
        "text": "测试切片句子",
        "start": 100.0,
        "end": 110.0,
    }
    mapped = map_clause(vt, clause)
    assert mapped["text"] == "测试切片句子"
    assert mapped["timeline_id"] == "vt_jianying_01"
    assert mapped["timeline_start"] == 100.0
    assert mapped["timeline_end"] == 110.0
    assert mapped["segment_id"] == "seg-1"
    assert pytest.approx(mapped["segment_offset"], 0.001) == 100.0
    assert pytest.approx(mapped["source_start"], 0.001) == 2068.0
    assert pytest.approx(mapped["source_end"], 0.001) == 2081.0
    assert pytest.approx(mapped["speed"], 0.001) == 1.3


def test_extract_virtual_timeline_audio(tmp_path):
    dummy_video = tmp_path / "video.mp4"
    dummy_video.write_bytes(b"dummy")
    seg = TimelineSegment(
        segment_id="s1",
        source_path=str(dummy_video),
        source_start=10.0,
        source_end=30.0,
        timeline_start=0.0,
        timeline_end=20.0,
        speed=1.0,
    )
    vt = VirtualTimeline(timeline_id="vt_audio", segments=[seg])

    with patch("subprocess.run") as mock_run, patch("shutil.copy2") as mock_copy:
        mock_run.return_value = MagicMock(returncode=0)
        out_wav = extract_virtual_timeline_audio(vt, tmp_path / "cache" / "virtual_audio.wav")
        assert Path(out_wav).name == "virtual_audio.wav"
        assert mock_run.called
        assert mock_copy.called


def test_build_virtual_segments_and_render_timeline():
    from agent_video.pipeline.render import build_virtual_segments

    seg1 = TimelineSegment(
        segment_id="片段1",
        source_path="/raw/cam1.mp4",
        source_start=1000.0,
        source_end=1500.0,
        timeline_start=0.0,
        timeline_end=500.0,
        speed=1.0,
    )
    seg2 = TimelineSegment(
        segment_id="片段2",
        source_path="/raw/cam2.mp4",
        source_start=2000.0,
        source_end=2260.0,
        timeline_start=500.0,
        timeline_end=700.0,
        speed=1.3,
    )
    vt = VirtualTimeline(
        timeline_id="vt_multi",
        segments=[seg1, seg2],
    )

    ordered_clauses = [
        {"id": 0, "start": 50.0, "end": 60.0, "text": "第一段话"},
        {"id": 1, "start": 520.0, "end": 530.0, "text": "第二段话"},
    ]

    segments = build_virtual_segments(ordered_clauses, vt)
    assert len(segments) == 2
    # Clause 0落在片段1
    s0 = segments[0]
    assert s0["id"] == 0
    assert s0["segment_id"] == "片段1"
    assert s0["source_path"] == "/raw/cam1.mp4"
    assert s0["source_start"] == 1050.0
    assert s0["source_end"] == 1060.0
    assert s0["speed"] == 1.0

    # Clause 1落在片段2 (offset = 520 - 500 = 20s, speed = 1.3 -> source_start = 2000 + 20 * 1.3 = 2026.0)
    s1 = segments[1]
    assert s1["id"] == 1
    assert s1["segment_id"] == "片段2"
    assert s1["source_path"] == "/raw/cam2.mp4"
    assert pytest.approx(s1["source_start"], 0.001) == 2026.0
    assert pytest.approx(s1["source_end"], 0.001) == 2039.0
    assert pytest.approx(s1["speed"], 0.001) == 1.3

