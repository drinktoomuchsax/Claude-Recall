# Multi-Host 协议演进路线图

本文档规划 Claude-Recall 从"单机协议"演进到"可级联多 daemon 协议"的完整路径，分 4 个 PR 落地。

## 设计决策对齐

在开始实施前，已对齐以下关键决策（详见本文末尾"决策对齐表"）：

| 维度 | 选择 | 理由 |
|------|------|------|
| 落地节奏 | 分 4 个 PR 递进 | 每 PR 独立可验证、风险可控 |
| Host 身份 | hostname 默认 + 配置可覆盖 | 简单、可读、够用 |
| 鉴权 | Bearer token | 起步最简，后续可升级 JWT |
| 版本协商 | 帧内 `schema_version` 字段 | 路径不带版本，帧级兜底 |
| 防环 | `forwarded_by` 路径 + `message_id` 去重 | 既防环又有可观测性 |
| Presence | 二元 online/offline | 够用，避免过度设计 |
| 节点角色 | 可同时为 server 和 push client | 实现"输出接输入无限级联" |
| v1 兼容 | 软兼容（缺失字段填默认值） | 渐进式升级 |
| 配置发现 | token 里携带 upstream URL | 一个字符串即完整配置 |
| 文档 | 新建 `docs/multi-host.md` | 单独成文便于阅读 |

---

## 顶层愿景

```
                      ┌─────────────────────┐
                      │  公司看板 daemon     │
                      │  (/ingest + ws)      │
                      └──────▲──────────────┘
                             │
                             │  cloudflared tunnel
                             │  wss://recall.company.com/ingest
                             │
       ┌─────────────────────┼───────────────────┐
       │                     │                   │
  ┌────▼────┐          ┌─────▼────┐        ┌────▼─────┐
  │ daemon A │          │ daemon B │        │ daemon C │
  │ (张三)   │          │ (小李)   │        │ (小王)   │
  └──────────┘          └──────────┘        └──────────┘
       │                     │                   │
       ▼                     ▼                   ▼
   本地 viewer            本地 viewer         本地 viewer
```

**核心协议特性：**
- 每个 daemon 既能作为 WebSocket server 服务本地 viewer
- 也能作为 WebSocket client 向上游 daemon 推送（PushTransport）
- 也能作为 upstream 接受下游 daemon 推送（`/ingest` endpoint）
- 三者可任意组合，支持无限级联拓扑
- 每帧带 `host` 身份 + `forwarded_by` 路径追踪，确保消息不丢失、不重复、不环路

---

## PR 1：协议骨架（schema v2） ✅ 已完成

### 目标

让 schema v2 的**数据结构**全部就位，daemon 发出的帧携带完整身份信息，但尚未启用 push/ingest 能力。

### 目的

- 把"谁产的这条帧"的概念引入协议
- 为后续级联 PR 打好基础
- 对老版本 receiver 保持软兼容
- schema bump 到 v2

### 改动清单

**核心代码 (`core/src/claude_recall/models.py`)：**
- 新增 `HostIdentity` pydantic 模型
  - `host_id: str`（默认 hostname）
  - `display_name: str | None`（人类可读名字）
- `StateFrame` 加字段：
  - `host: HostIdentity`
  - `forwarded_by: list[str] = []`
  - `message_id: str`（UUID，源头生成）
- `AggregateFrame` 加字段：
  - `host: HostIdentity`
  - `forwarded_by: list[str] = []`
  - `message_id: str`
- 新增 `PresenceFrame`：
  - `host: HostIdentity`
  - `status: Literal["online", "offline"]`
  - `last_active_ago_ms: int | None`
- 更新 `FRAME_SCHEMA_VERSION = 2`

**配置 (`core/src/claude_recall/config.py`)：**
- 新增 `HostConfig`：
  ```python
  class HostConfig(BaseModel):
      id: str | None = None          # None 时用 socket.gethostname()
      display_name: str | None = None
  ```
- `RecallConfig` 加 `host: HostConfig` 字段

**Daemon 启动 (`server.py`)：**
- 启动时构造 `HostIdentity`（读配置或兜底 hostname）
- 所有出厂的帧都由 daemon 填上 `host`、`message_id`、空 `forwarded_by`
- `SessionRegistry.handle_transition` 签名增加 host identity 参数

**测试：**
- `test_host_identity.py`：HostConfig 默认值、配置覆盖
- `test_frame_schema.py`：扩展现有测试，验证 v2 字段
- `test_message_id_unique.py`：每帧 message_id 不重复

**文档：**
- 更新 `docs/protocol.md` 到 schema v2
- 帧示例加上新字段
- 新增"Multi-host readiness"章节说明 host 字段语义

### 验收标准

