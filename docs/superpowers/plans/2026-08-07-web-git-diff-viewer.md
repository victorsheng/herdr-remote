# Web Git Diff Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the Git status overlay, tap a changed file to open a second overlay showing unified diff (default) or full file content, with Working tree vs configurable base-branch modes.

**Architecture:** Extend the existing read-only git helpers in `relay/herdr_relay.py` with path-safe `git_diff` / `git_show` (and base-mode `git_status`). Web keeps on-demand WebSocket requests: enhance `showGitStatusDialog` into a clickable list + mode switch, then add a stacked file overlay with Diff/全文 toggle and +/- coloring.

**Tech Stack:** Python 3 asyncio relay (`relay/herdr_relay.py`), unittest, single-file Web (`web/index.html`)

**Spec:** `docs/superpowers/specs/2026-08-07-web-git-diff-viewer-design.md`

**Prerequisite:** Uncommitted Git status work (`relay/herdr_relay.py`, `web/index.html`, `tests/test_relay_git_status.py`) must land first (Task 0). Diff work builds on those helpers (`_resolve_git_target`, `fetch_git_status_async`, status overlay).

---

## File map

| File | Responsibility |
|------|----------------|
| `relay/herdr_relay.py` | Path sanitize, truncate, base detect, `fetch_git_diff_async`, `fetch_git_show_async`, base-mode status list, WS handlers |
| `tests/test_relay_git_status.py` | Keep existing status tests green after Task 0 |
| `tests/test_relay_git_diff.py` | New: sanitize, truncate, base detect, diff/show fetch mocks, WS branches |
| `web/index.html` | Clickable status list, mode/base UI, file overlay, `git_diff`/`git_show` handlers, diff coloring |

---

### Task 0: Land existing Git status WIP

**Files:**
- Existing (already modified/untracked): `relay/herdr_relay.py`, `web/index.html`, `tests/test_relay_workspace.py`, `tests/test_relay_git_status.py`

- [ ] **Step 1: Run status tests**

```bash
cd /Users/victor/code-github/herdr-remote
python -m unittest tests.test_relay_git_status tests.test_relay_workspace -v
```

Expected: PASS (fix any failures before continuing).

- [ ] **Step 2: Commit the Git status feature alone**

```bash
git add relay/herdr_relay.py web/index.html tests/test_relay_git_status.py tests/test_relay_workspace.py
git commit -m "$(cat <<'EOF'
feat(web/relay): read-only git status from Space and terminal

EOF
)"
```

---

### Task 1: Path sanitization helper (TDD)

**Files:**
- Create: `tests/test_relay_git_diff.py`
- Modify: `relay/herdr_relay.py` (near `_resolve_git_target` / `fetch_git_status_async`)

- [ ] **Step 1: Write failing tests**

Create `tests/test_relay_git_diff.py`:

```python
#!/usr/bin/env python3
"""git_diff / git_show helpers and WebSocket branches."""
import json
import sys
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.SanitizeGitPathTests -v
```

Expected: FAIL (`_sanitize_git_path` missing).

- [ ] **Step 3: Implement**

In `relay/herdr_relay.py` near the other git helpers:

```python
def _sanitize_git_path(path):
    """Return a safe repo-relative path, or None if rejected."""
    if path is None:
        return None
    p = str(path).strip().replace("\\", "/")
    if not p or p.startswith("/") or p.startswith("~"):
        return None
    if p.startswith("./"):
        p = p[2:]
    parts = [part for part in p.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)
```

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff.SanitizeGitPathTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): sanitize relative paths for git file viewers

EOF
)"
```

---

### Task 2: Truncation helper (TDD)

**Files:**
- Modify: `tests/test_relay_git_diff.py`
- Modify: `relay/herdr_relay.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_relay_git_diff.py`:

```python
class TruncateTextTests(unittest.TestCase):

    def test_under_limit_unchanged(self):
        text, truncated = herdr_relay._truncate_git_text("hello", limit=10)
        self.assertEqual(text, "hello")
        self.assertFalse(truncated)

    def test_over_limit_truncated(self):
        text, truncated = herdr_relay._truncate_git_text("abcdefghij", limit=4)
        self.assertEqual(text, "abcd")
        self.assertTrue(truncated)
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.TruncateTextTests -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

