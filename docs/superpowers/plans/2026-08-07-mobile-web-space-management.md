# Mobile Web Space Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the mobile Web client create, rename, and close Spaces via the relay, mirroring the existing `create_tab` flow.

**Architecture:** Add three WebSocket message handlers in `relay/herdr_relay.py` that call `herdr workspace create|rename|close`. Update `web/index.html` so the Spaces chip strip is always visible (with `+`), and when a Space is selected show `⋯` → Rename / Close modals. Clear selection only after a successful `workspace_closed` reply.

**Tech Stack:** Python 3 + `unittest` (relay), single-file Web app (`web/index.html`), herdr CLI over subprocess.

**Spec:** `docs/superpowers/specs/2026-08-07-mobile-web-space-management-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `tests/test_relay_workspace.py` | Relay WS handlers for create / rename / close (TDD) |
| `relay/herdr_relay.py` | Implement the three `elif msg_type == ...` branches next to `create_tab` |
| `web/index.html` | Always-on Spaces strip, `+`, `⋯` sheet, rename/close modals, WS send/receive |

---

### Task 1: Relay `create_workspace` (TDD)

**Files:**
- Create: `tests/test_relay_workspace.py`
- Modify: `relay/herdr_relay.py` (after the `create_tab` branch ~line 960)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_relay_workspace.py`:

```python
#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets", "zeroconf", "pywebpush"]
# ///
"""create_workspace / rename_workspace / close_workspace relay handlers."""
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


class CreateWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset()

    async def test_create_workspace_calls_herdr_with_focus(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "create_workspace"})])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(0, "")),
        ) as run:
            await herdr_relay.handle_client(ws)
        run.assert_awaited()
        args = run.await_args.args
        self.assertEqual(args[:2], ("workspace", "create"))
        self.assertIn("--focus", args)
        types = [m["type"] for m in ws.sent_messages()]
        self.assertIn("workspace_created", types)
        created = next(m for m in ws.sent_messages() if m["type"] == "workspace_created")
        self.assertTrue(created["ok"])

    async def test_create_workspace_herdr_failure_returns_error(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "create_workspace"})])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(1, "")),
        ):
            await herdr_relay.handle_client(ws)
        types = [m["type"] for m in ws.sent_messages()]
        self.assertNotIn("workspace_created", types)
        self.assertIn("error", types)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/victor/code-github/herdr-remote && python3 -m unittest tests.test_relay_workspace -v`

Expected: FAIL (no `create_workspace` handler / no `workspace_created`)

- [ ] **Step 3: Implement `create_workspace` in relay**

In `relay/herdr_relay.py`, insert **before** `elif msg_type == "push_subscribe":` (immediately after the `create_tab` block):

```python
            elif msg_type == "create_workspace":
                log.info("Create workspace from %s (%s)", ip, device)
                audit("create_workspace", ip, device, "", "")
                try:
                    returncode, _ = await run_herdr_rc_async(
                        "workspace", "create", "--focus")
                except Exception as e:
                    log.warning("create_workspace failed: %s", e)
                    await ws.send(json.dumps({
                        "type": "error", "message": "create_workspace command failed"}))
                    continue
                if returncode != 0:
                    log.warning("create_workspace failed with exit %s", returncode)
                    await ws.send(json.dumps({
                        "type": "error", "message": "create_workspace command failed"}))
                    continue
                await ws.send(json.dumps({"type": "workspace_created", "ok": True}))
```

Note: use `run_herdr_rc_async` (not fire-and-forget `run_herdr_async`) so the client only gets `ok: true` when herdr exits 0 — required by close UX in the same feature family.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_relay_workspace -v`

Expected: `CreateWorkspaceTests` PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_workspace.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): add create_workspace WebSocket handler

Allow mobile web to create a Space via herdr workspace create --focus.
EOF
)"
```

---

### Task 2: Relay `rename_workspace` (TDD)

**Files:**
- Modify: `tests/test_relay_workspace.py`
- Modify: `relay/herdr_relay.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_relay_workspace.py` (before `if __name__`):

