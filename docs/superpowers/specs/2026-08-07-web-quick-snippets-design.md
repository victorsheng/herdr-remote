# Web Quick Actions: editable Snippets + denser layout

**Date:** 2026-08-07  
**Status:** Approved for implementation planning  
**Scope:** Web client (`web/index.html`) only

## Goal

Add three user-editable quick-send snippets to the existing Quick Actions dock, and tighten that dock’s layout so it wastes less vertical space on mobile.

## Background

- Quick Actions dock already has Confirm (`yes` / `no`) and Common (`continue` / `retry` / `skip` / `commit & push`) buttons that call `quickSend(text)` → `send_text` + `Enter`.
- The dock uses large padded buttons and generous section spacing, which feels sparse on phones.
- Users often send the same long agent instructions (e.g. parallel subagent workflow); they need named, editable presets rather than retyping.

## Decisions

| Topic | Choice |
|-------|--------|
| Placement | Extend existing Quick Actions dock (not a third keypad tab, not command palette) |
| Trigger | Tap snippet button → send immediately (same as `quickSend`) |
| Empty body | Tap opens edit UI; do not send |
| Labels | Editable title + body per slot (not fixed “1 / 2 / 3”) |
| Persistence | `localStorage` key `herdr_snippets` |
| Physical keyboard 1/2/3 | Out of scope (conflicts with digit pad / numbered terminal options) |
| Clients | Web only |
| Reset-to-default UI | Out of scope (clear localStorage manually if needed) |

## Data model

```json
[
  {
    "title": "并行",
    "body": "用 subagents 并行开发；本地开发不用 worktree；不要用 superpowers 相关的 TDD；最后统一验证。"
  },
  { "title": "Slot 2", "body": "" },
  { "title": "Slot 3", "body": "" }
]
```

- Exactly three slots.
- Missing key, invalid JSON, or wrong shape → fall back to the defaults above.
- After user edits, save the full three-slot array back to `localStorage`.

## Interaction

### Send

1. User opens Quick Actions dock (existing lightning control).
2. Taps a Snippets button whose `body` is non-empty (after trim).
3. Client runs existing `quickSend(body)`: haptic/cue if available, `send_text`, `Enter`, hide dock, refresh pane.

### Edit

- Entry: each snippet button shows a small edit control; long-press on the button also opens edit.
- UI: lightweight modal/sheet with:
  - title `<input>`
  - body `<textarea>`
  - Save / Cancel
- Save rules:
  - Persist to `herdr_snippets`
  - Re-render snippet buttons
  - If title is empty/whitespace, store fallback `Slot N` (1-based index)
- Empty body after save: button stays tappable but next tap re-opens edit (no send).

### Privacy / storage failures

- If `localStorage` throws (private mode): keep in-memory defaults/edits for the session; do not block the main terminal flow (same pattern as keypad state).

## UI layout (denser Quick Actions)

- Keep three sections: Confirm, Common, Snippets — with smaller section labels and reduced vertical gaps.
- Prefer compact 3-column grids and `nav-key`-like button heights over the current oversized quick-action padding.
- Confirm and Common retain existing commands; only spacing/typography/grid density change.
- Snippets row: three buttons showing **title** text (truncate with ellipsis if long).

## Out of scope

- Physical keyboard shortcuts
- Unlimited / reorderable / import-export snippets
- Relay, iOS, macOS, Telegram, TUI changes
- Changing blocked-agent Yes/No/Trust strip (`#quickActions`)

## Test plan

- Open Quick dock → tap default「并行」→ agent receives default body + Enter; dock closes.
- Tap「Slot 2」(empty) → edit opens; save title + body → button label updates.
- Tap the filled Slot 2 → sends new body.
- Reload page → titles/bodies still present.
- Confirm/Common buttons still send correctly; dock height is visibly shorter than before.
- With WS disconnected / no active pane → snippet send is a no-op (same as `quickSend`).
