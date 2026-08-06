# Moshi 能力对齐调研

调研日期：2026-08-06
对标产品：[Moshi](https://getmoshi.app/) — SSH & MOSH Terminal for AI Coding Agents

---

## 一、为什么对标 Moshi

Moshi 与 herdr-remote 的定位高度重合，官方自述是「AI agent 的婴儿监视器」（baby monitor
for your AI agents）——手机上看住长时间运行的 coding agent，随时审批与介入。

更直接的是：**Moshi 官方文档专门有一章讲 Herdr 集成**，把 Herdr 与 tmux、Zellij 并列为
支持的 multiplexer，用的是 herdr-remote 依赖的同一个 CLI。两者面向同一批用户、同一个
上游工具，属于正面对标关系。

---

## 二、Moshi 能力清单

### 连接与传输

| 能力 | 说明 |
|---|---|
| 协议 | SSH / Mosh / ET（Eternal Terminal），可设 Auto 路由 |
| 抗断线 | 基于 Mosh 协议，网络切换、休眠、隧道穿行不掉线 |
| Tailscale | 原生支持 |
| Multiplexer | tmux、Zellij、**Herdr** |
| 架构特点 | **直连自有机器，无中继服务器**（官方强调「no relay wrapper, no hosted sandbox」） |

### Agent 支持

官方集成 Claude Code、Codex、OpenCode、Cursor、Kimi Code、Qwen Code、Grok Build、PiOM
等 8+ 个 agent。任何 CLI 工具都能跑，但只有上述 agent 享有专属的收件箱、通知与 Diff 查看。

### 事件与通知（moshi-hook）

`moshi-hook` 是 CLI 与常驻守护进程的混合体：

- 安装时改写各 agent 的配置文件（如 `~/.claude/settings.json`），指向本地 Unix socket
- 捕获 agent hook 事件，规范化为 5 类：
  `approval_required` / `task_complete` / `session_started` / `tool_running` / `tool_finished`
- 读取 `$HERDR_ENV` 与 `$HERDR_SESSION`，在事件上标记 `kind=herdr` 及对应 session/workspace

数据边界：
- **仅通知摘要上云**（200 字符提示词 + 80 字符助手回复 + 元数据）
- 完整转录与 diff 走 SSH 转发的本地网关（`127.0.0.1:24543`），不经服务器
- 审批决策通过 WebSocket 从 App 回传守护进程

### 移动端交互

| 能力 | 说明 |
|---|---|
| 锁屏审批 | 锁屏、动态岛、Apple Watch 上直接 Approve/Deny，无需打开 App；通知内含最多 256 字符的命令详情 |
| 统一收件箱 | 聚合审批、提问、工具活动、任务完成；点击事件直接重连主机并 attach 对应 session |
| Live Activities | 常驻显示当前活跃任务 |
| 语音输入 | Apple 本地 SpeechAnalyzer / Whisper / 云端引擎 |
| 图片直传 | 粘贴截图，agent 拿到可访问 URL |
| Diff 查看器 | 审视代码变更，内容留在主机 |
| 浏览器预览 | 内置网页预览 |
| 手势导航 | 单指滑动切 tab、双指横滑切 pane、双指竖滑切 workspace、捏合缩放 |
| 键盘 | 移动端工具栏、硬件键盘快捷键（⌘K、⌘O 等）、CJK/IME 支持 |
| 其他 | Face ID 解锁 SSH 密钥、iCloud 同步、主题/字体定制 |

### Herdr 专属集成

- **Session Picker**：连接时在 Herdr 标签下列出可用 workspace（而非 session），选中后
  直接聚焦该 workspace 再 attach，登录即处于目标项目视图；已停止的会话自动过滤
- **Shortcut Panel**：预绑定 `Ctrl-B` 前缀命令（C 新建 tab、N/P 前后切换、W 工作区导航、Z 缩放）
- **Deep Link**：`moshi://herdr?workspace=<id>&session=<name>`
- **workspace 卡片**：侧栏汇总每个 workspace 内最紧急的 agent 状态（阻塞/运行中/完成）

Moshi 对手机端管理 agent 的核心痛点判断：**一屏只容得下一个 pane**。其解法是引导用户
「tab 优先于 pane 分割」，配合手势而非前缀键来切换。

### 商业模式

免费起步（无账户、无信用卡），Pro 解锁高级功能。App Store 4.8 星。

---

## 三、架构对比

| 维度 | herdr-remote | Moshi |
|---|---|---|
| 连接 | 自建 relay 中继（:8375）+ Cloudflare tunnel | 直连 SSH/Mosh/ET，无中继 |
| 状态获取 | 2 秒轮询 + herdr hook 事件（UDP 推送） | moshi-hook 守护进程，事件驱动 |
| 数据上云 | 经自建 relay | 仅通知摘要；全文与 diff 走 SSH 本地网关 |
| 客户端 | web / macOS / iOS / Telegram / TUI | iOS/iPadOS 原生 + Apple Watch |
| 部署 | 自托管 | 商业产品 |

**架构分歧不应盲目对齐。** relay 是 herdr-remote 的基石，它换来的是多客户端支持
（Web / Telegram / TUI 共用一套后端）与零客户端安装——浏览器打开即用，这恰是
Moshi 直连架构做不到的。Moshi 的「无中继」是其卖点，但对本项目不是要补的短板。

---

## 四、能力差距

### 已具备

- **事件驱动**：`relay/herdr-plugin.toml` 已注册 `pane.agent_status_changed`，
  经 `relay/on_event.py` 以 UDP 推送至 relay
- **Web Push**：relay 侧已有 VAPID 基建（`pywebpush` / `py-vapid`），Web 端 PWA + Service Worker 就绪
- **通知内审批**：Telegram bot 已实现——blocked 时推送带 inline keyboard 的通知
  （Yes / Trust / No），并有 generation 随机数做 replay 防护，避免点旧通知误批准新 prompt
- **多主机**：`HERDR_REMOTES` 经 SSH 轮询，pane 按 `host:pane_id` 命名空间化
- **终端交互**：`read_pane` / `send_keys` / `send_text` / `respond`
- **移动端适配**：Web 端已完成显示密度优化、CJK 终端列对齐、触摸目标与手势修复

### 值得补齐

按投入产出比排序。

| 优先级 | 能力 | 说明 |
|---|---|---|
| 高 | **通知内 Approve/Deny（Web）** | Moshi 最核心的体验。Telegram 端已验证此模式可行，Web 端属移植而非从零设计；Web Push 在 iOS 16.4+ 支持通知 action |
| 高 | **统一收件箱** | 跨主机聚合审批/提问/完成事件，点击直达对应 pane。事件驱动改造的直接受益 |
| 中 | **Diff 查看器** | herdr 可取 git 状态，diff 内容无需上云 |
| 中 | **语音输入** | 手机打字是真实痛点；Web Speech API 在移动 Safari 可用，成本低 |
| 中 | **workspace 维度导航** | 当前按 status 分组，Moshi 按 workspace 组织并汇总其内最紧急状态 |
| 低 | **图片/截图上传** | 需新增二进制通道 |
| 低 | **Live Activities / 动态岛** | 依赖 iOS 原生 |

### 不建议对齐

| 能力 | 原因 |
|---|---|
| 无中继直连 | 与 relay 架构冲突，且会牺牲多客户端能力 |
| Mosh 协议 | 目标（抗断线）已由 WebSocket 重连覆盖，路径不同 |
| Apple Watch / Face ID / iCloud 同步 | 纯 iOS 生态投入，需先确定是否主推 iOS 原生 |
| 锁屏审批（iOS 原生） | 依赖原生 Live Activity + AppIntent；Web Push 的通知 action 可覆盖大部分场景 |

---

## 五、落地路线

**载体选择：主攻 Web 端。**

Web 端是本项目功能最完整的移动客户端，且 relay 已具备 Web Push 基建。Moshi 的锁屏审批
依赖 iOS 原生能力，而 Web Push 的通知 action（iOS 16.4+）能覆盖「不打开 App 直接审批」
这一核心场景，投入远低于原生方案。

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 事件入口 schema 校验、token 常量时间比较、Telegram `/interrupt` 键名修正 | ✅ 已完成（`5f5c2d9`） |
| P1 | 事件路径补 Web Push 与 blocked 去重；herdr 调用异步化，多 remote 并发查询 | ✅ 已完成（`3de7131`） |
| P2 | 通知内 Approve/Deny（Web PWA） | 待办 |
| P3 | 统一收件箱（跨主机事件流） | 待办 |
| P4 | Diff 查看器、语音输入 | 待办 |

P0/P1 是 P2 的前提：没有低延迟且不重复的结构化事件，通知与收件箱都做不好。

---

## 参考来源

- [Moshi 官网](https://getmoshi.app/)
- [What Moshi does](https://getmoshi.app/docs/introduction)
- [Hooks](https://getmoshi.app/docs/hooks)
- [Herdr 集成](https://getmoshi.app/docs/herdr)
- [Herdr on Mobile 指南](https://getmoshi.app/guides/herdr)
- [推送通知与 Webhook](https://getmoshi.app/docs/notifications)
- [App Store](https://apps.apple.com/us/app/moshi-ssh-mosh-terminal/id6757859949)