```python
class RenameWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset()

    async def test_rename_workspace_calls_herdr(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "rename_workspace",
            "workspace_id": "w1",
            "label": "My Space",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(0, "")),
        ) as run:
            await herdr_relay.handle_client(ws)
        args = run.await_args.args
        self.assertEqual(args, ("workspace", "rename", "w1", "My Space"))
        renamed = next(m for m in ws.sent_messages() if m["type"] == "workspace_renamed")
        self.assertTrue(renamed["ok"])

    async def test_rename_requires_workspace_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "rename_workspace", "label": "x",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(0, "")),
        ) as run:
            await herdr_relay.handle_client(ws)
        run.assert_not_awaited()
        err = next(m for m in ws.sent_messages() if m["type"] == "error")
        self.assertIn("workspace_id", err["message"])

    async def test_rename_requires_non_empty_label(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "rename_workspace",
            "workspace_id": "w1",
            "label": "  ",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(0, "")),
        ) as run:
            await herdr_relay.handle_client(ws)
        run.assert_not_awaited()
        err = next(m for m in ws.sent_messages() if m["type"] == "error")
        self.assertIn("label", err["message"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_relay_workspace.RenameWorkspaceTests -v`

Expected: FAIL (no handler)

- [ ] **Step 3: Implement `rename_workspace`**

Insert after the `create_workspace` branch:

```python
            elif msg_type == "rename_workspace":
                workspace_id = (msg.get("workspace_id") or "").strip()
                label = (msg.get("label") or "").strip()
                if not workspace_id:
                    await ws.send(json.dumps({
                        "type": "error", "message": "workspace_id required"}))
                    continue
                if not label:
                    await ws.send(json.dumps({
                        "type": "error", "message": "label required"}))
                    continue
                log.info("Rename workspace from %s (%s): id=%s label=%s",
                         ip, device, workspace_id, label)
                audit("rename_workspace", ip, device, "",
                      f"workspace={workspace_id} label={label}")
                try:
                    returncode, _ = await run_herdr_rc_async(
                        "workspace", "rename", workspace_id, label)
                except Exception as e:
                    log.warning("rename_workspace failed: %s", e)
                    await ws.send(json.dumps({
                        "type": "error", "message": "rename_workspace command failed"}))
                    continue
                if returncode != 0:
                    log.warning("rename_workspace failed with exit %s", returncode)
                    await ws.send(json.dumps({
                        "type": "error", "message": "rename_workspace command failed"}))
                    continue
                await ws.send(json.dumps({"type": "workspace_renamed", "ok": True}))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m unittest tests.test_relay_workspace.RenameWorkspaceTests -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_workspace.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): add rename_workspace WebSocket handler

Validate workspace_id and non-empty label, then call herdr workspace rename.
EOF
)"
```

---

### Task 3: Relay `close_workspace` (TDD)

**Files:**
- Modify: `tests/test_relay_workspace.py`
- Modify: `relay/herdr_relay.py`

- [ ] **Step 1: Write the failing tests**

Append:

```python
class CloseWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset()

    async def test_close_workspace_calls_herdr(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "close_workspace", "workspace_id": "w1",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(0, "")),
        ) as run:
            await herdr_relay.handle_client(ws)
        self.assertEqual(run.await_args.args, ("workspace", "close", "w1"))
        closed = next(m for m in ws.sent_messages() if m["type"] == "workspace_closed")
        self.assertTrue(closed["ok"])

    async def test_close_requires_workspace_id(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "close_workspace"})])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(0, "")),
        ) as run:
            await herdr_relay.handle_client(ws)
        run.assert_not_awaited()
        err = next(m for m in ws.sent_messages() if m["type"] == "error")
        self.assertIn("workspace_id", err["message"])

    async def test_close_herdr_failure_returns_error(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "close_workspace", "workspace_id": "w1",
        })])
        with mock.patch.object(
            herdr_relay, "run_herdr_rc_async",
            new=mock.AsyncMock(return_value=(1, "")),
        ):
            await herdr_relay.handle_client(ws)
        types = [m["type"] for m in ws.sent_messages()]
        self.assertNotIn("workspace_closed", types)
        self.assertIn("error", types)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m unittest tests.test_relay_workspace.CloseWorkspaceTests -v`

