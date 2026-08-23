# 飞书客户端：herdr-remote Lark bot

**Date:** 2026-08-22
**Status:** Draft — 待评审
**Scope:** 新增 `relay/herdr_lark.py` + 单元测试；relay 协议不变；`install-service.sh` 增加飞书引导

## Goal

新增一个飞书客户端，能力对齐现有 Telegram 客户端（`relay/herdr_telegram.py`），让手机在国内网络下无需公网入口即可监控与操控 herdr agent。

## Background

- 现有远程接入依赖 Tailscale Funnel / Cloudflare Tunnel 暴露 relay，公司侧双重硬 NAT 导致延迟 400–600ms（见 `../../../tailcale/PROBLEM.md`）。
- Telegram 客户端已验证**另一条路**：IM 客户端连 `ws://127.0.0.1:8375`，只需 Mac 出站联网，不需要任何公网入口。README 原文：

  > Telegram connects to the relay over localhost, so this setup does **not** require Cloudflare Tunnel.

- Telegram 在国内网络不可直连；飞书是同等模型下国内可用的替代。
- 飞书支持 WebSocket 长连接接收事件，同样是纯出站，无需回调 URL、无需域名、无需公网 IP。

### 为什么不用官方 Claude Code Channels

调研结论（2026-08-22）：

| 维度 | 官方 Channels | 本方案需要 |
|---|---|---|
| 会话模型 | 单个 running session | 多 agent + `pane_id` 编队 |
| 远程能力 | 仅 `allow` / `deny` | read / send_keys / interrupt / trust / digest |
| 平台支持 | Telegram / Discord / iMessage | 飞书（官方文档零提及） |
| 运行时 | Bun + TypeScript + MCP SDK | Python（与 relay 一致） |
| 稳定性 | 研究预览，协议契约可能变 | relay 协议自控 |
| 组织限制 | Team/Enterprise 需 Owner 开 `channelsEnabled` | 无 |

官方 Channels 的权限中继是 herdr 能力的**子集**，无法替代。仅借鉴其协议细节（见「借鉴官方 Channels 的设计」）。

### 为什么不直接复用 Hanson/claude-client

该项目（TypeScript）是「飞书 ↔ Claude Agent SDK」直连，**没有 relay 层**，也没有 pane 模型、`read_pane`、`send_keys`、blocked 状态机。可复用的是飞书平台侧的踩坑知识，不是代码。

## Decisions

| 主题 | 选择 |
|---|---|
| 语言 / 运行时 | Python + PEP 723 内联依赖，`uv run`，与其他 relay 组件一致 |
| 飞书 SDK | `lark-oapi`（官方 Python SDK，`pip install lark-oapi`） |
| 事件接入 | **WebSocket 长连接**（`lark.ws.Client`），不用 webhook |
| 线程模型 | 飞书 WS 客户端独立线程 + asyncio 主循环，`run_coroutine_threadsafe` 桥接 |
| relay 连接 | 复用 Telegram 版模式：`ws://127.0.0.1:8375`，持久连接 + 5s 重连 |
| 应用类型 | 飞书自建应用（个人可创建，免费） |
| 交互载体 | 交互式卡片（按钮）+ 纯文本消息，双通道等价 |
| 状态合并 | 复用 `relay/agent_state.py` 的 `apply_agent_message` |
| 消息去重 | 必需，持久化到 `~/.config/herdr-remote/lark_seen.json` |
| 权限模型 | 单一 `HERDR_LARK_CHAT_ID` 白名单，与 Telegram 的 `CHAT_ID` 同构 |
| 不做 | webhook 模式、多租户、群聊多人并发仲裁 |

## 架构

```
                    出站 WSS（无需公网入口）
  飞书服务器  ←───────────────────────────→  herdr_lark.py
      ↕                                            │
   你的手机                                        │ ws://127.0.0.1:8375
                                                   ▼
                                          relay (herdr_relay.py)
                                                   │
                                                   ▼
                                            herdr CLI / panes
```

两个方向都是 Mac 主动出站。Tailscale / Funnel / CF Tunnel / VPS **全部不需要**。

## 能力对齐矩阵

| Telegram 命令 | 飞书对应 | relay 消息 |
|---|---|---|
| `/start` | `/start` 或「面板」 | — （本地状态渲染卡片） |
| `/agents` | `/agents` | — |
| `/status` | `/status` | — |
| `/read [project]` | `/read` | `read_pane` |
| `/reply [project]` | `/reply` | `read_pane` + `send_text` |
| `/send [project] [text]` | `/send` | `send_text` + `send_keys(["Enter"])` |
| `/trust [project]` | `/trust` | `respond`（`"trust, always allow"`） |
| `/interrupt [project]` | `/interrupt` | `send_keys(["C-c"])` |
| `/digest` | `/digest` | —（本地统计） |
| inline 按钮 | 卡片按钮 `action.value` | 同上 |
| ForceReply 回复 | 回复消息 / 直接发文本 | `send_text` |
| blocked 推送 | blocked 卡片 | 接收 `blocked` |
| 完成通知 | 文本 + 卡片按钮 | 接收 `agent_update` |

