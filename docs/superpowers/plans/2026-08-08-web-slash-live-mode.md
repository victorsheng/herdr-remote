# Web Slash Live Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the user starts a `/` command on web, sync keystrokes to the remote agent so its native slash menu appears; keep normal chat locally buffered; fall back to the hardcoded command palette on failure.

**Architecture:** Add an `inputMode` (`buffered` | `slash-live`) in `web/index.html`. Enter live via `/` button or a leading `/` in the input; sync chars with `send_text` / `send_keys`; exit with cleanup (`Escape` + optional backspaces). Pure helpers (`looksLikeSlashMenu`, `buildSlashCleanupKeys`, mode transition predicates) stay global for `web/tests/slash-live.test.html` iframe tests. No relay/herdr API changes.

**Tech Stack:** Single-file Web app (`web/index.html`), existing WS types (`send_text`, `send_keys`, `read_pane`), HTML fixture tests under `web/tests/`.

**Spec:** `docs/superpowers/specs/2026-08-08-web-slash-live-mode-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `web/index.html` | `inputMode` state machine; LIVE affordance; termInput live sync; `/` button; cleanup; refresh interval; timeout → fallback |
| `web/tests/slash-live.test.html` | Fixture tests for menu heuristic, cleanup keys, enter/exit predicates, Send-in-live behavior helpers |

---

### Task 1: Pure helpers + fixture tests (TDD)

**Files:**
- Create: `web/tests/slash-live.test.html`
- Modify: `web/index.html` (add helpers just above `// --- Command Palette ---` ~line 1940)

- [ ] **Step 1: Write the failing test page**

Create `web/tests/slash-live.test.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Slash Live Mode 测试 · herdr-remote</title>
<style>
  body { font-family: -apple-system, system-ui, 'PingFang SC', sans-serif; margin: 0; padding: 20px;
         background: #1a1b26; color: #c0caf5; line-height: 1.5; }
  h1 { font-size: 1.1rem; margin: 0 0 4px; }
  .sub { color: #565f89; font-size: 0.8rem; margin-bottom: 16px; }
  table { border-collapse: collapse; width: 100%; font-size: 0.82rem; margin-bottom: 20px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #2f3549; }
  th { color: #565f89; font-weight: 600; font-size: 0.72rem; }
  .pass { color: #9ece6a; font-weight: 600; }
  .fail { color: #f7768e; font-weight: 600; }
  #summary { padding: 12px 14px; border-radius: 8px; margin-bottom: 18px; font-weight: 600; }
  #summary.ok { background: rgba(158,206,106,0.12); border: 1px solid #9ece6a; color: #9ece6a; }
  #summary.bad { background: rgba(247,118,142,0.12); border: 1px solid #f7768e; color: #f7768e; }
  #frame { position: absolute; left: -99999px; top: 0; width: 390px; height: 844px; border: 0; }
</style>
</head>
<body>
<h1>Slash Live Mode 测试</h1>
<div class="sub">加载 <code>../index.html</code>，验证菜单启发式、清理键序、进入/退出判定</div>
<div id="summary">运行中…</div>
<div id="results"></div>
<iframe id="frame" src="../index.html"></iframe>
<script>
const rows = [];
let allPass = true;
function record(name, actual, expected, ok) {
  if (!ok) allPass = false;
  rows.push({ name, actual, expected, ok });
}
function run(win) {
  const {
    looksLikeSlashMenu,
    buildSlashCleanupKeys,
    shouldEnterSlashLive,
    SLASH_LIVE_TIMEOUT_MS,
    SLASH_SCRUB_BACKSPACES,
  } = win;

  record('looksLikeSlashMenu 存在', typeof looksLikeSlashMenu, 'function', typeof looksLikeSlashMenu === 'function');
  if (typeof looksLikeSlashMenu !== 'function') return;

  record('空文本不成菜单', String(looksLikeSlashMenu('')), 'false', looksLikeSlashMenu('') === false);
  record('普通日志不成菜单', String(looksLikeSlashMenu('Running tests…\nok')), 'false', looksLikeSlashMenu('Running tests…\nok') === false);
  const menuish = 'Commands\n  /clear   Clear conversation\n  /compact Compact context\n  /model   Switch model';
  record('多条 /cmd 视为菜单', String(looksLikeSlashMenu(menuish)), 'true', looksLikeSlashMenu(menuish) === true);
  record('单条 /foo 不够', String(looksLikeSlashMenu('try /foo later')), 'false', looksLikeSlashMenu('try /foo later') === false);

  const keysNoScrub = buildSlashCleanupKeys({ scrub: false });
  record('清理仅 Escape', JSON.stringify(keysNoScrub), '["Escape"]', JSON.stringify(keysNoScrub) === JSON.stringify(['Escape']));
  const keysScrub = buildSlashCleanupKeys({ scrub: true });
  record('清理 Escape+backspace', String(keysScrub[0] === 'Escape' && keysScrub.length === 1 + SLASH_SCRUB_BACKSPACES), 'true',
    keysScrub[0] === 'Escape' && keysScrub.length === 1 + SLASH_SCRUB_BACKSPACES && keysScrub.every((k, i) => i === 0 || k === 'backspace'));

  record('空输入打 / 进入', String(shouldEnterSlashLive('', '/')), 'true', shouldEnterSlashLive('', '/') === true);
  record('已有正文打 / 不进', String(shouldEnterSlashLive('hello', '/')), 'false', shouldEnterSlashLive('hello', '/') === false);
  record('空输入打 a 不进', String(shouldEnterSlashLive('', 'a')), 'false', shouldEnterSlashLive('', 'a') === false);

  record('超时常量 1500', String(SLASH_LIVE_TIMEOUT_MS), '1500', SLASH_LIVE_TIMEOUT_MS === 1500);
  record('scrub 上限 8', String(SLASH_SCRUB_BACKSPACES), '8', SLASH_SCRUB_BACKSPACES === 8);
}
function finish() {
  const el = document.getElementById('results');
  el.innerHTML = '<table><tr><th>用例</th><th>实际</th><th>期望</th><th></th></tr>' +
    rows.map(r => `<tr><td>${r.name}</td><td>${r.actual}</td><td>${r.expected}</td><td class="${r.ok?'pass':'fail'}">${r.ok?'PASS':'FAIL'}</td></tr>`).join('') +
    '</table>';
  const s = document.getElementById('summary');
  s.textContent = allPass ? `全部通过 (${rows.length})` : `失败 (${rows.filter(r=>!r.ok).length}/${rows.length})`;
  s.className = allPass ? 'ok' : 'bad';
}
const frame = document.getElementById('frame');
frame.addEventListener('load', () => {
  try { run(frame.contentWindow); } catch (e) { record('运行异常', String(e), 'no throw', false); }
  finish();
});
</script>
</body>
</html>
```

