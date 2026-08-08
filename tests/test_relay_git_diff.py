#!/usr/bin/env python3
"""git_diff / git_show helpers and WebSocket branches."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class SanitizeGitPathTests(unittest.TestCase):

    def test_accepts_relative_file(self):
        self.assertEqual(herdr_relay._sanitize_git_path("src/a.py"), "src/a.py")

    def test_rejects_absolute(self):
        self.assertIsNone(herdr_relay._sanitize_git_path("/etc/passwd"))

    def test_rejects_dotdot(self):
        self.assertIsNone(herdr_relay._sanitize_git_path("../secret"))

    def test_rejects_empty(self):
        self.assertIsNone(herdr_relay._sanitize_git_path(""))
        self.assertIsNone(herdr_relay._sanitize_git_path("   "))

    def test_strips_leading_dot_slash(self):
        self.assertEqual(herdr_relay._sanitize_git_path("./foo/bar"), "foo/bar")


class TruncateTextTests(unittest.TestCase):

    def test_under_limit_unchanged(self):
        text, truncated = herdr_relay._truncate_git_text("hello", limit=10)
        self.assertEqual(text, "hello")
        self.assertFalse(truncated)

    def test_over_limit_truncated(self):
        text, truncated = herdr_relay._truncate_git_text("abcdefghij", limit=4)
        self.assertEqual(text, "abcd")
        self.assertTrue(truncated)


class ResolveBaseRefTests(unittest.TestCase):

    def test_prefers_origin_main(self):
        self.assertEqual(
            herdr_relay._pick_base_ref(["origin/main", "main", "master"]),
            "origin/main",
        )

    def test_falls_back_to_main(self):
        self.assertEqual(
            herdr_relay._pick_base_ref(["main", "master"]),
            "main",
        )

    def test_none_when_empty(self):
        self.assertIsNone(herdr_relay._pick_base_ref([]))


class ParseNameStatusTests(unittest.TestCase):

    def test_parse_name_status(self):
        raw = "M\trelay/a.py\nA\tweb/b.html\n"
        files = herdr_relay._parse_git_name_status(raw)
        self.assertEqual(files, [
            {"status": "M", "path": "relay/a.py"},
            {"status": "A", "path": "web/b.html"},
        ])


class FetchGitDiffTests(unittest.IsolatedAsyncioTestCase):

    async def test_rejects_bad_path(self):
        result = await herdr_relay.fetch_git_diff_async("/tmp/repo", "../x", mode="worktree")
        self.assertFalse(result["ok"])
        self.assertIn("path", result["message"].lower())

    async def test_worktree_diff_ok(self):
        async def fake_run(cwd, remote, args, timeout=10):
            if args[:2] == ["diff", "HEAD"]:
                return 0, b"diff --git a/a.py b/a.py\n+hi\n", b""
            return 1, b"", b"nope"

        with mock.patch.object(herdr_relay, "_run_git_async", side_effect=fake_run):
            result = await herdr_relay.fetch_git_diff_async(
                "/tmp/repo", "a.py", mode="worktree")
        self.assertTrue(result["ok"])
        self.assertIn("+hi", result["text"])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["mode"], "worktree")
        self.assertEqual(result["path"], "a.py")

    async def test_untracked_uses_no_index(self):
        calls = []

        async def fake_run(cwd, remote, args, timeout=10):
            calls.append(args)
            if args[:3] == ["diff", "HEAD", "--"]:
                return 0, b"", b""  # empty → try untracked
            if args[0] == "diff" and "--no-index" in args:
                return 1, b"diff --git a/ /dev/null b/new.txt\n+new\n", b""
            return 1, b"", b"err"

        with mock.patch.object(herdr_relay, "_run_git_async", side_effect=fake_run):
            result = await herdr_relay.fetch_git_diff_async(
                "/tmp/repo", "new.txt", mode="worktree")
        self.assertTrue(result["ok"])
        self.assertIn("+new", result["text"])
        self.assertTrue(any("--no-index" in c for c in calls))

    async def test_untracked_hint_skips_head_diff(self):
        calls = []

        async def fake_run(cwd, remote, args, timeout=10):
            calls.append(args)
            if args[0] == "diff" and "--no-index" in args:
                return 1, b"diff --git a/ /dev/null b/new.txt\n+new\n", b""
            return 1, b"", b"should not call HEAD"

        with mock.patch.object(herdr_relay, "_run_git_async", side_effect=fake_run):
            result = await herdr_relay.fetch_git_diff_async(
                "/tmp/repo", "new.txt", mode="worktree", untracked=True)
        self.assertTrue(result["ok"])
        self.assertIn("+new", result["text"])
        self.assertEqual(len(calls), 1)
        self.assertIn("--no-index", calls[0])
        self.assertNotIn("HEAD", calls[0])

    async def test_source_mentioning_binary_is_not_rejected(self):
        blob = (
            b'diff --git a/relay/herdr_relay.py b/relay/herdr_relay.py\n'
            b'+    if "Binary files " in text and " differ" in text:\n'
        )

        async def fake_run(cwd, remote, args, timeout=10):
            if args[:2] == ["diff", "HEAD"]:
                return 0, blob, b""
            return 1, b"", b"nope"

        with mock.patch.object(herdr_relay, "_run_git_async", side_effect=fake_run):
            result = await herdr_relay.fetch_git_diff_async(
                "/tmp/repo", "relay/herdr_relay.py", mode="worktree")
        self.assertTrue(result["ok"], result)

    async def test_real_binary_notice_rejected(self):
        blob = b"diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n"

        async def fake_run(cwd, remote, args, timeout=10):
            if args[:2] == ["diff", "HEAD"]:
                return 0, blob, b""
            return 1, b"", b"nope"

        with mock.patch.object(herdr_relay, "_run_git_async", side_effect=fake_run):
            result = await herdr_relay.fetch_git_diff_async(
                "/tmp/repo", "x.bin", mode="worktree")
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "binary file")


class FetchGitShowTests(unittest.IsolatedAsyncioTestCase):

    async def test_rejects_bad_path(self):
        result = await herdr_relay.fetch_git_show_async("/tmp/repo", "/etc/passwd")
        self.assertFalse(result["ok"])

    async def test_reads_local_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.txt"
            p.write_text("hello\n世界\n", encoding="utf-8")
            result = await herdr_relay.fetch_git_show_async(td, "note.txt")
        self.assertTrue(result["ok"])
        self.assertIn("世界", result["text"])
        self.assertEqual(result["path"], "note.txt")


class FetchGitStatusBaseTests(unittest.IsolatedAsyncioTestCase):

    async def test_base_mode_uses_name_status(self):
        async def fake_run(cwd, remote, args, timeout=10):
            if args[:2] == ["rev-parse", "--verify"]:
                return (0, b"abc\n", b"") if args[2] == "origin/main" else (1, b"", b"")
            if args[0] == "diff" and args[1] == "--name-status":
                return 0, b"M\ta.py\n", b""
            return 1, b"", b""

        with mock.patch.object(herdr_relay, "_run_git_async", side_effect=fake_run):
            result = await herdr_relay.fetch_git_status_async(
                "/tmp/repo", remote=None, mode="base", base="")
        self.assertTrue(result["ok"])
        self.assertEqual(result["resolved_base"], "origin/main")
        self.assertEqual(result["files"], [{"status": "M", "path": "a.py"}])
        self.assertFalse(result["clean"])


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


class GitDiffHandlerTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()
        herdr_relay.known_panes.add("w1:p1")
        herdr_relay.agent_cache["w1:p1"] = {
            "pane_id": "w1:p1",
            "cwd": "/tmp/repo",
            "workspace_id": "w1",
        }

    async def test_git_diff_by_pane(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "git_diff", "pane_id": "w1:p1", "path": "a.py", "mode": "worktree",
        })])
        with mock.patch.object(
            herdr_relay, "fetch_git_diff_async", new_callable=mock.AsyncMock,
            return_value={"ok": True, "path": "a.py", "mode": "worktree", "text": "+x",
                          "truncated": False, "base": "", "resolved_base": "", "cwd": "/tmp/repo"},
        ) as fetch:
            await herdr_relay.handle_client(ws)
        fetch.assert_awaited_once()
        msg = next(m for m in ws.sent_messages() if m.get("type") == "git_diff")
        self.assertTrue(msg["ok"])
        self.assertEqual(msg["path"], "a.py")

    async def test_git_show_by_pane(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "git_show", "pane_id": "w1:p1", "path": "a.py",
        })])
        with mock.patch.object(
            herdr_relay, "fetch_git_show_async", new_callable=mock.AsyncMock,
            return_value={"ok": True, "path": "a.py", "text": "hi", "truncated": False, "cwd": "/tmp/repo"},
        ) as fetch:
            await herdr_relay.handle_client(ws)
        fetch.assert_awaited_once()
        msg = next(m for m in ws.sent_messages() if m.get("type") == "git_show")
        self.assertTrue(msg["ok"])

    async def test_git_diff_bad_path_short_circuits(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "git_diff", "pane_id": "w1:p1", "path": "../x",
        })])
        with mock.patch.object(
            herdr_relay, "fetch_git_diff_async", new_callable=mock.AsyncMock,
        ) as fetch:
            await herdr_relay.handle_client(ws)
        fetch.assert_not_awaited()
        msg = next(m for m in ws.sent_messages() if m.get("type") == "git_diff")
        self.assertFalse(msg["ok"])


if __name__ == "__main__":
    unittest.main()
