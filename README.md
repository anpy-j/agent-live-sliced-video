# Agent Live Sliced Video

一个本地优先、模型无关的直播切片 Agent。它把现有切片引擎包装为可排队、可观察、可恢复的任务系统，并通过 MCP 供 WorkBuddy、Codex、Antigravity、OpenCode 等客户端触发。

## 第一版能力

- 本地任务队列与独立工作区
- 五个真实生产节点的流程总览、执行事件、错误和结果
- 图片、JSON、日志和 MP4 产物预览
- LiveCut 可主动调用 WorkBuddy、Antigravity、Codex 或 OpenCode CLI 完成一次创意编排，也可回退为人工或外部 MCP Agent 决策
- Multica 可作为第五个 AI 提供方，通过工作区中的 Agent 发起编排 Run
- 新建任务和待编排节点都可选择 AI 提供方与模型
- 在线编辑并自动备份薄 Skill
- 带 Bearer Token 的 Streamable HTTP MCP 入口
- 兼容现有 `douyin-womenswear-slicing` 执行引擎
- SQLite 持久化，服务重启后恢复排队任务

## 启动

Web 管理端本身没有第三方依赖，Python 3.11 及以上可以直接启动：

```bash
python -m agent_video
```

打开 <http://127.0.0.1:8787>。MCP 地址为 `http://127.0.0.1:8787/mcp`，客户端配置与密钥可在「MCP 接入」页面复制。

首次使用切片引擎时，建议建立独立 Python 3.13 环境并安装底层引擎依赖。

macOS：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r /path/to/douyin-womenswear-slicing/requirements.txt
```

Windows PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r "D:\path\to\douyin-womenswear-slicing\requirements.txt"
```

原生视频文件选择器同时支持 macOS 和 Windows。Windows 使用系统自带的 PowerShell 打开文件对话框，并支持中文文件名和目录；Linux 客户端仍需填写素材的绝对路径。

如需使用 Multica，先安装 CLI 并执行 `multica setup`。随后在「系统设置」填写 Multica CLI 路径；Profile 和 Workspace ID 可留空以使用 Multica 当前默认值。模型选择器会列出工作区 Agent，并显示各 Agent 绑定的底层模型。

首次运行前，在「系统设置」确认底层切片引擎目录和引擎 Python。macOS 的虚拟环境解释器通常是 `.venv/bin/python`，Windows 通常是 `.venv\Scripts\python.exe`。

## 当前工作流

1. 页面或 MCP 创建任务。
2. 「素材索引」读取媒体、转写并生成精简候选摘要。
3. 「AI 音画编排」由 LiveCut 主动调用所选 CLI 模型，取得一个最强方案的 `picks`；AI 调用失败才进入人工决策。
4. 「校验与自动修复」执行时间线边界和画面结构规则校验，不再单独等待联系表审核。
5. 「低清粗剪与审片」在标准/精修模式生成 720p 粗剪并看实际视频确认；快速模式跳过独立粗剪，只渲染一次高清成片。
6. 「高清导出与 QC」仅在粗剪通过后读取已验证双轨时间线，直接高清渲染，不再重复抽帧、对齐或验证；最终 MP4 使用任务的成片名称。

粗剪和最终成片都是“钩子 + 完整正文”。底层引擎生成的“钩子 + 正文前 10 秒”文件只作为接缝预览，不会被登记为最终成片。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 安全

- 默认只监听 `127.0.0.1`。
- MCP 需要 Bearer Token。
- 素材使用绝对路径读取，不自动复制或上传。
- 不向 MCP 暴露任意 Shell 执行接口。
- WorkBuddy、Antigravity、Codex 与 OpenCode 编排进程均只接收候选摘要并返回结构化 JSON；Antigravity、Codex、OpenCode 在隔离临时目录中运行，Codex 使用只读沙箱，OpenCode 使用专用禁工具 Agent。
- AI 选段必须逐条来自候选摘要；伪造时间、改写原声或重复画面会在渲染前被拒绝。