Expected: FAIL

- [ ] **Step 3: Implement `close_workspace`**

Insert after `rename_workspace`:

```python
            elif msg_type == "close_workspace":
                workspace_id = (msg.get("workspace_id") or "").strip()
                if not workspace_id:
                    await ws.send(json.dumps({
                        "type": "error", "message": "workspace_id required"}))
                    continue
                log.info("Close workspace from %s (%s): id=%s", ip, device, workspace_id)
                audit("close_workspace", ip, device, "", f"workspace={workspace_id}")
                try:
                    returncode, _ = await run_herdr_rc_async(
                        "workspace", "close", workspace_id)
                except Exception as e:
                    log.warning("close_workspace failed: %s", e)
                    await ws.send(json.dumps({
                        "type": "error", "message": "close_workspace command failed"}))
                    continue
                if returncode != 0:
                    log.warning("close_workspace failed with exit %s", returncode)
                    await ws.send(json.dumps({
                        "type": "error", "message": "close_workspace command failed"}))
                    continue
                await ws.send(json.dumps({"type": "workspace_closed", "ok": True}))
```

- [ ] **Step 4: Run full workspace test module**

Run: `python3 -m unittest tests.test_relay_workspace -v`

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_workspace.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): add close_workspace WebSocket handler

Gate success on herdr exit code so the web client can clear selection safely.
EOF
)"
```

---

### Task 4: Web — always show Spaces strip + create

**Files:**
- Modify: `web/index.html` (`render`, `renderWorkspaces`, add `createWorkspace`)

- [ ] **Step 1: Stop hiding the strip when ≤1 Space**

Replace the early-return in `render()`:

```javascript
function render() {
  document.getElementById('agentCount').textContent = agents.length ? `${agents.length}` : '';
  const workspaces = [...new Set(agents.map(a => a.workspace_id).filter(Boolean))];
  // Spec: Spaces strip always visible (0 / 1 / many) so + Space is reachable.
  if (activeWorkspace && !workspaces.includes(activeWorkspace)) {
    activeWorkspace = null;
    activeTab = null;
  }
  renderWorkspaces(workspaces);
}
```

- [ ] **Step 2: Update `renderWorkspaces` chip strip**

Replace the Spaces chip-strip block inside `renderWorkspaces` so that:

- 0 Spaces → only `+`
- 1+ Spaces → `All` + chips + `+`
- selected Space → also `⋯` (full handlers in Task 5)

```javascript
  // Space chip strip (always)
  html += `<div class="chip-strip"><span class="chip-label">Spaces</span>`;
  if (workspaces.length >= 1) {
    html += `<button class="chip${activeWorkspace===null?' active':''}" onclick="backToWorkspaces()">All</button>`;
    for (const wsId of workspaces) {
      const wsAgents = agents.filter(a => a.workspace_id === wsId);
      const hasBlocked = wsAgents.some(a => a.status === 'blocked');
      const name = wsAgents[0]?.project || wsId.slice(0, 8);
      html += `<button class="chip${activeWorkspace===wsId?' active':''}${hasBlocked?' alert':''}" onclick="selectWorkspace('${wsId}')">${esc(name)}</button>`;
    }
  }
  html += `<button class="chip chip-add" onclick="createWorkspace()" aria-label="New Space">+</button>`;
  if (activeWorkspace) {
    html += `<button class="chip" onclick="openSpaceMenu()" aria-label="Space actions">⋯</button>`;
  }
  html += `</div>`;
```

If `esc()` does not exist yet, add near the script top:

```javascript
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
```

Keep the existing Tabs strip block as-is (still only when `activeWorkspace` is set).

When `workspaces.length === 0`, still render agent sections / empty state after the strip (reuse filtered list logic; empty → `Waiting for agents…`).

- [ ] **Step 3: Add `createWorkspace`**

Next to `createTab`:

```javascript
function createWorkspace() {
  if (!ws) return;
  if (window.cue) cue('sparkle');
  ws.send(JSON.stringify({type:'create_workspace'}));
}

