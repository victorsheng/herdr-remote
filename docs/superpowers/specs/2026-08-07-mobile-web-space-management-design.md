# Mobile Web: Space create / rename / close

**Date:** 2026-08-07  
**Status:** Approved for implementation planning  
**Scope:** Web client (`web/index.html`) + relay (`relay/herdr_relay.py`) only

## Goal

Let the mobile Web client manage Spaces the same way it already creates Tabs: one-tap create, plus rename and close for the selected Space.

## Background

- Web already sends `create_tab` → relay runs `herdr tab create --workspace <id> --focus`.
- Spaces chip strip is hidden when `workspaces.length <= 1`, so there is no place to create a second Space from the phone.
- Relay has no `create_workspace` / `rename_workspace` / `close_workspace` handlers.
- CLI already supports:
  - `herdr workspace create [--focus]`
  - `herdr workspace rename <WORKSPACE_ID> <LABEL>...`
  - `herdr workspace close <workspace_id>`

## Decisions

| Topic | Choice |
|-------|--------|
| Clients | Web only (phone browser / PWA) |
| Create UX | One-tap, no cwd/label form |
| Create entry | Always available (`+` on Spaces strip for 0 / 1 / many Spaces) |
| Approach | Mirror `create_tab` end-to-end |
| Rename / Close entry | When a Space is selected, show `⋯` → sheet with Rename / Close |
| Close confirm | Always confirm (「关闭这个 Space？」) |
| After close | Clear `activeWorkspace`, return to All |
| After create / rename | Do not invent local IDs; wait for agent snapshot / updates |
| Out of scope | Tab rename/close, create form (cwd/label), iOS / Telegram / TUI |

## Protocol

### Client → Relay

```json
{ "type": "create_workspace" }
```

```json
{ "type": "rename_workspace", "workspace_id": "<id>", "label": "<text>" }
```

```json
{ "type": "close_workspace", "workspace_id": "<id>" }
```

### Relay behavior

| Message | herdr invocation | Success reply |
|---------|------------------|---------------|
| `create_workspace` | `workspace create --focus` | `{ "type": "workspace_created", "ok": true }` |
| `rename_workspace` | `workspace rename <id> <label>` | `{ "type": "workspace_renamed", "ok": true }` |
| `close_workspace` | `workspace close <id>` | `{ "type": "workspace_closed", "ok": true }` |

Validation:

- `rename_workspace` / `close_workspace` missing `workspace_id` → `error`
- `rename_workspace` with empty/missing `label` → `error`
- Command failures → existing `error` path; audit on invoke (same timing pattern as `create_tab`)

State refresh remains push-driven via `agents` / `agent_update`. Clients must not locally fabricate workspace IDs. After rename, the Spaces chip label continues to come from the next snapshot (`project` / agent label); do not patch the chip label client-side.

## Web UI

### Spaces strip (always rendered)

- Always show the Spaces chip strip, including when there are 0 or 1 Spaces.
- Chips:
  - 0 Spaces: only `+`
  - 1+ Spaces: `All`, one chip per Space, trailing dashed `+`
  - When a specific Space is selected (not All), also show `⋯`

### `⋯` sheet

- Actions: **Rename…**, **Close…** (destructive styling).
- Opens only while a Space is selected.

### Rename modal

- Text field prefilled with current display label when available.
- Cancel / Save.
- Empty label: do not send (disable Save or ignore click).

### Close confirm modal

- Copy: 「关闭这个 Space？」
- Cancel / Close.
- On `workspace_closed` with `ok: true` only: set `activeWorkspace = null`, `activeTab = null`, re-render All. Do not clear selection optimistically on send.

### Tabs strip

- Unchanged; keep existing Tab `+` → `create_tab`.

### Disconnected behavior

- If WebSocket is down, create / rename / close are no-ops (same as `createTab` today).

## Error handling

- Relay validation and herdr failures return `error`; Web should not clear selection on failed close.
- Prefer lightweight feedback consistent with existing Web patterns (toast if present; otherwise avoid blocking the list).
- Rename empty label never leaves the client.

## Testing

- Relay unit tests (mocked herdr): happy path + missing-field errors for all three messages; assert CLI argv includes `--focus` on create and correct id/label on rename/close.
- Manual Web checks:
  - 0 / 1 / many Spaces: `+` visible
  - Select Space → `⋯` → Rename modal works
  - Close confirm → Space gone → back on All
  - Offline / disconnected: actions no-op

## Non-goals

- Renaming or closing Tabs from Web
- Prompting for `--cwd` / `--label` on create
- Native iOS, Telegram, or TUI support in this change