```python
GIT_TEXT_LIMIT = 200 * 1024  # ~200KB


def _truncate_git_text(text, limit=GIT_TEXT_LIMIT):
    raw = text if isinstance(text, str) else ""
    if len(raw.encode("utf-8", errors="replace")) <= limit:
        return raw, False
    # Truncate by UTF-8 bytes without splitting mid-character awkwardly:
    encoded = raw.encode("utf-8", errors="replace")[:limit]
    return encoded.decode("utf-8", errors="ignore"), True
```

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff.TruncateTextTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): truncate oversized git text payloads

EOF
)"
```

---

### Task 3: Base branch detection helper (TDD)

**Files:**
- Modify: `tests/test_relay_git_diff.py`
- Modify: `relay/herdr_relay.py`

- [ ] **Step 1: Add failing tests**

```python
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
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.ResolveBaseRefTests -v
```

Expected: FAIL.

- [ ] **Step 3: Implement pick + async detect**

```python
_BASE_CANDIDATES = ("origin/main", "main", "master")


def _pick_base_ref(existing):
    """Pick first candidate present in `existing` (ordered preference)."""
    have = set(existing or [])
    for name in _BASE_CANDIDATES:
        if name in have:
            return name
    return None


async def _run_git_async(cwd, remote, args, timeout=10):
    """Run git -C cwd … locally or via SSH. Returns (returncode, stdout, stderr)."""
    if remote:
        inner = "git -C " + shlex.quote(cwd) + " " + " ".join(shlex.quote(a) for a in args)
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, inner]
    else:
        cmd = ["git", "-C", cwd, *args]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return -1, b"", b"timeout"
    except Exception as e:
        return -1, b"", str(e).encode()
    return proc.returncode, stdout, stderr


async def detect_git_base_async(cwd, remote=None, timeout=10):
    found = []
    for cand in _BASE_CANDIDATES:
        rc, out, _ = await _run_git_async(
            cwd, remote, ["rev-parse", "--verify", cand], timeout=timeout)
        if rc == 0 and out.strip():
            found.append(cand)
    return _pick_base_ref(found)
```

(If `_run_git_async` would duplicate too much of `fetch_git_status_async`, refactor status to use it in a follow-up micro-step inside this task — keep behavior identical.)

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff.ResolveBaseRefTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): detect default git base ref for diffs

EOF
)"
```

---

### Task 4: `fetch_git_diff_async` (worktree + untracked) (TDD)

**Files:**
- Modify: `tests/test_relay_git_diff.py`
- Modify: `relay/herdr_relay.py`

- [ ] **Step 1: Add failing tests (mocked `_run_git_async`)**

```python
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
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.FetchGitDiffTests -v
```

Expected: FAIL.

- [ ] **Step 3: Implement `fetch_git_diff_async`**

```python
async def fetch_git_diff_async(cwd, path, mode="worktree", base="", remote=None, timeout=10):
    safe = _sanitize_git_path(path)
    if not safe:
        return {"ok": False, "message": "invalid path", "path": path or ""}
    if not cwd:
        return {"ok": False, "message": "cwd required", "path": safe}

    mode = (mode or "worktree").strip() or "worktree"
    resolved_base = ""
    if mode == "base":
        resolved_base = (base or "").strip() or await detect_git_base_async(
            cwd, remote=remote, timeout=timeout)
        if not resolved_base:
            return {"ok": False, "message": "could not resolve base branch", "path": safe}
        rc, out, err = await _run_git_async(
            cwd, remote, ["diff", resolved_base, "--", safe], timeout=timeout)
    else:
        mode = "worktree"
        rc, out, err = await _run_git_async(
            cwd, remote, ["diff", "HEAD", "--", safe], timeout=timeout)
        if rc == 0 and not out.strip():
            # Likely untracked: synthesize add diff via --no-index (exit 1 is normal)
            rc2, out2, err2 = await _run_git_async(
                cwd, remote,
                ["diff", "--no-index", "--", "/dev/null", safe],
                timeout=timeout,
            )
            if out2.strip():
                rc, out, err = rc2, out2, err2
            elif rc2 == -1:
                return {"ok": False, "message": err2.decode(errors="replace") or "diff failed",
                        "path": safe}

    if rc not in (0, 1) and not out.strip():
        # git diff returns 1 when --no-index finds differences; treat stdout as success
        msg = err.decode(errors="replace").strip() or "git diff failed"
        return {"ok": False, "message": msg, "path": safe}

    # Binary detection: git prints "Binary files … differ"
    text = out.decode(errors="replace")
    if "Binary files " in text and " differ" in text:
        return {"ok": False, "message": "binary file", "path": safe}

    text, truncated = _truncate_git_text(text)
    payload = {
        "ok": True,
        "path": safe,
        "mode": mode,
        "base": (base or "").strip(),
        "resolved_base": resolved_base if mode == "base" else "",
        "text": text or "(no diff)",
        "truncated": truncated,
        "cwd": cwd,
    }
    return payload
```

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff.FetchGitDiffTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): fetch per-file git diff for worktree and base

