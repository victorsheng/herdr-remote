#!/usr/bin/env python3
"""Application-level WebSocket ping/pong RTT probe."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

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


class PingPongHandlerTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()

    async def test_ping_echoes_t(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "ping", "t": 1723000000123,
        })])
        with mock.patch.object(herdr_relay, "audit") as audit:
            await herdr_relay.handle_client(ws)
        audit.assert_not_called()
        pong = next(m for m in ws.sent_messages() if m.get("type") == "pong")
        self.assertEqual(pong["t"], 1723000000123)

    async def test_ping_missing_t_still_pongs(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "ping"})])
        with mock.patch.object(herdr_relay, "audit") as audit:
            await herdr_relay.handle_client(ws)
        audit.assert_not_called()
        pong = next(m for m in ws.sent_messages() if m.get("type") == "pong")
        self.assertEqual(pong["t"], 0)


if __name__ == "__main__":
    unittest.main()
