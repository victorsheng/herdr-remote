# Web Slash Live Mode: sync `/` with remote agent

**Date:** 2026-08-08  
**Status:** Approved for implementation planning  
**Scope:** Web client (`web/index.html`) primarily; no new relay/herdr APIs

## Goal

Make the web `/` path interact with the remote agent’s native slash-command UI instead of only showing a hardcoded local command list. Ordinary chat input stays locally buffered.

## Background

- The terminal input (`#termInput`) is a **local buffer**: keystrokes are not sent until Send/Enter runs `send_text` + `Enter`.
- The `/` button opens a **hardcoded** `COMMANDS` palette keyed by agent family (`claude` / `codex` / `pi` / `opencode`). Selecting an entry sends that text + Enter.
- Agents already implement their own `/` autocomplete menus. The local list drifts from reality and never triggers the remote menu.
- Root cause: the input box does not interact with the remote while composing slash commands.

## Decisions

| Topic | Choice |
|-------|--------|
| Approach | Slash Live Mode (buffered by default; live only for `/`) |
| Command selection UX | Use remote TUI menu + existing Keys dock (↑↓ / Enter / Esc) |
| Local palette | Keep as **fallback** when live fails/times out |
| Probe via `/help` | Out of scope (pollutes session history) |
| Parse remote menu into tappable local list | Out of scope (later) |
| Always-on character sync for normal chat | Out of scope |
| Relay/herdr slash-command API | Out of scope |
| Clients | Web only |

## Input modes

| Mode | Behavior |
|------|----------|
| `buffered` (default) | Current behavior: local buffer; Enter → `send_text` + `Enter` |
| `slash-live` | Printable chars / Backspace sync to remote immediately; local box mirrors the slash prefix |

### Enter `slash-live`

1. Tap `/` button → clear any leftover local input → send `/` to remote → enter live → speed up pane refresh.
2. In `buffered`, typing a leading `/` as the first character of the input → enter live and sync that `/`.

Show a small `LIVE /` affordance near the input so the user knows sync is active.

### Exit `slash-live`

| Trigger | Cleanup | Next state |
|---------|---------|------------|
| Esc (Keys or keyboard) | Send `Escape` (+ optional backspace scrub) | `buffered` |
| Enter (command submitted to agent) | None beyond the Enter already sent | `buffered` |
| Close terminal / switch pane | Send `Escape` (+ scrub) | `buffered` |
| Live timeout (~1.5s **without** a slash-menu signal) | Send `Escape` (+ scrub) | `buffered`, then open hardcoded fallback palette |

Timeout applies only to the wait-for-menu window after entering live. Once a menu signal is seen (or the user is actively typing a `/…` prefix), do **not** auto-exit on a wall clock.

## Keystroke sync (live only)

- Printable character → `send_text` (single char or short chunk)
- `Backspace` → `send_keys: ['backspace']` and update local mirror string
- `Enter` → `send_keys: ['Enter']`, then exit live
- `Escape` → `send_keys: ['Escape']`, then exit live
- Arrow / mod keys → existing Keys dock `send_keys` path (unchanged)
- IME composition (`compositionstart` … `compositionend`): do **not** sync intermediate composing text; sync only after composition ends
- Do **not** use buffered “whole string + Enter” while in live mode

## Cleanup (prevent remote residue)

Any non-successful-submit exit must leave the remote agent without a dangling `/…` in its input:

1. Always send `Escape` first.
2. Optional second pass: if the pane still looks stuck in a slash menu / input still shows a leading `/`, send up to **8** `backspace` keys.
3. Fallback palette must run this cleanup **before** opening, so a later hardcoded `/clear` is not concatenated onto a leftover `/`.

## Refresh

While in `slash-live`, temporarily shorten pane refresh interval (target **300–500ms**). Restore the previous interval on exit.

## Fallback

- Keep existing `COMMANDS` / `getAgentCommands()` / `runCommand()` behavior.
- Trigger when live sync fails (WS down / send error) or timeout without a usable remote slash menu signal.
- “Slash menu signal” for v1: best-effort heuristic from recent pane text (e.g. lines looking like `/command` menus). If heuristic is inconclusive, prefer timeout → fallback rather than hanging in live forever.
- Demo worker / environments without a real agent menu: fallback must still work.

## Out of scope

- Building a tappable list by scraping the remote menu
- Using `/help` + Enter as the discovery path
- Changing Telegram / TUI / iOS / macOS clients
- New herdr socket APIs for command enumeration
- Reworking Quick Actions / snippets

## Acceptance

1. Normal Chinese/English messages remain locally buffered and send as one unit on Enter.
2. Tapping `/` opens the remote agent’s native slash UI (when the agent supports it); ↑↓ / Enter / Esc via Keys work.
3. Esc or leaving the terminal does not leave a remote `/…` residue.
4. On live failure/timeout, hardcoded palette opens and remote is cleaned first.
5. Demo / no-menu agents still get a usable fallback palette.

## Implementation notes

- Concentrate changes in `web/index.html` around `#termInput`, `openCommandPalette`, `sendText`, and key handlers.
- Prefer a small explicit state flag/enum (`inputMode`) over scattering live checks.
- Reuse existing WS message types only: `send_text`, `send_keys`, `read_pane`.