- [ ] **Step 2: Open the test and confirm helpers are missing**

Open `web/tests/slash-live.test.html` in a browser (or `npx --yes serve web -p 8765` then visit `/tests/slash-live.test.html`).

Expected: FAIL — `looksLikeSlashMenu 存在` actual `undefined`.

- [ ] **Step 3: Add pure helpers to `web/index.html`**

Insert above `// --- Command Palette ---`:

```javascript
// --- Slash Live Mode helpers (also covered by web/tests/slash-live.test.html) ---
var SLASH_LIVE_TIMEOUT_MS = 1500;
var SLASH_SCRUB_BACKSPACES = 8;
var SLASH_REFRESH_MS = 400;
var BUFFERED_REFRESH_MS = 3000;

function looksLikeSlashMenu(text) {
  if (!text || typeof text !== 'string') return false;
  // 至少两行形如可选空白 + /命令名，避免把正文里偶然的 "/foo" 当成菜单
  const hits = text.split('\n').filter(line => /^\s*\/[a-zA-Z][\w-]*/.test(line));
  return hits.length >= 2;
}

function buildSlashCleanupKeys({ scrub }) {
  const keys = ['Escape'];
  if (scrub) {
    for (let i = 0; i < SLASH_SCRUB_BACKSPACES; i++) keys.push('backspace');
  }
  return keys;
}

/** buffered 下：仅当输入框当前为空且即将输入的是 `/` 时进入 live */
function shouldEnterSlashLive(currentValue, incomingChar) {
  return (!currentValue || currentValue === '') && incomingChar === '/';
}
```

- [ ] **Step 4: Re-run fixture test**

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/tests/slash-live.test.html
git commit -m "$(cat <<'EOF'
feat(web): add slash-live pure helpers and fixture tests

EOF
)"
```

---

### Task 2: Mode state + LIVE affordance

**Files:**
- Modify: `web/index.html` (markup near `#termInput` ~418–423; CSS; JS state)

