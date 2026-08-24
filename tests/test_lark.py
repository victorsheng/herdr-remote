#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0"]
# ///
"""herdr-remote 飞书客户端：纯函数与事件处理测试。

这些测试刻意不依赖 lark-oapi SDK：herdr_lark 只在真正建立长连接时才导入
SDK，纯逻辑部分保持可独立测试。
"""
import asyncio
import importlib
import json
import os
import string
import pathlib
import re
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))
os.environ.setdefault("HERDR_LARK_APP_ID", "cli_test")
os.environ.setdefault("HERDR_LARK_APP_SECRET", "secret-test")
# 测试绝不能写进真实配置：make_bot() 会构造 BindingStore/SeenStore，
# 用默认路径的话单测的假 chat_id 会污染 ~/.config/herdr-remote/。
_TEST_STATE = tempfile.mkdtemp(prefix="herdr-lark-test-")
os.environ["HERDR_LARK_SEEN_PATH"] = os.path.join(_TEST_STATE, "seen.json")
os.environ["HERDR_LARK_BINDING_PATH"] = os.path.join(_TEST_STATE, "bindings.json")
os.environ["HERDR_LARK_CHATS_PATH"] = os.path.join(_TEST_STATE, "chats.json")

lk = importlib.import_module("herdr_lark")


def make_agents(count, *, status="idle", project="project"):
    return [
        {
            "pane_id": f"w{i}:p1",
            "agent": "opencode",
            "status": status,
            "project": project,
            "cwd": f"/work/{project}/{i}",
            "host": "local",
        }
        for i in range(count)
    ]


class ScrubTests(unittest.TestCase):
    """密钥绝不能出现在日志或发回飞书的文本里。"""

    def test_masks_app_secret(self):
        with unittest.mock.patch.object(lk, "APP_SECRET", "s3cr3t-value"):
            self.assertNotIn("s3cr3t-value", lk.scrub("boom s3cr3t-value boom"))

    def test_masks_relay_token(self):
        with unittest.mock.patch.object(lk, "_RELAY_TOKEN", "tok-abc"):
            out = lk.scrub("ws://127.0.0.1:8375?token=tok-abc failed")
            self.assertNotIn("tok-abc", out)
            self.assertIn("<redacted>", out)

    def test_accepts_non_string(self):
        self.assertIsInstance(lk.scrub(ValueError("nope")), str)

    def test_empty_secret_does_not_mask_everything(self):
        """空密钥不能把每个字符都替换掉。"""
        with unittest.mock.patch.object(lk, "APP_SECRET", ""):
            with unittest.mock.patch.object(lk, "_RELAY_TOKEN", ""):
                self.assertEqual(lk.scrub("plain text"), "plain text")


class PaneTokenTests(unittest.TestCase):
    def test_token_is_stable_and_short(self):
        a = lk.pane_callback_token("w1:p1")
        self.assertEqual(a, lk.pane_callback_token("w1:p1"))
        self.assertEqual(len(a), 16)

    def test_distinct_panes_differ(self):
        self.assertNotEqual(lk.pane_callback_token("w1:p1"), lk.pane_callback_token("w1:p2"))

    def test_resolve_unique_match(self):
        agents = make_agents(3)
        token = lk.pane_callback_token("w1:p1")
        self.assertEqual(lk.resolve_pane_token(token, agents, {}), "w1:p1")

    def test_resolve_unknown_returns_none(self):
        self.assertIsNone(lk.resolve_pane_token("deadbeef", make_agents(2), {}))

    def test_resolve_falls_back_to_pending(self):
        """agent 已消失但仍有待回复消息时，仍要能解析出 pane。"""
        pending = {("chat", "msg"): "w9:p1"}
        token = lk.pane_callback_token("w9:p1")
        self.assertEqual(lk.resolve_pane_token(token, [], pending), "w9:p1")

    def test_duplicate_pane_id_is_not_ambiguous(self):
        """同一 pane 在快照里出现两次不算歧义，仍应解析成功。"""
        agents = [
            {"pane_id": "w1:p1", "status": "idle"},
            {"pane_id": "w1:p1", "status": "working"},
        ]
        token = lk.pane_callback_token("w1:p1")
        self.assertEqual(lk.resolve_pane_token(token, agents, {}), "w1:p1")

    def test_resolve_ambiguous_returns_none(self):
        """不同 pane 撞同一 token 时必须拒绝，不能猜。"""
        agents = [{"pane_id": "w1:p1"}, {"pane_id": "w2:p2"}]
        token = lk.pane_callback_token("w1:p1")
        with unittest.mock.patch.object(lk, "pane_callback_token", lambda _p: token):
            self.assertIsNone(lk.resolve_pane_token(token, agents, {}))


class ActionValueTests(unittest.TestCase):
    def test_roundtrip(self):
        value = lk.action_value("trust", "w1:p1")
        parsed = lk.parse_action_value(value, make_agents(2) + [{"pane_id": "w1:p1"}], {})
        self.assertEqual(parsed["action"], "trust")
        self.assertEqual(parsed["pane_id"], "w1:p1")

    def test_value_is_dict_not_json_string(self):
        """飞书 action.value 原生是对象，不该再套一层 JSON 字符串。"""
        self.assertIsInstance(lk.action_value("read", "w1:p1"), dict)

    def test_extra_fields_preserved(self):
        value = lk.action_value("approval", "w1:p1", g="abcde", k="2")
        self.assertEqual(value["g"], "abcde")
        self.assertEqual(value["k"], "2")

    def test_unknown_code_yields_invalid(self):
        parsed = lk.parse_action_value({"a": "zz", "p": "x"}, [], {})
        self.assertEqual(parsed["action"], "invalid")

    def test_missing_action_key_is_invalid(self):
        self.assertEqual(lk.parse_action_value({}, [], {})["action"], "invalid")


class GenerationTests(unittest.TestCase):
    """借鉴官方 Claude Code Channels：手机上不该把 l 看成 1。"""

    def test_five_lowercase_letters(self):
        for _ in range(50):
            g = lk.new_generation()
            self.assertEqual(len(g), 5)
            self.assertTrue(set(g) <= set(string.ascii_lowercase))

    def test_never_contains_letter_l(self):
        self.assertTrue(all("l" not in lk.new_generation() for _ in range(200)))

    def test_values_vary(self):
        self.assertGreater(len({lk.new_generation() for _ in range(50)}), 1)


class SortingTests(unittest.TestCase):
    def test_blocked_sorts_first(self):
        agents = [
            {"pane_id": "a", "status": "idle", "project": "z", "agent": "x", "host": "local"},
            {"pane_id": "b", "status": "blocked", "project": "z", "agent": "x", "host": "local"},
            {"pane_id": "c", "status": "working", "project": "z", "agent": "x", "host": "local"},
        ]
        self.assertEqual([a["status"] for a in lk.sorted_agents(agents)],
                         ["blocked", "working", "idle"])

    def test_agents_for_action_filters_interrupt(self):
        agents = [
            {"pane_id": "a", "status": "idle"},
            {"pane_id": "b", "status": "working"},
            {"pane_id": "c", "status": "blocked"},
        ]
        got = {a["pane_id"] for a in lk.agents_for_action("interrupt", agents)}
        self.assertEqual(got, {"b", "c"})

    def test_agents_for_action_filters_trust_to_blocked(self):
        agents = [{"pane_id": "a", "status": "working"}, {"pane_id": "b", "status": "blocked"}]
        got = [a["pane_id"] for a in lk.agents_for_action("trust", agents)]
        self.assertEqual(got, ["b"])


class LabelTests(unittest.TestCase):
    def test_labels_are_unique_for_identical_agents(self):
        agents = make_agents(3, project="same")
        labels = lk.agent_button_labels(agents)
        self.assertEqual(len(set(labels)), 3)

    def test_label_includes_status_and_project(self):
        agents = make_agents(1, status="blocked", project="herdr")
        label = lk.agent_button_labels(agents)[0]
        self.assertIn("BLOCKED", label)
        self.assertIn("herdr", label)

    def test_remote_host_surfaced(self):
        agents = make_agents(1)
        agents[0]["host"] = "build-box"
        self.assertIn("build-box", lk.agent_button_labels(agents)[0])


class PendingTests(unittest.TestCase):
    def test_register_and_lookup(self):
        pending = {}
        lk.register_pending(pending, "chat1", "msg1", "w1:p1")
        self.assertEqual(lk.pending_pane(pending, "chat1", "msg1"), "w1:p1")

    def test_unknown_returns_none(self):
        self.assertIsNone(lk.pending_pane({}, "chat1", "nope"))

    def test_evicts_oldest_beyond_limit(self):
        pending = {}
        for i in range(lk.PENDING_LIMIT + 10):
            lk.register_pending(pending, "chat", f"msg{i}", f"w{i}:p1")
        self.assertLessEqual(len(pending), lk.PENDING_LIMIT)
        self.assertIsNone(lk.pending_pane(pending, "chat", "msg0"))
        self.assertIsNotNone(
            lk.pending_pane(pending, "chat", f"msg{lk.PENDING_LIMIT + 9}")
        )


class SeenStoreTests(unittest.TestCase):
    """飞书长连接会重推消息，去重是必需的（Telegram 的 update_id 天然单调，无此问题）。"""

    def _store(self, path=None, limit=None):
        with tempfile.TemporaryDirectory() as d:
            yield lk.SeenStore(path or os.path.join(d, "seen.json"), limit=limit or 5000)

    def test_first_sight_is_new_repeat_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            store = lk.SeenStore(os.path.join(d, "seen.json"))
            self.assertTrue(store.add("om_1"))
            self.assertFalse(store.add("om_1"))

    def test_distinct_ids_all_new(self):
        with tempfile.TemporaryDirectory() as d:
            store = lk.SeenStore(os.path.join(d, "seen.json"))
            self.assertTrue(store.add("om_1"))
            self.assertTrue(store.add("om_2"))

    def test_evicts_oldest_half_over_limit(self):
        with tempfile.TemporaryDirectory() as d:
            store = lk.SeenStore(os.path.join(d, "seen.json"), limit=10)
            for i in range(11):
                store.add(f"om_{i}")
            self.assertLessEqual(len(store), 10)
            # 最旧的被淘汰，会被当成新消息
            self.assertTrue(store.add("om_0"))
            # 最新的仍记得
            self.assertFalse(store.add("om_10"))

    def test_persists_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            lk.SeenStore(path).add("om_keep")
            self.assertFalse(lk.SeenStore(path).add("om_keep"))

    def test_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = lk.SeenStore(os.path.join(d, "nope", "seen.json"))
            self.assertEqual(len(store), 0)
            self.assertTrue(store.add("om_1"))

    def test_corrupt_file_does_not_crash(self):
        """损坏的缓存文件只该降级成空集合，不能让整个 bot 起不来。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            with open(path, "w") as fh:
                fh.write("{not json at all")
            store = lk.SeenStore(path)
            self.assertEqual(len(store), 0)
            self.assertTrue(store.add("om_1"))

    def test_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            lk.SeenStore(path).add("om_1")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_non_list_payload_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "seen.json")
            with open(path, "w") as fh:
                json.dump({"unexpected": "shape"}, fh)
            self.assertEqual(len(lk.SeenStore(path)), 0)


class FakeWS:
    """假的 relay 连接：录下发出的消息，按脚本回放响应。"""

    def __init__(self, responses=None):
        self.sent = []
        self._responses = list(responses or [])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._responses:
            raise AssertionError("relay 收到了超出预期的 recv")
        return json.dumps(self._responses.pop(0))


def fake_connect(ws):
    def _connect(*args, **kwargs):
        return ws
    return _connect


class SendKeysAckTests(unittest.TestCase):
    """Telegram 版踩过的坑：不读 ack 就报成功，键名被 relay 拒绝了用户也不知道。

    relay 的 SAFE_KEYS 只认 "C-c" 这类名字，发 "Ctrl+C" 会被整条拒绝。
    """

    def _run(self, ws):
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            return asyncio.run(lk.send_keys_to_relay("w1:p1", ["C-c"]))

    def test_succeeds_on_ok_ack(self):
        ws = FakeWS([{"type": "command_result", "command": "send_keys", "ok": True}])
        self._run(ws)
        self.assertEqual(ws.sent[0]["type"], "send_keys")
        self.assertEqual(ws.sent[0]["keys"], ["C-c"])

    def test_raises_when_relay_nacks(self):
        ws = FakeWS([
            {"type": "command_result", "command": "send_keys", "ok": False, "message": "bad key"}
        ])
        with self.assertRaises(RuntimeError):
            self._run(ws)

    def test_raises_on_error_message(self):
        ws = FakeWS([{"type": "error", "message": "unauthorized"}])
        with self.assertRaises(RuntimeError):
            self._run(ws)

    def test_skips_unrelated_broadcasts_before_ack(self):
        """relay 可能先广播 agents，ack 在后面几条里。"""
        ws = FakeWS([
            {"type": "agents", "agents": []},
            {"type": "agent_update", "agent": {"pane_id": "w1:p1"}},
            {"type": "command_result", "command": "send_keys", "ok": True},
        ])
        self._run(ws)

    def test_raises_when_ack_never_arrives(self):
        ws = FakeWS([{"type": "agents", "agents": []} for _ in range(5)])
        with self.assertRaises(RuntimeError):
            self._run(ws)


class RelayMessageTests(unittest.TestCase):
    def test_respond_sends_text(self):
        ws = FakeWS()
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            asyncio.run(lk.send_to_relay("w1:p1", "trust, always allow"))
        self.assertEqual(ws.sent, [
            {"type": "respond", "pane_id": "w1:p1", "text": "trust, always allow"}
        ])

    def test_send_text_appends_enter(self):
        """relay 的 send_text 不带回车，要再补一次 Enter 才会提交。

        Enter 必须在 ack 之后发——详见 SendTextAckTests。
        """
        ws = FakeWS([{"type": "command_result", "command": "send_text", "ok": True}])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            asyncio.run(lk.send_text_to_relay("w1:p1", "hello"))
        self.assertEqual([m["type"] for m in ws.sent], ["send_text", "send_keys"])
        self.assertEqual(ws.sent[1]["keys"], ["Enter"])

    def test_send_text_rejects_empty(self):
        with self.assertRaises(ValueError):
            asyncio.run(lk.send_text_to_relay("w1:p1", ""))

    def test_send_text_rejects_overlong(self):
        with self.assertRaises(ValueError):
            asyncio.run(lk.send_text_to_relay("w1:p1", "x" * 1001))

    def test_read_pane_returns_content(self):
        ws = FakeWS([{"type": "pane_content", "content": "line one\nline two"}])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            out = asyncio.run(lk.read_pane("w1:p1"))
        self.assertEqual(out, "line one\nline two")

    def test_read_pane_skips_preceding_broadcasts(self):
        ws = FakeWS([
            {"type": "agents", "agents": []},
            {"type": "pane_content", "content": "actual output"},
        ])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            out = asyncio.run(lk.read_pane("w1:p1"))
        self.assertEqual(out, "actual output")

    def test_read_pane_scrubs_errors(self):
        """读失败的提示会发回飞书，绝不能带 token。"""
        def boom(*a, **k):
            raise RuntimeError("ws://x?token=tok-abc broke")
        with unittest.mock.patch.object(lk, "_RELAY_TOKEN", "tok-abc"):
            with unittest.mock.patch.object(lk, "ws_connect", boom):
                out = asyncio.run(lk.read_pane("w1:p1"))
        self.assertNotIn("tok-abc", out)


def buttons_in(card):
    """把卡片里所有按钮抓出来，便于断言。"""
    out = []
    for element in card.get("elements", []):
        if element.get("tag") == "action":
            out.extend(element.get("actions", []))
    return out


def option_lines_in(card):
    """正文里的选项清单，一项一行（不含编号前缀）。

    选项全文列在正文、按钮只放序号，所以「选项对不对」得看这里，
    不能再看按钮文字。
    """
    out = []
    for element in card.get("elements", []):
        if element.get("tag") != "div":
            continue
        for line in element.get("text", {}).get("content", "").splitlines():
            m = re.match(r"^\*\*(\d+)\.\*\*\s+(.*)$", line)
            if m:
                out.append(m.group(2).strip())
    return out


def option_text_in(card):
    """正文选项清单拼成一串，方便做 assertIn。"""
    return " ".join(option_lines_in(card))


class TruncateTests(unittest.TestCase):
    """借鉴官方 Channels：命令结尾对审批者最要紧，不能被直接截掉。"""

    def test_short_text_untouched(self):
        self.assertEqual(lk.truncate_prompt("rm -rf ./build", 400), "rm -rf ./build")

    def test_keeps_head_and_tail(self):
        text = "START" + ("x" * 500) + "END"
        out = lk.truncate_prompt(text, 100)
        self.assertTrue(out.startswith("START"))
        self.assertTrue(out.endswith("END"))

    def test_marks_elided_amount(self):
        out = lk.truncate_prompt("y" * 500, 100)
        self.assertIn("省略", out)
        self.assertLess(len(out), 500)


class BlockedCardTests(unittest.TestCase):
    def test_encodes_option_index_not_text(self):
        """必须发选项序号当按键。

        直接把选项文本用 respond 发过去是不行的：relay 走 send-text 粘贴，
        Claude 的 TUI 把粘贴内容里的换行当正文而不是回车，提示永远确认不了。
        """
        card = lk.build_blocked_card(
            "w1:p1", "opencode", "herdr", "may I?", lk.TOOL_OPTIONS, "abcde",
        )
        approvals = [b for b in buttons_in(card) if b["value"].get("a") == "k"]
        self.assertEqual([b["value"]["k"] for b in approvals], ["1", "2", "3"])
        for button in approvals:
            self.assertNotIn("text", button["value"])

    def test_carries_generation(self):
        card = lk.build_blocked_card("w1:p1", "a", "p", "q", lk.TOOL_OPTIONS, "abcde")
        for button in buttons_in(card):
            if button["value"].get("a") == "k":
                self.assertEqual(button["value"]["g"], "abcde")

    def test_uses_tool_buttons_for_trust_prompt(self):
        card = lk.build_blocked_card(
            "w1:p1", "a", "p", "q", ["yes, single permission", "trust, always allow"], "abcde",
        )
        self.assertIn("Trust", option_text_in(card))

    def test_uses_subagent_buttons_for_approve_all(self):
        card = lk.build_blocked_card(
            "w1:p1", "a", "p", "q", ["approve all pending", "configure"], "abcde",
        )
        self.assertIn("Approve all", option_text_in(card))

    def test_defaults_to_tool_options_when_none(self):
        card = lk.build_blocked_card("w1:p1", "a", "p", "q", None, "abcde")
        approvals = [b for b in buttons_in(card) if b["value"].get("a") == "k"]
        self.assertEqual(len(approvals), len(lk.TOOL_BUTTONS))

    def test_includes_open_output_button(self):
        card = lk.build_blocked_card("w1:p1", "a", "p", "q", None, "abcde")
        actions = {b["value"]["a"] for b in buttons_in(card)}
        self.assertIn(lk.ACTION_CODES["select_reply"], actions)

    def test_prompt_is_truncated(self):
        card = lk.build_blocked_card("w1:p1", "a", "p", "z" * 2000, None, "abcde")
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertLess(len(rendered), 2000)


class AgentPickerCardTests(unittest.TestCase):
    def test_one_button_per_agent(self):
        card = lk.build_agent_picker_card("read", make_agents(3))
        self.assertEqual(len(buttons_in(card)), 3)

    def test_paginates_at_page_size(self):
        card = lk.build_agent_picker_card("read", make_agents(25))
        picks = [b for b in buttons_in(card) if b["value"].get("a") != lk.ACTION_CODES["page"]]
        self.assertEqual(len(picks), lk.AGENT_PAGE_SIZE)

    def test_second_page_holds_remainder(self):
        card = lk.build_agent_picker_card("read", make_agents(25), page=1)
        picks = [b for b in buttons_in(card) if b["value"].get("a") != lk.ACTION_CODES["page"]]
        self.assertEqual(len(picks), 5)

    def test_no_nav_on_single_page(self):
        card = lk.build_agent_picker_card("read", make_agents(3))
        nav = [b for b in buttons_in(card) if b["value"].get("a") == lk.ACTION_CODES["page"]]
        self.assertEqual(nav, [])

    def test_clamps_out_of_range_page(self):
        card = lk.build_agent_picker_card("read", make_agents(3), page=99)
        self.assertEqual(len(buttons_in(card)), 3)

    def test_blocked_agent_listed_first(self):
        agents = make_agents(2) + make_agents(1, status="blocked", project="urgent")
        card = lk.build_agent_picker_card("read", agents)
        self.assertIn("BLOCKED", buttons_in(card)[0]["text"]["content"])


class MessageGateTests(unittest.TestCase):
    """决定一条飞书消息该不该处理。"""

    def ctx(self, **kw):
        base = dict(chat_id="oc_1", message_id="om_1", sender_open_id="ou_user",
                    chat_type="p2p", mentioned_bot=False, text="hi")
        base.update(kw)
        return lk.MessageContext(**base)

    def test_p2p_message_accepted(self):
        self.assertTrue(lk.should_handle(self.ctx(), bot_open_id="ou_bot", chat_id="oc_1"))

    def test_group_without_mention_ignored(self):
        ctx = self.ctx(chat_type="group", mentioned_bot=False)
        self.assertFalse(lk.should_handle(ctx, bot_open_id="ou_bot", chat_id="oc_1"))

    def test_group_with_mention_accepted(self):
        ctx = self.ctx(chat_type="group", mentioned_bot=True)
        self.assertTrue(lk.should_handle(ctx, bot_open_id="ou_bot", chat_id="oc_1"))

    def test_solo_group_treated_as_p2p(self):
        """只有 bot 和自己的群不该逼人每次都 @。"""
        ctx = self.ctx(chat_type="group", mentioned_bot=False, solo_group=True)
        self.assertTrue(lk.should_handle(ctx, bot_open_id="ou_bot", chat_id="oc_1"))

    def test_bot_own_message_ignored(self):
        """不忽略就会自己跟自己聊到天荒地老。"""
        ctx = self.ctx(sender_open_id="ou_bot")
        self.assertFalse(lk.should_handle(ctx, bot_open_id="ou_bot", chat_id="oc_1"))

    def test_unauthorized_chat_ignored(self):
        self.assertFalse(lk.should_handle(self.ctx(), bot_open_id="ou_bot", chat_id="oc_other"))

    def test_discovery_mode_allows_any_chat(self):
        """未配置 CHAT_ID 时放行，方便第一次拿 chat_id。"""
        self.assertTrue(lk.should_handle(self.ctx(), bot_open_id="ou_bot", chat_id=""))


class StripMentionTests(unittest.TestCase):
    def test_removes_at_prefix(self):
        out = lk.strip_bot_mention("@_user_1 /agents", [{"key": "@_user_1", "name": "demo"}])
        self.assertEqual(out, "/agents")

    def test_removes_by_name(self):
        out = lk.strip_bot_mention("@demo /read", [{"key": "@_user_1", "name": "demo"}])
        self.assertEqual(out, "/read")

    def test_no_mentions_untouched(self):
        self.assertEqual(lk.strip_bot_mention("/status", []), "/status")


class ParseMessageTests(unittest.TestCase):
    def test_extracts_text(self):
        event = {
            "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "p2p",
                        "message_type": "text", "content": json.dumps({"text": "hello"})},
            "sender": {"sender_id": {"open_id": "ou_user"}},
        }
        ctx = lk.parse_message_event(event, bot_open_id="ou_bot")
        self.assertEqual(ctx.text, "hello")
        self.assertEqual(ctx.chat_id, "oc_1")

    def test_detects_bot_mention(self):
        event = {
            "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "group",
                        "message_type": "text", "content": json.dumps({"text": "@demo hi"}),
                        "mentions": [{"key": "@_user_1", "name": "demo",
                                      "id": {"open_id": "ou_bot"}}]},
            "sender": {"sender_id": {"open_id": "ou_user"}},
        }
        ctx = lk.parse_message_event(event, bot_open_id="ou_bot")
        self.assertTrue(ctx.mentioned_bot)
        self.assertEqual(ctx.text, "hi")

    def test_malformed_content_does_not_crash(self):
        event = {
            "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "p2p",
                        "message_type": "text", "content": "not json"},
            "sender": {"sender_id": {"open_id": "ou_user"}},
        }
        self.assertEqual(lk.parse_message_event(event, bot_open_id="ou_bot").text, "")


class CardActionNormalizationTests(unittest.TestCase):
    """点按钮和打字要走同一条下游路径，不维护两份分支。"""

    def test_card_action_becomes_message_context(self):
        event = {
            "open_message_id": "om_1",
            "open_chat_id": "oc_1",
            "operator": {"open_id": "ou_user"},
            "action": {"value": {"a": "r", "p": "abc"}},
        }
        ctx = lk.parse_card_action(event)
        self.assertIsInstance(ctx, lk.MessageContext)
        self.assertEqual(ctx.chat_id, "oc_1")
        self.assertEqual(ctx.sender_open_id, "ou_user")
        self.assertEqual(ctx.action, {"a": "r", "p": "abc"})

    def test_card_action_counts_as_explicit_intent(self):
        """点击本身就是明确意图，不该再要求 @。"""
        event = {"open_message_id": "om_1", "open_chat_id": "oc_1",
                 "operator": {"open_id": "ou_user"}, "action": {"value": {"a": "r"}}}
        self.assertTrue(lk.parse_card_action(event).mentioned_bot)

    def test_real_sdk_shape_puts_ids_under_context(self):
        """lark-oapi 的 P2CardActionTriggerData 只在 context 下放这两个 id。

        顶层没有 open_chat_id / open_message_id，实测自 SDK 源码。
        """
        event = {
            "operator": {"open_id": "ou_user"},
            "context": {"open_chat_id": "oc_real", "open_message_id": "om_real"},
            "action": {"value": {"a": "r", "p": "abc"}},
        }
        ctx = lk.parse_card_action(event)
        self.assertEqual(ctx.chat_id, "oc_real")
        self.assertEqual(ctx.message_id, "om_real")

    def test_legacy_top_level_ids_still_work(self):
        event = {"open_message_id": "om_1", "open_chat_id": "oc_1",
                 "operator": {"open_id": "ou_user"}, "action": {"value": {"a": "r"}}}
        self.assertEqual(lk.parse_card_action(event).chat_id, "oc_1")

    def test_missing_value_yields_none(self):
        event = {"open_message_id": "om_1", "open_chat_id": "oc_1",
                 "operator": {"open_id": "ou_user"}, "action": {}}
        self.assertIsNone(lk.parse_card_action(event))


class ApprovalGenerationTests(unittest.TestCase):
    """陈旧按钮不能生效——三小时前那条通知不该还能批准。"""

    def test_matching_generation_accepted(self):
        tokens = {"w1:p1": "abcde"}
        self.assertTrue(lk.approval_is_current(tokens, "w1:p1", "abcde"))

    def test_stale_generation_rejected(self):
        tokens = {"w1:p1": "fghij"}
        self.assertFalse(lk.approval_is_current(tokens, "w1:p1", "abcde"))

    def test_missing_generation_rejected(self):
        self.assertFalse(lk.approval_is_current({}, "w1:p1", "abcde"))

    def test_none_generation_rejected(self):
        self.assertFalse(lk.approval_is_current({"w1:p1": "abcde"}, "w1:p1", None))

    def test_cleared_when_pane_leaves_blocked(self):
        tokens = {"w1:p1": "abcde", "w2:p1": "fghij"}
        lk.prune_approval_tokens(tokens, [
            {"pane_id": "w1:p1", "status": "blocked"},
            {"pane_id": "w2:p1", "status": "working"},
        ])
        self.assertIn("w1:p1", tokens)
        self.assertNotIn("w2:p1", tokens)


class CommandParseTests(unittest.TestCase):
    def test_splits_command_and_args(self):
        self.assertEqual(lk.parse_command("/read herdr"), ("read", "herdr"))

    def test_bare_command(self):
        self.assertEqual(lk.parse_command("/agents"), ("agents", ""))

    def test_strips_at_suffix(self):
        """飞书群里命令可能带 @机器人 后缀。"""
        self.assertEqual(lk.parse_command("/status@demo"), ("status", ""))

    def test_non_command_is_free_text(self):
        self.assertEqual(lk.parse_command("just some text"), (None, "just some text"))

    def test_case_insensitive(self):
        self.assertEqual(lk.parse_command("/READ x")[0], "read")

    def test_multiword_args_preserved(self):
        self.assertEqual(lk.parse_command("/send proj hello world"), ("send", "proj hello world"))


class MatchAgentTests(unittest.TestCase):
    def test_matches_by_project(self):
        agents = make_agents(1, project="herdr-remote")
        self.assertIsNotNone(lk.match_agent(agents, "herdr"))

    def test_matches_by_agent_name(self):
        self.assertIsNotNone(lk.match_agent(make_agents(1), "opencode"))

    def test_no_match_returns_none(self):
        self.assertIsNone(lk.match_agent(make_agents(1), "nonexistent"))

    def test_case_insensitive(self):
        agents = make_agents(1, project="HerdR")
        self.assertIsNotNone(lk.match_agent(agents, "herdr"))


class StatusSummaryTests(unittest.TestCase):
    def test_counts_by_status(self):
        agents = (make_agents(2, status="blocked") + make_agents(1, status="working")
                  + make_agents(3, status="idle"))
        out = lk.status_summary(agents)
        self.assertIn("2 blocked", out)
        self.assertIn("1 working", out)

    def test_empty_is_safe(self):
        self.assertIsInstance(lk.status_summary([]), str)


class AgentListTests(unittest.TestCase):
    def test_status_shown_per_row(self):
        """状态用行首图标表示（不再分组，那样会让序号乱序）。"""
        agents = make_agents(1, status="blocked", project="urgent") + make_agents(1, project="calm")
        out = lk.format_agent_list(agents)
        self.assertIn("urgent", out)
        self.assertIn("⏸", out)

    def test_marks_remote_host(self):
        agents = make_agents(1)
        agents[0]["host"] = "build-box"
        self.assertIn("build-box", lk.format_agent_list(agents))

    def test_empty_message(self):
        self.assertIn("No agents", lk.format_agent_list([]))


class DigestTests(unittest.TestCase):
    def test_reports_working_minutes(self):
        stats = {"w1:p1": {"agent": "opencode", "project": "herdr",
                           "blocked_count": 2, "working_mins": 95}}
        out = lk.format_digest(stats)
        self.assertIn("herdr", out)
        self.assertIn("1h35m", out)

    def test_short_duration_in_minutes(self):
        stats = {"w1:p1": {"agent": "a", "project": "p", "blocked_count": 0, "working_mins": 7}}
        self.assertIn("7m", lk.format_digest(stats))

    def test_empty_digest(self):
        self.assertIn("No activity", lk.format_digest({}))


class IndexedAgentTests(unittest.TestCase):
    """15 个 agent 时打项目名太烦，用 /agents 里的序号直接选。"""

    def test_list_is_numbered(self):
        out = lk.format_agent_list(make_agents(3))
        self.assertIn("1.", out)
        self.assertIn("3.", out)

    def test_numbering_follows_sorted_order(self):
        """序号必须和 sorted_agents 一致，否则 /read 2 会选错人。"""
        agents = make_agents(2) + make_agents(1, status="blocked", project="urgent")
        ordered = lk.sorted_agents(agents)
        self.assertEqual(lk.match_agent(agents, "1")["pane_id"], ordered[0]["pane_id"])

    def test_index_selects_agent(self):
        agents = make_agents(3)
        ordered = lk.sorted_agents(agents)
        self.assertEqual(lk.match_agent(agents, "2")["pane_id"], ordered[1]["pane_id"])

    def test_index_is_one_based(self):
        agents = make_agents(3)
        self.assertEqual(lk.match_agent(agents, "1")["pane_id"],
                         lk.sorted_agents(agents)[0]["pane_id"])

    def test_out_of_range_index_returns_none(self):
        self.assertIsNone(lk.match_agent(make_agents(3), "9"))

    def test_zero_index_returns_none(self):
        self.assertIsNone(lk.match_agent(make_agents(3), "0"))

    def test_name_match_still_works(self):
        """加了序号不能把按名字找的能力弄丢。"""
        self.assertIsNotNone(lk.match_agent(make_agents(1, project="herdr"), "herdr"))

    def test_numeric_project_name_prefers_index(self):
        """项目名恰好是数字时，序号优先——用户看到的就是序号。"""
        agents = make_agents(2, project="2024")
        picked = lk.match_agent(agents, "1")
        self.assertEqual(picked["pane_id"], lk.sorted_agents(agents)[0]["pane_id"])


class SendWithIndexTests(unittest.TestCase):
    def test_send_splits_index_from_payload(self):
        """/send 2 hello world → 选 2 号，发 'hello world'。"""
        cmd, rest = lk.parse_command("/send 2 hello world")
        self.assertEqual(cmd, "send")
        query, _, payload = rest.partition(" ")
        self.assertEqual(query, "2")
        self.assertEqual(payload, "hello world")

    def test_read_index_has_no_payload(self):
        cmd, rest = lk.parse_command("/read 3")
        self.assertEqual((cmd, rest), ("read", "3"))


REAL_PANE = """抓取实际 pane 输出
  ⎿  $ cd /Users/victor/code-github/herdr-remote && set -a
     export HERDR_RELAY="ws://127.0.0.1:8375"

