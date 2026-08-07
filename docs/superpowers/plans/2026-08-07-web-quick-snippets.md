# Web Quick Actions Snippets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three editable quick-send snippets to the Quick Actions dock and densify that dock’s layout so it uses less vertical space on mobile.

**Architecture:** Keep everything in `web/index.html`. Persist a three-slot `{title, body}[]` in `localStorage` (`herdr_snippets`). Render a Snippets row in `#quickDock`; non-empty body taps call existing `quickSend`, empty body opens a small edit sheet. Pure load/normalize/save helpers stay global so `web/tests/snippets.test.html` can assert them via iframe (same pattern as keypad/touch tests).

**Tech Stack:** Single-file Web app (`web/index.html`), browser `localStorage`, HTML fixture tests under `web/tests/`.

**Spec:** `docs/superpowers/specs/2026-08-07-web-quick-snippets-design.md`

---

## File map

| File | Responsibility |
|------|----------------|
| `web/index.html` | Dense Quick dock markup/CSS; snippet load/save/render; edit sheet; send/edit wiring |
| `web/tests/snippets.test.html` | Fixture tests for defaults, normalize, empty→edit vs send, persistence round-trip, dock density markers |

---

### Task 1: Snippet storage helpers (TDD)

**Files:**
- Create: `web/tests/snippets.test.html`
- Modify: `web/index.html` (JS near `quickSend`, after `KEYPAD_STATE_KEY` block ~line 977)

- [ ] **Step 1: Write the failing test page**

Create `web/tests/snippets.test.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Quick Snippets 测试 · herdr-remote</title>
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
<h1>Quick Snippets 测试</h1>
<div class="sub">加载 <code>../index.html</code>，验证默认值、normalize、持久化</div>
<div id="summary">运行中…</div>
<div id="results"></div>
<iframe id="frame" src="../index.html"></iframe>
<script>
const rows = [];
let allPass = true;
let fatal = null;
function record(name, actual, expected, ok) {
  if (!ok) allPass = false;
  rows.push({ name, actual, expected, ok });
}
function run(win) {
  const { DEFAULT_SNIPPETS, loadSnippets, saveSnippets, normalizeSnippets, SNIPPETS_KEY } = win;
  if (typeof loadSnippets !== 'function') {
    record('loadSnippets 存在', '缺失', 'function', false);
    return;
  }
  record('loadSnippets 存在', 'function', 'function', true);

  try { win.localStorage.removeItem(SNIPPETS_KEY); } catch (e) {}
  const defaults = loadSnippets();
  record('默认 3 槽', String(defaults.length), '3', defaults.length === 3);
  record('槽0 标题=并行', defaults[0].title, '并行', defaults[0].title === '并行');
  record('槽0 正文非空', defaults[0].body ? 'non-empty' : 'empty', 'non-empty', !!defaults[0].body.trim());
  record('槽1/2 正文空',
    `1="${defaults[1].body}" 2="${defaults[2].body}"`,
    'both empty',
    !defaults[1].body.trim() && !defaults[2].body.trim());

  const bad = normalizeSnippets({ nope: true });
  record('坏数据回退默认', bad[0].title, '并行', bad[0].title === '并行' && bad.length === 3);

  const custom = [
    { title: 'A', body: 'aaa' },
    { title: '  ', body: 'bbb' },
    { title: 'C', body: '' },
  ];
  const norm = normalizeSnippets(custom);
  record('空标题→Slot 2', norm[1].title, 'Slot 2', norm[1].title === 'Slot 2');

  saveSnippets([
    { title: '并行', body: 'keep' },
    { title: 'Mine', body: 'hello' },
    { title: 'Slot 3', body: '' },
  ]);
  const loaded = loadSnippets();
  record('持久化 round-trip', loaded[1].body, 'hello', loaded[1].title === 'Mine' && loaded[1].body === 'hello');

  try { win.localStorage.removeItem(SNIPPETS_KEY); } catch (e) {}
}
function finish() {
  const el = document.getElementById('summary');
  el.className = allPass && !fatal ? 'ok' : 'bad';
  el.textContent = fatal ? ('FATAL: ' + fatal) : (allPass ? '全部通过' : '有失败');
  document.getElementById('results').innerHTML =
    '<table><tr><th>用例</th><th>实际</th><th>期望</th><th></th></tr>' +
    rows.map(r => `<tr><td>${r.name}</td><td>${r.actual}</td><td>${r.expected}</td>` +
      `<td class="${r.ok?'pass':'fail'}">${r.ok?'PASS':'FAIL'}</td></tr>`).join('') +
    '</table>';
}
document.getElementById('frame').onload = () => {
  try {
    const win = document.getElementById('frame').contentWindow;
    run(win);
  } catch (e) { fatal = String(e); allPass = false; }
  finish();
};
</script>
</body>
</html>
```