- [ ] **Step 1: Add LIVE badge markup**

Inside `.term-input`, after the `/` button and before `#termInput`:

```html
<span id="slashLiveBadge" hidden style="font-size:0.65rem;font-weight:700;letter-spacing:0.04em;color:var(--blue);flex-shrink:0">LIVE /</span>
```

- [ ] **Step 2: Add mode state + UI updater**

Near other terminal globals (`activePane`, etc.):

```javascript
var inputMode = 'buffered'; // 'buffered' | 'slash-live'
var slashLiveTimer = null;
var slashMenuSeen = false;
var slashComposing = false;

function setInputMode(mode) {
  inputMode = mode;
  const badge = document.getElementById('slashLiveBadge');
  if (badge) badge.hidden = mode !== 'slash-live';
  const input = document.getElementById('termInput');
  if (input) input.placeholder = mode === 'slash-live' ? 'Slash live…' : 'Type…';
}
```

- [ ] **Step 3: Manual smoke**

In browser DevTools on a loaded page: `setInputMode('slash-live')` shows badge; `setInputMode('buffered')` hides it.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): add slash-live mode badge and state flag

EOF
)"
```

---

### Task 3: Enter / exit slash-live + rewire `/` button

**Files:**
- Modify: `web/index.html` (`openCommandPalette`, new enter/exit helpers, `closeTerminal`, `openTerminal`)

- [ ] **Step 1: Implement enter/exit/cleanup**

```javascript
function clearSlashLiveTimer() {
  if (slashLiveTimer) { clearTimeout(slashLiveTimer); slashLiveTimer = null; }
}

function setPaneRefreshInterval(ms) {
  if (refreshInterval) clearInterval(refreshInterval);
  if (activePane) refreshInterval = setInterval(refreshPane, ms);
}

function cleanupSlashRemote({ scrub }) {
  if (!ws || !activePane) return;
  const keys = buildSlashCleanupKeys({ scrub });
  ws.send(JSON.stringify({ type: 'send_keys', pane_id: activePane, keys }));
}

function exitSlashLive({ submitted, scrub, openFallback } = {}) {
  if (inputMode !== 'slash-live') {
    if (openFallback) openCommandPaletteFallback();
    return;
  }
  clearSlashLiveTimer();
  if (!submitted) cleanupSlashRemote({ scrub: !!scrub });
  setInputMode('buffered');
  slashMenuSeen = false;
  const input = document.getElementById('termInput');
  if (input && !submitted) input.value = '';
  setPaneRefreshInterval(BUFFERED_REFRESH_MS);
  if (openFallback) openCommandPaletteFallback();
}

function enterSlashLive({ seedSlash }) {
  if (!ws || !activePane) {
    openCommandPaletteFallback();
    return;
  }
  const input = document.getElementById('termInput');
  if (input) input.value = seedSlash ? '/' : (input.value || '');
  setInputMode('slash-live');
  slashMenuSeen = false;
  setPaneRefreshInterval(SLASH_REFRESH_MS);
  if (seedSlash) {
    ws.send(JSON.stringify({ type: 'send_text', pane_id: activePane, text: '/' }));
  }
  refreshPane();
  clearSlashLiveTimer();
  slashLiveTimer = setTimeout(() => {
    if (inputMode === 'slash-live' && !slashMenuSeen) {
      exitSlashLive({ submitted: false, scrub: true, openFallback: true });
    }
  }, SLASH_LIVE_TIMEOUT_MS);
}

/** Keep old palette entry point name for fallback only */
function openCommandPaletteFallback() {
  document.getElementById('cmdPalette').style.display = '';
  document.getElementById('cmdSearch').value = '';
  filterCommands();
  document.getElementById('cmdSearch').focus();
}

function openCommandPalette() {
  // `/` button: enter live (do not open local palette immediately)
  if (window.cue) cue('page');
  const input = document.getElementById('termInput');
  if (input) input.value = '';
  enterSlashLive({ seedSlash: true });
  if (input) input.focus();
}
```

- [ ] **Step 2: Hook terminal open/close**

In `closeTerminal`, before clearing `activePane`:

```javascript
exitSlashLive({ submitted: false, scrub: true });
```

In `openTerminal`, at the start (when switching panes while already in a terminal):

```javascript
if (activePane && activePane !== paneId) {
  exitSlashLive({ submitted: false, scrub: true });
}
```

Also ensure `openTerminal` still ends with `setInterval(refreshPane, BUFFERED_REFRESH_MS)` (replace bare `3000`).

- [ ] **Step 3: Manual check**

With a real agent: tap `/` → badge shows, pane refresh faster, remote receives `/`. Tap back → cleanup runs, badge clears.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): enter slash-live from / button with cleanup on exit

EOF
)"
```

