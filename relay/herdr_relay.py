#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "zeroconf>=0.80.0", "pywebpush>=2.0.0", "py-vapid>=1.9.0"]
# ///
"""herdr-remote relay — polls herdr, accepts push events (HTTP POST + WebSocket + UDP), broadcasts to clients."""
import asyncio, json, logging, os, re, secrets, shutil, signal, socket, subprocess, time

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
last_statuses = {}
event_queue = asyncio.Queue()
pane_remote_map = {}
known_panes = set()
agent_cache = {}

SAFE_RESPONSES = {"y", "n", "a", "yes", "no", "trust", "yes, single permission", "trust, always allow", "no (tab to edit)", "approve all pending", "configure individually", "exit (cancel subagents)"}
SAFE_KEYS = {"y", "n", "a", "Enter", "Tab", "Escape", "C-c", "Up", "Down", "Left", "Right", "BSpace"} | {
    str(number) for number in range(10)
}


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
# 上限取 4000：足够容纳 relay 自己截断到 500 的 prompt，又不至于撑爆推送体积。
_EVENT_FIELD_LIMIT = 4000
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


async def get_all_agents_async():
    """本机与所有 remote 并发查询，总耗时取最慢的一个而非累加。"""
    results = await asyncio.gather(
        get_agents_from_host_async(remote=None),
        *(get_agents_from_host_async(remote=r) for r in REMOTES),
    )
    agents = []
    for chunk in results:
        agents.extend(chunk)
    return agents


async def read_pane_async(pane_id, remote=None):
    raw = await run_herdr_async("pane", "read", pane_id, "--lines", "50",
                                "--source", "recent", remote=remote)
    lines = [l for l in raw.splitlines() if l.strip() and not CHROME_RE.search(l)]
    return "\n".join(lines[-20:])


def _parse_pane_list(raw, remote):
    """解析 `herdr pane list` 的 JSON 输出。同步与异步版共用，避免两处漂移。"""
    host_label = remote or "local"
    try:
        data = json.loads(raw)
        panes = data.get("result", {}).get("panes", [])
        return [
            {
                "pane_id": p["pane_id"],
                "agent": p.get("agent", ""),
                "label": p.get("label", ""),
                "status": p.get("agent_status", "unknown"),
                "cwd": p.get("cwd", ""),
                "project": os.path.basename(p.get("cwd", "")),
                "host": host_label,
                "remote": remote,
                "workspace_id": p.get("workspace_id", ""),
                "tab_id": p.get("tab_id", ""),
            }
            for p in panes if p.get("agent")
        ]
    except (json.JSONDecodeError, KeyError):
        return []


def get_agents_from_host(remote=None):
    return _parse_pane_list(run_herdr("pane", "list", remote=remote), remote)


def get_all_agents():
    agents = get_agents_from_host(remote=None)
    for remote in REMOTES:
        agents.extend(get_agents_from_host(remote=remote))
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


async def broadcast(msg):
    data = json.dumps(msg)
    dead = set()
    for ws in list(clients):
        try:
            await ws.send(data)
        except (ConnectionClosedError, ConnectionClosedOK):
            dead.add(ws)
        except Exception:
            dead.add(ws)
    if dead:
        log.debug("Removed %d dead client(s)", len(dead))
    clients.difference_update(dead)


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
        "prompt": content[:500],
        "options": options or TOOL_OPTIONS,
    })
    await send_web_push(
        title=f"🐑 {project} blocked",
        body=content[:120],
        url=f"/?pane={pane_id}",
    )


