#!/usr/bin/env python3
"""连接建立时应立即推送缓存快照，而不是让客户端干等下一次轮询。

实测（iPhone 视口，本机 relay）：
    WebSocket 握手      75 ms
    首条 agents 到达   534 ms   ← 握手后又白等了 458 ms

握手其实很快，慢在 handle_client 里 clients.add(ws) 之后直接进消息循环，
不推任何初始数据，客户端只能等下一次 2 秒轮询广播——最坏要等满 POLL_INTERVAL，
而 agent_cache 里的数据一直都在。这是纯等待，不是网络慢。
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


AGENT_A = {
    "pane_id": "w1:p1", "agent": "claude", "label": "", "status": "working",
    "cwd": "/work/alpha", "project": "alpha", "host": "local",
    "remote": None, "workspace_id": "w1", "tab_id": "w1:t1",
}
AGENT_B = {
    "pane_id": "w2:p1", "agent": "codex", "label": "", "status": "blocked",
    "cwd": "/work/beta", "project": "beta", "host": "local",
    "remote": None, "workspace_id": "w2", "tab_id": "w2:t1",
}


class FakeWebSocket:
    """够用的 websocket 替身：记录 send 的内容，收完预置消息后结束迭代。"""

    def __init__(self, incoming=None, headers=None):
        self.sent = []
        self._incoming = list(incoming or [])
        self.remote_address = ("127.0.0.1", 55555)

        class _Req:
            def __init__(self, hdrs):
                self.headers = hdrs or {}
        self.request = _Req(headers or {"User-Agent": "iPhone"})

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        async def gen():
            for item in self._incoming:
                yield item
        return gen()

    def sent_messages(self):
        return [json.loads(s) for s in self.sent]


def _reset():
    herdr_relay.agent_cache.clear()
    herdr_relay.known_panes.clear()
    herdr_relay.pane_remote_map.clear()
    herdr_relay.clients.clear()
    if hasattr(herdr_relay, "client_caps"):
        herdr_relay.client_caps.clear()


class InitialSnapshotTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()

    async def test_sends_snapshot_immediately_on_connect(self):
        herdr_relay.agent_cache["w1:p1"] = dict(AGENT_A)
        herdr_relay.agent_cache["w2:p1"] = dict(AGENT_B)

        ws = FakeWebSocket()
        await herdr_relay.handle_client(ws)

        msgs = ws.sent_messages()
        self.assertTrue(msgs, "连接后应立即收到消息，而不是等轮询")
        first = msgs[0]
        self.assertEqual(first.get("type"), "agents",
                         f"首条消息应为 agents 快照，实际为 {first.get('type')!r}")
        pane_ids = {a["pane_id"] for a in first["agents"]}
        self.assertEqual(pane_ids, {"w1:p1", "w2:p1"},
                         "快照应包含缓存里的全部 agent")

    async def test_snapshot_preserves_agent_fields(self):
        """快照必须是完整的 agent 记录，缺字段会让客户端渲染不出卡片。"""
        herdr_relay.agent_cache["w1:p1"] = dict(AGENT_A)

        ws = FakeWebSocket()
        await herdr_relay.handle_client(ws)

        agent = ws.sent_messages()[0]["agents"][0]
        for key in ("pane_id", "agent", "status", "cwd", "project", "host"):
            self.assertIn(key, agent, f"快照缺少字段 {key}")
        self.assertEqual(agent["status"], "working")
        self.assertEqual(agent["project"], "alpha")

    async def test_empty_cache_still_sends_snapshot(self):
        """缓存为空也要发一条空快照，让客户端从 loading 态切到 empty 态，
        否则界面会一直停在 Loading 直到下一次轮询。"""
        ws = FakeWebSocket()
        await herdr_relay.handle_client(ws)

        msgs = ws.sent_messages()
        self.assertTrue(msgs, "空缓存也应发快照")
        self.assertEqual(msgs[0]["type"], "agents")
        self.assertEqual(msgs[0]["agents"], [])

    async def test_snapshot_does_not_leak_to_other_clients(self):
        """初始快照只发给新连上的这一个客户端，不应广播给所有人。"""
        herdr_relay.agent_cache["w1:p1"] = dict(AGENT_A)
        existing = FakeWebSocket()
        herdr_relay.clients.add(existing)

        ws = FakeWebSocket()
        await herdr_relay.handle_client(ws)

        self.assertEqual(existing.sent, [],
                         "已连接的其它客户端不应收到这条初始快照")

    async def test_snapshot_precedes_message_handling(self):
        """快照必须在处理客户端消息之前发出。否则客户端一连上就发请求时，
        响应会排在快照前面，UI 拿到乱序数据。"""
        herdr_relay.agent_cache["w1:p1"] = dict(AGENT_A)
        # 客户端一连上就发了个 read_pane（pane 未知，会回 error）
        ws = FakeWebSocket(incoming=[json.dumps({"type": "read_pane", "pane_id": "nope"})])

        await herdr_relay.handle_client(ws)

        types = [m.get("type") for m in ws.sent_messages()]
        self.assertEqual(types[0], "agents",
                         f"首条必须是快照，实际顺序为 {types}")

    async def test_send_failure_does_not_break_connection(self):
        """快照发送失败（比如客户端刚连上就断了）不应让 handle_client 抛异常，
        否则一个坏连接会在日志里刷 traceback。"""
        herdr_relay.agent_cache["w1:p1"] = dict(AGENT_A)

        ws = FakeWebSocket()
        ws.send = mock.AsyncMock(side_effect=Exception("connection gone"))

        try:
            await herdr_relay.handle_client(ws)
        except Exception as e:
            self.fail(f"快照发送失败不应向上抛异常: {e}")


if __name__ == "__main__":
    unittest.main()
