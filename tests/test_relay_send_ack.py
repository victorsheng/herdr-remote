#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets", "zeroconf", "pywebpush"]
# ///
"""send_text / send_keys 必须回带 id 的 ack，web 才能在确认成功后再清输入框。"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class FakeWebSocket:
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
    herdr_relay.client_caps.clear()


class AttachRequestIdTests(unittest.TestCase):
    def test_echoes_short_string_id(self):
        payload = herdr_relay.attach_request_id({"type": "error", "message": "x"}, {"id": "s1"})
        self.assertEqual(payload["id"], "s1")

    def test_ignores_missing_or_oversized_id(self):
        self.assertNotIn("id", herdr_relay.attach_request_id({"ok": True}, {}))
        self.assertNotIn("id", herdr_relay.attach_request_id({"ok": True}, {"id": "x" * 65}))
        self.assertNotIn("id", herdr_relay.attach_request_id({"ok": True}, {"id": 12}))


class SendTextAckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset()
        herdr_relay.known_panes.add("w1:p1")
        herdr_relay.pane_remote_map["w1:p1"] = None
        herdr_relay.agent_cache["w1:p1"] = {"agent": "claude", "pane_id": "w1:p1"}

    async def test_send_text_success_acks_with_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "send_text", "pane_id": "w1:p1", "text": "hi", "id": "s1",
        })])
        with mock.patch.object(herdr_relay, "run_herdr_rc_async", AsyncMock(return_value=(0, ""))), \
             mock.patch.object(herdr_relay, "settle_after_paste", AsyncMock()) as settle, \
             mock.patch.object(herdr_relay, "audit"):
            await herdr_relay.handle_client(ws)
        settle.assert_awaited()
        result = next(m for m in ws.sent_messages() if m.get("type") == "command_result")
        self.assertEqual(result["command"], "send_text")
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "s1")

    async def test_send_text_nonzero_exit_errors_with_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "send_text", "pane_id": "w1:p1", "text": "hi", "id": "s1",
        })])
        with mock.patch.object(herdr_relay, "run_herdr_rc_async", AsyncMock(return_value=(1, ""))), \
             mock.patch.object(herdr_relay, "settle_after_paste", AsyncMock()) as settle, \
             mock.patch.object(herdr_relay, "audit"):
            await herdr_relay.handle_client(ws)
        settle.assert_not_awaited()
        err = next(m for m in ws.sent_messages() if m.get("type") == "error")
        self.assertEqual(err["message"], "send_text command failed")
        self.assertEqual(err["id"], "s1")

    async def test_send_keys_echoes_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "send_keys", "pane_id": "w1:p1", "keys": ["Enter"], "id": "s1",
        })])
        with mock.patch.object(herdr_relay, "run_herdr_rc_async", AsyncMock(return_value=(0, ""))), \
             mock.patch.object(herdr_relay, "audit"):
            await herdr_relay.handle_client(ws)
        result = next(m for m in ws.sent_messages() if m.get("type") == "command_result")
        self.assertEqual(result["command"], "send_keys")
        self.assertTrue(result["ok"])
        self.assertEqual(result["id"], "s1")

    async def test_send_keys_failure_errors_with_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "send_keys", "pane_id": "w1:p1", "keys": ["Enter"], "id": "s1",
        })])
        with mock.patch.object(herdr_relay, "run_herdr_rc_async", AsyncMock(return_value=(1, ""))), \
             mock.patch.object(herdr_relay, "audit"):
            await herdr_relay.handle_client(ws)
        err = next(m for m in ws.sent_messages() if m.get("type") == "error")
        self.assertEqual(err["message"], "send_keys command failed")
        self.assertEqual(err["id"], "s1")


if __name__ == "__main__":
    unittest.main()