EOF
)"
```

---

### Task 5: `fetch_git_show_async` (full file) (TDD)

**Files:**
- Modify: `tests/test_relay_git_diff.py`
- Modify: `relay/herdr_relay.py`

- [ ] **Step 1: Add failing tests**

```python
class FetchGitShowTests(unittest.IsolatedAsyncioTestCase):

    async def test_rejects_bad_path(self):
        result = await herdr_relay.fetch_git_show_async("/tmp/repo", "/etc/passwd")
        self.assertFalse(result["ok"])

    async def test_reads_local_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.txt"
            p.write_text("hello\n世界\n", encoding="utf-8")
            result = await herdr_relay.fetch_git_show_async(td, "note.txt")
        self.assertTrue(result["ok"])
        self.assertIn("世界", result["text"])
        self.assertEqual(result["path"], "note.txt")
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.FetchGitShowTests -v
```

Expected: FAIL.

- [ ] **Step 3: Implement**

```python
async def fetch_git_show_async(cwd, path, remote=None, timeout=10):
    safe = _sanitize_git_path(path)
    if not safe:
        return {"ok": False, "message": "invalid path", "path": path or ""}
    if not cwd:
        return {"ok": False, "message": "cwd required", "path": safe}

    if remote:
        # Read file via SSH; refuse if NUL bytes suggest binary
        inner = "dd if=" + shlex.quote(f"{cwd.rstrip('/')}/{safe}") + " bs=1 count=" + str(GIT_TEXT_LIMIT + 1) + " 2>/dev/null"
        # Prefer simpler: python-less cat with head -c
        inner = "head -c " + str(GIT_TEXT_LIMIT + 1) + " " + shlex.quote(f"{cwd.rstrip('/')}/{safe}")
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, inner]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return {"ok": False, "message": "read timed out", "path": safe}
        except Exception as e:
            return {"ok": False, "message": f"read failed: {e}", "path": safe}
        if proc.returncode not in (0, None) and not stdout:
            return {"ok": False, "message": stderr.decode(errors="replace").strip() or "read failed",
                    "path": safe}
        data = stdout
    else:
        full = Path(cwd) / safe
        try:
            full = full.resolve()
            root = Path(cwd).resolve()
            if root not in full.parents and full != root:
                return {"ok": False, "message": "path escapes cwd", "path": safe}
            data = full.read_bytes()
        except FileNotFoundError:
            return {"ok": False, "message": "file not found", "path": safe}
        except Exception as e:
            return {"ok": False, "message": f"read failed: {e}", "path": safe}

    if b"\x00" in data[:8192]:
        return {"ok": False, "message": "binary file", "path": safe}

    text = data.decode("utf-8", errors="replace")
    text, truncated = _truncate_git_text(text)
    return {
        "ok": True,
        "path": safe,
        "text": text,
        "truncated": truncated,
        "cwd": cwd,
    }
```

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff.FetchGitShowTests -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): read working-tree file contents for git show

EOF
)"
```

---

### Task 6: WebSocket handlers for `git_diff` / `git_show` (TDD)

**Files:**
- Modify: `tests/test_relay_git_diff.py`
- Modify: `relay/herdr_relay.py` (`handle_client`, after `git_status` branch)

- [ ] **Step 1: Add failing handler tests**

Reuse the same `FakeWebSocket` / `_reset` pattern as `tests/test_relay_git_status.py` (copy the small helpers into `test_relay_git_diff.py` or import if you extract them — prefer copy to keep files independent).

```python
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
        # Handler may call fetch (which rejects) OR reject before — either ok;
        # assert client gets ok=false
        msg = next(m for m in ws.sent_messages() if m.get("type") == "git_diff")
        self.assertFalse(msg["ok"])
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.GitDiffHandlerTests -v
```

Expected: FAIL (no handler / no messages).

- [ ] **Step 3: Wire handlers after `git_status`**

