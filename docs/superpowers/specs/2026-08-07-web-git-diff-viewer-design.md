# Web: Git status file list → per-file diff / full content

**Date:** 2026-08-07  
**Status:** Approved for implementation planning  
**Scope:** Web client (`web/index.html`) + relay (`relay/herdr_relay.py`) + unit tests

## Goal

From the existing Git status overlay, let the user open a changed file and inspect either its unified diff or its current full file content (no compare), including optional comparison against a configurable base branch.

## Background

- Web already has Git status entry points (terminal header icon; Space menu → Git status…).
- Relay already resolves `pane_id` / `workspace_id` → `(cwd, remote)` and runs read-only `git status --porcelain=v1 -b`.
- Status UI currently shows only filenames / porcelain text; there is no per-file diff or file viewer.
- Agents often leave dirty worktrees; phone users need to review changes without SSH.

## Decisions

| Topic | Choice |
|-------|--------|
| Diff scope | Both: **working tree vs HEAD** (default) and **vs base branch** |
| Base branch | Auto-detect `origin/main` → `main` → `master`; UI can override |
| Open file UX | Tap filename → second overlay (mobile-friendly) |
| Diff presentation | Unified diff text with `+` / `-` / hunk coloring |
| Full content | Same overlay; toggle **Diff** (default) ↔ **全文** |
| Data loading | On-demand per file (`git_diff` / `git_show`); do not bundle all diffs in status |
| Clients | Web only this round |
| Out of scope | Side-by-side diff, edit, stage/commit/push, syntax highlight, binary preview |

## Approach

**On-demand fetch (recommended):** keep `git_status` for the list; when the user taps a path, send `git_diff` (or `git_show` for full text). Caps payload size and works over SSH remotes.

Rejected alternatives:

- Bundle all file diffs in one status response — too large / timeout-prone.
- Single mega-patch then client-split — fragile parsing; untracked files awkward.

## Protocol

### Client → Relay

```json
{ "type": "git_status", "pane_id": "<id>" }
```

```json
{ "type": "git_status", "workspace_id": "<id>" }
```

Existing `git_status` remains the source for **Working tree** file rows.

For **vs base** file rows, the client sends:

```json
{
  "type": "git_status",
  "pane_id": "<id>",
  "mode": "base",
  "base": "origin/main"
}
```

Relay then lists files via `git diff --name-status <resolved_base>` (plus untracked from porcelain if useful), and still returns `files[]`, `text`, `resolved_base`. Omitting `mode` or `mode=worktree` keeps today’s porcelain status behavior.

```json
{
  "type": "git_diff",
  "pane_id": "<id>",
  "path": "relative/path",
  "mode": "worktree",
  "base": ""
}
```

```json
{
  "type": "git_diff",
  "workspace_id": "<id>",
  "path": "relative/path",
  "mode": "base",
  "base": "origin/main"
}
```

- `mode`: `worktree` | `base` (default `worktree`)
- `base`: used when `mode=base`; empty → server auto-detects and returns `resolved_base`

```json
{
  "type": "git_show",
  "pane_id": "<id>",
  "path": "relative/path"
}
```

Reads the current working-tree file contents (no diff).

### Relay → Client

Success:

```json
{
  "type": "git_diff",
  "ok": true,
  "path": "relative/path",
  "mode": "worktree",
  "base": "",
  "resolved_base": "",
  "text": "diff --git ...",
  "truncated": false
}
```

```json
{
  "type": "git_show",
  "ok": true,
  "path": "relative/path",
  "text": "...",
  "truncated": false
}
```

Failure (same style as status):

```json
{ "type": "git_diff", "ok": false, "message": "...", "path": "..." }
```

### Relay commands (read-only)

| Intent | Behavior |
|--------|----------|
| Resolve target | Reuse `_resolve_git_target(pane_id, workspace_id)` |
| Detect base | `git rev-parse --verify` for `origin/main`, then `main`, then `master` |
| worktree diff | `git diff HEAD -- path` (includes staged + unstaged vs HEAD for tracked files) |
| base diff | `git diff <resolved_base> -- path` |
| base file list | `git diff --name-status <resolved_base>` mapped into the same `files[{status,path}]` shape |
| Untracked (`??`) | Diff as full-file add vs `/dev/null`; full view still reads the file |
| Full content | Local file read or SSH `cat` with quoted path under `cwd` |
| Truncation | Cap response text ~**200KB**; set `truncated: true` |
| Binary | Reject with clear `message` (no base64) |
| Timeout | ~10s (aligned with status) |
| Path safety | Reject `..`, absolute paths, and paths that escape `cwd` |
| Audit | Log `git_diff` / `git_show` like `git_status` |

Remote hosts: wrap the same git/file commands via existing SSH pattern used by `fetch_git_status_async`.

## Web UI

### Status overlay (enhance existing)

- Segmented control: **Working tree** | **vs base**
- In base mode: editable base field, prefilled with `resolved_base` when known
- Render clickable file rows (`status` + `path`) instead of only a dead `<pre>` dump (keep a compact summary if useful)
- Clean tree: keep current clean messaging

### File overlay (new)

- Title: filename; subtitle: mode / base
- Toggle: **Diff** (default) | **全文**
- Body: monospace `<pre>`; colorize diff lines starting with `+`, `-`, `@@`
- If `truncated`: footer note
- Close / backdrop returns to status overlay; closing status clears both

### Caching (optional, light)

- After a successful `git_diff` for `(path, mode, base)`, switching to 全文 and back may reuse the last diff text without refetch; refetch if mode/base/path changes.

## Testing

- Unit tests for path sanitization, truncation flag, untracked handling, base detection helpers, and WebSocket branches for `git_diff` / `git_show` (extend `tests/test_relay_git_status.py` or add `tests/test_relay_git_diff.py`).
- Manual Web check: open status → tap file → diff → toggle 全文 → switch base → reopen file.
- No required browser automation this round.

## Non-goals

- Side-by-side / word-level diff
- Editing, staging, committing, pushing from Web
- Syntax highlighting beyond +/-/hunk colors
- Binary or image preview
- iOS / Telegram / TUI clients

## Implementation notes

- Depends on (or lands with) the in-progress Git status feature already present in local `web/index.html` + `relay/herdr_relay.py`.
- Prefer small helpers next to existing `_parse_git_porcelain` / `fetch_git_status_async` rather than a new module unless the file becomes unwieldy.