## 必须原样继承的关键设计

以下四条是 Telegram 版**用注释记录的踩坑**，飞书版若重写会重蹈覆辙。

### 1. 选项确认必须发按键，不能发文本

`herdr_telegram.py:handle_callback` 原注释：

> Sending the option *text* via `respond` does NOT work: the relay pastes it via
> send-text, and Claude's TUI treats a pasted trailing newline as paste content,
> not as Enter, so the prompt never gets confirmed. A real key press does.

因此批准动作必须走 `send_keys(pane_id, [str(选项序号)])`，把选项的 **1-based 位置**当按键发。卡片按钮的 `value` 里存序号，不存选项文本。

### 2. `send_keys` 必须读 relay 的 ack

`cmd_interrupt` 原注释：

> 原先自己建连接发完就报成功，而 Telegram 侧用的键名不在 relay 的 SAFE_KEYS
> 里（relay 只认 C-c），会被整条拒绝——用户却看到 "Sent Ctrl+C" 的假成功。

`send_keys_to_relay` 要循环读最多 5 条消息，等 `command_result` 且 `command == "send_keys"`，`ok` 为假则抛错。飞书版必须照搬这个 ack 循环。

relay 的 `SAFE_KEYS` 白名单（`herdr_relay.py:113`）：

```
y n a Enter Tab Escape C-c Up Down Left Right backspace Space 0-9 + KEY_ALIASES
```

中断只能用 `"C-c"`，不能用 `"Ctrl+C"` 之类别名。

### 3. 审批 generation 防串号

`approval_tokens: dict[pane_id, generation]`，每次推 blocked 生成新的 `secrets.token_hex(4)`。点击时校验按钮携带的 generation 是否等于当前值，不等则拒绝并提示「属于更早的提示」。

pane 状态离开 `blocked` 时（`agents` 快照或 `agent_update`）清除对应 token，防止陈旧按钮生效。

### 4. 密钥脱敏

`scrub()` 把 relay token 和 bot token 从任何日志/外发文本里替换成 `<redacted>`。WebSocket 异常（如 `InvalidURI`）会把带 `?token=` 的完整 URL 带进异常文本。飞书版同样需要，脱敏对象换成 `APP_SECRET` 和 relay token。

## 飞书平台约束（2026-08-22 核实）

长连接可行，但有三条硬约束，直接影响实现：

| 约束 | 影响 |
|---|---|
| 事件与回调**分别**配置订阅方式 | 「事件配置」和「回调配置」是后台两个独立页面，都要设为「使用长连接」 |
| 保存配置时长连接**必须在线** | 后台保存订阅方式时本地程序必须已连接，否则保存失败。安装流程需引导「先起进程，再存配置」 |
| 收到事件后须 **3 秒内**处理完 | `read_pane` 走 relay 往返可能超时 |
| 仅限**企业自建应用**，不支持商店应用 | 与既有判断一致 |
| 单应用最多 50 个连接；集群模式随机投递 | 同一 app 不可多实例部署，否则事件被随机分走 |

### 3 秒超时的应对（关键设计）

飞书回调处理器必须在 3 秒内返回，而 `read_pane` 需要连 relay、发请求、等响应。**所有耗时操作必须异步化**：

- 回调处理器**立即返回**（卡片回调返回 toast，消息事件返回 `None`）
- 真正的工作用 `asyncio.run_coroutine_threadsafe()` 投递到后台事件循环
- 结果通过**新发一条消息**或**更新原卡片**回传，而不是靠回调返回值

`lark.ws.Client.start()` 是阻塞的同步调用，而 relay 连接是 asyncio。因此需要：主线程跑 asyncio 事件循环，飞书 WS 客户端跑在独立线程，两者通过 `run_coroutine_threadsafe` 通信。

## 飞书平台特有的处理

以下是 Telegram 没有、飞书必须做的。

### 消息去重（必需）

飞书长连接**会重推消息**。Telegram 的 `update_id` 天然单调，飞书没有等价保证。

- 维护 `seen_message_ids`，上限 5000，超限删最旧一半
- 持久化到 `~/.config/herdr-remote/lark_seen.json`（mode 0600）
- 重启后加载，避免重放历史消息

### 群聊需 @ 机器人

```python
if chat_type == "group" and not mentioned_bot:
    return
```

并用 `strip_bot_mention()` 剥掉文本里的 `@机器人`，否则命令解析会带上前缀。

### 忽略机器人自身消息

`sender_open_id == bot_open_id` 时直接返回，否则无限循环。

`bot_open_id` 通过 `GET /open-apis/bot/v3/info` 获取，SDK 未封装，需裸调用。初始化失败要给出明确的「检查 APP_ID / APP_SECRET」提示。

### 卡片点击归一成文本消息

借鉴 Hanson/claude-client 的 `handleCardAction`：把 `card.action.trigger` 的 `action.value` 转换成等效的消息上下文，走**同一个** `on_message` 处理器。

好处：点「批准」按钮和打字回「批准」在下游是同一条路径，不用维护两份分支逻辑。

### 富文本降级链

