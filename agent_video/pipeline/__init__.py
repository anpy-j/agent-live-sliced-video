# -*- coding: utf-8 -*-
"""精简切片管线（MVP）。

一条确定性优先的流水线：ASR → 规则筛 → 2 次无状态 AI → 按时间戳切 → 渲染。
旧 ``edit_plan``/``validation`` 编排保留不动，这里用独立入口切换。

    from agent_video.pipeline import run_pipeline

步骤 5（换画面）不在本 MVP；渲染层预留了 ``select_visual_fn`` 接缝。
"""
from .ai import DECISION_SCHEMA, ORDER_SCHEMA, ai_call
from .errors import (AIReturnError, AsrError, PipelineConfigError, PipelineError,
                     RenderError, RuleFilterEmpty, TargetUnreachable)
from .filter import filter_clauses
from .render import build_segments, render_video, select_visual
from .run import run_pipeline
from .split import split_clauses

__all__ = [
    "AIReturnError",
    "AsrError",
    "DECISION_SCHEMA",
    "ORDER_SCHEMA",
    "PipelineConfigError",
    "PipelineError",
    "RenderError",
    "RuleFilterEmpty",
    "TargetUnreachable",
    "ai_call",
    "build_segments",
    "filter_clauses",
    "render_video",
    "run_pipeline",
    "select_visual",
    "split_clauses",
]
