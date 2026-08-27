# herdr-remote 飞书客户端操作手册

用飞书在手机上监控和指挥本机的 herdr agent。**无需公网入口** —— 不用 Cloudflare Tunnel、不用 Tailscale Funnel、不用 VPS。

最后更新：2026-08-22

---

## 一、它解决什么问题

原来从手机访问本机 agent 要把 relay 暴露到公网，走 Tailscale 时因双重硬 NAT 延迟 400–600ms。

飞书客户端换了条路：**两个方向都是 Mac 主动出站**。

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

relay 仍然只监听 localhost，飞书客户端只是它的一个本地客户端，和网页客户端平级。

---

## 二、日常使用

### 典型闭环

```
/agents          看有哪些 agent（带序号）
/read 1          看 1 号最近 40 行进展
继续改这个函数     直接打字 → 自动发给刚才读的那个 agent
/read 1          再看进展
```

**关键概念：一个群绑一个 agent。**

`/read`、`/send`、点卡片按钮 —— 任何一次交互都会把那个 agent 绑到当前群，并把**群名改成 `<状态符号> <项目名>`**，会话列表里一眼可见现在管的是谁、忙不忙。

**群名开头的符号就是 agent 状态**，不用点进群就知道该先处理哪个：

| 符号 | 状态 | 含义 |
|---|---|---|
| 🔴 | blocked | 等你批准，要动手 |
| 🟡 | working | 正在跑 |
| 🟢 | done | 完工了 |
| ⚪️ | idle | 没事干 |

```
🔴 yqg-dw-datapilot6        ← 卡住了，先看这个
🟡 dolphinscheduler-newnew3
🟢 herdr-remote
⚪️ 2026-hackson
```

早先用的是统一的 `herdr · ` 前缀，但每个群都一样，把真正要看的项目名挤掉了。换成符号后同样的宽度还能表达优先级。

**符号会滞后半分钟。** 改群名会在群里留一条「XXX 修改群名为…」的系统消息，而 relay 每 2 秒推一次状态 —— 状态一变就改名会刷屏。所以改名做了节流：状态稳定 30 秒才改，同一个群两次改名至少隔 60 秒。**例外是 blocked，它立即改** —— 那是唯一要你马上动手的状态，等半分钟没意义。

**同名 agent 会带上区分标记**，例如两个群分别绑同一项目的两个 agent：

```
🟡 [w1B] yqg-dw-datapilot
🟢 [w22] yqg-dw-datapilot
```

**标记放在项目名前面** —— 会话列表宽度有限，尾部会被截掉，放后面等于看不见。前 20 个字符就能分清两个群。

标记规则与 `/agents` 列表一致：父目录能区分就用父目录（`[frontend]` / `[backend]`），否则用 workspace id。项目名太长时宁可截项目名，也要保住标记。

绑定后：

- 直接打字就是发给它，不用每句都带序号（回 `→ 已发给 tailcale`）
- 它的完成通知、blocked 审批**只推到这个群**，不会和别的 agent 互相刷屏
- 想换人就再 `/read <序号>`，群名跟着变

**没绑过的 agent 收不到推送。** 通知只发显式绑过的群，没有「默认群」兜底：

```
你只在 A 群 /read 过 herdr-remote
→ datapilot6 卡住了，飞书上不会有任何提示
   （用 /agents 主动看，或先 /read 一次把它绑到某个群）
```

这是有意的。早期版本会把「无主」通知倒进配置里的第一个群，结果那个群自己也绑了 agent —— datapilot6 的进展出现在 `herdr · herdr-remote` 群里，看的人以为那是 herdr-remote 的输出。**串群比丢通知更糟**：丢了你还知道要去查，串了你会读到错的东西。

**要同时盯多个 agent，就建多个群**，各自 `/read` 绑不同的 agent。15 个 agent 挤一个群会分不清谁是谁。

一键搞定：

```
/spaces dry    先看规划（复用几个、新建几个）
/spaces 3      只建 3 个，试试效果
/spaces        全部建齐
```

按群名匹配已有的群 —— **有就复用，不重复建**。新群建完自动绑好对应 agent，进群直接发消息即可。

多群配置：`HERDR_LARK_CHAT_ID` 支持逗号分隔，`/spaces` 建的群会自动加进授权列表。

### 手工建的群：配 `HERDR_LARK_USER_ID` 就不用登记了

群白名单要求「先建群、再查 chat_id、再改配置、再重启」，四步里三步是机械劳动。
漏一步的表现是**机器人在群里装死**——不报错，只是不理人。

配上 `HERDR_LARK_USER_ID`（你的 open_id）之后，你发的消息在任何群都放行，
并且这个群会被自动**收养**：登记进授权列表 + 把 observer 拉进去。手工建的群
直接可用，不必再动配置文件。

