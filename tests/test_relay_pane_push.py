#!/usr/bin/env python3
"""服务端主动推 pane 变化 + 增量行传输（pane-push-v1）。

这三件事是为跨国高延迟链路做的：原来前端每 3 秒（slash 模式 0.4 秒）
全量拉一次整屏，每次都吃一个完整 RTT。改成 relay 侧比对、只在有变化时
推增量之后，静止画面的开销降到零。
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay

PANE_PUSH_CAP = "pane-push-v1"


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    def msgs(self):
        out = []
        for s in self.sent:
            if isinstance(s, str):
                out.append(json.loads(s))
            else:
                out.append(herdr_relay.decode_hgz1(s))
        return out


class BuildPaneDeltaTest(unittest.TestCase):
    """纯函数：整屏 → 增量消息。"""

    def test_no_baseline_returns_none_for_full_send(self):
        msg, state = herdr_relay.build_pane_delta(None, "p1", 200, "a\nb")
        self.assertIsNone(msg, "没有基线时必须发全量")
        self.assertEqual(state, (200, ["a", "b"]))

    def test_identical_content_reports_unchanged(self):
        prev = (200, ["a", "b"])
        msg, _ = herdr_relay.build_pane_delta(prev, "p1", 200, "a\nb")
        self.assertTrue(msg["unchanged"])
        self.assertNotIn("tail", msg, "没变就不该带任何行数据")

    def test_appended_lines_send_only_tail(self):
        prev = (200, ["a", "b"])
        msg, state = herdr_relay.build_pane_delta(prev, "p1", 200, "a\nb\nc\nd")
        self.assertEqual(msg["keep"], 2)
        self.assertEqual(msg["tail"], ["c", "d"])
        self.assertEqual(msg["total"], 4)
        self.assertEqual(state, (200, ["a", "b", "c", "d"]))

    def test_tail_rewrite_keeps_common_prefix(self):
        # 终端最后一行常被原地改写（进度条、spinner）。
        prev = (200, ["a", "b", "old"])
        msg, _ = herdr_relay.build_pane_delta(prev, "p1", 200, "a\nb\nnew")
        self.assertEqual(msg["keep"], 2)
        self.assertEqual(msg["tail"], ["new"])

    def test_scrolled_screen_sends_delta_not_full(self):
        """屏满后头部滚出——终端最常见的形态，必须走增量。

        纯前缀比对在这里会失效（首行就变了），所以实现里额外找滚动偏移。
        """
        prev = ["l%d" % i for i in range(200)]
        cur = prev[3:] + ["new1", "new2", "new3"]
        msg, _ = herdr_relay.build_pane_delta(
            (200, prev), "p1", 200, "\n".join(cur))
        self.assertIsNotNone(msg, "头部滚动不该退回全量")
        self.assertEqual(msg["drop"], 3)
        self.assertEqual(msg["tail"], ["new1", "new2", "new3"])
        rebuilt = prev[msg["drop"]:][:msg["keep"]] + msg["tail"]
        self.assertEqual(rebuilt, cur, "增量必须能无损拼回原文")
        self.assertEqual(msg["total"], len(cur))

    def test_scroll_by_one_line(self):
        prev = ["l%d" % i for i in range(100)]
        cur = prev[1:] + ["tail"]
        msg, _ = herdr_relay.build_pane_delta(
            (200, prev), "p1", 200, "\n".join(cur))
        self.assertEqual(msg["drop"], 1)
        self.assertEqual(msg["tail"], ["tail"])
        rebuilt = prev[1:][:msg["keep"]] + msg["tail"]
        self.assertEqual(rebuilt, cur)

    def test_full_screen_change_falls_back_to_full(self):
        # 首行就变了（clear / 切 TUI 视图），增量不划算。
        prev = (200, ["a", "b"])
        msg, _ = herdr_relay.build_pane_delta(prev, "p1", 200, "x\ny")
        self.assertIsNone(msg)

    def test_window_size_change_forces_full(self):
        # loadMore 改了 lines，行窗口错位，增量会拼错。
        prev = (200, ["a", "b"])
        msg, _ = herdr_relay.build_pane_delta(prev, "p1", 700, "a\nb\nc")
        self.assertIsNone(msg)

    def test_shrunk_content_keeps_prefix(self):
        prev = (200, ["a", "b", "c"])
        msg, _ = herdr_relay.build_pane_delta(prev, "p1", 200, "a\nb")
        self.assertEqual(msg["keep"], 2)
        self.assertEqual(msg["tail"], [])
        self.assertEqual(msg["total"], 2)

    def test_delta_reconstruction_matches_original(self):
        """增量必须能拼回服务端看到的原文，否则界面会错行。"""
        prev_lines = ["l%d" % i for i in range(50)]
        cur_lines = prev_lines[:48] + ["changed", "added", "more"]
        msg, _ = herdr_relay.build_pane_delta(
            (200, prev_lines), "p1", 200, "\n".join(cur_lines))
        rebuilt = prev_lines[:msg["keep"]] + msg["tail"]
        self.assertEqual(rebuilt, cur_lines)


class SendPaneUpdateTest(unittest.TestCase):
    """发送路径：cap 决定走增量还是全量。"""

    def setUp(self):
        herdr_relay.client_caps.clear()
        herdr_relay.pane_last_sent.clear()
        herdr_relay.pane_subs.clear()

    def tearDown(self):
        herdr_relay.client_caps.clear()
        herdr_relay.pane_last_sent.clear()
        herdr_relay.pane_subs.clear()

    def test_client_without_cap_always_gets_full_content(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = set()
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, "a\nb"))
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, "a\nb\nc"))
        types = [m["type"] for m in ws.msgs()]
        self.assertEqual(types, ["pane_content", "pane_content"],
                         "没声明 cap 的老客户端不该收到 pane_delta")

    def test_second_send_uses_delta(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, "a\nb"))
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, "a\nb\nc"))
        msgs = ws.msgs()
        self.assertEqual(msgs[0]["type"], "pane_content", "首次没基线，发全量")
        self.assertEqual(msgs[1]["type"], "pane_delta")
        self.assertEqual(msgs[1]["tail"], ["c"])

    def test_force_full_resets_baseline(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, "a\nb"))
        asyncio.run(herdr_relay.send_pane_update(
            ws, "p1", 200, "a\nb\nc", force_full=True))
        msgs = ws.msgs()
        self.assertEqual(msgs[1]["type"], "pane_content",
                         "force_full 时必须发全量（客户端刚打开视图，手里没基线）")

    def test_unchanged_content_sends_tiny_message(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
        big = "\n".join("line %d" % i for i in range(500))
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, big))
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, big))
        msgs = ws.msgs()
        self.assertTrue(msgs[1]["unchanged"])
        # 这就是本次改造的意义：静止画面的第二次推送几乎不占字节。
        self.assertLess(len(json.dumps(msgs[1])), 120)

    def test_per_client_baselines_are_independent(self):
        """两个客户端看同一 pane，基线不能共用，否则会发错增量。"""
        a, b = FakeWebSocket(), FakeWebSocket()
        herdr_relay.client_caps[a] = {PANE_PUSH_CAP}
        herdr_relay.client_caps[b] = {PANE_PUSH_CAP}
        asyncio.run(herdr_relay.send_pane_update(a, "p1", 200, "a\nb"))
        # b 还没有基线，即便 a 已经有了，b 也必须先拿全量。
        asyncio.run(herdr_relay.send_pane_update(b, "p1", 200, "a\nb"))
        self.assertEqual(b.msgs()[0]["type"], "pane_content")

    def test_forget_client_clears_all_state(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
        herdr_relay.pane_subs[ws] = {"pane_id": "p1", "lines": 200}
        asyncio.run(herdr_relay.send_pane_update(ws, "p1", 200, "a\nb"))
        self.assertTrue(herdr_relay.pane_last_sent)
        herdr_relay.forget_client(ws)
        self.assertNotIn(ws, herdr_relay.client_caps)
        self.assertNotIn(ws, herdr_relay.pane_subs)
        self.assertEqual(
            [k for k in herdr_relay.pane_last_sent if k[0] is ws], [],
            "按 ws 存的基线必须随连接一起清掉，否则重连多了会无限涨")


class PushSubscribedPanesTest(unittest.TestCase):
    def setUp(self):
        herdr_relay.client_caps.clear()
        herdr_relay.pane_last_sent.clear()
        herdr_relay.pane_subs.clear()
        herdr_relay.clients.clear()
        herdr_relay.known_panes.clear()

    def tearDown(self):
        self.setUp()

    def test_reads_pane_once_for_multiple_subscribers(self):
        a, b = FakeWebSocket(), FakeWebSocket()
        for ws in (a, b):
            herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
            herdr_relay.clients.add(ws)
            herdr_relay.pane_subs[ws] = {"pane_id": "p1", "lines": 200}
        herdr_relay.known_panes.add("p1")

        calls = []

        async def fake_read(*args, **kwargs):
            calls.append(args)
            return "hello\nworld"

        orig = herdr_relay.run_herdr_async
        herdr_relay.run_herdr_async = fake_read
        try:
            asyncio.run(herdr_relay.push_subscribed_panes())
        finally:
            herdr_relay.run_herdr_async = orig

        self.assertEqual(len(calls), 1,
                         "同一 pane 被多个客户端订阅时只该读一次 herdr")
        self.assertEqual(len(a.msgs()), 1)
        self.assertEqual(len(b.msgs()), 1)

    def test_drops_subscription_when_client_gone(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
        herdr_relay.pane_subs[ws] = {"pane_id": "p1", "lines": 200}
        # 故意不加入 clients：模拟已断开。
        asyncio.run(herdr_relay.push_subscribed_panes())
        self.assertNotIn(ws, herdr_relay.pane_subs)

    def test_skips_unknown_pane(self):
        ws = FakeWebSocket()
        herdr_relay.client_caps[ws] = {PANE_PUSH_CAP}
        herdr_relay.clients.add(ws)
        herdr_relay.pane_subs[ws] = {"pane_id": "ghost", "lines": 200}
        # known_panes 里没有 ghost
        asyncio.run(herdr_relay.push_subscribed_panes())
        self.assertEqual(ws.msgs(), [])

    def test_no_subscribers_is_a_noop(self):
        # 没人订阅时不该去碰 herdr。
        def boom(*a, **k):
            raise AssertionError("不该读 herdr")

        orig = herdr_relay.run_herdr_async
        herdr_relay.run_herdr_async = boom
        try:
            asyncio.run(herdr_relay.push_subscribed_panes())
        finally:
            herdr_relay.run_herdr_async = orig


if __name__ == "__main__":
    unittest.main()
