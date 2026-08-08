# Web WebSocket Compression Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrink Web↔relay WebSocket traffic via `permessage-deflate` plus HGZ1 binary gzip frames for whitelist downlink types, without breaking clients that never send `hello`.

**Architecture:** Pure encode/decode helpers in `relay/herdr_relay.py` (unit-tested). Per-connection `client_caps` drives whether whitelist sends use HGZ1 or text JSON. Web sets `binaryType='arraybuffer'`, sends `{type:"hello",caps:["bin-gzip-v1"]}` on open, and dual-path decodes string vs HGZ1 into the existing `handleMessage`. Initial snapshot is deferred briefly so capable clients can hello first.

**Tech Stack:** Python 3 + `websockets` + stdlib `gzip`/`struct`; single-file web JS + `DecompressionStream` (with `pako`-free fallback via `DecompressionStream('gzip')` / manual inflate check); `unittest`.

**Spec:** `docs/superpowers/specs/2026-08-08-web-ws-compression-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `relay/herdr_relay.py` | HGZ1 helpers, `client_caps`, `send_to_client` / `broadcast`, `hello` handler, compression on `serve`, deferred initial snapshot |
| `tests/test_relay_ws_compression.py` | Encode/decode, fallback, hello caps, broadcast split |
| `web/index.html` | `binaryType`, hello, HGZ1 decode → `handleMessage` |
| `demo-worker/src/index.js` | Best-effort hello + HGZ1 for mock agents/pane (optional if time; plain JSON still OK) |

**Whitelist constant:** `BIN_GZIP_TYPES = frozenset({"agents", "pane_content", "git_diff", "git_show"})`  
**Cap string:** `bin-gzip-v1`

---

### Task 1: HGZ1 encode/decode helpers (TDD)

**Files:**
- Create: `tests/test_relay_ws_compression.py`
- Modify: `relay/herdr_relay.py` (helpers near top-level utilities, after imports / before `broadcast`)

- [ ] **Step 1: Write failing tests**

```python
#!/usr/bin/env python3
"""HGZ1 binary gzip frames + capability-aware send."""
import gzip
import json
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class Hgz1CodecTests(unittest.TestCase):
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

    def test_encode_prefers_text_when_not_smaller(self):
        # tiny payload: helper used by send path should signal "use text"
        msg = {"type": "agents", "agents": []}
        choice = herdr_relay.encode_for_client(msg, caps={"bin-gzip-v1"})
        # either bytes HGZ1 or str JSON — if bytes, must decode; policy: return str when not smaller
        self.assertTrue(isinstance(choice, str) or isinstance(choice, (bytes, bytearray)))

    def test_no_cap_always_text(self):
        msg = {"type": "pane_content", "pane_id": "x", "content": "y" * 5000}
        out = herdr_relay.encode_for_client(msg, caps=set())
        self.assertIsInstance(out, str)
        self.assertEqual(json.loads(out)["type"], "pane_content")

    def test_non_whitelist_always_text_even_with_cap(self):
        msg = {"type": "pong", "t": 1}
        out = herdr_relay.encode_for_client(msg, caps={"bin-gzip-v1"})
        self.assertIsInstance(out, str)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect fail**

```bash
cd /Users/victor/code-github/herdr-remote && python3 -m pytest tests/test_relay_ws_compression.py -v
```

Expected: FAIL — `encode_hgz1` missing.

- [ ] **Step 3: Implement helpers in `relay/herdr_relay.py`**