`send_card` → 失败回退 `send_markdown` → 失败回退 `send_text`。飞书富文本格式挑剔，兜底是必要的。

## 卡片设计

### blocked 卡片

```
🐑 {agent} blocked in {project}

```
{prompt[:400]}
```

[1. Yes (once)] [2. Trust (always)] [3. No]
[Open output & reply]
```

按钮 `value`：

```json
{ "a": "k", "p": "<pane_token>", "g": "<generation>", "k": "1" }
```

字段沿用 Telegram 版的紧凑编码（`ACTION_CODES`）。飞书 `action.value` 无 64 字节限制，但保持一致便于两端逻辑复用。

选项来源：relay 的 `blocked` 消息带 `options`，由 `detect_options()` 判定：

- 含 `"yes, single permission"` → `TOOL_OPTIONS`（3 项）
- 含 `"approve all pending"` → `SUBAGENT_OPTIONS`（3 项）
- 否则 → 默认 `TOOL_OPTIONS`

### agent 选择卡片

分页，每页 20 项（`AGENT_PAGE_SIZE`）。按 `STATUS_ORDER`（blocked → working → done → idle）排序。

标签去重逻辑（`agent_button_labels`）：同名 agent 追加 host、pane_id 摘要、序号，逐级消歧。飞书按钮文本上限比 Telegram 的 64 宽松，但保留该逻辑以防同名混淆。

## pane_id token 化

沿用 `pane_callback_token()` = `sha256(pane_id)[:16]`。

`resolve_pane_token()` 的解析顺序：先在 `agents` 里找唯一匹配；找不到再在 `pending` 里找。多个匹配返回 `None`（拒绝执行，提示刷新）。

## 配置

| 环境变量 | 用途 |
|---|---|
| `HERDR_LARK_APP_ID` | 飞书自建应用 App ID |
| `HERDR_LARK_APP_SECRET` | App Secret |
| `HERDR_LARK_CHAT_ID` | 授权会话 ID；未设置时进入发现模式 |
| `HERDR_LARK_DOMAIN` | `feishu`（国内）/ `lark`（海外），默认 `feishu` |
| `HERDR_RELAY` | relay URL，默认 `ws://127.0.0.1:8375` |

凭据写入 `~/.config/herdr-remote/secrets.env`，mode 0600，与现有安装脚本一致。

### 所需飞书权限 scope

最小集（**不要**照抄 Hanson 那份含 sheets/wiki/docs/aily 的全量清单）：

```
im:message
im:message:send_as_bot
im:message.p2p_msg:readonly
im:message.group_at_msg:readonly
im:chat
cardkit:card:write
```

## 借鉴官方 Channels 的设计

官方权限中继协议有三处比 Telegram 版更严谨，飞书版采纳：

1. **request_id 用五个小写字母，排除 `l`** —— 避免手机上看成 `1` 或 `I`。可用于替代当前 `token_hex(4)` 的 generation，人眼可读。
2. **凭据脱敏** —— 外发的 prompt 文本中屏蔽形似 API key / PAT 的片段为 `[REDACTED]`。
3. **长文本首尾保留截断** —— 当前实现是 `prompt[:400]` 直接截断，丢失命令末尾。改为保留首尾、中间标注省略字数，让审批者能看到命令结尾。

## 安全

- `HERDR_LARK_CHAT_ID` 白名单是唯一鉴权，与 Telegram 同构。**任何能在该会话发言的人都能批准工具调用**，群聊需谨慎。
- relay 仍监听 localhost；飞书客户端不改变 relay 的暴露面。
- 若 relay 启用了 `HERDR_RELAY_TOKEN`，客户端用 `?token=` 连接，且必须经 `scrub()` 脱敏后才可日志/外发。

## 测试

新增 `tests/test_lark.py`，参照 `tests/test_telegram.py`：

- 卡片 `action.value` 编解码往返
- generation 校验：陈旧 token 被拒绝
- `send_keys` ack：`ok: false` 必须抛错，不得假成功
- 消息去重：重复 message_id 只处理一次；持久化往返
- 群聊未 @ 时忽略；机器人自身消息忽略
- `scrub()` 覆盖 app secret 与 relay token
- `resolve_pane_token` 多匹配时返回 `None`

## Out of Scope

- webhook 模式（长连接已足够，且需公网入口，与本方案初衷冲突）
- 多用户 / 多租户会话隔离
- 图片、文件上传
- 流式输出（飞书卡片可更新，但 pane 内容更适合按需 `read`）
- 替换现有 Telegram 客户端（两者并存）

## 未决问题

1. `install-service.sh` 是否同时支持 Telegram 与飞书共存的 LaunchAgent？倾向支持，各自独立 service。
2. 飞书单条消息长度上限需实测，确认 `read_pane` 的 3500 字符截断是否需下调。
3. 企业租户下自建应用是否需管理员审批发布 —— 个人版可直接使用，企业版待确认。**这是唯一可能让整条路走不通的阻塞项**，实现前需先在开放平台后台确认能否创建自建应用。
4. 3 秒超时下 `read_pane` 的实际耗时需实测；若 relay 往返偶发超过 3 秒，需要在卡片上先回「读取中」再更新。
