#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""herdr-remote relay — polls herdr, accepts push events (HTTP POST + WebSocket + UDP), broadcasts to clients."""
import asyncio, gzip, json, logging, os, re, secrets, shlex, shutil, signal, socket, struct, subprocess, time
from pathlib import Path

from agent_state import complete_agent_update_message

try:
    from websockets.asyncio.server import serve
except ImportError:
    from websockets.server import serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from logging.handlers import RotatingFileHandler
import sys

def _get_log_dir():
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Logs/herdr-remote")
    if os.path.isdir("/var/log") and os.access("/var/log", os.W_OK):
        return "/var/log/herdr-remote"
    return os.path.expanduser("~/.local/state/herdr-remote/log")

LOG_DIR = os.environ.get("HERDR_LOG_DIR", _get_log_dir())
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "relay.log")
AUDIT_FILE = os.path.join(LOG_DIR, "audit.log")

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setFormatter(_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_formatter)

log = logging.getLogger("herdr-relay")
log.setLevel(logging.INFO)
log.addHandler(_file_handler)
log.addHandler(_console_handler)
logging.getLogger("websockets").setLevel(logging.WARNING)

HERDR = os.environ.get("HERDR_BIN") or shutil.which("herdr") or "/opt/homebrew/bin/herdr"
WS_PORT = int(os.environ.get("HERDR_RELAY_PORT", "8375"))
POLL_INTERVAL = 2
# pane 推送两轮之间的让路间隔。read 本身约 7s（herdr 侧固有开销），
# 所以这里不需要大，只是别把事件循环占满。
PANE_PUSH_GAP = 0.5
AUTH_TOKEN = os.environ.get("HERDR_RELAY_TOKEN", "")  # Optional: shared secret for relay auth

# VAPID Web Push
VAPID_PUBLIC_KEY = os.environ.get("HERDR_VAPID_PUBLIC", "")
VAPID_PRIVATE_KEY = os.environ.get("HERDR_VAPID_PRIVATE", "")
VAPID_SUBJECT = os.environ.get("HERDR_VAPID_SUBJECT", "mailto:herdr@localhost")
push_subscriptions = []  # list of PushSubscription dicts
PUSH_SUBS_FILE = os.path.join(LOG_DIR, "push_subs.json")

# Remote hosts: comma-separated SSH targets
REMOTES = [r.strip() for r in os.environ.get("HERDR_REMOTES", "").split(",") if r.strip()]

TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]
SUBAGENT_OPTIONS = ["approve all pending", "configure individually", "exit (cancel subagents)"]
CHROME_RE = re.compile(
    r"^[\s─━═_—│|◔◑◕●\s]+$"
    r"|Kiro\s[·•]"
    r"|esc to cancel"
    r"|type to queue"
    r"|^\s*[◔◑◕●]\s+(Shell|Bash)"
)

clients = set()
BIN_GZIP_CAP = "bin-gzip-v1"
BIN_GZIP_TYPES = frozenset({"agents", "pane_content", "pane_delta", "git_diff", "git_show"})
# 服务端主动推 pane 变化 + 只发增量行。客户端声明后才启用，
# 老客户端继续走 read_pane 全量拉取那条路。
PANE_PUSH_CAP = "pane-push-v1"
client_caps = {}  # ws -> set[str]
# ws -> {"pane_id": str, "lines": int}：客户端当前打开哪个 pane。
# 只有被订阅的 pane 才在 poll 里读内容，避免为没人看的 pane 白跑 herdr。
pane_subs = {}
# (ws, pane_id) -> (lines, [行文本])：上次推给「这个客户端」的内容，用来算增量。
# 按 ws 分开存：两个客户端看同一 pane 时基线可能不同，共用一份会发错增量。
# 按 lines 存是因为 loadMore 会改窗口大小，窗口一变就得重发全量。
pane_last_sent = {}
last_statuses = {}
event_queue = asyncio.Queue()
pane_remote_map = {}
known_panes = set()
agent_cache = {}
# agent_cache 最后一次被轮询填充的墙钟毫秒。客户端用它算"内容年龄"：
# WS 心跳只能证明管子通，证明不了 relay→herdr 这一段还活着。
agent_cache_ts = 0
# workspace_id -> display label from `herdr workspace list` / rename.
# Pane list only has cwd basename as "project"; Space chips need this label.
workspace_label_cache = {}
# 窄屏模式：{目标 pane_id: 为挤窄它而分出的陪衬 pane_id}。
# 记录而非推断——关闭时只关我们自己开的那个，避免误关用户在 Mac 上手开的 pane。
narrow_companions = {}

SAFE_RESPONSES = {"y", "n", "a", "yes", "no", "trust", "yes, single permission", "trust, always allow", "no (tab to edit)", "approve all pending", "configure individually", "exit (cancel subagents)"}
# herdr pane send-keys 认的是 parse_key_combo 名（backspace / bs），不是 tmux 的 BSpace。
# 别名在送入 herdr 前归一化；白名单同时收别名与规范名，避免旧客户端被拒。
KEY_ALIASES = {
    "BSpace": "backspace",
    "Backspace": "backspace",
    "bs": "backspace",
    # 手机端常说「删除」；herdr 没有 forward-delete，映射到退格。
    "Delete": "backspace",
    "Del": "backspace",
}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "C-c", "Up", "Down", "Left", "Right",
             "backspace", "Space"} | set(KEY_ALIASES) | {
    str(number) for number in range(10)
}


def normalize_key(key: str) -> str:
    """把客户端别名归一成 herdr 能解析的键名。"""
    return KEY_ALIASES.get(key, key)


def attach_request_id(payload, msg):
    """把客户端请求 id 原样带回，方便 web 把 ack 对上一次发送。"""
    req_id = msg.get("id") if isinstance(msg, dict) else None
    if isinstance(req_id, str) and 0 < len(req_id) <= 64:
        payload["id"] = req_id
    return payload



# Cursor Agent 的跟进输入框对「paste 后立刻 Enter」特别敏感：PTY 已写入、
# TUI 还在消化 bracketed paste 时若收到 Enter，会把 prompt 提交出去，但原文
# 仍留在 → 跟进框里，下一次远程输入会拼到残留后面。Claude / Codex 无此问题。
# 实测 ≥100ms settle 可稳定清空；取 150ms 留余量。只对 cursor 生效，避免拖慢别家。
CURSOR_PASTE_SETTLE_S = 0.15
_CURSOR_AGENT_NAMES = frozenset({"cursor", "cursor-agent"})


def is_cursor_agent(agent: str | None) -> bool:
    """识别 Cursor Agent（含 cursor-agent 别名）。"""
    return (agent or "").strip().lower() in _CURSOR_AGENT_NAMES


async def settle_after_paste(agent: str | None) -> None:
    """Cursor paste 后等待 TUI 消化，避免紧随其后的 Enter 留下跟进框残留。"""
    if is_cursor_agent(agent):
        await asyncio.sleep(CURSOR_PASTE_SETTLE_S)

# --- Agent event validation ---
# agent_event / UDP / HTTP ?d= 三个入口原先零校验，而 AUTH_TOKEN 默认为空、
# relay 监听 0.0.0.0 且主动 mDNS 广播。组合起来同网段任何设备都能伪造 blocked
# 事件，进而在 Telegram / Web Push 里诱导用户点 "Trust always"。
# 这里做的是最小充分校验：类型、必需键、状态枚举、字段长度上限。
AGENT_STATUSES = {"blocked", "working", "idle", "done", "error"}
# prompt 是唯一可能较长的字段（blocked 时的提示原文），其余都是标识符量级。
# 上限取 4000：足够容纳 relay 自己截断到 BLOCKED_PROMPT_LIMIT 的 prompt，
# 又不至于撑爆推送体积。
_EVENT_FIELD_LIMIT = 4000

# blocked 推送里 prompt 的截断长度。
#
# 原来是硬编码的 500，实测会把选择器腰斩：一屏 AskUserQuestion（含每项的
# 描述行）轻松超过 500 字符，最后那行
#     Enter to select · ↑/↓ to navigate · Esc to cancel
# 被截成 `↑/↓ to nav`，客户端的 is_selector_hint 认不出，就判定「选择器
# 已经翻过去了」，整组选项丢弃，卡片回落成 Yes/Trust/No——按钮和屏幕上
# 问的对不上，点了等于替 agent 乱答。
#
# 2000 装得下实测抓屏（read_pane_async 只留 20 行），又在 _EVENT_FIELD_LIMIT
# 之内。中文一个字符顶一个额度，别按英文的直觉估。
BLOCKED_PROMPT_LIMIT = 2000
_EVENT_MAX_KEYS = 32