✻ Drizzling… (12s · ↓ 304 tokens)
                                                          ✔ Update installed · Restart to update
─────────────────────────────────────────────────────────────────
❯
─────────────────────────────────────────────────────────────────
  [Opus 5 (1M context)] │ tailcale
  Context ███░░░░░░░ 34% │ Usage ░░░░░░░░░░ 3% (resets in 4h 17m)
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent"""


class PaneCleanupTests(unittest.TestCase):
    """手机屏幕小，TUI 状态栏会把真正的输出挤没。"""

    def test_drops_model_and_context_statusline(self):
        out = lk.clean_pane(REAL_PANE)
        self.assertNotIn("Context ", out)
        self.assertNotIn("Opus 5 (1M context)", out)

    def test_drops_permission_mode_line(self):
        self.assertNotIn("bypass permissions", lk.clean_pane(REAL_PANE))

    def test_drops_update_banner(self):
        self.assertNotIn("Update installed", lk.clean_pane(REAL_PANE))

    def test_drops_separator_rules(self):
        self.assertNotIn("─────", lk.clean_pane(REAL_PANE))

    def test_drops_empty_prompt_line(self):
        self.assertNotIn("\n❯", "\n" + lk.clean_pane(REAL_PANE))

    def test_keeps_actual_output(self):
        """真正的内容一个字都不能丢。"""
        out = lk.clean_pane(REAL_PANE)
        self.assertIn("抓取实际 pane 输出", out)
        self.assertIn("HERDR_RELAY", out)

    def test_keeps_spinner_progress(self):
        """还在跑的状态是有用信息，保留。"""
        self.assertIn("Drizzling", lk.clean_pane(REAL_PANE))

    def test_substantially_shorter(self):
        self.assertLess(len(lk.clean_pane(REAL_PANE)), len(REAL_PANE) * 0.75)

    def test_prompt_with_text_is_kept(self):
        """带内容的输入行是用户输入，不能当装饰丢掉。"""
        self.assertIn("/read 1", lk.clean_pane("❯ /read 1"))

    def test_collapses_blank_runs(self):
        self.assertNotIn("\n\n\n", lk.clean_pane("a\n\n\n\n\nb"))

    def test_empty_input_safe(self):
        self.assertEqual(lk.clean_pane(""), "")


class ReadDepthTests(unittest.TestCase):
    """手机上看进展，15 行只能看到尾巴。"""

    def test_requests_generous_line_count(self):
        ws = FakeWS([{"type": "pane_content", "content": "x"}])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            asyncio.run(lk.read_pane("w1:p1"))
        self.assertGreaterEqual(ws.sent[0]["lines"], 120)

    def test_explicit_lines_respected(self):
        ws = FakeWS([{"type": "pane_content", "content": "x"}])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            asyncio.run(lk.read_pane("w1:p1", lines=400))
        self.assertEqual(ws.sent[0]["lines"], 400)


class ActivePaneTests(unittest.TestCase):
    """读完要能直接接着说话——这是闭环的关键。"""

    def test_active_pane_set_on_read(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        self.assertEqual(bot.active_pane("oc_1"), "w1:p1")

    def test_active_pane_is_per_chat(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w2:p1")
        self.assertEqual(bot.active_pane("oc_1"), "w1:p1")
        self.assertEqual(bot.active_pane("oc_2"), "w2:p1")

    def test_latest_read_wins(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_1", "w2:p1")
        self.assertEqual(bot.active_pane("oc_1"), "w2:p1")

    def test_unknown_chat_has_no_active(self):
        self.assertIsNone(make_bot().active_pane("oc_none"))


class FollowUpFormatTests(unittest.TestCase):
    def test_footer_names_active_agent(self):
        """得让人知道现在说话是发给谁。"""
        out = lk.follow_up_hint("tailcale")
        self.assertIn("tailcale", out)


def make_bot(chat_id="oc_1"):
    """干净的 bot。

    不清空的话，上一个测试写进共享临时文件的绑定会漏到下一个测试里
    ——实际发生过：chats_watching 多返回了几个群。
    """
    api = unittest.mock.MagicMock()
    api.bot_open_id = "ou_bot"
    bot = lk.LarkBot(api, chat_id, loop=None)
    bot._active = {}
    bot._staged = {}
    bot.chat_ids = lk.parse_chat_ids(chat_id)
    bot.audit_on = True
    return bot


TOOL_PROMPT = """⏺ Bash(rm -rf ./build)
  ⎿  Do you want to proceed?
     1. yes, single permission
     2. trust, always allow
     3. no (tab to edit)"""

ASK_PROMPT = """要用哪种方案实现？
  1. 直接改现有函数
  2. 新增一层抽象
  3. 先写测试再说"""

ARROW_PROMPT = """Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again
   3. No, and tell Claude what to do differently"""


class OptionDetectionTests(unittest.TestCase):
    """读到一个正卡着的 agent 时，得把选项变成能点的按钮。"""

    def test_detects_tool_permission_options(self):
        opts = lk.detect_pane_options(TOOL_PROMPT)
        self.assertEqual(len(opts), 3)
        self.assertIn("yes", opts[0].lower())

    def test_detects_ask_question_options(self):
        """AskUserQuestion 的自定义选项，relay 的 detect_options 认不出来。"""
        opts = lk.detect_pane_options(ASK_PROMPT)
        self.assertEqual(len(opts), 3)
        self.assertIn("新增一层抽象", opts[1])

    def test_detects_arrow_marked_options(self):
        opts = lk.detect_pane_options(ARROW_PROMPT)
        self.assertEqual(len(opts), 3)
        self.assertIn("Yes", opts[0])

    def test_plain_output_has_no_options(self):
        self.assertIsNone(lk.detect_pane_options("just some log output\nsecond line"))

    def test_numbered_list_in_prose_not_treated_as_options(self):
        """散文里的编号列表不算选择器——不能一看到数字就弹按钮。"""
        text = "改动如下：\n1. 修了 a\n2. 修了 b\n然后跑了测试，全绿。"
        self.assertIsNone(lk.detect_pane_options(text))

    def test_requires_at_least_two_options(self):
        self.assertIsNone(lk.detect_pane_options("Proceed?\n1. yes"))

    def test_options_must_be_near_end(self):
        """选项要在末尾附近；历史记录里的旧选项不该被当成待办。"""
        text = "1. old\n2. old2\n" + "\n".join(f"log line {i}" for i in range(40))
        self.assertIsNone(lk.detect_pane_options(text))

    def test_caps_option_count(self):
        many = "选一个：\n" + "\n".join(f"{i}. opt{i}" for i in range(1, 15))
        opts = lk.detect_pane_options(many)
        self.assertLessEqual(len(opts), 9)

    def test_empty_safe(self):
        self.assertIsNone(lk.detect_pane_options(""))


class DigitInterceptTests(unittest.TestCase):
    """卡在选择器上时，直接打「2」应当按键，而不是把文本发过去。"""

    def test_bare_digit_is_option_press(self):
        self.assertTrue(lk.looks_like_option_press("2"))

    def test_padded_digit_ok(self):
        self.assertTrue(lk.looks_like_option_press("  3 "))

    def test_multi_digit_rejected(self):
        self.assertFalse(lk.looks_like_option_press("12"))

    def test_zero_rejected(self):
        self.assertFalse(lk.looks_like_option_press("0"))

    def test_text_rejected(self):
        self.assertFalse(lk.looks_like_option_press("2 files changed"))


class SendTextAckTests(unittest.TestCase):
    """Enter 必须等 send_text 的 ack 之后再发。

    relay 在 paste 之后会 settle（Cursor 尤其需要），settle 完才回 ack。
    不等 ack 就发 Enter，回车可能赶在粘贴稳定之前到达，表现为
    「消息进去了但没有回车」。
    """

    def test_waits_for_ack_before_enter(self):
        ws = FakeWS([{"type": "command_result", "command": "send_text", "ok": True}])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            asyncio.run(lk.send_text_to_relay("w1:p1", "hello"))
        self.assertEqual([m["type"] for m in ws.sent], ["send_text", "send_keys"])
        self.assertEqual(ws.sent[1]["keys"], ["Enter"])

    def test_raises_when_send_text_nacked(self):
        ws = FakeWS([
            {"type": "command_result", "command": "send_text", "ok": False, "message": "nope"}
        ])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            with self.assertRaises(RuntimeError):
                asyncio.run(lk.send_text_to_relay("w1:p1", "hello"))

    def test_no_enter_sent_when_paste_failed(self):
        """粘贴失败还回车，会把上一条残留内容提交出去。"""
        ws = FakeWS([{"type": "error", "message": "send_text command failed"}])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            with self.assertRaises(RuntimeError):
                asyncio.run(lk.send_text_to_relay("w1:p1", "hello"))
        self.assertNotIn("send_keys", [m["type"] for m in ws.sent])

    def test_skips_unrelated_broadcasts_before_ack(self):
        ws = FakeWS([
            {"type": "agents", "agents": []},
            {"type": "command_result", "command": "send_text", "ok": True},
        ])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            asyncio.run(lk.send_text_to_relay("w1:p1", "hello"))
        self.assertEqual(ws.sent[-1]["keys"], ["Enter"])

    def test_raises_when_ack_never_arrives(self):
        ws = FakeWS([{"type": "agents", "agents": []} for _ in range(5)])
        with unittest.mock.patch.object(lk, "ws_connect", fake_connect(ws)):
            with self.assertRaises(RuntimeError):
                asyncio.run(lk.send_text_to_relay("w1:p1", "hello"))


class FinishTransitionTests(unittest.TestCase):
    """agent 停下来时要主动推，而且要带上它干了什么。"""

    def test_working_to_idle_is_finish(self):
        self.assertTrue(lk.is_finish_transition("working", "idle"))

    def test_working_to_done_is_finish(self):
        self.assertTrue(lk.is_finish_transition("working", "done"))

    def test_blocked_to_idle_is_finish(self):
        self.assertTrue(lk.is_finish_transition("blocked", "idle"))

    def test_idle_to_idle_is_not(self):
        self.assertFalse(lk.is_finish_transition("idle", "idle"))

    def test_first_sight_is_not_finish(self):
        """首次见到就是 idle 的 agent 不该触发通知，否则一启动就刷屏。"""
        self.assertFalse(lk.is_finish_transition(None, "idle"))

    def test_working_to_blocked_is_not_finish(self):
        """转 blocked 有专门的审批卡片，不走完成通知。"""
        self.assertFalse(lk.is_finish_transition("working", "blocked"))

    def test_idle_to_working_is_not(self):
        self.assertFalse(lk.is_finish_transition("idle", "working"))


class FinishNotificationTests(unittest.TestCase):
    def test_message_includes_output(self):
        """只说「finished」等于没说——得看到它干了什么。"""
        out = lk.format_finish_message("tailcale", "claude", "跑完了 152 个测试\n全绿")
        self.assertIn("tailcale", out)
        self.assertIn("152 个测试", out)

    def test_handles_empty_output(self):
        out = lk.format_finish_message("tailcale", "claude", "")
        self.assertIn("tailcale", out)

    def test_truncates_long_output(self):
        out = lk.format_finish_message("p", "a", "x" * 5000)
        self.assertLess(len(out), 3200)

    def test_keeps_tail_when_truncating(self):
        """结论在末尾，截断要留尾巴。"""
        out = lk.format_finish_message("p", "a", "x" * 5000 + "CONCLUSION")
        self.assertIn("CONCLUSION", out)


class ChatBindingTests(unittest.TestCase):
    """一个群绑一个 agent：15 个 agent 挤一个群会分不清谁是谁，
    完成推送也会互相刷屏。"""

    def test_bind_and_read_back(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        self.assertEqual(bot.active_pane("oc_1"), "w1:p1")

    def test_binding_is_per_chat(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w2:p1")
        self.assertEqual(bot.active_pane("oc_1"), "w1:p1")

    def test_chat_title_names_the_agent(self):
        self.assertIn("tailcale", lk.chat_title_for("tailcale"))

    def test_chat_title_leads_with_status_glyph(self):
        """项目群靠状态符号识别，不再靠统一前缀。"""
        self.assertTrue(
            lk.chat_title_for("x", status="working").startswith("🟡 "))

    def test_chat_title_truncates_long_project(self):
        self.assertLessEqual(len(lk.chat_title_for("p" * 200)), 60)

    def test_unbound_chat_returns_none(self):
        self.assertIsNone(make_bot().active_pane("oc_nope"))


class BroadcastScopeTests(unittest.TestCase):
    """完成推送只发给绑定了这个 agent 的群。"""

    def test_finds_chats_bound_to_pane(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w2:p1")
        self.assertEqual(lk.chats_watching(bot, "w1:p1"), ["oc_1"])

    def test_multiple_chats_can_watch_same_pane(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w1:p1")
        self.assertEqual(sorted(lk.chats_watching(bot, "w1:p1")), ["oc_1", "oc_2"])

    def test_unbound_pane_gets_nothing(self):
        """没有任何群绑它就不发。宁可丢通知，也不能串到别人的群里。"""
        bot = make_bot()
        self.assertEqual(lk.chats_watching(bot, "w9:p1"), [])

    def test_no_default_and_no_binding_yields_nothing(self):
        api = unittest.mock.MagicMock()
        api.bot_open_id = "ou_bot"
        bot = lk.LarkBot(api, "", loop=None)
        # 用干净的存储：其它测试可能已经往共享文件里写过群。
        bot.chat_ids = set()
        bot._active = {}
        self.assertEqual(lk.chats_watching(bot, "w9:p1"), [])


class ChatInventoryTests(unittest.TestCase):
    """列群不能因为群名重复就把群丢掉。

    线上事故：绑定 bug 把 16 个群逐个改成同一个名字，而 list_chats 用群名当
    字典 key，16 个群塌缩成 1 条。据此判断「bot 已不在这些群」，差点漏删，
    也会让 /spaces 误以为已有群可复用。
    """

    def test_same_name_chats_are_all_counted(self):
        rows = [("herdr · [w2B] x", f"oc_{i}") for i in range(5)]
        self.assertEqual(len(lk.chat_inventory(rows)), 5)

    def test_inventory_keeps_every_chat_id(self):
        rows = [("同名", "oc_a"), ("同名", "oc_b")]
        self.assertEqual({c["chat_id"] for c in lk.chat_inventory(rows)},
                         {"oc_a", "oc_b"})

    def test_unnamed_chat_not_dropped(self):
        """没名字的群也得列出来，否则删不掉也看不见。"""
        rows = [("", "oc_x")]
        self.assertEqual(len(lk.chat_inventory(rows)), 1)

    def test_duplicate_names_are_reported(self):
        """同名群要能被识别出来——那本身就是 bug 的信号。"""
        rows = [("同名", "oc_a"), ("同名", "oc_b"), ("独一份", "oc_c")]
        dupes = lk.duplicate_named_chats(lk.chat_inventory(rows))
        self.assertEqual(sorted(dupes), ["oc_a", "oc_b"])

    def test_no_duplicates_yields_empty(self):
        rows = [("a", "oc_a"), ("b", "oc_b")]
        self.assertEqual(lk.duplicate_named_chats(lk.chat_inventory(rows)), [])

    def test_name_index_still_available_for_reuse(self):
        """/spaces 靠群名找现成群，这个映射要保留；同名时取一个即可。"""
        rows = [("同名", "oc_a"), ("同名", "oc_b")]
        index = lk.chat_name_index(lk.chat_inventory(rows))
        self.assertIn(index["同名"], ("oc_a", "oc_b"))


class NoFallbackTests(unittest.TestCase):
    """没有群绑这个 pane 就不发。绝不回落到任何群。

    去掉主群回落的原因：主群不是中立收件箱，它自己也会被某个 pane 绑走。
    实际发生过——datapilot6（w1R:p1）一个群都没绑，它的通知回落到主群，
    而主群已经绑给了 herdr-remote（w2B:p1），于是 datapilot6 的进展出现在
    「herdr · herdr-remote」群里，看的人会以为那是 herdr-remote 的输出。

    宁可不发：通知只到你明确指定过的地方。没绑的 agent 靠 /agents 主动查。
    """

    def test_unbound_pane_gets_nothing(self):
        bot = make_bot("oc_main,oc_b,oc_c")
        self.assertEqual(lk.chats_watching(bot, "w9:p1"), [])

    def test_unbound_pane_ignores_configured_order(self):
        """配置里的第一个群不再有特殊地位。"""
        bot = make_bot("oc_zzz,oc_aaa")
        self.assertEqual(lk.chats_watching(bot, "w9:p1"), [])

    def test_bound_pane_unaffected(self):
        """显式绑定优先，且绑几个就发几个——主路径不能受影响。"""
        bot = make_bot("oc_main,oc_b,oc_c")
        bot.set_active("oc_b", "w1:p1")
        bot.set_active("oc_c", "w1:p1")
        self.assertEqual(lk.chats_watching(bot, "w1:p1"), ["oc_b", "oc_c"])

    def test_other_panes_binding_does_not_leak(self):
        """回归防线：别的 pane 绑了群，不代表这个 pane 能用那个群。

        这正是串群的形状——oc_main 绑着 w1:p1，w9:p1 不该借它发消息。
        """
        bot = make_bot("oc_main,oc_b")
        bot.set_active("oc_main", "w1:p1")
        self.assertEqual(lk.chats_watching(bot, "w9:p1"), [])

    def test_no_chats_at_all_yields_nothing(self):
        api = unittest.mock.MagicMock()
        api.bot_open_id = "ou_bot"
        bot = lk.LarkBot(api, "", loop=None)
        bot.chat_ids = set()
        bot._active = {}
        self.assertEqual(lk.chats_watching(bot, "w9:p1"), [])


class BroadcastDoesNotBindTests(unittest.IsolatedAsyncioTestCase):
    """回落广播不能把群永久绑到 pane 上。

    线上事故：某个 pane 完成时还没有群绑它，chats_watching 回落成「发给全部
    授权群」，而 _notify_finished 在循环里对每个群都调了 set_active。一次
    广播就把 16 个群全部持久化绑到同一个 pane，此后它每次完成都在 16 个群
    各刷一遍，重启也不会好——绑定已经落盘。
    """

    async def _finish(self, bot, pane_id="w9:p1"):
        """跑一次完成推送，read_pane 打桩成没有选择器的普通输出。"""
        with unittest.mock.patch.object(
                lk, "read_pane", new=unittest.mock.AsyncMock(return_value="done\n")):
            await lk._notify_finished(bot, {
                "pane_id": pane_id, "agent": "claude", "project": "proj"})

    async def test_broadcast_leaves_no_bindings(self):
        """没绑定时广播出去，事后仍然没有绑定。"""
        bot = make_bot("oc_1,oc_2,oc_3")
        await self._finish(bot)
        self.assertEqual(bot._active, {})

    async def test_broadcast_is_not_sticky(self):
        """广播两次，第二次的收件群不该比第一次多——也不该被固化。"""
        bot = make_bot("oc_1,oc_2,oc_3")
        first = lk.chats_watching(bot, "w9:p1")
        await self._finish(bot)
        second = lk.chats_watching(bot, "w9:p1")
        self.assertEqual(first, second)

    async def test_explicit_binding_still_kept(self):
        """用户主动绑过的群，完成推送不能把它解绑。"""
        bot = make_bot("oc_1,oc_2,oc_3")
        bot.set_active("oc_2", "w9:p1")
        await self._finish(bot)
        self.assertEqual(bot._active.get("oc_2"), "w9:p1")

    async def test_bound_pane_does_not_broadcast(self):
        """已经有群绑它时，只发那个群,不碰其它群的绑定。"""
        bot = make_bot("oc_1,oc_2,oc_3")
        bot.set_active("oc_2", "w9:p1")
        await self._finish(bot)
        self.assertNotIn("oc_1", bot._active)
        self.assertNotIn("oc_3", bot._active)


PANE_SAMPLE = """⏺ 实测清理效果与耗时
  ⎿  $ pkill -f herdr_lark.py
     export HERDR_RELAY="ws://127.0.0.1:8375"