- [ ] **Step 2: Open the test and confirm it fails**

Run: open `web/tests/snippets.test.html` in a browser (or serve `web/` and visit `/tests/snippets.test.html`).

Expected: FAIL — `loadSnippets 存在` → 缺失

- [ ] **Step 3: Add helpers to `web/index.html`**

Insert after the `KEYPAD_STATE_KEY` / `saveKeypadState` block (before `toggleKeysDock`):

```javascript
const SNIPPETS_KEY = 'herdr_snippets';
const DEFAULT_SNIPPETS = [
  {
    title: '并行',
    body: '用 subagents 并行开发；本地开发不用 worktree；不要用 superpowers 相关的 TDD；最后统一验证。',
  },
  { title: 'Slot 2', body: '' },
  { title: 'Slot 3', body: '' },
];

function normalizeSnippets(raw) {
  if (!Array.isArray(raw) || raw.length !== 3) return DEFAULT_SNIPPETS.map(s => ({ ...s }));
  return raw.map((s, i) => {
    const title = (s && typeof s.title === 'string' ? s.title : '').trim() || `Slot ${i + 1}`;
    const body = s && typeof s.body === 'string' ? s.body : '';
    return { title, body };
  });
}

function loadSnippets() {
  try {
    const raw = localStorage.getItem(SNIPPETS_KEY);
    if (!raw) return DEFAULT_SNIPPETS.map(s => ({ ...s }));
    return normalizeSnippets(JSON.parse(raw));
  } catch (e) {
    return DEFAULT_SNIPPETS.map(s => ({ ...s }));
  }
}

function saveSnippets(list) {
  const normalized = normalizeSnippets(list);
  try { localStorage.setItem(SNIPPETS_KEY, JSON.stringify(normalized)); } catch (e) { /* privacy mode */ }
  return normalized;
}
```

Expose on `window` only if needed for tests — in this file top-level `function` / `const` in a classic script are already globals on `window`.

- [ ] **Step 4: Re-run test — expect PASS**

Expected: `全部通过`

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/tests/snippets.test.html
git commit -m "feat(web): add editable snippet storage helpers"
```

---

### Task 2: Dense Quick dock markup + Snippets row

**Files:**
- Modify: `web/index.html` (CSS near `.quick-actions` / `.nav-key`; HTML `#quickDock` ~360–379)
- Modify: `web/tests/snippets.test.html` (add DOM assertions)

- [ ] **Step 1: Extend the test with dock structure checks**

Inside `run(win)` after storage checks, add:

```javascript
  const doc = win.document;
  const tv = doc.querySelector('.terminal-view');
  if (tv) tv.classList.add('active');
  const dock = doc.getElementById('quickDock');
  if (dock) dock.style.display = '';

  const snippets = doc.getElementById('snippetButtons');
  record('snippetButtons 容器存在', snippets ? 'yes' : 'no', 'yes', !!snippets);

  if (typeof win.renderSnippets === 'function') {
    win.renderSnippets();
  }
  const btns = snippets ? snippets.querySelectorAll('.snippet-btn') : [];
  record('三个 snippet 按钮', String(btns.length), '3', btns.length === 3);

  const firstTitle = btns[0] && btns[0].querySelector('.snippet-title');
  record('默认标题「并行」可见', firstTitle ? firstTitle.textContent.trim() : '', '并行',
    firstTitle && firstTitle.textContent.trim() === '并行');

  const dense = dock && dock.classList.contains('quick-dock-dense');
  record('dock 使用 dense class', dense ? 'yes' : 'no', 'yes', !!dense);
```

- [ ] **Step 2: Confirm FAIL** (missing `#snippetButtons` / `renderSnippets`)

