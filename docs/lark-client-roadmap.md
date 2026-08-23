# 飞书客户端：同类项目调研与待办

> 完整功能对齐分析见 [lark-client-parity-report.md](lark-client-parity-report.md)。

调研时间：2026-08-22

---

## 同类项目

| 项目 | 语言/规模 | 卡片交互 | 特色 |
|---|---|---|---|
| [qingpingwang/remote-claude-code](https://github.com/qingpingwang/remote-claude-code) | Python 578 行 | 无，纯文本 | SQLite 持久化会话、**串行队列** |
| [Hanson/claude-client](https://github.com/Hanson/claude-client) | TypeScript ~2500 行 | 有 | **流式更新卡片**，实时展示执行过程 |
| OpenClaw（小龙虾） | — | 有 | 多平台：微信 / QQ / 飞书 / Discord |
| **herdr 飞书客户端**（本项目） | Python ~1400 行 | 有 | **多 agent 编队**、pane 模型、`send_keys` |

### 与它们的根本差异

它们都是 **「飞书 ↔ Claude Code SDK」直连**：每个飞书会话 fork 一个 Claude 子进程，管的是「一个对话」。

本项目是 **「飞书 ↔ relay ↔ herdr CLI」**：管的是**已经在跑的一批 agent**，能读 pane、发按键、看编队状态。这是它们都没有的。

反过来，它们有本项目做不到的：`/mode` 直接切 `acceptEdits` / `plan` / `bypassPermissions` —— 因为它们持有子进程句柄。本项目只能通过 pane 按键间接影响。

---

## 待办（按性价比排序）

### 1. 串行队列 —— 已完成 ✅

**问题**：同一个群连发两条消息，两个 `run_coroutine_threadsafe` 并发执行，`send_text` 的粘贴与回车可能交错 —— 第二条的粘贴插进第一条的回车之前。

**做法**（借鉴 `remote-claude-code`）：同一 chat_id 串行，不同 chat_id 并行。

```python
_active_queues: dict[str, Queue] = {}
# 已有队列 → 直接入队；没有 → 建队列 + 起 worker
```

### 2. 绑定持久化 —— 已完成 ✅

**问题**：`_active`（群 ↔ agent 绑定）只在内存里，服务重启后绑定丢失，得重新 `/read` 一次。

**做法**：复用 `SeenStore` 的落盘模式，存 `~/.config/herdr-remote/lark_bindings.json`。

### 3. 流式更新卡片 —— 已完成 ✅

**价值**：agent 干活时不用反复 `/read`，卡片自己更新。

**可行性已验证**：`lark-oapi` 的 `cardkit.v1.card_element` 提供 `patch` / `update` / `content`。

**做法**（借鉴 `claude-client`）：节流约 1.5 秒 patch 一次卡片。

**落地**：`/watch [序号]` 命令。`StreamThrottle` 负责节流（1.5 秒 + 内容去重 + sequence 递增），agent 连续 3 轮 idle 自动收工，创建失败降级提示改用 `/read`。

---

### 4. 新建 agent —— 已完成 ✅

**问题**：只能操控已存在的 agent，没法从飞书开新的。

**落地**：`/new <序号> [类型]`。用序号指工作目录（抄该 agent 的 `cwd`），
relay 的 `create_workspace` 建空 shell，再 `send_text` 打入 `cd ... && <类型>`。
不需要改 relay。

---

### 5. `/health` 自检 —— 已完成 ✅

一条命令检查：飞书长连接、relay 连接、授权群、绑定状态、去重缓存。
现在排查要翻日志。低成本高回报。

### 6. 解绑 —— 已完成 ✅（实现为 `/unbind`）

不是清上下文（那在 agent 侧），而是解绑当前群 + 恢复默认群名。
现在只能靠 `/read` 换绑。

### 7. 图片支持 —— 已完成 ✅

agent 输出里提到的绝对路径图片自动传到飞书。校验文件头而非只看扩展名，
单条最多 3 张、单张 8 MB。需 `im:resource` 权限（已开通并发布）。

### 8. 队列可视化 —— 已完成 ✅（并入 `/health`）

`ChatQueue` 里排队的消息不可见，连发多条时不知道排到第几个。

---

## 明确不做

| 项目 | 原因 |
|---|---|
| 切 permission mode（`/mode`） | 需持有 Claude 子进程句柄；本项目走 relay + pane，模型不同 |
| 多 IM 平台（微信 / QQ / Discord） | 飞书已够用；Telegram 客户端已存在 |
| 图片 / 文件上传 | 与「监控指挥 agent」的核心场景无关 |