✻ Drizzling… (1m 13s · ↓ 4.1k tokens)"""


class PaneCardTests(unittest.TestCase):
    """头部状态上色，主体走代码块保持等宽对齐。"""

    def test_returns_card_dict(self):
        card = lk.build_pane_card("tailcale", "claude", "working", PANE_SAMPLE)
        self.assertIn("elements", card)

    def test_body_is_code_block(self):
        """代码块保住缩进和对齐——终端输出全靠这个。"""
        card = lk.build_pane_card("p", "a", "idle", PANE_SAMPLE)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("```", blob)

    def test_status_line_is_colored(self):
        card = lk.build_pane_card("p", "a", "working", PANE_SAMPLE)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("<font color=", blob)

    def test_working_and_idle_differ(self):
        """在跑和停了要一眼看出区别。"""
        a = json.dumps(lk.build_pane_card("p", "x", "working", "out"), ensure_ascii=False)
        b = json.dumps(lk.build_pane_card("p", "x", "idle", "out"), ensure_ascii=False)
        self.assertNotEqual(a, b)

    def test_blocked_uses_alarming_header(self):
        card = lk.build_pane_card("p", "a", "blocked", "out")
        self.assertIn(card["header"]["template"], ("orange", "red"))

    def test_project_in_header(self):
        card = lk.build_pane_card("tailcale", "claude", "idle", "out")
        self.assertIn("tailcale", card["header"]["title"]["content"])

    def test_backticks_in_output_escaped(self):
        """输出里本来就有 ``` 时不能把代码块提前闭合。"""
        card = lk.build_pane_card("p", "a", "idle", "before\n```\nafter")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("after", blob)

    def test_long_output_truncated(self):
        card = lk.build_pane_card("p", "a", "idle", "x" * 8000)
        self.assertLess(len(json.dumps(card)), 6000)

    def test_empty_output_safe(self):
        self.assertIn("elements", lk.build_pane_card("p", "a", "idle", ""))


class StatusColorTests(unittest.TestCase):
    def test_each_status_has_a_color(self):
        for st in ("blocked", "working", "done", "idle", "unknown"):
            self.assertTrue(lk.status_color(st))

    def test_working_is_not_idle_color(self):
        self.assertNotEqual(lk.status_color("working"), lk.status_color("idle"))


class RenderModeTests(unittest.TestCase):
    """卡片好看，但纯文本更省、更不容易被飞书改版坑到——两种都留着。"""

    def test_default_is_card(self):
        self.assertEqual(lk.normalize_render_mode(""), "card")

    def test_text_mode_recognized(self):
        self.assertEqual(lk.normalize_render_mode("text"), "text")

    def test_case_and_space_tolerant(self):
        self.assertEqual(lk.normalize_render_mode("  TEXT "), "text")

    def test_unknown_falls_back_to_card(self):
        self.assertEqual(lk.normalize_render_mode("rainbow"), "card")

    def test_none_is_card(self):
        self.assertEqual(lk.normalize_render_mode(None), "card")


class RenderCommandTests(unittest.TestCase):
    def test_render_is_a_command(self):
        self.assertEqual(lk.parse_command("/render text"), ("render", "text"))

    def test_bare_render_shows_current(self):
        self.assertEqual(lk.parse_command("/render"), ("render", ""))


class PaneTextFallbackTests(unittest.TestCase):
    """text 模式下必须还是原来那套纯文本，不能悄悄变形。"""

    def test_text_mode_output_is_plain(self):
        out = lk.format_pane_text("tailcale", PANE_SAMPLE, follow_up="继续")
        self.assertIn("tailcale", out)
        self.assertIn("继续", out)
        self.assertNotIn("<font", out)

    def test_text_mode_truncates(self):
        out = lk.format_pane_text("p", "x" * 9000, follow_up="")
        self.assertLess(len(out), 3400)


class BlankLineTests(unittest.TestCase):
    """手机屏幕小，40% 都是空行等于一半内容被挤出屏幕。"""

    def test_collapses_single_blank_between_paragraphs(self):
        """段落间的单个空行直接去掉——Markdown 的段距在聊天里是浪费。"""
        out = lk.clean_pane("第一段\n\n第二段\n\n第三段")
        self.assertEqual(out, "第一段\n第二段\n第三段")

    def test_keeps_content_lines(self):
        out = lk.clean_pane("a\n\nb")
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_collapses_many_blanks(self):
        self.assertNotIn("\n\n", lk.clean_pane("a\n\n\n\n\nb"))

    def test_drops_html_comment(self):
        """AI_DIALOG_SUMMARY 这类注释对手机阅读毫无价值。"""
        self.assertNotIn("AI_DIALOG_SUMMARY",
                         lk.clean_pane("正文\n<!-- AI_DIALOG_SUMMARY: xxx -->"))

    def test_drops_br_tag(self):
        self.assertNotIn("<br>", lk.clean_pane("正文\n<br>\n更多"))

    def test_blank_ratio_drops(self):
        text = "\n\n".join(f"段落 {i}" for i in range(10))
        out = lk.clean_pane(text)
        blanks = sum(1 for l in out.splitlines() if not l.strip())
        self.assertEqual(blanks, 0)

    def test_real_sample_has_no_blank_lines(self):
        sample = "标题\n\n  正文一\n\n  正文二\n\n  <br>\n\n  结尾"
        out = lk.clean_pane(sample)
        self.assertEqual(sum(1 for l in out.splitlines() if not l.strip()), 0)
        self.assertIn("结尾", out)


class SerialQueueTests(unittest.TestCase):
    """同一个群的消息必须串行处理。

    并发跑的话，send_text 的「粘贴 + 回车」两步会交错：第二条的粘贴
    插进第一条的回车之前，结果是两条消息糊成一条。
    """

    def test_same_chat_runs_in_order(self):
        order = []

        async def work(tag, delay):
            await asyncio.sleep(delay)
            order.append(tag)

        async def main():
            q = lk.ChatQueue()
            # 第一个慢、第二个快；串行的话仍应先 a 后 b
            q.submit("oc_1", lambda: work("a", 0.05))
            q.submit("oc_1", lambda: work("b", 0.0))
            await q.drain()

        asyncio.run(main())
        self.assertEqual(order, ["a", "b"])

    def test_different_chats_run_concurrently(self):
        """不同群之间不该互相阻塞。"""
        order = []

        async def work(tag, delay):
            await asyncio.sleep(delay)
            order.append(tag)

        async def main():
            q = lk.ChatQueue()
            q.submit("oc_slow", lambda: work("slow", 0.08))
            q.submit("oc_fast", lambda: work("fast", 0.0))
            await q.drain()

        asyncio.run(main())
        self.assertEqual(order, ["fast", "slow"])

    def test_failure_does_not_block_queue(self):
        """一条炸了，后面的还得跑完。"""
        done = []

        async def boom():
            raise RuntimeError("nope")

        async def ok():
            done.append("ok")

        async def main():
            q = lk.ChatQueue()
            q.submit("oc_1", boom)
            q.submit("oc_1", ok)
            await q.drain()

        asyncio.run(main())
        self.assertEqual(done, ["ok"])

    def test_queue_cleaned_up_when_empty(self):
        """队列跑空要销毁，否则群一多就泄漏。"""
        async def main():
            q = lk.ChatQueue()
            q.submit("oc_1", lambda: asyncio.sleep(0))
            await q.drain()
            return len(q)

        self.assertEqual(asyncio.run(main()), 0)

    def test_many_items_all_run(self):
        seen = []

        async def main():
            q = lk.ChatQueue()
            for i in range(10):
                q.submit("oc_1", lambda i=i: _append(seen, i))
            await q.drain()

        async def _append(bucket, value):
            bucket.append(value)

        asyncio.run(main())
        self.assertEqual(seen, list(range(10)))


class BindingStoreTests(unittest.TestCase):
    """群 ↔ agent 的绑定要落盘：服务重启后不该让人重新 /read 一遍。"""

    def test_saves_and_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            lk.BindingStore(path).set("oc_1", "w1:p1")
            self.assertEqual(lk.BindingStore(path).get("oc_1"), "w1:p1")

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(lk.BindingStore(os.path.join(d, "none.json")).get("oc_1"))

    def test_corrupt_file_does_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            with open(path, "w") as fh:
                fh.write("{broken")
            self.assertIsNone(lk.BindingStore(path).get("oc_1"))

    def test_non_dict_payload_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            with open(path, "w") as fh:
                json.dump(["not", "a", "dict"], fh)
            self.assertIsNone(lk.BindingStore(path).get("oc_1"))

    def test_overwrite_updates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            store = lk.BindingStore(path)
            store.set("oc_1", "w1:p1")
            store.set("oc_1", "w2:p1")
            self.assertEqual(lk.BindingStore(path).get("oc_1"), "w2:p1")

    def test_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            lk.BindingStore(path).set("oc_1", "w1:p1")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_as_dict_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            store = lk.BindingStore(path)
            store.set("oc_1", "w1:p1")
            store.set("oc_2", "w2:p1")
            self.assertEqual(store.as_dict(), {"oc_1": "w1:p1", "oc_2": "w2:p1"})


class StreamCardTests(unittest.TestCase):
    """流式卡片：agent 干活时卡片自己刷新，不用反复 /read。"""

    def test_card_json_declares_streaming(self):
        card = lk.build_stream_card_json("tailcale", "claude")
        self.assertTrue(card["config"]["streaming_mode"])

    def test_card_has_addressable_element(self):
        """流式更新要按 element_id 定位，没有它就没法追加。"""
        card = lk.build_stream_card_json("p", "a")
        blob = json.dumps(card)
        self.assertIn(lk.STREAM_ELEMENT_ID, blob)

    def test_schema_is_2_0(self):
        """streaming_mode 只有 schema 2.0 认。"""
        self.assertEqual(lk.build_stream_card_json("p", "a")["schema"], "2.0")

    def test_project_in_header(self):
        card = lk.build_stream_card_json("tailcale", "claude")
        self.assertIn("tailcale", json.dumps(card, ensure_ascii=False))


class StreamThrottleTests(unittest.TestCase):
    """节流：pane 每秒都在变，全推的话既费配额又刷得人眼晕。"""

    def test_first_update_passes(self):
        t = lk.StreamThrottle(interval=1.5)
        self.assertTrue(t.should_send("abc", now=100.0))

    def test_same_content_suppressed(self):
        t = lk.StreamThrottle(interval=1.5)
        t.should_send("abc", now=100.0)
        self.assertFalse(t.should_send("abc", now=200.0))

    def test_too_soon_suppressed(self):
        t = lk.StreamThrottle(interval=1.5)
        t.should_send("abc", now=100.0)
        self.assertFalse(t.should_send("changed", now=100.5))

    def test_after_interval_passes(self):
        t = lk.StreamThrottle(interval=1.5)
        t.should_send("abc", now=100.0)
        self.assertTrue(t.should_send("changed", now=102.0))

    def test_sequence_increases(self):
        """飞书按 sequence 排序，重复或倒退会丢帧。"""
        t = lk.StreamThrottle(interval=0)
        a = t.next_sequence()
        b = t.next_sequence()
        self.assertGreater(b, a)

    def test_sequence_starts_above_one(self):
        """创建卡片本身算 sequence 1，追加得从 2 起。"""
        self.assertGreaterEqual(lk.StreamThrottle(interval=0).next_sequence(), 2)


TABLE_SAMPLE = """三项待办全部完成
  ┌─────┬────────────┬───────────────┐
  │  #  │     项     │     状态      │
  │ 1   │ 串行队列   │ ✅ 修并发隐患 │
  │ 2   │ 绑定持久化 │ ✅ 重启不丢   │
  └─────┴────────────┴───────────────┘
后续正文"""


class TableBorderTests(unittest.TestCase):
    """Markdown 表格在终端里被渲染成 ┌─┬─┐ 边框；手机窄屏本来就会错行，
    与其留个残缺的框，不如只留内容。"""

    def test_drops_top_border(self):
        self.assertNotIn("┌", lk.clean_pane(TABLE_SAMPLE))

    def test_drops_bottom_border(self):
        self.assertNotIn("└", lk.clean_pane(TABLE_SAMPLE))

    def test_drops_mid_border(self):
        out = lk.clean_pane("a\n  ├────┼────┤\nb")
        self.assertNotIn("├", out)

    def test_keeps_table_content(self):
        """边框去掉，行里的数据不能丢。"""
        out = lk.clean_pane(TABLE_SAMPLE)
        self.assertIn("串行队列", out)
        self.assertIn("重启不丢", out)

    def test_keeps_surrounding_text(self):
        out = lk.clean_pane(TABLE_SAMPLE)
        self.assertIn("三项待办全部完成", out)
        self.assertIn("后续正文", out)

    def test_strips_leading_trailing_pipes(self):
        """行首尾的 │ 在手机上只占地方。"""
        out = lk.clean_pane("│ 内容 │")
        self.assertNotIn("│", out)
        self.assertIn("内容", out)

    def test_inner_separators_become_spaces(self):
        """列之间要留可读的间隔，不能挤成一坨。"""
        out = lk.clean_pane("│ a │ b │")
        self.assertIn("a", out)
        self.assertIn("b", out)
        self.assertNotIn("ab", out.replace(" ", "x"))

    def test_markdown_rule_dropped(self):
        self.assertEqual(lk.clean_pane("正文\n---\n更多"), "正文\n更多")

    def test_dashed_separator_dropped(self):
        self.assertNotIn("───", lk.clean_pane("a\n────────\nb"))


class StableIndexTests(unittest.TestCase):
    """序号必须与状态无关。

    原来按 sorted_agents（状态优先）编号，agent 一开始干活就跳到队首，
    其余全部后移——用户看到列表、几秒后打 /read 3，操作到的已经是别人了。
    """

    def test_index_survives_status_change(self):
        agents = make_agents(4)
        before = lk.index_agents(agents)
        # 3 号开始干活
        changed = [dict(a) for a in agents]
        target = before[2]["pane_id"]
        for a in changed:
            if a["pane_id"] == target:
                a["status"] = "working"
        after = lk.index_agents(changed)
        self.assertEqual([a["pane_id"] for a in before],
                         [a["pane_id"] for a in after])

    def test_lookup_stable_across_status_change(self):
        agents = make_agents(4)
        picked = lk.match_agent(agents, "3")["pane_id"]
        changed = [dict(a) for a in agents]
        for a in changed:
            if a["pane_id"] == picked:
                a["status"] = "blocked"
        self.assertEqual(lk.match_agent(changed, "3")["pane_id"], picked)

    def test_order_is_deterministic(self):
        """同一批 agent 任意打乱，序号都该一样。"""
        agents = make_agents(5)
        import random
        shuffled = agents[:]
        random.shuffle(shuffled)
        self.assertEqual([a["pane_id"] for a in lk.index_agents(agents)],
                         [a["pane_id"] for a in lk.index_agents(shuffled)])

    def test_new_agent_appended_not_inserted(self):
        """新 agent 出现不该把已有序号顶掉——除非 pane_id 排序就该在前。"""
        agents = make_agents(3)
        first_three = [a["pane_id"] for a in lk.index_agents(agents)]
        extra = agents + [{"pane_id": "zz99:p1", "agent": "x", "status": "idle",
                           "project": "zzz", "cwd": "/z", "host": "local"}]
        self.assertEqual([a["pane_id"] for a in lk.index_agents(extra)][:3], first_three)

    def test_list_is_ordered_by_number(self):
        """序号必须顺着排。乱序的 8/4/13/2 扫一眼根本找不到目标。"""
        agents = make_agents(5)
        import re
        nums = [int(m.group(1)) for m in
                (re.match(r"\s*(\d+)\.", l) for l in lk.format_agent_list(agents).splitlines())
                if m]
        self.assertEqual(nums, sorted(nums))

    def test_status_still_visible(self):
        """不分组了，但状态得看得见。"""
        agents = make_agents(1, status="blocked", project="urgent")
        self.assertIn("urgent", lk.format_agent_list(agents))
        out = lk.format_agent_list(agents)
        self.assertTrue(any(t in out for t in ("BLOCKED", "⏸", "blocked")))

    def test_blocked_marked_distinctly(self):
        agents = make_agents(1, project="calm") + [
            {"pane_id": "w9:p1", "agent": "a", "status": "blocked",
             "project": "urgent", "cwd": "/u", "host": "local"}]
        out = lk.format_agent_list(agents)
        calm_line = [l for l in out.splitlines() if "calm" in l][0]
        urgent_line = [l for l in out.splitlines() if "urgent" in l][0]
        self.assertNotEqual(calm_line.strip()[:2], urgent_line.strip()[:2])

    def test_list_numbers_match_lookup(self):
        """列表上写的号，match_agent 必须认——这是全部的意义所在。"""
        agents = make_agents(3)
        agents.append({"pane_id": "w9:p1", "agent": "opencode", "status": "blocked",
                       "project": "urgent", "cwd": "/w/u", "host": "local"})
        out = lk.format_agent_list(agents)
        import re
        for line in out.splitlines():
            found = re.match(r"\s*(\d+)\.\s+(\S+)", line)
            if found:
                number, project = found.group(1), found.group(2)
                picked = lk.match_agent(agents, number)
                self.assertIsNotNone(picked, f"列表里的 {number} 号查不到")
                self.assertEqual(picked.get("project"), project)