def validate_agent_event(event):
    """校验外部送入的 agent 事件。返回 (ok, reason)。

    宽进严出：字段缺失交由 complete_agent_update_message 兜底（它会要求
    pane_id/agent/status/cwd/project 齐全），这里只拦明显畸形与伪造。
    """
    if not isinstance(event, dict):
        return False, "event must be an object"
    if len(event) > _EVENT_MAX_KEYS:
        return False, "too many keys"

    pane_id = event.get("pane_id")
    if not isinstance(pane_id, str) or not pane_id.strip():
        return False, "pane_id must be a non-empty string"

    status = event.get("status")
    if status is not None and status not in AGENT_STATUSES:
        return False, f"unknown status: {status!r}"

    for key, value in event.items():
        if not isinstance(key, str):
            return False, "keys must be strings"
        if isinstance(value, str) and len(value) > _EVENT_FIELD_LIMIT:
            return False, f"field {key} exceeds {_EVENT_FIELD_LIMIT} chars"
    return True, ""


def token_matches(provided, expected):
    """常量时间比较，避免逐字符比较带来的时序侧信道。

    两侧都为空时视为不匹配——是否放行由调用方按 AUTH_TOKEN 是否配置决定，
    这里不替调用方做"未启用鉴权"的语义判断。
    """
    if not provided or not expected:
        return False
    return secrets.compare_digest(str(provided), str(expected))

# --- Audit logging ---
_audit_handler = RotatingFileHandler(AUDIT_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_audit_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
audit_log = logging.getLogger("herdr-audit")
audit_log.setLevel(logging.INFO)
audit_log.addHandler(_audit_handler)
audit_log.propagate = False


def audit(action: str, ip: str, device: str, pane_id: str, detail: str = ""):
    """Append a write action to the audit log as structured JSONL."""
    import datetime
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "action": action,
        "paneId": pane_id,
        "ip": ip,
        "device": device,
    }
    if detail:
        entry["detail"] = detail[:120]  # truncate like collie
    audit_log.info(json.dumps(entry, separators=(",", ":")))


# --- Web Push helpers ---
def _load_push_subs():
    global push_subscriptions
    if os.path.isfile(PUSH_SUBS_FILE):
        try:
            with open(PUSH_SUBS_FILE) as f:
                push_subscriptions = json.load(f)
        except Exception:
            push_subscriptions = []


def _save_push_subs():
    with open(PUSH_SUBS_FILE, "w") as f:
        json.dump(push_subscriptions, f)


async def send_web_push(title: str, body: str, url: str = "/", clear: bool = False):
    """Send push notification to all registered subscriptions.
    
    Uses collapse topic + TTL so offline devices get only the latest.
    If clear=True, sends a clear instruction instead of showing a notification.
    """
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush not installed, skipping push")
        return
    if clear:
        payload = json.dumps({"type": "clear", "tag": "herdr-blocked"})
    else:
        payload = json.dumps({"title": title, "body": body, "url": url})
    headers = {"Topic": "herdr-herd", "TTL": "21600"}  # 6h TTL, collapse key
    dead = []
    for i, sub in enumerate(push_subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                headers=headers,
            )
        except Exception as e:
            log.warning("Push failed for sub %d: %s", i, e)
            if "410" in str(e) or "404" in str(e):
                dead.append(i)
    if dead:
        for i in reversed(dead):
            push_subscriptions.pop(i)
        _save_push_subs()

_load_push_subs()


def run_herdr_result(*args, remote=None):
    if remote:
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, HERDR, *args]
    else:
        cmd = [HERDR, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def run_herdr(*args, remote=None):
    try:
        return run_herdr_result(*args, remote=remote).stdout.strip()
    except Exception:
        return ""


def _herdr_cmd(*args, remote=None):
    if remote:
        return ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, HERDR, *args]
    return [HERDR, *args]


async def run_herdr_async(*args, remote=None, timeout=15):
    """run_herdr 的协程版本。

    同步版用 subprocess.run 阻塞调用线程——放在 asyncio 事件循环里意味着
    一个慢 SSH（最长 15s）会卡住 relay 对所有客户端的处理，包括其它人的
    read_pane 和整个 poll_loop。多 remote 时 N 次超时还会串行累加。

    行为与同步版保持一致：任何失败都吞掉返回空串，调用方无需 try。
    """
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *_herdr_cmd(*args, remote=remote),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        # 超时必须回收子进程，否则 SSH 会留成僵尸并占住连接
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        log.warning("herdr call timed out: args=%s remote=%s", args, remote)
        return ""
    except Exception:
        return ""