判据是**发消息的人**，不是群主。群主是你的群里也可能有别人，而过了守门就能用
`/reply`、`/send` 往 agent 终端塞任意文本——按群主放行等于把机器控制权交给
群里所有人。所以别人在群里 @ 机器人依然不生效，除非那个群本身在群白名单里。

两份白名单是**并集**：群命中或人命中都放行，现有配置照旧能用。

open_id 按应用隔离，同一个人在不同应用下的 open_id 不一样。填错了的表现同样是
「装死」，所以入站日志会打 `sender=`，照着日志抄即可。

### `/git` 的输出

```
herdr-remote
⎇ feat/lark-client
↕ 2 个提交未推送
未提交 2 个文件：
  M  relay/herdr_lark.py
  M  tests/test_lark.py
```

**未推送的提交**和**未提交的文件**是两件事，所以分两行显示：前者已经 commit
只是没 push，后者连 commit 都没有。工作区干净但有 `ahead` 是最容易忘的状态——
只说「干净」会让人以为活都交出去了。

### 群描述里维护 space 的额外信息

群信息页的描述会自动同步成 `⎇ 分支 · agent 类型 · @远程主机 · 路径`，例如：

```
⎇ feat/lark-client ↑2 · claude · /Users/victor/code-github/herdr-remote
```

`↑2` 是**未推送的提交数**，`↓1` 是落后远端的数量，`↑?` 表示这个分支还没有上游
（一次都没 push 过）。这段数据不需要额外查 git —— `git status --porcelain -b`
的分支行本来就带 `[ahead 2]`，relay 早就原样带回来了。

**为什么不是群公告**：飞书的群公告是独立的 docx，`im/v1/chats` 返回的字段里
根本没有公告项，API 改不了。群描述是唯一能写的地方。

分支查不到时**这一项直接省掉**，不写「分支: 未知」——留空比显示一个可能过时的
值好。本地主机不写（本地是常态），远程一定写，否则会以为在本地跑、找错机器。

改描述和改群名一样会在群里留系统消息，所以走同一套节流（防抖 30 秒 + 最小间隔
60 秒），且内容没变就不提交。分支另有 5 分钟 TTL 缓存：描述同步挂在 2 秒一帧的
状态循环上，不缓存的话一个 agent 一天要查四万次 git，远程的还得走 SSH。

**绑定会落盘**（`lark_bindings.json`），服务重启后自动恢复，不用重新 `/read`。

**同一个群的消息串行处理**：连发两条时，`send_text` 的「粘贴 + 回车」不会交错糊成一条；不同群之间并行，互不阻塞。

> 群名改失败（权限不足等）不影响绑定 —— 改名只是让人看得清楚，不是功能前提。
>
> 飞书的群公告是 docx 类型，API 改不了（`232097 Unable to operate docx type chat announcement`），所以用群名。群名在会话列表里本来就更醒目。

### 审计回执

每次写操作都在**发起它的那个群**里留一行痕迹，不再另设审计群：

```
→ send  tailcale (w1X:p1)  继续改这个函数
✓ approve  tailcale (w1X:p1)  选项 2
🔓 trust  niuma (w19:p1)
⛔ interrupt  herdr (w21:p1)
✚ new  herdr (w22:p1)  启动 codex
```

覆盖 8 个写操作点：`send` / `approve` / `trust` / `interrupt` / `new` 等。

**为什么落在本群而不是单独一个审计群**：操作发生在哪个群，追溯就该在哪个群。
一个项目一个群，痕迹和上下文（指令、pane 输出、完成通知）在同一条时间线上，
不用切到别处对时间戳；也不必再为「审计群能看到全部项目的指令内容」单独设一层只读权限。

两条约束：

- **内容脱敏** —— 指令里可能带密钥，`scrub()` 会滤掉；超长内容截断到 200 字
- **发不出去不影响主流程** —— 审计只记日志，不让留痕失败连带把指令搞挂

想关掉：

```bash
HERDR_LARK_AUDIT=off
```

### 查用量

`/usage` 按两个计费周期分别统计 Claude 的 token 消耗：

```
Claude 用量

5 小时窗   1.1M tokens
   08/23 10:00 起 · 2小时6分后重置
   486 条消息 · 3 个会话
   opus-5 323.7K

本周   71.3M tokens
   08/17 00:00 起 · 11小时6分后重置
   18746 条消息 · 212 个会话
   opus-5 9.3M · haiku-4-5 281.2K · sonnet-5 244.6K

本周项目 Top3
   datapilot6  2.7M
   subagents  2.1M
   tailcale  928.2K
```

数据源是 Claude Code 自己写的会话日志 `~/.claude/projects/**/*.jsonl`，
每条助手消息都带 usage 明细。只扫 mtime 在窗口内的文件，1290 个日志
（约 700MB）跑完不到 1 秒。

**5 小时窗按整点对齐**（0/5/10/15/20 点），不是「从现在往前推 5 小时」——
滑动窗口的话「还剩多久重置」就没有意义了。