- 所有现有测试通过（向后兼容）
- 新帧 JSON 序列化里能看到 `host` / `forwarded_by` / `message_id`
- 老版本 receiver 能忽略新字段继续工作
- `uv run claude-recall status` 输出不变

### 代码量估计

约 250 行（含测试和文档）。

### 不在本 PR 范围

- PushTransport（PR 2）
- `/ingest` endpoint（PR 3）
- Token 能力（PR 4）

---

## PR 2：PushTransport（主动上报能力） ✅ 已完成

### 目标

实现一个新的 transport，让 daemon 可以作为 WebSocket client 把自己产生的帧 push 到上游 daemon。

### 目的

- 实现"反转方向"的核心能力——daemon 主动连出去，不等 viewer 订阅
- 为 NAT 穿透场景做好准备（daemon 无需公网可达）
- 默认关闭，现有单机用户零影响
- 为后续"无限级联"做好生产者侧的工作

### 改动清单

**新增文件 (`core/src/claude_recall/transports/push.py`)：**
- `PushTransport` 类继承 `BaseTransport`
- 初始化：读配置里的 `upstream_url` + `auth_token`
- `start()`：开启后台重连循环
- `_connect_loop()`：
  - 连接到 `upstream_url`
  - 带 `Authorization: Bearer <auth_token>` header
  - 发送 `hello` 消息自报 `HostIdentity`
  - 断线时指数退避重试（1s → 2s → 4s → ... → 上限 60s）
- `send(frame)` / `send_aggregate(frame)`：转为 JSON 通过 WS 发出
- `stop()`：优雅关闭连接

**配置 (`core/src/claude_recall/config.py`)：**
- 支持新的 transport 配置：
  ```yaml
  transports:
    push:
      type: push
      enabled: false   # 默认关闭
      options:
        upstream_url: "wss://recall.company.com/ingest"
        auth_token: "xxx"
  ```
- 环境变量覆盖：
  - `CLAUDE_RECALL_UPSTREAM_URL`
  - `CLAUDE_RECALL_TOKEN`

**注册 (`transports/__init__.py`)：**
- 注册 `push` 类型映射到 `PushTransport`

**测试：**
- `test_push_transport.py`：
  - Mock WebSocket server，验证 daemon 能连上并 push 帧
  - 验证 Authorization header 正确
  - 验证断线后会自动重连
  - 验证 `hello` 消息格式
- 测试"push transport 关闭时不影响其他 transport"

**文档：**
- `docs/protocol.md` 加"Push Mode"章节
- 配置示例

### 验收标准

- daemon 启动配置 `push.enabled: true` + 合法 upstream → 自动连接
- push 出去的帧带完整 v2 schema（host + forwarded_by + message_id）
- 断线后 60 秒内能重连成功（退避策略正确）
- 未配置 push 时，daemon 行为与 PR 1 完全一致

### 代码量估计

约 200 行（含测试）。

### 不在本 PR 范围

- `/ingest` 端点（PR 3）——PR 2 只测试 mock server
- Token 解析高级逻辑（PR 4）——token 本期当黑盒字符串使用

---

## PR 3：/ingest endpoint + 防环（级联闭环）

### 目标

让 daemon 能接受别的 daemon push 进来的帧，与 PR 2 的 PushTransport 配合，实现真正的多级级联。

### 目的

- 闭合"daemon ↔ daemon"的双向通信
- 实现防环机制（`forwarded_by` + `message_id` 去重）
- 集成 Presence（连接生命周期 → online/offline）
- 支持"daemon 同时是 server、client、relay"的三合一角色（决策 Q7）

### 改动清单

**核心端点 (`core/src/claude_recall/server.py`)：**
- 新增 `@api.websocket("/ingest")`：
  - 握手：
    - 读 `Authorization: Bearer <token>` header
    - 验证 token 在 `allowed_tokens` 白名单
    - 接受连接，等待 client 发 `hello` 消息
    - 解析 `hello.host` 得到对方身份
  - 主循环：
    - 收到 frame JSON：
      1. 解析为 `StateFrame` / `AggregateFrame` / `PresenceFrame`
      2. 检查 `forwarded_by` 是否包含自己 host_id → 有则丢弃（split horizon）
      3. 检查 `message_id` 是否在 LRU 缓存 → 有则丢弃（dedup）
      4. 通过：
         - append 自己到 `forwarded_by`
         - 存入 LRU 缓存
         - 本机广播（给本地 viewer）
         - 如配置了 upstream，继续转发（级联）
  - 连接断开：
    - 生成 `PresenceFrame { status: "offline", host: <对方> }`
    - 广播给本地 viewer 和 upstream（让整条链都知道这台机器下线）

