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
                "title": {"type": "string"},
                "brief": {"type": "string"},
                "mode": {"type": "string", "enum": ["fast", "standard", "refined"]},
                "ai_model": {"type": "string", "description": "如 workbuddy:auto、antigravity:gemini-3.1-pro-high、codex:gpt-5.6-sol、opencode:openai/gpt-5.6-sol 或 manual"},
            }, "required": ["source_path"]},
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
            "description": "提交当前等待节点的结构化决策。编排节点提交 main_product+picks；粗剪节点提交 verdict=approve。",
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