**周起点默认周一**，但 Anthropic 的周额度按订阅日重置，未必是周一：

```bash
HERDR_USAGE_WEEK_ANCHOR=6    # 改成周日起算
```

**缓存读取不计入总量**。它按折扣计费，和「烧掉多少额度」不是一个口径 ——
本周缓存读 2.8B 而实际总量 71M，把它算进来数字虚高几十倍，看不出真实消耗。
命令行加 `--detail` 能看到完整明细（含缓存读）。

#### 关于百分比

**额度上限拿不到**：它不在本地任何文件里（`stats-cache.json` 只有消息计数，
`policy-limits.json` 只有功能开关），只有 Anthropic 服务端知道。所以默认
只显示绝对用量，不假装算得出「用了百分之几」。

想看进度条，自己把上限填进去：

```bash
HERDR_USAGE_5H_LIMIT=2000000
HERDR_USAGE_WEEK_LIMIT=100000000
```

```
5 小时窗   ●●●●●○○○○○ 55%
   1.1M / 2.0M
```

命令行也能直接跑，和飞书里同一套口径：

```bash
uv run relay/herdr_usage.py           # 同 /usage
uv run relay/herdr_usage.py --detail  # 加缓存明细与耗时
```

### agent 停下来时会主动推

不用一直盯着。agent 从 working/blocked 变回 idle/done 时，自动推送：

```
✅ tailcale (claude) 停下来了

跑完了 152 个测试，全绿。下一步是…
```

**带着实际输出**，不是干巴巴一句 finished。推完自动绑为当前 agent，直接打字就能接着指挥。若它停在选择器上，还会多一张按钮卡片。

不会刷屏：首次见到的 agent 不推（否则一启动 15 个 idle 各推一条），转 blocked 也不推（那有专门的审批卡片）。

### 命令表

| 命令 | 作用 |
|---|---|
| `/start` 或 `/help` | 面板概览 + agent 选择卡片 |
| `/agents` | 列出全部 agent，**带稳定序号**，行首图标表状态；并附一张可点的卡片 |
| `/status` | relay 连接状态与 agent 计数 |
| `/git [序号]` | 分支、与远端的 ahead/behind、未提交的文件列表；不带序号用本群绑的 agent |
| `/read <序号>` | 读终端输出（200 行，清理后约 40 行）；TUI 折叠的消息会自动按 ↓ 展开 |
| `/reply <序号>` | 同 `/read`，并提示可直接回复 |
| `/send <序号> <内容>` | 发文本给指定 agent |
| `/trust <序号>` | 对 blocked 的 agent 发「trust, always allow」 |
| `/interrupt <序号>` | 发 Ctrl+C |
| `/digest` | 今日活动统计（工作时长、被阻塞次数） |
| `/usage` | Claude 用量：5 小时窗 + 本周，两个周期分别统计 |
| `/render card\|text` | 切换输出样式，见下 |
| `/watch [序号]` | 持续跟随，卡片自己刷新；`/watch stop` 停止 |
| `/new <序号> [类型]` | **新开一个 agent**，用该序号 agent 的目录 |
| `/autowatch on\|off\|<秒>` | 发完指令是否自动跟随（默认开，120 秒） |
| `/spaces [数量\|dry]` | **一键给每个 agent 拉一个群**，已有的复用 |
| `/unbind [drop]` | 解绑本群；`drop` 连群一起解散 |
| `/health` | 自检：连接、群、绑定、队列、跟随 |

**三种选 agent 的方式**，任选：

| 方式 | 用法 | 适合 |
|---|---|---|
| 序号 | `/read 3` | 已看过 `/agents`，最快 |
| 卡片按钮 | `/agents` 或 `/read`（不带参数） | 不想打字，点一下 |
| 名字 | `/read herdr` | 记得住名字 |

**序号是稳定的**：按 `pane_id` 排序，与状态无关。agent 开始干活、卡住、完成，序号都不会变。

> 早期版本按状态排序编号，结果 agent 一开始干活就跳到队首、其余全部后移 —— 你看到列表、几秒后打 `/read 3`，操作到的已经是别人了。现在状态用行首图标表示：`⏸` 待批、`▶` 在跑、`✅` 完成、`○` 空闲。

同名项目会自动带上区分标记：

```
✅  3. yqg-dw-datapilot (claude) [w1B]
○  10. yqg-dw-datapilot (claude) [w22]
▫  12. herdr-remote (shell)            ← 没开 agent，只是裸终端
```

**`▫` 表示这个 space 里没有正在跑的 agent**（herdr 报成 `agent="shell"` + `status="unknown"`）。往里面发消息只会被当成 shell 命令 —— 实际发生过：一个「1」进了 zsh，报 `command not found`。

所以发送前会拦一道：

```
⚠️ herdr-remote 里没有正在运行的 agent，发过去只会被当成 shell 命令。
用 /new <序号> 在它的目录下开一个，或 /agents 换一个。
```