- [ ] **Step 3: Replace `#quickDock` inner structure**

Replace the current `#quickDock` block with:

```html
  <div class="term-keys quick-dock-dense" id="quickDock" style="display:none">
    <div style="display:flex;align-items:center;justify-content:space-between;width:100%;margin-bottom:4px">
      <span style="font-size:0.65rem;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--muted)">Quick Actions</span>
      <button onclick="toggleQuickDock()" aria-label="Close" style="background:none;border:none;color:var(--muted);font-size:1rem;cursor:pointer">✕</button>
    </div>
    <div style="width:100%">
      <p class="qd-label">Confirm</p>
      <div class="qd-grid qd-grid-2">
        <button class="nav-key qd-btn" style="color:var(--green);border-color:var(--green)" onclick="quickSend('yes')">yes</button>
        <button class="nav-key qd-btn" style="color:var(--red);border-color:var(--red)" onclick="quickSend('no')">no</button>
      </div>
      <p class="qd-label">Common</p>
      <div class="qd-grid qd-grid-3">
        <button class="nav-key qd-btn" onclick="quickSend('continue')">continue</button>
        <button class="nav-key qd-btn" onclick="quickSend('retry')">retry</button>
        <button class="nav-key qd-btn" onclick="quickSend('skip')">skip</button>
        <button class="nav-key qd-btn" onclick="quickSend('commit and push')" style="grid-column:1/-1">commit & push</button>
      </div>
      <p class="qd-label">Snippets</p>
      <div class="qd-grid qd-grid-3" id="snippetButtons"></div>
    </div>
  </div>
```

Add CSS (near other `.term-keys` / `.nav-key` rules):

```css
.quick-dock-dense .qd-label {
  font-size: 0.55rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--muted);
  margin: 4px 0 2px;
}
.quick-dock-dense .qd-grid { display: grid; gap: 3px; margin-bottom: 4px; }
.quick-dock-dense .qd-grid-2 { grid-template-columns: 1fr 1fr; }
.quick-dock-dense .qd-grid-3 { grid-template-columns: repeat(3, 1fr); }
.quick-dock-dense .qd-btn { min-height: 36px; padding: 6px 4px; font-size: 12px; }
.snippet-btn {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  min-height: 36px;
  padding: 6px 4px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  overflow: hidden;
}
.snippet-btn .snippet-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
.snippet-btn .snippet-edit {
  flex-shrink: 0;
  border: none;
  background: transparent;
  color: var(--muted);
  font-size: 11px;
  padding: 2px 4px;
  cursor: pointer;
  min-height: 28px;
  min-width: 28px;
}
```

- [ ] **Step 4: Add `renderSnippets`**

```javascript
function renderSnippets() {
  const host = document.getElementById('snippetButtons');
  if (!host) return;
  const list = loadSnippets();
  host.innerHTML = list.map((s, i) => {
    const title = s.title.replace(/</g, '&lt;').replace(/"/g, '&quot;');
    return `<div class="snippet-btn" data-snippet-idx="${i}" role="button" tabindex="0">` +
      `<span class="snippet-title">${title}</span>` +
      `<button type="button" class="snippet-edit" data-edit-idx="${i}" aria-label="Edit snippet ${i + 1}">✎</button>` +
      `</div>`;
  }).join('');
}
```

Call `renderSnippets()` at the end of `toggleQuickDock` when showing the dock, and once on page load after helpers exist:

```javascript
function toggleQuickDock() {
  const el = document.getElementById('quickDock');
  const keys = document.getElementById('termKeys');
  const show = el.style.display === 'none';
  el.style.display = show ? '' : 'none';
  if (show) {
    keys.style.display = 'none';
    renderSnippets();
  }
  saveKeypadState(show ? 'quick' : null);
  if (window.cue) cue(show ? 'page' : 'tick');
}
```

Also call `renderSnippets()` from `restoreKeypadState` when restoring `'quick'`.

