# 群名状态色环

日期：2026-08-24
状态：已确认，待实现

## 一、问题

飞书群名当前格式为 `herdr · [标记] <项目名>`（`chat_title_for`，`relay/herdr_lark.py:3414`）。
`herdr · ` 前缀在每个项目群都相同，在会话列表这种宽度极窄的位置纯属浪费——
真正需要看清的项目名反而被挤掉。

同时会话列表本可以承载更多信息：现在必须点进群才知道哪个 agent 卡住了。

## 二、方案

用彩色状态符号替代固定前缀，让会话列表兼作状态仪表盘。

改造前后：

```
改造前                                改造后
herdr · yqg-dw-datapilot6            🔴 yqg-dw-datapilot6
herdr · dolphinscheduler-newnew3     🟡 dolphinscheduler-newnew3
herdr · herdr-remote                 🟢 herdr-remote
herdr · 2026-hackson                 ⚪️ 2026-hackson
```

一眼可见谁需要处理，不必逐个点进去。

### 符号映射

复用代码里已有的状态语义（`_STATUS_COLORS`，`relay/herdr_lark.py:1120`），
把颜色名换成对应的彩色 emoji。四态全部保留彩色，靠颜色区分而非形状——
四个符号等宽，项目名起始位置对齐。

| status | 现有颜色 | 群名符号 | 含义 |
|--------|---------|---------|------|
| `blocked` | red | 🔴 | 要人动手 |
| `working` | orange | 🟡 | 在忙 |
| `done` | green | 🟢 | 好了 |
| `idle` | grey | ⚪️ | 闲着 |
| `unknown` | grey | ⚪️ | 闲着（同 idle） |

已实测飞书群名接受这四个 emoji：改名 API 返回 `code 0`，回读群名字符完整。

新群名格式：`<符号> [标记] <项目名>`。

重名消歧标记（`disambiguate_suffixes`）保持不动——它解决两个同名 agent
的问题，与状态正交。标记仍放在项目名前面，理由不变：会话列表尾部会被截断。

未绑定 agent 的群（`/unbind` 之后）名为 `herdr`，与现状一致。

`CHAT_TITLE_PREFIX = "herdr · "` 这个常量删除。它现有两个用途，分别接管：

- 生成群名的前缀 → 由状态符号取代
- `/unbind` 重置群名时的 `CHAT_TITLE_PREFIX.rstrip(" ·· ")`（求值为 `"herdr"`，
  `relay/herdr_lark.py:2838`）→ 换成新常量 `UNBOUND_CHAT_NAME = "herdr"`，
  避免继续用 `rstrip` 这种绕弯的写法表达一个字面量

## 三、群名的角色转变

这是本设计的核心风险：群名从**静态标识符**变成**动态展示层**。
有三处代码在拿群名当标识符用，必须一起改，否则功能会坏。

### 3.1 `/spaces` 的群复用

`find_existing_chat`（`relay/herdr_lark.py:1480`）靠群名精确匹配判断
「这个 agent 已经有群了，复用它」。

群名带上会变的状态符号后必然失配：agent 从 `working` 变 `done`、
群名从 `🟡 x` 变 `🟢 x`，`/spaces` 认不出这个群，会**重复建群**。

改为按绑定关系匹配。`lark_bindings.json` 的 `{chat_id: pane_id}` 是事实源，
群名只是展示。查绑定比查群名可靠——人手改了群名也认得出。

复用判定：`pane_id` 在绑定表里已有对应 `chat_id`，且该 `chat_id` 仍在
授权群列表内（群可能已被解散）。

### 3.2 observer 的项目群判定

`_chat_covers`（`relay/herdr_lark_observer.py:582`）用 `startswith("herdr")`
判断「这是不是 herdr 的项目群」，只有项目群的消息参与质检对账。

前缀去掉后会把**所有项目群判为无关群**，对账全废（漏发检测失效）。

改为按状态符号判定：群名以四个状态符号之一开头即为项目群。
保留 `herdr` 开头的判定，覆盖未绑定的空闲群。

注意 observer 不 import `herdr_lark`（刻意的进程隔离，见 4.1），
判定函数与符号表在 observer 侧各有一份副本。

### 3.3 改名节流（新增）

**实测发现：每次改群名都会在群里留下一条 `system` 消息**
（`{from_user} updated the group name from ...`）。连续改名 3 次产生 3 条。

relay 每 2 秒推一次状态（`POLL_INTERVAL`，`relay/herdr_relay.py:47`），
不节流会刷屏——比原来浪费宽度的问题严重得多。节流是必需项，不是优化。

节流规则：

| 规则 | 值 | 理由 |
|------|-----|------|
| 防抖 | 状态稳定持续 30s 才改名 | 穿越性抖动（working⇄idle）不触发 |
| `blocked` 例外 | 立即改名，不等防抖 | 唯一需要立刻知道的状态，延迟无意义 |
| 最小间隔 | 同一群两次改名间隔 ≥60s | 兜底，防御未预料的抖动模式 |
| 幂等 | 目标名与当前名相同则跳过 | 不产生无意义的 API 调用与系统消息 |

`blocked` 例外优先于最小间隔：进入 `blocked` 无条件立即改名。
离开 `blocked` 走正常防抖。

状态变化信号复用现成的 `_track_updates`（`relay/herdr_lark.py:3470`，
已在跟踪 `old_status → new_status`），不新增轮询。

## 四、组件划分

新增一个纯函数层和一个有状态的节流器，两者可独立测试。

### 4.1 纯函数：群名生成