`/new <序号>` 就是用来在这种空 space 里开 agent 的。

优先用父目录（`api [frontend]` / `api [backend]`），目录也一样时用 workspace id。不重名的不加标记，保持干净。

### 打错命令会被拦住

不带 `/` 的文本直接发给当前 agent，这是主路径。但**形似命令的错拼会先拦一道**：

```
你: /raed 3
    没有 /raed 这个命令，你是不是想用 /read？
    确认要把这行原样发给 agent 的话，去掉开头的 / 再发一次。
```

不拦的话，`/raed 3` 会被当成普通文本原样粘进终端 —— 实际发生过。

猜不出来的（比如 `/xyzzyplugh`，或 `/Users/victor/foo.py` 这种路径）照旧放行，不会挡住正常输入。

### 新开一个 agent：`/new`

不只是操控已有的 agent，也能从飞书开新的：

```
/new 3         在 3 号的目录下开一个 claude
/new 3 codex   同上，但用 codex
/new           看用法与可选类型
```

用序号指目录 —— 手机上打全路径太痛苦，直接抄现有 agent 的 `cwd` 最省事。

建完**自动绑定当前群**，直接打字就能下指令。

支持的类型（`herdr agent start --kind`）：

```
claude codex gemini cursor opencode copilot kimi kiro droid amp grok
hermes kilo qodercli maki pi devin agy cline omp mastracode
```

> 实现上分两步：relay 的 `create_workspace` 建一个空 shell 工作区（它写死了不带 `--cwd`），再用 `send_text` 把 `cd <目录> && <类型>` 打进去。所以不需要改 relay。

### 发完指令自动跟随

**默认开着**：发完指令自动起一张流式卡片，不用再打 `/watch`。

收工条件哪个先到算哪个：

- agent 停下来（连续几轮 idle/done）
- 到时限（默认 120 秒）

```
/autowatch        看当前设置
/autowatch off    关闭
/autowatch on     打开
/autowatch 180    打开并设为 180 秒（20–600）
```

也可用环境变量设默认：`HERDR_LARK_AUTOWATCH=off` 或 `=180`。

> 时限只是兜底 —— agent 30 秒干完就 30 秒收工，不会干等满 120 秒。反过来，卡住不动时也不会一直跟着白烧配额。
>
> 手工 `/watch` **不限时**，那是你主动要看的。

### 持续跟随：`/watch`

agent 正在干活时，不用反复 `/read`：

```
/watch 1      跟随 1 号，卡片自己刷新
/watch        跟随当前绑定的 agent
/watch stop   停止
```

卡片每 1.5 秒更新一次，**内容没变则不推**。agent 连续 3 轮处于 idle/done 就自动收工 —— 一直跟着白烧飞书配额。

一个群同时只跟一个 agent；再发 `/watch` 会先停掉上一个。

> 流式更新用的是飞书 CardKit：`schema 2.0` + `streaming_mode`，按 `element_id` 定位、`sequence` 保序。sequence 必须单调递增，重复或倒退会丢帧。
>
> 创建卡片失败时会提示改用 `/read`，不会静默失败。

### 输出样式：卡片 / 纯文本

两种都保留，随时可切，不用重启：

```
/render          看当前模式
/render card     彩色卡片（默认）
/render text     纯文本
```

| 模式 | 样子 | 适合 |
|---|---|---|
| `card` | 头部按状态上色（▶ 蓝 / ⏸ 橙 / ✅ 绿 / ○ 灰），主体等宽代码块 | 默认，读终端输出最清楚 |
| `text` | 纯文本，无任何标记 | 客户端渲染有问题时、或想要最小体积 |

也可以用环境变量设默认值：`HERDR_LARK_RENDER=text`。

> **颜色不是从终端"保留"下来的。** `herdr pane read` 输出的是纯文本，ANSI 转义序列在 relay 那层就没了（实测 0 处）。卡片里的颜色是按 agent 状态**重新上色**的。
>
> 代码块内部无法上色 —— 飞书的 ``` 是纯文本渲染，`<font>` 标记不生效。所以取舍是：**对齐给主体，颜色给状态**。`⏺`/`❯`/`✻` 这些符号本身已足够区分语义。
>
> 体积上卡片反而更省：长输出 5189 → 2809 字符（卡片截断阈值 2400，比纯文本的 3000 更紧）。

### 图片自动发送

agent 输出里提到本地图片路径时，**自动传到飞书**：

```
⏺ 截图已保存到 /tmp/shot.png
   → 飞书里直接看到这张图，不用跑回电脑
