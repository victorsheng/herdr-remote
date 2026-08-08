# Web ↔ Relay: WebSocket compression (deflate + binary gzip)

**Date:** 2026-08-08  
**Status:** Approved for implementation planning  
**Scope:** Web client (`web/index.html`) + relay (`relay/herdr_relay.py`) + unit tests; demo-worker best-effort

## Goal

Reduce WebSocket payload size on phone / Tailscale / tunnel links by combining transport-level compression with application-level gzip binary frames for large whitelist messages—without breaking clients that do not opt in.

## Background

- Today every WS message is uncompressed text JSON (`json.dumps` / `JSON.stringify`).
- Large downlink messages (`pane_content`, full `agents` snapshots, `git_diff` / `git_show`) dominate bytes and feel slow on high-RTT links.
- Existing latency work only surfaces RTT; it does not shrink payloads.
- Non-web clients (Telegram, TUI, native apps) must keep receiving plain JSON.

## Decisions

| Topic | Choice |
|-------|--------|
| Transport compression | Enable WebSocket `permessage-deflate` for all frames |
| Application compression | Binary gzip frames for a **type whitelist** |
| Whitelist (v1) | `agents`, `pane_content`, `git_diff`, `git_show` |
| Non-whitelist | Remain text JSON (still may use permessage-deflate) |
| Encoding | Binary frames (`HGZ1`), not base64-in-JSON |
| Client scope | Web + relay only; capability negotiation |
| Other clients | No code changes; always plain JSON |
| Uplink app-layer gzip | Out of scope for v1 (control messages are small; deflate covers them) |
| Rejected | MessagePack / schema change; gzip+base64 JSON envelope (approach 1) |

## Capability negotiation

After WS open, web sends a **text** JSON hello:

```json
{ "type": "hello", "caps": ["bin-gzip-v1"] }
```

Relay stores per-connection caps (default: empty).

- Connection **with** `bin-gzip-v1`: whitelist outbound messages may use HGZ1 binary frames.
- Connection **without**: all outbound messages stay text JSON (current behavior).

Relay does not require hello for basic operation; missing hello ⇒ plain JSON forever on that socket.

## Transport: permessage-deflate

- Relay: enable the `websockets` compression extension when creating the server (library defaults / explicit `compression` config as appropriate for the installed major version).
- Browser: negotiates automatically when the server offers it.
- Applies to both text and binary frames once negotiated.
- Demo worker: best-effort; if unsupported, rely on HGZ1 and/or plain JSON.

## Application: HGZ1 binary frame

WebSocket opcode = binary. Layout:

| Field | Size | Description |
|-------|------|-------------|
| magic | 4 | ASCII `HGZ1` |
| flags | 1 | bit0 = 1 if `payload` is gzip-compressed |
| type_len | 2 | big-endian length of `type` |
| type | type_len | UTF-8 logical message type (e.g. `pane_content`) |
| raw_len | 4 | big-endian length of uncompressed UTF-8 JSON |
| payload | rest | gzip(utf8(json)) if bit0 set, else utf8(json) |

Rules:

1. Uncompressed JSON must be the **full original message object** (including `type`), identical to today’s text frames after `JSON.parse`.
2. Header `type` must match the JSON `type` field (defense-in-depth for demux / logging).
3. If gzip fails or compressed size ≥ raw size, send **text JSON** for that message instead (still eligible for permessage-deflate).
4. Invalid frames: drop; optionally send text `{type:"error", message:"bad compressed frame"}`; do **not** close the socket.

## Relay send path

Replace naive “one `json.dumps` for all clients” broadcast for whitelist types:

1. Build the Python `dict` message as today.
2. For each connected client:
   - if `bin-gzip-v1` in caps → encode HGZ1 (or text fallback) and `await ws.send(bytes)`
   - else → `await ws.send(json.dumps(msg))` as today

Unicast replies (`pane_content`, `git_diff`, `git_show`) use the same per-connection rule.

## Web receive path

On `ws.onmessage`:

1. If `typeof data === 'string'` → existing `JSON.parse` + handlers.
2. If `Blob` / `ArrayBuffer` → parse HGZ1 → inflate if needed → `JSON.parse` → **same** handlers (`agents`, `pane_content`, …).
3. Set `binaryType = 'arraybuffer'` (or equivalent) when opening the socket.

On `ws.onopen` (after connected): send `hello` with `caps: ["bin-gzip-v1"]` before or immediately after the first ping.

**Initial snapshot timing:** Relay still pushes the cached `agents` snapshot **immediately** on connect (before the message loop), so UI does not wait on poll and snapshot ordering stays ahead of early client requests. That first snapshot usually precedes `hello`, so it is typically plain JSON (still eligible for permessage-deflate). Subsequent whitelist messages after `hello` use HGZ1 when beneficial.

## Demo worker

- Prefer: accept `hello`, emit HGZ1 for mock `agents` / `pane_content` when capable.
- Acceptable: ignore hello and keep plain JSON (web must keep dual-path decode).

## Out of scope

- Application-layer gzip for uplink control messages
- Compressing every message type at the app layer
- Changing Telegram / TUI / iOS / macOS clients
- Replacing JSON with MessagePack / CBOR
- UI bandwidth meters or compression stats (optional later)

## Acceptance

1. Clients without `hello` / without `bin-gzip-v1` behave exactly as today (text JSON).
2. Web with capability receives whitelist types as binary HGZ1 when compression wins; UI renders correctly after decode.
3. `ping`/`pong` RTT, slash-live, git diff/show still work.
4. Corrupt binary frames do not kill the connection.
5. Unit tests cover: encode/decode round-trip, gzip flag, fallback when compression does not shrink, per-client broadcast split (capable vs incapable).

## Implementation notes

- Keep encode/decode helpers pure and unit-tested in Python; mirror decode in web JS (or minimal shared test vectors in `tests/`).
- Prefer a small `client_caps: dict[ws, set[str]]` (or attribute on the connection wrapper) next to the existing clients set.
- Do not audit `hello` noisily; at most debug log.