```
status_glyph(status: str) -> str
    状态 → 符号。未知状态回落 ⚪️。

chat_title_for(project: str, marker: str = "", status: str = "") -> str
    现有函数，加 status 参数。空 status 时不带符号前缀
    （兼容尚不知道状态的调用点）。

is_project_chat(name: str) -> bool
    群名是否为 herdr 项目群（状态符号开头，或未绑定的 "herdr"）。
```

`is_project_chat` 放在 `herdr_lark.py`，observer 里**复制一份**。

不共享代码是刻意的：observer 与 herdr_lark 是两个独立进程，
observer 现在完全不 import herdr_lark，靠注释约定手工同步
（见 `relay/herdr_lark_observer.py:183`「与 herdr_lark.py 保持同步」）。
本设计沿用这个既有约定，不引入新的跨进程代码依赖。

代价是符号表在两处维护。按现有惯例，在 observer 侧的副本上加同样的
同步注释，指明符号表以 `herdr_lark.py` 为准。

### 4.2 有状态：改名节流器

```
class ChatRenamer:
    职责：决定「现在该不该改这个群的名字」，并执行改名。

    decide(chat_id, target_name, status, now) -> str | None
        返回该改成的名字，或 None 表示按住不动。
        纯决策，不做 IO，可单测。

    apply(chat_id, target_name) -> None
        调 set_chat_name，异常吞掉只记日志。
```

决策与执行分离：`decide` 是纯函数式的（输入含 `now`，不读时钟），
时间和 IO 都在外层，测试不需要 mock 时钟或网络。

内部状态：每个 `chat_id` 记 `{当前显示的名字, 上次改名时刻, 待定状态, 待定起始时刻}`。
纯内存，不落盘。

重启后该表为空，此时「当前显示的名字」未知，幂等判定无从比较，
每个群都会被多余地改名一次（每次重启每群一条系统消息）。

这个量不能忽略：开发期重启频繁（当前日志里已有 47 次 `Bot ready`，
`BindingStore` 的注释也印证「重启恰恰是改完代码后最常做的事」），
而 `/spaces` 的目标是一 agent 一群、群数会长到十几个。
47 次重启 × 15 群 ≈ 700 条无谓系统消息。

因此启动时用 `chat_inventory()`（现有方法，`/spaces` 已在用）拉一次群名，
把「当前显示的名字」填进节流器作为基线。代价是一次 API 调用，
换掉每次重启的整轮无谓改名。

拉取失败时降级为空基线——退回上面那种「每群多改一次」的行为，不阻断启动。

## 五、错误处理

| 情况 | 行为 | 理由 |
|------|------|------|
| 改名 API 失败 | 记 warning，继续 | 沿用现有 `set_active` 的做法：改名是展示，不是功能前提 |
| 改名失败后重试 | 不主动重试，等下次状态变化 | 避免失败时反复打 API；状态每 2 秒推一次，机会很多 |
| 群已被解散 | 改名失败，同上 | 授权列表清理由现有逻辑负责 |
| 状态字段缺失 | 视为 `unknown` → ⚪️ | 与现有 `_STATUS_ICONS` 的回落一致 |
| 绑定表指向已消失的群 | `/spaces` 视为无群，新建 | 现有 `prune_stale_bindings` 会清理 |
| 启动时拉群名失败 | 记 warning，空基线启动 | 不阻断启动；代价只是每群多改一次名 |

## 六、测试

沿用 `tests/test_lark.py` 的风格（纯函数直测，不起服务）。

纯函数：

- `status_glyph` 四态映射 + 未知状态回落
- `chat_title_for` 带 status / 不带 status / 带 marker 的组合
- `chat_title_for` 长项目名截断时保住符号与标记（现有不变式）
- `is_project_chat` 认四种符号开头、认 `herdr`、拒普通群名

节流器 `decide`（传入递增的 `now`，无需 mock 时钟）：

- 状态稳定不足 30s → None
- 状态稳定超过 30s → 返回目标名
- 抖动（working→idle→working）在 30s 内 → 全程 None
- 进入 blocked → 立即返回，不等防抖
- 进入 blocked 且距上次改名不足 60s → 仍立即返回（例外优先）
- 离开 blocked → 走正常防抖
- 目标名与当前名相同 → None（幂等）
- 距上次改名不足 60s 且非 blocked → None
- 有基线且群名已正确 → None（重启后不多余改名）
- 无基线（拉取失败）且群名已正确 → 走防抖后改名一次（可接受的降级）

`/spaces` 复用：

- agent 已有绑定 → 复用该群，不新建
- agent 有绑定但群已不在授权列表 → 新建
- agent 无绑定 → 新建
- 群名被人手改过 → 仍能通过绑定认出，不重复建

回归：

- `tests/test_lark.py:1147` 和 `:2342` 断言了 `CHAT_TITLE_PREFIX`，需同步更新
- observer 侧 `_chat_covers` 的现有测试需覆盖新符号

## 七、不做的事

- 不改卡片里的 `_STATUS_ICONS`（`⏸ ▶ ✅ ○`）。卡片有足够空间显示文字标签，
  黑白符号在正文里更克制；群名才需要彩色抢眼。两处符号语义一致但形态不同，
  是刻意的。
- 不做改名失败的重试队列。状态每 2 秒推一次，下次变化自然会重试。
- 不把节流器状态落盘。启动时从飞书拉一次群名当基线即可（见 4.2），
  落盘会多一份可能与飞书实际群名不一致的副本。
- 不动重名消歧逻辑。