```

规则：

- 只认**绝对路径**（相对路径无从定位 —— agent 的 cwd 和客户端不一定一致）
- 支持 png / jpg / jpeg / gif / webp
- **校验文件头**，不只看扩展名（输出里的 `.png` 可能只是一段文字）
- 单条最多 3 张、单张最大 8 MB
- 上传失败只记日志，不影响正常输出

需要 `im:resource` 权限（见第四节权限清单）。

### 遇到选择器

agent 弹出选项时（权限确认、`AskUserQuestion`、plan 批准），两条路径都支持：

- **agent 主动卡住** → 自动收到橙色 blocked 卡片
- **`/read` 时恰好卡着** → 自动补一张青色「⌨︎ 正在等你选」卡片

选择方式二选一：

- 点卡片按钮
- **直接回一个数字**（如 `2`）

> 直接打数字之所以有效，是因为客户端会识别成**按键**而非文本。发文本是没用的 —— relay 用 send-text 粘贴，Claude 的 TUI 把粘贴内容里的换行当正文而不是回车，提示永远确认不了。

能识别三种选择器形态：

```
工具权限         1. yes, single permission / 2. trust, always allow / 3. no
AskUserQuestion  1. 直接改现有函数 / 2. 新增一层抽象 / 3. 先写测试
箭头样式        ❯ 1. Yes / 2. Yes, and don't ask again / 3. No
```

散文里的编号列表（「改动如下：1. 修了 a」）**不会**误判成选择器 —— 要求编号 1..n 连续，且必须紧贴输出末尾。

**多组问题**（`AskUserQuestion` 一次问好几个）也能处理：

- TUI 是**逐组问**的：答完第一组才显示第二组
- 卡片**只推 agent 当前在等的那一组**，并显示它的问题
- 答完自动推下一组，直到全部答完

> 之所以不同时展示所有组：两组都有 `1/2/3`，你若先点第二组，那个数字会被当成第一组的答案 —— 静默答错，最难排查。

### 审批时效

每条 blocked 通知带一个 5 字母的 generation。点击旧通知的按钮会被拒绝：

> 那条审批属于更早的提示，请用最新一条 blocked 通知上的按钮。

agent 一旦离开 blocked 状态，对应按钮立即失效。这是防止你点到三小时前那条通知。

---

## 三、启动与运维

### 开机自启（推荐）

```bash
cd ~/code-github/herdr-remote
./relay/install-lark-service.sh
```

装成用户服务：**开机自启 + 崩溃自动拉起**（`KeepAlive`，已实测）。

| 命令 | 作用 |
|---|---|
| `./relay/install-lark-service.sh` | 安装并启动 |
| `./relay/install-lark-service.sh status` | 查看状态 |
| `./relay/install-lark-service.sh restart` | 重启（改代码后用） |
| `./relay/install-lark-service.sh uninstall` | 卸载 |

安装前会自动检查：飞书凭据是否齐全、`uv` 是否存在、`HERDR_RELAY` 有没有带 token、**有没有前台进程在跑**（两个实例会抢同一条飞书长连接，集群模式随机投递，症状是事件时有时无）。

日志（**Python logging 走 stderr**，别查错文件）：

```bash
grep -v '^\[Lark\]\|^INFO:Lark' ~/Library/Logs/herdr-remote/lark-stderr.log | tail -20
```

底层命令（macOS）：

```bash
launchctl print   "gui/$(id -u)/com.herdr-remote.lark"
launchctl bootout "gui/$(id -u)/com.herdr-remote.lark"
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.herdr-remote.lark.plist
```

Linux 用 systemd user unit（`herdr-lark.service`），脚本自动识别。

### 手动启动（调试用）

```bash
cd ~/code-github/herdr-remote
set -a
. ~/.config/herdr-remote/secrets.env
[ -f ~/.config/herdr-remote/config.env ] && . ~/.config/herdr-remote/config.env
set +a
export HERDR_RELAY="ws://127.0.0.1:8375?token=${HERDR_RELAY_TOKEN}"
uv run relay/herdr_lark.py
```

**relay 若启用了 token，必须带上 `?token=`**，否则会一直 `HTTP 401` 重连。

### 启动成功的样子

```
INFO:herdr-lark:Bot ready: ou_8eb545d1317919fd1554ff1272291b79
INFO:herdr-lark:Loaded 5 seen message ids
INFO:herdr-lark:Lark long connection thread started
INFO:herdr-lark:Connected to relay at ws://127.0.0.1:8375
INFO:Lark:connected to wss://msg-frontier.feishu.cn/ws/v2?...
```

四条都出现才算就绪：机器人身份、去重缓存、飞书长连接线程、relay 连接。

### 后台运行

```bash
nohup uv run relay/herdr_lark.py > /tmp/lark_run.log 2>&1 &
```

只看自己的日志（滤掉 SDK 噪音）：

```bash
grep -v "^\[Lark\]\|^INFO:Lark" /tmp/lark_run.log
```

> 开机自启用 `install-lark-service.sh`（见上）。主 `install-service.sh` 只管 relay / tunnel / telegram。

### 配置项

| 环境变量 | 用途 | 默认 |
|---|---|---|
| `HERDR_LARK_APP_ID` | 飞书自建应用 App ID | 必填 |
| `HERDR_LARK_APP_SECRET` | App Secret | 必填 |
| `HERDR_LARK_CHAT_ID` | 授权会话；留空则进入发现模式（响应任何会话） | 空 |
| `HERDR_LARK_USER_ID` | 授权的人（open_id，逗号分隔）；这些人发的消息在任何群都放行，并自动收养该群 | 空 |
| `HERDR_LARK_DOMAIN` | `feishu`(国内) / `lark`(海外) | `feishu` |
| `HERDR_LARK_SEEN_PATH` | 去重缓存路径 | `~/.config/herdr-remote/lark_seen.json` |
| `HERDR_LARK_BINDING_PATH` | 群↔agent 绑定落盘路径 | `~/.config/herdr-remote/lark_bindings.json` |
| `HERDR_LARK_RENDER` | 输出样式 `card` / `text` | `card` |
| `HERDR_LARK_AUTOWATCH` | 自动跟随 `off` / 秒数 | 开，120 秒 |
| `HERDR_LARK_AUDIT` | 审计回执开关 `off` 关闭 | 开 |
| `HERDR_USAGE_5H_LIMIT` | 5 小时窗 token 上限（配了才显示百分比） | 空 |
| `HERDR_USAGE_WEEK_LIMIT` | 周 token 上限 | 空 |
| `HERDR_USAGE_WEEK_ANCHOR` | 周额度从哪天起算，0=周一…6=周日 | 0 |
| `HERDR_RELAY` | relay 地址（含 token） | `ws://127.0.0.1:8375` |

