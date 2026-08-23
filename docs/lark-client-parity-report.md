# 功能对齐报告：herdr 飞书客户端 vs OpenClaw / claude-client

调研时间：2026-08-23
对照版本：`relay/herdr_lark.py` 2812 行、399 个单测全绿
最后更新：2026-08-23（借鉴项已全部落地）

---

## 一、结论先行

**不是同一类产品，不该按同一套标准打分。**

| | OpenClaw（小龙虾） | Hanson/claude-client | **herdr 飞书客户端** |
|---|---|---|---|
| 本质 | 全能 AI 助理平台 | 飞书 ↔ Claude SDK 桥 | **多 agent 编队遥控器** |
| 管的对象 | 一个 AI 助理 | 一个会话 | **一批已在跑的 agent** |
| 典型场景 | 问答、搜索、写文档、订会议 | 手机上写代码 | **看 15 个 agent 谁卡了、批准、继续** |
| 规模 | 平台级（可视化后台） | ~2500 行 TS | 2812 行 Python |

三者的交集只有「在飞书里操控 AI」这一层皮，内核完全不同。下面的对齐分析建立在这个前提上。

---

## 二、逐项对齐

### 我们有、它们没有

这些源于 herdr 的 pane 模型，是本项目的立身之本。

| 能力 | 说明 | 它们为什么没有 |
|---|---|---|
| **多 agent 编队视图** | `/agents` 一屏看 16 个 agent 的状态 | 它们一个会话一个子进程，没有「编队」概念 |
| **稳定序号** | 按 `pane_id` 排序，状态变化不漂移 | 无对应问题 |
| **重名区分** | 同项目多 agent 自动加 `[w1B]` / `[frontend]` | 无对应问题 |
| **读取真实终端** | `read_pane` 读 tmux pane 原始输出 | 它们只能拿到 SDK 的结构化返回 |
| **按键注入** | `send_keys` 发 `C-c`、数字键 | 无终端可发 |
| **一群一 agent 绑定** | 群名自动改为 `herdr · [标记] 项目` | 它们是一会话一 session |
| **接管已在跑的 agent** | 不是它启动的也能管 | 它们只能管自己 fork 的进程 |
| **识别空 space** | `▫` 标出没开 agent 的裸终端，发送前拦截 | 它们的会话必然有 agent |

### 它们有、我们没有

诚实列出，并说明是否该补。

| 能力 | OpenClaw | claude-client | 我们 | 该不该补 |
|---|---|---|---|---|
| 多 IM 平台 | ✅ 8+ 平台 | 仅飞书 | 仅飞书 | **不补** —— Telegram 客户端已存在，够用 |
| 图片上传 | ✅ | ✅ | ✅ | **已做** —— 输出里的图片路径自动传 |
| 文件上传 | ✅ | ✅ | ❌ | **不补** —— 图片够用，文件多在本机 |
| 会话上下文持久化 | ✅ | ✅ SQLite | ❌ | **不需要** —— 上下文在 agent 自己的终端里，本就不丢 |
| Skills / 插件系统 | ✅ | ✅ `/skills` | ❌ | **不补** —— 那是 agent 自己的能力，不该由遥控器管 |
| 切 permission mode | — | ✅ `/mode` | ❌ | **做不到** —— 需持有子进程句柄；我们走 relay + pane |
| 工作目录切换 | ✅ | ✅ `/dirs` `/pwd` | 部分 | `/new <序号>` 已覆盖主要场景 |
| 可视化管理后台 | ✅ | ❌ | ❌ | **不补** —— 定位不同 |
| RAG / 企业搜索 | ✅ | ❌ | ❌ | **不补** —— 不是同一类产品 |
| 多 Agent 协同编排 | ✅ 角色分工 | ❌ | ❌ | 我们是**观测与操控**，不做编排 |

### 双方都有，实现不同

| 能力 | 它们的做法 | 我们的做法 |
|---|---|---|
| 权限审批 | 卡片按钮 → SDK 回调 | 卡片按钮 → **发数字按键**到 pane |
| 流式输出 | 卡片 patch，节流 1.5s | 同左（`StreamThrottle`）+ **按行截断防抖** |
| 长连接 | 飞书 WebSocket | 同左 |
| 串行处理 | claude-client 有队列 | `ChatQueue`（同群串行、异群并行） |
| 选项识别 | SDK 结构化返回 | **文本解析**，支持三种形态 + 多组 |

---

## 三、我们做得更细的地方

这些是踩坑后加的，同类项目未见对应处理。

**1. 多组选择器逐组推送**

