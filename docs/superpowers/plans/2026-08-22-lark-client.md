# 飞书客户端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `relay/herdr_lark.py`，能力对齐 `relay/herdr_telegram.py`，通过飞书长连接实现无公网入口的远程 agent 监控与操控。

**Architecture:** 纯函数（编码/解析/去重/卡片构造）先 TDD 落地并单测；飞书 WS 客户端跑独立线程，asyncio 主循环持有 relay 连接，两者用 `run_coroutine_threadsafe` 桥接以满足飞书 3 秒回调超时。

**Tech Stack:** Python 3.10+ + `lark-oapi` + `websockets`，PEP 723 内联依赖，`unittest`。

**Spec:** `docs/superpowers/specs/2026-08-22-lark-client-design.md`

---

## 前置阻塞项（必须先做）

- [ ] **Step 0: 确认企业租户可创建自建应用**

在[飞书开放平台](https://open.feishu.cn/app)尝试创建一个企业自建应用。若 `@fintopia.tech` 租户禁止自建应用或需管理员审批，**整个方案不可行**，需回退到 Telegram 版或申请审批。

确认可创建后，记录 `App ID` / `App Secret`，并添加 scope：

```
im:message
im:message:send_as_bot
im:message.p2p_msg:readonly
im:message.group_at_msg:readonly
im:chat
cardkit:card:write
```

**在此项确认前不要开始 Task 1。**

---

## File map

| File | Responsibility |
|------|----------------|
| `relay/herdr_lark.py` | 飞书客户端主体：纯函数 + 事件处理 + relay 监听 + 线程桥接 |
| `tests/test_lark.py` | 纯函数与事件处理单测 |
| `tests/run.sh` | 追加飞书语法检查与测试调用 |
| `relay/install-service.sh` | 飞书凭据引导 + LaunchAgent |
| `README.md` | 飞书接入章节 |
| `CLAUDE.md` | 组件表补一行 |

**关键常量**（与 Telegram 版保持同名，便于对照）：
`ACTION_CODES` / `CODE_ACTIONS` / `STATUS_ORDER` / `STATUS_LABELS` / `AGENT_PAGE_SIZE` / `PENDING_LIMIT` / `TOOL_BUTTONS` / `SUBAGENT_BUTTONS`

---

### Task 1: 纯函数骨架（TDD）

把不依赖飞书 SDK、不依赖网络的逻辑先落地。这部分可直接从 `herdr_telegram.py` 移植，是全部任务里最确定的一块。

**Files:**
- Create: `tests/test_lark.py`
- Create: `relay/herdr_lark.py`

- [ ] **Step 1: 写失败测试**

覆盖：

```python
# scrub 脱敏：APP_SECRET 与 relay token 都要被替换
def test_scrub_masks_app_secret_and_relay_token()

# pane token 往返
def test_pane_callback_token_stable()
def test_resolve_pane_token_unique_match()
def test_resolve_pane_token_multiple_matches_returns_none()  # 多匹配必须返回 None
def test_resolve_pane_token_falls_back_to_pending()

# action value 编解码
def test_action_value_roundtrip()
def test_parse_action_value_unknown_code_yields_invalid()

# agent 排序与标签去重
def test_sorted_agents_blocked_first()
def test_agent_button_labels_disambiguates_duplicates()

# 五字母 request id（借鉴官方 Channels）
def test_generation_id_is_five_lowercase_letters_without_l()
```

- [ ] **Step 2: 实现至通过**

从 `herdr_telegram.py` 移植：`scrub`、`pane_callback_token`、`resolve_pane_token`、`sorted_agents`、`agents_for_action`、`compact_identifier`、`agent_button_labels`、`register_pending`、`pending_pane`、`find_agent`。

新增 `new_generation()`：五个小写字母、排除 `l`，替代 `secrets.token_hex(4)`。

把 `pane_callback_data` 改名为 `action_value`，返回 **dict** 而非 JSON 字符串（飞书 `action.value` 原生是对象，无 64 字节限制）。

- [ ] **Step 3: 验证**

```bash
uv run tests/test_lark.py
```

---

### Task 2: 消息去重与持久化（TDD）

飞书特有，Telegram 版无对应物。

**Files:**
- Modify: `tests/test_lark.py`, `relay/herdr_lark.py`

- [ ] **Step 1: 写失败测试**

```python
def test_seen_ids_dedupes_repeat_message()
def test_seen_ids_evicts_oldest_half_over_limit()   # 上限 5000
def test_seen_ids_persist_roundtrip(tmp_path)
def test_seen_ids_load_tolerates_missing_or_corrupt_file()  # 损坏文件不得崩溃
```

- [ ] **Step 2: 实现**

`SeenStore` 类：`OrderedDict` 存 ID，超 5000 删最旧一半，持久化到 `~/.config/herdr-remote/lark_seen.json`，写入 mode 0600。加载失败降级为空集合并 warn，不抛。

- [ ] **Step 3: 验证** — `uv run tests/test_lark.py`

---

### Task 3: relay 通信层（TDD）

**Files:**
- Modify: `tests/test_lark.py`, `relay/herdr_lark.py`

- [ ] **Step 1: 写失败测试**

最关键的是 ack 那条 —— 这是 Telegram 版踩过的坑（假成功）：

```python
def test_send_keys_raises_when_relay_nacks()        # ok: false 必须抛
def test_send_keys_raises_on_error_message()
def test_send_keys_raises_when_no_ack_within_5_msgs()
def test_send_keys_succeeds_on_command_result_ok()
def test_read_pane_skips_non_pane_content_messages()  # 可能先收到 agents 广播
```

- [ ] **Step 2: 实现**

移植 `send_to_relay` / `send_keys_to_relay` / `read_pane` / `send_text_to_relay`。

`send_keys_to_relay` **必须**保留最多读 5 条消息、等 `command_result` + `command == "send_keys"` 的 ack 循环；`ok` 为假抛 `RuntimeError`。

`send_text_to_relay` 保持「先 `send_text` 再 `send_keys(["Enter"])`」两步。

- [ ] **Step 3: 验证** — `uv run tests/test_lark.py`

---

### Task 4: 卡片构造（TDD）

**Files:**
- Modify: `tests/test_lark.py`, `relay/herdr_lark.py`

- [ ] **Step 1: 写失败测试**

```python
def test_blocked_card_encodes_option_index_not_text()  # 必须发序号，不是选项文本
def test_blocked_card_uses_tool_buttons_for_trust_prompt()
def test_blocked_card_uses_subagent_buttons_for_approve_all()
def test_blocked_card_falls_back_to_tool_options_when_none()
def test_prompt_truncation_keeps_head_and_tail()       # 命令结尾不能丢
def test_agent_picker_card_paginates_at_20()
def test_agent_picker_card_omits_nav_on_single_page()
```

- [ ] **Step 2: 实现**

`build_blocked_card(pane_id, agent, project, prompt, options, generation)`：
- 按钮 `value` 存 **1-based 序号**（`"k"` 字段），不存选项文本
- 附带 `generation`（`"g"` 字段）
- 末行加「Open output & reply」按钮

`truncate_prompt(text, limit=400)`：超限时保留首尾，中间插 `⋯ 省略 N 字 ⋯`（借鉴官方 Channels，避免丢失命令结尾）。

`build_agent_picker_card(action, page)`：每页 20，多页时加上/下页按钮。

`send_card` → 失败回退 `send_markdown` → 失败回退 `send_text` 的降级链。

- [ ] **Step 3: 验证** — `uv run tests/test_lark.py`

---

### Task 5: 飞书事件处理（TDD）

**Files:**
- Modify: `tests/test_lark.py`, `relay/herdr_lark.py`

- [ ] **Step 1: 写失败测试**

用假事件对象，不连真飞书：

```python
def test_group_message_without_mention_ignored()
def test_bot_own_message_ignored()
def test_strip_bot_mention_removes_at_prefix()
def test_card_action_normalized_into_message_context()  # 点击 → 等效文本
def test_unauthorized_chat_id_rejected()
def test_stale_generation_rejected()                    # 陈旧审批必须拒绝
def test_generation_cleared_when_pane_leaves_blocked()
def test_handler_returns_within_deadline()              # 不得阻塞 3 秒
```

- [ ] **Step 2: 实现**

`on_message(ctx)` 统一入口，处理命令与自由文本。

`on_card_action(data)` 把 `action.value` 归一成与消息同构的 context，调用同一个 `on_message` —— 点按钮与打字走同一条路径。

**3 秒超时的处理**：两个 handler 都立即返回（卡片回调返回 toast），真正工作用 `asyncio.run_coroutine_threadsafe(coro, loop)` 投递到 asyncio 主循环，结果通过新消息或更新卡片回传。

审批分支：校验 `generation` 与 `approval_tokens[pane_id]` 相等，否则回「属于更早的提示」；通过则 `send_keys_to_relay(pane_id, [str(k)])`。

- [ ] **Step 3: 验证** — `uv run tests/test_lark.py`

---

### Task 6: 命令与 relay 监听

**Files:**
- Modify: `relay/herdr_lark.py`

- [ ] **Step 1: 实现命令**

对齐 Telegram 版九个：`/start` `/agents` `/status` `/read` `/reply` `/send` `/trust` `/interrupt` `/digest`。

`/interrupt` 只能用 `"C-c"`（relay `SAFE_KEYS` 白名单，见 `herdr_relay.py:113`），且必须走带 ack 的 `send_keys_to_relay`。

- [ ] **Step 2: 实现 relay 监听**

移植 `relay_listener`：持久 WS + 5s 重连；处理 `agents` / `agent_update` / `blocked` 三类消息；用 `agent_state.apply_agent_message` 合并状态；`track_agent_updates` 维护 `daily_stats` 与完成通知。

pane 离开 blocked 时清 `approval_tokens[pane_id]`。

- [ ] **Step 3: 线程桥接**

```python
loop = asyncio.new_event_loop()
threading.Thread(target=lark_ws_client.start, daemon=True).start()
loop.run_until_complete(relay_listener())
```

飞书 SDK 的 `cli.start()` 是阻塞同步调用，必须放独立线程；handler 内用 `run_coroutine_threadsafe` 回到主 loop。

- [ ] **Step 4: 验证**

```bash
python3 -c "import ast; ast.parse(open('relay/herdr_lark.py').read())"
uv run tests/test_lark.py
```

---

### Task 7: 真机联调

单测覆盖不到长连接握手，必须实测。

- [ ] **Step 1: 配置订阅方式**

注意顺序 —— 后台保存时长连接必须在线：

1. 先本地启动 `HERDR_LARK_APP_ID=... uv run relay/herdr_lark.py`
2. 保持运行，再去开放平台后台：
   - **事件与回调 > 事件配置** → 订阅方式选「使用长连接接收事件」→ 保存
   - **事件与回调 > 回调配置** → 同样选长连接 → 保存（**这是独立的一页，容易漏**）
3. 订阅事件：`im.message.receive_v1`、`card.action.trigger`

- [ ] **Step 2: 冒烟**

| 验证项 | 期望 |
|---|---|
| 手机发 `/agents` | 返回 agent 列表 |
| 手机发 `/read <project>` | 返回终端输出，**3 秒内不超时** |
| agent 进入 blocked | 手机收到卡片 |
| 点「Trust (always)」 | agent 实际继续（不是假成功） |
| 点陈旧卡片按钮 | 拒绝并提示 |
| 重启客户端 | 不重放历史消息 |
| 断网重连 relay | 自动恢复 |

- [ ] **Step 3: 实测 `read_pane` 耗时**

若偶发超过 3 秒，改为先回「读取中…」再更新卡片。记录实测数字到设计文档的「未决问题 4」。

---

### Task 8: 集成与文档

**Files:**
- Modify: `tests/run.sh`, `relay/install-service.sh`, `README.md`, `CLAUDE.md`

- [ ] **Step 1: `tests/run.sh` 追加**

参照现有 Telegram 检查项，新增一节：

```sh
echo "=== Lark bot ==="
python3 -c "import ast; ast.parse(open('$DIR/relay/herdr_lark.py').read())"
assert_eq "$?" "0" "herdr_lark.py parses"

grep -q "requires-python" "$DIR/relay/herdr_lark.py"
assert_eq "$?" "0" "inline deps present"

for cmd in cmd_start cmd_agents cmd_status cmd_read cmd_send cmd_reply cmd_trust cmd_interrupt cmd_digest; do
  grep -q "async def $cmd" "$DIR/relay/herdr_lark.py" || { FAIL=$((FAIL+1)); echo "  FAIL: missing $cmd"; }
done

uv run "$DIR/tests/test_lark.py"
assert_eq "$?" "0" "lark bot tests"
```

- [ ] **Step 2: `install-service.sh`**

新增飞书分支：交互式收集 `APP_ID` / `APP_SECRET` / `CHAT_ID`，写入 `~/.config/herdr-remote/secrets.env`（mode 0600），生成 `com.herdr-remote.lark` LaunchAgent。与 Telegram service 并存，互不影响。

引导文案必须说明「先起进程再存后台配置」的顺序，以及事件配置/回调配置是**两个独立页面**。

- [ ] **Step 3: 文档**

`README.md` 新增「飞书 Bot」章节（对照现有 Telegram 章节），强调无需 Tunnel。
`CLAUDE.md` 组件表加 `relay/herdr_lark.py` 一行，运行命令区加启动示例。

- [ ] **Step 4: 全量验证**

```bash
sh tests/run.sh
```

---

## 风险与对策

| 风险 | 对策 |
|---|---|
| 企业租户禁自建应用 | **Step 0 先验证**，不通过则整个方案作废 |
| 3 秒回调超时 | 全异步化；`read_pane` 慢则先回「读取中」再更新卡片 |
| 卡片回调未配长连接 | Task 7 Step 1 明确要求配置**两个**页面 |
| 多实例抢事件 | 单应用最多 50 连接且随机投递 —— 只跑一个实例 |
| 飞书重推消息 | Task 2 的去重 + 持久化 |
| `send_keys` 假成功 | Task 3 的 ack 循环，测试强制覆盖 |
| 审批串号 | Task 5 的 generation 校验 |

## Out of Scope

webhook 模式、多租户、图片文件上传、流式输出、替换 Telegram 客户端（两者并存）。