凭据存 `~/.config/herdr-remote/secrets.env`，权限 0600。

代码内可调常量（`relay/herdr_lark.py` 顶部）：

| 常量 | 值 | 说明 |
|---|---|---|
| `READ_LINES` | 200 | 读取行数，relay 上限 5000 |
| `AGENT_PAGE_SIZE` | 20 | 卡片每页 agent 数 |
| `SEEN_LIMIT` | 5000 | 去重缓存上限，超限删最旧一半 |
| `PENDING_LIMIT` | 500 | 待回复消息映射上限 |

---

## 四、飞书应用配置

一次性配置。当前应用：`demo`（建在个人企业「盛粥粥」下）。

### 1. 创建应用

https://open.feishu.cn/app → 创建企业自建应用

> 企业租户可能禁止自建应用或需管理员审批。个人企业无此限制。

### 2. 启用机器人能力

「添加应用能力」→「机器人」→ 添加

不加这一步，所有消息 API 返回 `11205 app do not have bot`。

### 3. 开通权限

「权限管理」，需要这八个（全部免审）：

```
im:message                        收发消息
im:message:send_as_bot            以机器人身份发送
im:message.p2p_msg:readonly       读单聊消息
im:message.group_at_msg:readonly  读群里 @ 机器人的消息
im:message.group_msg              读群里全部消息      ← 关键，见下
im:chat                           群信息（判断单人群）
cardkit:card:write                创建与更新卡片
im:resource                       上传图片
```

快捷方式：直接访问带参数的 URL，权限会预填在弹窗里

```
https://open.feishu.cn/app/<APP_ID>/auth?q=im:message,im:message:send_as_bot,im:message.p2p_msg:readonly,im:message.group_at_msg:readonly,im:message.group_msg,im:chat,cardkit:card:write,im:resource&op_from=openapi&token_type=tenant
```

**`im:message.group_msg` 是必须的**：即使群里只有你一个人，`chat_mode` 仍是 `group`。没有这个权限，飞书**只推送 @ 了机器人的消息**，裸 `/status` 完全没反应 —— 而且客户端侧看不到任何日志，因为事件压根没送到。

### 4. 配置长连接 —— 两个独立页面

> **顺序很重要：先启动本地程序，再来后台保存配置。** 保存时飞书会校验长连接是否在线，程序没跑会保存失败。

**「事件与回调」→「事件配置」**
1. 订阅方式 → 点铅笔图标 → 选「使用长连接接收事件」
2. 点「验证」，应显示**连接成功**
3. 保存
4. 「添加事件」→ 搜「接收消息」→ 勾选 `im.message.receive_v1` → 添加

**「事件与回调」→「回调配置」**（**这是另一个页签，极易遗漏**）
1. 订阅方式 → 同样选长连接 → 验证 → 保存
2. 「添加回调」→ 勾选**卡片回传交互** `card.action.trigger`（新版，不是 `_v1` 旧版）→ 添加

> 只配事件不配回调的后果：发消息正常，但**卡片按钮点了没反应**。这种半通状态最难排查。

### 5. 发布版本

「版本管理与发布」→ 创建版本 → 保存 → **确认发布**

个人企业免审，提交即生效。权限和事件配置改动**都需要重新发布**才生效。

注意：保存后状态是「待申请」，必须再点「确认发布」才变「已发布」。