- [ ] **Step 5: Re-run test — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/tests/snippets.test.html
git commit -m "feat(web): densify Quick Actions and render snippet buttons"
```

---

### Task 3: Edit sheet + empty-tap opens editor

**Files:**
- Modify: `web/index.html` (modal HTML + JS)
- Modify: `web/tests/snippets.test.html`

- [ ] **Step 1: Add test assertions for editor API**

```javascript
  record('openSnippetEditor 存在', typeof win.openSnippetEditor, 'function', typeof win.openSnippetEditor === 'function');
  record('fireSnippet 存在', typeof win.fireSnippet, 'function', typeof win.fireSnippet === 'function');

  // empty body → editor shown, no send
  let sent = [];
  const realWs = win.ws;
  win.ws = { send(data) { sent.push(JSON.parse(data)); } };
  win.activePane = 'pane-test';
  try { win.localStorage.removeItem(SNIPPETS_KEY); } catch (e) {}
  win.renderSnippets();
  win.fireSnippet(1); // Slot 2 empty
  const editor = doc.getElementById('snippetEditor');
  record('空槽打开编辑器', editor && editor.style.display !== 'none' ? 'open' : 'closed', 'open',
    editor && editor.style.display !== 'none');
  record('空槽不发送', String(sent.length), '0', sent.length === 0);

  // filled body → quickSend path
  sent = [];
  if (editor) editor.style.display = 'none';
  win.fireSnippet(0);
  record('非空槽发送 send_text',
    sent[0] && sent[0].type,
    'send_text',
    sent.some(m => m.type === 'send_text' && typeof m.text === 'string' && m.text.includes('subagents')));
  record('非空槽跟随 Enter',
    sent.some(m => m.type === 'send_keys') ? 'yes' : 'no',
    'yes',
    sent.some(m => m.type === 'send_keys' && m.keys && m.keys[0] === 'Enter'));

  win.ws = realWs;
  win.activePane = null;
```

- [ ] **Step 2: Confirm FAIL**

- [ ] **Step 3: Add editor markup** (after `#quickDock`, before command palette)

```html
<div id="snippetEditor" style="display:none;position:fixed;inset:0;z-index:110;">
  <div onclick="closeSnippetEditor()" style="position:absolute;inset:0;background:rgba(0,0,0,0.5)"></div>
  <div style="position:absolute;bottom:var(--kb-inset,0px);left:50%;transform:translateX(-50%);width:100%;max-width:420px;background:var(--surface);border-top-left-radius:16px;border-top-right-radius:16px;border-top:1px solid var(--border);padding:12px 16px;padding-bottom:max(env(safe-area-inset-bottom,8px) - var(--kb-inset,0px),12px);display:flex;flex-direction:column;gap:8px">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <span style="font-size:0.85rem;font-weight:600">Edit snippet</span>
      <button type="button" onclick="closeSnippetEditor()" aria-label="Close" style="background:none;border:none;color:var(--muted);font-size:1.2rem;cursor:pointer">✕</button>
    </div>
    <label style="font-size:0.7rem;color:var(--muted)">Title
      <input id="snippetTitleInput" type="text" maxlength="40" autocomplete="off"
        style="display:block;width:100%;margin-top:4px;padding:10px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:16px" />
    </label>
    <label style="font-size:0.7rem;color:var(--muted)">Body
      <textarea id="snippetBodyInput" rows="5"
        style="display:block;width:100%;margin-top:4px;padding:10px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:16px;resize:vertical"></textarea>
    </label>
    <div style="display:flex;gap:8px">
      <button type="button" onclick="closeSnippetEditor()" class="nav-key" style="flex:1">Cancel</button>
      <button type="button" onclick="commitSnippetEditor()" class="nav-key" style="flex:1;background:var(--blue);color:#fff;border-color:var(--blue)">Save</button>
    </div>
  </div>
</div>
```

- [ ] **Step 4: Wire editor + fireSnippet + event delegation**

