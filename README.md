# Agent Live Sliced Video

一个本地优先、模型无关的直播切片 Agent。它把现有切片引擎包装为可排队、可观察、可恢复的任务系统，并通过 MCP 供 WorkBuddy、Codex、Antigravity、OpenCode 等客户端触发。

## 第一版能力

- 本地任务队列与独立工作区
- 五个真实生产节点的流程总览、执行事件、错误和结果
- 图片、JSON、日志和 MP4 产物预览
- 外部 Agent 只提交一次创意编排；第二次直接审看低清粗剪
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
2. 「素材索引」读取媒体、转写并生成精简候选摘要。
3. 「AI 音画编排」只调用一次外部 Agent，提交一个最强方案的 `picks`。
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