---

### Task 4: Live keystroke sync on `#termInput`

**Files:**
- Modify: `web/index.html` (replace the single Enter keydown listener ~1931; update `sendText`)

- [ ] **Step 1: Replace termInput listeners**

Remove:

```javascript
document.getElementById('termInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();sendText();}});
```

Add:

```javascript
(function wireTermInput() {
  const input = document.getElementById('termInput');
  if (!input) return;

  input.addEventListener('compositionstart', () => { slashComposing = true; });
  input.addEventListener('compositionend', (e) => {
    slashComposing = false;
    if (inputMode !== 'slash-live' || !ws || !activePane) return;
    const text = e.data || '';
    if (text) ws.send(JSON.stringify({ type: 'send_text', pane_id: activePane, text }));
    refreshPane();
  });

  input.addEventListener('beforeinput', (e) => {
    if (inputMode === 'buffered' && e.inputType === 'insertText' && shouldEnterSlashLive(input.value, e.data || '')) {
      e.preventDefault();
      enterSlashLive({ seedSlash: true });
      return;
    }
    if (inputMode !== 'slash-live' || slashComposing) return;
    if (!ws || !activePane) return;

    if (e.inputType === 'insertText' && e.data) {
      // Let the character appear locally; sync the same chunk to remote
      ws.send(JSON.stringify({ type: 'send_text', pane_id: activePane, text: e.data }));
      refreshPane();
      return;
    }
    if (e.inputType === 'deleteContentBackward' || e.inputType === 'deleteContentForward') {
      ws.send(JSON.stringify({ type: 'send_keys', pane_id: activePane, keys: ['backspace'] }));
      refreshPane();
    }
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (inputMode === 'slash-live') {
        if (!ws || !activePane) return;
        ws.send(JSON.stringify({ type: 'send_keys', pane_id: activePane, keys: ['Enter'] }));
        input.value = '';
        exitSlashLive({ submitted: true });
        setTimeout(refreshPane, 500);
        return;
      }
      sendText();
      return;
    }
    if (e.key === 'Escape' && inputMode === 'slash-live') {
      e.preventDefault();
      exitSlashLive({ submitted: false, scrub: true });
      setTimeout(refreshPane, 300);
    }
  });
})();
```

- [ ] **Step 2: Fix `sendText` for live mode**

```javascript
function sendText() {
  const i = document.getElementById('termInput');
  if (!ws || !activePane) return;
  if (inputMode === 'slash-live') {
    // chars already synced — only submit
    ws.send(JSON.stringify({ type: 'send_keys', pane_id: activePane, keys: ['Enter'] }));
    i.value = '';
    exitSlashLive({ submitted: true });
    setTimeout(refreshPane, 500);
    return;
  }
  if (!i.value) return;
  ws.send(JSON.stringify({ type: 'send_text', pane_id: activePane, text: i.value }));
  ws.send(JSON.stringify({ type: 'send_keys', pane_id: activePane, keys: ['Enter'] }));
  i.value = '';
  setTimeout(refreshPane, 500);
}
```

- [ ] **Step 3: Manual check**

1. Type `hello` then Enter → still one buffered send (unchanged).  
2. Type `/` → enters live; type `c` → remote shows filter; Backspace syncs.  
3. IME: composition intermediates must not flood remote.

- [ ] **Step 4: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): sync termInput keystrokes while in slash-live

EOF
)"
```

---

### Task 5: Keys dock Escape/Enter + menu signal + scrub

**Files:**
- Modify: `web/index.html` (`fireKey`, `pane_content` handler)

- [ ] **Step 1: Teach `fireKey` about slash-live**

At the top of `fireKey(k)` after cue:

```javascript
  if (inputMode === 'slash-live' && (k === 'Escape' || k === 'Enter') && keyQueue.length === 0 && !armedMod) {
    if (k === 'Enter') {
      sendKeys(['Enter']);
      const input = document.getElementById('termInput');
      if (input) input.value = '';
      exitSlashLive({ submitted: true });
      return;
    }
    exitSlashLive({ submitted: false, scrub: true });
    setTimeout(refreshPane, 300);
    return;
  }