```python
import gzip
import struct

BIN_GZIP_CAP = "bin-gzip-v1"
BIN_GZIP_TYPES = frozenset({"agents", "pane_content", "git_diff", "git_show"})
# ws -> set[str]
client_caps = {}


def encode_hgz1(msg: dict) -> bytes:
    raw = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    typ = str(msg.get("type") or "")
    typ_b = typ.encode("utf-8")
    if len(typ_b) > 0xFFFF:
        raise ValueError("type too long")
    compressed = gzip.compress(raw, compresslevel=6)
    flags = 1
    payload = compressed
    return b"HGZ1" + bytes([flags]) + struct.pack(">H", len(typ_b)) + typ_b + struct.pack(">I", len(raw)) + payload


def parse_hgz1_header(frame: bytes):
    if len(frame) < 11 or frame[:4] != b"HGZ1":
        raise ValueError("bad HGZ1 magic")
    flags = frame[4]
    type_len = struct.unpack(">H", frame[5:7])[0]
    typ = frame[7:7 + type_len].decode("utf-8")
    raw_len = struct.unpack(">I", frame[7 + type_len:11 + type_len])[0]
    payload = frame[11 + type_len:]
    return typ, flags, raw_len, payload


def decode_hgz1(frame: bytes) -> dict:
    typ, flags, raw_len, payload = parse_hgz1_header(frame)
    if flags & 1:
        raw = gzip.decompress(payload)
    else:
        raw = payload
    if len(raw) != raw_len:
        raise ValueError("raw_len mismatch")
    msg = json.loads(raw.decode("utf-8"))
    if msg.get("type") != typ:
        raise ValueError("type mismatch")
    return msg


def encode_for_client(msg: dict, caps) -> str | bytes:
    """Return str JSON or bytes HGZ1. Non-whitelist / no cap / no shrink → str."""
    caps = caps or set()
    typ = msg.get("type")
    if typ not in BIN_GZIP_TYPES or BIN_GZIP_CAP not in caps:
        return json.dumps(msg, separators=(",", ":"))
    raw = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    try:
        frame = encode_hgz1(msg)
    except Exception:
        return raw.decode("utf-8")
    if len(frame) >= len(raw):
        return raw.decode("utf-8")
    return frame
```

Adjust `test_encode_prefers_text_when_not_smaller` to assert `isinstance(choice, str)` for empty agents.

- [ ] **Step 4: Re-run tests — expect pass**

```bash
python3 -m pytest tests/test_relay_ws_compression.py -v
```

- [ ] **Step 5: Commit**

```bash
git add relay/herdr_relay.py tests/test_relay_ws_compression.py
git commit -m "$(cat <<'EOF'
feat(relay): add HGZ1 binary gzip codec helpers

EOF
)"
```

---

### Task 2: `client_caps`, `send_to_client`, `broadcast`, `hello`

**Files:**
- Modify: `relay/herdr_relay.py` (`broadcast`, `handle_client`, disconnect cleanup)
- Modify: `tests/test_relay_ws_compression.py`

- [ ] **Step 1: Add handler/broadcast tests**

```python
import asyncio
from unittest import mock

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


def _reset():
    herdr_relay.agent_cache.clear()
    herdr_relay.known_panes.clear()
    herdr_relay.pane_remote_map.clear()
    herdr_relay.clients.clear()
    herdr_relay.client_caps.clear()


class HelloAndBroadcastTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _reset()

    async def test_hello_stores_caps(self):
        ws = FakeWebSocket(incoming=[json.dumps({
            "type": "hello", "caps": ["bin-gzip-v1"],
        })])
        await herdr_relay.handle_client(ws)
        self.assertIn(BIN_GZIP_CAP := "bin-gzip-v1", herdr_relay.client_caps.get(ws, set()))

    async def test_broadcast_splits_capable_and_plain(self):
        plain = FakeWebSocket()
        cap = FakeWebSocket()
        herdr_relay.clients.add(plain)
        herdr_relay.clients.add(cap)
        herdr_relay.client_caps[cap] = {"bin-gzip-v1"}
        msg = {"type": "pane_content", "pane_id": "w", "content": ("line\n" * 400)}
        await herdr_relay.broadcast(msg)
        self.assertTrue(any(isinstance(s, str) for s in plain.sent))
        self.assertTrue(any(isinstance(s, (bytes, bytearray)) for s in cap.sent))
```