```python
elif msg_type == "git_diff":
    pane_id = msg.get("pane_id", "")
    workspace_id = msg.get("workspace_id", "")
    path = msg.get("path", "")
    mode = msg.get("mode", "worktree")
    base = msg.get("base", "")
    cwd, remote, resolved_ws = _resolve_git_target(pane_id, workspace_id)
    if not cwd:
        await ws.send(json.dumps({
            "type": "git_diff", "ok": False,
            "message": "unknown pane or workspace, or cwd unavailable",
            "path": path or "",
        }))
        continue
    audit("git_diff", ip, device, pane_id or resolved_ws, f"cwd={cwd} path={path}")
    result = await fetch_git_diff_async(
        cwd, path, mode=mode, base=base, remote=remote)
    payload = {"type": "git_diff", **result}
    if pane_id:
        payload["pane_id"] = pane_id
    if resolved_ws:
        payload["workspace_id"] = resolved_ws
    await ws.send(json.dumps(payload))
elif msg_type == "git_show":
    pane_id = msg.get("pane_id", "")
    workspace_id = msg.get("workspace_id", "")
    path = msg.get("path", "")
    cwd, remote, resolved_ws = _resolve_git_target(pane_id, workspace_id)
    if not cwd:
        await ws.send(json.dumps({
            "type": "git_show", "ok": False,
            "message": "unknown pane or workspace, or cwd unavailable",
            "path": path or "",
        }))
        continue
    audit("git_show", ip, device, pane_id or resolved_ws, f"cwd={cwd} path={path}")
    result = await fetch_git_show_async(cwd, path, remote=remote)
    payload = {"type": "git_show", **result}
    if pane_id:
        payload["pane_id"] = pane_id
    if resolved_ws:
        payload["workspace_id"] = resolved_ws
    await ws.send(json.dumps(payload))
```

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff.GitDiffHandlerTests tests.test_relay_git_status -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): handle git_diff and git_show websocket messages

EOF
)"
```

---

### Task 7: Base-mode `git_status` list (TDD)

**Files:**
- Modify: `tests/test_relay_git_diff.py` (and/or `tests/test_relay_git_status.py`)
- Modify: `relay/herdr_relay.py` (`fetch_git_status_async` + handler)

- [ ] **Step 1: Add failing tests for name-status parse + fetch**

```python
class ParseNameStatusTests(unittest.TestCase):

    def test_parse_name_status(self):
        raw = "M\trelay/a.py\nA\tweb/b.html\n"
        files = herdr_relay._parse_git_name_status(raw)
        self.assertEqual(files, [
            {"status": "M", "path": "relay/a.py"},
            {"status": "A", "path": "web/b.html"},
        ])


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
```

- [ ] **Step 2: Run to verify fail**

```bash
python -m unittest tests.test_relay_git_diff.ParseNameStatusTests tests.test_relay_git_diff.FetchGitStatusBaseTests -v
```

Expected: FAIL.

- [ ] **Step 3: Implement parse + extend `fetch_git_status_async`**

```python
def _parse_git_name_status(raw):
    files = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        # format: STATUS<TAB>path  (or STATUS<TAB>old<TAB>new for renames)
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip() or "?"
        path = parts[-1]
        files.append({"status": status, "path": path})
    return files
```

Update `fetch_git_status_async` signature to accept `mode="worktree", base=""`:

- If `mode != "base"`: keep current porcelain behavior; optionally still attach `resolved_base` via detect when cheap — **skip** for YAGNI (only resolve when base mode or client asks).
- If `mode == "base"`:
  1. `resolved_base = base.strip() or await detect_git_base_async(...)`
  2. `_run_git_async(..., ["diff", "--name-status", resolved_base])`
  3. Build `files`, `clean`, `text` summary, return `resolved_base`

Update `git_status` handler to pass `mode=msg.get("mode","worktree")`, `base=msg.get("base","")`.

- [ ] **Step 4: Run to verify pass**

```bash
python -m unittest tests.test_relay_git_diff tests.test_relay_git_status -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_git_diff.py tests/test_relay_git_status.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): list files vs base branch in git_status

