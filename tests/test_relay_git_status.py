#!/usr/bin/env python3
"""git_status WebSocket 分支与 porcelain 解析测试。"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class GitStatusParseTests(unittest.TestCase):

    def test_parse_clean_repo(self):
        raw = "## main...origin/main\n"
        parsed = herdr_relay._parse_git_porcelain(raw)
        self.assertTrue(parsed["clean"])
        self.assertEqual(parsed["branch"], "main...origin/main")
        self.assertEqual(parsed["files"], [])

    def test_parse_dirty_repo(self):
        raw = "## feat/test...origin/feat/test [ahead 2]\n M relay/herdr_relay.py\n?? web/new.html\n"
        parsed = herdr_relay._parse_git_porcelain(raw)
        self.assertFalse(parsed["clean"])
        self.assertEqual(parsed["branch"], "feat/test...origin/feat/test [ahead 2]")
        self.assertEqual(parsed["files"], [
            {"status": "M", "path": "relay/herdr_relay.py"},
            {"status": "??", "path": "web/new.html"},
        ])


class ResolveGitTargetTests(unittest.TestCase):

    def setUp(self):
        herdr_relay.known_panes.clear()
        herdr_relay.agent_cache.clear()
        herdr_relay.pane_remote_map.clear()

    def test_resolve_by_pane_id(self):
        herdr_relay.known_panes.add("w1:p1")
        herdr_relay.agent_cache["w1:p1"] = {
            "pane_id": "w1:p1",
            "cwd": "/tmp/repo",
            "workspace_id": "w1",
        }
        herdr_relay.pane_remote_map["w1:p1"] = "devbox"
        cwd, remote, ws = herdr_relay._resolve_git_target(pane_id="w1:p1")
        self.assertEqual(cwd, "/tmp/repo")
        self.assertEqual(remote, "devbox")
        self.assertEqual(ws, "w1")

    def test_resolve_by_workspace_id(self):
        herdr_relay.known_panes.update({"w1:p1", "w2:p1"})
        herdr_relay.agent_cache["w1:p1"] = {
            "pane_id": "w1:p1",
            "cwd": "/tmp/a",
            "workspace_id": "w1",
        }
        herdr_relay.agent_cache["w2:p1"] = {
            "pane_id": "w2:p1",
            "cwd": "/tmp/b",
            "workspace_id": "w2",
        }
        cwd, remote, ws = herdr_relay._resolve_git_target(workspace_id="w2")
        self.assertEqual(cwd, "/tmp/b")
        self.assertIsNone(remote)
        self.assertEqual(ws, "w2")

    def test_unknown_pane_returns_empty(self):
        cwd, remote, ws = herdr_relay._resolve_git_target(pane_id="nope")
        self.assertIsNone(cwd)
        self.assertIsNone(remote)
        self.assertEqual(ws, "")


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


class GitStatusHandlerTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()
        herdr_relay.known_panes.add("w1:p1")
        herdr_relay.agent_cache["w1:p1"] = {
            "pane_id": "w1:p1",
            "cwd": "/tmp/repo",
            "workspace_id": "w1",
        }

    async def test_git_status_by_pane_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "git_status", "pane_id": "w1:p1"})])
        payload = {
            "ok": True,
            "clean": False,
            "branch": "main...origin/main",
            "files": [{"status": "M", "path": "a.py"}],
            "text": " M a.py",
            "cwd": "/tmp/repo",
            "workspace_id": "w1",
        }
        with mock.patch.object(
            herdr_relay, "fetch_git_status_async", new_callable=mock.AsyncMock,
            return_value=payload,
        ) as fetch, mock.patch.object(herdr_relay, "audit") as audit:
            await herdr_relay.handle_client(ws)

        fetch.assert_awaited_once()
        args, kwargs = fetch.await_args
        self.assertEqual(args[0], "/tmp/repo")
        self.assertIsNone(kwargs.get("remote"))
        audit.assert_called()
        msg = next(m for m in ws.sent_messages() if m.get("type") == "git_status")
        self.assertTrue(msg["ok"])
        self.assertFalse(msg["clean"])
        self.assertEqual(msg["cwd"], "/tmp/repo")

    async def test_git_status_unknown_pane_errors(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "git_status", "pane_id": "nope"})])
        with mock.patch.object(
            herdr_relay, "fetch_git_status_async", new_callable=mock.AsyncMock,
        ) as fetch:
            await herdr_relay.handle_client(ws)
        fetch.assert_not_awaited()
        msg = next(m for m in ws.sent_messages() if m.get("type") == "git_status")
        self.assertFalse(msg["ok"])


if __name__ == "__main__":
    unittest.main()
