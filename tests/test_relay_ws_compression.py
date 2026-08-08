#!/usr/bin/env python3
"""HGZ1 binary gzip WebSocket compression codec and client-cap routing."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay

BIN_GZIP_CAP = "bin-gzip-v1"


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
        return [json.loads(s) for s in self.sent if isinstance(s, str)]


def _reset():
    herdr_relay.agent_cache.clear()
    herdr_relay.known_panes.clear()
    herdr_relay.pane_remote_map.clear()
    herdr_relay.clients.clear()
    herdr_relay.workspace_label_cache.clear()
    herdr_relay.client_caps.clear()


class Hgz1CodecTests(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_roundtrip_gzip(self):
        msg = {"type": "pane_content", "pane_id": "w1:p1", "content": "hello\n" * 200}
        frame = herdr_relay.encode_hgz1(msg)
        self.assertIsInstance(frame, (bytes, bytearray))
        self.assertTrue(frame.startswith(b"HGZ1"))
        out = herdr_relay.decode_hgz1(frame)
        self.assertEqual(out, msg)

    def test_header_type_matches(self):
        msg = {"type": "agents", "agents": []}
        frame = herdr_relay.encode_hgz1(msg)
        typ, flags, raw_len, payload = herdr_relay.parse_hgz1_header(frame)
        self.assertEqual(typ, "agents")
        self.assertEqual(flags & 1, 1)
        self.assertEqual(raw_len, len(json.dumps(msg, separators=(",", ":")).encode()))

    def test_bad_magic_raises(self):
        with self.assertRaises(ValueError):
            herdr_relay.decode_hgz1(b"XXXX\x00\x00\x00")

    def test_no_cap_always_text(self):
        msg = {"type": "pane_content", "pane_id": "x", "content": "y" * 5000}
        out = herdr_relay.encode_for_client(msg, caps=set())
        self.assertIsInstance(out, str)
        self.assertEqual(json.loads(out)["type"], "pane_content")

    def test_non_whitelist_always_text_even_with_cap(self):
        msg = {"type": "pong", "t": 1}
        out = herdr_relay.encode_for_client(msg, caps={"bin-gzip-v1"})
        self.assertIsInstance(out, str)

    def test_encode_prefers_text_when_not_smaller(self):
        msg = {"type": "agents", "agents": []}
        out = herdr_relay.encode_for_client(msg, caps={"bin-gzip-v1"})
        self.assertIsInstance(out, str)


class HelloAndBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset()

    async def test_hello_stores_caps(self):
        # disconnect 会 pop caps，所以在处理 ping→pong 时采样
        seen = []
        ws = FakeWebSocket(incoming=[
            json.dumps({"type": "hello", "caps": ["bin-gzip-v1"]}),
            json.dumps({"type": "ping", "t": 42}),
        ])
        orig = herdr_relay.send_to_client

        async def wrapped(w, msg):
            if msg.get("type") == "pong":
                seen.append(set(herdr_relay.client_caps.get(w, set())))
            return await orig(w, msg)

        with mock.patch.object(herdr_relay, "send_to_client", wrapped):
            await herdr_relay.handle_client(ws)
        self.assertTrue(seen, "应发出 pong")
        self.assertIn(BIN_GZIP_CAP, seen[0])

    async def test_broadcast_splits_capable_and_plain(self):
        plain = FakeWebSocket()
        cap = FakeWebSocket()
        herdr_relay.clients.add(plain)
        herdr_relay.clients.add(cap)
        herdr_relay.client_caps[plain] = set()
        herdr_relay.client_caps[cap] = {"bin-gzip-v1"}
        msg = {"type": "pane_content", "pane_id": "w", "content": ("line\n" * 400)}
        await herdr_relay.broadcast(msg)
        self.assertTrue(any(isinstance(s, str) for s in plain.sent))
        self.assertTrue(any(isinstance(s, (bytes, bytearray)) for s in cap.sent))


if __name__ == "__main__":
    unittest.main()