EOF
)"
```

---

### Task 8: Web — clickable status list + mode switch

**Files:**
- Modify: `web/index.html` (CSS near `.git-dialog`, JS `requestGitStatus` / `showGitStatusDialog`, message handler)

- [ ] **Step 1: Add state + CSS**

Near other git/workspace state:

```javascript
let gitStatusContext = { pane_id: '', workspace_id: '', mode: 'worktree', base: '', resolved_base: '' };
let gitFileCache = {}; // key → { diff?, show? }
```

CSS additions:

```css
.git-dialog { width:min(420px,calc(100% - 24px)); max-height:85vh; display:flex; flex-direction:column; }
.git-dialog .git-mode { display:flex; gap:6px; margin:0 0 8px; }
.git-dialog .git-mode button { flex:1; text-align:center; padding:8px; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--muted); font-size:0.82rem; }
.git-dialog .git-mode button.active { background:var(--blue); color:#fff; border-color:var(--blue); }
.git-dialog .git-base-row { display:flex; gap:6px; margin:0 0 8px; }
.git-dialog .git-base-row input { flex:1; margin:0; }
.git-dialog .git-file-list { list-style:none; margin:0; padding:0; max-height:45vh; overflow:auto; }
.git-dialog .git-file-list li { display:flex; gap:8px; padding:10px 8px; border-bottom:1px solid var(--border); cursor:pointer; font-family:'SF Mono','Menlo',monospace; font-size:0.78rem; }
.git-dialog .git-file-list li:active { background:color-mix(in srgb, var(--text) 8%, transparent); }
.git-dialog .git-file-list .st { color:var(--orange); flex-shrink:0; min-width:2.2em; }
```

- [ ] **Step 2: Update request/show**

Replace `requestGitStatus` / `showGitStatusDialog` so that:

1. `requestGitStatus` stores context and sends `{type:'git_status', pane_id|workspace_id, mode, base}`.
2. `showGitStatusDialog` renders mode buttons, optional base input (when `mode==='base'`), clickable `files` rows calling `openGitFile(path)`, and keeps Close.

Example file row onclick (escape path carefully with `esc` for HTML and JS string encode):

```javascript
function openGitFile(path) {
  if (!ws || !path) return;
  gitFileCache = {};
  const payload = {
    type: 'git_diff',
    path,
    mode: gitStatusContext.mode || 'worktree',
    base: gitStatusContext.base || '',
  };
  if (gitStatusContext.pane_id || activePane) payload.pane_id = gitStatusContext.pane_id || activePane;
  else if (gitStatusContext.workspace_id) payload.workspace_id = gitStatusContext.workspace_id;
  showGitFileDialog({ loading: true, path, view: 'diff' });
  ws.send(JSON.stringify(payload));
}
```

Mode switch should update `gitStatusContext.mode` / `base` and re-call `requestGitStatus`.

When `msg.type === 'git_status'`, also store `resolved_base` into `gitStatusContext`.

- [ ] **Step 3: Manual smoke (relay running)**

Open web → terminal → Git status → confirm file rows appear and mode toggle re-fetches.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): clickable git status list with worktree/base modes

EOF
)"
```

---

### Task 9: Web — file overlay with Diff / 全文

**Files:**
- Modify: `web/index.html`

- [ ] **Step 1: Add CSS for diff lines + file dialog**

```css
.git-file-dialog pre { margin:0; max-height:55vh; overflow:auto; padding:10px; border-radius:8px; background:var(--term-bg); color:var(--term-text); font-family:'SF Mono','Menlo',monospace; font-size:0.72rem; line-height:1.35; white-space:pre; overflow-wrap:normal; }
.git-file-dialog .diff-add { color:var(--green); }
.git-file-dialog .diff-del { color:var(--red); }
.git-file-dialog .diff-hunk { color:var(--blue); }
.git-dialog .git-view-toggle { display:flex; gap:6px; margin:0 0 8px; }
.git-dialog .git-view-toggle button.active { background:var(--blue); color:#fff; }
```

- [ ] **Step 2: Implement colorize + dialog + WS handlers**

```javascript
function colorizeDiffText(text) {
  return esc(text || '').split('\n').map(line => {
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-add">${line}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-del">${line}</span>`;
    if (line.startsWith('@@')) return `<span class="diff-hunk">${line}</span>`;
    return line;
  }).join('\n');
}