class NewAgentParseTests(unittest.TestCase):
    """/new <序号> [agent 类型] —— 在某个 agent 的同目录再开一个。"""

    def test_index_only_defaults_to_claude(self):
        self.assertEqual(lk.parse_new_args("3"), ("3", "claude"))

    def test_explicit_kind(self):
        self.assertEqual(lk.parse_new_args("3 codex"), ("3", "codex"))

    def test_kind_is_lowercased(self):
        self.assertEqual(lk.parse_new_args("3 CODEX")[1], "codex")

    def test_empty_yields_no_target(self):
        self.assertEqual(lk.parse_new_args(""), (None, "claude"))

    def test_extra_words_ignored(self):
        """多余的词不当成 agent 类型——避免把任务描述误当类型。"""
        self.assertEqual(lk.parse_new_args("3 codex 顺便改一下"), ("3", "codex"))

    def test_unknown_kind_rejected(self):
        self.assertFalse(lk.is_valid_agent_kind("definitely-not-an-agent"))

    def test_known_kinds_accepted(self):
        for kind in ("claude", "codex", "gemini", "opencode"):
            self.assertTrue(lk.is_valid_agent_kind(kind))


class NewAgentCommandTests(unittest.TestCase):
    def test_new_is_a_command(self):
        self.assertEqual(lk.parse_command("/new 3"), ("new", "3"))

    def test_bare_new(self):
        self.assertEqual(lk.parse_command("/new"), ("new", ""))


class StartAgentTests(unittest.TestCase):
    """新工作区起来是个空 shell，得把启动命令打进去。"""

    def test_launch_line_uses_kind(self):
        self.assertIn("codex", lk.agent_launch_command("codex", "/w/proj"))

    def test_launch_line_cds_first(self):
        """relay 建的工作区不带 --cwd，得自己 cd 过去。"""
        line = lk.agent_launch_command("claude", "/w/proj")
        self.assertIn("cd ", line)
        self.assertIn("/w/proj", line)

    def test_no_cwd_skips_cd(self):
        self.assertNotIn("cd ", lk.agent_launch_command("claude", ""))

    def test_quotes_path_with_spaces(self):
        line = lk.agent_launch_command("claude", "/w/my proj")
        self.assertIn("'/w/my proj'", line)


MULTI_QUESTION = """要用哪种方案实现？
  1. 直接改现有函数
  2. 新增一层抽象
  3. 先写测试

新 agent 用哪个？
  1. 默认 claude
  2. 只起 codex
  3. 只开空 shell"""


class MultiQuestionTests(unittest.TestCase):
    """AskUserQuestion 一次能问好几组，每组都从 1 开始重新编号。

    只认第一组的话，卡片显示的是第一组选项，而 agent 可能正等第二组的
    答案——点下去就答错了。
    """

    def test_detects_multiple_groups(self):
        groups = lk.detect_option_groups(MULTI_QUESTION)
        self.assertEqual(len(groups), 2)

    def test_each_group_keeps_own_options(self):
        groups = lk.detect_option_groups(MULTI_QUESTION)
        self.assertIn("新增一层抽象", groups[0]["options"][1])
        self.assertIn("只起 codex", groups[1]["options"][1])

    def test_group_carries_its_question(self):
        """得让人知道这组按钮在回答哪个问题。"""
        groups = lk.detect_option_groups(MULTI_QUESTION)
        self.assertIn("方案", groups[0]["question"])
        self.assertIn("agent", groups[1]["question"])

    def test_single_group_still_works(self):
        groups = lk.detect_option_groups("选哪个？\n  1. A\n  2. B")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["options"], ["A", "B"])

    def test_plain_output_yields_nothing(self):
        self.assertEqual(lk.detect_option_groups("just logs\nsecond line"), [])

    def test_prose_numbering_not_a_group(self):
        self.assertEqual(
            lk.detect_option_groups("改动：\n1. 修了 a\n2. 修了 b\n然后跑了测试。"), [])

    def test_legacy_helper_returns_last_group(self):
        """detect_pane_options 保持兼容，返回最后一组（agent 当前在等的那个）。"""
        self.assertEqual(lk.detect_pane_options(MULTI_QUESTION)[1], "只起 codex")


class CurrentGroupTests(unittest.TestCase):
    """只推 agent 当前在等的那一组。

    实测：tab 栏列出全部组，选项区只渲染当前 tab 那一组。数字键只作用于
    当前 tab——同时展示两组的话，你点第二组，那个数字会被当成第一组的答案。
    真实抓屏见 RealPaneSelectorTests。
    """

    def test_uses_last_group(self):
        """最后一组才是 agent 正在等的。"""
        groups = lk.detect_option_groups(MULTI_QUESTION)
        current = lk.current_option_group(groups)
        self.assertIn("只起 codex", current["options"][1])

    def test_single_group_returned_as_is(self):
        groups = lk.detect_option_groups("选哪个？\n  1. A\n  2. B")
        self.assertEqual(lk.current_option_group(groups)["options"], ["A", "B"])

    def test_empty_yields_none(self):
        self.assertIsNone(lk.current_option_group([]))

    def test_card_shows_the_question(self):
        """得让人知道这组按钮在回答什么。"""
        groups = lk.detect_option_groups(MULTI_QUESTION)
        current = lk.current_option_group(groups)
        card = lk.build_options_card("w1:p1", "p", current["options"], "abcde",
                                     question=current["question"])
        self.assertIn("agent", json.dumps(card, ensure_ascii=False))

    def test_card_without_question_still_valid(self):
        card = lk.build_options_card("w1:p1", "p", ["A", "B"], "abcde")
        self.assertIn("elements", card)


class UnifiedOptionCardTests(unittest.TestCase):
    """blocked 卡片和「读到时补的」选项卡片必须长一个样。

    以前是两个 build 函数各带一份头部配色和按钮样式：relay 推的 blocked 走
    orange + primary/default/danger，答完第一组后补推的下一组走 turquoise +
    只有首项 primary。同一次选择的前后两张卡片看着像两个功能。

    现在合并成 build_option_card 一条渲染路径，两个旧名字保留为薄包装。
    """

    def _keys(self, card):
        return [b["value"]["k"] for b in buttons_in(card)
                if b["value"].get("a") == lk.ACTION_CODES["approval"]]

    def _styles(self, card):
        return [b["type"] for b in buttons_in(card)
                if b["value"].get("a") == lk.ACTION_CODES["approval"]]

    def test_same_header_template(self):
        blocked = lk.build_option_card(
            "w1:p1", "herdr", ["A", "B"], "abcde",
            prompt="may I?", agent="opencode")
        follow_up = lk.build_option_card("w1:p1", "herdr", ["A", "B"], "abcde")
        self.assertEqual(blocked["header"]["template"],
                         follow_up["header"]["template"])

    def test_same_button_styles(self):
        blocked = lk.build_option_card(
            "w1:p1", "herdr", ["A", "B"], "abcde", prompt="q", agent="a")
        follow_up = lk.build_option_card("w1:p1", "herdr", ["A", "B"], "abcde")
        self.assertEqual(self._styles(blocked), self._styles(follow_up))

    def test_blocked_variant_shows_prompt(self):
        card = lk.build_option_card(
            "w1:p1", "herdr", ["A", "B"], "abcde",
            prompt="rm -rf /tmp/x", agent="opencode")
        self.assertIn("rm -rf /tmp/x", json.dumps(card, ensure_ascii=False))

    def test_follow_up_variant_shows_question(self):
        card = lk.build_option_card(
            "w1:p1", "herdr", ["A", "B"], "abcde", question="选哪个？")
        self.assertIn("选哪个？", json.dumps(card, ensure_ascii=False))

    def test_keys_are_indexes_in_both_variants(self):
        """两种形态都必须发选项序号，不能发选项文本。"""
        for kwargs in ({"prompt": "q", "agent": "a"}, {"question": "选哪个？"}):
            card = lk.build_option_card(
                "w1:p1", "herdr", ["A", "B", "C"], "abcde", **kwargs)
            self.assertEqual(self._keys(card), ["1", "2", "3"])

    def test_all_options_get_a_button(self):
        """检出几个选项就要有几个按钮，不能少。"""
        options = [f"选项 {i}" for i in range(1, 8)]
        card = lk.build_option_card("w1:p1", "herdr", options, "abcde")
        self.assertEqual(len(self._keys(card)), 7)

    def test_exactly_one_primary(self):
        """一组里只能有一个高亮按钮——两个等于没有。"""
        for opts in (["直接改现有函数", "新增一层抽象", "先写测试"],
                     ["approve all pending", "configure", "cancel"],
                     lk.TOOL_OPTIONS):
            card = lk.build_option_card("w1:p1", "p", opts, "g")
            primaries = [b for b in buttons_in(card) if b["type"] == "primary"]
            self.assertEqual(len(primaries), 1, opts)

    def test_reject_option_is_danger(self):
        """拒绝项要红：点错了代价最大。"""
        card = lk.build_option_card("w1:p1", "p", ["Yes", "No (tab to edit)"], "g")
        # 按钮只放序号，配色仍按选项文字判定，用序号索引取。
        styles = {b["text"]["content"]: b["type"] for b in buttons_in(card)}
        self.assertEqual(styles["2"], "danger")

    def test_trust_does_not_steal_primary(self):
        """「总是允许」不该是最显眼的按钮——它风险最大。"""
        card = lk.build_option_card(
            "w1:p1", "p", ["trust, always allow", "yes once"], "g")
        styles = {b["text"]["content"]: b["type"] for b in buttons_in(card)}
        self.assertEqual(styles["1"], "default")
        self.assertEqual(styles["2"], "primary")

    def test_plain_chinese_option_not_reddened(self):
        """「不错的方案」不是拒绝项，别染红。"""
        card = lk.build_option_card("w1:p1", "p", ["不错的方案", "重构但不动接口"], "g")
        self.assertNotIn("danger", [b["type"] for b in buttons_in(card)])

    def test_caps_at_max_options(self):
        """按钮数封顶，飞书一张卡片放不下太多。"""
        card = lk.build_option_card(
            "w1:p1", "p", [f"opt{i}" for i in range(20)], "g")
        keys = [b["value"]["k"] for b in buttons_in(card)
                if b["value"].get("a") == lk.ACTION_CODES["approval"]]
        self.assertEqual(len(keys), lk._MAX_OPTIONS)

    def test_legacy_builders_still_work(self):
        """旧名字保留：relay 监听和读 pane 两条路径都还在调。"""
        self.assertIn("elements", lk.build_blocked_card(
            "w1:p1", "a", "p", "q", lk.TOOL_OPTIONS, "abcde"))
        self.assertIn("elements", lk.build_options_card(
            "w1:p1", "p", ["A", "B"], "abcde"))

    def test_legacy_builders_agree_on_style(self):
        """两个旧入口渲染出来的样式必须一致——这正是原来的毛病。"""
        blocked = lk.build_blocked_card(
            "w1:p1", "a", "p", "q", ["yes", "no"], "abcde")
        options = lk.build_options_card("w1:p1", "p", ["yes", "no"], "abcde")
        self.assertEqual(blocked["header"]["template"],
                         options["header"]["template"])
        self.assertEqual(self._styles(blocked), self._styles(options))


class LongSelectorTests(unittest.TestCase):
    """选项多、正文长、每项自带说明时，选择器不能被漏掉或缺项。

    e7934c3 把 detect_option_groups 拆成小函数时改掉了两个真实缺陷，但没带
    测试。这些用例把缺陷钉住：
    - 原先按固定 42 行截窗口，长选择器靠前的组编号不从 1 起，被整组丢弃；
    - 原先「最后一个选项之后还有内容」就判定选择器已翻过去，而
      AskUserQuestion 每个选项都带一行缩进说明，于是一组都认不出来。
    """

    def _long_tail(self, groups=2, options=4, preamble=60):
        lines = [f"日志第 {i} 行：跑测试、读文件、写补丁" for i in range(preamble)]
        for g in range(groups):
            lines.append("")
            lines.append(f"第 {g + 1} 个问题要怎么定？")
            for o in range(options):
                lines.append(f"  {o + 1}. 第 {g + 1} 组的第 {o + 1} 个候选方案")
        return "\n".join(lines)

    def test_keeps_all_options_in_each_group(self):
        groups = lk.detect_option_groups(self._long_tail())
        self.assertEqual(len(groups), 2)
        for group in groups:
            self.assertEqual(len(group["options"]), 4)

    def test_survives_long_preamble(self):
        """选择器前面压 200 行日志，照样要认出来。"""
        groups = lk.detect_option_groups(self._long_tail(preamble=200))
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[-1]["options"]), 4)

    def test_long_selector_keeps_early_groups(self):
        """选择器本身超过旧的 42 行窗口时，靠前的组不能丢。"""
        groups = lk.detect_option_groups(self._long_tail(groups=5, options=7))
        self.assertEqual(len(groups), 5)
        for group in groups:
            self.assertEqual(len(group["options"]), 7)

    def test_last_group_is_complete(self):
        """当前在等的那一组尤其不能缺项——缺了就点不到。"""
        current = lk.current_option_group(
            lk.detect_option_groups(self._long_tail(options=5)))
        self.assertEqual(len(current["options"]), 5)
        self.assertIn("第 5 个候选", current["options"][4])

    def test_options_with_description_lines(self):
        """AskUserQuestion 每个选项自带一行缩进说明。

        把说明当成「选择器后面的正文」的话整组被丢掉——线上表现是卡片一个
        按钮都没有，只能手打数字。
        """
        text = """要用哪种方案实现？
  1. 直接改现有函数
     在原地扩展，改动最小
  2. 新增一层抽象
     多一个间接层，但更好测
  3. 先写测试
     TDD，慢但稳"""
        groups = lk.detect_option_groups(text)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["options"]), 3)

    def test_survives_tui_input_box(self):
        """选择器下面就是 TUI 的圆角输入框，不能算正文。

        框线字符漏一个（比如 ╮），输入框就被当成正文，整个选择器被判定为
        「已翻过去」，卡片一个按钮都不剩。
        """
        text = """选哪个？
  1. A
     说明 A
  2. B
     说明 B

╭──────────────────────╮
│ >                    │
╰──────────────────────╯"""
        groups = lk.detect_option_groups(text)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["options"], ["A", "B"])

    def test_two_groups_with_descriptions(self):
        """两组都带说明行时，两组都要在，且都不缺项。"""
        text = """要用哪种方案？
  1. 改现有函数
     改动最小
  2. 新增抽象
     更好测

新 agent 用哪个？
  1. 默认 claude
     跟现在一致
  2. 只起 codex
     换个模型"""
        groups = lk.detect_option_groups(text)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]["options"]), 2)
        self.assertEqual(len(groups[1]["options"]), 2)
        self.assertIn("agent", groups[1]["question"])

    def test_prose_after_options_still_rejected(self):
        """放宽判定不能把散文里的编号列表也认成选择器。"""
        text = self._long_tail() + "\n\n然后我跑了测试，全部通过。"
        self.assertEqual(lk.detect_option_groups(text), [])


class AutoWatchTests(unittest.TestCase):
    """发完指令自动跟随——手工再打一次 /watch 太啰嗦。"""

    def test_default_is_on(self):
        self.assertTrue(lk.normalize_autowatch("").enabled)

    def test_off_recognized(self):
        self.assertFalse(lk.normalize_autowatch("off").enabled)

    def test_explicit_on(self):
        self.assertTrue(lk.normalize_autowatch("on").enabled)

    def test_custom_seconds(self):
        self.assertEqual(lk.normalize_autowatch("180").limit, 180)

    def test_seconds_clamped_to_ceiling(self):
        """跟太久白烧配额，给个上限。"""
        self.assertLessEqual(lk.normalize_autowatch("99999").limit, lk.AUTOWATCH_MAX_S)

    def test_seconds_clamped_to_floor(self):
        """太短的话卡片刚建好就结束，没意义。"""
        self.assertGreaterEqual(lk.normalize_autowatch("1").limit, lk.AUTOWATCH_MIN_S)

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(lk.normalize_autowatch("banana").limit, lk.AUTOWATCH_DEFAULT_S)

    def test_off_keeps_limit_for_later(self):
        """关掉时也保留时长，重新打开不用再设一遍。"""
        self.assertGreater(lk.normalize_autowatch("off").limit, 0)


class AutowatchCommandTests(unittest.TestCase):
    def test_is_a_command(self):
        self.assertEqual(lk.parse_command("/autowatch off"), ("autowatch", "off"))

    def test_bare_shows_current(self):
        self.assertEqual(lk.parse_command("/autowatch"), ("autowatch", ""))


class WatchDeadlineTests(unittest.TestCase):
    """跟随要有上限：agent 卡住不动时不能一直跟着。"""

    def test_not_expired_within_limit(self):
        self.assertFalse(lk.watch_expired(started=100.0, now=150.0, limit=120))

    def test_expired_past_limit(self):
        self.assertTrue(lk.watch_expired(started=100.0, now=260.0, limit=120))

    def test_zero_limit_never_expires(self):
        """limit=0 表示不限时（手工 /watch 用）。"""
        self.assertFalse(lk.watch_expired(started=100.0, now=99999.0, limit=0))


class MultiChatAuthTests(unittest.TestCase):
    """多个群各管一个 agent：守门必须放行所有授权群，而不是只放行一个。"""

    def ctx(self, chat_id):
        return lk.MessageContext(chat_id=chat_id, message_id="om_1",
                                 sender_open_id="ou_user", chat_type="p2p")

    def test_single_chat_still_works(self):
        self.assertTrue(lk.is_authorized_chat("oc_1", {"oc_1"}))

    def test_second_chat_allowed_when_listed(self):
        self.assertTrue(lk.is_authorized_chat("oc_2", {"oc_1", "oc_2"}))

    def test_unlisted_chat_rejected(self):
        self.assertFalse(lk.is_authorized_chat("oc_evil", {"oc_1", "oc_2"}))

    def test_empty_allowlist_is_discovery_mode(self):
        """没配任何群时放行，方便第一次拿 chat_id。"""
        self.assertTrue(lk.is_authorized_chat("oc_any", set()))

    def test_parse_comma_separated(self):
        self.assertEqual(lk.parse_chat_ids("oc_1,oc_2"), {"oc_1", "oc_2"})

    def test_parse_tolerates_spaces(self):
        self.assertEqual(lk.parse_chat_ids(" oc_1 , oc_2 "), {"oc_1", "oc_2"})

    def test_parse_empty_yields_empty_set(self):
        self.assertEqual(lk.parse_chat_ids(""), set())

    def test_gate_allows_multiple_chats(self):
        allowed = {"oc_1", "oc_2"}
        for chat in ("oc_1", "oc_2"):
            self.assertTrue(lk.should_handle(self.ctx(chat), "ou_bot", allowed))

    def test_gate_rejects_outsider(self):
        self.assertFalse(
            lk.should_handle(self.ctx("oc_x"), "ou_bot", {"oc_1", "oc_2"}))

    def test_gate_accepts_legacy_string(self):
        """向后兼容：单个字符串 chat_id 仍能用。"""
        self.assertTrue(lk.should_handle(self.ctx("oc_1"), "ou_bot", "oc_1"))


