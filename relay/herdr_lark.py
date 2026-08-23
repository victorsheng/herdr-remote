#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lark-oapi>=1.4.0", "websockets>=14.0"]
# ///
"""herdr-remote 飞书客户端 —— 在飞书里监控与操控 herdr agent。

与 Telegram 客户端同构：都连本机 relay 的 ws://127.0.0.1:8375，只需 Mac 出站
联网，不需要 Cloudflare Tunnel / Tailscale Funnel 之类的公网入口。

设计与实施计划见:
  docs/superpowers/specs/2026-08-22-lark-client-design.md
  docs/superpowers/plans/2026-08-22-lark-client.md
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("herdr-lark")

APP_ID = os.environ.get("HERDR_LARK_APP_ID", "")
APP_SECRET = os.environ.get("HERDR_LARK_APP_SECRET", "")
CHAT_ID = os.environ.get("HERDR_LARK_CHAT_ID", "")
DOMAIN = os.environ.get("HERDR_LARK_DOMAIN", "feishu")
# card（默认，带色彩与等宽代码块）/ text（纯文本，最省、最不挑客户端）
RENDER_MODE = os.environ.get("HERDR_LARK_RENDER", "card")
# 发完指令自动跟随几秒；off 关闭。
AUTOWATCH_ENV = os.environ.get("HERDR_LARK_AUTOWATCH", "")
# 审计回执：写操作在本群留一行痕迹。off/0/false 关闭。
AUDIT_ENV = os.environ.get("HERDR_LARK_AUDIT", "on")

CONFIG_DIR = os.path.expanduser("~/.config/herdr-remote")
SEEN_PATH = os.environ.get("HERDR_LARK_SEEN_PATH", os.path.join(CONFIG_DIR, "lark_seen.json"))
SEEN_LIMIT = 5000
BINDING_PATH = os.environ.get(
    "HERDR_LARK_BINDING_PATH", os.path.join(CONFIG_DIR, "lark_bindings.json"))
CHATS_PATH = os.environ.get(
    "HERDR_LARK_CHATS_PATH", os.path.join(CONFIG_DIR, "lark_chats.json"))

RELAY_WS = os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375")
# 不带 token 的变体，用于展示与日志；带 token 的原串绝不外泄。
RELAY_WS_SAFE = RELAY_WS.split("?", 1)[0]
_RELAY_TOKEN = RELAY_WS.split("token=", 1)[1] if "token=" in RELAY_WS else ""

AGENT_PAGE_SIZE = 20
PENDING_LIMIT = 500
# 手机上看进展要够长；relay 上限 5000，取 200 行足够看清最近做了什么。
READ_LINES = 200

STATUS_ORDER = {"blocked": 0, "working": 1, "done": 2, "idle": 3, "unknown": 3}
STATUS_LABELS = {
    "blocked": "BLOCKED",
    "working": "WORKING",
    "done": "DONE",
    "idle": "IDLE",
    "unknown": "IDLE",
}

# 动作与单字母编码互查。飞书 action.value 没有 Telegram 的 64 字节上限，
# 但沿用同一套编码，两端逻辑才能一一对照。
ACTION_CODES = {
    "read": "r",
    "interrupt": "i",
    "select_send": "s",
    "select_reply": "q",
    "trust": "t",
    "approval": "k",
    "page": "g",
}
CODE_ACTIONS = {code: action for action, code in ACTION_CODES.items()}

# 审批 generation 的字母表。借鉴官方 Claude Code Channels：去掉 l，
# 免得在手机上被看成 1 或 I。
_GENERATION_ALPHABET = "abcdefghijkmnopqrstuvwxyz"
_GENERATION_LENGTH = 5


def scrub(value) -> str:
    """把密钥从任何将被记录或外发的字符串里抹掉。

    websockets 的异常（如 InvalidURI）会把带 ?token= 的完整 relay URL 塞进
    异常文本，原样发给用户就等于泄露 token。
    """
    text = str(value)
    for secret in (_RELAY_TOKEN, APP_SECRET):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def new_generation() -> str:
    """生成一次审批的世代标识。"""
    return "".join(secrets.choice(_GENERATION_ALPHABET) for _ in range(_GENERATION_LENGTH))


# --- 消息去重 ---

class SeenStore:
    """记住已处理过的飞书 message_id。

    飞书长连接会重推消息，同一条 message_id 可能到达多次；不去重的话 agent
    会收到重复指令。Telegram 没这个问题（update_id 单调递增），所以这是飞书
    客户端独有的一层。

    状态落盘，进程重启后不会把历史消息重放一遍。
    """

    def __init__(self, path: str = SEEN_PATH, limit: int = SEEN_LIMIT):
        self.path = path
        self.limit = limit
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._load()

    def __len__(self) -> int:
        return len(self._ids)

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            # 缓存损坏只该丢掉历史，不该让 bot 起不来。
            log.warning("Ignoring unreadable seen-id cache: %s", scrub(exc))
            return
        if not isinstance(payload, list):
            log.warning("Ignoring seen-id cache with unexpected shape")
            return
        for message_id in payload:
            if isinstance(message_id, str):
                self._ids[message_id] = None
        log.info("Loaded %d seen message ids", len(self._ids))

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(list(self._ids), fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Failed to persist seen message ids: %s", scrub(exc))

    def add(self, message_id: str) -> bool:
        """记下这条消息；返回 True 表示首次见到，应当处理。"""
        if message_id in self._ids:
            return False
        self._ids[message_id] = None
        if len(self._ids) > self.limit:
            for _ in range(self.limit // 2):
                self._ids.popitem(last=False)
        self._save()
        return True


class BindingStore:
    """群 ↔ agent 的绑定，落盘。

    只在内存里的话，服务一重启绑定就没了，得重新 /read 一遍才能继续
    指挥——而重启恰恰是改完代码后最常做的事。
    """

    def __init__(self, path: str = BINDING_PATH):
        self.path = path
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable binding cache: %s", scrub(exc))
            return
        if not isinstance(payload, dict):
            log.warning("Ignoring binding cache with unexpected shape")
            return
        self._map = {
            str(k): str(v) for k, v in payload.items() if isinstance(v, str)
        }
        if self._map:
            log.info("Loaded %d chat bindings", len(self._map))

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(self._map, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Failed to persist bindings: %s", scrub(exc))

    def get(self, chat_id: str) -> str | None:
        return self._map.get(str(chat_id))

    def set(self, chat_id: str, pane_id: str) -> None:
        self._map[str(chat_id)] = pane_id
        self._save()

    def as_dict(self) -> dict[str, str]:
        return dict(self._map)

    def replace(self, table: dict[str, str]) -> None:
        self._map = dict(table)
        self._save()

    def remove(self, chat_id: str) -> None:
        if self._map.pop(str(chat_id), None) is not None:
            self._save()


def prune_bindings(table: dict[str, str], known_chats: set[str],
                   known_panes: set[str]) -> dict[str, str]:
    """清掉指向已消失的群或 agent 的绑定。

    群被解散、agent 被关掉之后，陈旧绑定会让通知发到错的地方，
    也会让 /spaces 的复用判定认错。

    两个集合都空时不动——那通常意味着还没拿到列表，不是真的都没了。
    """
    if not known_chats and not known_panes:
        return dict(table)
    return {
        chat: pane for chat, pane in table.items()
        if (not known_chats or chat in known_chats)
        and (not known_panes or pane in known_panes)
    }


class ChatIdStore:
    """授权群列表，落盘。

    /spaces 建的群只加进内存的话，重启后就丢了——而且 prune_bindings
    会把它们的绑定当成「群已消失」删掉。
    """

    def __init__(self, path: str = CHATS_PATH):
        self.path = path
        self._ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Ignoring unreadable chat-id cache: %s", scrub(exc))
            return
        if isinstance(payload, list):
            self._ids = {str(c) for c in payload if c}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as fh:
                json.dump(sorted(self._ids), fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Failed to persist chat ids: %s", scrub(exc))

    def all(self) -> set[str]:
        return set(self._ids)

    def add(self, chat_id: str) -> None:
        if chat_id and chat_id not in self._ids:
            self._ids.add(chat_id)
            self._save()

    def remove(self, chat_id: str) -> None:
        if chat_id in self._ids:
            self._ids.discard(chat_id)
            self._save()

    def seed(self, chat_ids: set[str]) -> None:
        """把环境变量里的群并进来。"""
        merged = self._ids | {c for c in chat_ids if c}
        if merged != self._ids:
            self._ids = merged
            self._save()


# --- pane 标识 ---

def pane_callback_token(pane_id: str) -> str:
    return hashlib.sha256(pane_id.encode()).hexdigest()[:16]


def resolve_pane_token(token: str, agents: list[dict], pending: dict) -> str | None:
    """把按钮里的 token 还原成 pane_id；有歧义时返回 None 而不是猜。"""
    agent_matches = {
        agent.get("pane_id") for agent in agents
        if agent.get("pane_id") and pane_callback_token(agent["pane_id"]) == token
    }
    if len(agent_matches) == 1:
        return next(iter(agent_matches))
    if len(agent_matches) > 1:
        return None
    pending_matches = {
        pane_id for pane_id in pending.values()
        if pane_id and pane_callback_token(pane_id) == token
    }
    return next(iter(pending_matches)) if len(pending_matches) == 1 else None


def action_value(action: str, pane_id: str, **extra) -> dict:
    """构造卡片按钮的 value。飞书原生收发 dict，无需再序列化成字符串。"""
    return {"a": ACTION_CODES[action], "p": pane_callback_token(pane_id), **extra}


def parse_action_value(value: dict, agents: list[dict], pending: dict) -> dict:
    data = dict(value or {})
    data["action"] = CODE_ACTIONS.get(data.get("a"), "invalid")
    if "p" in data:
        data["pane_id"] = resolve_pane_token(data["p"], agents, pending)
    return data


# --- agent 列表 ---

def agents_for_action(action: str, agents: list[dict]) -> list[dict]:
    if action == "interrupt":
        return [a for a in agents if a.get("status") in ("working", "blocked")]
    if action == "trust":
        return [a for a in agents if a.get("status") == "blocked"]
    return list(agents)



def index_agents(agents: list[dict]) -> list[dict]:
    """给 agent 定序号，**与状态无关**。

    不能用 sorted_agents（状态优先）编号：agent 一开始干活就跳到队首，
    其余全部后移——用户看到列表、几秒后打 /read 3，操作到的已经是别人了。
    按 pane_id 排序，状态怎么变序号都不动。
    """
    return sorted(agents, key=lambda a: str(a.get("pane_id") or ""))


def sorted_agents(agent_list: list[dict]) -> list[dict]:
    return sorted(agent_list, key=lambda agent: (
        STATUS_ORDER.get(agent.get("status", "unknown"), 3),
        (agent.get("project") or "").lower(),
        (agent.get("agent") or "").lower(),
        (agent.get("host") or "local").lower(),
        agent.get("pane_id") or "",
    ))


def compact_identifier(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    digest = hashlib.sha1(value.encode()).hexdigest()[:12]
    return value[:limit - 15] + "..." + digest


def agent_button_labels(agent_list: list[dict], limit: int = 64) -> list[str]:
    """给每个 agent 一个人眼可区分的按钮文案。

    同名项目 + 同名 agent 很常见，逐级补上 host、pane_id 摘要、序号来消歧，
    否则一屏按钮长得一模一样，点错了不知道点了谁。
    """
    bases, contexts = [], []
    for agent in agent_list:
        status = STATUS_LABELS.get(agent.get("status", "unknown"), "UNKNOWN")
        project = agent.get("project") or agent.get("cwd") or "unknown"
        name = agent.get("agent") or "agent"
        host = agent.get("host", "local")
        bases.append(f"[{status}] {project} ({name})")
        contexts.append(f" @{compact_identifier(host, 24)}" if host != "local" else "")

    provisional = [
        base[:max(1, limit - len(ctx))] + ctx for base, ctx in zip(bases, contexts)
    ]
    counts = Counter(provisional)

    labels = []
    for agent, base, ctx, candidate in zip(agent_list, bases, contexts, provisional):
        if counts[candidate] == 1:
            labels.append(candidate)
            continue
        pane_id = compact_identifier(agent.get("pane_id", "?"), 18)
        suffix = ctx + f" [{pane_id}]"
        labels.append(base[:max(1, limit - len(suffix))] + suffix)

    unique, used = [], set()
    for label in labels:
        candidate, ordinal = label, 2
        while candidate in used:
            marker = f" #{ordinal}"
            candidate = label[:limit - len(marker)] + marker
            ordinal += 1
        used.add(candidate)
        unique.append(candidate)
    return unique


# --- 待回复消息 ---

def register_pending(pending: dict, chat_id: str, message_id: str, pane_id: str) -> None:
    """记住某条消息对应哪个 pane，用户回复它时才知道发给谁。"""
    key = (str(chat_id), str(message_id))
    pending[key] = pane_id
    if isinstance(pending, OrderedDict):
        pending.move_to_end(key)
    while len(pending) > PENDING_LIMIT:
        oldest = next(iter(pending))
        del pending[oldest]


def pending_pane(pending: dict, chat_id: str, message_id: str) -> str | None:
    return pending.get((str(chat_id), str(message_id)))


def find_agent(agents: list[dict], pane_id: str) -> dict | None:
    matches = [a for a in agents if a.get("pane_id") == pane_id]
    return matches[0] if len(matches) == 1 else None


# --- relay 通信 ---

def ws_connect(*args, **kwargs):
    """包一层，测试才好替换掉真实连接。"""
    import websockets
    return websockets.connect(*args, **kwargs)


async def send_to_relay(pane_id: str, text: str) -> None:
    """走 respond 把文本交给 agent（relay 会用 send-text 粘贴过去）。"""
    async with ws_connect(RELAY_WS) as ws:
        await ws.send(json.dumps({"type": "respond", "pane_id": pane_id, "text": text}))


async def send_keys_to_relay(pane_id: str, keys: list[str]) -> None:
    """发按键，并且一定要等 relay 的 ack。

    不等 ack 就报成功是 Telegram 版踩过的坑：relay 的 SAFE_KEYS 白名单只认
    "C-c" 这类名字，发别名会被整条拒绝，而用户看到的却是「已发送」。
    """
    async with ws_connect(RELAY_WS) as ws:
        await ws.send(json.dumps({"type": "send_keys", "pane_id": pane_id, "keys": keys}))
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            response = json.loads(raw)
            if response.get("type") == "error":
                raise RuntimeError(response.get("message", "relay rejected keys"))
            if (response.get("type") == "command_result"
                    and response.get("command") == "send_keys"):
                if not response.get("ok"):
                    raise RuntimeError(response.get("message", "relay rejected keys"))
                return
        raise RuntimeError("relay did not acknowledge keys")


async def send_text_to_relay(pane_id: str, text: str) -> None:
    """把文本送进 pane 并回车。

    send_text 本身不带换行，得再补一次 Enter——但**必须等 relay 的 ack**。
    relay 在粘贴之后会做 settle（Cursor 尤其需要，见 herdr_relay.py 的
    settle_after_paste），settle 完才回 ack。不等就发 Enter，回车会赶在
    粘贴稳定之前到达，表现为「消息进去了但没有回车」。

    粘贴失败时更不能补 Enter：那会把输入框里残留的上一条内容提交出去。
    """
    if not text or len(text) > 1000:
        raise ValueError("text must contain 1-1000 characters")
    async with ws_connect(RELAY_WS) as ws:
        await ws.send(json.dumps({"type": "send_text", "pane_id": pane_id, "text": text}))
        for _ in range(5):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            response = json.loads(raw)
            if response.get("type") == "error":
                raise RuntimeError(response.get("message", "relay rejected text"))
            if (response.get("type") == "command_result"
                    and response.get("command") == "send_text"):
                if not response.get("ok"):
                    raise RuntimeError(response.get("message", "relay rejected text"))
                await ws.send(json.dumps({
                    "type": "send_keys", "pane_id": pane_id, "keys": ["Enter"],
                }))
                return
        raise RuntimeError("relay did not acknowledge text")


async def read_pane(pane_id: str, lines: int = READ_LINES) -> str:
    """读取 pane 的终端输出。失败时返回可直接外发的提示（已脱敏）。"""
    try:
        async with ws_connect(RELAY_WS) as ws:
            await ws.send(json.dumps({
                "type": "read_pane", "pane_id": pane_id, "lines": lines,
            }))
            # 可能先撞上一条 agents 广播，往后多读几条找 pane_content。
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT_S)
                msg = json.loads(raw)
                if msg.get("type") == "pane_content":
                    return msg.get("content", "(empty)")
    except Exception as exc:
        return f"(error reading pane: {scrub(exc)})"
    return "(no response)"


# --- 卡片构造 ---

# 与 relay 的 TOOL_OPTIONS / SUBAGENT_OPTIONS 对应（见 herdr_relay.py:63）。
TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]

TOOL_BUTTONS = [
    ("Yes (once)", "primary"),
    ("Trust (always)", "default"),
    ("No", "danger"),
]

SUBAGENT_BUTTONS = [
    ("Approve all", "primary"),
    ("Configure", "default"),
    ("Cancel", "danger"),
]


def truncate_prompt(text: str, limit: int = 400) -> str:
    """超长时保留首尾，中间标注省略量。

    直接 text[:limit] 会把命令结尾切掉，而审批一条 rm -rf 时最该看清的恰恰
    是末尾的路径。做法来自官方 Claude Code Channels 的权限中继。
    """
    if len(text) <= limit:
        return text
    keep = max(1, (limit - 20) // 2)
    elided = len(text) - keep * 2
    return f"{text[:keep]}\n⋯ 省略 {elided} 字 ⋯\n{text[-keep:]}"


def _approval_buttons(options: list[str] | None) -> list[tuple[str, str]]:
    joined = " ".join(options or []).lower()
    if options and "trust" in joined:
        return TOOL_BUTTONS
    if options and "approve all" in joined:
        return SUBAGENT_BUTTONS
    if not options:
        return TOOL_BUTTONS
    return [(opt.split(",")[0], "default") for opt in options]


def _button(label: str, value: dict, button_type: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "value": value,
    }


def build_blocked_card(
    pane_id: str,
    agent: str,
    project: str,
    prompt: str,
    options: list[str] | None,
    generation: str,
) -> dict:
    """agent 卡住时推的审批卡片。

    按钮带的是选项的 1-based 序号，点击后按对应数字键；发选项文本是不行的
    （见 build 出的 value 与 send_keys_to_relay 的注释）。
    """
    buttons = _approval_buttons(options)
    actions = [
        _button(f"{i + 1}. {label}", action_value(
            "approval", pane_id, g=generation, k=str(i + 1)), style)
        for i, (label, style) in enumerate(buttons)
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"🐑 {agent} blocked in {project}"},
        },
        "elements": [
            {"tag": "div", "text": {
                "tag": "lark_md",
                "content": f"```\n{truncate_prompt(prompt)}\n```",
            }},
            {"tag": "action", "actions": actions},
            {"tag": "action", "actions": [
                _button("Open output & reply", action_value("select_reply", pane_id)),
            ]},
        ],
    }


def build_options_card(pane_id: str, project: str, options: list[str],
                       generation: str, question: str = "") -> dict:
    """读到一个正等着你选的 agent 时，把选项渲染成可点的按钮。

    与 blocked 卡片同构（按钮发的都是选项序号），区别只在于这是
    「读的时候顺手发现的」，而不是 relay 主动推的。
    """
    actions = [
        _button(f"{i + 1}. {opt[:40]}", action_value(
            "approval", pane_id, g=generation, k=str(i + 1)),
            "primary" if i == 0 else "default")
        for i, opt in enumerate(options)
    ]
    elements = []
    if question:
        elements.append({"tag": "div", "text": {
            "tag": "lark_md", "content": f"**{question}**"}})
    elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "turquoise",
            "title": {"tag": "plain_text", "content": f"⌨︎ {project} 正在等你选"},
        },
        "elements": elements,
    }


def current_option_group(groups: list[dict]) -> dict | None:
    """agent 当前在等的那一组。

    TUI 逐组问：答完第一组才显示第二组，所以最后一组才是当前的。
    """
    return groups[-1] if groups else None


def build_agent_picker_card(
    action: str,
    agents: list[dict],
    page: int = 0,
    title: str | None = None,
) -> dict:
    """列出可选 agent 的卡片，超过一页时带翻页按钮。"""
    ordered = sorted_agents(agents)
    disambig = disambiguate_suffixes(ordered)
    page_count = max(1, (len(ordered) + AGENT_PAGE_SIZE - 1) // AGENT_PAGE_SIZE)
    page = min(max(page, 0), page_count - 1)
    start = page * AGENT_PAGE_SIZE
    visible = ordered[start:start + AGENT_PAGE_SIZE]
    labels = agent_button_labels(ordered)[start:start + AGENT_PAGE_SIZE]

    elements = [{"tag": "action", "actions": [
        _button(label + disambig.get(str(agent.get("pane_id")), ""),
                action_value(action, agent["pane_id"]))
        for agent, label in zip(visible, labels)
    ]}]

    if page_count > 1:
        nav = []
        if page > 0:
            nav.append(_button("上一页", {
                "a": ACTION_CODES["page"], "menu": action, "page": page - 1}))
        if page + 1 < page_count:
            nav.append(_button("下一页", {
                "a": ACTION_CODES["page"], "menu": action, "page": page + 1}))
        elements.append({"tag": "action", "actions": nav})

    header_title = title or f"Select an agent ({len(ordered)} total)"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": header_title},
        },
        "elements": elements,
    }


# --- 飞书事件解析 ---

@dataclass
class MessageContext:
    """一次用户输入，来源可能是打字，也可能是点卡片按钮。

    两者归一成同一个结构，下游只有一条处理路径。
    """
    chat_id: str
    message_id: str
    sender_open_id: str
    chat_type: str = "p2p"
    mentioned_bot: bool = False
    text: str = ""
    solo_group: bool = False
    action: dict | None = None


def strip_bot_mention(text: str, mentions: list[dict] | None) -> str:
    """去掉文本里的 @机器人，否则命令解析会带上 @ 前缀。"""
    result = text
    for mention in mentions or []:
        key = mention.get("key")
        name = mention.get("name")
        if key:
            result = result.replace(key, "")
        if name:
            result = result.replace(f"@{name}", "")
    return result.strip()


def parse_message_event(event: dict, bot_open_id: str) -> MessageContext:
    message = event.get("message") or {}
    sender = (event.get("sender") or {}).get("sender_id") or {}
    mentions = message.get("mentions") or []

    raw = message.get("content") or ""
    text = ""
    try:
        parsed = json.loads(raw)
        if message.get("message_type") == "text":
            text = parsed.get("text", "")
    except (json.JSONDecodeError, TypeError):
        text = ""

    mentioned = any(
        (m.get("id") or {}).get("open_id") == bot_open_id for m in mentions
    ) if bot_open_id else bool(mentions)

    return MessageContext(
        chat_id=message.get("chat_id", ""),
        message_id=message.get("message_id", ""),
        sender_open_id=sender.get("open_id", ""),
        chat_type=message.get("chat_type", "p2p"),
        mentioned_bot=mentioned,
        text=strip_bot_mention(text, mentions),
    )


def parse_card_action(event: dict) -> MessageContext | None:
    """把卡片点击转成与消息同构的上下文。

    做法借鉴 Hanson/claude-client：点按钮和打字回同样的话，在下游是同一条路径。
    """
    value = (event.get("action") or {}).get("value")
    if not value:
        return None
    # SDK 的 P2CardActionTriggerData 把这两个字段放在 context 下；顶层取值
    # 只是对旧版回调结构的兼容。
    context = event.get("context") or {}
    chat_id = context.get("open_chat_id") or event.get("open_chat_id", "")
    message_id = context.get("open_message_id") or event.get("open_message_id", "")
    return MessageContext(
        chat_id=chat_id,
        message_id=message_id,
        sender_open_id=(event.get("operator") or {}).get("open_id", ""),
        chat_type="p2p",
        mentioned_bot=True,  # 点击即明确意图，无需再要求 @
        action=value,
    )



def parse_chat_ids(value: str) -> set[str]:
    """解析 HERDR_LARK_CHAT_ID：逗号分隔的多个群。"""
    return {c.strip() for c in (value or "").split(",") if c.strip()}


def is_authorized_chat(chat_id: str, allowed) -> bool:
    """这个群授权了吗。空集合 = 发现模式，放行任何群。"""
    if not allowed:
        return True
    if isinstance(allowed, str):
        return chat_id == allowed
    return chat_id in allowed


def should_handle(ctx: MessageContext, bot_open_id: str, chat_id) -> bool:
    """守门：授权、自言自语、群里没 @ 我，三种情况直接丢掉。"""
    if bot_open_id and ctx.sender_open_id == bot_open_id:
        return False
    if not is_authorized_chat(ctx.chat_id, chat_id):
        return False
    if ctx.chat_type == "group" and not ctx.mentioned_bot and not ctx.solo_group:
        return False
    return True


# --- 审批世代 ---

def approval_is_current(tokens: dict, pane_id: str, generation: str | None) -> bool:
    """确认这次点击来自最新一条 blocked 通知，而不是历史消息里的旧按钮。"""
    if not generation:
        return False
    return tokens.get(pane_id) == generation


def prune_approval_tokens(tokens: dict, agents: list[dict]) -> None:
    """pane 一旦离开 blocked，它的审批按钮就该失效。"""
    blocked = {
        a.get("pane_id") for a in agents if a.get("status") == "blocked"
    }
    for pane_id in list(tokens):
        if pane_id not in blocked:
            tokens.pop(pane_id, None)


# --- 终端输出清理 ---

# agent TUI 的界面装饰。手机屏幕就那么大，状态栏、分隔线、更新提示
# 会把真正的输出挤到看不见。relay 的 CHROME_RE 只滤纯分隔线，这里补齐
# Claude Code / Codex 一类 TUI 的状态栏。
_CHROME_PATTERNS = [
    # 纯分隔线 / 表格边框：Markdown 表格在终端里被渲染成 ┌─┬─┐，
    # 手机窄屏本来就会错行，留个残缺的框不如只留内容。
    r"^[\s─━═_—–·⎯⏤=*~│|┃┏┓┗┛┌┐└┘├┤┬┴┼╭╮╰╯+-]+$",
    r"^\s*Context\s+[█░▁▂▃▄▅▆▇]",           # Context ███░░░ 34%
    r"^\s*\[.+\]\s*│",                      # [Opus 5 (1M context)] │ tailcale
    r"bypass permissions on",                # ⏵⏵ bypass permissions on (shift+tab…)
    r"Update installed",                     # ✔ Update installed · Restart to update
    r"^\s*❯\s*$",                            # 空输入行
    r"shift\+tab to cycle",
    r"esc to cancel",
    r"type to queue",
    r"^\s*Usage\s+[█░]",
    r"^\s*<!--.*-->\s*$",                   # HTML 注释，手机上看毫无价值
    r"^\s*<br\s*/?>\s*$",                   # Markdown 里的换行标记
]
_CHROME_RE = re.compile("|".join(_CHROME_PATTERNS))


def follow_up_hint(project: str) -> str:
    """告诉用户现在打字会发给谁——不然不知道自己在跟哪个 agent 说话。"""
    return f"— 直接发消息即可继续指挥 {project}（/agents 换人）"


def _strip_table_pipes(line: str) -> str:
    """去掉表格行的竖线，列之间留空格。

    行首尾的 │ 在手机上纯占地方；列分隔换成空格，内容才不会挤成一坨。
    """
    if "│" not in line and "┃" not in line:
        return line
    stripped = line.strip()
    for ch in ("│", "┃"):
        if stripped.startswith(ch):
            stripped = stripped[1:]
        if stripped.endswith(ch):
            stripped = stripped[:-1]
    return re.sub(r"\s*[│┃]\s*", "   ", stripped).rstrip()


def clean_pane(text: str) -> str:
    """去掉 TUI 界面装饰和空行，只留真正的输出。

    空行全部去掉：Markdown 的段落间距在终端输出里能占到四成篇幅
    （实测 40 行里 16 行是空的），手机窄屏上等于一半内容被挤出屏幕。

    保留还在跑的进度行（如 "✻ Drizzling… (12s)"）——那是有用状态，
    不是装饰。
    """
    lines = []
    for line in (text or "").splitlines():
        if not line.strip() or _CHROME_RE.search(line):
            continue
        lines.append(_strip_table_pipes(line))
    return "\n".join(l for l in lines if l.strip()).strip()


# --- 终端输出卡片 ---

# 颜色在 relay 那层就没了（herdr pane read 输出纯文本，无 ANSI），
# 所以不是「保留」颜色，而是按状态重新上色。
_STATUS_COLORS = {
    "blocked": "red",
    "working": "orange",
    "done": "green",
    "idle": "grey",
    "unknown": "grey",
}
_STATUS_HEADERS = {
    "blocked": "orange",
    "working": "blue",
    "done": "green",
    "idle": "grey",
    "unknown": "grey",
}
_STATUS_ICONS = {
    "blocked": "⏸",
    "working": "▶",
    "done": "✅",
    "idle": "○",
    "unknown": "○",
}
_CARD_BODY_LIMIT = 2400


def status_color(status: str) -> str:
    return _STATUS_COLORS.get(status, "grey")


def build_pane_card(project: str, agent: str, status: str, output: str) -> dict:
    """终端输出卡片：头部状态上色，主体走代码块。

    代码块保住等宽与缩进——终端输出的可读性全靠对齐；颜色则集中在
    头部状态行，两者的好处都占上。
    """
    status = status or "unknown"
    icon = _STATUS_ICONS.get(status, "○")
    color = status_color(status)
    label = STATUS_LABELS.get(status, "IDLE")

    body = (output or "").strip() or "(无输出)"
    if len(body) > _CARD_BODY_LIMIT:
        body = "⋯\n" + body[-_CARD_BODY_LIMIT:]
    # 输出里本来就有 ``` 时会把代码块提前闭合，替换掉。
    body = body.replace("```", "\u2063``\u2063`")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _STATUS_HEADERS.get(status, "grey"),
            "title": {"tag": "plain_text", "content": f"{icon} {project}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                f"<font color='{color}'>{label}</font> · {agent}"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"```\n{body}\n```"}},
        ],
    }


RENDER_MODES = ("card", "text")


def normalize_render_mode(mode: str | None) -> str:
    """认不出来的一律当 card——默认给好看的那个。"""
    value = (mode or "").strip().lower()
    return value if value in RENDER_MODES else "card"


def format_pane_text(project: str, output: str, follow_up: str = "") -> str:
    """text 模式的终端输出：就是原来那套纯文本，不带任何标记。"""
    body = (output or "").strip() or "(无输出)"
    if len(body) > 3000:
        body = "⋯\n" + body[-3000:]
    tail = f"\n\n{follow_up}" if follow_up else ""
    return f"{project}:\n\n{body}{tail}"


# --- 串行队列 ---

class ChatQueue:
    """同一个群串行、不同群并行。

    不串行的话，同一个群连发两条消息会并发跑，而 send_text 是「粘贴 +
    回车」两步——第二条的粘贴可能插进第一条的回车之前，两条糊成一条。

    做法借鉴 remote-claude-code：每个 chat_id 一个 worker 任务，队列
    跑空就销毁，避免群一多就泄漏。
    """

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

    def __len__(self) -> int:
        return len(self._queues)

    def depth(self) -> int:
        """还在排队的任务数。连发多条时能看出排到第几个。"""
        return sum(q.qsize() for q in self._queues.values())

    def submit(self, chat_id: str, factory) -> None:
        """把一个「返回协程的工厂函数」排进这个群的队列。

        传工厂而不是协程对象：协程一旦创建就开始持有资源，排队期间
        干等着没意义。
        """
        chat_id = str(chat_id)
        queue = self._queues.get(chat_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[chat_id] = queue
            self._workers[chat_id] = asyncio.ensure_future(self._run(chat_id, queue))
        queue.put_nowait(factory)

    async def _run(self, chat_id: str, queue: asyncio.Queue) -> None:
        while True:
            if queue.empty():
                # 跑空就退场，连同队列一起清掉。
                self._queues.pop(chat_id, None)
                self._workers.pop(chat_id, None)
                return
            factory = queue.get_nowait()
            try:
                await factory()
            except Exception as exc:
                # 一条炸了不能拖垮后面的。
                log.exception("queued task failed: %s", scrub(exc))

    async def drain(self) -> None:
        """等所有队列跑完。测试用；生产里队列自生自灭。"""
        while self._workers:
            await asyncio.gather(*list(self._workers.values()),
                                 return_exceptions=True)


# --- 流式卡片 ---

# 流式更新按 element_id 定位要改的那块内容。
STREAM_ELEMENT_ID = "pane_out"
STREAM_INTERVAL_S = 1.5
# 卡片里留多少内容——太长手机上滑不动。
STREAM_BODY_LIMIT = 2000
# 连续这么多轮都是 idle 就收工，别一直跟着白烧配额。
WATCH_IDLE_ROUNDS = 3
# 连续这么多次读不到就收工——不限时的手工 /watch 也不会永远转。
WATCH_MAX_READ_FAILURES = 5


def build_stream_card_json(project: str, agent: str) -> dict:
    """可流式更新的卡片骨架。

    streaming_mode 只有 schema 2.0 认；打字机效果由 print_frequency_ms
    与 print_step 控制。
    """
    return {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "streaming_config": {
                "print_frequency_ms": {"default": 30},
                "print_step": {"default": 2},
            },
        },
        "header": {
            "title": {"tag": "plain_text", "content": f"▶ {project}"},
            "subtitle": {"tag": "plain_text", "content": agent},
            "template": "blue",
        },
        "body": {"elements": [{
            "tag": "markdown",
            "element_id": STREAM_ELEMENT_ID,
            "content": "读取中…",
        }]},
    }


class StreamThrottle:
    """决定这一帧要不要推。

    pane 内容每秒都在变，全推既烧飞书配额又刷得人眼晕；内容没变时
    更没必要推。sequence 必须单调递增——飞书按它排序，重复或倒退会丢帧。
    """

    def __init__(self, interval: float = STREAM_INTERVAL_S):
        self.interval = interval
        self._last_sent = 0.0
        self._last_content: str | None = None
        self._sequence = 1  # 卡片创建本身算第 1 帧

    def should_send(self, content: str, now: float) -> bool:
        if content == self._last_content:
            return False
        if now - self._last_sent < self.interval:
            return False
        self._last_sent = now
        self._last_content = content
        return True

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence


# --- 新建 agent ---

# herdr agent start 支持的类型（herdr agent start --help）。
AGENT_KINDS = frozenset({
    "pi", "claude", "codex", "gemini", "cursor", "devin", "agy", "cline",
    "omp", "mastracode", "opencode", "copilot", "kimi", "kiro", "droid",
    "amp", "grok", "hermes", "kilo", "qodercli", "maki",
})
DEFAULT_AGENT_KIND = "claude"


def is_valid_agent_kind(kind: str) -> bool:
    return (kind or "").strip().lower() in AGENT_KINDS


def parse_unbind_args(rest: str) -> tuple[bool]:
    """/unbind [drop] —— 是否连群一起解散。

    认不出来的一律当成只解绑：删群不可逆，宁可保守。
    """
    return ((rest or "").strip().lower() in ("drop", "delete", "解散", "删除"),)


def parse_new_args(rest: str) -> tuple[str | None, str]:
    """拆 /new 的参数：目标（序号或名字）+ 可选的 agent 类型。

    多余的词直接忽略——否则「/new 3 codex 顺便改一下」会把后面的话
    当成类型。
    """
    parts = (rest or "").split()
    if not parts:
        return None, DEFAULT_AGENT_KIND
    target = parts[0]
    kind = DEFAULT_AGENT_KIND
    if len(parts) > 1 and is_valid_agent_kind(parts[1]):
        kind = parts[1].strip().lower()
    return target, kind


def agent_launch_command(kind: str, cwd: str) -> str:
    """新工作区起来是个空 shell，把启动命令打进去。

    relay 的 create_workspace 写死了不带 --cwd，所以得自己 cd 过去。
    """
    if not cwd:
        return kind
    return f"cd {shlex.quote(cwd)} && {kind}"


async def create_workspace_via_relay() -> dict:
    """让 relay 建一个新工作区，返回 {workspace_id, label, pane_id}。

    relay 建的是空 shell（写死了 --focus、不带 --cwd），agent 得自己
    再启动一次。
    """
    async with ws_connect(RELAY_WS) as ws:
        await ws.send(json.dumps({"type": "create_workspace"}))
        for _ in range(6):
            raw = await asyncio.wait_for(ws.recv(), timeout=20)
            msg = json.loads(raw)
            if msg.get("type") == "error":
                raise RuntimeError(msg.get("message", "create_workspace failed"))
            if msg.get("type") == "workspace_created":
                if not msg.get("ok"):
                    raise RuntimeError("relay rejected create_workspace")
                return msg
        raise RuntimeError("relay did not acknowledge create_workspace")


# --- 自动跟随 ---

AUTOWATCH_DEFAULT_S = 120
AUTOWATCH_MIN_S = 20
AUTOWATCH_MAX_S = 600


@dataclass
class AutoWatch:
    """发完指令自动跟随的设置。"""
    enabled: bool = True
    limit: int = AUTOWATCH_DEFAULT_S


def normalize_autowatch(value: str | None, current: "AutoWatch | None" = None) -> AutoWatch:
    """解析 /autowatch 的参数：on / off / 秒数。"""
    base = current or AutoWatch()
    text = (value or "").strip().lower()
    if not text:
        return AutoWatch(True, base.limit)
    if text in ("off", "no", "0", "false"):
        # 关掉也留着时长，重新打开不用再设一遍。
        return AutoWatch(False, base.limit)
    if text in ("on", "yes", "true"):
        return AutoWatch(True, base.limit)
    if text.isdigit():
        seconds = max(AUTOWATCH_MIN_S, min(int(text), AUTOWATCH_MAX_S))
        return AutoWatch(True, seconds)
    return AutoWatch(True, AUTOWATCH_DEFAULT_S)


def watch_expired(started: float, now: float, limit: int) -> bool:
    """跟随是否超时。limit=0 表示不限时（手工 /watch 用）。"""
    if limit <= 0:
        return False
    return now - started >= limit


def is_transient_read(content: str) -> bool:
    """这次读取是不是失败了（而不是真的没内容）。

    relay 偶发超时会返回 "(no response)" / "(error reading pane: ...)"，
    照推上去卡片会突然清空，看着像内容丢了。
    """
    text = (content or "").strip()
    if not text:
        return True
    return text.startswith("(no response)") or text.startswith("(error reading pane")


STREAM_BODY_LINES = 40
# read_pane 的硬超时。agent 一多 relay 轮询会拖慢读取，实测可达 7s。
READ_TIMEOUT_S = 15


def stream_body(content: str, max_lines: int = STREAM_BODY_LINES) -> str:
    """给流式卡片裁内容：按整行留末尾若干行。

    原来按字符截末尾 N 个，结果内容一长，每多一个字所有文字就往上挪
    一格，看着像在抖；开头还常是半个单词。按行裁就是「往下追加」，
    首行只在真的滚动时才变。
    """
    text = (content or "").strip()
    if not text:
        return "(无输出)"
    lines = text.splitlines()[-max_lines:]
    body = "\n".join(lines)
    # 单行超长时兜底，避免撑爆卡片。
    if len(body) > STREAM_BODY_LIMIT:
        body = body[-STREAM_BODY_LIMIT:]
    return body


def pick_chat_for_pane(candidates: list[str], pane_id: str,
                       bindings: dict[str, str]) -> str | None:
    """同名群里挑一个。

    群名可能重复（改名后撞上），优先选已经绑了这个 pane 的那个，
    否则取第一个。
    """
    if not candidates:
        return None
    for chat_id in candidates:
        if bindings.get(chat_id) == pane_id:
            return chat_id
    return candidates[0]


def find_existing_chat(existing: dict[str, str], project: str, marker: str) -> str | None:
    """按群名找已有的群，找到就复用，不重复建。"""
    return existing.get(chat_title_for(project, marker))


def plan_chat_provisioning(agents: list[dict],
                           existing: dict[str, str]) -> list[dict]:
    """算出每个 agent 该用哪个群：已有的复用，缺的才建。

    返回 [{pane_id, project, title, chat_id}]，chat_id 为空表示要新建。
    """
    ordered = index_agents(agents)
    markers = disambiguate_suffixes(ordered)
    plan = []
    for agent in ordered:
        pane_id = str(agent.get("pane_id") or "")
        project = agent.get("project") or agent.get("agent") or "agent"
        marker = markers.get(pane_id, "")
        title = chat_title_for(project, marker)
        plan.append({
            "pane_id": pane_id,
            "project": project,
            "title": title,
            "chat_id": find_existing_chat(existing, project, marker) or "",
        })
    return plan


# --- 图片 ---

MAX_IMAGES_PER_MSG = 3
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_PATH_RE = re.compile(r"(/[^\s\'\"]+\.(?:png|jpe?g|gif|webp))", re.I)
# 各格式的文件头。光看扩展名不够——agent 输出里的 .png 未必真是图片。
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",   # png
    b"\xff\xd8\xff",          # jpeg
    b"GIF87a", b"GIF89a",      # gif
    b"RIFF",                   # webp（后 4 字节是 WEBP，这里放宽）
)


def find_image_paths(text: str) -> list[str]:
    """从输出里找图片路径。

    agent 常把截图存到本地再说一句「见 /tmp/shot.png」——手机上看不到，
    还得跑回电脑。直接传到飞书省这一趟。
    """
    seen: list[str] = []
    for match in _IMAGE_PATH_RE.findall(text or ""):
        path = match.rstrip("。，、）)]}>\'\"")
        if path not in seen:
            seen.append(path)
        if len(seen) >= MAX_IMAGES_PER_MSG:
            break
    return seen


def is_sendable_image(path: str) -> bool:
    """这个路径能不能当图片传上去。

    校验文件头而不只看扩展名：输出里出现的 .png 可能只是文档里的一段
    文字，传上去会失败或传错东西。
    """
    try:
        if os.path.getsize(path) > MAX_IMAGE_BYTES:
            return False
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    return any(head.startswith(magic) for magic in _IMAGE_MAGIC)


# --- agent 是否真的开着 ---

SHELL_ICON = "▫"


def has_live_agent(agent: dict) -> bool:
    """这个 pane 里到底有没有跑 agent。

    herdr 把没开 agent 的裸终端报成 agent="shell" + status="unknown"。
    分不清的话，发过去的文本会变成 shell 命令——实际发生过：一个「1」
    进了 zsh，报 command not found。

    shell 但状态不是 unknown 说明真在跑东西（比如手动执行的脚本），
    仍算活的。
    """
    name = (agent or {}).get("agent")
    if not name:
        return False
    if name != "shell":
        return True
    return (agent or {}).get("status") not in (None, "", "unknown")


def should_warn_shell(agent: dict) -> bool:
    return not has_live_agent(agent)


def shell_hint(project: str) -> str:
    return (f"⚠️ {project} 里没有正在运行的 agent，发过去只会被当成 shell 命令。\n"
            f"用 /new <序号> 在它的目录下开一个，或 /agents 换一个。")


def format_health(*, relay_connected: bool, relay_url: str, agents: int,
                  live_agents: int, chats: int, bindings: int, staged: int,
                  queued: int, watchers: int, seen: int, render: str,
                  autowatch: bool, autowatch_limit: int) -> str:
    """一屏看清全貌。排查问题时不用再翻日志。"""
    ok = lambda flag: "✓" if flag else "✗"
    lines = [
        f"{ok(relay_connected)} relay  {scrub(relay_url).split('?')[0]}",
        f"✓ 飞书长连接（收到本条即证明）",
        "",
        f"agent  {agents} 个，其中 {live_agents} 个有 agent 在跑",
        f"群     {chats} 个授权，{bindings} 个已绑定"
        + (f"，{staged} 个待确认" if staged else ""),
        f"队列   {queued} 条排队，{watchers} 个跟随中",
        f"去重   {seen} 条消息 id",
        "",
        f"渲染 {render}  ·  自动跟随 "
        + (f"开（{autowatch_limit}s）" if autowatch else "关"),
    ]
    return "\n".join(lines)


# --- Claude 用量 ---

def usage_report() -> str:
    """跑 herdr_usage 的统计，拿来当 /usage 的回复。

    单独一个模块而不是内联：命令行也要能直接跑，两边同一套口径。
    同目录导入，import 失败就说清楚，别让用户对着空回复猜。
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "herdr_usage.py")
    try:
        spec = importlib.util.spec_from_file_location("herdr_usage", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.format_report(mod.collect())
    except FileNotFoundError:
        return f"找不到 {path}"
    except Exception as exc:
        return f"统计用量失败: {scrub(exc)}"


# --- 审计 ---

AUDIT_DETAIL_LIMIT = 200
_AUDIT_ICONS = {
    "send": "→", "approve": "✓", "trust": "🔓", "interrupt": "⛔",
    "new": "✚", "spaces": "🏠", "unbind": "✂", "drop": "🗑",
}


def format_audit(action: str, project: str, pane_id: str, detail: str) -> str:
    """一行审计记录：谁被做了什么。

    指令内容可能带密钥（比如「用 xxx 登录」），群里人多，必须脱敏。
    """
    icon = _AUDIT_ICONS.get(action, "·")
    text = scrub(detail or "").strip().replace("\n", " ⏎ ")
    if len(text) > AUDIT_DETAIL_LIMIT:
        text = text[:AUDIT_DETAIL_LIMIT] + "…"
    tail = f"  {text}" if text else ""
    return f"{icon} {action}  {project} ({pane_id}){tail}"


def audit_enabled(value: str | None) -> bool:
    """审计回执开关。默认开：留痕的成本只有一行文本。"""
    return str(value if value is not None else "").strip().lower() not in (
        "off", "0", "false", "no")


# --- 选择器检测 ---

# 「 ❯ 1. Yes 」「2. trust, always allow」——前面可能有箭头或缩进。
_OPTION_RE = re.compile(r"^\s*[❯>»\*]?\s*(\d{1,2})[.)．]\s+(\S.*)$")
# 从末尾往上找选择器，最多翻这么多行。够深以容纳多组多选项的长选择器，
# 真正的边界靠「连续正文块」判定，不靠行数。
_OPTION_SCAN_LINES = 200
# 选项后面允许跟几行缩进的描述文字——AskUserQuestion 每个选项都带一行说明，
# 一旦把它当成「选择器后面的正文」，整个选择器就被丢掉，卡片一个按钮都没有。
_OPTION_TRAILING_PROSE = 2
_MAX_OPTIONS = 9


def detect_option_groups(text: str) -> list[dict]:
    """认出所有「正在等你选」的组。

    AskUserQuestion 一次能问好几组，每组都从 1 重新编号。只认第一组的话
    卡片显示的是第一组，而 agent 可能正等第二组的答案——点下去就答错了。

    从末尾反向定位选择器区间，而不是截固定行数的窗口：窗口切在某组中间时，
    那组编号不从 1 起，会被连续性校验整组丢掉，卡片上就少选项。

    每组返回 {"question": 提问行, "options": [选项...]}。
    """
    lines = (text or "").splitlines()
    if not lines:
        return []

    tail = lines[-_OPTION_SCAN_LINES:]
    last_option_at = _last_option_line(tail)
    if last_option_at < 0:
        return []
    # 选项要贴着输出末尾。允许后面跟几行缩进说明（每个选项自带一行描述），
    # 但跟着大段正文的是散文里的编号列表，不是选择器。
    if not _is_selector_tail(tail[last_option_at + 1:]):
        return []

    start = _selector_start(tail, last_option_at)
    return _parse_groups(tail, start, last_option_at)


def _last_option_line(lines: list[str]) -> int:
    """最后一个选项行的下标，没有则 -1。"""
    for index in range(len(lines) - 1, -1, -1):
        if _OPTION_RE.match(lines[index]):
            return index
    return -1


def _is_selector_tail(rest: list[str]) -> bool:
    """最后一个选项之后剩下的内容，还允许把这当成活着的选择器吗。

    只放过缩进行——那是选项自己的描述（AskUserQuestion 每项都带一行说明，
    误判成正文的话整个选择器被丢弃，卡片一个按钮都没有）。顶格正文说明
    选择器已经翻过去了，是历史记录里的旧选择器或散文里的编号列表。
    """
    for line in rest:
        stripped = line.strip()
        if not stripped or _is_tui_chrome(stripped):
            continue
        if line[:1] in (" ", "\t"):  # 缩进 = 选项的描述行
            continue
        return False
    return True


# TUI 边框与输入框提示：不是 agent 的正文输出。
# 圆角、直角、粗线四套框线都要收全——漏一个字符（比如 ╮），底部输入框就会
# 被当成正文，整个选择器被判定为「已翻过去」，卡片一个按钮都不剩。
_TUI_CHROME_RE = re.compile(
    r"^[\s│┃|>❯╭╮╰╯─━┌┐└┘┏┓┗┛├┤┬┴┼╌╍┄┅·]*$")


def _is_tui_chrome(stripped: str) -> bool:
    return bool(_TUI_CHROME_RE.match(stripped))


def _selector_start(lines: list[str], last_option_at: int) -> int:
    """选择器区间的起点：从最后一个选项往上，走到第一组的「1.」为止。

    途中遇到连续正文块就停——那是选择器上方的 agent 输出。
    """
    start = last_option_at
    prose_run = 0
    for index in range(last_option_at, -1, -1):
        line = lines[index]
        match = _OPTION_RE.match(line)
        if match:
            start = index
            prose_run = 0
            continue
        stripped = line.strip()
        if not stripped or _is_tui_chrome(stripped):
            prose_run = 0
            continue
        if line[:1] in (" ", "\t"):  # 选项的描述行，不是边界
            continue
        # 顶格正文：一行可能是某组的提问，连续多行就是选择器上方的输出了。
        prose_run += 1
        if prose_run > _OPTION_TRAILING_PROSE:
            break
    return start


def _parse_groups(lines: list[str], start: int, end: int) -> list[dict]:
    """把选择器区间切成组。编号回到 1 就是新的一组。"""
    groups: list[dict] = []
    current: list[tuple[int, str]] = []
    question = ""
    # 提问行是选择器区间上方最近的一行正文。
    last_prose = _preceding_prose(lines, start)

    def flush() -> None:
        nonlocal current, question
        if len(current) >= 2:
            numbers = [n for n, _ in current]
            if numbers == list(range(1, len(numbers) + 1)):
                groups.append({
                    "question": question.strip(),
                    "options": [t for _, t in current][:_MAX_OPTIONS],
                })
        current = []
        question = ""

    for index in range(start, end + 1):
        line = lines[index]
        match = _OPTION_RE.match(line)
        if match:
            number = int(match.group(1))
            if number == 1 and current:
                flush()
            if not current:
                question = last_prose
            current.append((number, match.group(2).strip()))
            continue
        stripped = line.strip()
        # 顶格正文才可能是下一组的提问；选项的缩进描述行不算。
        if stripped and not _is_tui_chrome(stripped) and line[:1] not in (" ", "\t"):
            last_prose = stripped
    flush()
    return groups


def _preceding_prose(lines: list[str], start: int) -> str:
    """选择器上方最近的一行正文，作为第一组的提问。"""
    for index in range(start - 1, -1, -1):
        stripped = lines[index].strip()
        if stripped and not _is_tui_chrome(stripped):
            return stripped
    return ""


def detect_pane_options(text: str) -> list[str] | None:
    """兼容旧调用：返回最后一组选项（agent 当前在等的那一组）。"""
    groups = detect_option_groups(text)
    return groups[-1]["options"][:_MAX_OPTIONS] if groups else None


def looks_like_option_press(text: str) -> bool:
    """用户只打了一个数字——在选择器面前，这是按键而非要发的文本。"""
    stripped = (text or "").strip()
    return len(stripped) == 1 and stripped in "123456789"


# --- 命令解析与渲染 ---

# 命令表 —— 单一数据源。
# 加命令时改这里就够了：COMMANDS 从它派生，/help 也从它渲染，
# 有测试盯着两边一致，不会出现「加了命令但帮助里没有」。
COMMAND_HELP = [
    {"group": "看", "name": "agents", "args": "", "desc": "列出全部 agent"},
    {"group": "看", "name": "read", "args": "<序号>", "desc": "看它最近在干什么"},
    {"group": "看", "name": "watch", "args": "[序号]", "desc": "跟随输出，stop 停止"},
    {"group": "看", "name": "status", "args": "", "desc": "连接状态"},
    {"group": "看", "name": "digest", "args": "", "desc": "今日活动统计"},
    {"group": "看", "name": "usage", "args": "", "desc": "Claude 用量（5h 窗 + 本周）"},

    {"group": "干", "name": "send", "args": "<序号> <内容>", "desc": "发指令（也可直接打字）"},
    {"group": "干", "name": "trust", "args": "<序号>", "desc": "批准并总是允许"},
    {"group": "干", "name": "interrupt", "args": "<序号>", "desc": "中断（Ctrl+C）"},
    {"group": "干", "name": "new", "args": "<序号> [类型]", "desc": "在同目录新开一个 agent"},

    {"group": "设", "name": "spaces", "args": "[数量|dry]", "desc": "一键一 agent 一群"},
    {"group": "设", "name": "autowatch", "args": "[on|off|秒]", "desc": "发完是否自动跟随"},
    {"group": "设", "name": "render", "args": "[card|text]", "desc": "输出样式"},
    {"group": "设", "name": "unbind", "args": "[drop]", "desc": "解绑本群，drop 连群解散"},
    {"group": "设", "name": "health", "args": "", "desc": "自检：连接、群、队列"},
    {"group": "设", "name": "help", "args": "", "desc": "本帮助"},

    # 别名，不在帮助里单列
    {"group": "", "name": "start", "args": "", "desc": "同 /help"},
    {"group": "", "name": "reply", "args": "<序号>", "desc": "同 /read"},
]

COMMANDS = {entry["name"] for entry in COMMAND_HELP}


def format_help() -> str:
    """渲染命令帮助。手机上看的，务必短。"""
    lines = []
    current = None
    for entry in COMMAND_HELP:
        group = entry["group"]
        if not group:      # 别名不单列
            continue
        if group != current:
            lines.append(f"【{group}】")
            current = group
        args = f" {entry['args']}" if entry["args"] else ""
        lines.append(f"/{entry['name']}{args} — {entry['desc']}")
    lines.append("")
    lines.append("选 agent 用序号（/agents 里的编号）；读过之后直接打字即可继续指挥。")
    return "\n".join(lines)


def parse_command(text: str) -> tuple[str | None, str]:
    """拆出命令名与参数；不是命令就当自由文本。"""
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None, stripped
    head, _, rest = stripped[1:].partition(" ")
    # 群里命令可能写成 /status@demo
    head = head.split("@", 1)[0].lower()
    if head not in COMMANDS:
        return None, stripped
    return head, rest.strip()


def match_agent(agents: list[dict], query: str) -> dict | None:
    """按序号或名字选 agent。

    序号优先：/agents 输出里的编号就是用户眼前看到的东西，agent 一多，
    打项目名比打数字麻烦得多。序号对应 sorted_agents 的顺序，与列表一致。
    """
    needle = (query or "").strip()
    if not needle:
        return None

    if needle.isdigit():
        ordered = index_agents(agents)
        index = int(needle) - 1  # 展示用 1-based
        if 0 <= index < len(ordered):
            return ordered[index]
        return None

    needle = needle.lower()
    for agent in agents:
        project = (agent.get("project") or "").lower()
        name = (agent.get("agent") or "").lower()
        if needle in project or needle in name:
            return agent
    return None


def _status_counts(agents: list[dict]) -> tuple[int, int, int, int]:
    blocked = sum(a.get("status") == "blocked" for a in agents)
    working = sum(a.get("status") == "working" for a in agents)
    done = sum(a.get("status") == "done" for a in agents)
    return blocked, working, done, len(agents) - blocked - working - done


def status_summary(agents: list[dict]) -> str:
    blocked, working, done, idle = _status_counts(agents)
    return (f"Agents: {len(agents)} "
            f"({blocked} blocked, {working} working, {done} done, {idle} idle)")



def disambiguate_suffixes(agents: list[dict]) -> dict[str, str]:
    """给重名的 agent 算一个区分后缀；不重名的返回空串。

    同一个项目开两个 agent 时，列表上两行一模一样，根本分不清谁是谁。
    优先用目录尾巴（人能看懂），目录也一样时退回 workspace_id。
    """
    keys = [f"{a.get('project')}|{a.get('agent')}" for a in agents]
    counts = Counter(keys)

    suffixes: dict[str, str] = {}
    for agent, key in zip(agents, keys):
        pane_id = str(agent.get("pane_id") or "")
        if counts[key] < 2:
            suffixes[pane_id] = ""
            continue
        same = [a for a, k in zip(agents, keys) if k == key]
        # 同名的里面，父目录能区分开就用它——比 workspace id 好懂。
        parents = [os.path.basename(os.path.dirname(a.get("cwd") or "")) for a in same]
        if len(set(parents)) == len(same) and all(parents):
            suffixes[pane_id] = f" [{os.path.basename(os.path.dirname(agent.get('cwd') or ''))}]"
        else:
            # 父目录也一样（或缺失）：只剩 workspace id 能区分。
            wid = agent.get("workspace_id") or pane_id.split(":")[0]
            suffixes[pane_id] = f" [{wid}]"
    return suffixes


def format_agent_list(agents: list[dict]) -> str:
    """按状态分组列出 agent，带序号。

    序号取自 sorted_agents 的位置，与 match_agent 的解析一一对应——
    两者若不一致，/read 2 就会选错人。
    """
    if not agents:
        return "No agents connected."
    # 按序号顺排。曾经按状态分组，结果序号变成 8/4/13/2 的乱序，
    # 扫一眼根本找不到目标——状态改用行首图标表示。
    ordered = index_agents(agents)
    extra = disambiguate_suffixes(ordered)

    lines = []
    for index, agent in enumerate(ordered, start=1):
        status = agent.get("status") or "unknown"
        icon = (_STATUS_ICONS.get(status, "○") if has_live_agent(agent)
                else SHELL_ICON)
        host = agent.get("host", "local")
        suffix = f" @{host}" if host != "local" else ""
        mark = extra.get(str(agent.get("pane_id")), "")
        lines.append(
            f"{icon} {index}. {agent.get('project')} ({agent.get('agent')}){mark}{suffix}")
    lines.append("")
    lines.append(f"⏸ 待批  ▶ 在跑  ✅ 完成  ○ 空闲  {SHELL_ICON} 无 agent"
                 "   ·   用序号，例如 /read 1")
    return "\n".join(lines)


def format_digest(stats: dict) -> str:
    if not stats:
        return "No activity recorded yet today."
    lines = ["Today's activity:", ""]
    for entry in sorted(stats.values(), key=lambda s: s.get("working_mins", 0), reverse=True):
        mins = entry.get("working_mins", 0)
        span = f"{mins}m" if mins < 60 else f"{mins // 60}h{mins % 60}m"
        blocked = entry.get("blocked_count") or 0
        suffix = f", blocked {blocked}x" if blocked else ""
        lines.append(f"  {entry.get('project')} ({entry.get('agent')}): {span} working{suffix}")
    return "\n".join(lines)


# --- 飞书 API ---

class LarkAPI:
    """薄薄一层飞书发送封装，带富文本降级。"""

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu"):
        import lark_oapi as lark
        self._lark = lark
        base = lark.FEISHU_DOMAIN if domain != "lark" else lark.LARK_DOMAIN
        self.client = (lark.Client.builder()
                       .app_id(app_id)
                       .app_secret(app_secret)
                       .domain(base)
                       .build())
        self.bot_open_id = ""

    def fetch_bot_open_id(self) -> str:
        """SDK 没封装 bot/v3/info，直接发原始请求。

        没有 bot_open_id 就无从判断「群里有没有 @ 我」和「这条是不是我自己发的」。
        """
        lark = self._lark
        request = (lark.BaseRequest.builder()
                   .http_method(lark.HttpMethod.GET)
                   .uri("/open-apis/bot/v3/info")
                   .token_types({lark.AccessTokenType.TENANT})
                   .build())
        response = self.client.request(request)
        if not response.success():
            raise RuntimeError(
                "飞书机器人初始化失败，请检查 HERDR_LARK_APP_ID / HERDR_LARK_APP_SECRET，"
                f"以及机器人能力是否已启用：{response.msg}"
            )
        payload = json.loads(response.raw.content)
        self.bot_open_id = ((payload.get("bot") or {}).get("open_id")
                            or ((payload.get("data") or {}).get("bot") or {}).get("open_id", ""))
        return self.bot_open_id

    def _send(self, chat_id: str, msg_type: str, content: str) -> str:
        lark = self._lark
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        request = (CreateMessageRequest.builder()
                   .receive_id_type("chat_id")
                   .request_body(CreateMessageRequestBody.builder()
                                 .receive_id(chat_id)
                                 .msg_type(msg_type)
                                 .content(content)
                                 .build())
                   .build())
        response = self.client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"飞书发送失败 ({msg_type}): {response.msg}")
        return getattr(response.data, "message_id", "") or ""

    def list_chats(self) -> dict[str, str]:
        """机器人所在的全部群：{群名: chat_id}。"""
        from lark_oapi.api.im.v1 import ListChatRequest
        out: dict[str, str] = {}
        page_token = None
        for _ in range(10):  # 上限保护，别翻页翻到天荒地老
            builder = ListChatRequest.builder().page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            response = self.client.im.v1.chat.list(builder.build())
            if not response.success():
                raise RuntimeError(f"列群失败: {response.msg}")
            for item in (getattr(response.data, "items", None) or []):
                name = getattr(item, "name", "") or ""
                if name:
                    out[name] = getattr(item, "chat_id", "")
            page_token = getattr(response.data, "page_token", None)
            if not getattr(response.data, "has_more", False):
                break
        return out

    def delete_chat(self, chat_id: str) -> None:
        from lark_oapi.api.im.v1 import DeleteChatRequest
        response = self.client.im.v1.chat.delete(
            DeleteChatRequest.builder().chat_id(chat_id).build())
        if not response.success():
            raise RuntimeError(f"解散群失败: {response.msg}")

    def create_chat(self, name: str, user_open_ids: list[str]) -> str:
        from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody
        request = (CreateChatRequest.builder()
                   .user_id_type("open_id")
                   .request_body(CreateChatRequestBody.builder()
                                 .name(name)
                                 .user_id_list(user_open_ids)
                                 .build())
                   .build())
        response = self.client.im.v1.chat.create(request)
        if not response.success():
            raise RuntimeError(f"建群失败: {response.msg}")
        return getattr(response.data, "chat_id", "")

    def chat_member_count(self, chat_id: str) -> int:
        from lark_oapi.api.im.v1 import GetChatMembersRequest
        request = (GetChatMembersRequest.builder()
                   .chat_id(chat_id)
                   .page_size(20)
                   .build())
        response = self.client.im.v1.chat_members.get(request)
        if not response.success():
            raise RuntimeError(f"读取群成员失败: {response.msg}")
        return len(getattr(response.data, "items", None) or [])

    def set_chat_name(self, chat_id: str, name: str) -> None:
        """改群名。飞书的群公告是 docx 类型、API 改不了，群名反而更醒目。"""
        from lark_oapi.api.im.v1 import UpdateChatRequest, UpdateChatRequestBody
        request = (UpdateChatRequest.builder()
                   .chat_id(chat_id)
                   .request_body(UpdateChatRequestBody.builder().name(name).build())
                   .build())
        response = self.client.im.v1.chat.update(request)
        if not response.success():
            raise RuntimeError(f"改群名失败: {response.msg}")

    def create_stream_card(self, card_json: dict) -> str:
        """建一张可流式更新的卡片实体，返回 card_id。"""
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody
        request = (CreateCardRequest.builder()
                   .request_body(CreateCardRequestBody.builder()
                                 .type("card_json")
                                 .data(json.dumps(card_json))
                                 .build())
                   .build())
        response = self.client.cardkit.v1.card.create(request)
        if not response.success():
            raise RuntimeError(f"创建流式卡片失败: {response.msg}")
        return response.data.card_id

    def send_card_entity(self, chat_id: str, card_id: str) -> str:
        """把卡片实体发到会话。之后所有更新都通过 card_id 打进这条消息。"""
        return self._send(chat_id, "interactive",
                          json.dumps({"type": "card", "data": {"card_id": card_id}}))

    def stream_content(self, card_id: str, content: str, sequence: int) -> None:
        """往卡片里流式写内容。sequence 必须递增，否则飞书丢帧。"""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )
        request = (ContentCardElementRequest.builder()
                   .card_id(card_id)
                   .element_id(STREAM_ELEMENT_ID)
                   .request_body(ContentCardElementRequestBody.builder()
                                 .content(content)
                                 .sequence(sequence)
                                 .build())
                   .build())
        response = self.client.cardkit.v1.card_element.content(request)
        if not response.success():
            raise RuntimeError(f"流式更新失败: {response.msg}")

    def send_image(self, chat_id: str, path: str) -> str:
        """把本地图片传上去再发到会话。"""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
        with open(path, "rb") as fh:
            request = (CreateImageRequest.builder()
                       .request_body(CreateImageRequestBody.builder()
                                     .image_type("message")
                                     .image(fh)
                                     .build())
                       .build())
            response = self.client.im.v1.image.create(request)
        if not response.success():
            raise RuntimeError(f"图片上传失败: {response.msg}")
        image_key = getattr(response.data, "image_key", "")
        return self._send(chat_id, "image", json.dumps({"image_key": image_key}))

    def send_text(self, chat_id: str, text: str) -> str:
        return self._send(chat_id, "text", json.dumps({"text": text}))

    def send_card(self, chat_id: str, card: dict) -> str:
        """发交互卡片，失败降级成纯文本——飞书对卡片结构挑剔，不能因此丢消息。"""
        try:
            return self._send(chat_id, "interactive", json.dumps(card))
        except Exception as exc:
            log.warning("Card send failed, falling back to text: %s", scrub(exc))
            title = ((card.get("header") or {}).get("title") or {}).get("content", "")
            return self.send_text(chat_id, title or "(card render failed)")


