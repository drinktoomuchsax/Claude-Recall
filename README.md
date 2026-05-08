# Claude Recall

> **把人带回 Claude Code 的工作回路中。**

Claude Recall 实时追踪你所有 Claude Code session 的状态，并通过灯光、看板、手机推送等方式提醒你：Claude 完成了、需要权限了、出错了 — 不用一直盯着终端。

> [English Version](./README_EN.md)

![Dashboard - 多 session 实时看板](./docs/assets/dashboard-multi.png)

## 它能做什么

当你让 Claude Code 在后台跑任务时：

- **Claude 完成了** → 橙色提醒，灯亮/通知响
- **Claude 需要权限** → 紫色闪烁，你不批准它就卡着
- **Claude 出错了** → 红色警告
- **Claude 在工作** → 蓝色呼吸，安心等着就好

支持同时追踪多个 Claude Code session，每个独立显示状态。

## 30 秒上手

```bash
# 1. 克隆
git clone https://github.com/yourname/Claude-Recall.git
cd Claude-Recall

# 2. 安装
uv sync

# 3. 配置 Claude Code hooks（全局生效，所有项目所有 session）
mkdir -p ~/.claude-recall/hooks
cp hooks/emit.py ~/.claude-recall/hooks/emit.py
```

然后把以下内容合并到你的 `~/.claude/settings.json`：

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "Notification": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "python3 ~/.claude-recall/hooks/emit.py"}]}]
  }
}
```

**搞定！** Daemon 会在第一次 hook 触发时自动启动，无需手动管理。

## 看板

打开 Web Dashboard 实时查看所有 session 状态：

```bash
cd receivers/web-dashboard
npm install && npx vite
```

浏览器打开 `http://localhost:5173`。

![Dashboard - 状态图例和橱窗](./docs/assets/dashboard.png)

每个 Claude Code session 显示为一个独立的"商店橱窗"，不同 session 有不同的主题风格。Claude 的状态通过颜色和动画实时反映：

| 状态 | 颜色 | 动画 | 含义 |
|------|------|------|------|
| Idle | 暗绿 | — | 会话存在，无事发生 |
| Working | 蓝 | 旋转 | Claude 在思考 |
| Tool Active | 亮蓝 | 旋转 | 正在执行工具 |
| Awaiting Input | 橙 | 弹跳 | **完成了，等你来** |
| Needs Permission | 紫 | 脉冲发光 | **被阻塞，需要你批准** |
| Notification | 浅紫 | 脉冲发光 | Claude 有消息 |
| Error | 红 | 抖动 | 出错了 |

## 终端监控

不想开浏览器？用 CLI 实时看：

```bash
uv run claude-recall watch --mode all
```

![Terminal Watch](./docs/assets/terminal-watch.png)

## 架构

```
Claude Code ──stdin JSON──▶ emit.py ──POST──▶ Core Daemon ──broadcast──▶ Receivers
                                                   │
                                                   ├── WebSocket (看板/App 连接)
                                                   ├── Serial (USB 灯)
                                                   ├── MQTT (IoT 设备)
                                                   └── Terminal (bell/title)
```

- **emit.py** — 零依赖 shim，读取 Claude Code hook 的 stdin（含 session_id），转发给 daemon。首次触发自动拉起 daemon。
- **Core Daemon** — 维护每个 session 的状态机，广播状态帧。不关心颜色/声音，只算状态。
- **Receivers** — 各自连接 daemon，自己决定怎么呈现状态（颜色、动画、声音）。

## 仓库结构

```
core/                状态机 + 广播 daemon (Python)
hooks/               Claude Code hook 对接
receivers/
  └── web-dashboard/ 浏览器看板 (React + TypeScript)
  └── (more)         USB 灯、Flutter App、WLED...
docs/
  └── protocol.md    状态帧协议（给 receiver 开发者看）
```

## CLI 命令

```bash
claude-recall daemon              # 启动 daemon（一般不需要手动，hook 会自动拉起）
claude-recall status              # 查看聚合状态
claude-recall sessions            # 列出所有活跃 session
claude-recall watch [--mode all]  # 实时监控
claude-recall test <state> [-s id] # 测试状态转换
```

## 开发一个 Receiver

连接 WebSocket，解析 JSON 状态帧：

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:8765/ws") as ws:
        async for msg in ws:
            frame = json.loads(msg)
            print(f"State: {frame['state']}")

asyncio.run(main())
```

详见 [docs/protocol.md](docs/protocol.md)。

## License

MIT