function closeSpaceOverlays() {}
```

- [ ] **Step 4: Handle success / error in `onmessage`**

In the message handler (near `error` / `command_result`):

```javascript
  } else if (msg.type === 'workspace_created' && msg.ok) {
    if (window.cue) cue('sparkle');
  } else if (msg.type === 'workspace_renamed' && msg.ok) {
    closeSpaceOverlays();
  } else if (msg.type === 'workspace_closed' && msg.ok) {
    activeWorkspace = null;
    activeTab = null;
    closeSpaceOverlays();
    render();
  }
```

- [ ] **Step 5: Manual smoke (optional in this task)**

Open Web against a running relay: confirm Spaces strip shows with `+` when only one Space exists; tap `+` and watch relay logs for `Create workspace`.

- [ ] **Step 6: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): always show Spaces strip with create action

Keep + Space reachable when there are zero or one workspaces.
EOF
)"
```

---

### Task 5: Web — `⋯` sheet, rename modal, close confirm

**Files:**
- Modify: `web/index.html` (CSS + JS overlays)

- [ ] **Step 1: Add minimal overlay CSS**

In the `<style>` block, append:

```css
.space-overlay { position:fixed; inset:0; background:rgba(0,0,0,0.45); z-index:10000; display:flex; align-items:flex-end; justify-content:center; }
.space-sheet, .space-dialog { width:min(420px,100%); background:var(--surface); color:var(--text); border-radius:14px 14px 0 0; padding:14px 14px calc(14px + env(safe-area-inset-bottom,0px)); border:1px solid var(--border); }
.space-dialog { align-self:center; border-radius:14px; margin:16px; width:min(360px,calc(100% - 32px)); }
.space-sheet button, .space-dialog button { display:block; width:100%; text-align:left; padding:12px 10px; border:none; background:transparent; color:var(--text); font-size:1rem; border-radius:8px; cursor:pointer; }
.space-sheet button:active, .space-dialog .row button:active { background:color-mix(in srgb, var(--text) 8%, transparent); }
.space-sheet .danger, .space-dialog .danger { color:var(--red); }
.space-dialog h3 { margin:0 0 10px; font-size:1rem; }
.space-dialog input { width:100%; box-sizing:border-box; padding:10px; border-radius:8px; border:1px solid var(--border); background:var(--bg); color:var(--text); font-size:16px; margin-bottom:12px; }
.space-dialog .row { display:flex; gap:8px; justify-content:flex-end; }
.space-dialog .row button { width:auto; padding:10px 14px; background:var(--border); }
.space-dialog .row button.primary { background:var(--blue); color:#fff; }
```

- [ ] **Step 2: Overlay helpers**

Replace the stub `closeSpaceOverlays` and add:

```javascript
function closeSpaceOverlays() {
  document.querySelectorAll('.space-overlay').forEach(el => el.remove());
}

function openSpaceMenu() {
  if (!activeWorkspace) return;
  closeSpaceOverlays();
  const ov = document.createElement('div');
  ov.className = 'space-overlay';
  ov.onclick = (e) => { if (e.target === ov) closeSpaceOverlays(); };
  ov.innerHTML = `<div class="space-sheet" role="menu" aria-label="Space actions">
    <button type="button" onclick="openRenameSpace()">Rename…</button>
    <button type="button" class="danger" onclick="openCloseSpace()">Close…</button>
  </div>`;
  document.body.appendChild(ov);
}

function currentSpaceLabel() {
  const a = agents.find(x => x.workspace_id === activeWorkspace);
  return a?.project || activeWorkspace || '';
}

function openRenameSpace() {
  if (!activeWorkspace) return;
  closeSpaceOverlays();
  const ov = document.createElement('div');
  ov.className = 'space-overlay';
  ov.onclick = (e) => { if (e.target === ov) closeSpaceOverlays(); };
  const label = esc(currentSpaceLabel());
  ov.innerHTML = `<div class="space-dialog" role="dialog" aria-label="Rename Space">
    <h3>Rename Space</h3>
    <input id="spaceRenameInput" value="${label}" maxlength="80" />
    <div class="row">
      <button type="button" onclick="closeSpaceOverlays()">Cancel</button>
      <button type="button" class="primary" onclick="submitRenameSpace()">Save</button>
    </div>
  </div>`;
  document.body.appendChild(ov);
  const input = document.getElementById('spaceRenameInput');
  input.focus();
  input.select();
}

function submitRenameSpace() {
  if (!ws || !activeWorkspace) return;
  const input = document.getElementById('spaceRenameInput');
  const label = (input?.value || '').trim();
  if (!label) return;
  ws.send(JSON.stringify({
    type: 'rename_workspace',
    workspace_id: activeWorkspace,
    label,
  }));
}

function openCloseSpace() {
  if (!activeWorkspace) return;
  closeSpaceOverlays();
  const ov = document.createElement('div');
  ov.className = 'space-overlay';
  ov.onclick = (e) => { if (e.target === ov) closeSpaceOverlays(); };
  ov.innerHTML = `<div class="space-dialog" role="dialog" aria-label="Close Space">
    <h3>关闭这个 Space？</h3>
    <div class="row">
      <button type="button" onclick="closeSpaceOverlays()">Cancel</button>
      <button type="button" class="danger" onclick="submitCloseSpace()">Close</button>
    </div>
  </div>`;
  document.body.appendChild(ov);
}

function submitCloseSpace() {
  if (!ws || !activeWorkspace) return;
  ws.send(JSON.stringify({
    type: 'close_workspace',
    workspace_id: activeWorkspace,
  }));
}
```

- [ ] **Step 3: Manual checklist**

Against a live relay + herdr:

1. 0 / 1 / many Spaces: `+` visible
2. Select a Space → `⋯` → Rename → Save → chip label updates after next agents snapshot
3. `⋯` → Close → confirm → selection returns to All; Space disappears after snapshot
4. Disconnect WS → actions no-op
5. Failed close → selection stays

- [ ] **Step 4: Run relay regression**

Run: `python3 -m unittest tests.test_relay_workspace tests.test_relay_initial_snapshot -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): add Space rename and close via overflow menu

Selected Space shows ⋯ with rename dialog and confirmed close.
EOF
)"
```

---

### Task 6: Docs touch-up (optional, small)

**Files:**
- Modify: `README.md` only if it documents WebSocket client→server messages

- [ ] **Step 1:** If README lists client message types, add `create_workspace`, `rename_workspace`, `close_workspace` next to `create_tab`.

- [ ] **Step 2: Commit only if README changed**

```bash
git add README.md
git commit -m "docs: document Space WebSocket messages"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|------------------|------|
| `create_workspace` → `herdr workspace create --focus` | Task 1 |
| `rename_workspace` + validation | Task 2 |
| `close_workspace` + validation | Task 3 |
| Success replies `workspace_*` | Tasks 1–3 |
| Clear selection only after `workspace_closed` ok | Tasks 4–5 |
| Spaces strip always visible + `+` | Task 4 |
| `⋯` sheet Rename / Close | Task 5 |
| Rename modal + empty label guard | Task 5 |
| Close confirm copy | Task 5 |
| Disconnected no-op | Tasks 4–5 (`if (!ws) return`) |
| No Tab rename/close / no create form / Web only | Not implemented (YAGNI) |
| Relay unit tests | Tasks 1–3 |

## Placeholder / consistency notes

- Message type names are fixed across tasks: `create_workspace`, `rename_workspace`, `close_workspace`, `workspace_created`, `workspace_renamed`, `workspace_closed`.
- Relay uses `run_herdr_rc_async` for all three so `ok: true` is honest (needed for close).
- Chip labels still come from the next agents snapshot after rename (no client-side label patch).