# --- 应用主体 ---

class LarkBot:
    """把飞书事件、relay 状态和命令处理串起来。

    线程模型：飞书 SDK 的 ws.Client.start() 是阻塞的同步调用，跑在独立线程；
    relay 连接是 asyncio，跑在主线程的事件循环。事件处理器必须在 3 秒内返回
    （飞书硬性要求），所以真正干活的协程一律用 run_coroutine_threadsafe 投到
    主循环，结果靠后续新消息回传，而不是靠处理器的返回值。
    """

    def __init__(self, api: "LarkAPI", chat_id: str, loop):
        self.api = api
        # 支持多群：每个群绑一个 agent，互不干扰。
        # 环境变量是种子，/spaces 建的群会落盘，重启后不丢。
        env_chats = parse_chat_ids(chat_id) if isinstance(chat_id, str) else set(chat_id)
        self.chat_store = ChatIdStore()
        self.chat_store.seed(env_chats)
        self.chat_ids = self.chat_store.all() or env_chats
        # 单值形式仍保留，供只需要「默认群」的地方用。
        self.chat_id = next(iter(sorted(self.chat_ids)), "")
        self.loop = loop
        self.agents: list[dict] = []
        self.relay_connected = False
        self.pending: OrderedDict = OrderedDict()
        self.approval_tokens: dict[str, str] = {}
        self.prev_statuses: dict[str, str] = {}
        self.daily_stats: dict[str, dict] = {}
        self.seen = SeenStore()
        # 只有你和机器人的群，不该逼你每句话都 @ 一下
        self._solo_groups: dict[str, bool] = {}
        # 每个会话「当前正在跟哪个 agent 说话」。读完就设上，
        # 之后直接发文本即可继续指挥，不用每句都带序号。
        # 落盘：重启后不用重新 /read 一遍。
        self.bindings = BindingStore()
        self._active: dict[str, str] = self.bindings.as_dict()
        # /spaces 批量建群时的预绑定：进群确认后才生效。
        # 直接进 _active 的话，用户在陌生群里随便打个字就发进了
        # 一个他没选过的 agent 的终端。
        self._staged: dict[str, str] = {}
        self._pruned = False
        # 渲染模式可在飞书里用 /render 随时切，不必重启服务。
        self.render_mode = normalize_render_mode(RENDER_MODE)
        # 同群串行：连发两条时 send_text 的粘贴与回车不能交错。
        self.queue = ChatQueue()
        # chat_id -> 正在跑的 watch 任务。一个群同时只跟一个 agent。
        self._watchers: dict[str, asyncio.Task] = {}
        # 发完指令自动跟随，省得每次手打 /watch。
        self.autowatch = normalize_autowatch(AUTOWATCH_ENV)
        # 审计回执：写操作在发起它的那个群里留一行痕迹。
        self.audit_on = audit_enabled(AUDIT_ENV)

    # --- 出站 ---

    def reply_text(self, chat_id: str, text: str) -> str:
        try:
            return self.api.send_text(chat_id, text)
        except Exception as exc:
            log.warning("send_text failed: %s", scrub(exc))
            return ""

    def reply_card(self, chat_id: str, card: dict) -> str:
        try:
            return self.api.send_card(chat_id, card)
        except Exception as exc:
            log.warning("send_card failed: %s", scrub(exc))
            return ""

    def remember(self, chat_id: str, message_id: str, pane_id: str) -> None:
        if message_id:
            register_pending(self.pending, chat_id, message_id, pane_id)

    def prune_stale_bindings(self, agents: list[dict]) -> None:
        """清掉指向已消失 agent 的绑定。只在首帧做一次，避免反复写盘。"""
        if self._pruned or not agents:
            return
        self._pruned = True
        panes = {str(a.get("pane_id")) for a in agents if a.get("pane_id")}
        cleaned = prune_bindings(self._active, self.chat_ids, panes)
        if cleaned != self._active:
            dropped = set(self._active) - set(cleaned)
            log.info("Pruned %d stale binding(s): %s", len(dropped), sorted(dropped))
            self._active = cleaned
            self.bindings.replace(cleaned)

    def stage_binding(self, chat_id: str, pane_id: str) -> None:
        """预绑定，等用户在群里确认后才生效。"""
        self._staged[str(chat_id)] = pane_id

    def staged_pane(self, chat_id: str) -> str | None:
        return self._staged.get(str(chat_id))

    def confirm_staged(self, chat_id: str) -> str | None:
        """把预绑定转正。"""
        pane_id = self._staged.pop(str(chat_id), None)
        if pane_id:
            self._active[str(chat_id)] = pane_id
            self.bindings.set(str(chat_id), pane_id)
        return pane_id

    def set_active(self, chat_id: str, pane_id: str, project: str | None = None) -> None:
        """把这个群绑到某个 agent 上，并把群名改成它。

        群名失败不影响绑定——改名只是让人看得清楚，不是功能前提。
        """
        chat_id = str(chat_id)
        changed = self._active.get(chat_id) != pane_id
        self._active[chat_id] = pane_id
        if changed:
            self.bindings.set(chat_id, pane_id)
        if changed and project:
            try:
                # 重名的 agent 要带区分标记，否则两个群名一模一样。
                marker = disambiguate_suffixes(
                    index_agents(self.agents)).get(str(pane_id), "")
                self.api.set_chat_name(chat_id, chat_title_for(project, marker))
            except Exception as exc:
                log.warning("rename chat failed: %s", scrub(exc))

    def active_pane(self, chat_id: str) -> str | None:
        return self._active.get(str(chat_id))

    def _is_solo_group(self, chat_id: str) -> bool:
        """成员只有一个人的群，交互上等同单聊。结果缓存，避免每条消息都查。"""
        if chat_id in self._solo_groups:
            return self._solo_groups[chat_id]
        solo = False
        try:
            solo = self.api.chat_member_count(chat_id) <= 1
        except Exception as exc:
            log.warning("chat member lookup failed: %s", scrub(exc))
        self._solo_groups[chat_id] = solo
        return solo

    # --- 入站（飞书线程，必须立刻返回）---

    def on_message_event(self, event: dict) -> None:
        ctx = parse_message_event(event, self.api.bot_open_id)
        if ctx.chat_type == "group" and not ctx.mentioned_bot:
            ctx.solo_group = self._is_solo_group(ctx.chat_id)
        log.info("inbound message chat=%s type=%s mention=%s text=%r",
                 ctx.chat_id, ctx.chat_type, ctx.mentioned_bot, ctx.text[:80])
        if not ctx.message_id or not self.seen.add(ctx.message_id):
            log.info("  dropped: duplicate or missing message_id")
            return  # 飞书会重推，去重后才处理
        if not should_handle(ctx, self.api.bot_open_id, self.chat_ids):
            log.info("  dropped: gate rejected (authorized=%s)", self.chat_ids or "any")
            return
        self._dispatch(ctx)

    def on_card_action(self, event: dict) -> None:
        ctx = parse_card_action(event)
        log.info("inbound card action chat=%s value=%s",
                 ctx.chat_id if ctx else None, ctx.action if ctx else None)
        if ctx is None:
            log.info("  dropped: no action value")
            return
        if not should_handle(ctx, self.api.bot_open_id, self.chat_ids):
            log.info("  dropped: gate rejected")
            return
        self._dispatch(ctx)

    def _dispatch(self, ctx: MessageContext) -> None:
        """把实际处理丢进 asyncio 循环，飞书那条线程立刻脱身。

        经队列走，保证同一个群的消息串行：并发跑的话 send_text 的
        「粘贴 + 回车」会交错，两条消息糊成一条。
        """
        self.loop.call_soon_threadsafe(
            self.queue.submit, ctx.chat_id, lambda: self._handle(ctx))

    # --- 处理（asyncio 主循环）---

    async def _handle(self, ctx: MessageContext) -> None:
        try:
            if ctx.action is not None:
                await self._handle_action(ctx)
            else:
                await self._handle_text(ctx)
        except Exception as exc:
            log.exception("handler failed")
            self.reply_text(ctx.chat_id, f"Failed: {scrub(exc)}")

    async def _handle_text(self, ctx: MessageContext) -> None:
        command, rest = parse_command(ctx.text)

        if command is None:
            await self._handle_free_text(ctx, rest)
            return
        if command in ("start", "help"):
            self.reply_text(
                ctx.chat_id, f"{self._dashboard_text()}\n\n{format_help()}")
            return
        if command == "agents":
            self.reply_text(ctx.chat_id, format_agent_list(self.agents))
            return
        if command == "status":
            state = "Connected" if self.relay_connected else "Disconnected"
            self.reply_text(ctx.chat_id, f"Relay: {RELAY_WS_SAFE}\nStatus: {state}\n"
                                         f"{status_summary(self.agents)}")
            return
        if command == "digest":
            self.reply_text(ctx.chat_id, format_digest(self.daily_stats))
            return
        if command == "usage":
            # 扫 ~/.claude 下的日志要读几十 MB，别卡住事件循环。
            self.reply_text(ctx.chat_id, await asyncio.to_thread(usage_report))
            return
        if command == "health":
            self.reply_text(ctx.chat_id, format_health(
                relay_connected=self.relay_connected,
                relay_url=RELAY_WS_SAFE,
                agents=len(self.agents),
                live_agents=sum(1 for a in self.agents if has_live_agent(a)),
                chats=len(self.chat_ids),
                bindings=len(self._active),
                staged=len(self._staged),
                queued=self.queue.depth(),
                watchers=len(self._watchers),
                seen=len(self.seen),
                render=self.render_mode,
                autowatch=self.autowatch.enabled,
                autowatch_limit=self.autowatch.limit,
            ))
            return
        if command == "unbind":
            await self._handle_unbind(ctx, rest)
            return
        if command == "spaces":
            await self._handle_spaces(ctx, rest)
            return
        if command == "autowatch":
            if not rest:
                state = "开" if self.autowatch.enabled else "关"
                self.reply_text(
                    ctx.chat_id,
                    f"自动跟随: {state}，最长 {self.autowatch.limit} 秒\n"
                    "  /autowatch off   关闭\n"
                    "  /autowatch on    打开\n"
                    "  /autowatch 180   打开并设为 180 秒"
                    f"（{AUTOWATCH_MIN_S}–{AUTOWATCH_MAX_S}）",
                )
                return
            self.autowatch = normalize_autowatch(rest, self.autowatch)
            state = "开" if self.autowatch.enabled else "关"
            self.reply_text(
                ctx.chat_id, f"自动跟随已设为: {state}，最长 {self.autowatch.limit} 秒")
            return
        if command == "new":
            await self._handle_new(ctx, rest)
            return
        if command == "watch":
            await self._handle_watch(ctx, rest)
            return
        if command == "render":
            if not rest:
                self.reply_text(
                    ctx.chat_id,
                    f"当前渲染模式: {self.render_mode}\n"
                    "  /render card — 彩色卡片 + 等宽代码块（默认）\n"
                    "  /render text — 纯文本，最省、最不挑客户端",
                )
                return
            mode = normalize_render_mode(rest)
            if mode != rest.strip().lower():
                self.reply_text(ctx.chat_id, f"没有 '{rest}' 这个模式，可选: card / text")
                return
            self.render_mode = mode
            self.reply_text(ctx.chat_id, f"渲染模式已切到 {mode}")
            return

        if not self.agents:
            self.reply_text(ctx.chat_id, "No agents connected.")
            return

        if command in ("read", "reply", "send", "trust", "interrupt"):
            await self._handle_agent_command(ctx, command, rest)

    async def _handle_agent_command(self, ctx: MessageContext, command: str, rest: str) -> None:
        pick_action = {
            "read": "read", "reply": "select_reply", "send": "select_send",
            "trust": "trust", "interrupt": "interrupt",
        }[command]

        if not rest:
            # 这个群是 /spaces 预绑定的：直接用它，省得再选一遍。
            staged = self.confirm_staged(ctx.chat_id)
            if staged:
                agent = find_agent(self.agents, staged)
                if agent:
                    await self._handle_agent_command(
                        ctx, command, str(index_agents(self.agents).index(agent) + 1))
                    return
            candidates = agents_for_action(pick_action, self.agents)
            if not candidates:
                self.reply_text(ctx.chat_id, f"No eligible agents for /{command}.")
                return
            self.reply_card(ctx.chat_id, build_agent_picker_card(
                pick_action, candidates, title=f"/{command} — pick an agent"))
            return

        # /send 的第一个词是项目名，其余是要发的内容
        query, _, payload = rest.partition(" ") if command == "send" else (rest, "", "")
        agent = match_agent(self.agents, query)
        if agent is None:
            self.reply_text(ctx.chat_id, f"No agent matching '{query}'.")
            return

        pane_id = agent["pane_id"]
        if command == "read":
            await self._send_pane_content(ctx.chat_id, agent)
        elif command == "reply":
            await self._send_pane_content(ctx.chat_id, agent, prompt=True)
        elif command == "trust":
            await send_to_relay(pane_id, "trust, always allow")
            self.audit(ctx.chat_id, "trust", agent)
            self.reply_text(ctx.chat_id, f"Trusted {agent.get('project')} (always allow)")
        elif command == "interrupt":
            await self._interrupt(ctx.chat_id, agent)
        elif command == "send":
            if not payload.strip():
                self._prompt_for_reply(ctx.chat_id, agent)
                return
            await send_text_to_relay(pane_id, payload.strip())
            self.audit(ctx.chat_id, "send", agent, payload.strip())
            self.set_active(ctx.chat_id, pane_id, agent.get("project"))
            self.reply_text(ctx.chat_id, f"→ 已发给 {agent.get('project')}")
            self._maybe_autowatch(ctx.chat_id, pane_id, agent.get("project") or "")

    def _start_watch(self, chat_id: str, pane_id: str, project: str,
                     agent_name: str = "", limit: int = 0) -> None:
        """起一个跟随任务，先停掉这个群原有的。"""
        old = self._watchers.pop(chat_id, None)
        if old:
            old.cancel()
        self._watchers[chat_id] = asyncio.ensure_future(
            self._watch_loop(chat_id, pane_id, project, agent_name, limit))

    def _maybe_autowatch(self, chat_id: str, pane_id: str, project: str) -> None:
        """发完指令自动跟随——手工再打一次 /watch 太啰嗦。

        跟到 agent 停下来为止（连续几轮 idle），或到时限为止；
        两个条件哪个先到都收工。
        """
        if not self.autowatch.enabled:
            return
        agent = find_agent(self.agents, pane_id) or {}
        self._start_watch(chat_id, pane_id, project,
                          agent.get("agent", ""), self.autowatch.limit)

    async def _handle_unbind(self, ctx: MessageContext, rest: str) -> None:
        """/unbind [drop] —— 解绑本群；drop 连群一起解散。"""
        chat_id = ctx.chat_id
        (drop,) = parse_unbind_args(rest)

        watcher = self._watchers.pop(chat_id, None)
        if watcher:
            watcher.cancel()
        self._active.pop(chat_id, None)
        self.bindings.remove(chat_id)

        if not drop:
            try:
                self.api.set_chat_name(chat_id, CHAT_TITLE_PREFIX.rstrip(" ·· "))
            except Exception as exc:
                log.warning("reset chat name failed: %s", scrub(exc))
            self.reply_text(chat_id, "已解绑。用 /agents 重新选一个。")
            return

        # 先说一声再解散——群没了消息也就看不到了。
        self.reply_text(chat_id, "正在解散本群…")
        self.chat_ids.discard(chat_id)
        self.chat_store.remove(chat_id)
        try:
            self.api.delete_chat(chat_id)
        except Exception as exc:
            log.warning("delete chat failed: %s", scrub(exc))
            self.reply_text(chat_id, f"解散失败: {scrub(exc)}")

    async def _handle_spaces(self, ctx: MessageContext, rest: str) -> None:
        """/spaces —— 给每个 agent 拉一个群，已有的复用。

        agent 一多，挤在一个群里根本分不清；一 agent 一群才好用。
        """
        if not self.agents:
            self.reply_text(ctx.chat_id, "还没有 agent。")
            return

        arg = rest.strip().lower()
        dry_run = arg in ("dry", "preview", "看看")
        # /spaces 3 —— 先建几个试试，别一次刷出十几个群。
        cap = int(arg) if arg.isdigit() else 0
        try:
            existing = self.api.list_chats()
        except Exception as exc:
            self.reply_text(ctx.chat_id, f"列群失败: {scrub(exc)}")
            return

        plan = plan_chat_provisioning(self.agents, existing)
        reuse = [p for p in plan if p["chat_id"]]
        create = [p for p in plan if not p["chat_id"]]

        if dry_run:
            lines = [f"共 {len(plan)} 个 agent：复用 {len(reuse)}，新建 {len(create)}", ""]
            lines += [f"  复用  {p['title']}" for p in reuse[:10]]
            lines += [f"  新建  {p['title']}" for p in create[:10]]
            self.reply_text(ctx.chat_id, "\n".join(lines))
            return

        if not create:
            self.reply_text(
                ctx.chat_id, f"{len(plan)} 个 agent 都已有群，无需新建。")
            return

        if cap:
            create = create[:cap]
        self.reply_text(
            ctx.chat_id,
            f"共 {len(plan)} 个 agent：复用 {len(reuse)} 个群，本次新建 {len(create)} 个…")

        # 把发起人拉进新群，否则建了也进不去。
        members = [ctx.sender_open_id] if ctx.sender_open_id else []
        made, failed = 0, 0
        for item in create:
            try:
                chat_id = self.api.create_chat(item["title"], members)
            except Exception as exc:
                log.warning("create chat failed for %s: %s", item["title"], scrub(exc))
                failed += 1
                continue
            if not chat_id:
                failed += 1
                continue
            made += 1
            self.chat_ids.add(chat_id)
            self.chat_store.add(chat_id)
            # 只做预绑定：用户进群看到提示、确认之后才生效。
            self.stage_binding(chat_id, item["pane_id"])

        summary = f"✅ 新建 {made} 个群"
        if failed:
            summary += f"，{failed} 个失败（看日志）"
        remaining = len([p for p in plan if not p["chat_id"]]) - len(create)
        if remaining > 0:
            summary += f"\n还有 {remaining} 个未建，再发一次 /spaces 继续。"
        summary += "\n\n新群已各自绑好 agent，进群直接发消息即可。"
        self.reply_text(ctx.chat_id, summary)

    async def _handle_new(self, ctx: MessageContext, rest: str) -> None:
        """/new <序号> [类型] —— 在某个 agent 的同目录再开一个。

        手机上打全路径太痛苦，所以用序号指目录：抄它的 cwd。
        """
        target, kind = parse_new_args(rest)
        if not target:
            self.reply_text(
                ctx.chat_id,
                "用法: /new <序号> [agent类型]\n"
                "  /new 3         在 3 号的目录下开一个 claude\n"
                "  /new 3 codex   同上，但用 codex\n"
                f"可选类型: {', '.join(sorted(AGENT_KINDS))}",
            )
            return

        source = match_agent(self.agents, target)
        if source is None:
            self.reply_text(ctx.chat_id, f"没找到 '{target}'。用 /agents 看列表。")
            return

        cwd = source.get("cwd") or ""
        project = source.get("project") or "?"
        self.reply_text(ctx.chat_id, f"正在 {project} 的目录下开一个 {kind}…")

        try:
            created = await create_workspace_via_relay()
        except Exception as exc:
            self.reply_text(ctx.chat_id, f"建工作区失败: {scrub(exc)}")
            return

        pane_id = created.get("pane_id")
        if not pane_id:
            self.reply_text(
                ctx.chat_id, "工作区建好了，但没拿到 pane —— 用 /agents 看看。")
            return

        # 新 pane 是个空 shell，把启动命令打进去。
        try:
            await send_text_to_relay(pane_id, agent_launch_command(kind, cwd))
            self.audit(ctx.chat_id, "new", {"project": project,
                                            "pane_id": pane_id}, f"启动 {kind}")
        except Exception as exc:
            self.reply_text(ctx.chat_id, f"启动 {kind} 失败: {scrub(exc)}")
            return

        label = created.get("label") or project
        self.set_active(ctx.chat_id, pane_id, label)
        self.reply_text(
            ctx.chat_id,
            f"✅ 已在 {project} 的目录下开好 {kind}\n\n直接发消息即可下指令。",
        )

    async def _handle_watch(self, ctx: MessageContext, rest: str) -> None:
        """/watch [序号] —— 卡片自己刷新，不用反复 /read。"""
        chat_id = ctx.chat_id
        existing = self._watchers.pop(chat_id, None)
        if existing:
            existing.cancel()
            if not rest or rest.strip().lower() in ("stop", "off"):
                self.reply_text(chat_id, "已停止跟随。")
                return

        if rest and rest.strip().lower() in ("stop", "off"):
            self.reply_text(chat_id, "当前没有在跟随。")
            return

        pane_id = None
        if rest:
            agent = match_agent(self.agents, rest)
            if agent is None:
                self.reply_text(chat_id, f"没找到 '{rest}'。用 /agents 看列表。")
                return
            pane_id = agent["pane_id"]
        else:
            pane_id = self.active_pane(chat_id)
            if pane_id is None:
                self.reply_text(chat_id, "还没选 agent。用 /watch <序号>。")
                return
            agent = find_agent(self.agents, pane_id) or {"pane_id": pane_id}

        project = agent.get("project") or agent.get("agent") or "agent"
        self.set_active(chat_id, pane_id, project)
        # 手工 /watch 不限时——是你主动要看的。
        self._start_watch(chat_id, pane_id, project, agent.get("agent", ""), limit=0)

    async def _watch_loop(self, chat_id: str, pane_id: str,
                          project: str, agent_name: str,
                          limit: int = 0) -> None:
        """建一张流式卡片，持续把 pane 内容推进去。

        跟到 agent 停下来为止——一直跟着没意义，而且白烧配额。
        """
        throttle = StreamThrottle()
        try:
            card_id = self.api.create_stream_card(
                build_stream_card_json(project, agent_name))
            message_id = self.api.send_card_entity(chat_id, card_id)
            self.remember(chat_id, message_id, pane_id)
        except Exception as exc:
            log.warning("stream card setup failed: %s", scrub(exc))
            self.reply_text(chat_id, f"流式卡片创建失败，改用 /read。({scrub(exc)})")
            return

        idle_rounds = 0
        read_failures = 0
        started = time.time()
        try:
            while idle_rounds < WATCH_IDLE_ROUNDS:
                if watch_expired(started, time.time(), limit):
                    break
                raw = clean_pane(await read_pane(pane_id))
                if is_transient_read(raw):
                    # 读失败就跳过这一帧，别把卡片刷空。
                    # 但不能无限跳：手工 /watch 不限时，relay 一直读不到
                    # 就会永远转下去。
                    read_failures += 1
                    if read_failures >= WATCH_MAX_READ_FAILURES:
                        break
                    await asyncio.sleep(STREAM_INTERVAL_S)
                    continue
                read_failures = 0
                body = stream_body(raw)
                if throttle.should_send(body, now=time.time()):
                    try:
                        self.api.stream_content(
                            card_id, body, throttle.next_sequence())
                    except Exception as exc:
                        log.warning("stream update failed: %s", scrub(exc))
                        break

                agent = find_agent(self.agents, pane_id)
                status = (agent or {}).get("status")
                idle_rounds = idle_rounds + 1 if status in ("idle", "done") else 0
                await asyncio.sleep(STREAM_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("watch loop failed: %s", scrub(exc))
        finally:
            self._watchers.pop(chat_id, None)
            self.reply_text(chat_id, f"— {project} 跟随结束，直接发消息即可继续指挥")

    async def _handle_free_text(self, ctx: MessageContext, text: str) -> None:
        """自由文本直接发给当前 agent——这是「看完接着指挥」的闭环。"""
        if not text:
            return
        pane_id = self.active_pane(ctx.chat_id)
        if pane_id is None:
            staged = self.staged_pane(ctx.chat_id)
            if staged:
                # 这个群是 /spaces 建的，还没确认过。先问清楚再说，
                # 别把用户随口打的字发进一个他没选过的终端。
                agent = find_agent(self.agents, staged) or {}
                project = agent.get("project") or staged
                if should_warn_shell(agent):
                    self.reply_text(
                        ctx.chat_id,
                        f"本群对应 **{project}**，但里面还没开 agent。\n"
                        f"用 /new <序号> 开一个，或 /agents 换一个。",
                    )
                else:
                    self.reply_text(
                        ctx.chat_id,
                        f"本群对应 **{project}**，尚未启用。\n"
                        "发 /read 查看它的进展并启用；或 /agents 换一个。",
                    )
                return
            pane_id = self._recent_pane()
        if pane_id is None:
            self.reply_text(ctx.chat_id, "还没选 agent。先 /agents 看列表，再 /read <序号>。")
            return
        agent = find_agent(self.agents, pane_id)
        if agent is None:
            self.reply_text(ctx.chat_id, "那个 agent 已经不在了。用 /agents 重新选。")
            return
        project = agent.get("project") or agent.get("agent") or "agent"

        # 裸 shell 里没有 agent，发过去只会变成 shell 命令。
        # 实际发生过：一个「1」进了 zsh，报 command not found。
        if should_warn_shell(agent):
            self.reply_text(ctx.chat_id, shell_hint(project))
            return

        # 只打了个数字，且这个 pane 确实在等人选 —— 那是按键，不是要发的文本。
        # 走 send_text 是没用的：粘贴进去的换行会被 TUI 当正文，确认不了提示。
        if looks_like_option_press(text) and pane_id in self.approval_tokens:
            await send_keys_to_relay(pane_id, [text.strip()])
            self.audit(ctx.chat_id, "approve", agent, f"选项 {text.strip()}")
            self.approval_tokens.pop(pane_id, None)
            self.reply_text(ctx.chat_id, f"→ 已选 {text.strip()}（{project}）")
            return

        await send_text_to_relay(pane_id, text)
        self.audit(ctx.chat_id, "send", agent, text)
        self.reply_text(ctx.chat_id, f"→ 已发给 {project}")
        self._maybe_autowatch(ctx.chat_id, pane_id, project)

    async def _handle_action(self, ctx: MessageContext) -> None:
        data = parse_action_value(ctx.action, self.agents, self.pending)
        action = data.get("action")

        if action == "page":
            menu = data.get("menu", "select_reply")
            self.reply_card(ctx.chat_id, build_agent_picker_card(
                menu, agents_for_action(menu, self.agents), page=int(data.get("page", 0))))
            return

        pane_id = data.get("pane_id")
        if not pane_id:
            self.reply_text(ctx.chat_id, "That agent is no longer available. Use /agents.")
            return
        agent = find_agent(self.agents, pane_id) or {"pane_id": pane_id, "project": pane_id}

        if action == "read":
            await self._send_pane_content(ctx.chat_id, agent)
        elif action == "select_reply":
            await self._send_pane_content(ctx.chat_id, agent, prompt=True)
        elif action == "select_send":
            self._prompt_for_reply(ctx.chat_id, agent)
        elif action == "trust":
            await send_to_relay(pane_id, "trust, always allow")
            self.audit(ctx.chat_id, "trust", agent)
            self.reply_text(ctx.chat_id, f"Trusted {agent.get('project')} (always allow)")
        elif action == "interrupt":
            await self._interrupt(ctx.chat_id, agent)
        elif action == "approval":
            await self._approve(ctx, data, pane_id)
        else:
            self.reply_text(ctx.chat_id, "That action is no longer valid. Use /agents.")

    async def _approve(self, ctx: MessageContext, data: dict, pane_id: str) -> None:
        if not approval_is_current(self.approval_tokens, pane_id, data.get("g")):
            self.reply_text(
                ctx.chat_id,
                "那条审批属于更早的提示，请用最新一条 blocked 通知上的按钮。",
            )
            return
        key = data.get("k")
        if key is None:
            self.reply_text(ctx.chat_id, "该审批动作已不受支持，请用最新的通知。")
            return
        # 按选项序号发按键。发选项文本不行：relay 用 send-text 粘贴，
        # Claude 的 TUI 把粘贴里的换行当正文而非回车，提示确认不了。
        await send_keys_to_relay(pane_id, [str(key)])
        self.audit(ctx.chat_id, "approve",
                   find_agent(self.agents, pane_id) or {"pane_id": pane_id},
                   f"选项 {key}")
        self.approval_tokens.pop(pane_id, None)
        self.reply_text(ctx.chat_id, f"已选 {key}")
        # 多组问题是逐组问的：答完这组，下一组才会显示出来。
        # 等 TUI 刷新后再看一眼，有新的就接着推。
        await asyncio.sleep(1.2)
        await self._push_next_group(ctx.chat_id, pane_id)

    async def _push_next_group(self, chat_id: str, pane_id: str) -> None:
        """答完一组后，若还有下一组就接着推。"""
        try:
            content = clean_pane(await read_pane(pane_id))
        except Exception as exc:
            log.warning("next-group read failed: %s", scrub(exc))
            return
        groups = detect_option_groups(content)
        current = current_option_group(groups)
        if not current:
            return
        agent = find_agent(self.agents, pane_id) or {}
        project = agent.get("project") or "agent"
        generation = new_generation()
        self.approval_tokens[pane_id] = generation
        card_id = self.reply_card(chat_id, build_options_card(
            pane_id, project, current["options"], generation,
            question=current["question"]))
        self.remember(chat_id, card_id, pane_id)

    # --- 辅助 ---

    def _recent_pane(self) -> str | None:
        return next(reversed(self.pending.values()), None) if self.pending else None

    def _prompt_for_reply(self, chat_id: str, agent: dict) -> None:
        project = agent.get("project") or agent.get("agent") or "agent"
        self.set_active(chat_id, agent["pane_id"], project)
        message_id = self.reply_text(chat_id, follow_up_hint(project))
        self.remember(chat_id, message_id, agent["pane_id"])

    def audit(self, chat_id: str, action: str, agent: dict, detail: str = "") -> None:
        """在发起操作的那个群里留一行痕迹。失败只记日志，不影响主流程。

        痕迹落在本群而不是单独的审计群：操作发生在哪个群，追溯就该在哪个群，
        不用切到别处对时间线。
        """
        if not self.audit_on or not chat_id:
            return
        project = (agent or {}).get("project") or "?"
        pane_id = (agent or {}).get("pane_id") or "?"
        line = format_audit(action, project, pane_id, detail)
        try:
            self.api.send_text(chat_id, line)
        except Exception as exc:
            log.warning("audit send failed: %s", scrub(exc))

    def _send_images_in(self, chat_id: str, content: str) -> int:
        """输出里提到的图片直接发出去，省得跑回电脑看。"""
        sent = 0
        for path in find_image_paths(content):
            if not is_sendable_image(path):
                continue
            try:
                self.api.send_image(chat_id, path)
                sent += 1
            except Exception as exc:
                log.warning("send image failed (%s): %s", path, scrub(exc))
        return sent

    async def _send_pane_content(self, chat_id: str, agent: dict, prompt: bool = False) -> None:
        content = clean_pane(await read_pane(agent["pane_id"]))
        project = agent.get("project") or agent.get("agent") or "agent"
        pane_id = agent["pane_id"]
        # 读过之后就把它设成当前 agent：接下来直接打字就是发给它，
        # 不必每句都带序号。这是「看进展 → 接着指挥」的闭环。
        self.set_active(chat_id, pane_id, project)

        groups = detect_option_groups(content)
        hint = ("— 点按钮或直接回数字即可选择"
                if groups else follow_up_hint(project))

        if self.render_mode == "card":
            message_id = self.reply_card(chat_id, build_pane_card(
                project, agent.get("agent", ""), agent.get("status", ""), content))
            self.remember(chat_id, message_id, pane_id)
            self.reply_text(chat_id, hint)
        else:
            message_id = self.reply_text(
                chat_id, format_pane_text(project, content, hint))
            self.remember(chat_id, message_id, pane_id)

        self._send_images_in(chat_id, content)

        # 正卡在选择器上：补一张按钮卡片，省得手打数字。
        if groups:
            generation = new_generation()
            self.approval_tokens[pane_id] = generation
            current = current_option_group(groups)
            card_id = self.reply_card(chat_id, build_options_card(
                pane_id, project, current["options"], generation,
                question=current["question"]))
            self.remember(chat_id, card_id, pane_id)

    async def _interrupt(self, chat_id: str, agent: dict) -> None:
        # 必须走带 ack 的 helper，且键名只能是 relay SAFE_KEYS 里的 "C-c"。
        try:
            await send_keys_to_relay(agent["pane_id"], ["C-c"])
            self.audit(chat_id, "interrupt", agent)
            self.reply_text(chat_id, f"Sent Ctrl+C to {agent.get('project')}")
        except Exception as exc:
            self.reply_text(chat_id, f"Failed: {scrub(exc)}")

    def _dashboard_text(self) -> str:
        if not self.relay_connected:
            return "herdr-remote\n\nRelay disconnected. Use /status for details."
        if not self.agents:
            return "herdr-remote\n\nConnected to relay. No agents are running."
        return f"herdr-remote\n\n{status_summary(self.agents)}"


def is_finish_transition(old_status: str | None, new_status: str) -> bool:
    """agent 是不是刚从「在干活」变成「停下来等你」。

    old_status 为 None 表示首次见到这个 pane——启动时一屋子 idle agent
    不该各推一条通知。转 blocked 也不算：那有专门的审批卡片。
    """
    return (old_status in ("working", "blocked")
            and new_status in ("idle", "done")
            and old_status != new_status)


def format_finish_message(project: str, agent: str, output: str) -> str:
    """完成通知。只说一句 finished 等于没说，得带上它干了什么。"""
    body = (output or "").strip() or "(无输出)"
    limit = 2600
    if len(body) > limit:
        # 结论通常在末尾，留尾巴。
        body = "⋯\n" + body[-limit:]
    return f"✅ {project} ({agent}) 停下来了\n\n{body}"


# --- 群与 agent 的绑定 ---

# 一个群只跟一个 agent 打交道。15 个 agent 挤一个群会分不清谁是谁，
# 完成推送也会互相刷屏。群名直接写上当前绑的是谁，会话列表里一眼可见。
CHAT_TITLE_PREFIX = "herdr · "
_CHAT_TITLE_LIMIT = 60


def chat_title_for(project: str, marker: str = "") -> str:
    """把群名改成「herdr · [标记] <项目>」，一眼看出这个群管的是谁。

    重名 agent 必须带上标记：两个群都叫「herdr · yqg-dw-datapilot」的话，
    会话列表里根本切不对。

    标记放在项目名**前面**：会话列表宽度有限，尾部会被截掉，放后面等于
    看不见。项目名太长时宁可截项目名，也要保住标记。
    """
    marker = (marker or "").strip()
    head = f"{marker} " if marker else ""
    room = _CHAT_TITLE_LIMIT - len(CHAT_TITLE_PREFIX) - len(head)
    return CHAT_TITLE_PREFIX + head + (project or "?")[:max(1, room)]


def chats_watching(bot: "LarkBot", pane_id: str) -> list[str]:
    """哪些群该收到这个 pane 的通知。

    绑定了就发给绑定的群；一个都没绑时回落到默认会话，
    否则通知会悄无声息地丢掉。
    """
    bound = [chat for chat, pane in bot._active.items() if pane == pane_id]
    if bound:
        return sorted(bound)
    # 一个群都没绑它：发给所有授权群，否则通知会悄无声息地丢掉。
    return sorted(bot.chat_ids) if bot.chat_ids else []


# --- relay 监听 ---

def _notify_blocked(bot: "LarkBot", msg: dict) -> None:
    pane_id = msg.get("pane_id")
    if not pane_id:
        return
    generation = new_generation()
    project = msg.get("project", "")
    card = build_blocked_card(
        pane_id,
        msg.get("agent", "unknown"),
        project,
        msg.get("prompt", ""),
        msg.get("options"),
        generation,
    )
    bot.approval_tokens[pane_id] = generation
    for chat_id in chats_watching(bot, pane_id):
        message_id = bot.reply_card(chat_id, card)
        bot.remember(chat_id, message_id, pane_id)


def _track_updates(bot: "LarkBot", updated: list[dict]) -> None:
    """维护每日统计，并在 agent 干完活时推一条通知。"""
    import time
    now = time.time()
    for agent in updated:
        pane_id = agent.get("pane_id")
        if not pane_id:
            continue
        new_status = agent.get("status", "unknown")
        old_status = bot.prev_statuses.get(pane_id)

        stats = bot.daily_stats.setdefault(pane_id, {
            "agent": agent.get("agent", ""), "project": agent.get("project", ""),
            "blocked_count": 0, "working_mins": 0, "last_change": now,
        })
        if old_status == "working" and old_status != new_status:
            stats["working_mins"] += int((now - stats["last_change"]) / 60)
        if new_status == "blocked" and old_status != "blocked":
            stats["blocked_count"] += 1
        if old_status != new_status:
            stats["last_change"] = now

        # 有群绑着它就推（没绑时 chats_watching 会回落到默认会话）。
        if is_finish_transition(old_status, new_status) and chats_watching(bot, pane_id):
            # 读 pane 要走 relay 往返，不能卡住监听循环——丢到后台跑。
            asyncio.create_task(_notify_finished(bot, dict(agent)))
        bot.prev_statuses[pane_id] = new_status


async def _notify_finished(bot: "LarkBot", agent: dict) -> None:
    """agent 停下来时主动推：带上输出，卡在选择器就补按钮。"""
    pane_id = agent.get("pane_id")
    project = agent.get("project") or agent.get("agent") or "agent"
    log.info("agent finished, pushing to Lark: %s (%s)", project, pane_id)
    try:
        content = clean_pane(await read_pane(pane_id))
    except Exception as exc:
        log.warning("finish notify read failed: %s", scrub(exc))
        content = ""

    groups = detect_option_groups(content)
    generation = new_generation() if groups else None
    if generation:
        bot.approval_tokens[pane_id] = generation

    # 只发给绑定了这个 agent 的群；一个都没绑才回落到默认会话。
    for chat_id in chats_watching(bot, pane_id):
        if bot.render_mode == "card":
            # 完成时状态已是 idle/done，用 done 让头部显示为绿色。
            message_id = bot.reply_card(chat_id, build_pane_card(
                project, agent.get("agent", ""), "done", content))
        else:
            message_id = bot.reply_text(
                chat_id, format_finish_message(project, agent.get("agent", ""), content))
        bot.remember(chat_id, message_id, pane_id)
        bot.set_active(chat_id, pane_id, project)
        bot._send_images_in(chat_id, content)
        if groups:
            current = current_option_group(groups)
            card_id = bot.reply_card(chat_id, build_options_card(
                pane_id, project, current["options"], generation,
                question=current["question"]))
            bot.remember(chat_id, card_id, pane_id)


async def relay_listener(bot: "LarkBot") -> None:
    """一直连着 relay，断了 5 秒后重连。"""
    from agent_state import apply_agent_message

    while True:
        try:
            async with ws_connect(RELAY_WS) as ws:
                bot.relay_connected = True
                log.info("Connected to relay at %s", RELAY_WS_SAFE)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    kind = msg.get("type")

                    if kind == "agents":
                        incoming = msg.get("agents", [])
                        bot.prune_stale_bindings(incoming)
                        prune_approval_tokens(bot.approval_tokens, incoming)
                        _track_updates(bot, incoming)
                        bot.agents = apply_agent_message(bot.agents, msg)
                    elif kind == "agent_update":
                        agent = msg.get("agent") or {}
                        if agent.get("pane_id"):
                            _track_updates(bot, [agent])
                            bot.agents = apply_agent_message(bot.agents, msg)
                    elif kind == "blocked":
                        _notify_blocked(bot, msg)
        except Exception as exc:
            log.warning("Relay connection lost: %s, reconnecting in 5s", scrub(exc))
        bot.relay_connected = False
        bot.agents = []
        bot.approval_tokens.clear()
        await asyncio.sleep(5)


# --- 入口 ---

def _start_lark_thread(bot: "LarkBot") -> None:
    """飞书长连接跑在独立线程：SDK 的 start() 是阻塞的同步调用。"""
    import threading
    import lark_oapi as lark

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(
                   lambda data: bot.on_message_event(
                       json.loads(lark.JSON.marshal(data)).get("event", {})))
               .register_p2_card_action_trigger(
                   lambda data: _card_action_adapter(bot, lark, data))
               .build())

    client = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler,
                            log_level=lark.LogLevel.INFO)
    threading.Thread(target=client.start, daemon=True, name="lark-ws").start()
    log.info("Lark long connection thread started")


def _card_action_adapter(bot: "LarkBot", lark, data):
    """卡片回调必须立刻返回，否则会撞上飞书的 3 秒超时。"""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
    try:
        bot.on_card_action(json.loads(lark.JSON.marshal(data)).get("event", {}))
    except Exception as exc:
        log.warning("card action failed: %s", scrub(exc))
    return P2CardActionTriggerResponse({
        "toast": {"type": "info", "content": "已收到"}
    })


def main() -> None:
    if not APP_ID or not APP_SECRET:
        raise SystemExit("请设置 HERDR_LARK_APP_ID 与 HERDR_LARK_APP_SECRET")

    api = LarkAPI(APP_ID, APP_SECRET, DOMAIN)
    bot_open_id = api.fetch_bot_open_id()
    log.info("Bot ready: %s", bot_open_id)

    if not CHAT_ID:
        log.warning("未设置 HERDR_LARK_CHAT_ID：处于发现模式，任何会话都会被响应")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot = LarkBot(api, CHAT_ID, loop)
    _start_lark_thread(bot)
    loop.run_until_complete(relay_listener(bot))


if __name__ == "__main__":
    main()