class MultiChatIsolationTests(unittest.TestCase):
    """两个群各绑各的 agent，互不串台。"""

    def test_bindings_are_independent(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w2:p1")
        self.assertEqual(bot.active_pane("oc_1"), "w1:p1")
        self.assertEqual(bot.active_pane("oc_2"), "w2:p1")

    def test_notification_goes_only_to_bound_chat(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w2:p1")
        self.assertEqual(lk.chats_watching(bot, "w1:p1"), ["oc_1"])
        self.assertEqual(lk.chats_watching(bot, "w2:p1"), ["oc_2"])

    def test_rebinding_one_chat_leaves_other(self):
        bot = make_bot()
        bot.set_active("oc_1", "w1:p1")
        bot.set_active("oc_2", "w2:p1")
        bot.set_active("oc_1", "w9:p1")
        self.assertEqual(bot.active_pane("oc_2"), "w2:p1")


def dup_agents():
    """两个完全同名同目录的 agent，只有 workspace_id 不同。"""
    return [
        {"pane_id": "w1B:p1", "agent": "claude", "status": "idle",
         "project": "yqg-dw-datapilot", "cwd": "/code/yqg-dw-datapilot",
         "host": "local", "workspace_id": "w1B"},
        {"pane_id": "w22:p1", "agent": "claude", "status": "idle",
         "project": "yqg-dw-datapilot", "cwd": "/code/yqg-dw-datapilot",
         "host": "local", "workspace_id": "w22"},
        {"pane_id": "w30:p1", "agent": "claude", "status": "idle",
         "project": "unique-one", "cwd": "/code/unique", "host": "local",
         "workspace_id": "w30"},
    ]


class DuplicateNameTests(unittest.TestCase):
    """同项目同目录开两个 agent 时，列表上两行一模一样，根本分不清。"""

    def test_duplicate_rows_are_distinguishable(self):
        lines = [l for l in lk.format_agent_list(dup_agents()).splitlines()
                 if "yqg-dw-datapilot" in l]
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[0], lines[1])

    def test_workspace_shown_for_duplicates(self):
        out = lk.format_agent_list(dup_agents())
        self.assertIn("w1B", out)
        self.assertIn("w22", out)

    def test_unique_names_stay_clean(self):
        """不重名的不该被加上噪音。"""
        line = [l for l in lk.format_agent_list(dup_agents()).splitlines()
                if "unique-one" in l][0]
        self.assertNotIn("w30", line)

    def test_all_same_name_still_distinguishable(self):
        """三个同名同父目录时，退回 workspace id 也必须两两不同。"""
        lines = [l for l in lk.format_agent_list(make_agents(3)).splitlines()
                 if "project" in l and l.strip().startswith(("○","▶","✅","⏸"))]
        self.assertEqual(len(lines), 3)
        self.assertEqual(len(set(lines)), 3)

    def test_distinct_names_get_no_suffix(self):
        agents = [
            {"pane_id": "w1:p1", "agent": "claude", "status": "idle",
             "project": "alpha", "cwd": "/c/alpha", "host": "local"},
            {"pane_id": "w2:p1", "agent": "claude", "status": "idle",
             "project": "beta", "cwd": "/c/beta", "host": "local"},
        ]
        self.assertNotIn("[", lk.format_agent_list(agents))

    def test_cwd_tail_used_when_dirs_differ(self):
        """目录不同的重名，显示目录比显示 workspace id 好懂。"""
        agents = [
            {"pane_id": "w1:p1", "agent": "claude", "status": "idle",
             "project": "api", "cwd": "/code/frontend/api", "host": "local",
             "workspace_id": "w1"},
            {"pane_id": "w2:p1", "agent": "claude", "status": "idle",
             "project": "api", "cwd": "/code/backend/api", "host": "local",
             "workspace_id": "w2"},
        ]
        out = lk.format_agent_list(agents)
        self.assertIn("frontend", out)
        self.assertIn("backend", out)

    def test_picker_card_labels_distinguishable(self):
        """卡片按钮同样要能分清。"""
        card = lk.build_agent_picker_card("read", dup_agents())
        labels = [b["text"]["content"] for b in buttons_in(card)]
        dup = [l for l in labels if "yqg-dw-datapilot" in l]
        self.assertEqual(len(set(dup)), 2)


class StatusGlyphTests(unittest.TestCase):
    """群名用的彩色状态符号。

    卡片里用的是黑白符号（_STATUS_ICONS: ⏸ ▶ ✅ ○），群名要彩色——
    会话列表里灰扑扑的符号扫一眼分不出轻重。两套刻意不同。
    """

    def test_four_states_map_to_colored_glyphs(self):
        self.assertEqual(lk.status_glyph("blocked"), "🔴")
        self.assertEqual(lk.status_glyph("working"), "🟡")
        self.assertEqual(lk.status_glyph("done"), "🟢")
        self.assertEqual(lk.status_glyph("idle"), "⚪️")

    def test_unknown_falls_back_to_idle_glyph(self):
        """unknown 与 idle 同色——都是「没事干」。"""
        self.assertEqual(lk.status_glyph("unknown"), "⚪️")

    def test_unrecognized_status_falls_back(self):
        """没见过的状态不能抛异常，群名比状态准确性重要。"""
        self.assertEqual(lk.status_glyph("wat"), "⚪️")
        self.assertEqual(lk.status_glyph(""), "⚪️")

    def test_glyphs_are_all_distinct_except_idle_unknown(self):
        """四个符号必须互不相同，否则区分不出状态。"""
        glyphs = {lk.status_glyph(s)
                  for s in ("blocked", "working", "done", "idle")}
        self.assertEqual(len(glyphs), 4)

    def test_every_known_status_has_a_glyph(self):
        """STATUS_ORDER 里的状态都得有符号，加状态时别漏。"""
        for status in lk.STATUS_ORDER:
            self.assertNotEqual(lk.status_glyph(status), "",
                                f"{status} 没有符号")


class ChatTitleStatusTests(unittest.TestCase):
    """群名带状态符号，不再带 herdr · 前缀。

    前缀每个群都一样，在会话列表这种窄地方纯属浪费；换成符号后
    同样的宽度能表达「该先处理谁」。
    """

    def test_status_glyph_leads_the_title(self):
        title = lk.chat_title_for("datapilot", status="blocked")
        self.assertTrue(title.startswith("🔴 "), title)
        self.assertIn("datapilot", title)

    def test_no_herdr_prefix_anymore(self):
        """前缀省下来的宽度是这次改动的全部意义。"""
        self.assertNotIn("herdr", lk.chat_title_for("x", status="working"))

    def test_status_omitted_means_no_glyph(self):
        """还不知道状态的调用点不该被塞一个假符号。"""
        self.assertEqual(lk.chat_title_for("tailcale"), "tailcale")

    def test_marker_sits_between_glyph_and_project(self):
        """标记仍在项目名前面——会话列表尾部会被截掉。"""
        title = lk.chat_title_for("same", " [w22]", status="done")
        self.assertTrue(title.startswith("🟢 "), title)
        self.assertLess(title.index("w22"), title.index("same"))

    def test_long_project_truncated_but_glyph_survives(self):
        """项目名太长时宁可截项目名，也要保住符号和标记。"""
        title = lk.chat_title_for("p" * 200, " [w22]", status="blocked")
        self.assertLessEqual(len(title), 60)
        self.assertTrue(title.startswith("🔴 "), title)
        self.assertIn("w22", title)

    def test_empty_project_still_produces_a_title(self):
        title = lk.chat_title_for("", status="idle")
        self.assertTrue(title.startswith("⚪️ "), title)
        self.assertIn("?", title)

    def test_same_project_different_status_differs(self):
        """状态变了群名就得变，否则符号不起作用。"""
        a = lk.chat_title_for("x", status="working")
        b = lk.chat_title_for("x", status="done")
        self.assertNotEqual(a, b)


class UnboundChatNameTests(unittest.TestCase):
    """/unbind 之后群名重置成什么。"""

    def test_unbound_name_has_no_glyph(self):
        """没绑 agent 就没有状态可显示。"""
        self.assertEqual(lk.UNBOUND_CHAT_NAME, "herdr")

    def test_old_prefix_constant_is_gone(self):
        """CHAT_TITLE_PREFIX 的两个用途都被接管了，别留着误用。"""
        self.assertFalse(hasattr(lk, "CHAT_TITLE_PREFIX"))


class ProjectChatDetectionTests(unittest.TestCase):
    """群名是不是 herdr 项目群。

    observer 靠这个判定过滤对账范围（它自己有一份副本）。原来靠
    startswith("herdr")，前缀删掉后必须换判据，否则所有项目群都被
    判成无关群、对账全废。
    """

    def test_recognizes_all_four_status_glyphs(self):
        for status in ("blocked", "working", "done", "idle"):
            name = lk.chat_title_for("proj", status=status)
            self.assertTrue(lk.is_project_chat(name), name)

    def test_recognizes_unbound_chat(self):
        """/unbind 之后的空闲群仍属项目群。"""
        self.assertTrue(lk.is_project_chat(lk.UNBOUND_CHAT_NAME))

    def test_recognizes_unbound_with_trailing_text(self):
        """历史上的「herdr · xxx」旧群名也得认，改造期间新旧并存。"""
        self.assertTrue(lk.is_project_chat("herdr · datapilot"))

    def test_rejects_unrelated_chat(self):
        """闲聊群不参与对账。"""
        self.assertFalse(lk.is_project_chat("盛大宝123"))
        self.assertFalse(lk.is_project_chat("项目讨论"))

    def test_rejects_empty_name(self):
        self.assertFalse(lk.is_project_chat(""))
        self.assertFalse(lk.is_project_chat(None))

    def test_rejects_glyph_in_the_middle(self):
        """符号必须在开头，正文里出现不算。"""
        self.assertFalse(lk.is_project_chat("讨论 🔴 的问题"))


class ChatRenamerDebounceTests(unittest.TestCase):
    """改名节流：状态稳住了才改，别刷屏。

    实测每次改群名都在群里留一条「XXX 修改群名为…」的系统消息，
    而 relay 每 2 秒推一次状态。不节流的话 working⇄idle 抖几下
    就刷一屏系统消息，比原来浪费群名宽度的问题严重得多。

    decide() 传入 now，不读时钟——测试不需要 sleep 或 mock。
    """

    def test_first_sight_waits_for_debounce(self):
        """刚看到一个状态先不动，可能只是抖动。"""
        r = lk.ChatRenamer()
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=0))

    def test_stable_past_debounce_renames(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(
            r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")

    def test_just_under_debounce_holds(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=29))

    def test_flapping_never_renames(self):
        """working→idle→working 在防抖窗口内反复，全程不改名。"""
        r = lk.ChatRenamer()
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=0))
        self.assertIsNone(r.decide("oc_1", "⚪️ x", "idle", now=10))
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=20))
        self.assertIsNone(r.decide("oc_1", "⚪️ x", "idle", now=25))

    def test_flapping_then_settling_renames(self):
        """抖完了稳住 30s，还是要改的。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        r.decide("oc_1", "⚪️ x", "idle", now=10)
        self.assertEqual(
            r.decide("oc_1", "⚪️ x", "idle", now=45), "⚪️ x")

    def test_idempotent_when_name_already_correct(self):
        """群名已经对了就不要再调 API——那会白刷一条系统消息。"""
        r = lk.ChatRenamer(known_names={"oc_1": "🟡 x"})
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=100))

    def test_renaming_updates_known_name(self):
        """改过一次之后，同样的目标名不该再改第二次。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=200))

    def test_chats_are_independent(self):
        """一个群的防抖不该影响另一个群。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertIsNone(r.decide("oc_2", "🟡 y", "working", now=31))
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")

    def test_baseline_from_startup_avoids_pointless_rename(self):
        """启动时拉一次群名当基线，省掉每次重启的整轮无谓改名。"""
        r = lk.ChatRenamer(known_names={"oc_1": "🟢 x"})
        r.decide("oc_1", "🟢 x", "done", now=0)
        self.assertIsNone(r.decide("oc_1", "🟢 x", "done", now=999))

    def test_empty_baseline_degrades_gracefully(self):
        """拉群名失败就空基线：每群多改一次名，不阻断启动。"""
        r = lk.ChatRenamer(known_names={})
        r.decide("oc_1", "🟢 x", "done", now=0)
        self.assertEqual(r.decide("oc_1", "🟢 x", "done", now=31), "🟢 x")


class ChatRenamerBlockedAndIntervalTests(unittest.TestCase):
    """blocked 立即改名，其余守最小间隔。

    blocked 是唯一「要人立刻动手」的状态，等 30s 防抖没有意义——
    等的这半分钟正是最该看见它的时候。
    """

    def test_blocked_renames_immediately(self):
        r = lk.ChatRenamer()
        self.assertEqual(
            r.decide("oc_1", "🔴 x", "blocked", now=0), "🔴 x")

    def test_blocked_beats_min_interval(self):
        """刚改过名也照样立即改——例外优先于最小间隔。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        self.assertEqual(
            r.decide("oc_1", "🔴 x", "blocked", now=32), "🔴 x")

    def test_blocked_still_idempotent(self):
        """已经是 blocked 名字了就别再改。"""
        r = lk.ChatRenamer(known_names={"oc_1": "🔴 x"})
        self.assertIsNone(r.decide("oc_1", "🔴 x", "blocked", now=0))

    def test_leaving_blocked_uses_debounce(self):
        """离开 blocked 走正常防抖，不必抢时间。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🔴 x", "blocked", now=0)
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=1))
        self.assertIsNone(r.decide("oc_1", "🟡 x", "working", now=20))

    def test_min_interval_holds_non_blocked(self):
        """两次改名间隔不足 60s，非 blocked 的一律按住。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        r.decide("oc_1", "🟢 x", "done", now=40)
        self.assertIsNone(r.decide("oc_1", "🟢 x", "done", now=71))

    def test_rename_allowed_after_min_interval(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        self.assertEqual(r.decide("oc_1", "🟡 x", "working", now=31), "🟡 x")
        r.decide("oc_1", "🟢 x", "done", now=40)
        self.assertEqual(r.decide("oc_1", "🟢 x", "done", now=95), "🟢 x")

    def test_min_interval_is_per_chat(self):
        r = lk.ChatRenamer()
        r.decide("oc_1", "🟡 x", "working", now=0)
        r.decide("oc_1", "🟡 x", "working", now=31)
        r.decide("oc_2", "🟡 y", "working", now=32)
        self.assertEqual(r.decide("oc_2", "🟡 y", "working", now=63), "🟡 y")

    def test_forget_clears_interval_state(self):
        """群解散后再出现的同 id，不该被旧的间隔按住。"""
        r = lk.ChatRenamer()
        r.decide("oc_1", "🔴 x", "blocked", now=0)
        r.forget("oc_1")
        self.assertEqual(
            r.decide("oc_1", "🔴 x", "blocked", now=1), "🔴 x")


class ChatTitleDisambiguationTests(unittest.TestCase):
    """两个群绑同名 agent 时，群名不能一模一样——会话列表里切都切不对。"""

    def test_plain_title_without_marker(self):
        self.assertEqual(lk.chat_title_for("tailcale", status="idle"),
                         "⚪️ tailcale")

    def test_marker_included_when_given(self):
        title = lk.chat_title_for("yqg-dw-datapilot", " [w22]")
        self.assertIn("w22", title)
        self.assertIn("yqg-dw-datapilot", title)

    def test_two_markers_produce_distinct_titles(self):
        a = lk.chat_title_for("same", " [w1B]")
        b = lk.chat_title_for("same", " [w22]")
        self.assertNotEqual(a, b)

    def test_still_within_length_limit(self):
        title = lk.chat_title_for("p" * 200, " [w99]")
        self.assertLessEqual(len(title), 60)

    def test_marker_survives_truncation(self):
        """项目名太长时，宁可截项目名也要保住区分标记。"""
        title = lk.chat_title_for("p" * 200, " [w99]")
        self.assertIn("w99", title)

    def test_marker_comes_before_project(self):
        """会话列表宽度有限，尾部会被截掉——标记必须在前面才看得见。"""
        title = lk.chat_title_for("some-project", " [w22]")
        self.assertLess(title.index("w22"), title.index("some-project"))

    def test_marker_visible_in_narrow_prefix(self):
        """只看前 20 个字符也要能分清两个群。"""
        a = lk.chat_title_for("very-long-project-name-here", " [w1B]")[:20]
        b = lk.chat_title_for("very-long-project-name-here", " [w22]")[:20]
        self.assertNotEqual(a, b)

    def test_empty_marker_behaves_like_none(self):
        self.assertEqual(lk.chat_title_for("x", ""), lk.chat_title_for("x"))


class SetActiveTitleTests(unittest.TestCase):
    def test_rename_uses_disambiguated_title(self):
        """绑定重名 agent 时，群名要带上区分标记。"""
        api = unittest.mock.MagicMock()
        api.bot_open_id = "ou_bot"
        bot = lk.LarkBot(api, "oc_1", loop=None)
        bot.agents = dup_agents()
        bot.set_active("oc_1", "w22:p1", "yqg-dw-datapilot")
        called = api.set_chat_name.call_args[0][1]
        self.assertIn("w22", called)

    def test_unique_agent_gets_plain_title(self):
        api = unittest.mock.MagicMock()
        api.bot_open_id = "ou_bot"
        bot = lk.LarkBot(api, "oc_1", loop=None)
        bot.agents = dup_agents()
        bot.set_active("oc_1", "w30:p1", "unique-one")
        called = api.set_chat_name.call_args[0][1]
        self.assertNotIn("[", called)


class StreamBodyTests(unittest.TestCase):
    """流式卡片的内容要稳，不能每帧整体平移。

    原来按字符截末尾 N 个：内容一长，每多一个字所有文字就往上挪一格，
    看着像在抖；开头还常是半个单词。
    """

    def test_cuts_on_line_boundary(self):
        body = lk.stream_body("完整第一行\n第二行\n第三行", max_lines=2)
        self.assertFalse(body.startswith("整"))
        self.assertTrue(body.startswith("第二行"))

    def test_keeps_tail_lines(self):
        body = lk.stream_body("\n".join(f"行{i}" for i in range(10)), max_lines=3)
        self.assertIn("行9", body)
        self.assertNotIn("行5", body)

    def test_short_content_untouched(self):
        self.assertEqual(lk.stream_body("只有一行", max_lines=5), "只有一行")

    def test_first_line_stable_while_appending(self):
        """还没到行数上限时，追加内容不该让首行变。"""
        a = lk.stream_body("行1\n行2", max_lines=5)
        b = lk.stream_body("行1\n行2\n行3", max_lines=5)
        self.assertEqual(a.splitlines()[0], b.splitlines()[0])

    def test_scrolls_by_whole_lines(self):
        """超过上限后按整行滚动，而不是按字符平移。"""
        a = lk.stream_body("\n".join(f"行{i}" for i in range(5)), max_lines=3)
        b = lk.stream_body("\n".join(f"行{i}" for i in range(6)), max_lines=3)
        self.assertEqual(a.splitlines()[1], b.splitlines()[0])

    def test_empty_is_safe(self):
        self.assertEqual(lk.stream_body("", max_lines=3), "(无输出)")

    def test_respects_char_ceiling(self):
        """行数没超但单行超长时，仍要有字符上限兜底。"""
        body = lk.stream_body("x" * 9000, max_lines=50)
        self.assertLessEqual(len(body), lk.STREAM_BODY_LIMIT + 20)


class TransientReadTests(unittest.TestCase):
    """relay 偶发读超时会返回占位串；直接推上去卡片会突然清空。"""

    def test_no_response_is_transient(self):
        self.assertTrue(lk.is_transient_read("(no response)"))

    def test_error_reading_is_transient(self):
        self.assertTrue(lk.is_transient_read("(error reading pane: timeout)"))

    def test_empty_is_transient(self):
        self.assertTrue(lk.is_transient_read(""))

    def test_real_content_is_not(self):
        self.assertFalse(lk.is_transient_read("⏺ 正在编译\n  ⎿ done"))

    def test_no_output_placeholder_is_not_transient(self):
        """agent 真的没输出时是有效状态，不能当成故障跳过。"""
        self.assertFalse(lk.is_transient_read("(无输出)"))


class SpacesReuseByBindingTests(unittest.TestCase):
    """/spaces 靠绑定关系复用群，不靠群名。

    群名带上会变的状态符号后，按名字精确匹配必然失配——agent 从
    working 变 done、群名从 🟡 x 变 🟢 x，/spaces 就认不出这个群，
    会给同一个 agent 重复建群。

    绑定表（lark_bindings.json 的 {chat_id: pane_id}）才是事实源。
    """

    def _agent(self, pane_id, project):
        return {"pane_id": pane_id, "project": project,
                "agent": "claude", "status": "working"}

    def test_reuses_chat_bound_to_pane(self):
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={"oc_a": "w1:p1"},
            authorized={"oc_a"})
        self.assertEqual(plan[0]["chat_id"], "oc_a")

    def test_creates_when_no_binding(self):
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={}, authorized=set())
        self.assertEqual(plan[0]["chat_id"], "")

    def test_creates_when_bound_chat_left_authorized_set(self):
        """群被解散了，绑定还在——得当成没群，重新建。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={"oc_gone": "w1:p1"},
            authorized={"oc_other"})
        self.assertEqual(plan[0]["chat_id"], "")

    def test_each_agent_gets_its_own_chat(self):
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "a"), self._agent("w2:p1", "b")],
            bindings={"oc_a": "w1:p1", "oc_b": "w2:p1"},
            authorized={"oc_a", "oc_b"})
        got = {p["pane_id"]: p["chat_id"] for p in plan}
        self.assertEqual(got, {"w1:p1": "oc_a", "w2:p1": "oc_b"})

    def test_plan_titles_carry_status_glyph(self):
        """建群时就带上当时的状态符号。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "datapilot")],
            bindings={}, authorized=set())
        self.assertTrue(plan[0]["title"].startswith("🟡 "), plan[0]["title"])

    def test_duplicate_projects_still_disambiguated(self):
        """同名 agent 的标记逻辑没被破坏。"""
        plan = lk.plan_chat_provisioning(
            [self._agent("w1:p1", "same"), self._agent("w2:p1", "same")],
            bindings={}, authorized=set())
        self.assertNotEqual(plan[0]["title"], plan[1]["title"])


class RenameOnStatusChangeTests(unittest.TestCase):
    """状态变了就（按节流规则）改群名。"""

    def _bot_with_binding(self):
        bot = make_bot()
        bot.chat_ids = {"oc_1"}
        bot._active = {"oc_1": "w1:p1"}
        bot.api.set_chat_name = unittest.mock.Mock()
        return bot

    def test_blocked_triggers_immediate_rename(self):
        bot = self._bot_with_binding()
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w1:p1", "project": "datapilot",
              "status": "blocked"}],
            now=0)
        bot.api.set_chat_name.assert_called_once()
        args = bot.api.set_chat_name.call_args[0]
        self.assertEqual(args[0], "oc_1")
        self.assertTrue(args[1].startswith("🔴 "), args[1])

    def test_working_waits_for_debounce(self):
        bot = self._bot_with_binding()
        agents = [{"pane_id": "w1:p1", "project": "datapilot",
                   "status": "working"}]
        lk._sync_chat_names(bot, agents, now=0)
        bot.api.set_chat_name.assert_not_called()
        lk._sync_chat_names(bot, agents, now=31)
        bot.api.set_chat_name.assert_called_once()

    def test_unbound_chat_not_renamed(self):
        """没绑 agent 的群不该被改名。"""
        bot = self._bot_with_binding()
        bot._active = {}
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w1:p1", "project": "x", "status": "blocked"}],
            now=0)
        bot.api.set_chat_name.assert_not_called()

    def test_rename_failure_does_not_raise(self):
        """改名失败不能影响正事——它只是展示。"""
        bot = self._bot_with_binding()
        bot.api.set_chat_name.side_effect = RuntimeError("改群名失败: boom")
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w1:p1", "project": "x", "status": "blocked"}],
            now=0)  # 不抛异常即通过

    def test_agent_without_matching_chat_is_skipped(self):
        bot = self._bot_with_binding()
        lk._sync_chat_names(
            bot,
            [{"pane_id": "w9:p9", "project": "other",
              "status": "blocked"}],
            now=0)
        bot.api.set_chat_name.assert_not_called()


class ProvisionGroupTests(unittest.TestCase):
    """一键为每个 agent 拉一个群；已绑的复用，不重复建。"""

    def test_plan_creates_for_missing_only(self):
        agents = [
            {"pane_id": "w1:p1", "project": "a", "agent": "claude",
             "status": "idle", "cwd": "/a", "host": "local"},
            {"pane_id": "w2:p1", "project": "b", "agent": "claude",
             "status": "idle", "cwd": "/b", "host": "local"},
        ]
        bindings = {"oc_a": "w1:p1"}
        authorized = {"oc_a"}
        plan = lk.plan_chat_provisioning(agents, bindings, authorized)
        reuse = [p for p in plan if p["chat_id"]]
        create = [p for p in plan if not p["chat_id"]]
        self.assertEqual(len(reuse), 1)
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["project"], "b")

    def test_plan_includes_marker_for_duplicates(self):
        plan = lk.plan_chat_provisioning(dup_agents(), {}, set())
        titles = [p["title"] for p in plan if p["project"] == "yqg-dw-datapilot"]
        self.assertEqual(len(set(titles)), 2)

    def test_plan_covers_every_agent(self):
        plan = lk.plan_chat_provisioning(dup_agents(), {}, set())
        self.assertEqual(len(plan), len(dup_agents()))