async def run_herdr_rc_async(*args, remote=None, timeout=15):
    """需要退出码的场景（send_keys 要据此回 ack）。返回 (returncode, stdout)。

    与 run_herdr_async 不同，这里不吞异常——调用方需要区分"命令失败"
    和"命令成功但无输出"。
    """
    proc = await asyncio.create_subprocess_exec(
        *_herdr_cmd(*args, remote=remote),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode, stdout.decode(errors="replace").strip()


async def get_agents_from_host_async(remote=None):
    raw = await run_herdr_async("pane", "list", remote=remote)
    return _parse_pane_list(raw, remote)


def _parse_workspace_labels(raw):
    """Parse `herdr workspace list` JSON into {workspace_id: label}."""
    try:
        data = json.loads(raw or "")
        workspaces = data.get("result", {}).get("workspaces", [])
        out = {}
        for w in workspaces:
            wid = w.get("workspace_id") or ""
            if not wid:
                continue
            label = (w.get("label") or "").strip()
            if label:
                out[wid] = label
        return out
    except (json.JSONDecodeError, TypeError, AttributeError):
        return {}


def _parse_workspace_created(raw):
    """Parse `herdr workspace create` JSON → (workspace_id, label, pane_id)."""
    try:
        data = json.loads(raw or "")
        result = data.get("result") or {}
        ws = result.get("workspace") or {}
        root = result.get("root_pane") or {}
        wid = (ws.get("workspace_id") or result.get("workspace_id") or "").strip()
        label = (ws.get("label") or "").strip()
        pane_id = (root.get("pane_id") or "").strip()
        return wid, label, pane_id
    except (json.JSONDecodeError, TypeError, AttributeError):
        return "", "", ""


def _parse_git_porcelain(raw):
    """Parse `git status --porcelain=v1 -b` into branch/files/clean."""
    branch = ""
    files = []
    for line in (raw or "").splitlines():
        if line.startswith("## "):
            branch = line[3:].strip()
        elif len(line) >= 4 and line[2] == " ":
            status = line[:2].strip() or line[0]
            files.append({"status": status, "path": line[3:]})
    clean = not files
    return {"branch": branch, "files": files, "clean": clean}


def _format_git_status_text(branch, files):
    lines = []
    if branch:
        head = branch.split("...")[0]
        lines.append(f"## {branch}")
        lines.append(f"On branch {head}")
    if not files:
        lines.append("nothing to commit, working tree clean")
    else:
        for item in files:
            lines.append(f"{item['status']:>3}  {item['path']}")
    return "\n".join(lines)


def _resolve_git_target(pane_id="", workspace_id=""):
    """Resolve (cwd, remote, workspace_id) from known panes."""
    pane_id = (pane_id or "").strip()
    workspace_id = (workspace_id or "").strip()
    if pane_id:
        if pane_id not in known_panes:
            return None, None, ""
        agent = agent_cache.get(pane_id) or {}
        cwd = (agent.get("cwd") or "").strip()
        remote = pane_remote_map.get(pane_id)
        ws = agent.get("workspace_id") or workspace_id
        return cwd or None, remote, ws
    if workspace_id:
        for pid in known_panes:
            agent = agent_cache.get(pid) or {}
            if agent.get("workspace_id") != workspace_id:
                continue
            cwd = (agent.get("cwd") or "").strip()
            if cwd:
                return cwd, pane_remote_map.get(pid), workspace_id
        return None, None, workspace_id
    return None, None, ""


def _sanitize_git_path(path):
    """Return a safe repo-relative path, or None if rejected."""
    if path is None:
        return None
    p = str(path).strip().replace("\\", "/")
    if not p or p.startswith("/") or p.startswith("~"):
        return None
    if p.startswith("./"):
        p = p[2:]
    parts = [part for part in p.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


GIT_TEXT_LIMIT = 200 * 1024  # ~200KB


def _truncate_git_text(text, limit=GIT_TEXT_LIMIT):
    raw = text if isinstance(text, str) else ""
    if len(raw.encode("utf-8", errors="replace")) <= limit:
        return raw, False
    encoded = raw.encode("utf-8", errors="replace")[:limit]
    return encoded.decode("utf-8", errors="ignore"), True


_BASE_CANDIDATES = ("origin/main", "main", "master")


def _pick_base_ref(existing):
    """Pick first candidate present in `existing` (ordered preference)."""
    have = set(existing or [])
    for name in _BASE_CANDIDATES:
        if name in have:
            return name
    return None


def _parse_git_name_status(raw):
    files = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        # format: STATUS<TAB>path  (or STATUS<TAB>old<TAB>new for renames)
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip() or "?"
        path = parts[-1]
        files.append({"status": status, "path": path})
    return files


async def _run_git_async(cwd, remote, args, timeout=10):
    """Run git -C cwd … locally or via SSH. Returns (returncode, stdout, stderr)."""
    if remote:
        inner = "git -C " + shlex.quote(cwd) + " " + " ".join(shlex.quote(a) for a in args)
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, inner]
    else:
        cmd = ["git", "-C", cwd, *args]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return -1, b"", b"timeout"
    except Exception as e:
        return -1, b"", str(e).encode()
    return proc.returncode, stdout, stderr


async def detect_git_base_async(cwd, remote=None, timeout=10):
    found = []
    for cand in _BASE_CANDIDATES:
        rc, out, _ = await _run_git_async(
            cwd, remote, ["rev-parse", "--verify", cand], timeout=timeout)
        if rc == 0 and out.strip():
            found.append(cand)
    return _pick_base_ref(found)


async def fetch_git_diff_async(cwd, path, mode="worktree", base="", remote=None, timeout=10,
                               untracked=False):
    safe = _sanitize_git_path(path)
    if not safe:
        return {"ok": False, "message": "invalid path", "path": path or ""}
    if not cwd:
        return {"ok": False, "message": "cwd required", "path": safe}

    mode = (mode or "worktree").strip() or "worktree"
    resolved_base = ""
    if mode == "base":
        resolved_base = (base or "").strip() or await detect_git_base_async(
            cwd, remote=remote, timeout=timeout)
        if not resolved_base:
            return {"ok": False, "message": "could not resolve base branch", "path": safe}
        rc, out, err = await _run_git_async(
            cwd, remote, ["diff", resolved_base, "--", safe], timeout=timeout)
    else:
        mode = "worktree"
        if untracked:
            # Client already knows porcelain "??" — skip empty HEAD diff round-trip.
            rc, out, err = await _run_git_async(
                cwd, remote,
                ["diff", "--no-index", "--", "/dev/null", safe],
                timeout=timeout,
            )
        else:
            rc, out, err = await _run_git_async(
                cwd, remote, ["diff", "HEAD", "--", safe], timeout=timeout)
            if rc == 0 and not out.strip():
                # Likely untracked: synthesize add diff via --no-index (exit 1 is normal)
                rc2, out2, err2 = await _run_git_async(
                    cwd, remote,
                    ["diff", "--no-index", "--", "/dev/null", safe],
                    timeout=timeout,
                )
                if out2.strip():
                    rc, out, err = rc2, out2, err2
                elif rc2 == -1:
                    return {"ok": False, "message": err2.decode(errors="replace") or "diff failed",
                            "path": safe}

    if rc not in (0, 1) and not out.strip():
        # git diff returns 1 when --no-index finds differences; treat stdout as success
        msg = err.decode(errors="replace").strip() or "git diff failed"
        return {"ok": False, "message": msg, "path": safe}

    text = out.decode(errors="replace")
    # Match git's own binary notice line only — not the substring inside source diffs.
    if any(
        line.startswith("Binary files ") and line.endswith(" differ")
        for line in text.splitlines()
    ):
        return {"ok": False, "message": "binary file", "path": safe}

    text, truncated = _truncate_git_text(text)
    return {
        "ok": True,
        "path": safe,
        "mode": mode,
        "base": (base or "").strip(),
        "resolved_base": resolved_base if mode == "base" else "",
        "text": text or "(no diff)",
        "truncated": truncated,
        "cwd": cwd,
    }


async def fetch_git_show_async(cwd, path, remote=None, timeout=10):
    safe = _sanitize_git_path(path)
    if not safe:
        return {"ok": False, "message": "invalid path", "path": path or ""}
    if not cwd:
        return {"ok": False, "message": "cwd required", "path": safe}

    if remote:
        inner = (
            "head -c " + str(GIT_TEXT_LIMIT + 1) + " "
            + shlex.quote(f"{cwd.rstrip('/')}/{safe}")
        )
        cmd = ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", remote, inner]
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return {"ok": False, "message": "read timed out", "path": safe}
        except Exception as e:
            return {"ok": False, "message": f"read failed: {e}", "path": safe}
        if proc.returncode not in (0, None) and not stdout:
            return {"ok": False, "message": stderr.decode(errors="replace").strip() or "read failed",
                    "path": safe}
        data = stdout
    else:
        full = Path(cwd) / safe
        try:
            full = full.resolve()
            root = Path(cwd).resolve()
            if root not in full.parents and full != root:
                return {"ok": False, "message": "path escapes cwd", "path": safe}
            data = full.read_bytes()
        except FileNotFoundError:
            return {"ok": False, "message": "file not found", "path": safe}
        except Exception as e:
            return {"ok": False, "message": f"read failed: {e}", "path": safe}

    if b"\x00" in data[:8192]:
        return {"ok": False, "message": "binary file", "path": safe}

    text = data.decode("utf-8", errors="replace")
    text, truncated = _truncate_git_text(text)
    return {
        "ok": True,
        "path": safe,
        "text": text,
        "truncated": truncated,
        "cwd": cwd,
    }


async def fetch_git_status_async(cwd, remote=None, timeout=10, mode="worktree", base=""):
    """Run read-only git status (worktree porcelain or vs-base name-status)."""
    if not cwd:
        return {"ok": False, "message": "cwd required"}

    mode = (mode or "worktree").strip() or "worktree"
    if mode == "base":
        resolved_base = (base or "").strip() or await detect_git_base_async(
            cwd, remote=remote, timeout=timeout)
        if not resolved_base:
            return {"ok": False, "message": "could not resolve base branch"}
        rc, stdout, stderr = await _run_git_async(
            cwd, remote, ["diff", "--name-status", resolved_base], timeout=timeout)
        if rc == -1:
            err = stderr.decode(errors="replace").strip()
            if err == "timeout":
                return {"ok": False, "message": "git status timed out"}
            return {"ok": False, "message": f"git status failed: {err}" if err else "git status failed"}
        if rc != 0:
            err = stderr.decode(errors="replace").strip()
            return {"ok": False, "message": err or "git diff --name-status failed"}
        files = _parse_git_name_status(stdout.decode(errors="replace"))
        clean = not files
        if clean:
            text = f"## vs {resolved_base}\nnothing to commit, working tree clean"
        else:
            lines = [f"## vs {resolved_base}"]
            for item in files:
                lines.append(f"{item['status']:>3}  {item['path']}")
            text = "\n".join(lines)
        return {
            "ok": True,
            "clean": clean,
            "branch": "",
            "files": files,
            "text": text,
            "cwd": cwd,
            "mode": "base",
            "base": (base or "").strip(),
            "resolved_base": resolved_base,
        }

    rc, stdout, stderr = await _run_git_async(
        cwd, remote, ["status", "--porcelain=v1", "-b"], timeout=timeout)
    if rc == -1:
        err = stderr.decode(errors="replace").strip()
        if err == "timeout":
            return {"ok": False, "message": "git status timed out"}
        return {"ok": False, "message": f"git status failed: {err}" if err else "git status failed"}
    if rc != 0:
        err = stderr.decode(errors="replace").strip()
        return {"ok": False, "message": err or "not a git repository"}
    parsed = _parse_git_porcelain(stdout.decode(errors="replace"))
    text = _format_git_status_text(parsed["branch"], parsed["files"])
    return {
        "ok": True,
        "clean": parsed["clean"],
        "branch": parsed["branch"],
        "files": parsed["files"],
        "text": text,
        "cwd": cwd,
    }


def _apply_workspace_labels(agents, labels):
    for a in agents:
        wid = a.get("workspace_id") or ""
        a["workspace_label"] = labels.get(wid, "") if wid else ""
    return agents


def _set_workspace_label(workspace_id, label):
    """Update cache + in-memory agents after a successful rename."""
    workspace_label_cache[workspace_id] = label
    for a in agent_cache.values():
        if a.get("workspace_id") == workspace_id:
            a["workspace_label"] = label


async def get_all_agents_async():
    """本机与所有 remote 并发查询，总耗时取最慢的一个而非累加。"""
    hosts = [None, *REMOTES]
    results = await asyncio.gather(
        *(get_agents_from_host_async(remote=r) for r in hosts),
        *(run_herdr_async("workspace", "list", remote=r) for r in hosts),
    )
    n = len(hosts)
    agents = []
    for chunk in results[:n]:
        agents.extend(chunk)
    labels = {}
    for raw in results[n:]:
        labels.update(_parse_workspace_labels(raw))
    if labels:
        workspace_label_cache.update(labels)
    return _apply_workspace_labels(agents, workspace_label_cache)


# 视口不在底部时抓到的内容不是最新的，给它挂个标注。
# 症状是群里的消息末尾拼着终端的 `Jump to bottom (click) ↓` 提示符——
# 那是「底部还有内容没抓到」的信号。只把提示符滤掉是错的：内容照样缺，
# 而且再也看不出哪条不可信，所以这里明确标注出来。
STALE_VIEWPORT_MARK = "⚠ 终端当时处于回滚状态，以下内容可能不是最新的"


async def pane_scroll_offset(pane_id, remote=None) -> int:
    """视口离底部多少行。0 = 在底部（内容是最新的）。

    herdr 在 `pane get` 里一直返回 scroll.offset_from_bottom，用它判断比
    正则匹配 UI 提示文案可靠——文案会随版本改，这个字段不会。

    拿不到就返回 0（按「在底部」处理）：抓屏是主路径，不能因为多这一次
    查询就失败，更不能凭空给正常内容挂上警告。
    """
    try:
        raw = await run_herdr_async("pane", "get", pane_id, remote=remote)
        pane = json.loads(raw).get("result", {}).get("pane", {})
        return int(pane.get("scroll", {}).get("offset_from_bottom") or 0)
    except Exception:
        return 0


async def read_pane_async(pane_id, remote=None):
    raw = await run_herdr_async("pane", "read", pane_id, "--lines", "50",
                                "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    content = "\n".join(lines[-20:])
    # 标注放在最前面：手机上往往只看得到开头几行。
    if await pane_scroll_offset(pane_id, remote=remote) > 0:
        return f"{STALE_VIEWPORT_MARK}\n{content}" if content else STALE_VIEWPORT_MARK
    return content


# 布局操作被 herdr 拒绝时的原因 → 面向用户的中文说明。
# 这些都不是错误，而是"操作在当前布局下无意义"，UI 该给出解释而不是报错。
LAYOUT_REASONS = {
    "single_pane": "该 tab 只有一个 pane，无需缩放",
    "already_zoomed": "该 pane 已处于缩放状态",
    "already_unzoomed": "该 pane 未处于缩放状态",
    "unchanged": "布局未发生变化",
}


def parse_layout_panes(raw):
    """解析 `herdr pane layout` 的输出，返回 (pane_id, width) 列表。

    窄屏模式要据此判断：当前是否已分屏、关闭时该关掉哪个 pane。
    单 pane 时返回长度为 1 的列表，splits 为空。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    layout = (data.get("result") or {}).get("layout")
    if not isinstance(layout, dict):
        return []
    out = []
    for p in layout.get("panes") or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("pane_id")
        rect = p.get("rect") or {}
        if isinstance(pid, str):
            out.append((pid, rect.get("width", 0)))
    return out


def parse_split_result(raw):
    """解析 `herdr pane split` 的 JSON 输出，返回新建的 pane_id（失败返回 ""）。

    split 的返回结构与 zoom/resize 不同：它回 {"result": {"pane": {...},
    "type": "pane_info"}}，没有 layout / changed 字段，成功的标志就是拿到
    新 pane_id，所以单独解析而不复用 parse_layout_result。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    result = data.get("result")
    if not isinstance(result, dict):
        return ""
    pane = result.get("pane")
    if not isinstance(pane, dict):
        return ""
    pane_id = pane.get("pane_id")
    return pane_id if isinstance(pane_id, str) else ""


def parse_layout_result(raw, key):
    """解析 `herdr pane zoom/split` 的 JSON 输出。

    herdr 在"操作未生效"时同样返回退出码 0（实测：单 pane 上 zoom --on 得到
    changed:false / reason:single_pane），因此不能只看 returncode——那会让 UI
    报出假的成功。这里统一取 changed 与 reason，由调用方据此回 ack。

    返回 (ok, info)：ok 表示 JSON 结构可解析；info 含 changed/reason/layout。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False, {}
    result = data.get("result")
    if not isinstance(result, dict):
        return False, {}
    payload = result.get(key)
    if not isinstance(payload, dict):
        return False, {}
    layout = payload.get("layout") or {}
    return True, {
        "changed": bool(payload.get("changed", False)),
        "reason": payload.get("reason"),
        "zoomed": payload.get("zoomed"),
        "pane_id": payload.get("pane_id", ""),
        "focused_pane_id": payload.get("focused_pane_id", ""),
        # panes/splits 让客户端知道当前分屏结构；单 pane 时 splits 为空数组。
        "pane_count": len(layout.get("panes") or []),
        "split_count": len(layout.get("splits") or []),
    }

def _parse_pane_list(raw, remote):
    """解析 `herdr pane list` 的 JSON 输出。同步与异步版共用，避免两处漂移。"""
    host_label = remote or "local"
    try:
        data = json.loads(raw)
        panes = data.get("result", {}).get("panes", [])
        return [
            {
                "pane_id": p["pane_id"],
                # 空 shell pane（新建 Space 后尚未启动 agent）也要进列表，否则手机端看不到、用不了。
                "agent": p.get("agent") or "shell",
                "label": p.get("label", ""),
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": os.path.basename(p.get("cwd", "")),
                "host": host_label,
                "remote": remote,
                "workspace_id": p.get("workspace_id", ""),
                "workspace_label": "",
                "tab_id": p.get("tab_id", ""),
            }
            for p in panes if p.get("pane_id")
        ]
    except (json.JSONDecodeError, KeyError):
        return []


def get_agents_from_host(remote=None):
    return _apply_workspace_labels(
        _parse_pane_list(run_herdr("pane", "list", remote=remote), remote),
        workspace_label_cache,
    )


def get_all_agents():
    agents = get_agents_from_host(remote=None)
    for remote in REMOTES:
        agents.extend(get_agents_from_host(remote=remote))
    # Best-effort refresh of labels for sync callers (tests / rare paths).
    labels = _parse_workspace_labels(run_herdr("workspace", "list"))
    for remote in REMOTES:
        labels.update(_parse_workspace_labels(run_herdr("workspace", "list", remote=remote)))
    if labels:
        workspace_label_cache.update(labels)
        _apply_workspace_labels(agents, workspace_label_cache)
    return agents


def read_pane(pane_id, remote=None):
    raw = run_herdr("pane", "read", pane_id, "--lines", "50", "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    return "\n".join(lines[-20:])


def detect_options(text):
    lower = text.lower()
    if "yes, single permission" in lower:
        return TOOL_OPTIONS
    if "approve all pending" in lower:
        return SUBAGENT_OPTIONS
    return None


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


def build_pane_delta(prev_state, pane_id, lines, content):
    """把整屏内容压成增量消息。返回 (msg, new_state)。

    终端画面有两种典型变化，都要覆盖：

    1. 屏没满，尾部追加 → 公共前缀不变，发 keep + tail 即可。
    2. 屏已满，每来一行新输出顶部就滚掉一行 → 首行就变了，纯前缀比对
       会误判成「整屏皆变」而退回全量。这是最常见的形态，所以额外找一次
       滚动偏移：若 prev 去掉前 k 行等于 cur 的前若干行，就只发尾部，
       并带上 drop=k 告诉客户端「先扔掉自己头上 k 行」。

    lines 变了（loadMore）直接全量，省得处理窗口错位。
    """
    cur = content.split("\n")
    prev_lines, prev = prev_state if prev_state else (None, None)
    new_state = (lines, cur)

    if prev is None or prev_lines != lines:
        return None, new_state  # 无基线或窗口变了 → 调用方发全量

    if prev == cur:
        return {"type": "pane_delta", "pane_id": pane_id, "unchanged": True}, new_state

    def _msg(drop, keep, tail):
        m = {"type": "pane_delta", "pane_id": pane_id,
             "keep": keep, "tail": tail, "total": len(cur)}
        if drop:
            m["drop"] = drop
        return m

    # 情况 1：公共前缀（屏未满的纯追加，或末行原地改写）
    keep = 0
    for a, b in zip(prev, cur):
        if a != b:
            break
        keep += 1
    if keep > 0:
        return _msg(0, keep, cur[keep:]), new_state

    # 情况 2：头部滚动。找最小的 drop，使 prev[drop:] 是 cur 的前缀。
    # 限制搜索范围：滚动超过半屏就不如直接发全量了。
    limit = min(len(prev), max(1, len(cur) // 2))
    for drop in range(1, limit + 1):
        kept = prev[drop:]
        if not kept:
            break
        if cur[:len(kept)] == kept:
            tail = cur[len(kept):]
            # 只有真的省了才用增量，否则全量更省事。
            if len(tail) + 1 < len(cur):
                return _msg(drop, len(kept), tail), new_state
            break

    # 整屏都变了（clear / 切 TUI 视图），增量不划算，退回全量。
    return None, new_state


async def send_pane_update(ws, pane_id, lines, content, *, force_full=False):
    """给单个客户端发 pane 内容，能增量就增量。"""
    caps = client_caps.get(ws, set())
    ts = int(time.time() * 1000)
    if PANE_PUSH_CAP in caps:
        key = (ws, pane_id)
        prev_state = None if force_full else pane_last_sent.get(key)
        msg, new_state = build_pane_delta(prev_state, pane_id, lines, content)
        pane_last_sent[key] = new_state
        if msg is not None:
            msg["ts"] = ts
            await send_to_client(ws, msg)
            return
    await send_to_client(ws, {
        "type": "pane_content", "pane_id": pane_id, "content": content, "ts": ts,
    })


async def send_to_client(ws, msg: dict):
    data = encode_for_client(msg, client_caps.get(ws, set()))
    await ws.send(data)


def forget_client(ws):
    """连接没了就把它的所有 per-client 状态一起丢掉。

    pane_last_sent 是按 (ws, pane_id) 存的，不清理会随重连次数无限涨。
    """
    client_caps.pop(ws, None)
    pane_subs.pop(ws, None)
    for key in [k for k in pane_last_sent if k[0] is ws]:
        pane_last_sent.pop(key, None)


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
        forget_client(ws)


async def push_subscribed_panes():
    """把被订阅 pane 的变化主动推给客户端。

    这是替掉前端 setInterval(refreshPane) 的那一半：以前每个客户端
    每 3 秒（slash 模式 0.4 秒）拉一次全屏，跨国链路上光 RTT 就吃掉大半；
    现在由 relay 侧读一次、比对、只在有变化时发增量。

    同一个 pane 被多个客户端订阅时只读一次 herdr，读的结果各自算增量。
    """
    if not pane_subs:
        return
    # 先按 (pane_id, lines) 归组，避免同一 pane 读多遍。
    wanted = {}
    for ws, sub in list(pane_subs.items()):
        if ws not in clients:
            pane_subs.pop(ws, None)
            continue
        wanted.setdefault((sub["pane_id"], sub["lines"]), []).append(ws)

    for (pane_id, lines), subscribers in wanted.items():
        if pane_id not in known_panes:
            continue
        remote = pane_remote_map.get(pane_id)
        try:
            content = await run_herdr_async(
                "pane", "read", pane_id, "--lines", str(lines),
                "--source", "recent", remote=remote,
            )
        except Exception:
            log.debug("pane push read failed for %s", pane_id, exc_info=True)
            continue
        for ws in subscribers:
            try:
                await send_pane_update(ws, pane_id, lines, content)
            except (ConnectionClosedError, ConnectionClosedOK):
                pane_subs.pop(ws, None)
            except Exception:
                log.debug("pane push send failed", exc_info=True)


async def pane_push_loop():
    """独立于 agent 轮询的 pane 推送循环。

    刻意不放在 poll_loop 里：实测 `herdr pane read` 单次要 ~7s（纯等待，
    不是计算），而 _poll_once 只要 ~0.04s。串在一起会把 agent 列表的刷新
    也拖到 7s 一轮，比改造前更糟。分开跑，两者各自按自己的节奏。

    没人订阅时空转，代价只是一次字典判空。
    """
    while True:
        if pane_subs:
            try:
                await push_subscribed_panes()
            except Exception:
                log.exception("pane push cycle failed; retrying")
            # read 本身就是主要耗时，这里只留一个很短的让路间隔。
            await asyncio.sleep(PANE_PUSH_GAP)
        else:
            await asyncio.sleep(POLL_INTERVAL)


async def poll_loop():
    while True:
        try:
            await _poll_once()
        except Exception:
            log.exception("poll cycle failed; retrying")
        await asyncio.sleep(POLL_INTERVAL)


async def announce_blocked(pane_id, *, agent, project, host, remote):
    """广播 blocked 并发 Web Push。轮询与事件两条路径共用，
    避免像此前那样只有轮询路径发推送、事件路径静默。

    调用方负责去重（比对 last_statuses），本函数只管发。
    """
    content = await read_pane_async(pane_id, remote=remote)
    options = detect_options(content)
    await broadcast({
        "type": "blocked", "pane_id": pane_id,
        "agent": agent, "project": project, "host": host,
        "prompt": content[:BLOCKED_PROMPT_LIMIT],
        "options": options or TOOL_OPTIONS,
    })
    await send_web_push(
        title=f"🐑 {project} blocked",
        body=content[:120],
        url=f"/?pane={pane_id}",
    )


async def _poll_once():
        global agent_cache_ts
        agents = await get_all_agents_async()
        # Always broadcast (even empty list) so clients stay in sync
        for a in agents:
            pane_remote_map[a["pane_id"]] = a.get("remote")
            known_panes.add(a["pane_id"])
            agent_cache[a["pane_id"]] = a
        # 采集完成的时刻，而不是发送时刻：客户端要的是数据本身有多老。
        agent_cache_ts = int(time.time() * 1000)
        await broadcast({"type": "agents", "agents": agents, "ts": agent_cache_ts})
        for a in agents:
            pid, status = a["pane_id"], a["status"]
            if status == "blocked" and last_statuses.get(pid) != "blocked":
                await announce_blocked(
                    pid, agent=a["agent"], project=a["project"],
                    host=a.get("host", "local"), remote=a.get("remote"),
                )
            # Send clear push when agent unblocks
            if status != "blocked" and last_statuses.get(pid) == "blocked":
                await send_web_push("", "", clear=True)
            last_statuses[pid] = status
        # Clean up panes that are no longer reported
        current_pane_ids = {a["pane_id"] for a in agents}
        stale = known_panes - current_pane_ids
        if stale:
            known_panes.difference_update(stale)
            for pid in stale:
                pane_remote_map.pop(pid, None)
                last_statuses.pop(pid, None)
                agent_cache.pop(pid, None)


async def event_push():
    global agent_cache_ts
    while True:
        event = await event_queue.get()
        pane_id = event.get("pane_id", "")
        update = None
        if pane_id and event.get("type") == "agent_event":
            update = complete_agent_update_message(
                event,
                current=agent_cache.get(pane_id),
                local_hostname=socket.gethostname(),
            )
            if update is None:
                continue
        agent_data = update["agent"] if update else event
        status = agent_data.get("status", "")
        host = agent_data.get("host", "local")

        # 与轮询路径一致地做状态跳变判断：此前事件路径不看 last_statuses，
        # 同一 pane 反复推 blocked 会重复广播；也不更新 last_statuses，
        # 导致轮询随后又会把同一次 blocked 再报一遍。
        if pane_id:
            was_blocked = last_statuses.get(pane_id) == "blocked"

            if status == "blocked" and not was_blocked:
                remote = pane_remote_map.get(pane_id)
                if remote or host == "local":
                    await announce_blocked(
                        pane_id,
                        agent=agent_data.get("agent", ""),
                        project=agent_data.get("project", ""),
                        host=host, remote=remote,
                    )
                else:
                    # 远端 pane 尚未建立 remote 映射时读不到内容，
                    # 退回事件自带的 prompt，但推送照发——否则 hook 的
                    # 提速对远端主机完全失效。
                    content = event.get("prompt", "Agent is blocked")
                    options = detect_options(content)
                    await broadcast({
                        "type": "blocked", "pane_id": pane_id,
                        "agent": agent_data.get("agent", ""),
                        "project": agent_data.get("project", ""),
                        "host": host,
                        "prompt": content[:BLOCKED_PROMPT_LIMIT],
                        "options": options or TOOL_OPTIONS,
                    })
                    await send_web_push(
                        title=f"🐑 {agent_data.get('project', '')} blocked",
                        body=content[:120],
                        url=f"/?pane={pane_id}",
                    )

            if status and status != "blocked" and was_blocked:
                await send_web_push("", "", clear=True)

            if status:
                last_statuses[pane_id] = status

        if update:
            known_panes.add(pane_id)
            pane_remote_map.setdefault(pane_id, None)
            agent_cache[pane_id] = {**agent_cache.get(pane_id, {}), **update["agent"]}
            # 事件路径的数据比轮询更新鲜，同样刷新新鲜度时钟，
            # 否则 hook 一直在推、UI 却因为轮询卡住而报"内容陈旧"。
            agent_cache_ts = int(time.time() * 1000)
            await broadcast({**update, "ts": agent_cache_ts})


async def process_request(connection, request):
    """Handle HTTP POST on the same port as WebSocket."""
    from websockets.http11 import Response
    from websockets.datastructures import Headers

    # Token auth (if configured)
    if AUTH_TOKEN:
        token = None
        for key, value in request.headers.raw_items():
            if key.lower() == "authorization":
                token = value.replace("Bearer ", "")
        # Also check query param ?token=
        if not token and "token=" in (request.path or ""):
            import urllib.parse
            _, qs = request.path.split("?", 1) if "?" in request.path else (request.path, "")
            params = urllib.parse.parse_qs(qs)
            token = params.get("token", [None])[0]
        if not token_matches(token, AUTH_TOKEN):
            headers = Headers([("Content-Type", "text/plain")])
            return Response(401, "Unauthorized", headers, b"Invalid token\n")

    # Check if this is a WebSocket upgrade
    upgrade = None
    for key, value in request.headers.raw_items():
        if key.lower() == "upgrade":
            upgrade = value.lower()
    if upgrade == "websocket":
        return None  # proceed with WebSocket handshake

    # For CORS preflight
    if request.path and "OPTIONS" in str(request.headers):
        headers = Headers([
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
        ])
        return Response(204, "No Content", headers, b"")

    # Serve web app for GET / or GET /index.html
    path = (request.path or "/").split("?")[0]
    if path in ("/", "/index.html"):
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        index_path = os.path.join(web_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-cache"),
            ])
            return Response(200, "OK", headers, body)

    # Serve service worker
    if path == "/sw.js":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        sw_path = os.path.join(web_dir, "sw.js")
        if os.path.isfile(sw_path):
            with open(sw_path, "rb") as f:
                body = f.read()
            headers = Headers([
                ("Content-Type", "application/javascript"),
                ("Cache-Control", "no-cache"),
                ("Service-Worker-Allowed", "/"),
            ])
            return Response(200, "OK", headers, body)

    # Serve VAPID public key
    if path == "/api/vapid-public-key":
        body = json.dumps({"publicKey": VAPID_PUBLIC_KEY}).encode()
        headers = Headers([
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
        ])
        return Response(200, "OK", headers, body)

    # Serve logo.svg
    if path == "/logo.svg":
        web_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        svg_path = os.path.join(web_dir, "logo.svg")
        if os.path.isfile(svg_path):
            with open(svg_path, "rb") as f:
                body = f.read()
            headers = Headers([("Content-Type", "image/svg+xml")])
            return Response(200, "OK", headers, body)

    # HTTP POST — parse event from URL query params as fallback
    import urllib.parse
    if "?" in (request.path or ""):
        _, qs = request.path.split("?", 1)
        params = urllib.parse.parse_qs(qs)
        if "d" in params:
            try:
                event = json.loads(urllib.parse.unquote(params["d"][0]))
            except Exception:
                event = None
            if event is not None:
                ok, reason = validate_agent_event(event)
                if ok:
                    event_queue.put_nowait(event)
                else:
                    log.warning("Rejected HTTP event: %s", reason)

    headers = Headers([("Access-Control-Allow-Origin", "*")])
    return Response(200, "OK", headers, b"ok\n")


async def handle_client(ws):
    remote_addr = ws.remote_address
    ip = remote_addr[0] if remote_addr else "unknown"
    ua = ws.request.headers.get("User-Agent", "unknown") if ws.request else "unknown"
    origin = ws.request.headers.get("Origin", "") if ws.request else ""

    device = "unknown"
    ua_lower = ua.lower()
    if "iphone" in ua_lower or "ipad" in ua_lower:
        device = "iOS"
    elif "android" in ua_lower:
        device = "Android"
    elif "macintosh" in ua_lower or "mac os" in ua_lower:
        device = "macOS"
    elif "windows" in ua_lower:
        device = "Windows"
    elif "linux" in ua_lower:
        device = "Linux"
    elif "telegram" in ua_lower or "bot" in ua_lower:
        device = "bot"
    elif "python" in ua_lower:
        device = "script"

    log.info("Client connected: ip=%s device=%s origin=%s", ip, device, origin or "-")
    clients.add(ws)
    client_caps[ws] = set()
    connected_at = time.monotonic()

    # 立即推缓存快照（先于消息循环），避免客户端干等轮询。
    # 此时 hello 通常尚未到达，首包多半是明文 JSON；后续白名单消息在
    # hello(bin-gzip-v1) 之后走 HGZ1。传输层 permessage-deflate 仍覆盖首包。
    try:
        # ts 用缓存的采集时刻，不用当下：刚连上就显示"0s 前"是假的，
        # 这份快照可能已经躺了将近一个 POLL_INTERVAL。
        await send_to_client(ws, {
            "type": "agents",
            "agents": list(agent_cache.values()),
            "ts": agent_cache_ts or int(time.time() * 1000),
        })
    except Exception as e:
        log.debug("Initial snapshot not delivered to %s: %s", ip, e)

    try:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)) and not (len(raw) and raw[:1] in (b"{", b"[")):
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
            msg_type = msg.get("type")
            if msg_type == "hello":
                caps = msg.get("caps") or []
                if isinstance(caps, list):
                    client_caps[ws] = {str(c) for c in caps if isinstance(c, str)}
                else:
                    client_caps[ws] = set()
                # 回一份「双方都认」的能力集。客户端据此决定是否关掉定时轮询——
                # 光看自己声明过不够，得确认这版 relay 真的会推。
                agreed = sorted(client_caps[ws] & {BIN_GZIP_CAP, PANE_PUSH_CAP})
                await send_to_client(ws, {"type": "hello_ack", "caps": agreed})
                continue
            elif msg_type == "respond":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await send_to_client(ws, {"type": "error", "message": "unknown pane_id"})
                    continue
                text = msg.get("text", "")
                if text.strip().lower() not in SAFE_RESPONSES:
                    await send_to_client(ws, {"type": "error", "message": "response not in allowlist"})
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                await run_herdr_async("pane", "send-text", pane_id, text + "\n", remote=remote)
            elif msg_type == "agent_event":
                ok, reason = validate_agent_event(msg)
                if not ok:
                    await send_to_client(ws, {"type": "error", "message": f"invalid agent_event: {reason}"})
                else:
                    event_queue.put_nowait(msg)
            elif msg_type == "ping":
                # Lightweight RTT probe — echo client timestamp, never audit.
                t = msg.get("t", 0)
                try:
                    t = int(t)
                except (TypeError, ValueError):
                    t = 0
                await send_to_client(ws, {"type": "pong", "t": t})
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await send_to_client(ws, {"type": "error", "message": "unknown pane_id"})
                    continue
                try:
                    lines = max(1, min(int(msg.get("lines", 30)), 5000))
                except (TypeError, ValueError):
                    lines = 30
                remote = pane_remote_map.get(pane_id)
                content = await run_herdr_async("pane", "read", pane_id, "--lines", str(lines), "--source", "recent", remote=remote)
                # 登记订阅：之后 poll_loop 发现这个 pane 有变化就主动推，
                # 客户端不必再定时全量拉。显式 read_pane 一律发全量，
                # 因为客户端可能是刚打开视图、手里没有基线。
                if PANE_PUSH_CAP in client_caps.get(ws, set()):
                    pane_subs[ws] = {"pane_id": pane_id, "lines": lines}
                await send_pane_update(ws, pane_id, lines, content, force_full=True)
            elif msg_type == "unwatch_pane":
                # 客户端关掉终端视图。不退订的话 relay 会一直为没人看的
                # pane 跑 herdr read，白烧 CPU 和 SSH 往返。
                pane_subs.pop(ws, None)
                for key in [k for k in pane_last_sent if k[0] is ws]:
                    pane_last_sent.pop(key, None)
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "unknown pane_id"}, msg))
                    continue
                raw_keys = msg.get("keys", [])
                if not all(isinstance(k, str) and k in SAFE_KEYS for k in raw_keys):
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "keys contain disallowed values"}, msg))
                    continue
                keys = [normalize_key(k) for k in raw_keys]
                remote = pane_remote_map.get(pane_id)
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                try:
                    returncode, _ = await run_herdr_rc_async("pane", "send-keys", pane_id, *keys, remote=remote)
                except Exception as e:
                    log.warning("send_keys command failed for pane %s: %s", pane_id, e)
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "send_keys command failed"}, msg))
                    continue
                if returncode != 0:
                    log.warning("send_keys command failed for pane %s with exit %s", pane_id, returncode)
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "send_keys command failed"}, msg))
                    continue
                await send_to_client(ws, attach_request_id({"type": "command_result", "command": "send_keys", "ok": True}, msg))
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "unknown pane_id"}, msg))
                    continue
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "text empty or too long"}, msg))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                try:
                    returncode, _ = await run_herdr_rc_async("pane", "send-text", pane_id, text, remote=remote)
                except Exception as e:
                    log.warning("send_text command failed for pane %s: %s", pane_id, e)
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "send_text command failed"}, msg))
                    continue
                if returncode != 0:
                    log.warning("send_text command failed for pane %s with exit %s", pane_id, returncode)
                    await send_to_client(ws, attach_request_id({"type": "error", "message": "send_text command failed"}, msg))
                    continue
                # Web 等 send_text ack 后再发 Enter；对 Cursor 必须在 paste 与
                # ack 之间留出 settle，否则跟进框会残留原文。
                agent_name = (agent_cache.get(pane_id) or {}).get("agent")
                await settle_after_paste(agent_name)
                await send_to_client(ws, attach_request_id({"type": "command_result", "command": "send_text", "ok": True}, msg))
            elif msg_type == "pane_zoom":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await send_to_client(ws, {"type": "error", "message": "unknown pane_id"})
                    continue
                # mode 直接进命令行，必须白名单——herdr 的取值只有这三个。
                mode = msg.get("mode", "toggle")
                if mode not in ("toggle", "on", "off"):
                    await send_to_client(ws, {"type": "error", "message": "invalid zoom mode"})
                    continue
                remote = pane_remote_map.get(pane_id)
                audit("pane_zoom", ip, device, pane_id, f"mode={mode}")
                try:
                    returncode, out = await run_herdr_rc_async(
                        "pane", "zoom", "--pane", pane_id, f"--{mode}", remote=remote)
                except Exception as e:
                    log.warning("pane_zoom failed for pane %s: %s", pane_id, e)
                    await send_to_client(ws, {"type": "error", "message": "pane_zoom command failed"})
                    continue
                if returncode != 0:
                    log.warning("pane_zoom failed for pane %s with exit %s", pane_id, returncode)
                    await send_to_client(ws, {"type": "error", "message": "pane_zoom command failed"})
                    continue
                ok, info = parse_layout_result(out, "zoom")
                if not ok:
                    await send_to_client(ws, {"type": "error", "message": "pane_zoom returned unparsable output"})
                    continue
                await send_to_client(ws, {
                    "type": "command_result", "command": "pane_zoom",
                    # 退出码 0 但 changed=false 是常态（如单 pane），ok 必须反映 changed
                    "ok": info["changed"], "pane_id": pane_id,
                    "zoomed": info["zoomed"], "pane_count": info["pane_count"],
                    "note": LAYOUT_REASONS.get(info["reason"], ""),
                })
            elif msg_type == "narrow_mode":
                # 窄屏模式：herdr 没有"设置终端列宽"的接口（89 个 socket API
                # 方法里都没有），但 pane 变窄时 agent 的 TUI 会响应 SIGWINCH
                # 重排。实测 133 列分屏后变 67 列，读回内容从 132 列降到 64 列。
                # 所以这里靠"向右分出一个陪衬 pane"把目标 pane 挤窄。
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await send_to_client(ws, {"type": "error", "message": "unknown pane_id"})
                    continue
                enable = bool(msg.get("enable", True))
                remote = pane_remote_map.get(pane_id)
                log.info("Narrow mode from %s (%s): pane=%s enable=%s", ip, device, pane_id, enable)
                audit("narrow_mode", ip, device, pane_id, f"enable={enable}")

                if enable:
                    if pane_id in narrow_companions:
                        await send_to_client(ws, {
                            "type": "command_result", "command": "narrow_mode",
                            "ok": False, "pane_id": pane_id, "narrow": True,
                            "note": "该 pane 已处于窄屏模式",
                        })
                        continue
                    # 若当前 pane 已经 ≤80 列，再 split 会挤成「左边一条」，拒绝继续挤窄。
                    try:
                        lrc, lout = await run_herdr_rc_async(
                            "pane", "layout", "--pane", pane_id, remote=remote)
                    except Exception as e:
                        log.warning("narrow_mode layout probe failed for pane %s: %s", pane_id, e)
                        lrc, lout = 1, ""
                    if lrc == 0:
                        width = 0
                        for pid, w in parse_layout_panes(lout):
                            if pid == pane_id:
                                try:
                                    width = int(w or 0)
                                except (TypeError, ValueError):
                                    width = 0
                                break
                        if width and width <= 80:
                            # 虚拟窄屏：无需陪衬 pane，关闭时只清标记
                            narrow_companions[pane_id] = ""
                            await send_to_client(ws, {
                                "type": "command_result", "command": "narrow_mode",
                                "ok": True, "pane_id": pane_id, "narrow": True,
                                "note": f"当前已 {width} 列，未再分屏",
                            })
                            continue
                    try:
                        # --no-focus：挤窄是为了手机端可读，不该把 Mac 上的
                        # 焦点抢到那个空 shell 上去。
                        returncode, out = await run_herdr_rc_async(
                            "pane", "split", "--pane", pane_id,
                            "--direction", "right", "--no-focus", remote=remote)
                    except Exception as e:
                        log.warning("narrow_mode split failed for pane %s: %s", pane_id, e)
                        await send_to_client(ws, {"type": "error", "message": "narrow_mode command failed"})
                        continue
                    if returncode != 0:
                        log.warning("narrow_mode split failed for pane %s with exit %s", pane_id, returncode)
                        await send_to_client(ws, {"type": "error", "message": "narrow_mode command failed"})
                        continue
                    # split 与 zoom 返回结构不同：它回 {"pane": {...},
                    # "type": "pane_info"}，没有 layout/changed，成功标志是拿到新 pane_id。
                    companion = parse_split_result(out)
                    if not companion:
                        await send_to_client(ws, {"type": "error", "message": "narrow_mode returned unparsable output"})
                        continue
                    # 记住陪衬 pane 是谁：关闭时只关我们自己开的那个，
                    # 绝不去猜，否则可能关掉用户自己在 Mac 上开的 pane。
                    narrow_companions[pane_id] = companion
                    await send_to_client(ws, {
                        "type": "command_result", "command": "narrow_mode",
                        "ok": True, "pane_id": pane_id, "narrow": True,
                    })
                else:
                    if pane_id not in narrow_companions:
                        await send_to_client(ws, {
                            "type": "command_result", "command": "narrow_mode",
                            "ok": False, "pane_id": pane_id, "narrow": False,
                            "note": "该 pane 不在窄屏模式",
                        })
                        continue
                    companion = narrow_companions.get(pane_id) or ""
                    if not companion:
                        # 虚拟窄屏（开启时已足够窄、未创建陪衬）
                        narrow_companions.pop(pane_id, None)
                        await send_to_client(ws, {
                            "type": "command_result", "command": "narrow_mode",
                            "ok": True, "pane_id": pane_id, "narrow": False,
                        })
                        continue
                    try:
                        returncode, _ = await run_herdr_rc_async(
                            "pane", "close", companion, remote=remote)
                    except Exception as e:
                        log.warning("narrow_mode close failed for pane %s: %s", companion, e)
                        await send_to_client(ws, {"type": "error", "message": "narrow_mode command failed"})
                        continue
                    # 无论关闭成功与否都清掉映射：pane 可能已被用户在 Mac 上
                    # 手动关掉，此时再留着映射会让窄屏模式永远开不了。
                    narrow_companions.pop(pane_id, None)
                    if returncode != 0:
                        log.warning("narrow_mode close failed for pane %s with exit %s", companion, returncode)
                        await send_to_client(ws, {"type": "error", "message": "narrow_mode command failed"})
                        continue
                    await send_to_client(ws, {
                        "type": "command_result", "command": "narrow_mode",
                        "ok": True, "pane_id": pane_id, "narrow": False,
                    })
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                if workspace_id:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    await run_herdr_async("tab", "create", "--workspace", workspace_id, "--focus")
                    await send_to_client(ws, {"type": "tab_created", "ok": True})
                else:
                    await send_to_client(ws, {"type": "error", "message": "workspace_id required"})
            elif msg_type == "create_workspace":
                log.info("Create workspace from %s (%s)", ip, device)
                audit("create_workspace", ip, device, "", "")
                try:
                    returncode, stdout = await run_herdr_rc_async("workspace", "create", "--focus")
                except Exception as e:
                    log.warning("create_workspace failed: %s", e)
                    await send_to_client(ws, {"type": "error", "message": "create_workspace command failed"})
                    continue
                if returncode != 0:
                    log.warning("create_workspace failed with exit %s", returncode)
                    await send_to_client(ws, {"type": "error", "message": "create_workspace command failed"})
                    continue
                wid, label, pane_id = _parse_workspace_created(stdout)
                if wid and label:
                    _set_workspace_label(wid, label)
                elif wid:
                    workspace_label_cache.setdefault(wid, wid)
                if pane_id:
                    # 立刻登记，避免等下一轮 poll 才允许 read_pane / send_text
                    known_panes.add(pane_id)
                    pane_remote_map.setdefault(pane_id, None)
                    agent_cache[pane_id] = {
                        "pane_id": pane_id,
                        "agent": "shell",
                        "label": label,
                        "status": "unknown",
                        "cwd": "",
                        "project": label or wid,
                        "host": "local",
                        "remote": None,
                        "workspace_id": wid,
                        "workspace_label": label,
                        "tab_id": "",
                    }
                payload = {"type": "workspace_created", "ok": True}
                if wid:
                    payload["workspace_id"] = wid
                if label:
                    payload["label"] = label
                if pane_id:
                    payload["pane_id"] = pane_id
                await send_to_client(ws, payload)
                # 推一帧 agents，让其它客户端也能立刻看到空 shell pane
                try:
                    await _poll_once()
                except Exception as e:
                    log.warning("post-create poll failed: %s", e)
            elif msg_type == "rename_workspace":
                workspace_id = msg.get("workspace_id", "")
                label = (msg.get("label") or "").strip()
                if not workspace_id:
                    await send_to_client(ws, {"type": "error", "message": "workspace_id required"})
                    continue
                if not label:
                    await send_to_client(ws, {"type": "error", "message": "label required"})
                    continue
                log.info("Rename workspace from %s (%s): workspace=%s label=%s",
                         ip, device, workspace_id, label)
                audit("rename_workspace", ip, device, "", f"workspace={workspace_id} label={label}")
                try:
                    returncode, _ = await run_herdr_rc_async(
                        "workspace", "rename", workspace_id, label)
                except Exception as e:
                    log.warning("rename_workspace failed for %s: %s", workspace_id, e)
                    await send_to_client(ws, {"type": "error", "message": "rename_workspace command failed"})
                    continue
                if returncode != 0:
                    log.warning("rename_workspace failed for %s with exit %s", workspace_id, returncode)
                    await send_to_client(ws, {"type": "error", "message": "rename_workspace command failed"})
                    continue
                _set_workspace_label(workspace_id, label)
                await send_to_client(ws, {
                    "type": "workspace_renamed", "ok": True,
                    "workspace_id": workspace_id, "label": label,
                })
            elif msg_type == "close_workspace":
                workspace_id = msg.get("workspace_id", "")
                if not workspace_id:
                    await send_to_client(ws, {"type": "error", "message": "workspace_id required"})
                    continue
                log.info("Close workspace from %s (%s): workspace=%s", ip, device, workspace_id)
                audit("close_workspace", ip, device, "", f"workspace={workspace_id}")
                try:
                    returncode, _ = await run_herdr_rc_async("workspace", "close", workspace_id)
                except Exception as e:
                    log.warning("close_workspace failed for %s: %s", workspace_id, e)
                    await send_to_client(ws, {"type": "error", "message": "close_workspace command failed"})
                    continue
                if returncode != 0:
                    log.warning("close_workspace failed for %s with exit %s", workspace_id, returncode)
                    await send_to_client(ws, {"type": "error", "message": "close_workspace command failed"})
                    continue
                workspace_label_cache.pop(workspace_id, None)
                await send_to_client(ws, {
                    "type": "workspace_closed", "ok": True,
                    "workspace_id": workspace_id,
                })
            elif msg_type == "git_status":
                pane_id = msg.get("pane_id", "")
                workspace_id = msg.get("workspace_id", "")
                mode = msg.get("mode", "worktree")
                base = msg.get("base", "")
                cwd, remote, resolved_ws = _resolve_git_target(pane_id, workspace_id)
                if not cwd:
                    await send_to_client(ws, {
                        "type": "git_status", "ok": False,
                        "message": "unknown pane or workspace, or cwd unavailable",
                        "workspace_id": resolved_ws or workspace_id or "",
                        "pane_id": pane_id or "",
                    })
                    continue
                log.info("Git status from %s (%s): cwd=%s remote=%s mode=%s",
                         ip, device, cwd, remote or "local", mode or "worktree")
                audit("git_status", ip, device, pane_id or resolved_ws, f"cwd={cwd}")
                result = await fetch_git_status_async(
                    cwd, remote=remote, mode=mode, base=base)
                payload = {"type": "git_status", **result}
                if pane_id:
                    payload["pane_id"] = pane_id
                if resolved_ws:
                    payload["workspace_id"] = resolved_ws
                await send_to_client(ws, payload)
            elif msg_type == "git_diff":
                pane_id = msg.get("pane_id", "")
                workspace_id = msg.get("workspace_id", "")
                path = msg.get("path", "")
                mode = msg.get("mode", "worktree")
                base = msg.get("base", "")
                if not _sanitize_git_path(path):
                    await send_to_client(ws, {
                        "type": "git_diff", "ok": False,
                        "message": "invalid path",
                        "path": path or "",
                    })
                    continue
                cwd, remote, resolved_ws = _resolve_git_target(pane_id, workspace_id)
                if not cwd:
                    await send_to_client(ws, {
                        "type": "git_diff", "ok": False,
                        "message": "unknown pane or workspace, or cwd unavailable",
                        "path": path or "",
                    })
                    continue
                audit("git_diff", ip, device, pane_id or resolved_ws, f"cwd={cwd} path={path}")
                result = await fetch_git_diff_async(
                    cwd, path, mode=mode, base=base, remote=remote,
                    untracked=bool(msg.get("untracked")),
                )
                payload = {"type": "git_diff", **result}
                if pane_id:
                    payload["pane_id"] = pane_id
                if resolved_ws:
                    payload["workspace_id"] = resolved_ws
                await send_to_client(ws, payload)
            elif msg_type == "git_show":
                pane_id = msg.get("pane_id", "")
                workspace_id = msg.get("workspace_id", "")
                path = msg.get("path", "")
                if not _sanitize_git_path(path):
                    await send_to_client(ws, {
                        "type": "git_show", "ok": False,
                        "message": "invalid path",
                        "path": path or "",
                    })
                    continue
                cwd, remote, resolved_ws = _resolve_git_target(pane_id, workspace_id)
                if not cwd:
                    await send_to_client(ws, {
                        "type": "git_show", "ok": False,
                        "message": "unknown pane or workspace, or cwd unavailable",
                        "path": path or "",
                    })
                    continue
                audit("git_show", ip, device, pane_id or resolved_ws, f"cwd={cwd} path={path}")
                result = await fetch_git_show_async(cwd, path, remote=remote)
                payload = {"type": "git_show", **result}
                if pane_id:
                    payload["pane_id"] = pane_id
                if resolved_ws:
                    payload["workspace_id"] = resolved_ws
                await send_to_client(ws, payload)
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                if sub and sub not in push_subscriptions:
                    push_subscriptions.append(sub)
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await send_to_client(ws, {"type": "push_subscribed", "ok": True})
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                if sub and sub in push_subscriptions:
                    push_subscriptions.remove(sub)
                    _save_push_subs()
                await send_to_client(ws, {"type": "push_unsubscribed", "ok": True})
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)
        forget_client(ws)


class UDPPlugin(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        try:
            event = json.loads(data.decode())
        except Exception:
            return
        ok, reason = validate_agent_event(event)
        if not ok:
            log.warning("Rejected UDP event from %s: %s", addr, reason)
            return
        event_queue.put_nowait(event)


def start_mdns():
    try:
        from zeroconf import Zeroconf, ServiceInfo
        import socket as sock_mod
        import threading
        ip = sock_mod.gethostbyname(sock_mod.gethostname())
        info = ServiceInfo(
            "_herdr-remote._tcp.local.", "herdr-remote._herdr-remote._tcp.local.",
            addresses=[sock_mod.inet_aton(ip)], port=WS_PORT,
        )
        zc = Zeroconf()
        threading.Thread(target=zc.register_service, args=(info,), daemon=True).start()
        log.info("mDNS registering at %s", ip)
        return zc, info
    except Exception as e:
        log.warning("mDNS skipped: %s", e)
        return None, None


async def main():
    zc, info = start_mdns()
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(UDPPlugin, local_addr=("127.0.0.1", 8376))
    except OSError:
        log.warning("UDP 8376 in use, plugin push disabled")
    asyncio.create_task(poll_loop())
    asyncio.create_task(pane_push_loop())
    asyncio.create_task(event_push())
    # permessage-deflate: browsers negotiate automatically when offered
    server = await serve(
        handle_client,
        "0.0.0.0",
        WS_PORT,
        process_request=process_request,
        compression="deflate",
    )
    hosts = ["local"] + REMOTES
    log.info("herdr-remote relay on :%d (WebSocket + HTTP POST)", WS_PORT)
    log.info("Polling: %s", ", ".join(hosts))
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)
    await stop
    server.close()
    if zc and info:
        zc.unregister_service(info)
        zc.close()


if __name__ == "__main__":
    asyncio.run(main())