```javascript
let snippetEditIdx = null;

function openSnippetEditor(idx) {
  const list = loadSnippets();
  if (idx < 0 || idx > 2) return;
  snippetEditIdx = idx;
  document.getElementById('snippetTitleInput').value = list[idx].title;
  document.getElementById('snippetBodyInput').value = list[idx].body;
  document.getElementById('snippetEditor').style.display = '';
}

function closeSnippetEditor() {
  document.getElementById('snippetEditor').style.display = 'none';
  snippetEditIdx = null;
}

function commitSnippetEditor() {
  if (snippetEditIdx == null) return;
  const list = loadSnippets();
  list[snippetEditIdx] = {
    title: document.getElementById('snippetTitleInput').value,
    body: document.getElementById('snippetBodyInput').value,
  };
  saveSnippets(list);
  closeSnippetEditor();
  renderSnippets();
}

function fireSnippet(idx) {
  const list = loadSnippets();
  const s = list[idx];
  if (!s) return;
  if (!s.body.trim()) {
    openSnippetEditor(idx);
    return;
  }
  quickSend(s.body);
}

(function wireSnippetButtons() {
  const host = document.getElementById('snippetButtons');
  if (!host) return;
  host.addEventListener('click', (e) => {
    const edit = e.target.closest('[data-edit-idx]');
    if (edit) {
      e.preventDefault();
      e.stopPropagation();
      openSnippetEditor(Number(edit.getAttribute('data-edit-idx')));
      return;
    }
    const btn = e.target.closest('[data-snippet-idx]');
    if (btn) fireSnippet(Number(btn.getAttribute('data-snippet-idx')));
  });
  host.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const btn = e.target.closest('[data-snippet-idx]');
    if (!btn) return;
    e.preventDefault();
    fireSnippet(Number(btn.getAttribute('data-snippet-idx')));
  });
  let pressTimer = null;
  host.addEventListener('pointerdown', (e) => {
    const btn = e.target.closest('[data-snippet-idx]');
    if (!btn || e.target.closest('[data-edit-idx]')) return;
    const idx = Number(btn.getAttribute('data-snippet-idx'));
    pressTimer = setTimeout(() => { pressTimer = null; openSnippetEditor(idx); }, 550);
  });
  host.addEventListener('pointerup', () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } });
  host.addEventListener('pointercancel', () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } });
  host.addEventListener('pointerleave', () => { if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; } });
})();
```

- [ ] **Step 5: Re-run test — expect PASS**

- [ ] **Step 6: Commit**

```bash
git add web/index.html web/tests/snippets.test.html
git commit -m "feat(web): snippet edit sheet and send-or-edit behavior"
```

---

### Task 4: Manual smoke + touch-target sanity

**Files:**
- Possibly adjust: `web/index.html` (only if snippet edit hit area &lt; 28px or dock regressions)

- [ ] **Step 1: Manual checklist** (serve `web/` or open `index.html`)

1. Open terminal view → lightning → Quick dock shows Confirm / Common / Snippets denser than before.
2. Tap「并行」with a live relay + pane → agent gets default body + Enter; dock closes.
3. Tap「Slot 2」→ editor opens; set title `foo`, body `bar`; Save → button shows `foo`.
4. Tap `foo` → sends `bar`.
5. Reload → `foo` / `bar` still there.
6. ✎ on a button opens editor without sending.
7. Long-press (~0.55s) opens editor.
8. yes / continue still work.

- [ ] **Step 2: If anything fails, fix minimally and re-run `web/tests/snippets.test.html`**

- [ ] **Step 3: Commit only if fixes landed**

```bash
git add web/index.html web/tests/snippets.test.html
git commit -m "fix(web): polish snippet dock interaction"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|------------------|------|
| Extend Quick Actions dock | Task 2 |
| Dense layout (smaller labels/gaps/buttons) | Task 2 |
| Three slots title+body in `herdr_snippets` | Task 1 |
| Default「并行」+ empty Slot 2/3 | Task 1 |
| Non-empty → `quickSend` | Task 3 |
| Empty → open edit | Task 3 |
| Edit via ✎ + long-press | Task 3 |
| Empty title → `Slot N` | Task 1 (`normalizeSnippets`) |
| localStorage failure soft-fail | Task 1 (`try/catch`) |
| No physical keyboard shortcuts | Not implemented |
| No reset-default UI / no relay changes | Not implemented |
| Fixture tests | Tasks 1–3 |

## Placeholder / consistency notes

- Storage key is always `herdr_snippets`.
- Helpers: `DEFAULT_SNIPPETS`, `normalizeSnippets`, `loadSnippets`, `saveSnippets`, `renderSnippets`, `fireSnippet`, `openSnippetEditor`, `closeSnippetEditor`, `commitSnippetEditor`.
- Send path reuses `quickSend` — do not duplicate WS send logic.
- Long-press threshold fixed at 550ms in Task 3.