async def _poll_once():
        agents = await get_all_agents_async()
        # Always broadcast (even empty list) so clients stay in sync
        for a in agents:
            pane_remote_map[a["pane_id"]] = a.get("remote")
            known_panes.add(a["pane_id"])
            agent_cache[a["pane_id"]] = a
        await broadcast({"type": "agents", "agents": agents})
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
                        "prompt": content[:500],
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
            await broadcast(update)


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
    connected_at = time.monotonic()

    # 立即把缓存快照推给这个新客户端。此前连上后不发任何东西，客户端只能干等
    # 下一次 2 秒轮询广播——实测握手仅 75ms，首屏却要 534ms，最坏等满
    # POLL_INTERVAL，而 agent_cache 里的数据一直都在。
    #
    # 只发给 ws 本人而非 broadcast：其它客户端的数据没有变化，没必要重发。
    # 空缓存也要发，让客户端从 Loading 态切到 empty 态，否则界面会一直停在
    # Loading 直到下一次轮询。
    # 必须在进入消息循环之前发，否则客户端一连上就发请求时，响应会排在快照
    # 前面，UI 拿到乱序数据。
    try:
        await ws.send(json.dumps({"type": "agents", "agents": list(agent_cache.values())}))
    except Exception as e:
        # 客户端可能刚连上就断了；这不是错误路径，不该刷 traceback
        log.debug("Initial snapshot not delivered to %s: %s", ip, e)

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_type = msg.get("type")
            if msg_type == "respond":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if text.strip().lower() not in SAFE_RESPONSES:
                    await ws.send(json.dumps({"type": "error", "message": "response not in allowlist"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Response from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("respond", ip, device, pane_id, f"text={text!r}")
                await run_herdr_async("pane", "send-text", pane_id, text + "\n", remote=remote)
            elif msg_type == "agent_event":
                ok, reason = validate_agent_event(msg)
                if not ok:
                    await ws.send(json.dumps({"type": "error", "message": f"invalid agent_event: {reason}"}))
                else:
                    event_queue.put_nowait(msg)
            elif msg_type == "read_pane":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                lines = msg.get("lines", "30")
                remote = pane_remote_map.get(pane_id)
                content = await run_herdr_async("pane", "read", pane_id, "--lines", str(lines), "--source", "recent", remote=remote)
                await ws.send(json.dumps({"type": "pane_content", "pane_id": pane_id, "content": content}))
            elif msg_type == "send_keys":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                keys = msg.get("keys", [])
                if not all(k in SAFE_KEYS for k in keys):
                    await ws.send(json.dumps({"type": "error", "message": "keys contain disallowed values"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Keys from %s (%s): pane=%s keys=%s", ip, device, pane_id, keys)
                audit("send_keys", ip, device, pane_id, f"keys={keys}")
                try:
                    returncode, _ = await run_herdr_rc_async("pane", "send-keys", pane_id, *keys, remote=remote)
                except Exception as e:
                    log.warning("send_keys command failed for pane %s: %s", pane_id, e)
                    await ws.send(json.dumps({"type": "error", "message": "send_keys command failed"}))
                    continue
                if returncode != 0:
                    log.warning("send_keys command failed for pane %s with exit %s", pane_id, returncode)
                    await ws.send(json.dumps({"type": "error", "message": "send_keys command failed"}))
                    continue
                await ws.send(json.dumps({"type": "command_result", "command": "send_keys", "ok": True}))
            elif msg_type == "send_text":
                pane_id = msg["pane_id"]
                if pane_id not in known_panes:
                    await ws.send(json.dumps({"type": "error", "message": "unknown pane_id"}))
                    continue
                text = msg.get("text", "")
                if not text or len(text) > 1000:
                    await ws.send(json.dumps({"type": "error", "message": "text empty or too long"}))
                    continue
                remote = pane_remote_map.get(pane_id)
                log.info("Text from %s (%s): pane=%s text=%r", ip, device, pane_id, text)
                audit("send_text", ip, device, pane_id, f"text={text!r}")
                await run_herdr_async("pane", "send-text", pane_id, text, remote=remote)
                # Web/Telegram 都是 send_text 后紧跟 send_keys Enter；对 Cursor
                # 必须在两条命令之间留出 paste settle，否则跟进框会残留原文。
                agent_name = (agent_cache.get(pane_id) or {}).get("agent")
                await settle_after_paste(agent_name)
            elif msg_type == "create_tab":
                workspace_id = msg.get("workspace_id", "")
                if workspace_id:
                    log.info("Create tab from %s (%s): workspace=%s", ip, device, workspace_id)
                    audit("create_tab", ip, device, "", f"workspace={workspace_id}")
                    await run_herdr_async("tab", "create", "--workspace", workspace_id, "--focus")
                    await ws.send(json.dumps({"type": "tab_created", "ok": True}))
                else:
                    await ws.send(json.dumps({"type": "error", "message": "workspace_id required"}))
            elif msg_type == "push_subscribe":
                sub = msg.get("subscription")
                if sub and sub not in push_subscriptions:
                    push_subscriptions.append(sub)
                    _save_push_subs()
                    log.info("Push subscription added from %s (%s)", ip, device)
                await ws.send(json.dumps({"type": "push_subscribed", "ok": True}))
            elif msg_type == "push_unsubscribe":
                sub = msg.get("subscription")
                if sub and sub in push_subscriptions:
                    push_subscriptions.remove(sub)
                    _save_push_subs()
                await ws.send(json.dumps({"type": "push_unsubscribed", "ok": True}))
    except (ConnectionClosedError, ConnectionClosedOK):
        pass
    finally:
        duration = int(time.monotonic() - connected_at)
        log.info("Client disconnected: ip=%s device=%s duration=%ds", ip, device, duration)
        clients.discard(ws)


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
    asyncio.create_task(event_push())
    server = await serve(handle_client, "0.0.0.0", WS_PORT, process_request=process_request)
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