**防环 LRU (`core/src/claude_recall/message_cache.py` 新文件)：**
- 简单 LRU 实现：最近 10 分钟的 1000 条 `message_id`
- 线程安全（asyncio.Lock）

**Presence 管理：**
- daemon 启动时：本机 daemon 自己广播一条 `PresenceFrame { status: "online", host: self }`
- `/ingest` 连接建立：收到 `hello` 后广播 `PresenceFrame { status: "online", host: client }`
- `/ingest` 连接断开：广播 `PresenceFrame { status: "offline", host: client }`
- daemon 关闭（SIGTERM）：试图发 `offline`

**配置 (`core/src/claude_recall/config.py`)：**
- 新增 `IngestConfig`：
  ```yaml
  ingest:
    enabled: false
    allowed_tokens:
      - "child-token-a"
      - "child-token-b"
  ```

**测试：**
- `test_ingest_endpoint.py`：
  - 单元：token 验证、hello 握手
  - 防环：构造 `forwarded_by` 包含自己的帧，验证被丢弃
  - 去重：连发两条相同 `message_id`，验证第二条被丢弃
- `test_cascade_integration.py`（集成测试）：
  - 启两个 in-process daemon：A、B
  - A 的 push 指向 B 的 /ingest
  - 在 A 注入事件，验证 B 的 viewer 能收到带 host-A 标识的帧
  - 帧的 `forwarded_by` 应包含 `["host-b"]`（B 转发时加入）
- `test_cascade_three_levels.py`（集成测试）：
  - A → B → C 三级级联
  - 在 A 注入事件，验证 C 能收到
  - 构造一个恶意循环：C 伪造"来自 A"的帧 push 回 A，验证 A 检测到 `forwarded_by` 环并丢弃

**文档：**
- `docs/protocol.md` 加 "/ingest endpoint" 章节
- 防环机制说明
- Presence 生命周期说明

### 验收标准

- 两台 daemon 能正确级联（集成测试通过）
- 三级级联能工作
- 人为制造的环路不会导致消息风暴
- 重复 `message_id` 被正确丢弃
- Presence 帧在连接建立/断开时正确发出

### 代码量估计

约 300 行（含集成测试）。

### 不在本 PR 范围

- Token 里携带 upstream URL 的高级形态（PR 4）
- CLI `claude-recall join` 命令（PR 4）
- JWT 签名验证（未来 PR 5）

---

## PR 4：Token 携带配置 + 部署文档

### 目标

把 "token 即完整配置" 的理念落地：员工拿到一串 token 就能 join，不需要手填 URL。

### 目的

- 简化部署流程（发 token = 发配置）
- 提供完整部署文档，方便公司级推广
- 为未来 JWT 升级做好接口预留

### 改动清单

**Token 解析 (`core/src/claude_recall/auth.py` 新文件)：**
- `RecallToken` 数据类：
  ```python
  class RecallToken(BaseModel):
      upstream_url: str
      auth_secret: str
      display_name_hint: str | None = None
      issuer: str | None = None
  ```
- 编解码：
  - `encode_token(RecallToken) -> str`：JSON → base64url
  - `decode_token(str) -> RecallToken`
- 本期**不做签名验证**，留给未来 JWT PR
- 但接口预留：`decode_token` 签名接受可选 `verify_key`

**配置升级：**
- `CLAUDE_RECALL_TOKEN` 环境变量现在可以是完整 token（base64 字符串）
- daemon 启动时：
  ```
  if CLAUDE_RECALL_TOKEN:
      token = decode_token(env_token)
      push.upstream_url = token.upstream_url
      push.auth_token = token.auth_secret
      push.enabled = True
  ```

**CLI 命令 (`core/src/claude_recall/cli.py`)：**
- 新增 `claude-recall join <token>`：
  1. 解析 token 合法性
  2. 尝试连接 upstream（测试握手）
  3. 写入 `~/.config/claude-recall/token` 文件
  4. 显示：连接目标、身份、过期时间（如果有）
- 新增 `claude-recall leave`：
  1. 删除 token 文件
  2. 确认后生效（下次重启 daemon）
- 新增 `claude-recall issue --upstream <url> --secret <xxx>`（管理员用）：
  1. 生成一个 token 字符串打印出来
  2. 便于 IT 签发

**集成：**
- daemon 读 token 的优先级：
  1. `CLAUDE_RECALL_TOKEN` 环境变量
  2. `~/.config/claude-recall/token` 文件
  3. `config.yaml` 里 `transports.push` 的显式配置（老方式）

**测试：**
- `test_token_codec.py`：编解码可逆
- `test_cli_join.py`：
  - 合法 token → 成功
  - 畸形 token → 报错
  - 无法连接的 upstream → 报错

