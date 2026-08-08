# Web: Connection online status + latency

**Date:** 2026-08-07  
**Status:** Approved for implementation planning  
**Scope:** Web client (`web/index.html`) + relay (`relay/herdr_relay.py`) + unit tests

## Goal

Make the web header show **real-time online/offline** more clearly, and always show **WebSocket RTT** next to the live label (e.g. `live · 120ms`), so phone users can tell whether the relay link is healthy (especially over Tailscale / tunnel).

## Background

- Header already has `#statusDot` + `#connLabel` with `live` / `connecting…` / `offline`.
- Settings shows Connected / Disconnected only.
- There is no application-level ping; users cannot see latency. High Tailscale RTT (~500ms) was observed when opening git files and felt like “slowness” with no indicator.
- Browser WebSocket protocol ping/pong RTT is not exposed to JS, so app-level messages are required.

## Decisions

| Topic | Choice |
|-------|--------|
| Online status | Keep and clarify: green/orange/red dot + label |
| Latency display | Always visible in header: `live · Nms` |
| Measurement | Application JSON `ping` / `pong` over the same WebSocket |
| Interval | Every 5s while connected; immediate ping on open / foreground |
| Stale threshold | No `pong` for 15s → treat link as poor (red), do not force-close WS |
| Color by RTT | &lt;150ms green; 150–400ms orange; &gt;400ms red (dot + label) |
| Background tab | When `document.hidden`, **pause** ping timer; on visible again, ping immediately and resume 5s |
| Audit log | Do **not** audit every ping (noise) |
| Demo worker | Best-effort same `pong`; if missing, header may stay `live` without ms |
| Out of scope | Bandwidth/throughput estimates, latency charts, multi-hop diagnostics |

## Approach

**App-level ping (recommended):** client sends `{type:"ping",t:<client_ms>}`; relay replies `{type:"pong",t:<echo>}`. RTT = `now - t`.

Rejected alternatives:

- Rely only on WebSocket protocol ping — JS cannot read RTT.
- Separate HTTP `/health` probe — may not share the same path as the WS (tunnel vs Tailscale), numbers misleading.

## Protocol

### Client → Relay

```json
{ "type": "ping", "t": 1723000000123 }
```

`t` is client `Date.now()` (ms).

### Relay → Client

```json
{ "type": "pong", "t": 1723000000123 }
```

Echo `t` unchanged. No server clock required.

## UI

| State | `#connLabel` | `#statusDot` |
|-------|--------------|--------------|
| Connected, RTT known | `live · 120ms` | Color by RTT bands above |
| Connected, no RTT yet | `live` | Green (default connected) |
| Connecting | `connecting…` | Orange |
| Disconnected | `offline` | Red |
| Connected but stale (&gt;15s no pong) | Last RTT if any: `live · 120ms`; else `live · —` | Red |

Settings Status block may show the latest RTT as a convenience; header is the source of truth for at-a-glance use.

## Client behavior

1. On `ws.onopen`: `setStatus('connected')`, start ping loop, send first ping immediately.
2. Every 5s while connected and (preferably) document visible: send `ping`.
3. On `pong`: compute RTT, store `lastRttMs` / `lastPongAt`, refresh header.
4. On `ws.onclose` / hard failure: clear ping timer, `setStatus('disconnected')` → `offline`; existing 3s reconnect unchanged.
5. Watchdog: if `Date.now() - lastPongAt > 15000` while socket still open, mark poor (red) without closing; label keeps last RTT if known, otherwise `live · —`.
6. `visibilitychange`: if hidden, pause the ping timer; if visible again, ping once and resume 5s.

## Relay behavior

In the existing WebSocket message switch, handle `ping`:

- Respond immediately with `pong` and the same `t`.
- No `audit()` call.
- Invalid/missing `t`: still reply with whatever `t` was sent (or `0`) so the client can detect a broken echo if needed.

## Testing

- Unit: relay receives `ping` with `t=123` → sends `pong` with `t=123`.
- Manual: connect → ms appears within ~5s (usually &lt;1s); kill relay → `offline`; restore → `live · Nms` again; optional: throttle network and confirm color band changes.

## Non-goals

- Estimating bandwidth (“贷款/网速” beyond RTT).
- Persisting latency history or drawing graphs.
- Changing Cloudflare tunnel or Tailscale configuration.