### 6. 让机器人能找到你

在飞书里搜索机器人名称并发一条消息，即可建立会话。取 `chat_id`：

```bash
TOKEN=$(curl -s -X POST 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal' \
  -H 'Content-Type: application/json' \
  -d '{"app_id":"<APP_ID>","app_secret":"<APP_SECRET>"}' \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['tenant_access_token'])")

curl -s 'https://open.feishu.cn/open-apis/im/v1/chats?page_size=20' \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## 五、排查

### 先发 `/health`

```
✓ relay  ws://127.0.0.1:8375
✓ 飞书长连接（收到本条即证明）

agent  16 个，其中 12 个有 agent 在跑
群     16 个授权，16 个已绑定
队列   0 条排队，0 个跟随中
去重   40 条消息 id

渲染 card  ·  自动跟随 开（120s）
```

能收到这条回复本身就证明飞书长连接是通的。relay 那行显示 `✗` 说明本地 relay 没连上。



### 发消息没反应

按这个顺序查：

**1. 事件到了吗？**

```bash
# 服务方式运行（Python logging 走 stderr）
grep -v "^\[Lark\]\|^INFO:Lark" ~/Library/Logs/herdr-remote/lark-stderr.log | tail -20
# 前台调试时
grep -v "^\[Lark\]\|^INFO:Lark" /tmp/lark_run.log
```

有 `inbound message chat=... text=...` 说明事件到了，后面的 `dropped:` 会说明为什么被丢弃。

**2. 一条日志都没有** → 事件根本没送到，问题在飞书侧：

- 群聊没 @ 机器人，且没开 `im:message.group_msg`
- 事件配置未设为长连接，或未添加 `im.message.receive_v1`
- 配置改了但**没发布版本**
- 客户端启动**早于**配置发布 → 重启客户端

**3. 用最小探针隔离**（区分「没送达」和「送达但被我的代码丢了」）：

```bash
cat > /tmp/probe.py <<'PY'
# /// script
# dependencies = ["lark-oapi>=1.4.0"]
# ///
import os, lark_oapi as lark
def on_msg(d): print("### 收到消息 ###", lark.JSON.marshal(d)[:400], flush=True)
h = lark.EventDispatcherHandler.builder("","").register_p2_im_message_receive_v1(on_msg).build()
lark.ws.Client(os.environ["HERDR_LARK_APP_ID"], os.environ["HERDR_LARK_APP_SECRET"],
               event_handler=h, log_level=lark.LogLevel.DEBUG).start()
PY
set -a; . ~/.config/herdr-remote/secrets.env; set +a
uv run /tmp/probe.py
```

探针也收不到 → 确定是飞书配置问题，不用查代码。

**4. 直接查会话历史**（需 `im:message.group_msg`）—— 能看到机器人到底回没回：

```bash
curl -s "https://open.feishu.cn/open-apis/im/v1/messages?container_id_type=chat&container_id=<CHAT_ID>&page_size=5&sort_type=ByCreateTimeDesc" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

> 曾经发生过：日志看着没反应，查历史发现**机器人一秒内就回复了** —— 成功路径当时没打日志，误判成故障。现在已补上入站日志。

### 卡片按钮点了没反应

「回调配置」没设长连接，或没添加 `card.action.trigger`。这是与「事件配置」分开的第二个页签。

### 服务没起来

```bash
./relay/install-lark-service.sh status
```

`state = running` 才算正常。反复重启看 `last exit code`，再查 stderr 日志。

常见原因：`secrets.env` 里缺飞书凭据、`HERDR_RELAY` 没带 token、有前台进程抢占长连接。

### relay 一直 401

`HERDR_RELAY` 没带 token：

```bash
export HERDR_RELAY="ws://127.0.0.1:8375?token=${HERDR_RELAY_TOKEN}"
```

### 输出里塞满界面装饰

`_CHROME_PATTERNS`（`relay/herdr_lark.py`）是个正则列表，加一行即可。当前已滤掉：

```
分隔线、Context ███░░░ 34%、[Opus 5 (1M context)] │ xxx、
⏵⏵ bypass permissions、Update installed、空 ❯ 行、Usage ██░░、
HTML 注释 <!-- ... -->、<br> 标记、
表格边框 ┌─┬─┐ └─┴─┘ ├─┼─┤（含各种横线变体 ─━═—–·⎯⏤）
```

**表格的竖线也会去掉**：行首尾的 `│` 纯占地方，列之间换成空格。Markdown 表格在终端里被渲染成边框，手机窄屏上本来就会错行 —— 留个残缺的框不如只留内容。

**所有空行也会去掉。** Markdown 的段落间距在终端输出里能占到四成篇幅（实测 40 行里 16 行是空的），手机窄屏上等于一半内容被挤出屏幕。

保留 `✻ Drizzling… (12s)` 这类进度行 —— 那是「还在跑」的有用信号，不是装饰。

