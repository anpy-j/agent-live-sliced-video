---
name: live-sliced-video-agent
description: 通过本地 LiveCut Agent 创建、观察直播视频切片任务并获取成片；适用于把一段女装直播素材自动切成短视频，不用于直接手写 FFmpeg 命令。
---

# LiveCut Agent

通过已连接的 `live-slicer` MCP 服务管理直播切片。平台只有一条确定性优先的精简管线：语音转写与切分 → 规则粗筛 → AI 可用性判定 → AI 排序 → 渲染。Agent 只需创建任务并观察结果，不需要提交任何编排决策。

## 工作方式

1. 调用 `create_video_job` 创建任务，传入 `source_path`（本机视频绝对路径）与可选 `title`，保留返回的 `job_id`。
2. 使用 `get_video_job` 查看整体节点（`asr` → `filter` → `judge` → `order` → `render`）。运行中时不要重复创建任务。
3. 任务状态为 `completed` 表示成片已交付到 `deliverables/final.mp4`，可直接播放。
4. 失败时读取 `error` 与当前节点的事件，修正素材或系统设置后用 `retry_video_job` 重新开始；不要盲目重建任务。
5. 其它可用工具：`list_video_jobs`、`restart_video_job`、`cancel_video_job`、`delete_video_job`。

## 管线边界

- AI 只做两次无状态调用（可用性判定、排序），固定模型与固定 schema；任何非法返回都会明确报错并停止，没有多 provider 降级或本地兜底。
- 目标成片时长为 45–60 秒；素材不足或规则筛后无可用子句时任务会失败，而不是用弱内容填充。
- 规则粗筛会剔除违禁、价格/促销/库存、场控催拍、闲聊与重复内容。
- 步骤 5 换画面只保留渲染接缝 `select_visual`，当前不启用；分段导出与外部字幕复用不再提供。

## 创作边界

- 原声必须可追溯，不通过拼接制造原本不存在的事实、因果或承诺。
- 优先利用主播独特表达与临场情绪，不机械套用固定钩子结构。
- 职责分工：第三方 Agent 负责触发与观察，创意取舍由平台内的两次 AI 调用完成，渲染由本地引擎完成。
- 完整进度与产物以本地工作台为准，不依赖对话上下文保存状态。
- 任务标题就是最终 MP4 文件名；不要擅自改名。
