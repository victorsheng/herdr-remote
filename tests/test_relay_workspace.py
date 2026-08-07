#!/usr/bin/env python3
"""create_workspace / rename_workspace / close_workspace 的 WebSocket 分支测试。

对齐 create_tab 协议面，但用 run_herdr_rc_async 看退出码（与 pane_zoom 一致），
失败时回 error，成功才回 workspace_*。
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


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
    herdr_relay.workspace_label_cache.clear()


def _non_snapshot(msgs):
    return [m for m in msgs if m.get("type") != "agents"]


class WorkspaceLabelParseTests(unittest.TestCase):
    def test_parse_workspace_labels(self):
        raw = json.dumps({
            "result": {"type": "workspace_list", "workspaces": [
                {"workspace_id": "w1", "label": "Alpha"},
                {"workspace_id": "w2", "label": "  "},
                {"workspace_id": "", "label": "skip"},
            ]},
        })
        labels = herdr_relay._parse_workspace_labels(raw)
        self.assertEqual(labels, {"w1": "Alpha"})

    def test_apply_workspace_labels(self):
        agents = [
            {"pane_id": "w1:p1", "workspace_id": "w1", "project": "dir"},
            {"pane_id": "w2:p1", "workspace_id": "w2", "project": "other"},
        ]
        herdr_relay._apply_workspace_labels(agents, {"w1": "Nice Name"})
        self.assertEqual(agents[0]["workspace_label"], "Nice Name")
        self.assertEqual(agents[1]["workspace_label"], "")


class WorkspaceHandlerTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()

    async def test_create_workspace_passes_focus_and_succeeds(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "create_workspace"})])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
            return_value=(0, ""),
        ) as rc, mock.patch.object(herdr_relay, "audit") as audit:
            await herdr_relay.handle_client(ws)

        rc.assert_awaited_once_with("workspace", "create", "--focus")
        audit.assert_called()
        types = [m["type"] for m in _non_snapshot(ws.sent_messages())]
        self.assertIn("workspace_created", types)
        created = next(m for m in ws.sent_messages() if m.get("type") == "workspace_created")
        self.assertTrue(created["ok"])

    async def test_create_workspace_nonzero_exit_sends_error(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "create_workspace"})])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
            return_value=(1, "boom"),
        ):
            await herdr_relay.handle_client(ws)

        types = [m["type"] for m in _non_snapshot(ws.sent_messages())]
        self.assertIn("error", types)
        self.assertNotIn("workspace_created", types)

    async def test_rename_workspace_success_argv(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "rename_workspace",
            "workspace_id": "w1",
            "label": "  Alpha  ",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
            return_value=(0, ""),
        ) as rc, mock.patch.object(herdr_relay, "audit") as audit:
            await herdr_relay.handle_client(ws)

        rc.assert_awaited_once_with("workspace", "rename", "w1", "Alpha")
        audit.assert_called()
        renamed = next(m for m in ws.sent_messages() if m.get("type") == "workspace_renamed")
        self.assertTrue(renamed["ok"])
        self.assertEqual(renamed.get("workspace_id"), "w1")
        self.assertEqual(renamed.get("label"), "Alpha")
        self.assertEqual(herdr_relay.workspace_label_cache.get("w1"), "Alpha")

    async def test_rename_workspace_missing_id_or_blank_label_no_herdr(self):
        cases = [
            {"type": "rename_workspace", "label": "Alpha"},
            {"type": "rename_workspace", "workspace_id": "", "label": "Alpha"},
            {"type": "rename_workspace", "workspace_id": "w1", "label": "   "},
            {"type": "rename_workspace", "workspace_id": "w1"},
        ]
        for msg in cases:
            _reset()
            ws = FakeWebSocket(incoming=[json.dumps(msg)])
            with mock.patch.object(
                herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
            ) as rc:
                await herdr_relay.handle_client(ws)
            rc.assert_not_awaited()
            types = [m["type"] for m in _non_snapshot(ws.sent_messages())]
            self.assertIn("error", types, f"应回 error: {msg}")
            self.assertNotIn("workspace_renamed", types)

    async def test_close_workspace_success_argv(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "close_workspace",
            "workspace_id": "w1",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
            return_value=(0, ""),
        ) as rc, mock.patch.object(herdr_relay, "audit") as audit:
            await herdr_relay.handle_client(ws)

        rc.assert_awaited_once_with("workspace", "close", "w1")
        audit.assert_called()
        closed = next(m for m in ws.sent_messages() if m.get("type") == "workspace_closed")
        self.assertTrue(closed["ok"])

    async def test_close_workspace_missing_id_or_nonzero_exit(self):
        _reset()
        ws = FakeWebSocket(incoming=[json.dumps({"type": "close_workspace"})])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
        ) as rc:
            await herdr_relay.handle_client(ws)
        rc.assert_not_awaited()
        types = [m["type"] for m in _non_snapshot(ws.sent_messages())]
        self.assertIn("error", types)
        self.assertNotIn("workspace_closed", types)

        _reset()
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "close_workspace",
            "workspace_id": "w1",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async", new_callable=mock.AsyncMock,
            return_value=(2, "fail"),
        ):
            await herdr_relay.handle_client(ws)
        types = [m["type"] for m in _non_snapshot(ws.sent_messages())]
        self.assertIn("error", types)
        self.assertNotIn("workspace_closed", types)


if __name__ == "__main__":
    unittest.main()