实测效果：某个 pane 47 行 → 21 行，字符数 928 → 371（**省 61%**）。

---

### 加新命令怎么改

**只改一处**：`relay/herdr_lark.py` 里的 `COMMAND_HELP` 列表。

```python
COMMAND_HELP = [
    {"group": "看", "name": "agents", "args": "", "desc": "列出全部 agent"},
    ...
]
```

`COMMANDS` 集合和 `/help` 文案都从它派生，不会出现「加了命令但帮助里没有」。

`group` 为空表示别名（如 `/start`、`/reply`），不在帮助里单列。

有测试盯着一致性 —— 往 `COMMANDS` 里塞一个没写帮助的命令，`test_commands_derived_from_registry` 会挂。

## 六、设计要点（改代码前必读）

以下四条踩过坑，改动时容易重蹈覆辙。

**1. 选项确认必须发按键，不能发文本**

发选项文本用 `respond` 是无效的：relay 走 send-text 粘贴，Claude 的 TUI 把粘贴内容里的换行当作正文而非回车，提示永远确认不了。必须 `send_keys(pane_id, ["2"])` 按数字键。

**2. `send_keys` 必须等 relay 的 ack**

不等 ack 就报成功会造成**假成功**：relay 的 `SAFE_KEYS` 白名单只认 `C-c` 这类名字，发 `Ctrl+C` 会被整条拒绝，用户看到的却是「已发送」。

`SAFE_KEYS`（`herdr_relay.py:113`）：`y n a Enter Tab Escape C-c Up Down Left Right backspace Space 0-9`

**3. 飞书回调必须 3 秒内返回**

处理器立刻返回，实际工作用 `run_coroutine_threadsafe` 投到 asyncio 主循环，结果靠新消息回传。

线程模型：飞书 SDK 的 `ws.Client.start()` 是阻塞同步调用，跑独立线程；relay 连接是 asyncio，跑主线程。

> 实测 `read_pane` 仅 **0.02–0.13 秒**，离 3 秒红线有两个数量级余量，无需降级方案。

**4. 消息去重是必需的**

飞书长连接会重推消息（Telegram 的 `update_id` 天然单调，无此问题）。去重状态持久化到 `lark_seen.json`，重启后不重放历史。

### SDK 的两个坑（文档没写，查源码才发现）

**导入路径**：`P2CardActionTriggerResponse` 在
`lark_oapi.event.callback.model.p2_card_action_trigger`，
**不在** `lark_oapi.api.cardkit.v1`。

**卡片事件字段位置**：`open_chat_id` / `open_message_id` **只在 `context` 下**，顶层没有。读顶层会永远拿到空 chat_id，**卡片按钮完全失效**。

```python
context = event.get("context") or {}
chat_id = context.get("open_chat_id") or event.get("open_chat_id", "")
```

---

## 七、测试

```bash
uv run tests/test_lark.py             # 单元测试（FakeWS，不碰 relay）
uv run tests/e2e_lark.py --read-only  # 端到端，只读
uv run tests/e2e_lark.py              # 端到端，含写操作
sh tests/run.sh                       # 全量（e2e 走只读；无 relay 则跳过）
```

### e2e 覆盖什么

单测用 `FakeWS` 模拟 relay，测不到时序 —— 而最严重的几个 bug 恰恰都出在真实链路上。e2e 打真实 relay，覆盖 8 组：

| 组 | 防的是什么回归 |
|---|---|
| relay 连通性 | 快照结构、必需字段 |
| 序号稳定性 | **序号漂移导致操作错对象** |
| 空 space 识别 | **消息误发进裸 shell** |
| 读取终端 | 清理效果、耗时、失败占位 |
| 发送与回车 | **回车丢失**（relay 要在粘贴后 settle） |
| 按键 ack | **假成功**（非法键名必须报错） |
| 选择器识别 | 单组 / 多组 / 散文误判 |
| 卡片构造 | 状态色、按钮唯一、审批发序号 |

写操作只挑 `idle` 的活 agent，发的是无害的 `echo <标记>`，再读回确认标记出现 —— 这是验证回车是否真的提交的唯一办法。

状态文件全部指向临时目录，**不碰 `~/.config/herdr-remote/`**。

> `tests/run.sh` 有 1 个**既有失败**：`telegram dashboard tests` 报 `NameError: KEY_ALIASES`。这是仓库原有问题，与飞书客户端无关（移除飞书文件后依然失败）。

---

## 八、相关文档

- 设计：`docs/superpowers/specs/2026-08-22-lark-client-design.md`
- 实施计划：`docs/superpowers/plans/2026-08-22-lark-client.md`
- 延迟问题背景：`../tailcale/PROBLEM.md`、`SOLUTIONS.md`
- 代码：`relay/herdr_lark.py`、`tests/test_lark.py`
- 服务安装：`relay/install-lark-service.sh`