class DuplicateChatNameTests(unittest.TestCase):
    """群名可能重复（改名后撞上），复用判定不能认错。"""

    def test_prefers_already_bound_chat(self):
        """同名群里优先选已经绑了这个 pane 的那个。"""
        candidates = {"herdr · x": ["oc_1", "oc_2"]}
        bound = {"oc_2": "w9:p1"}
        self.assertEqual(
            lk.pick_chat_for_pane(candidates.get("herdr · x", []), "w9:p1", bound),
            "oc_2")

    def test_falls_back_to_first(self):
        self.assertEqual(
            lk.pick_chat_for_pane(["oc_1", "oc_2"], "w9:p1", {}), "oc_1")

    def test_empty_yields_none(self):
        self.assertIsNone(lk.pick_chat_for_pane([], "w9:p1", {}))


class HelpRegistryTests(unittest.TestCase):
    """命令表是单一数据源：加命令必须同时写帮助，否则测试挂。

    这是为了防止 /help 随版本腐烂——之前它只显示状态，加了 8 个命令
    一个都没体现。
    """

    def test_commands_derived_from_registry(self):
        self.assertEqual(lk.COMMANDS, {c["name"] for c in lk.COMMAND_HELP})

    def test_every_command_has_a_description(self):
        for entry in lk.COMMAND_HELP:
            self.assertTrue(entry.get("desc"), f"{entry['name']} 缺描述")

    def test_every_handled_command_is_documented(self):
        """代码里真正处理了的命令，都得在帮助里。"""
        source = (pathlib.Path(lk.__file__).read_text()
                  if hasattr(lk, "__file__") else "")
        for entry in lk.COMMAND_HELP:
            self.assertIn(entry["name"], lk.COMMANDS)

    def test_help_lists_every_non_alias_command(self):
        """非别名的命令一个都不能漏——这是防帮助腐烂的关键断言。"""
        out = lk.format_help()
        for entry in lk.COMMAND_HELP:
            if entry["group"]:
                self.assertIn(f"/{entry['name']}", out,
                              f"{entry['name']} 没出现在帮助里")

    def test_aliases_are_not_listed(self):
        """别名不单列，省地方。"""
        out = lk.format_help()
        aliases = [e["name"] for e in lk.COMMAND_HELP if not e["group"]]
        self.assertTrue(aliases, "应当至少有一个别名")
        for name in aliases:
            self.assertNotIn(f"/{name} ", out)

    def test_help_is_short(self):
        """手机上看的，太长就滑不动了。"""
        out = lk.format_help()
        self.assertLess(len(out.splitlines()), 30)
        self.assertLess(len(out), 900)

    def test_help_groups_are_labelled(self):
        out = lk.format_help()
        self.assertIn("看", out)

    def test_no_duplicate_names(self):
        names = [c["name"] for c in lk.COMMAND_HELP]
        self.assertEqual(len(names), len(set(names)))

    def test_args_shown_when_present(self):
        out = lk.format_help()
        self.assertIn("<序号>", out)


class ImagePathTests(unittest.TestCase):
    """agent 输出里提到的图片路径，直接发到飞书看，省得跑回电脑。"""

    def test_finds_png_path(self):
        found = lk.find_image_paths("截图存到 /tmp/shot.png 了")
        self.assertIn("/tmp/shot.png", found)

    def test_finds_multiple(self):
        found = lk.find_image_paths("/t/a.png 和 /x/b.jpg")
        self.assertEqual(len(found), 2)

    def test_relative_path_ignored(self):
        """相对路径无从定位——agent 的 cwd 和我们不一定一致。"""
        self.assertEqual(lk.find_image_paths("生成了 shot.png"), [])

    def test_ignores_non_image(self):
        self.assertEqual(lk.find_image_paths("改了 main.py 和 README.md"), [])

    def test_supports_common_formats(self):
        for ext in ("png", "jpg", "jpeg", "gif", "webp"):
            self.assertTrue(lk.find_image_paths(f"/t/x.{ext}"), ext)

    def test_dedupes(self):
        self.assertEqual(len(lk.find_image_paths("/a.png 又 /a.png")), 1)

    def test_caps_count(self):
        """一次别刷十几张图。"""
        text = " ".join(f"/t/{i}.png" for i in range(20))
        self.assertLessEqual(len(lk.find_image_paths(text)), lk.MAX_IMAGES_PER_MSG)

    def test_strips_trailing_punctuation(self):
        self.assertIn("/tmp/a.png", lk.find_image_paths("见 /tmp/a.png。"))


class ImageSafetyTests(unittest.TestCase):
    """别把任意文件当图片传上去。"""

    def test_rejects_missing_file(self):
        self.assertFalse(lk.is_sendable_image("/definitely/not/here.png"))

    def test_rejects_oversize(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "big.png")
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * (lk.MAX_IMAGE_BYTES + 10))
            self.assertFalse(lk.is_sendable_image(path))

    def test_accepts_real_png(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok.png")
            with open(path, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
            self.assertTrue(lk.is_sendable_image(path))

    def test_rejects_wrong_magic(self):
        """扩展名是 png 但内容不是——不传。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fake.png")
            with open(path, "w") as fh:
                fh.write("not an image at all")
            self.assertFalse(lk.is_sendable_image(path))

    def test_accepts_jpeg_magic(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok.jpg")
            with open(path, "wb") as fh:
                fh.write(b"\xff\xd8\xff" + b"0" * 100)
            self.assertTrue(lk.is_sendable_image(path))


class TestIsolationTests(unittest.TestCase):
    """单测不能写进真实配置——曾经把 oc_1 / oc_2 写进了 lark_bindings.json。"""

    def test_state_paths_are_temporary(self):
        self.assertNotIn(".config/herdr-remote", lk.BINDING_PATH)
        self.assertNotIn(".config/herdr-remote", lk.SEEN_PATH)

    def test_make_bot_does_not_touch_real_config(self):
        real = os.path.expanduser("~/.config/herdr-remote/lark_bindings.json")
        before = open(real).read() if os.path.exists(real) else None
        bot = make_bot()
        bot.set_active("oc_probe", "w99:p1")
        after = open(real).read() if os.path.exists(real) else None
        self.assertEqual(before, after, "单测污染了真实绑定文件")


class BindingHygieneTests(unittest.TestCase):
    """绑定表里不该留下已消失的群或 agent。"""

    def test_prunes_unknown_chats(self):
        kept = lk.prune_bindings(
            {"oc_live": "w1:p1", "oc_gone": "w2:p1"},
            known_chats={"oc_live"}, known_panes={"w1:p1", "w2:p1"})
        self.assertEqual(kept, {"oc_live": "w1:p1"})

    def test_prunes_dead_panes(self):
        kept = lk.prune_bindings(
            {"oc_live": "w1:p1", "oc_live2": "w9:p1"},
            known_chats={"oc_live", "oc_live2"}, known_panes={"w1:p1"})
        self.assertEqual(kept, {"oc_live": "w1:p1"})

    def test_keeps_valid_entries(self):
        table = {"oc_a": "w1:p1", "oc_b": "w2:p1"}
        self.assertEqual(
            lk.prune_bindings(table, {"oc_a", "oc_b"}, {"w1:p1", "w2:p1"}), table)

    def test_empty_known_sets_keep_everything(self):
        """还没拿到 agent 列表时别乱删。"""
        table = {"oc_a": "w1:p1"}
        self.assertEqual(lk.prune_bindings(table, set(), set()), table)


class ChatIdPersistenceTests(unittest.TestCase):
    """/spaces 建的群必须落盘，否则重启就丢，绑定还会被当成失效清掉。"""

    def test_new_chat_added_to_store(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "chats.json")
            store = lk.ChatIdStore(path)
            store.add("oc_new")
            self.assertIn("oc_new", lk.ChatIdStore(path).all())

    def test_seed_from_env_kept(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "chats.json")
            store = lk.ChatIdStore(path)
            store.seed({"oc_env"})
            self.assertIn("oc_env", lk.ChatIdStore(path).all())

    def test_corrupt_file_safe(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "chats.json")
            open(path, "w").write("{broken")
            self.assertEqual(lk.ChatIdStore(path).all(), set())

    def test_prune_keeps_chats_from_store(self):
        """群在存储里就不算消失——这是之前误删绑定的根因。"""
        kept = lk.prune_bindings(
            {"oc_spaces": "w1:p1"},
            known_chats={"oc_env", "oc_spaces"}, known_panes={"w1:p1"})
        self.assertIn("oc_spaces", kept)


class UnbindCommandTests(unittest.TestCase):
    """/unbind —— 解绑当前群；加 drop 连群一起解散。"""

    def test_parses_plain(self):
        self.assertEqual(lk.parse_unbind_args(""), (False,))

    def test_parses_drop(self):
        self.assertEqual(lk.parse_unbind_args("drop"), (True,))

    def test_parses_chinese(self):
        self.assertEqual(lk.parse_unbind_args("解散"), (True,))

    def test_unknown_arg_is_not_drop(self):
        """认不出来的一律当成只解绑——删群不可逆，宁可保守。"""
        self.assertEqual(lk.parse_unbind_args("banana"), (False,))

    def test_is_a_command(self):
        self.assertEqual(lk.parse_command("/unbind drop"), ("unbind", "drop"))

    def test_in_help(self):
        self.assertIn("/unbind", lk.format_help())


class BindingRemovalTests(unittest.TestCase):
    def test_remove_drops_entry(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.json")
            store = lk.BindingStore(path)
            store.set("oc_1", "w1:p1")
            store.remove("oc_1")
            self.assertIsNone(lk.BindingStore(path).get("oc_1"))

    def test_remove_missing_is_safe(self):
        with tempfile.TemporaryDirectory() as d:
            lk.BindingStore(os.path.join(d, "b.json")).remove("oc_none")

    def test_chat_store_remove(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            store = lk.ChatIdStore(path)
            store.add("oc_1")
            store.remove("oc_1")
            self.assertNotIn("oc_1", lk.ChatIdStore(path).all())


class SpacesNoAutoSendTests(unittest.TestCase):
    """/spaces 批量建群后不能自动绑定。

    实际事故：/spaces 建了群，用户在新群里打了个「1」，直接发进了一个
    他没选过的 agent 的终端。批量建群时用户并不知道哪个群绑了谁。
    """

    def test_pending_binding_is_not_active(self):
        """预绑定要放在待确认区，不能直接进 _active。"""
        bot = make_bot()
        bot.stage_binding("oc_new", "w1:p1")
        self.assertIsNone(bot.active_pane("oc_new"))

    def test_staged_binding_can_be_confirmed(self):
        bot = make_bot()
        bot.stage_binding("oc_new", "w1:p1")
        self.assertEqual(bot.confirm_staged("oc_new"), "w1:p1")
        self.assertEqual(bot.active_pane("oc_new"), "w1:p1")

    def test_confirm_unknown_yields_none(self):
        self.assertIsNone(make_bot().confirm_staged("oc_nope"))

    def test_staged_survives_until_confirmed(self):
        bot = make_bot()
        bot.stage_binding("oc_new", "w1:p1")
        self.assertEqual(bot.staged_pane("oc_new"), "w1:p1")

    def test_free_text_in_unbound_chat_is_refused(self):
        """没确认绑定的群里，随便打字不该发到任何 agent。"""
        bot = make_bot()
        bot.stage_binding("oc_new", "w1:p1")
        self.assertIsNone(bot.active_pane("oc_new"))


class DigitSafetyTests(unittest.TestCase):
    """纯数字只在 agent 真的在等选择时才当按键。"""

    def test_digit_without_pending_approval_is_text(self):
        """没有待审批时，「1」就是普通文本，不该当按键发。"""
        self.assertTrue(lk.looks_like_option_press("1"))
        # 真正的守卫在调用处：pane_id in approval_tokens
        self.assertNotIn("w1:p1", {})


def shell_pane(**kw):
    base = {"pane_id": "w1:p1", "agent": "shell", "status": "unknown",
            "project": "empty-space", "cwd": "/c/empty", "host": "local"}
    base.update(kw)
    return base


class AgentPresenceTests(unittest.TestCase):
    """有的 space 开了 agent，有的只是裸 shell —— 发消息前必须分清。

    实际事故：往裸 shell 发了个「1」，shell 报 command not found。
    """

    def test_shell_without_agent_detected(self):
        self.assertFalse(lk.has_live_agent(shell_pane()))

    def test_claude_pane_has_agent(self):
        self.assertTrue(lk.has_live_agent(make_agents(1)[0]))

    def test_shell_with_real_status_counts_as_live(self):
        """shell 但状态是 working —— 说明真在跑东西，不算空。"""
        self.assertTrue(lk.has_live_agent(shell_pane(status="working")))

    def test_missing_agent_field_is_not_live(self):
        self.assertFalse(lk.has_live_agent({"pane_id": "w1:p1", "status": "unknown"}))

    def test_empty_dict_safe(self):
        self.assertFalse(lk.has_live_agent({}))


class AgentPresenceDisplayTests(unittest.TestCase):
    def test_list_marks_shell_panes(self):
        agents = make_agents(1) + [shell_pane(pane_id="w9:p1")]
        out = lk.format_agent_list(agents)
        line = [l for l in out.splitlines() if "empty-space" in l][0]
        self.assertIn(lk.SHELL_ICON, line)

    def test_live_agents_keep_status_icon(self):
        line = [l for l in lk.format_agent_list(make_agents(1)).splitlines()
                if "project" in l][0]
        self.assertNotIn(lk.SHELL_ICON, line)

    def test_legend_explains_shell(self):
        self.assertIn(lk.SHELL_ICON, lk.format_agent_list(make_agents(1)))


class SendToShellGuardTests(unittest.TestCase):
    """往裸 shell 发文本要提醒，不能默默发进去变成 command not found。"""

    def test_warns_for_shell_pane(self):
        self.assertTrue(lk.should_warn_shell(shell_pane()))

    def test_no_warning_for_live_agent(self):
        self.assertFalse(lk.should_warn_shell(make_agents(1)[0]))

    def test_hint_names_the_fix(self):
        hint = lk.shell_hint("empty-space")
        self.assertIn("empty-space", hint)
        self.assertIn("/new", hint)


class HealthReportTests(unittest.TestCase):
    """/health —— 一条命令看清全貌，现在排查要翻日志。"""

    def _report(self, **kw):
        base = dict(relay_connected=True, relay_url="ws://127.0.0.1:8375",
                    agents=3, live_agents=2, chats=2, bindings=1, staged=0,
                    queued=0, watchers=0, seen=10, render="card",
                    autowatch=True, autowatch_limit=120)
        base.update(kw)
        return lk.format_health(**base)

    def test_shows_relay_state(self):
        self.assertIn("relay", self._report().lower())

    def test_flags_disconnected_relay(self):
        out = self._report(relay_connected=False)
        self.assertIn("✗", out)

    def test_healthy_shows_ok_marks(self):
        self.assertIn("✓", self._report())

    def test_counts_agents_and_live(self):
        out = self._report(agents=16, live_agents=12)
        self.assertIn("16", out)
        self.assertIn("12", out)

    def test_shows_queue_depth(self):
        """连发多条时得知道排到第几个。"""
        self.assertIn("3", self._report(queued=3))

    def test_shows_watchers(self):
        self.assertIn("2", self._report(watchers=2))

    def test_shows_staged_bindings(self):
        self.assertIn("5", self._report(staged=5))

    def test_url_is_scrubbed(self):
        """relay URL 带 token 时不能原样显示。"""
        out = self._report(relay_url="ws://x:8375?token=SECRET")
        self.assertNotIn("SECRET", out)

    def test_is_short(self):
        self.assertLess(len(self._report().splitlines()), 16)

    def test_in_help(self):
        self.assertIn("/health", lk.format_help())


class QueueDepthTests(unittest.TestCase):
    def test_empty_queue_is_zero(self):
        self.assertEqual(lk.ChatQueue().depth(), 0)

    def test_counts_pending_items(self):
        async def main():
            q = lk.ChatQueue()
            q.submit("oc_1", lambda: asyncio.sleep(0.05))
            q.submit("oc_1", lambda: asyncio.sleep(0))
            depth = q.depth()
            await q.drain()
            return depth
        self.assertGreaterEqual(asyncio.run(main()), 1)


class AuditRecordTests(unittest.TestCase):
    """审计回执：记录谁在什么时候对哪个 agent 做了什么。"""

    def test_formats_send(self):
        line = lk.format_audit("send", "tailcale", "w1:p1", "继续改这个函数")
        self.assertIn("tailcale", line)
        self.assertIn("继续改这个函数", line)

    def test_includes_action(self):
        self.assertIn("approve", lk.format_audit("approve", "p", "w1:p1", "2"))

    def test_truncates_long_detail(self):
        line = lk.format_audit("send", "p", "w1:p1", "x" * 2000)
        self.assertLess(len(line), 400)

    def test_empty_detail_safe(self):
        self.assertTrue(lk.format_audit("interrupt", "p", "w1:p1", ""))

    def test_scrubs_secrets(self):
        """指令里可能带 token，群里不能原样记。"""
        with unittest.mock.patch.object(lk, "_RELAY_TOKEN", "tok-secret"):
            line = lk.format_audit("send", "p", "w1:p1", "用 tok-secret 登录")
            self.assertNotIn("tok-secret", line)


class AuditRoutingTests(unittest.TestCase):
    """审计回执落在发起操作的那个群，而不是某个单独的审计群。"""

    def test_posts_into_originating_chat(self):
        bot = make_bot()
        bot.audit("oc_work", "send", {"project": "tailcale", "pane_id": "w1:p1"}, "改一下")
        bot.api.send_text.assert_called_once()
        chat_id, line = bot.api.send_text.call_args[0]
        self.assertEqual(chat_id, "oc_work")
        self.assertIn("tailcale", line)

    def test_does_not_fan_out_to_other_chats(self):
        """A 群的操作不该出现在 B 群——各群只看自己的痕迹。"""
        bot = make_bot()
        bot.chat_ids = {"oc_a", "oc_b"}
        bot.audit("oc_a", "send", {"project": "p", "pane_id": "w1:p1"}, "x")
        targets = [c.args[0] for c in bot.api.send_text.call_args_list]
        self.assertEqual(targets, ["oc_a"])

    def test_disabled_sends_nothing(self):
        bot = make_bot()
        bot.audit_on = False
        bot.audit("oc_work", "send", {"project": "p", "pane_id": "w1:p1"}, "x")
        bot.api.send_text.assert_not_called()

    def test_send_failure_does_not_raise(self):
        """审计发不出去也不能连带把主流程搞挂。"""
        bot = make_bot()
        bot.api.send_text.side_effect = RuntimeError("boom")
        bot.audit("oc_work", "trust", {"project": "p", "pane_id": "w1:p1"})

    def test_audit_switch_defaults_on(self):
        self.assertTrue(lk.audit_enabled("on"))
        self.assertTrue(lk.audit_enabled(""))
        self.assertTrue(lk.audit_enabled(None))

    def test_audit_switch_off(self):
        for value in ("off", "0", "false", "no", "OFF"):
            self.assertFalse(lk.audit_enabled(value), value)

    def test_every_chat_accepts_commands(self):
        """没有只读群了：所有授权群都能下指令。"""
        bot = make_bot()
        bot.chat_ids = {"oc_a", "oc_b"}
        for chat in ("oc_a", "oc_b"):
            self.assertTrue(lk.is_authorized_chat(chat, bot.chat_ids))


# 真实抓屏：Claude Code v2.1.239，一次 AskUserQuestion 问两组。
# 逐字复制自 `herdr agent read w2A:p1 --source visible`，别"整理"它——
# 这些行的顶格/缩进/尾行提示正是解析器踩过的坑。
REAL_MULTI_TAB_PANE = """\
 ▐▛███▛█   Claude Code v2.1.239
▝▜██████▀  Opus 5 (1M context) with high effort · Claude Team
  ▝▝ ▝▝    ~/code-github/dolphinscheduler-newversion


❯ 请调用 AskUserQuestion 工具，在一次调用里同时问我两个问题。
───────────────────────────────────────────────────────────────────────
←  ☐ 方案  ☐ Agent  ✔ Submit  →

要用哪种方案实现？

❯ 1. 直接改现有函数
     在现有函数内部直接修改实现
  2. 新增一层抽象
     引入新的抽象层，隔离改动
  3. 先写测试
     先补测试再动实现
  4. Type something.
───────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""


# 同一次调用的第二组：答完第一组后原地替换，tab 栏第一个变成 ☒。
REAL_SECOND_TAB_PANE = """\
❯ 请调用 AskUserQuestion 工具，在一次调用里同时问我两个问题。
───────────────────────────────────────────────────────────────────────
←  ☒ 方案  ☐ Agent  ✔ Submit  →

新 agent 用哪个？

❯ 1. 默认 claude
     使用默认 claude agent
  2. 只起 codex
     仅启动 codex agent
  3. 只开空 shell
     仅开一个空的 shell 环境
  4. Type something.
───────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""


class RealPaneSelectorTests(unittest.TestCase):
    """拿真实抓屏跑解析，而不是手写的理想样本。

    手写样本让两个互相矛盾的前提都"测过"了：detect_option_groups 假设多组
    同屏，current_option_group 假设逐组显示。真实 TUI 两者都不是——顶部
    tab 栏列出所有组，选项区只渲染当前那一组。
    """

    def test_real_pane_yields_a_group(self):
        """真实抓屏必须解析出选项。原来是 0 组，卡片上一个按钮都没有。"""
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTI_TAB_PANE))
        self.assertTrue(groups, "真实 pane 解析不出选项组")

    def test_hint_line_does_not_kill_selector(self):
        """`Enter to select · Tab/Arrow keys…` 是 TUI 提示，不是正文。

        它顶格且非边框字符，被当成"选择器已翻过去"的正文，整组被丢弃。
        """
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTI_TAB_PANE))
        self.assertEqual(len(groups), 1, "同屏只该有当前 tab 这一组")

    def test_only_real_options_kept(self):
        """`Type something.` / `Chat about this` 是 TUI 固定尾项。

        它们不是 agent 问你的内容，按下去会掉进自由输入框。
        """
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTI_TAB_PANE))
        self.assertEqual(groups[0]["options"],
                         ["直接改现有函数", "新增一层抽象", "先写测试"])

    def test_question_comes_from_the_tab(self):
        """提问行取选项上方那句，而不是 tab 栏或用户的输入回显。"""
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTI_TAB_PANE))
        self.assertEqual(groups[0]["question"], "要用哪种方案实现？")

    def test_tab_bar_is_not_an_option(self):
        """`←  ☐ 方案  ☐ Agent  ✔ Submit  →` 不含编号，不该混进选项。"""
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTI_TAB_PANE))
        joined = " ".join(groups[0]["options"])
        self.assertNotIn("Submit", joined)

    def test_second_tab_parses_too(self):
        """答完第一组后原地替换成第二组，同样要认得出。"""
        groups = lk.detect_option_groups(lk.clean_pane(REAL_SECOND_TAB_PANE))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["question"], "新 agent 用哪个？")
        self.assertEqual(groups[0]["options"],
                         ["默认 claude", "只起 codex", "只开空 shell"])

    def test_numbers_stay_aligned_with_screen(self):
        """按钮发的数字必须对得上屏幕上的编号。

        过滤尾项不能重排序号——过滤后仍是 1/2/3，与屏幕一致。
        """
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTI_TAB_PANE))
        card = lk.build_options_card("w2A:p1", "proj", groups[0]["options"],
                                     "abcde", question=groups[0]["question"])
        keys = [b["value"]["k"] for b in buttons_in(card)
                if b["value"].get("k")]
        self.assertEqual(keys, ["1", "2", "3"])


# 真实抓屏：AskUserQuestion 的多选框（allow multiple）。
# 与单选的差别：每项前面带 `[ ]` 复选标记，尾项写作 `Type something`（无句点），
# 且提示行用的是 `↑/↓ to navigate` 变体。
REAL_MULTISELECT_PANE = """\
要跑哪些检查？

❯ 1. [ ] 单测
  运行单元测试
  2. [ ] 静态扫描
  运行静态代码扫描
  3. [ ] 安全审计
  运行安全审计检查
  4. [ ] Type something
     Submit
───────────────────────────────────────────────────────────────────────
  5. Chat about this

Enter to select · ↑/↓ to navigate · Esc to cancel"""


class MultiSelectFormTests(unittest.TestCase):
    """多选框（allow multiple）的真实抓屏。

    单选那套抓屏盖不住它：选项带 `[ ]` 复选标记，尾项没有句点。
    """

    def test_parses(self):
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTISELECT_PANE))
        self.assertEqual(len(groups), 1)

    def test_checkbox_marker_stripped(self):
        """`[ ]` 是复选状态标记，不是选项文字的一部分。

        留着它，按钮上会显示「1. [ ] 单测」，还白占按钮宽度。
        """
        groups = lk.detect_option_groups(lk.clean_pane(REAL_MULTISELECT_PANE))
        self.assertEqual(groups[0]["options"], ["单测", "静态扫描", "安全审计"])

    def test_checked_marker_also_stripped(self):
        """已选中的是 `[x]`／`[✓]`，同样要摘掉。"""
        pane = ("选哪些？\n❯ 1. [x] 甲\n  2. [✓] 乙\n  3. [ ] 丙\n"
                "\nEnter to select · ↑/↓ to navigate · Esc to cancel")
        groups = lk.detect_option_groups(lk.clean_pane(pane))
        self.assertEqual(groups[0]["options"], ["甲", "乙", "丙"])

    def test_tail_item_without_period_dropped(self):
        """多选框的尾项写作 `Type something`，没有句点。"""
        self.assertTrue(lk.is_tui_tail_option("[ ] Type something"))

    def test_bracketed_tail_item_dropped(self):
        """带复选标记的尾项也要滤掉——摘标记与滤尾项的顺序不能反。"""
        for raw in ("[ ] Type something", "[ ] Chat about this",
                    "[x] Type something."):
            with self.subTest(raw=raw):
                self.assertTrue(lk.is_tui_tail_option(raw), raw)

    def test_real_option_with_bracket_kept(self):
        """别误伤：正文里本来就可能有方括号。"""
        self.assertFalse(lk.is_tui_tail_option("[ ] 单测"))
        self.assertFalse(lk.is_tui_tail_option("修 [bug] 再发版"))


# 真实场景（见飞书截图）：agent 停在 AskUserQuestion 选择器上，relay 却因为
# 不认得这种提示而不给 options，卡片退化成了通用的 Yes/Trust/No。
BLOCKED_WITH_REAL_SELECTOR = """\
当前状态和你以为的不一样：原本 15 个项目群现在只剩 3 个。怎么办？

❯ 1. 先停下，我去确认
     不动。你先弄清那个并行会话在做什么。
  2. 继续建群
     按原计划把缺的群补回来。
  3. 先看日志
     把 /unbind drop 那几条日志翻出来。
  4. Type something.
  5. Chat about this

Enter to select · Tab/Arrow keys to navigate · Esc to cancel"""


# 长正文 + 末尾选择器：truncate_prompt 的额度会被正文吃光，
# 中间那段 `⋯ 省略 N 字 ⋯` 正好盖住问题和选项（见飞书截图）。
LONG_PANE_WITH_SELECTOR = (
    "Ran 1 shell command\n"
    + "\n".join(f"输出第 {i} 行，这里是一大段无关的正文，用来把额度撑爆。"
                for i in range(30))
    + "\n\n当前状态和你以为的不一样，怎么办？\n\n"
      "\u276f 1. 先停下，我去确认\n"
      "     不动。你先弄清那个并行会话在做什么。\n"
      "  2. 继续建群\n"
      "  3. 先看日志\n\n"
      "Enter to select \u00b7 Tab/Arrow keys to navigate \u00b7 Esc to cancel"
)


class BlockedCardReadabilityTests(unittest.TestCase):
    """卡片上要看得清「在问什么」。

    问题出在两处叠加：选项既进了 ``` 代码块又进了按钮，重复一遍；而正文
    一长，truncate_prompt 只留首尾各约 190 字，中间的问题和选项被
    `⋯ 省略 N 字 ⋯` 整段吃掉——屏幕上问的是什么反而看不见了。
    """

    def _text_blocks(self, card):
        return [el["text"]["content"] for el in card["elements"]
                if el.get("tag") == "div"]

    def _labels(self, card):
        """选项文字现在列在正文，按钮只放序号——断言得看正文。"""
        return option_lines_in(card)

    def test_question_rendered_as_its_own_block(self):
        """问题行要单独成块，而不是只埋在代码块里等着被截断。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", LONG_PANE_WITH_SELECTOR, None, "abcde")
        blocks = self._text_blocks(card)
        self.assertTrue(
            any("当前状态和你以为的不一样" in b and not b.startswith("```")
                for b in blocks),
            f"问题行没有单独渲染：{blocks}")

    def test_selector_stripped_from_code_block(self):
        """选项已经是按钮了，代码块里不该再重复一遍占额度。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", LONG_PANE_WITH_SELECTOR, None, "abcde")
        code = next(b for b in self._text_blocks(card) if b.startswith("```"))
        self.assertNotIn("1. 先停下", code)
        self.assertNotIn("Enter to select", code)

    def test_question_survives_long_output(self):
        """正文再长，问题也不能被 `省略 N 字` 吃掉。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", LONG_PANE_WITH_SELECTOR, None, "abcde")
        whole = " ".join(self._text_blocks(card))
        self.assertIn("当前状态和你以为的不一样", whole)

    def test_options_still_buttons(self):
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", LONG_PANE_WITH_SELECTOR, None, "abcde")
        labels = " ".join(self._labels(card))
        self.assertIn("先停下", labels)
        self.assertIn("继续建群", labels)

    def test_prose_before_selector_kept(self):
        """选择器之前的正文是判断依据，要留着。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", LONG_PANE_WITH_SELECTOR, None, "abcde")
        code = next(b for b in self._text_blocks(card) if b.startswith("```"))
        self.assertIn("Ran 1 shell command", code)

    def test_no_selector_leaves_prompt_alone(self):
        """没有选择器时，代码块照旧是整段 prompt。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", "Bash(rm -rf build)\nProceed?",
            lk.TOOL_OPTIONS, "abcde")
        code = next(b for b in self._text_blocks(card) if b.startswith("```"))
        self.assertIn("rm -rf build", code)