- [ ] **Step 2: Run — expect fail** on `client_caps` / `hello`

- [ ] **Step 3: Implement wiring**

```python
async def send_to_client(ws, msg: dict):
    data = encode_for_client(msg, client_caps.get(ws, set()))
    await ws.send(data)


async def broadcast(msg):
    dead = set()
    for ws in list(clients):
        try:
            await send_to_client(ws, msg)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    clients.difference_update(dead)
    for ws in dead:
        client_caps.pop(ws, None)
```

In `handle_client`:

1. On connect: `client_caps[ws] = set()` (before snapshot).
2. **Defer initial snapshot** ~200ms to allow hello:

```python
    clients.add(ws)
    client_caps[ws] = set()
    connected_at = time.monotonic()

    async def _push_snapshot():
        await asyncio.sleep(0.2)
        if ws not in clients:
            return
        try:
            await send_to_client(ws, {"type": "agents", "agents": list(agent_cache.values())})
        except Exception as e:
            log.debug("Initial snapshot not delivered to %s: %s", ip, e)

    snapshot_task = asyncio.create_task(_push_snapshot())
```

3. In message loop, only `json.loads` when `isinstance(raw, (str, bytes))` and content is text — skip binary uplink for v1:

```python
            if isinstance(raw, (bytes, bytearray)) and not (len(raw) and raw[:1] in (b"{", b"[")):
                # ignore unexpected binary from client in v1
                continue
            if isinstance(raw, (bytes, bytearray)):
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
```

4. Handle hello (no audit):

```python
            elif msg_type == "hello":
                caps = msg.get("caps") or []
                if isinstance(caps, list):
                    client_caps[ws] = {str(c) for c in caps if isinstance(c, str)}
                else:
                    client_caps[ws] = set()
```

5. Replace unicast `await ws.send(json.dumps(...))` for whitelist types (`pane_content`, `git_diff`, `git_show`) with `await send_to_client(ws, payload)`. Keep small messages (`pong`, `error`, `command_result`, `git_status`) as text `json.dumps` **or** route all through `send_to_client` (safe — non-whitelist always text).

Prefer: **all outbound via `send_to_client`** so one path.

6. On disconnect (`finally`): `clients.discard(ws)`; `client_caps.pop(ws, None)`; cancel `snapshot_task` if pending.

- [ ] **Step 4: Fix `tests/test_relay_ping.py` / others** that assume first send is immediate snapshot — they may need `await asyncio.sleep(0.25)` or mock sleep. Update FakeWebSocket consumers:

```bash
python3 -m pytest tests/test_relay_ping.py tests/test_relay_ws_compression.py tests/test_relay_initial_snapshot.py -v
```

Update `test_relay_initial_snapshot.py` if it asserts snapshot before any client message: either advance time with `asyncio.sleep(0.25)` or patch `asyncio.sleep`.

- [ ] **Step 5: Commit**

```bash
git add relay/herdr_relay.py tests/test_relay_ws_compression.py tests/test_relay_ping.py tests/test_relay_initial_snapshot.py
git commit -m "$(cat <<'EOF'
feat(relay): negotiate bin-gzip-v1 and send HGZ1 whitelist frames

EOF
)"
```

---

### Task 3: Enable permessage-deflate on `serve`

**Files:**
- Modify: `relay/herdr_relay.py` `main()` ~1650

- [ ] **Step 1: Check library default**

```bash
python3 -c "import websockets,inspect; print(websockets.__version__); import websockets.asyncio.server as s; print('ok')"
```

- [ ] **Step 2: Explicitly enable compression**

For websockets 14+ asyncio API:

```python
    server = await serve(
        handle_client,
        "0.0.0.0",
        WS_PORT,
        process_request=process_request,
        compression="deflate",  # permessage-deflate; omit/None only if API rejects
    )
```

