# Web Connection Latency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show real-time online/offline in the web header and always display WebSocket RTT as `live · Nms`.

**Architecture:** Client sends JSON `{type:"ping",t}` every 5s on the existing WebSocket; relay echoes `{type:"pong",t}`. Client computes RTT and updates `#connLabel` / `#statusDot` (color bands). Pause pings while the tab is hidden; mark link poor (red) if no pong for 15s without forcing disconnect.

**Tech Stack:** Python asyncio relay (`relay/herdr_relay.py`), unittest, single-file Web (`web/index.html`); optional demo worker (`demo-worker/src/index.js`)

**Spec:** `docs/superpowers/specs/2026-08-07-web-connection-latency-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `tests/test_relay_ping.py` | WS `ping` → `pong` echo tests |
| `relay/herdr_relay.py` | Handle `ping` in `handle_client`; no audit |
| `web/index.html` | Ping loop, RTT state, header UI, visibility + stale watchdog |
| `demo-worker/src/index.js` | Echo `pong` so demo mode also shows ms |

---

### Task 1: Relay ping → pong (TDD)

**Files:**
- Create: `tests/test_relay_ping.py`
- Modify: `relay/herdr_relay.py` (inside `handle_client` message switch, near other lightweight branches)

- [ ] **Step 1: Write the failing test**

Create `tests/test_relay_ping.py`:

```python
#!/usr/bin/env python3
"""Application-level ping/pong for connection latency."""
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


class PingHandlerTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _reset()

    async def test_ping_echoes_t(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "ping", "t": 1723000000123})])
        with mock.patch.object(herdr_relay, "audit") as audit_mock:
            await herdr_relay.handle_client(ws)
        pongs = [m for m in ws.sent_messages() if m.get("type") == "pong"]
        self.assertEqual(len(pongs), 1)
        self.assertEqual(pongs[0]["t"], 1723000000123)
        audit_mock.assert_not_called()

    async def test_ping_missing_t_still_pongs(self):
        ws = FakeWebSocket(incoming=[json.dumps({"type": "ping"})])
        await herdr_relay.handle_client(ws)
        pongs = [m for m in ws.sent_messages() if m.get("type") == "pong"]
        self.assertEqual(len(pongs), 1)
        self.assertIn("t", pongs[0])
        self.assertEqual(pongs[0]["t"], 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/victor/code-github/herdr-remote
uv run --with websockets --with zeroconf --with pywebpush --with py-vapid \
  python -m unittest tests.test_relay_ping -v
```

Expected: FAIL (no `pong` messages / no `ping` branch).

- [ ] **Step 3: Implement relay handler**

In `relay/herdr_relay.py`, inside `handle_client`'s `msg_type` switch, add **early** (before heavy branches like `read_pane` / `git_*`):

```python
            elif msg_type == "ping":
                # Lightweight RTT probe — echo client timestamp, never audit.
                t = msg.get("t", 0)
                try:
                    t = int(t)
                except (TypeError, ValueError):
                    t = 0
                await ws.send(json.dumps({"type": "pong", "t": t}))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run --with websockets --with zeroconf --with pywebpush --with py-vapid \
  python -m unittest tests.test_relay_ping -v
```

Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_relay_ping.py relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): echo WebSocket ping/pong for client RTT

EOF
)"
```

---

### Task 2: Web header — connection state + RTT display

**Files:**
- Modify: `web/index.html` (near `connect` / `setStatus` / `handleMessage`)

- [ ] **Step 1: Add connection latency state + helpers**

Near other top-level `let` state (after `ws` / agents), add:

```javascript
let connState = 'disconnected'; // connected | connecting | disconnected
let lastRttMs = null;
let lastPongAt = 0;
let pingTimer = null;
let staleTimer = null;
const PING_INTERVAL_MS = 5000;
const STALE_MS = 15000;
const RTT_GOOD_MS = 150;
const RTT_OK_MS = 400;
```

Add helpers (replace existing `setStatus`):

```javascript
function clearPingTimers() {
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  if (staleTimer) { clearInterval(staleTimer); staleTimer = null; }
}

function rttColor(ms) {
  if (ms == null || !Number.isFinite(ms)) return 'var(--muted)';
  if (ms < RTT_GOOD_MS) return 'var(--green)';
  if (ms <= RTT_OK_MS) return 'var(--orange)';
  return 'var(--red)';
}

function sendPing() {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({ type: 'ping', t: Date.now() }));
}

function startPingLoop() {
  clearPingTimers();
  lastPongAt = Date.now();
  sendPing();
  pingTimer = setInterval(() => {
    if (document.hidden) return;
    sendPing();
  }, PING_INTERVAL_MS);
  staleTimer = setInterval(() => {
    if (connState !== 'connected') return;
    if (Date.now() - lastPongAt > STALE_MS) refreshConnLabel({ stale: true });
  }, 1000);
}

function refreshConnLabel(opts = {}) {
  const dot = document.getElementById('statusDot');
  const label = document.getElementById('connLabel');
  if (!dot || !label) return;
  const stale = !!opts.stale || (connState === 'connected' && lastPongAt && (Date.now() - lastPongAt > STALE_MS));

  if (connState === 'connecting') {
    dot.style.background = 'var(--orange)';
    label.textContent = 'connecting…';
    label.style.color = 'var(--orange)';
    return;
  }
  if (connState !== 'connected') {
    dot.style.background = 'var(--red)';
    label.textContent = 'offline';
    label.style.color = 'var(--red)';
    return;
  }

  let text = 'live';
  if (lastRttMs != null && Number.isFinite(lastRttMs)) text += ' · ' + Math.round(lastRttMs) + 'ms';
  else if (stale) text += ' · —';

  const color = stale ? 'var(--red)' : (lastRttMs != null ? rttColor(lastRttMs) : 'var(--green)');
  dot.style.background = color;
  label.textContent = text;
  label.style.color = color;
}

function setStatus(s) {
  connState = s === 'connected' ? 'connected' : s === 'connecting' ? 'connecting' : 'disconnected';
  if (connState !== 'connected') {
    clearPingTimers();
    lastRttMs = null;
  }
  refreshConnLabel();
}
```

- [ ] **Step 2: Wire connect / pong / visibility**

Update `connect()`:

```javascript
function connect() {
  let url = localStorage.getItem('herdr_relay_url') || (isSelfRelay ? autoRelayUrl : (isDemo ? DEMO_RELAY : ''));
  if (!url) { showSetup(); return; }
  if (ws) { try { ws.close(); } catch (_) {} }
  clearPingTimers();
  setStatus('connecting');
  const token = localStorage.getItem('herdr_relay_token');
  let wsUrl = url;
  if (token) wsUrl += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
  ws = new WebSocket(wsUrl);
  ws.onopen = () => {
    setStatus('connected');
    startPingLoop();
    if (window.cue) cue('ready');
  };
  ws.onclose = () => { setStatus('disconnected'); setTimeout(connect, 3000); };
  ws.onerror = () => setStatus('disconnected');
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
}
```

In `handleMessage`, add near the top (after parsing is done — first branch or early `else if`):

```javascript
  if (msg.type === 'pong') {
    const t = Number(msg.t);
    if (Number.isFinite(t) && t > 0) {
      lastRttMs = Math.max(0, Date.now() - t);
      lastPongAt = Date.now();
      refreshConnLabel();
    }
    return;
  }
```

Note: existing `handleMessage` uses `if` / `else if`. Either insert `pong` as the first `if` and keep the rest as `else if`, or add `else if (msg.type === 'pong')` and `return` is unnecessary if no fall-through.

Add once (near `connect` definition, not inside it):

```javascript
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (connState === 'connected') {
    sendPing();
    refreshConnLabel();
  }
});
```

- [ ] **Step 3: Settings status line (optional nicety)**

Where settings status is filled (`toggleSettings` / Connected HTML), include RTT when connected:

```javascript
document.getElementById('settingsStatus').innerHTML = ws && ws.readyState === 1
  ? `<span style="color:var(--green)">● Connected${lastRttMs != null ? ' · ' + Math.round(lastRttMs) + 'ms' : ''}</span>`
  : `<span style="color:var(--red)">● Disconnected</span>`;
```

- [ ] **Step 4: Manual check**

1. Restart relay with current `herdr_relay.py`.
2. Open `web/index.html` (or http://127.0.0.1:8765), connect.
3. Within ~1s header shows `live · Nms`.
4. Stop relay → `offline` (red). Start again → `live · Nms`.

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): show live connection RTT in header

EOF
)"
```

---

### Task 3: Demo worker echoes pong

**Files:**
- Modify: `demo-worker/src/index.js`

- [ ] **Step 1: Handle ping in demo message listener**

Inside `server.addEventListener('message', ...)`, before or after `read_pane`:

```javascript
        if (msg.type === 'ping') {
          let t = 0;
          try { t = parseInt(msg.t, 10) || 0; } catch { t = 0; }
          server.send(JSON.stringify({ type: 'pong', t }));
        } else if (msg.type === 'read_pane') {
```

(Keep existing `respond` branch; adjust `else if` chain so `ping` does not fall into other handlers.)

- [ ] **Step 2: Smoke (optional)**

If wrangler available: `cd demo-worker && npx wrangler dev` and connect demo URL — header should show ms.

- [ ] **Step 3: Commit**

```bash
git add demo-worker/src/index.js
git commit -m "$(cat <<'EOF'
feat(demo-worker): echo ping/pong for latency display

EOF
)"
```

---

### Task 4: Final verification

- [ ] **Step 1: Run relay unit tests**

```bash
cd /Users/victor/code-github/herdr-remote
uv run --with websockets --with zeroconf --with pywebpush --with py-vapid \
  python -m unittest tests.test_relay_ping tests.test_relay_git_diff tests.test_relay_git_status -v
```

Expected: all PASS.

- [ ] **Step 2: Spec checklist**

Confirm against `docs/superpowers/specs/2026-08-07-web-connection-latency-design.md`:

- [ ] Header `live · Nms` / `connecting…` / `offline`
- [ ] Color bands &lt;150 / 150–400 / &gt;400
- [ ] 5s ping; pause while `document.hidden`; resume on visible
- [ ] 15s stale → red, no forced WS close
- [ ] No audit on ping
- [ ] Demo worker pong (best-effort)

- [ ] **Step 3: Commit any leftover docs only if edited**

No further commit unless something was missed.

---

## Spec coverage (self-review)

| Spec item | Task |
|-----------|------|
| App ping/pong protocol | Task 1 |
| Header `live · Nms` | Task 2 |
| Color bands | Task 2 (`rttColor`) |
| 5s interval + open ping | Task 2 (`startPingLoop`) |
| Hidden tab pause | Task 2 (`document.hidden` in interval + visibility listener) |
| 15s stale red | Task 2 (`staleTimer`) |
| No audit | Task 1 test + handler |
| Demo best-effort | Task 3 |
| Unit test echo | Task 1 |
| Bandwidth charts / etc. | Out of scope — no tasks |
