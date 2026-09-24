# -*- coding: utf-8 -*-
"""精简管线的失败分类。

每一步只抛自己那一类的错误，调用方据此区分「规则筛空 / AI 报错 / 渲染失败」，
不会把某一步的失败伪装成另一步，也不做静默兜底。
"""


class PipelineError(RuntimeError):
    """精简管线的基类错误。"""

    stage = "pipeline"


class PipelineConfigError(PipelineError):
    """配置缺失或非法（例如选了不存在的 AI 引擎/提供方）。"""

    stage = "config"


class AsrError(PipelineError):
    """S1 转写/子句切分失败，没有产出可用的词级边界。"""

    stage = "asr"


class RuleFilterEmpty(PipelineError):
    """S2 规则粗筛后没有任何可用子句。"""

    stage = "rules"


class AIReturnError(PipelineError):
    """任一次 AI 调用返回不合契约（非 JSON / 缺 id / 时长不达标）。"""

    stage = "ai"


class TargetUnreachable(PipelineError):
    """AI 判定后可用子句总时长低于目标下限，S4 无论如何排不出达标成片。"""

    stage = "target"


class RenderError(PipelineError):
    """S6 ffmpeg 渲染失败。"""

    stage = "render"