function showGitFileDialog(state) {
  // state: { path, view:'diff'|'full', ok?, text?, message?, truncated?, loading?, mode?, resolved_base? }
  document.querySelectorAll('.git-file-overlay').forEach(el => el.remove());
  const ov = document.createElement('div');
  ov.className = 'space-overlay git-overlay git-file-overlay';
  ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  const view = state.view || 'diff';
  let body;
  if (state.loading) body = `<p style="color:var(--muted)">Loading…</p>`;
  else if (state.ok === false) body = `<p style="color:var(--red)">${esc(state.message || 'failed')}</p>`;
  else {
    const pre = view === 'diff'
      ? `<pre>${colorizeDiffText(state.text)}</pre>`
      : `<pre>${esc(state.text || '')}</pre>`;
    const trunc = state.truncated ? `<div class="meta">… truncated</div>` : '';
    body = pre + trunc;
  }
  ov.innerHTML = `<div class="space-dialog git-dialog git-file-dialog" role="dialog" aria-label="Git file">
    <h3>${esc(state.path || '')}</h3>
    <div class="meta">${esc((state.mode || gitStatusContext.mode || 'worktree') + (state.resolved_base ? ' · ' + state.resolved_base : ''))}</div>
    <div class="git-view-toggle">
      <button type="button" class="${view==='diff'?'active':''}" onclick="switchGitFileView('diff')">Diff</button>
      <button type="button" class="${view==='full'?'active':''}" onclick="switchGitFileView('full')">全文</button>
    </div>
    ${body}
    <div class="row"><button type="button" class="primary" onclick="this.closest('.git-file-overlay').remove()">Close</button></div>
  </div>`;
  document.body.appendChild(ov);
  window._gitFileState = state;
}

function switchGitFileView(view) {
  const st = window._gitFileState || {};
  const path = st.path;
  if (!path || !ws) return;
  if (view === 'diff') {
    if (gitFileCache.diff) {
      showGitFileDialog({ ...gitFileCache.diff, view: 'diff' });
      return;
    }
    openGitFile(path);
    return;
  }
  // full
  if (gitFileCache.show) {
    showGitFileDialog({ ...gitFileCache.show, view: 'full' });
    return;
  }
  showGitFileDialog({ ...st, loading: true, view: 'full' });
  const payload = { type: 'git_show', path };
  if (gitStatusContext.pane_id || activePane) payload.pane_id = gitStatusContext.pane_id || activePane;
  else if (gitStatusContext.workspace_id) payload.workspace_id = gitStatusContext.workspace_id;
  ws.send(JSON.stringify(payload));
}
```

In the WS `onmessage` branch:

```javascript
} else if (msg.type === 'git_diff') {
  gitFileCache.diff = { ...msg, view: 'diff' };
  showGitFileDialog({ ...msg, view: 'diff' });
} else if (msg.type === 'git_show') {
  gitFileCache.show = { ...msg, view: 'full' };
  showGitFileDialog({ ...msg, view: 'full' });
}
```

Clear `gitFileCache` when mode/base changes or status reopens.

- [ ] **Step 3: Manual check**

1. Status → tap file → see colored diff  
2. Toggle 全文 → see file body  
3. Toggle Diff → reuse cache  
4. Switch to vs base → edit base → reopen file  
5. Untracked file → add-style diff + 全文 works  

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): per-file git diff overlay with full-content toggle

EOF
)"
```

---

### Task 10: Final verification

- [ ] **Step 1: Run all related unit tests**

```bash
python -m unittest tests.test_relay_git_diff tests.test_relay_git_status tests.test_relay_workspace -v
```

Expected: all PASS.

- [ ] **Step 2: Spec coverage checklist**

Confirm each spec item is done:

| Spec item | Task |
|-----------|------|
| On-demand `git_diff` / `git_show` | 4–6 |
| worktree + base modes | 4, 7–8 |
| Auto base + UI override | 3, 7–8 |
| Tap file → second overlay | 9 |
| Unified +/- coloring | 9 |
| Diff ↔ 全文 toggle | 9 |
| Truncation / binary / path safety | 1–2, 4–5 |
| Tests | 1–7, 10 |

- [ ] **Step 3: Commit any leftover fixes only if needed**

No empty commit. If fixes were required:

```bash
git add -u
git commit -m "$(cat <<'EOF'
fix: polish git diff viewer edge cases

EOF
)"
```

---

## Self-review (plan vs spec)

1. **Spec coverage:** Working tree + base, clickable list, second overlay, unified coloring, 全文 toggle, on-demand fetch, truncate 200KB, binary reject, path sanitize, untracked via `--no-index`, audit, tests — all mapped to tasks. Side-by-side / edit / commit remain out of scope.
2. **Placeholders:** None intentional; Task 8–9 include concrete JS/CSS. Implementers must adapt line anchors if WIP shifts.
3. **Consistency:** Message types `git_diff` / `git_show` / extended `git_status` match the design spec; payload fields `mode`, `base`, `resolved_base`, `truncated` used consistently across tasks.