```

- [ ] **Step 2: Detect menu signal in `pane_content`**

Inside `msg.type === 'pane_content'` after rendering:

```javascript
    if (inputMode === 'slash-live' && !slashMenuSeen && looksLikeSlashMenu(msg.content || '')) {
      slashMenuSeen = true;
      clearSlashLiveTimer();
    }
```

- [ ] **Step 3: Optional scrub path after Escape**

Update `exitSlashLive` so non-submitted exits always send Escape first; if `scrub` is true, include backspaces in the same `send_keys` batch via `buildSlashCleanupKeys({ scrub: true })` (already in Task 3). No second round-trip required for v1.

- [ ] **Step 4: Manual check**

Open Keys → Esc while live → remote menu dismissed, local badge off, no dangling `/`. Enter on a highlighted remote command submits and exits live.

- [ ] **Step 5: Commit**

```bash
git add web/index.html
git commit -m "$(cat <<'EOF'
feat(web): wire Keys Esc/Enter and slash-menu detection for live mode

EOF
)"
```

---

### Task 6: Fallback path + acceptance pass

**Files:**
- Modify: `web/index.html` (ensure timeout/error → fallback; `runCommand` unchanged)
- Modify: `web/tests/slash-live.test.html` (optional: assert `openCommandPalette` no longer required for helpers)

- [ ] **Step 1: WS-down fallback on `/`**

Confirm `enterSlashLive` already calls `openCommandPaletteFallback()` when `!ws || !activePane`. Also guard sends:

```javascript
function liveSendText(text) {
  if (!ws || !activePane) {
    exitSlashLive({ submitted: false, scrub: false, openFallback: true });
    return false;
  }
  ws.send(JSON.stringify({ type: 'send_text', pane_id: activePane, text }));
  return true;
}
```

Use `liveSendText` from `enterSlashLive` / `beforeinput` instead of raw `ws.send` for text (keys can keep using `sendKeys`).

- [ ] **Step 2: Keep fallback palette behavior**

`runCommand` stays as today (`send_text` + `Enter`). Ensure timeout path uses `openFallback: true` **after** cleanup (Task 3 already orders cleanup before opening palette).

- [ ] **Step 3: Re-run fixture tests**

Open `web/tests/slash-live.test.html` — all PASS.

- [ ] **Step 4: Acceptance checklist (manual, real relay + agent)**

1. Normal Chinese/English message: buffered, one send on Enter.  
2. Tap `/`: remote native slash UI appears; ↑↓/Enter/Esc via Keys work.  
3. Esc or leave terminal: no remote `/…` residue.  
4. Disconnect WS or use demo without menu: after ~1.5s (or immediately if no WS), hardcoded palette opens; remote cleaned when WS was up.  
5. Typing `/model` filter chars in live updates remote menu.

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/tests/slash-live.test.html
git commit -m "$(cat <<'EOF'
feat(web): slash-live fallback to hardcoded command palette

EOF
)"
```

---

## Spec coverage check

| Spec requirement | Task |
|------------------|------|
| `buffered` vs `slash-live` modes | 2, 3 |
| Enter via `/` button | 3 |
| Enter via leading `/` | 4 |
| LIVE affordance | 2 |
| Exit Esc / Enter / close / switch pane | 3, 5 |
| Timeout only until menu signal | 3, 5 |
| Keystroke sync + IME | 4 |
| Send button does not double-send in live | 4 |
| Cleanup Escape + scrub backspaces | 1, 3 |
| Faster refresh 300–500ms | 3 (`SLASH_REFRESH_MS = 400`) |
| Hardcoded fallback | 3, 6 |
| `looksLikeSlashMenu` heuristic | 1, 5 |
| No relay API / no `/help` / no scrape-to-list | honored (out of scope) |

## Placeholder / consistency check

- Names locked: `inputMode`, `enterSlashLive`, `exitSlashLive`, `looksLikeSlashMenu`, `buildSlashCleanupKeys`, `shouldEnterSlashLive`, `openCommandPaletteFallback`, `SLASH_LIVE_TIMEOUT_MS`, `SLASH_SCRUB_BACKSPACES`, `SLASH_REFRESH_MS`, `BUFFERED_REFRESH_MS`.
- `/` button still calls `openCommandPalette()` but that function now enters live; palette UI opens only via `openCommandPaletteFallback()`.