If the installed version uses a different kwarg (`extensions=[...]`), match that API. If `compression="deflate"` is already default, keep the explicit kwarg for clarity in code comment:

```python
    # permessage-deflate: browsers negotiate automatically when offered
```

- [ ] **Step 3: Smoke start**

```bash
HERDR_RELAY_PORT=18379 uv run relay/herdr_relay.py
# expect: herdr-remote relay on :18379
# Ctrl-C
```

- [ ] **Step 4: Commit**

```bash
git add relay/herdr_relay.py
git commit -m "$(cat <<'EOF'
feat(relay): enable WebSocket permessage-deflate

EOF
)"
```

---

### Task 4: Web decode path + hello

**Files:**
- Modify: `web/index.html` (`connect`, message handler)
- Optional: `web/tests/hgz1.test.html` with fixed test vectors from Python

- [ ] **Step 1: Add JS decode helpers** (near `connect` / before `handleMessage`)

```javascript
const BIN_GZIP_CAP = 'bin-gzip-v1';

function u16be(view, o) { return (view.getUint8(o) << 8) | view.getUint8(o + 1); }
function u32be(view, o) {
  return ((view.getUint8(o) << 24) | (view.getUint8(o + 1) << 16) |
          (view.getUint8(o + 2) << 8) | view.getUint8(o + 3)) >>> 0;
}

async function gunzipU8(data) {
  if (typeof DecompressionStream === 'undefined') {
    throw new Error('DecompressionStream unavailable');
  }
  const ds = new DecompressionStream('gzip');
  const ab = await new Response(new Blob([data]).stream().pipeThrough(ds)).arrayBuffer();
  return new Uint8Array(ab);
}

async function decodeHgz1(buf) {
  const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (u8.length < 11) throw new Error('short HGZ1');
  const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (magic !== 'HGZ1') throw new Error('bad magic');
  const flags = u8[4];
  const typeLen = (u8[5] << 8) | u8[6];
  const typ = new TextDecoder().decode(u8.subarray(7, 7 + typeLen));
  const rawLen = u32be(new DataView(u8.buffer, u8.byteOffset + 7 + typeLen, 4), 0);
  // fix offset: after type
  const rawLenOff = 7 + typeLen;
  const rawLenVal = (u8[rawLenOff] << 24) | (u8[rawLenOff + 1] << 16) | (u8[rawLenOff + 2] << 8) | u8[rawLenOff + 3];
  let payload = u8.subarray(rawLenOff + 4);
  let raw = payload;
  if (flags & 1) raw = await gunzipU8(payload);
  if (raw.length !== (rawLenVal >>> 0)) throw new Error('raw_len mismatch');
  const msg = JSON.parse(new TextDecoder().decode(raw));
  if (msg.type !== typ) throw new Error('type mismatch');
  return msg;
}
```

(Use a single clear offset calculation in the real patch; avoid the duplicated `u32be` mistake above.)

Canonical decode body:

```javascript
async function decodeHgz1(buf) {
  const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (u8.length < 11 || u8[0] !== 0x48 || u8[1] !== 0x47 || u8[2] !== 0x5a || u8[3] !== 0x31)
    throw new Error('bad HGZ1');
  const flags = u8[4];
  const typeLen = (u8[5] << 8) | u8[6];
  let o = 7;
  const typ = new TextDecoder().decode(u8.subarray(o, o + typeLen));
  o += typeLen;
  const rawLen = ((u8[o] << 24) | (u8[o + 1] << 16) | (u8[o + 2] << 8) | u8[o + 3]) >>> 0;
  o += 4;
  let raw = u8.subarray(o);
  if (flags & 1) raw = await gunzipU8(raw);
  if (raw.length !== rawLen) throw new Error('raw_len mismatch');
  const msg = JSON.parse(new TextDecoder().decode(raw));
  if (msg.type !== typ) throw new Error('type mismatch');
  return msg;
}
```

