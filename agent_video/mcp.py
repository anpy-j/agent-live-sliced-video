from __future__ import annotations

import json
from typing import Any, Callable


PROTOCOL_VERSION = "2025-06-18"


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "create_video_job",
            "description": "创建一个直播切片任务并加入本地队列。",
            "inputSchema": {"type": "object", "properties": {
                "source_path": {"type": "string", "description": "本机视频绝对路径"},
                "subtitle_path": {"type": "string", "description": "可选字幕绝对路径；SRT/VTT/ASS/SSA/时间码 TXT，提供后跳过全片 ASR"},
                "title": {"type": "string"},
                "brief": {"type": "string"},
                "products": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                             "description": "商品名称；多商品按输入顺序组织内容"},
                "materials": {"type": "array", "items": {"type": "string"},
                              "description": "可选面料提示；留空由编排 Agent 判断"},
                "colors": {"type": "array", "items": {"type": "string"},
                           "description": "可选颜色提示；留空由编排 Agent 判断"},
                "delivery_mode": {"type": "string", "enum": ["merged", "segments"],
                                  "description": "merged 只输出唯一合并 MP4；segments 只输出独立片段"},
                "creative_strategy": {"type": "string", "enum": [
                    "auto", "selling", "tryon", "personality", "story", "visual"],
                    "description": "创作策略；auto 让 Agent 根据素材选择，不再强套固定销售结构"},
                "text_ai_model": {"type": "string", "description": "文本编排模型，如 workbuddy:auto、codex:gpt-5.6-sol 或 manual"},
                "visual_ai_model": {"type": "string", "description": "多模态混剪模型，如 workbuddy:glm-5v-turbo 或 codex:gpt-5.6-sol"},
            }, "required": ["source_path", "products"]},
        },
        {
            "name": "list_video_jobs",
            "description": "查看队列与历史切片任务。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_video_job",
            "description": "查看一个任务的全部流程节点、事件和产物。",
            "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        },
        {
            "name": "get_stage_packet",
            "description": "读取当前等待节点所需的精简决策包。",
            "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        },
        {
            "name": "submit_stage_payload",
            "description": "提交当前等待的 AI 文本编排决策，内容为 main_product+picks。后续原声锁定、多模态混剪和高清渲染由平台自动完成。",
            "inputSchema": {"type": "object", "properties": {
                "job_id": {"type": "string"},
                "payload": {"type": "object"},
            }, "required": ["job_id", "payload"]},
        },
        {
            "name": "retry_video_job",
            "description": "修正输入后重新排队执行失败或等待中的任务。",
            "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        },
        {
            "name": "cancel_video_job",
            "description": "取消一个排队或运行中的任务。",
            "inputSchema": {"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
        },
    ]


class McpEndpoint:
    def __init__(self, invoke: Callable[[str, dict[str, Any]], Any]):
        self.invoke = invoke

    def handle(self, message: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return 202, None
        if method == "initialize":
            return 200, self._ok(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-live-sliced-video", "version": "0.1.0"},
            })
        if method == "ping":
            return 200, self._ok(request_id, {})
        if method == "tools/list":
            return 200, self._ok(request_id, {"tools": tool_specs()})
        if method == "tools/call":
            params = message.get("params") or {}
            try:
                value = self.invoke(params.get("name", ""), params.get("arguments") or {})
                text = json.dumps(value, ensure_ascii=False, indent=2)
                return 200, self._ok(request_id, {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": value if isinstance(value, dict) else {"result": value},
                    "isError": False,
                })
            except Exception as exc:
                return 200, self._ok(request_id, {
                    "content": [{"type": "text", "text": str(exc)}], "isError": True,
                })
        return 200, self._error(request_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _ok(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