class StripSelectorTests(unittest.TestCase):
    """strip_selector：把末尾的选择器区间从正文里摘掉。"""

    def test_removes_options_and_hint(self):
        out = lk.strip_selector(LONG_PANE_WITH_SELECTOR)
        self.assertNotIn("1. 先停下", out)
        self.assertNotIn("Enter to select", out)

    def test_keeps_prose(self):
        out = lk.strip_selector(LONG_PANE_WITH_SELECTOR)
        self.assertIn("Ran 1 shell command", out)

    def test_no_selector_is_unchanged(self):
        text = "just some output\nsecond line"
        self.assertEqual(lk.strip_selector(text), text)

    def test_empty_is_safe(self):
        self.assertEqual(lk.strip_selector(""), "")


# AskUserQuestion 多选提交后的 Review 页（实测抓屏，见 observer 日志）。
# 它自己也是个编号选择器，不拦住就会被当成「下一组问题」推成一张卡片。
REVIEW_PAGE = """\
Ready to submit your answers?

\u276f 1. Submit answers
  2. Cancel

Enter to select · Esc to cancel"""


class SingleSelectSkipsRefreshTests(unittest.TestCase):
    """单选点完就该收尾，不走多选刷新。

    单选已经补 Enter 提交了，再去读屏找多选框有两个坏处：白等一次读屏；
    更糟的是屏幕上若恰好出现下一道多选题，会被误当成「这一题还在勾」而
    重推卡片，人以为刚才那次没生效。
    """

    def test_refresh_only_for_multiselect(self):
        import inspect
        source = inspect.getsource(lk.LarkBot._approve)
        # 守卫必须在 _refresh_multiselect 调用之前
        guard = source.index("multiselect and await self._refresh_multiselect")
        self.assertGreater(guard, 0)


class MultiselectFlagOnButtonsTests(unittest.TestCase):
    """按钮 value 要带上「这是多选」的标记。

    _approve 得在**发键之前**知道该不该补 Enter，不能等发完再读屏判断：
    单选补 Enter 才提交，多选补了就把没勾完的答案交出去了。卡片是什么形态
    在渲染时就已经确定，编进 value 最可靠。
    """

    def _option_values(self, card):
        out = []
        for el in card["elements"]:
            if el.get("tag") == "action":
                for b in el["actions"]:
                    if b["value"].get("k"):
                        out.append(b["value"])
        return out

    def test_multiselect_buttons_flagged(self):
        card = lk.build_option_card(
            "p1", "proj", ["单测", "静态扫描"], "gen",
            multiselect=True, checked=[False, False])
        for value in self._option_values(card):
            self.assertEqual(value.get("m"), 1, f"缺多选标记: {value}")

    def test_single_select_buttons_not_flagged(self):
        card = lk.build_option_card("p1", "proj", ["是", "否"], "gen")
        for value in self._option_values(card):
            self.assertIsNone(value.get("m"), f"单选不该带多选标记: {value}")


class OptionListInBodyTests(unittest.TestCase):
    """选项全文列在正文，按钮只放序号。

    长选项塞进按钮会在手机上折成好几行，一排选项堆起来很难扫。拆开之后
    正文负责「看清楚」，按钮负责「点得准」，各司其职。
    """

    LONG = ("直接修改现有的 normalizePlanStatus 函数把 PRUNED 分支补进去"
            "但要注意存量调用方的兼容性问题")

    def _card(self, options, **kw):
        return lk.build_option_card("p1", "proj", options, "gen", **kw)

    def _labels(self, card):
        return [b["text"]["content"]
                for el in card["elements"] if el.get("tag") == "action"
                for b in el["actions"] if b["value"].get("k")]

    def _body(self, card):
        return "\n".join(el["text"]["content"] for el in card["elements"]
                         if el.get("tag") == "div")

    def test_full_text_in_body(self):
        """正文要有完整选项文字，一个字都不能少。"""
        body = self._body(self._card([self.LONG, "短的"]))
        self.assertIn(self.LONG, body)

    def test_buttons_are_numbers_only(self):
        labels = self._labels(self._card([self.LONG, "短的"]))
        self.assertEqual(labels, ["1", "2"])

    def test_body_numbers_match_buttons(self):
        """正文的编号要和按钮对得上，否则人对照不了。"""
        card = self._card(["甲", "乙", "丙"])
        body = self._body(card)
        for i in ("1", "2", "3"):
            self.assertIn(f"{i}.", body)
        self.assertEqual(self._labels(card), ["1", "2", "3"])

    def test_multiselect_shows_checkmarks_in_body(self):
        """多选的勾选态要在正文看得见——按钮只剩序号，放不下标记了。"""
        card = self._card(["甲", "乙"], multiselect=True, checked=[True, False])
        body = self._body(card)
        self.assertIn("✔", body)
        self.assertIn("☐", body)

    def test_multiselect_buttons_still_numbers(self):
        card = self._card(["甲", "乙"], multiselect=True, checked=[True, False])
        self.assertEqual(self._labels(card), ["1", "2"])

    def test_multiselect_flag_survives(self):
        """按钮变短了，但多选标记不能丢——_approve 靠它决定补不补 Enter。"""
        card = self._card(["甲", "乙"], multiselect=True, checked=[False, False])
        values = [b["value"] for el in card["elements"]
                  if el.get("tag") == "action"
                  for b in el["actions"] if b["value"].get("k")]
        for value in values:
            self.assertEqual(value.get("m"), 1)

    def test_overlong_option_still_truncated_in_body(self):
        """正文也不能无限长，超长仍要截并留省略号。"""
        huge = "很长" * 400
        body = self._body(self._card([huge, "短的"]))
        self.assertIn("…", body)


class OptionLabelTruncationTests(unittest.TestCase):
    """选项文字不能被无声砍掉。

    实测：一个 59 字的选项被砍到 40 字变成「…把 PRUNED 分」，丢掉的恰好是
    「但要注意存量调用方的兼容性问题」——决策关键。更糟的是不加任何标记，
    读起来像句子说完了，人根本不知道后面还有内容。

    AskUserQuestion 的选项行在终端上不折行（实测抓屏：59 字完整占一行），
    所以解析拿到的就是全文，截断纯粹是我们自己按钮标签这一步造成的。
    """

    LONG = ("直接修改现有的 normalizePlanStatus 函数把 PRUNED 分支补进去"
            "但要注意存量调用方的兼容性问题")

    def _body(self, options):
        card = lk.build_option_card("p1", "proj", options, "gen")
        return "\n".join(el["text"]["content"] for el in card["elements"]
                         if el.get("tag") == "div")

    def test_realistic_long_option_kept_whole(self):
        """实测那条 59 字的选项要能完整显示。"""
        body = self._body([self.LONG, "短的"])
        self.assertIn("兼容性问题", body, f"关键信息被砍掉了: {body}")

    def test_overlong_option_gets_ellipsis(self):
        """真超长时要截，但必须留个记号。"""
        huge = "很长" * 400
        self.assertIn("…", self._body([huge, "短的"]))

    def test_overlong_option_respects_limit(self):
        huge = "很长" * 400
        line = [l for l in self._body([huge, "短的"]).splitlines()
                if "很长" in l][0]
        # 行首有 "**1.** " 前缀，比正文额度略长
        self.assertLessEqual(len(line), lk.OPTION_LABEL_LIMIT + 10, len(line))

    def test_short_option_untouched(self):
        """没超限的选项不该被动，尤其不该平白多个省略号。"""
        self.assertIn("**1.** 短的", self._body(["短的", "也短"]))
        self.assertNotIn("…", self._body(["短的", "也短"]))

    def test_limit_fits_realistic_options(self):
        """额度得装得下实测遇到的选项长度。"""
        self.assertGreaterEqual(lk.OPTION_LABEL_LIMIT, len(self.LONG))


class ApprovalKeysTests(unittest.TestCase):
    """单选要补回车，多选不能补。

    实测：单选框按数字只是把光标移到那一项并高亮，**不提交**——还得按
    Enter 才算选定。只发数字的话，人在飞书上点了按钮、屏幕上看着也选中了，
    agent 却一直卡在那儿不动（实测：选了 1，没给我打回车）。

    多选框相反：数字键是切换勾选，补 Enter 会把才勾了一项的答案提交出去，
    人还没勾完就被交卷了。所以两者必须分开。
    """

    def test_single_select_appends_enter(self):
        self.assertEqual(lk.approval_keys("1", multiselect=False), ["1", "Enter"])

    def test_multiselect_sends_digit_only(self):
        self.assertEqual(lk.approval_keys("2", multiselect=True), ["2"])

    def test_keys_are_relay_safe(self):
        """Enter 得用 relay SAFE_KEYS 认得的名字，发别名会被整条拒绝。"""
        self.assertIn("Enter", lk.approval_keys("1", multiselect=False))


class MultiselectSubmitSplitTests(unittest.TestCase):
    """Tab 和 1 必须分两次发，中间等 Review 页渲染出来。

    实测（逐键探测 w2A:p1）：一次性 send_keys(["Tab","1"]) 之后屏幕停在
    `Ready to submit your answers? / ❯ 1. Submit answers`——Tab 切页要时间，
    紧跟着的 1 在 Review 页渲染出来之前就到了，被丢掉。隔一会儿单独再发
    一次 1，答案立刻提交成功（`probe 多选 → aa, cc`）。
    """

    def test_submit_is_two_steps(self):
        steps = lk.multiselect_submit_steps()
        self.assertEqual([s["keys"] for s in steps], [["Tab"], ["1"]])

    def test_waits_between_steps(self):
        """第一步之后必须有等待，否则等于没拆。"""
        steps = lk.multiselect_submit_steps()
        self.assertGreater(steps[0]["wait"], 0)

    def test_legacy_helper_still_available(self):
        """旧的 multiselect_submit_keys 还有调用方，保持可用。"""
        self.assertEqual(lk.multiselect_submit_keys(), ["Tab", "1"])


class ReviewPageTests(unittest.TestCase):
    """多选提交后的 Review 页不是「下一组问题」。

    实测：Tab 进 Review 页后屏幕是 `Ready to submit your answers? /
    1. Submit answers / 2. Cancel`。它长得就是个编号选择器，_push_next_group
    读屏时会误当成新一组问题推成卡片——人看到一张莫名其妙的
    「1. Submit answers / 2. Cancel」卡片，点下去等于替 agent 乱答。
    """

    def test_review_page_is_recognised(self):
        self.assertTrue(lk.is_review_page(REVIEW_PAGE))

    def test_real_question_is_not_review_page(self):
        pane = ("要跑哪些检查？\n\n"
                "\u276f 1. [ ] 单测\n  2. [ ] 静态扫描\n\n"
                "Enter to select · Esc to cancel")
        self.assertFalse(lk.is_review_page(pane))

    def test_empty_is_not_review_page(self):
        self.assertFalse(lk.is_review_page(""))


class PaneReadErrorTests(unittest.TestCase):
    """read_pane 失败时返回的是错误**字符串**，不是抛异常。

    只 try/except 的调用方会把这句错误文本当成正常屏幕内容解析：解析不出
    多选框，就以为人已经答完，清掉 approval_token——于是勾第二项时点击被
    当成过期审批拒掉，人卡死在那张卡片上（实测：勾上 2 以后就卡住了）。
    """

    def test_error_text_is_recognised(self):
        self.assertTrue(lk.is_pane_read_error("(error reading pane: boom)"))

    def test_no_response_is_recognised(self):
        self.assertTrue(lk.is_pane_read_error("(no response)"))

    def test_normal_content_is_not_error(self):
        self.assertFalse(lk.is_pane_read_error("1. yes\n2. no"))

    def test_empty_is_error(self):
        """空屏没法判断状态，按失败处理，别拿它做「已答完」的依据。"""
        self.assertTrue(lk.is_pane_read_error(""))

    def test_error_text_does_not_look_like_multiselect(self):
        self.assertFalse(lk.detect_multiselect("(error reading pane: boom)"))


class SelectorMissingOptionsTests(unittest.TestCase):
    """选项不全 / 整组丢失的几种真实形态。

    共同后果都一样：解析不出这一组，blocked 卡片退回 Yes/Trust/No，
    按钮和屏幕上问的对不上，点了等于乱答。
    """

    def opts(self, pane):
        group = lk.current_option_group(lk.detect_option_groups(pane))
        return group["options"] if group else None

    def test_left_border_pipes(self):
        """带 TUI 左边框的选择器。

        relay 推 blocked 时给的是原始抓屏，没走 clean_pane，边框还在。
        """
        pane = ("│ 怎么办？\n"
                "│\n"
                "│ \u276f 1. 先停下，我去确认\n"
                "│   2. 继续建群\n"
                "│   3. 先看日志\n"
                "│\n"
                "│ Enter to select · Esc to cancel")
        self.assertEqual(self.opts(pane), ["先停下，我去确认", "继续建群", "先看日志"])

    def test_description_line_starts_with_number(self):
        """选项的描述文字自带编号，不该打乱选项编号序列。"""
        pane = ("怎么办？\n\n"
                "\u276f 1. 先停下，我去确认\n"
                "     1. 先弄清并行会话在干嘛\n"
                "     2. 再决定要不要建群\n"
                "  2. 继续建群\n"
                "  3. 先看日志\n\n"
                "Enter to select · Esc to cancel")
        self.assertEqual(self.opts(pane), ["先停下，我去确认", "继续建群", "先看日志"])

    def test_first_option_scrolled_off(self):
        """首项被卷出屏幕，编号从 2 起——剩下的仍该能选。"""
        pane = ("怎么办？\n\n"
                "  2. 继续建群\n"
                "  3. 先看日志\n\n"
                "Enter to select · Esc to cancel")
        self.assertEqual(self.opts(pane), ["继续建群", "先看日志"])

    @unittest.skip("_MAX_OPTIONS=9 是否放开待定：TUI 数字键只有 1-9，"
                   "第 10 项本来就按不到，先留着上限")
    def test_more_than_nine_options(self):
        """超过 9 项时不能静默砍掉——砍了人就选不到后面那些。"""
        pane = ("选哪个？\n\n" + "\n".join(f"  {i}. 选项{i}" for i in range(1, 13))
                + "\n\nEnter to select · Esc to cancel")
        self.assertEqual(len(self.opts(pane)), 12)