`AskUserQuestion` 一次问多组，TUI 是逐组问的。同时展示所有组会导致**静默答错** —— 两组都有 `1/2/3`，点第二组的按钮，数字被当成第一组的答案。我们只推 agent 当前在等的那一组，答完自动衔接下一组。

**2. 三层 ack 保护**

relay 的异步确认必须等，否则「假成功」：

- `send_keys` 不等 ack → 键名被拒也显示"已发送"
- `send_text` 不等 ack → **回车丢失**（relay 要在粘贴后 settle）
- 粘贴失败仍发回车 → 会把输入框残留内容提交出去

**3. 输出清理**

终端 TUI 装饰在手机上占掉大半屏。实测某 pane：47 行 → 21 行、928 → 371 字符（省 61%）。滤掉状态栏、分隔线、**Markdown 表格边框**、空行、HTML 注释。

**4. 流式防抖**

按字符截末尾 N 个会导致每帧整体平移一格（首行 `oreDeprecations` → `noreDeprecations`），看着在抖。改按整行截断 + 跳过 relay 读取失败帧。

**5. 审批时效**

每条 blocked 通知带 5 字母 generation（排除 `l`，防手机上看成 `1`），点旧通知的按钮会被拒绝。

---

## 四、命令对照

| 我们 | claude-client | 说明 |
|---|---|---|
| `/agents` | — | 编队视图，它们没有 |
| `/read <序号>` | — | 读终端，它们没有 |
| `/watch` | — | 流式跟随（它们是自动流式，无需命令） |
| `/new <序号> [类型]` | `/dirs` | 我们能指定 agent 类型 |
| `/spaces` | — | 一键一 agent 一群 |
| `/trust` `/interrupt` | — | 按键注入 |
| `/render card\|text` | — | 输出样式切换 |
| `/autowatch` | — | 自动跟随开关 |
| `/status` | `/status` | 相同 |
| `/help` | `/help` | 相同 |
| — | `/clear` `/reset` | 清上下文；我们的上下文在 agent 侧 |
| — | `/skills` | agent 自己的能力 |
| — | `/tasks` `/tasklist` | 任务管理 |
| `/health` | `/health` | 相同（我们还带队列深度） |
| `/unbind` | `/clear` | 我们解绑群，它们清上下文 |

---

## 五、借鉴项落地情况

四项全部完成（2026-08-23）。

| 项 | 状态 | 落地方式 |
|---|---|---|
| `/health` 自检 | ✅ | 连接、agent、群、绑定、队列、跟随一屏看清 |
| 图片支持 | ✅ | 输出里的绝对路径图片自动传，校验文件头 |
| `/clear` 语义 | ✅ | 实现为 `/unbind`，`drop` 可连群解散 |
| 队列可视化 | ✅ | 并入 `/health`，省了单独命令 |

---

## 六、不做的与原因

| 项 | 原因 |
|---|---|
| 多 IM 平台 | Telegram 客户端已存在；飞书够用 |
| Skills / 插件 | 那是 agent 的能力，遥控器不该管 |
| 可视化后台 | 定位是「手机上的遥控器」，不是平台 |
| RAG / 企业搜索 | 不是同一类产品 |
| 多 Agent 编排 | 我们做**观测与操控**，编排交给 herdr 本身 |
| 切 permission mode | 需子进程句柄，架构不支持；可用 `/trust` 近似 |

---

## 七、总评

**在「遥控已在跑的 agent 编队」这个场景上，我们的能力超过两者** —— 它们根本不解决这个问题。

**在「通用 AI 助理」上完全不可比** —— OpenClaw 是平台级产品，有可视化后台、Skills 生态、多平台接入、RAG。

**与 claude-client 最接近**，都是「飞书 ↔ 本地编码 agent」。差别：它管一个会话的完整生命周期（含启动、上下文、清理），我们管一批已存在 agent 的观测与操控。

第五节四项已全部落地。剩余差距均属定位不同，见第六节。

---

## 附：数据来源

- [OpenClaw GitHub](https://github.com/openclaw/openclaw)
- [OpenClaw 飞书官方插件](https://www.feishu.cn/content/article/7613711414611463386)
- [OpenClaw 多 Agent 配置](https://cloud.tencent.com/developer/article/2647731)
- [Hanson/claude-client](https://github.com/Hanson/claude-client)（源码实读）
- [qingpingwang/remote-claude-code](https://github.com/qingpingwang/remote-claude-code)（源码实读）
- 本项目：`relay/herdr_lark.py`、`docs/lark-client-manual.md`、`docs/lark-client-roadmap.md`
