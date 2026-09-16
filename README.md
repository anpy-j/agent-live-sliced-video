# Agent Live Sliced Video

一个本地优先、模型无关的直播切片 Agent。它把现有切片引擎包装为可排队、可观察、可恢复的任务系统，并通过 MCP 供 WorkBuddy、Codex、Antigravity、OpenCode 等客户端触发。

## 第一版能力

- 本地任务队列与独立工作区
- 九节点流程总览、执行事件、错误和结果
- 图片、JSON、日志和 MP4 产物预览
- 外部 Agent 在创意选段和画面复核节点提交结构化决策
- 在线编辑并自动备份薄 Skill
- 带 Bearer Token 的 Streamable HTTP MCP 入口
- 兼容现有 `douyin-womenswear-slicing` 执行引擎
- SQLite 持久化，服务重启后恢复排队任务

## 启动

Web 管理端本身没有第三方依赖。首次使用切片引擎时，先建立独立 Python 3.13 环境：

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r /Volumes/MacData/Users/anpy/develop/personal/自媒体/切片/douyin-womenswear-slicing/requirements.txt
```

然后启动：

```bash
python3 -m agent_video
```

打开 <http://127.0.0.1:8787>。MCP 地址为 `http://127.0.0.1:8787/mcp`，客户端配置与密钥可在「MCP 接入」页面复制。

首次运行前，在「系统设置」确认底层切片引擎路径。默认值指向：

```text
/Volumes/MacData/Users/anpy/develop/personal/自媒体/切片/douyin-womenswear-slicing
```

## 当前工作流

1. 页面或 MCP 创建任务。
2. 本地工作进程读取素材并调用现有切片引擎完成转写、摘要和概览。
3. 任务进入「创意方向」等待节点；外部 Agent 获取精简决策包并提交 `picks`。
4. 引擎继续对齐和门禁，在需要时进入「画面复核」等待节点。
5. 外部 Agent 提交画面决策后继续渲染，成片和质检报告登记到页面。

第一版保留了现有引擎的生产能力；后续版本会把素材索引、创意提案、低清粗剪和成片审片拆成原生节点。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 安全

- 默认只监听 `127.0.0.1`。
- MCP 需要 Bearer Token。
- 素材使用绝对路径读取，不自动复制或上传。
- 不向 MCP 暴露任意 Shell 执行接口。