class BlockedUsesRealOptionsTests(unittest.TestCase):
    """blocked 卡片要显示 agent 真正在问的选项。

    relay 的 detect_options 只认两种权限提示（yes, single permission /
    approve all pending），认不出就回落到 TOOL_OPTIONS。AskUserQuestion 的
    选择器落进这个回落里，卡片就显示成 Yes/Trust/No——按钮和屏幕上的选项对
    不上，点了等于乱答。
    """

    def _labels(self, card):
        """选项文字现在列在正文，按钮只放序号——断言得看正文。"""
        return option_lines_in(card)

    def test_real_options_win_over_fallback(self):
        """pane 里有真选择器时，用它，而不是 relay 的回落值。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "herdr-remote", BLOCKED_WITH_REAL_SELECTOR,
            lk.TOOL_OPTIONS, "abcde")
        labels = " ".join(self._labels(card))
        self.assertIn("先停下", labels)
        self.assertNotIn("Trust (always)", labels)

    def test_tail_items_still_filtered(self):
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "herdr-remote", BLOCKED_WITH_REAL_SELECTOR,
            None, "abcde")
        labels = " ".join(self._labels(card))
        self.assertNotIn("Type something", labels)
        self.assertNotIn("Chat about this", labels)

    def test_numbers_match_screen(self):
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "herdr-remote", BLOCKED_WITH_REAL_SELECTOR,
            None, "abcde")
        keys = [b["value"].get("k")
                for el in card["elements"] if el.get("tag") == "action"
                for b in el["actions"] if b["value"].get("k")]
        self.assertEqual(keys, ["1", "2", "3"])

    def test_permission_prompt_still_uses_short_labels(self):
        """真正的权限提示没有编号选择器，仍走 Yes/Trust/No 短标签。"""
        prompt = ("Bash(rm -rf build)\n"
                  "Do you want to proceed?\n"
                  "Esc to cancel")
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", prompt, lk.TOOL_OPTIONS, "abcde")
        labels = " ".join(self._labels(card))
        self.assertIn("Trust (always)", labels)

    def test_no_selector_falls_back(self):
        """prompt 里没有选择器时，行为不变。"""
        card = lk.build_blocked_card(
            "w2B:p1", "claude", "proj", "just some output", None, "abcde")
        labels = " ".join(self._labels(card))
        self.assertIn("Yes (once)", labels)


class MultiSelectToggleTests(unittest.TestCase):
    """多选框的真实按键语义（实测，见 detect_multiselect 的注释）。

    数字键是「切换勾选」而非「选中并提交」，Enter 也不提交——它只切换光标
    所在项。真正提交要 Tab 进 Review 页再按 1。
    """

    def test_detects_multiselect(self):
        self.assertTrue(
            lk.detect_multiselect(lk.clean_pane(REAL_MULTISELECT_PANE)))

    def test_single_select_is_not_multiselect(self):
        """单选框没有 [ ] 标记，别误判——否则单选也要求你按 Submit。"""
        self.assertFalse(
            lk.detect_multiselect(lk.clean_pane(REAL_MULTI_TAB_PANE)))

    def test_checked_state_is_read(self):
        """卡片要能显示哪些已勾上，否则你不知道自己点到哪了。"""
        pane = ("选哪些？\n❯ 1. [✔] 甲\n  2. [ ] 乙\n  3. [✔] 丙\n"
                "\nEnter to select · ↑/↓ to navigate · Esc to cancel")
        self.assertEqual(lk.checked_flags(lk.clean_pane(pane)),
                         [True, False, True])

    def test_unchecked_when_single_select(self):
        """单选框没有勾选态，返回空表示「不适用」。"""
        self.assertEqual(lk.checked_flags(lk.clean_pane(REAL_MULTI_TAB_PANE)), [])


class MultiSelectCardTests(unittest.TestCase):
    """多选卡片：勾多个再 Submit。"""

    def _card(self, checked=None):
        return lk.build_option_card(
            "w1:p1", "proj", ["单测", "静态扫描", "安全审计"], "abcde",
            question="要跑哪些检查？", multiselect=True,
            checked=checked or [False, False, False])

    def _buttons(self, card):
        out = []
        for el in card["elements"]:
            if el.get("tag") == "action":
                out.extend(el["actions"])
        return out

    def test_has_submit_button(self):
        labels = [b["text"]["content"] for b in self._buttons(self._card())]
        self.assertTrue(any("Submit" in x for x in labels), labels)

    def test_submit_uses_its_own_action(self):
        """Submit 不能复用 approval：它发的是 Tab+1，不是一个数字。"""
        submit = [b for b in self._buttons(self._card())
                  if "Submit" in b["text"]["content"]][0]
        self.assertEqual(submit["value"]["a"], lk.ACTION_CODES["submit"])

    def test_checked_options_marked(self):
        """已勾选的项要看得出来。

        按钮只放序号，勾选态显示在正文的选项清单里。
        """
        lines = option_lines_in(self._card([True, False, True]))
        self.assertTrue(lines[0].startswith("✔"), lines[0])
        self.assertFalse(lines[1].startswith("✔"), lines[1])
        self.assertTrue(lines[2].startswith("✔"), lines[2])

    def test_single_select_has_no_submit(self):
        """单选框点一下就定了，多一个 Submit 只会让人误以为还要再点。"""
        card = lk.build_option_card("w1:p1", "proj", ["甲", "乙"], "abcde",
                                    question="选哪个？")
        labels = [b["text"]["content"] for b in self._buttons(card)]
        self.assertFalse(any("Submit" in x for x in labels), labels)

    def test_toggle_buttons_keep_numeric_keys(self):
        """勾选仍靠数字键，与屏幕编号一一对应。"""
        keys = [b["value"].get("k") for b in self._buttons(self._card())
                if b["value"].get("a") == lk.ACTION_CODES["approval"]]
        self.assertEqual(keys, ["1", "2", "3"])

    def test_generation_carried_on_submit(self):
        """Submit 也要带 generation，否则旧卡片上的它仍能提交。"""
        submit = [b for b in self._buttons(self._card())
                  if "Submit" in b["text"]["content"]][0]
        self.assertEqual(submit["value"]["g"], "abcde")


class OptionsCardFromPaneTests(unittest.TestCase):
    """从真实 pane 文本推卡片时，多选/单选要各自渲染对。

    三个推卡片的调用点都走 build_options_card，所以在这一层验就够了。
    """

    def _labels(self, card):
        out = []
        for el in card["elements"]:
            if el.get("tag") == "action":
                out.extend(b["text"]["content"] for b in el["actions"])
        return out

    def test_multiselect_pane_gets_submit(self):
        content = lk.clean_pane(REAL_MULTISELECT_PANE)
        group = lk.current_option_group(lk.detect_option_groups(content))
        card = lk.build_options_card("w1:p1", "proj", group["options"],
                                     "abcde", question=group["question"],
                                     content=content)
        self.assertTrue(any("Submit" in x for x in self._labels(card)))

    def test_single_select_pane_has_no_submit(self):
        content = lk.clean_pane(REAL_MULTI_TAB_PANE)
        group = lk.current_option_group(lk.detect_option_groups(content))
        card = lk.build_options_card("w1:p1", "proj", group["options"],
                                     "abcde", question=group["question"],
                                     content=content)
        self.assertFalse(any("Submit" in x for x in self._labels(card)))

    def test_without_content_stays_single_select(self):
        """旧调用方不传 content，行为不变。"""
        card = lk.build_options_card("w1:p1", "proj", ["甲", "乙"], "abcde")
        self.assertFalse(any("Submit" in x for x in self._labels(card)))


class SubmitKeySequenceTests(unittest.TestCase):
    """提交的按键序列。实测：Enter 不提交，Tab 才进 Review 页。"""

    def test_sequence_is_tab_then_one(self):
        self.assertEqual(lk.multiselect_submit_keys(), ["Tab", "1"])

    def test_keys_are_relay_safe(self):
        """relay 的 SAFE_KEYS 白名单只认这些名字，发别名会被整条拒绝。"""
        for key in lk.multiselect_submit_keys():
            with self.subTest(key=key):
                self.assertIn(key, {"Tab", "Enter", "Escape"} |
                              {str(n) for n in range(10)})


class TuiTailItemTests(unittest.TestCase):
    """TUI 固定尾项的识别。它们跟着每一个 AskUserQuestion 选择器走。"""

    def test_type_something_dropped(self):
        self.assertTrue(lk.is_tui_tail_option("Type something."))

    def test_chat_about_this_dropped(self):
        self.assertTrue(lk.is_tui_tail_option("Chat about this"))

    def test_case_and_space_tolerated(self):
        self.assertTrue(lk.is_tui_tail_option("  type something.  "))

    def test_real_option_kept(self):
        """别误伤：正常选项里也可能出现这些词。"""
        self.assertFalse(lk.is_tui_tail_option("Type something into the form"))
        self.assertFalse(lk.is_tui_tail_option("先写测试"))


class SelectorHintLineTests(unittest.TestCase):
    """选择器底部的操作提示行。"""

    def test_enter_to_select_is_hint(self):
        self.assertTrue(lk.is_selector_hint(
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel"))

    def test_esc_to_cancel_is_hint(self):
        self.assertTrue(lk.is_selector_hint("Esc to cancel"))

    def test_prose_is_not_hint(self):
        """普通输出别被当成提示行放过，否则历史旧选择器会被误认。"""
        self.assertFalse(lk.is_selector_hint("跑完测试了，全绿。"))
        self.assertFalse(lk.is_selector_hint("Enter the build directory"))
        self.assertFalse(lk.is_selector_hint("Press any key"))
        self.assertFalse(lk.is_selector_hint("navigate to the folder"))


# herdr 自己判断 agent 是否 blocked，用的就是这些提示行。
# 抄自它的检测 manifest（跟着 Claude 版本远程更新）：
#   ~/.local/state/herdr/agent-detection/remote/claude.toml → live_blocked_form
# 照单次抓屏归纳会漏变体，漏一个，那个变体下的选择器就整组丢掉。
MANIFEST_HINT_PHRASES = [
    "enter to confirm",
    "enter to select",
    "tab/arrow keys to navigate",
    "arrow keys to navigate",
    "arrows to navigate",
    "↑/↓ to navigate",
    "↑↓ to navigate",
    "esc to cancel",
    "enter to set as default",
]


class ManifestHintCoverageTests(unittest.TestCase):
    """herdr manifest 列出的每个提示行变体都得认得。

    这些短语单独成行、或用 · 串起来出现在选择器底部。任何一个没认出来，
    _is_selector_tail 就会把它当正文，判定选择器已翻篇，卡片上一个按钮
    都不剩——正是 f5b03e1 修的那个 bug。
    """

    def test_every_phrase_alone_is_a_hint(self):
        for phrase in MANIFEST_HINT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertTrue(lk.is_selector_hint(phrase), phrase)

    def test_phrases_are_case_insensitive(self):
        for phrase in MANIFEST_HINT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertTrue(lk.is_selector_hint(phrase.title()), phrase)

    def test_phrases_joined_by_separator(self):
        """真实屏幕上是用 · 串起来的一整行。"""
        for nav in ("Tab/Arrow keys to navigate", "Arrow keys to navigate",
                    "Arrows to navigate", "↑/↓ to navigate", "↑↓ to navigate"):
            line = f"Enter to select · {nav} · Esc to cancel"
            with self.subTest(line=line):
                self.assertTrue(lk.is_selector_hint(line), line)

    def test_confirm_variant(self):
        """AskUserQuestion 的多选框用的是 confirm 而不是 select。"""
        self.assertTrue(lk.is_selector_hint("Enter to confirm · Esc to cancel"))

    def test_model_picker_variant(self):
        """选模型菜单：Enter to set as default。"""
        self.assertTrue(
            lk.is_selector_hint("Enter to set as default · Esc to cancel"))

    def test_hint_line_survives_in_full_pane(self):
        """每个变体都要能让选择器整体存活，而不只是谓词返回 True。"""
        for nav in ("Tab/Arrow keys to navigate", "↑/↓ to navigate",
                    "Arrows to navigate"):
            pane = ("选哪个？\n"
                    "❯ 1. 甲\n"
                    "  2. 乙\n"
                    "  3. Type something.\n"
                    f"\nEnter to select · {nav} · Esc to cancel")
            with self.subTest(nav=nav):
                groups = lk.detect_option_groups(lk.clean_pane(pane))
                self.assertEqual(len(groups), 1, nav)
                self.assertEqual(groups[0]["options"], ["甲", "乙"], nav)




class SuggestCommandTests(unittest.TestCase):
    """打错命令时给建议，而不是静默把它当指令发给 agent。

    这不只是便利性：/raed 3 原来会一路走到 _handle_free_text，
    真的把「/raed 3」粘进终端。
    """

    def test_typo_suggests_nearest(self):
        self.assertEqual(lk.suggest_command("/raed 3"), "read")

    def test_transposed_letters(self):
        self.assertEqual(lk.suggest_command("/agnets"), "agents")

    def test_missing_letter(self):
        self.assertEqual(lk.suggest_command("/intrrupt 1"), "interrupt")

    def test_valid_command_needs_no_suggestion(self):
        self.assertIsNone(lk.suggest_command("/read 1"))

    def test_free_text_is_not_a_typo(self):
        """不以 / 开头的就是要发给 agent 的正文，绝不能拦。"""
        self.assertIsNone(lk.suggest_command("run the tests"))

    def test_unrelated_slash_word_gives_no_suggestion(self):
        """离所有命令都太远时不要硬猜，否则更迷惑。"""
        self.assertIsNone(lk.suggest_command("/xyzzyplugh"))

    def test_ignores_at_suffix(self):
        self.assertEqual(lk.suggest_command("/raed@demo 3"), "read")

    def test_case_insensitive(self):
        self.assertEqual(lk.suggest_command("/RAED 3"), "read")

    def test_bare_slash_is_not_a_typo(self):
        self.assertIsNone(lk.suggest_command("/"))

    def test_suggestion_is_a_real_command(self):
        """建议必须真的能用——否则提示等于把人引到另一个错。"""
        for typo in ("/raed", "/agnets", "/statu", "/hlep"):
            with self.subTest(typo=typo):
                self.assertIn(lk.suggest_command(typo), lk.COMMANDS)

    def test_path_like_text_is_not_a_typo(self):
        """粘一条绝对路径进来，不该被当成打错的命令。"""
        self.assertIsNone(lk.suggest_command("/Users/victor/code/foo.py"))


class ClearCommandTests(unittest.TestCase):
    """/clear 把字面量 "/clear" 发进 pane，清掉 agent 自己的上下文。

    清的是 agent 的对话上下文（Claude Code 的 /clear），不是飞书群里的
    消息——所以走 send_text_to_relay，和用户在终端里手打完全等价。

    必须用 send_text（粘贴 + 等 ack + 回车）而不是 send_keys：
    send_keys 走 relay 的 SAFE_KEYS 白名单，"/clear" 不在里面会被整条
    拒绝，而用户看到的却是「已清空」。
    """

    def setUp(self):
        self.bot = make_bot()
        self.bot.agents = make_agents(2)

    def _run(self, text):
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, text=text)
        with unittest.mock.patch.object(
                lk, "send_text_to_relay",
                new=unittest.mock.AsyncMock()) as sender:
            asyncio.run(self.bot._handle_text(ctx))
        return sender

    def test_clear_is_a_known_command(self):
        """必须进命令表，否则 parse_command 把它当自由文本粘进 pane。"""
        self.assertIn("clear", lk.COMMANDS)

    def test_clear_sends_literal_slash_clear(self):
        sender = self._run("/clear 1")
        sender.assert_called_once()
        pane_id, text = sender.call_args[0][0], sender.call_args[0][1]
        self.assertEqual(text, "/clear")
        self.assertEqual(pane_id, lk.index_agents(self.bot.agents)[0]["pane_id"])

    def test_clear_targets_the_indexed_agent(self):
        """序号要对上 index_agents 的顺序，不能用 sorted_agents。"""
        sender = self._run("/clear 2")
        self.assertEqual(sender.call_args[0][0],
                         lk.index_agents(self.bot.agents)[1]["pane_id"])

    def test_clear_confirms_which_agent(self):
        """回执要点名项目，否则清错了都不知道。"""
        self._run("/clear 1")
        said = " ".join(str(c) for c in self.bot.api.send_text.call_args_list)
        target = lk.index_agents(self.bot.agents)[0]["project"]
        self.assertIn(target, said)

    def test_clear_without_index_does_not_send(self):
        """没指定序号时先让人选，绝不能瞎清一个。"""
        sender = self._run("/clear")
        sender.assert_not_called()

    def test_clear_unknown_agent_does_not_send(self):
        sender = self._run("/clear nonexistent-project")
        sender.assert_not_called()

    def test_clear_is_audited(self):
        """不可逆操作必须留痕。"""
        with unittest.mock.patch.object(
                self.bot, "audit") as audit:
            with unittest.mock.patch.object(
                    lk, "send_text_to_relay", new=unittest.mock.AsyncMock()):
                ctx = lk.MessageContext(
                    chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
                    chat_type="p2p", mentioned_bot=True, text="/clear 1")
                asyncio.run(self.bot._handle_text(ctx))
        audit.assert_called_once()
        self.assertEqual(audit.call_args[0][1], "clear")


class TypoInterceptTests(unittest.TestCase):
    """打错的命令必须被拦住，不能粘进终端。

    回归防线：/raed 3 曾经一路走到 _handle_free_text，被当成要发给
    agent 的正文原样送进 pane。
    """

    def setUp(self):
        self.bot = make_bot()
        self.bot.agents = make_agents(2)
        self.bot.set_active("oc_1", "w1:p1", "project1")

    def _run(self, text):
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, text=text)
        with unittest.mock.patch.object(
                lk, "send_text_to_relay",
                new=unittest.mock.AsyncMock()) as sender:
            asyncio.run(self.bot._handle_text(ctx))
        return sender

    def test_typo_is_not_sent_to_agent(self):
        sender = self._run("/raed 3")
        sender.assert_not_called()

    def test_typo_reply_names_the_real_command(self):
        self._run("/raed 3")
        said = " ".join(str(c) for c in self.bot.api.send_text.call_args_list)
        self.assertIn("/read", said)

    def test_free_text_still_reaches_agent(self):
        """纠错绝不能挡住正常指挥——那是主路径。"""
        sender = self._run("继续改这个函数")
        sender.assert_called_once()

    def test_unguessable_slash_word_still_sent(self):
        """猜不出来的就按原样放行，维持原有行为。"""
        sender = self._run("/xyzzyplugh")
        sender.assert_called_once()


class AgentActionsCardTests(unittest.TestCase):
    """选中一个 agent 后，能做什么应该是点出来的，不是打出来的。

    /agents 原来只回纯文本，看完还得手打 /read 1——手机上正是最累的一步。
    """

    def setUp(self):
        self.agents = make_agents(3)

    def test_card_names_the_agent(self):
        card = lk.build_agent_actions_card(self.agents[0])
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("project", blob)
        self.assertIn("opencode", blob)

    def test_offers_read_and_send(self):
        card = lk.build_agent_actions_card(self.agents[0])
        codes = _action_codes_in(card)
        self.assertIn(lk.ACTION_CODES["read"], codes)
        self.assertIn(lk.ACTION_CODES["select_send"], codes)

    def test_idle_agent_has_no_interrupt(self):
        """空闲的 agent 没什么可中断，按钮不该出现。"""
        idle = dict(self.agents[0], status="idle")
        codes = _action_codes_in(lk.build_agent_actions_card(idle))
        self.assertNotIn(lk.ACTION_CODES["interrupt"], codes)

    def test_working_agent_can_be_interrupted(self):
        working = dict(self.agents[0], status="working")
        codes = _action_codes_in(lk.build_agent_actions_card(working))
        self.assertIn(lk.ACTION_CODES["interrupt"], codes)

    def test_blocked_agent_offers_trust(self):
        blocked = dict(self.agents[0], status="blocked")
        codes = _action_codes_in(lk.build_agent_actions_card(blocked))
        self.assertIn(lk.ACTION_CODES["trust"], codes)

    def test_idle_agent_has_no_trust(self):
        """没在等审批就没有可批的东西。"""
        idle = dict(self.agents[0], status="idle")
        codes = _action_codes_in(lk.build_agent_actions_card(idle))
        self.assertNotIn(lk.ACTION_CODES["trust"], codes)

    def test_every_button_carries_a_pane_token(self):
        """按钮不带 pane 就点不动——回归防线。"""
        card = lk.build_agent_actions_card(dict(self.agents[0], status="blocked"))
        for action in _actions_in(card):
            with self.subTest(label=action["text"]["content"]):
                self.assertIn("p", action["value"])


def _actions_in(card):
    out = []
    for element in card.get("elements", []):
        if element.get("tag") == "action":
            out.extend(element.get("actions", []))
    return out


def _action_codes_in(card):
    return {a["value"].get("a") for a in _actions_in(card)}


class AgentsCardWiringTests(unittest.TestCase):
    """/agents 的按钮要能点开那个 agent 的动作卡。"""

    def setUp(self):
        self.bot = make_bot()
        self.bot.agents = make_agents(2)

    def _click(self, value):
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, action=value)
        asyncio.run(self.bot._handle_action(ctx))

    def test_menu_action_is_registered(self):
        self.assertIn("agent_menu", lk.ACTION_CODES)

    def test_clicking_agent_opens_actions_card(self):
        self._click(lk.action_value("agent_menu", "w1:p1"))
        self.bot.api.send_card.assert_called_once()
        blob = json.dumps(self.bot.api.send_card.call_args[0][1], ensure_ascii=False)
        self.assertIn("看进展", blob)

    def test_agents_command_replies_with_card(self):
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, text="/agents")
        asyncio.run(self.bot._handle_text(ctx))
        self.bot.api.send_card.assert_called_once()

    def test_agents_card_buttons_open_menus(self):
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, text="/agents")
        asyncio.run(self.bot._handle_text(ctx))
        card = self.bot.api.send_card.call_args[0][1]
        codes = _action_codes_in(card)
        self.assertIn(lk.ACTION_CODES["agent_menu"], codes)

    def test_no_agents_still_replies_text(self):
        """一个 agent 都没有时别发空卡片。"""
        self.bot.agents = []
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, text="/agents")
        asyncio.run(self.bot._handle_text(ctx))
        self.bot.api.send_card.assert_not_called()
        self.bot.api.send_text.assert_called_once()


class AgentsOverviewTests(unittest.TestCase):
    """/agents 既要能点，也要留住序号这条快路。

    卡片按钮只有项目名，agent 一多就难分辨；序号还能配 /read 3 直达。
    两者都要，不能拿一个换另一个。
    """

    def setUp(self):
        self.bot = make_bot()
        self.bot.agents = make_agents(3)

    def _agents(self):
        ctx = lk.MessageContext(
            chat_id="oc_1", message_id="om_1", sender_open_id="ou_u",
            chat_type="p2p", mentioned_bot=True, text="/agents")
        asyncio.run(self.bot._handle_text(ctx))

    def test_still_shows_numbered_list(self):
        self._agents()
        self.bot.api.send_text.assert_called_once()
        said = self.bot.api.send_text.call_args[0][1]
        self.assertIn("1.", said)

    def test_also_sends_card(self):
        self._agents()
        self.bot.api.send_card.assert_called_once()

    def test_numbers_match_match_agent(self):
        """列表里的序号必须和 match_agent 的解析对得上。

        picker 卡片用 sorted_agents，序号用 index_agents——两者顺序不一定
        相同，所以序号绝不能按按钮位置去标。
        """
        ordered = lk.index_agents(self.bot.agents)
        for position, agent in enumerate(ordered, start=1):
            with self.subTest(number=position):
                self.assertEqual(
                    lk.match_agent(self.bot.agents, str(position))["pane_id"],
                    agent["pane_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