- [ ] **Step 2: Wire `connect()`**

```javascript
function connect() {
  let url = localStorage.getItem('herdr_relay_url') || (isSelfRelay ? autoRelayUrl : (isDemo ? DEMO_RELAY : ''));
  if (!url) { showSetup(); return; }
  if (ws) ws.close();
  setStatus('connecting');
  const token = localStorage.getItem('herdr_relay_token');
  let wsUrl = url;
  if (token) wsUrl += (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token);
  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    setStatus('connected');
    try { ws.send(JSON.stringify({ type: 'hello', caps: [BIN_GZIP_CAP] })); } catch (_) {}
    startPingLoop();
    if (window.cue) cue('ready');
  };
  ws.onclose = () => { setStatus('disconnected'); setTimeout(connect, 3000); };
  ws.onerror = () => setStatus('disconnected');
  ws.onmessage = async (e) => {
    try {
      let msg;
      if (typeof e.data === 'string') msg = JSON.parse(e.data);
      else msg = await decodeHgz1(e.data);
      handleMessage(msg);
    } catch (err) {
      console.warn('ws message decode failed', err);
    }
  };
}
```

- [ ] **Step 3: Manual / browser check**

Restart relay + web. Connect, confirm Network/WS frames: after hello, `pane_content` / `agents` may show as binary. Terminal + agent list still render.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): decode HGZ1 frames and advertise bin-gzip-v1

EOF
)"
```

---

### Task 5: Demo worker best-effort + acceptance

**Files:**
- Modify: `demo-worker/src/index.js` (if straightforward)
- Modify: tests as needed

- [ ] **Step 1: Demo worker** — on text `hello` with `bin-gzip-v1`, set a flag on the connection; when sending mock `agents` / `pane_content`, if flag set and CompressionStream/gzip available in Workers, send ArrayBuffer HGZ1; else plain JSON.

If Worker gzip is awkward, **skip** and leave plain JSON (spec allows).

- [ ] **Step 2: Full pytest**

```bash
cd /Users/victor/code-github/herdr-remote && python3 -m pytest tests/test_relay_ws_compression.py tests/test_relay_ping.py tests/test_relay_initial_snapshot.py -v
```

Expected: all PASS.

- [ ] **Step 3: Acceptance checklist**

1. No-hello client (e.g. `websockets` script without hello) still gets text JSON agents.  
2. Web hello → whitelist binary when smaller; UI OK.  
3. ping RTT + git diff/show + pane refresh work.  
4. Truncate/corrupt a frame in DevTools → connection stays up.  

- [ ] **Step 4: Commit**

```bash
git add demo-worker/src/index.js tests/test_relay_ws_compression.py
git commit -m "$(cat <<'EOF'
feat: finish WS compression acceptance path

EOF
)"
```

---

## Spec coverage

| Spec item | Task |
|-----------|------|
| permessage-deflate | 3 |
| HGZ1 frame layout | 1 |
| Whitelist types | 1–2 |
| `hello` / `bin-gzip-v1` | 2, 4 |
| Per-client broadcast | 2 |
| Text fallback when not smaller | 1 |
| Web dual-path decode | 4 |
| Bad frame soft-fail | 4 |
| Other clients unchanged | 2 (default empty caps) |
| Demo best-effort | 5 |
| Unit tests | 1–2, 5 |
| Deferred snapshot for hello race | 2 |

## Consistency notes

- Names locked: `encode_hgz1`, `decode_hgz1`, `encode_for_client`, `send_to_client`, `client_caps`, `BIN_GZIP_CAP`, `BIN_GZIP_TYPES`, `decodeHgz1`, `BIN_GZIP_CAP` (JS).
- Initial snapshot delay is **200ms** max wait for hello; incapable clients still get plain JSON snapshot.
- `git_status` stays text (not in whitelist).