**文档（本 PR 的重头戏）：**

**`docs/multi-host.md`（新建）：**
- 架构图：扁平拓扑、树状级联、灾备双推
- Token 分发指南：IT 如何签发、员工如何 join
- Cloudflare Tunnel 部署步骤（一步步截图）
- Presence 语义说明
- 故障排查指南（token 过期、网络不通、防环误伤等）

**`docs/protocol.md` 更新：**
- 补全 schema v2 的 multi-host 完整描述
- 官方示例 token（用于测试）

**`docs/deployment-guide.md`（新建）：**
- 公司部署指南
- 三种规模的推荐方案：
  - 小团队（< 20 人）：单 dashboard + 直接推
  - 中团队（20-200 人）：树状级联
  - 大公司（> 200 人）：分部门聚合 + 中央看板
- Cloudflare Tunnel + Access SSO 的集成步骤
- 推荐硬件配置
- 监控与告警建议

**`.coderabbit.yaml` 微调：**
- 确保文档 review 语气友好（本 PR 文档量大）

### 验收标准

- `claude-recall issue` → 生成 token
- `claude-recall join <token>` → daemon 自动配置并 push
- 文档包含一个"30 分钟从零部署公司看板"的 walkthrough
- Token 换了之后 daemon 能优雅重新连接

### 代码量估计

约 150 行代码 + 大量文档。

### 不在本 PR 范围

- JWT 签名验证（未来 PR 5）
- Token 过期与续期（未来 PR 5）
- Scope 权限粒度（未来 PR 5）
- `.well-known/claude-recall-config.json` discovery（未来 PR 6）

---

## PR 5-6：未来规划（暂不实施）

### PR 5：JWT 升级

在 PR 4 的 base64 token 基础上升级为签名 JWT：
- 引入签名密钥管理
- 过期与续期
- Scope 权限粒度（push:aggregate、push:state、push:metadata）
- dashboard 侧 JWT 验证

### PR 6：Discovery Endpoint

参考 OIDC：
- `/.well-known/claude-recall-config.json`
- 员工只需要知道公司域名
- daemon 自动拉取完整配置
- SSO 集成

---

## 决策对齐表（全部决策的详细说明）

### Q1. 落地节奏 = B（完整版，分 3-4 PR）
- 对应本文档的 PR 1-4
- 每个 PR 独立可验证、风险可控

### Q2. Host 身份 = A（hostname + 可覆盖）
- 默认用 `socket.gethostname()`
- 配置 `host.id` 可覆盖
- `display_name` 作为人类可读标签分离存储

### Q3. 鉴权 = A（Bearer token）
- 最简起步
- 明文字符串通过 `Authorization: Bearer <token>` header
- 后续可升级 JWT 零改动接口

### Q4. 版本协商 = A（只看 schema_version 字段）
- 路径 `/ingest` 不带版本号
- 协议演进靠 `schema_version` bump
- URL 永久稳定

### Q5. 防环 = C（forwarded_by + message_id）
- BGP 风格的路径追踪（split horizon）
- Gossip 风格的去重（LRU 缓存）
- 双保险，生产级可靠

### Q6. Presence = A（二元 online/offline）
- Matrix 风格
- 避免 XMPP 式过度设计
- 可附带 `last_active_ago_ms` 作为增强

### Q7. 节点角色 = A（同时支持多角色）
- daemon 可以同时作为 server、push client、ingest relay
- 这是"无限级联"的核心
- 配置独立，不同角色可按需启用

### Q8. v1 兼容 = A（软兼容）
- v1 帧进 v2 daemon：自动填 host（用握手时的身份）
- v2 帧出 v1 daemon：v1 忽略未知字段
- 允许渐进升级

### Q9. 配置发现 = B（token 里带 upstream URL）
- 一个字符串即完整配置
- 员工只需 `claude-recall join <token>`
- 无需配置文件

### Q10. 文档 = B（新建 multi-host.md）
- 级联、拓扑、部署单独成文
- 不污染 protocol.md

---

## 推进节奏预估

| PR | 代码量 | 预计时间 | 依赖 |
|----|-------|---------|------|
| PR 1 | ~250 行 | 1-2 天 | 无（当前分支继续）|
| PR 2 | ~200 行 | 1 天 | PR 1 合并 |
| PR 3 | ~300 行 | 2 天 | PR 2 合并 |
| PR 4 | ~150 行 + 文档 | 2 天 | PR 3 合并 |

**总计**：约 900 行核心代码 + 文档，预计 1-2 周完成。

---

## 下一步

PR 1（协议骨架）和 PR 2（PushTransport）已完成，下一步进入 **PR 3：/ingest endpoint + 防环（级联闭环）** 的实现与联调。
