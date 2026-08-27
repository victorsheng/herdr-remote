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

# observer 应用的 app_id。/spaces 建群时顺手把它拉进去——observer 只能巡检
# 自己所在的群，漏拉一个群等于那个群的质检静默关掉，而这种缺失不会报错。
# 空值表示没部署 observer，跳过即可。
OBSERVER_APP_ID = os.environ.get("HERDR_LARK_OBSERVER_APP_ID", "").strip()

# 按人授权：这些 open_id 发来的消息，无论在哪个群都放行，并顺手把群收养
# （登记 + 拉 observer）。群白名单要求「先建群、再查 ID、再改配置、再重启」，
# 漏一步的表现是机器人在群里装死——不报错，只是不理人。
# 注意 open_id 按应用隔离：这里要填 herdr 机器人看到的你，不是别的应用里的。
USER_IDS = os.environ.get("HERDR_LARK_USER_ID", "")

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
    "clear": "c",
    "approval": "k",
    "submit": "u",
    "page": "g",
    "agent_menu": "m",
    "git": "v",
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


# relay 协议层对 send_text 的硬限制（herdr_relay.py 也校验同一个数）。
SEND_TEXT_LIMIT = 1000


def split_send_text(text: str, limit: int = SEND_TEXT_LIMIT) -> list[str]:
    """把超长文本切成 ≤limit 的段，一个字都不丢。

    以前超长直接抛 ValueError，异常冒到 _handle 顶层只记一行日志——群里
    完全没反馈，人以为发出去了，其实 agent 根本没收到（lark-stderr.log
    里那条 `handler failed` 就是）。

    尽量在换行处断：粘进终端之后，从句子中间断开的可读性差很多。找不到
    合适的换行就硬切，宁可难看也不能丢字。
    """
    text = text or ""
    if not text:
        return []
    pieces = []
    while len(text) > limit:
        window = text[:limit]
        cut = window.rfind("\n")
        # 换行太靠前就不用了，否则会切出一堆碎片。
        if cut < limit // 2:
            cut = limit
        pieces.append(text[:cut])
        text = text[cut:].lstrip("\n") if cut < limit else text[cut:]
    if text:
        pieces.append(text)
    return pieces


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


# 按 ↓ 展开折叠消息的次数上限。按键会动 TUI 焦点，是有副作用的操作；
# TUI 不响应时（提示一直在）不设限就会无限按下去——Left 切组那处踩过。
MAX_EXPAND_PRESSES = 3


async def read_pane_expanded(pane_id: str, lines: int = READ_LINES) -> str:
    """读屏，遇到折叠的消息就按 ↓ 展开，把前后内容拼起来。

    TUI 折叠时屏幕上只留 `8 new messages (click) ↓`，那几条在飞书上根本
    读不到——手机上也点不了那个 (click)。

    只在**真的发现折叠提示**时才按键：按 ↓ 会动 TUI 焦点，没折叠还按就是
    平白干扰 agent。任何一步失败都退回已有内容——展开失败最多是看不到折叠
    的那几条，绝不能反过来把本来能看的也弄没了。
    """
    content = await read_pane(pane_id)
    if is_pane_read_error(content):
        return content            # 让调用方的 is_pane_read_error 认出来
    for _ in range(MAX_EXPAND_PRESSES):
        if find_collapsed_messages(content) is None:
            break
        try:
            await send_keys_to_relay(pane_id, ["Down"])
            nxt = await read_pane(pane_id, lines)
        except Exception as exc:
            log.warning("展开折叠消息失败 %s: %s", pane_id, scrub(exc))
            break
        if is_pane_read_error(nxt):
            break                 # 保住已经拿到的内容
        merged = drop_collapsed_hints(merge_expanded(content, nxt))
        if merged == content:
            break                 # 按了没变化，别再按
        content = merged
    return content


async def fetch_git_status(pane_id: str) -> dict:
    """向 relay 要这个 pane 的 git 状态。

    relay 那边按 pane_id 解析 cwd 和 SSH remote，所以远程 agent 也能查——
    这里不碰 git，也不需要知道代码在哪台机器上。

    失败返回 {"ok": False, "message": ...}，与 relay 的错误结构一致，
    调用方只处理一种形状。
    """
    try:
        async with ws_connect(RELAY_WS) as ws:
            await ws.send(json.dumps({
                "type": "git_status", "pane_id": pane_id, "mode": "worktree",
            }))
            # 可能先撞上 agents 广播，往后多读几条找 git_status。
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT_S)
                msg = json.loads(raw)
                if msg.get("type") == "git_status":
                    return msg
    except Exception as exc:
        return {"ok": False, "message": scrub(str(exc))}
    return {"ok": False, "message": "relay 没有响应"}


def is_pane_read_error(content: str) -> bool:
    """read_pane 这次是失败了吗。

    read_pane 失败不抛异常，而是返回 `(error reading pane: …)` / `(no response)`
    这样的字符串——调用方只 try/except 是拦不住的，会把这句话当成正常屏幕
    内容去解析。解析不出选择器就以为人已经答完，清掉 approval_token，
    于是下一次点击被当成过期审批拒掉，人卡死在卡片上。
    """
    text = (content or "").strip()
    if not text:  # 空屏判断不了状态，按失败处理，别拿它当「已答完」的依据
        return True
    return text.startswith("(error reading pane:") or text == "(no response)"


# --- 卡片构造 ---

# 与 relay 的 TOOL_OPTIONS / SUBAGENT_OPTIONS 对应（见 herdr_relay.py:63）。
TOOL_OPTIONS = ["yes, single permission", "trust, always allow", "no (tab to edit)"]

# 选项卡片的统一样式：两种来源共用，改一处就到处生效。
OPTION_CARD_TEMPLATE = "turquoise"
# 正文里每个选项的文字上限。
#
# 选项全文列在正文、按钮只放序号（见 build_option_card）：长选项塞进按钮
# 会在手机上折成好几行，一排堆起来根本没法扫。拆开之后正文负责「看清楚」，
# 按钮负责「点得准」。
#
# 正文不像按钮那样受排版挤压，额度给得宽：240 够装下实测遇到的选项
# （抓屏里最长 59 字），真超了才截。原来按钮限 40 字时，一个 59 字的选项
# 被砍成「…把 PRUNED 分」，丢掉的恰好是「但要注意存量调用方的兼容性问题」
# 这个决策关键——而且不留任何标记，读起来像句子说完了。
OPTION_LABEL_LIMIT = 240


def option_label(text: str, limit: int = OPTION_LABEL_LIMIT) -> str:
    """正文里显示的选项文字，超长才截，且一定留省略号。

    无声截断最坑：砍在句子中间时看起来就像原文如此，人照着它做判断，
    而真正影响决策的半句在屏幕上——他根本不知道自己没看全。
    """
    body = (text or "").strip()
    return body if len(body) <= limit else body[:limit - 1].rstrip() + "…"

TOOL_BUTTON_LABELS = ["Yes (once)", "Trust (always)", "No"]
SUBAGENT_BUTTON_LABELS = ["Approve all", "Configure", "Cancel"]

# 兼容旧引用：配色现在由 _option_styles 统一算，这里只留标签。
TOOL_BUTTONS = [(label, "default") for label in TOOL_BUTTON_LABELS]
SUBAGENT_BUTTONS = [(label, "default") for label in SUBAGENT_BUTTON_LABELS]


# 卡片正文额度。输出卡片（build_pane_card）和审批卡片共用同一个数，
# 免得同一份内容在更需要看全的审批场景反而被砍得更狠。
_CARD_BODY_LIMIT = 2400


def truncate_prompt(text: str, limit: int = _CARD_BODY_LIMIT) -> str:
    """超长时保留**末尾**，开头标一个记号。

    直接 text[:limit] 会把命令结尾切掉，而审批一条 rm -rf 时最该看清的恰恰
    是末尾的路径。

    原先是「保首尾、中间挖空」（抄官方 Channels 的权限中继），实测中间那段
    `⋯ 省略 N 字 ⋯` 恰好盖住问题和选项——审批要看的问题、选项、命令结尾
    全在末尾，开头往往是已经翻过去的旧上文。改成保末尾，与 build_pane_card
    的 body[-N:] 一致。

    额度也从写死的 400 提到 _CARD_BODY_LIMIT：同一份内容在输出卡片有 2400，
    在更需要看全的审批卡片却只有 400，是反的。
    """
    if len(text) <= limit:
        return text
    return "⋯\n" + text[-limit:]


# 一条 blocked 最多铺几张卡片。超长正文拆开发，但不能无限铺——agent 吐一屏
# 长输出时会把群刷满。3 张 = 前置 2 张 + 带按钮的审批卡 1 张，共 7200 字额度。
_MAX_PROMPT_CARDS = 3


def split_prompt_cards(text: str, limit: int = _CARD_BODY_LIMIT,
                       max_cards: int = _MAX_PROMPT_CARDS) -> list[str]:
    """把超长正文切成多段，**末尾一定落在最后一段**。

    为什么不直接截断：审批（尤其是选择）的判断依据在上文里，砍掉就只能盲选。
    额度提到 2400 之后仍不够用的场景是真实存在的，所以拆开发。

    为什么末尾必须在最后一段：问题、选项、命令结尾都在末尾，而按钮挂在最后
    那张卡上——人往下滑，最后看到的就该是要点的东西。

    总额度还装不下时舍弃**最开头**的，并在首段留 ⋯ 记号：宁可明说「前面还有
    内容」，也不假装内容是全的。
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    budget = limit * max_cards
    dropped = len(text) - budget
    if dropped > 0:
        text = text[-budget:]
    # 从末尾往前切，保证最后一段是原文的末尾（不足一段的零头落在最前面）。
    pieces = [text[max(0, i - limit):i] for i in range(len(text), 0, -limit)]
    pieces.reverse()
    if dropped > 0:
        pieces[0] = "⋯\n" + pieces[0]
    return pieces


def _approval_labels(options: list[str] | None) -> list[str]:
    """把 relay 给的选项换成按钮文字。

    只管文字，不管配色——配色由 _option_styles 按语义统一决定，两处各判一次
    就会出现同一个选项在两张卡片上颜色不同。

    relay 的选项文本偏长（"yes, single permission"），认得出的两种常见提示
    换成短标签；认不出就取逗号前那截。
    """
    joined = " ".join(options or []).lower()
    if not options or "trust" in joined:
        return TOOL_BUTTON_LABELS
    if "approve all" in joined:
        return SUBAGENT_BUTTON_LABELS
    return [opt.split(",")[0] for opt in options]


def _button(label: str, value: dict, button_type: str = "default") -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": button_type,
        "value": value,
    }


# 选项按钮的配色：按语义给，不按位置给。
# 只匹配开头的词——「No」「取消」是拒绝，但「不错的方案」不是。子串匹配会把
# 正常选项误染成红色。
_DANGER_RE = re.compile(
    r"^\s*(no\b|nope\b|cancel|reject|deny|abort|skip\b|拒绝|取消|不要|别)")
# 放权项：让它别抢 primary，否则手机上最显眼的按钮是「总是允许」。
_CAUTION_RE = re.compile(r"(trust|always|approve\s+all|总是|全部允许)", re.I)


def _option_styles(options: list[str]) -> list[str]:
    """整组选项的按钮配色。

    语义优先于位置：danger 给会拒绝掉的那项；放权项（trust / approve all）
    不抢 primary，否则手机上最显眼的按钮恰好是风险最大的那个。
    primary 只给一个——第一个既不拒绝也不放权的选项；一组里两个高亮按钮
    等于没有高亮。整组都是放权/拒绝时就不给 primary。
    """
    styles = []
    primary_used = False
    for label in options:
        if _DANGER_RE.match(label.strip().lower()):
            styles.append("danger")
        elif _CAUTION_RE.search(label):
            styles.append("default")
        elif not primary_used:
            styles.append("primary")
            primary_used = True
        else:
            styles.append("default")
    return styles


def build_option_card(
    pane_id: str,
    project: str,
    options: list[str],
    generation: str,
    *,
    question: str = "",
    prompt: str = "",
    agent: str = "",
    multiselect: bool = False,
    checked: list[bool] | None = None,
    numbers: list[int] | None = None,
) -> dict:
    """「正等你选」的唯一一张卡片。

    numbers 是各选项在**屏幕上**的编号。不传就按 1..n（旧调用方）。
    传了就必须用它渲染和发键——屏幕滚动后编号可能从 2 起，用下标+1 会
    让人点错。

    两种来源同一个样子：relay 推的 blocked（带 prompt 和 agent 名），和读
    pane 时顺手发现的下一组（带 question）。以前是两个 build 函数各带一份
    配色，同一次选择的前后两张卡片看着像两个功能。

    按钮带的是选项的 1-based 序号，点击后按对应数字键。发选项文本是不行的：
    relay 用 send-text 粘贴，Claude 的 TUI 把粘贴里的换行当正文而非回车，
    提示永远确认不了。
    """
    shown = options[:_MAX_OPTIONS]
    flags = list(checked or [])
    if multiselect:
        # 多选按数字是「切换勾选」，不是选中并前进，所以整排都不给 primary
        # ——高亮某一项会让人以为点它就定了。
        styles = ["default"] * len(shown)
        marks = ["✔ " if i < len(flags) and flags[i] else "☐ "
                 for i in range(len(shown))]
    else:
        styles = _option_styles(shown)
        marks = [""] * len(shown)
    # 带上「这是多选」的标记：_approve 得在发键之前决定补不补 Enter
    # （单选补了才提交，多选补了就把没勾完的答案交出去），而卡片是什么形态
    # 在这里就已经确定，编进 value 比事后读屏判断可靠。
    flag = {"m": 1} if multiselect else {}
    # 屏幕上的真实编号。shown 可能被 _MAX_OPTIONS 截过，所以按 shown 对齐。
    labels = [str(n) for n in (numbers or [])][:len(shown)]
    if len(labels) < len(shown):
        labels = [str(i + 1) for i in range(len(shown))]
    # 按钮只放序号：选项全文在正文里列着，按钮再重复一遍就会在手机上折成
    # 好几行，一排选项堆起来没法扫。
    actions = [
        _button(labels[i], action_value(
            "approval", pane_id, g=generation, k=labels[i], **flag), styles[i])
        for i, _ in enumerate(shown)
    ]

    elements: list[dict] = []
    if prompt:
        elements.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": f"```\n{truncate_prompt(prompt)}\n```",
        }})
    if question:
        elements.append({"tag": "div", "text": {
            "tag": "lark_md", "content": f"**{question}**"}})
    # 选项清单。多选的勾选态也在这儿——按钮只剩序号，放不下标记了。
    elements.append({"tag": "div", "text": {
        "tag": "lark_md",
        "content": "\n".join(f"**{labels[i]}.** {marks[i]}{option_label(opt)}"
                             for i, opt in enumerate(shown)),
    }})
    elements.append({"tag": "action", "actions": actions})
    if multiselect:
        # 多选要显式提交：勾完点 Submit，卡片才把答案交上去。
        # 单选不给这个按钮——点一下就定了，多一个 Submit 只会让人以为还要再点。
        elements.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": "_可多选：点选项切换勾选，选完点 Submit_"}})
        elements.append({"tag": "action", "actions": [
            _button("✔ Submit", action_value(
                "submit", pane_id, g=generation), "primary"),
        ]})
    # blocked 是被动推来的，人还没看过输出，给个直达入口。
    if prompt:
        elements.append({"tag": "action", "actions": [
            _button("Open output & reply", action_value("select_reply", pane_id)),
        ]})

    title = (f"🐑 {agent} blocked in {project}" if agent
             else f"⌨︎ {project} 正在等你选")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": OPTION_CARD_TEMPLATE,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def build_blocked_card(
    pane_id: str,
    agent: str,
    project: str,
    prompt: str,
    options: list[str] | None,
    generation: str,
    body_override: str | None = None,
) -> dict:
    """agent 卡住时推的审批卡片。relay 监听那条路径在调。

    body_override：正文超长拆多条时，这张卡只放最后一段正文。选项仍从**完整**
    prompt 解析——问题和选项在末尾，但解析要看全文（比如多选的勾选态）。
    两者分开传，别用截断后的文本去解析选项。

    优先用 prompt 里真正的选择器。relay 的 detect_options 只认两种权限提示
    （yes, single permission / approve all pending），AskUserQuestion 的选择器
    认不出，就回落成 TOOL_OPTIONS——卡片显示 Yes/Trust/No，而屏幕上问的却是
    「1. 先停下 2. 继续建群 3. 先看日志」，按钮和选项对不上，点了等于乱答。
    我们这边已经能解析选择器，就别再信那个回落值。
    """
    group = current_option_group(detect_option_groups(prompt or ""))
    labels = group["options"] if group else _approval_labels(options)
    multiselect = detect_multiselect(prompt or "") if group else False
    # 选项已经是按钮了，正文里再留一份纯属重复，还会挤掉 truncate_prompt
    # 的额度——正文一长，中间那段省略正好盖住问题和选项。摘掉选择器，
    # 问题单独成块，剩下的额度留给上文。
    body = strip_selector(prompt or "") if group else (prompt or "")
    if body_override is not None:
        body = body_override
    return build_option_card(pane_id, project, labels, generation,
                             prompt=body or " ", agent=agent or "agent",
                             question=group["question"] if group else "",
                             multiselect=multiselect,
                             numbers=group.get("numbers") if group else None,
                             checked=checked_flags(prompt or "") if multiselect else None)


def build_options_card(pane_id: str, project: str, options: list[str],
                       generation: str, question: str = "",
                       content: str = "") -> dict:
    """读 pane 时发现正等着选，或答完一组后补推下一组。

    给了 content 就从中判断这是不是多选框——多选按数字只是切换勾选，得配一个
    Submit 按钮，语义与单选完全不同。不给则按单选渲染（旧调用方）。
    """
    multiselect = detect_multiselect(content) if content else False
    return build_option_card(pane_id, project, options, generation,
                             question=question, multiselect=multiselect,
                             checked=checked_flags(content) if multiselect else None)


def current_option_group(groups: list[dict]) -> dict | None:
    """agent 当前在等的那一组。

    实测（Claude Code v2.1.239）：顶部 tab 栏列出全部组（`☐ 方案 ☐ Agent`），
    选项区只渲染当前 tab 那一组，答完后原地替换成下一组。所以同屏正常只有
    一组，取最后一组即可；多于一组说明上方还粘着历史选择器的残影，此时
    仍是最后一组最新。

    只推当前这一组，不去渲染 tab 栏里的其它组：数字键只作用于当前 tab，
    跨组得先发 Tab，按钮发数字答不了第二组。答完后 _push_next_group 会把
    下一组接着推上来。
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


def build_agent_actions_card(agent: dict) -> dict:
    """选中一个 agent 后能做什么，全部摊成按钮。

    /agents 原来只回纯文本，看完还得手打「/read 1」——手机上打字是最累的
    一步，序号设计本就是为了省事，更省事的是根本不打。

    按钮按状态给，不是一律全给：空闲的 agent 没什么可中断，没卡住就没有
    可批的东西。判断口径与 agents_for_action 保持一致，免得卡片上有按钮、
    点下去却被「无可用 agent」拒掉。
    """
    pane_id = agent.get("pane_id") or ""
    project = agent.get("project") or agent.get("agent") or "agent"
    status = agent.get("status") or "unknown"

    actions = [
        _button("看进展", action_value("read", pane_id), "primary"),
        _button("发指令", action_value("select_send", pane_id)),
    ]
    if status in ("working", "blocked"):
        actions.append(_button("中断", action_value("interrupt", pane_id), "danger"))
    if status == "blocked":
        # 放在中断之后：最显眼的位置不该是「总是允许」。
        actions.append(_button("批准并总是允许", action_value("trust", pane_id)))

    icon = _STATUS_ICONS.get(status, "○")
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _STATUS_HEADERS.get(status, "grey"),
            "title": {"tag": "plain_text", "content": f"{icon} {project}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"`{agent.get('agent', '')}` · {status}"}},
            {"tag": "action", "actions": actions},
        ],
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



def chat_inventory(rows) -> list[dict]:
    """把列群结果整成清单，一群一条。

    不能按群名归并：群名可能重复（改名逻辑出过 bug，16 个群同名），
    用名字当 key 会让它们互相覆盖，群就凭空「消失」了。
    """
    return [{"name": name or "", "chat_id": chat_id}
            for name, chat_id in rows if chat_id]


def duplicate_named_chats(inventory: list[dict]) -> list[str]:
    """名字重复的群的 chat_id。同名本身就是出问题的信号。"""
    seen: dict[str, list[str]] = {}
    for item in inventory:
        name = item.get("name") or ""
        if name:
            seen.setdefault(name, []).append(item["chat_id"])
    out = []
    for ids in seen.values():
        if len(ids) > 1:
            out.extend(ids)
    return sorted(out)


def chat_name_index(inventory: list[dict]) -> dict[str, str]:
    """{群名: chat_id}，供 /spaces 按名字复用现成的群。

    同名时保留其中一个——复用哪个都对；要完整清单请用 chat_inventory。
    """
    index: dict[str, str] = {}
    for item in inventory:
        name = item.get("name") or ""
        if name:
            index.setdefault(name, item["chat_id"])
    return index


def parse_chat_ids(value: str) -> set[str]:
    """解析 HERDR_LARK_CHAT_ID：逗号分隔的多个群。"""
    return set(parse_chat_list(value))


def parse_chat_list(value) -> list[str]:
    """同上，但保留配置里的顺序并去重。

    顺序本身不再有语义（主群回落已去掉），保留它只为让日志和 /health 的
    输出跟配置对得上——排查时能照着配置逐条比。
    """
    if not isinstance(value, str):
        value = ",".join(value or [])
    out: list[str] = []
    for chat in value.split(","):
        chat = chat.strip()
        if chat and chat not in out:
            out.append(chat)
    return out


def parse_user_ids(value: str) -> set[str]:
    """逗号分隔的 open_id 列表。与 parse_chat_ids 同形，只是不需要保序。"""
    if not isinstance(value, str):
        value = ",".join(value or [])
    return {u.strip() for u in value.split(",") if u.strip()}


def is_authorized_sender(sender_open_id: str, allowed) -> bool:
    """这个人授权了吗。

    与 is_authorized_chat 的空集语义**相反**：空集授权任何人都不通过。
    群白名单空集是「发现模式」（第一次部署时要能拿到 chat_id），而用户
    白名单空集如果也放行，就等于把群白名单的限制整个绕过去了。
    """
    if not allowed or not sender_open_id:
        return False
    if isinstance(allowed, str):
        return sender_open_id == allowed
    return sender_open_id in allowed


def is_authorized_chat(chat_id: str, allowed) -> bool:
    """这个群授权了吗。空集合 = 发现模式，放行任何群。"""
    if not allowed:
        return True
    if isinstance(allowed, str):
        return chat_id == allowed
    return chat_id in allowed


def should_handle(ctx: MessageContext, bot_open_id: str, chat_id,
                  users=None) -> bool:
    """守门：授权、自言自语、群里没 @ 我，三种情况直接丢掉。

    授权走「群 or 人」的并集：群在白名单里放行（谁发都算），或者发消息
    的人在用户白名单里放行（在哪个群都算）。后者让自己新建的群不必再
    手工登记 chat_id。

    判据是**发消息的人**，不是群主：群主是自己的群里也可能有别人，而过了
    守门就能用 /reply、/send 往 agent 终端塞任意文本。

    自言自语的判定必须排在授权之前——否则把机器人自己的 open_id 配进
    白名单会让它自己跟自己聊到天荒地老。
    """
    if bot_open_id and ctx.sender_open_id == bot_open_id:
        return False
    if not (is_authorized_chat(ctx.chat_id, chat_id)
            or is_authorized_sender(ctx.sender_open_id, users)):
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


# 终端右下角的滚动提示符，会被**拼在正文行尾**，中间垫一大段空格：
#     5. Chat about this                       Jump to bottom (click) ↓
# 所以 _CHROME_PATTERNS 那些 ^...$ 整行锚定的模式滤不掉它。
# 用「前面有 2 个以上空格」区分装饰与正文：agent 真的在讨论这个提示符时
# （比如本次修复的讨论过程），那串字出现在句子里、前面不会有大段空格。
# 两种形态都要剪：
#   拼在正文行尾  →  前面垫了 2 个以上空格
#   独占一行      →  整行只有缩进 + 提示符
# 都靠「提示符前面没有正文」来判定，所以 agent 正文里以这串字开头的句子
# （比如本次修复的讨论）不受影响——那种情况前面是行首、后面紧跟中文。
_SCROLL_HINT_RE = re.compile(r"(?:\s{2,}|^\s*)Jump to bottom \(click\)\s*↓\s*$")


# TUI 把消息折叠起来时，屏幕上只留一行 `8 new messages (click) ↓`。
# 那些内容在飞书上根本读不到——手机上也点不了那个 (click)。
#
# 判据沿用 _SCROLL_HINT_RE：提示符是**拼在行尾、前面垫大段空格**的装饰。
# 不能按字面匹配整行——agent 讨论这个提示本身时（本次修复的对话里就有），
# 那串字出现在正文句子中间，误判会把真内容当成装饰。
_COLLAPSED_RE = re.compile(
    r"(?:\s{2,}|^\s*)(\d+)\s+new messages?\s*\(click\)\s*↓\s*$")


def collapsed_message_count(line: str) -> int | None:
    """这一行是折叠提示吗，是的话折了几条。"""
    match = _COLLAPSED_RE.search(line or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def find_collapsed_messages(text: str) -> int | None:
    """整屏里有没有折叠提示。有多处时取最后一处——那是最新的。"""
    found = None
    for line in (text or "").splitlines():
        count = collapsed_message_count(line)
        if count is not None:
            found = count
    return found


def drop_collapsed_hints(text: str) -> str:
    """去掉折叠提示行。

    展开之后这行就只是装饰了，留着有两个害处：手机上点不了那个 (click)
    纯占地方；更要紧的是合并后的内容里还留着它，下一轮会被当成「还有折叠」
    而多按一次 ↓，白白干扰 TUI。
    """
    kept = []
    for line in (text or "").splitlines():
        if collapsed_message_count(line) is not None:
            # 提示可能拼在正文行尾，剪掉提示、留下正文。
            head = _COLLAPSED_RE.sub("", line)
            if head.strip():
                kept.append(head)
            continue
        kept.append(line)
    return "\n".join(kept)


def merge_expanded(before: str, after: str) -> str:
    """把展开前后的两屏拼起来。

    两边都不能单独用：
      只留展开后的 —— 折叠提示上面那些**已经可见**的内容会丢，TUI 展开时
        视口往下滚，上面的内容滚出屏幕。
      只留展开前的 —— 折叠的那几条还是看不到，等于没修。

    按行找最大重叠（before 的后缀 == after 的前缀），重叠部分只留一份。
    终端两屏之间本来就是滚动关系，重叠必然存在；找不到重叠就直接接上，
    宁可多一点也别丢。
    """
    before = (before or "").rstrip()
    after = (after or "").rstrip()
    if not after:
        return before
    if not before or before == after:
        return after if not before else before
    b_lines = before.splitlines()
    a_lines = after.splitlines()
    # before 整段出现在 after 里（展开后是超集，视口没往下滚）——直接用
    # after，拼的话前半段会重复一遍。
    for offset in range(len(a_lines) - len(b_lines) + 1):
        if a_lines[offset:offset + len(b_lines)] == b_lines:
            return after
    # 从最大可能的重叠往下试，第一个命中的就是最大重叠。
    for size in range(min(len(b_lines), len(a_lines)), 0, -1):
        if b_lines[-size:] == a_lines[:size]:
            return "\n".join(b_lines + a_lines[size:])
    # after 整个被 before 包住（按 ↓ 没滚动）——不重复拼。
    if after in before:
        return before
    return "\n".join(b_lines + a_lines)


def _strip_scroll_hint(line: str) -> str:
    """剪掉行尾拼接的终端滚动提示符，留下行首的真实内容。"""
    return _SCROLL_HINT_RE.sub("", line)


# AskUserQuestion 带 preview 时，终端把选项和预览面板**并排渲染成两列**：
#     ❯ 1. 前文单发，最后一条带选项     ┌────────────────────────┐
#          超出额度的上文拆成 N 条      │ 【卡片 1/3】上文前段…  │
# 解析器按行切，右列的边框和别人的预览内容就混进了选项文字——实测 3 个选项
# 只认出 2 个（有答案点不到），选项文字也面目全非。
#
# 判据分两半，都要满足才切：
#   1. 框线出现在**大段空白**之后（≥3 空格）——preview 面板与左边的选项
#      文字之间是填充空白，而表格的竖线间距通常只有 1-2 格
#   2. 框线**左边那段本身不含框线字符**——表格行是 `│ a │ b │` 这种成对
#      闭合的形态，左边一定已经出现过竖线；preview 的左列是纯文本
# 只用条件 1 会把 `  │  #  │ 项 │` 这种表格切掉（实测吃掉了整张表的数据）。
_PREVIEW_PANEL_RE = re.compile(r"\s{3,}[┌└│├].*$")
_BOX_CHARS = "│┃┌┐└┘├┤┬┴┼─━"
# 框线顶到行首（前面没有正文）。两种情况都会这样：
#   选项自己没文字，preview 右列占满整行  →  `  3. │  建议 "iew-changes"  │`
#   preview 内容跨行续行                  →  `│ --ansi），置灰信息…`
# 实测后果比「文字被污染」更糟：_strip_table_pipes 把竖线剥掉后，preview
# 内容伪装成了选项文字，编号连续性被打乱，**整组选项直接丢失**。
_LEADING_PANEL_RE = re.compile(r"^\s*[┌└│├┐┘┤].*$")
# 序号还在、但序号后面直接是面板：`  3. │  建议 "iew-changes"  │`
# 选项自己没文字时会这样。序号要留（否则编号断档整组丢），面板要剥。
_INDEX_THEN_PANEL_RE = re.compile(r"^(\s*\d+[.．]\s*)[┌└│├┐┘┤].*$")
# 选项文字被 preview 面板完全遮住时的占位符。宁可显示「看不到」，也不能
# 让这一项消失——消失了人就少一个答案可点，而且编号会跟屏幕错开。
PANEL_HIDDEN_LABEL = "（选项文字被预览面板遮住）"


def _is_table_row(stripped: str) -> bool:
    """这行是表格而不是 preview 面板。

    表格是**成对闭合的多列**：`│ a │ b │` 至少三根竖线（两列要三根）。
    preview 面板是单侧起始的一整段，最多首尾两根。用这个区分，比数空格
    可靠——两者的竖线都可能顶在行首。
    """
    return (stripped.startswith("│") and stripped.endswith("│")
            and stripped.count("│") >= 3)


# 并排 diff 的右列：一根竖线之后跟着「行号 + 可选的 +/- + 代码」。
# preview 面板的右列是散文或框线，不会长这样。用它把两者分开——面板要
# 切掉（渲染装饰），diff 右列要留下（那是真内容，用户等着看加了哪行）。
_DIFF_SECOND_COLUMN_RE = re.compile(r"^[│┃]\s*\d+\s*[+\-]?\s*\S")


def strip_preview_panel(line: str) -> str:
    """切掉右侧并排的 preview 面板，只留左边的选项文字。

    只管「左边有正文、右边是面板」这一种。面板内容顶到行首的情况没法
    逐行判断（跟表格行 `│ 内容 │` 长得一样），交给 _drop_panel_block
    按整块处理。

    但右边不一定是面板：宽终端下 diff 也是并排两列，中间一根 │ 隔开，
    右列是「行号 + 代码」。那是内容不是装饰，切掉的话用户对着一句
    「Added 1 line」根本找不到加的是哪行——比粘在一起更糟。所以先认一下
    右段的形态，是 diff 就整行留给 _strip_table_pipes 去拆。
    """
    match = _PREVIEW_PANEL_RE.search(line)
    if not match:
        return line
    head = line[:match.start()]
    if any(ch in head for ch in _BOX_CHARS):
        return line          # 左边已有框线 → 这是表格，不是并排面板
    if _DIFF_SECOND_COLUMN_RE.match(line[match.start():].lstrip()):
        return line          # 右边是 diff 的第二列 → 内容，留着
    return head


# diff 行：缩进 + 行号 + +/-/空格 + 内容。行号是关键——正常 diff 行一定
# 有，被终端软换行折出来的续行一定没有。
_DIFF_NUMBERED_RE = re.compile(r"^\s*\d+\s*[+\-]?\s")
# 续行：有 +/- 标记但没有行号。缩进深浅跟着行号位数变，不能拿它当判据。
_DIFF_CONTINUATION_RE = re.compile(r"^\s{2,}[+\-](?!\s*$)")


def merge_diff_wraps(lines: list[str]) -> list[str]:
    """把被终端软换行折断的 diff 行合并回去。

    终端宽度放不下时，一行 diff 被折成几段，续行没有行号、只留 +/- 标记：

        12 -  核心口径「草稿优先…」…… 这样「A 与 C 改过
           -（有草稿版）、B 未改过…」的链路在 B
        13 -  处不会断开。

    内容没丢，但碎成三截，还容易把「13 处不会断开」误读成独立的一行。
    合并之后一行就是一行。

    判据是「有 +/- 但没行号」。不能只看缩进——缩进深浅跟着行号位数变，
    两位数和三位数行号的续行缩进就不一样。

    开头就是续行（上文被读屏窗口截断）时原样留着：并不到哪去，但丢了
    就少一段内容。
    """
    out: list[str] = []
    for line in lines:
        if (out and _DIFF_CONTINUATION_RE.match(line)
                and not _DIFF_NUMBERED_RE.match(line.lstrip())
                and _DIFF_NUMBERED_RE.match(out[-1].lstrip())):
            # 续行的 +/- 是折行留下的痕迹，不是内容，去掉再接上。
            out[-1] = out[-1].rstrip() + line.lstrip()[1:]
            continue
        out.append(line)
    return out


def _drop_panel_block(lines: list[str]) -> list[str]:
    """把 preview 面板占满整行的那些行整块去掉。

    逐行判断做不到：面板内容顶到行首时（`│  建议 "x"  │`）跟表格行
    (`│ 内容 │`) 形态完全一样。但**上下文**能分开——preview 面板一定
    跟在「右边挂着面板」的行后面，是同一个面板的延续；孤立的
    `│ 内容 │` 前面没有那种行。

    真实故障（群里 03:34 那张卡）：选项 3 自己没文字，面板右列占满整行，
    竖线被 _strip_table_pipes 剥掉后 preview 内容伪装成了选项文字，编号
    连续性被打乱，**整组选项直接丢失**。
    """
    out, in_panel = [], False
    for line in lines:
        # 右边挂着面板 → 面板开始（或仍在面板区内）
        if _PREVIEW_PANEL_RE.search(line):
            head = line[:_PREVIEW_PANEL_RE.search(line).start()]
            if not any(ch in head for ch in _BOX_CHARS):
                in_panel = True
                out.append(line)
                continue
        if in_panel:
            # 序号后面直接跟面板：选项自己没文字。留下序号（编号断档会让
            # 整组被连续性校验丢掉），把面板那段剥掉。
            indexed = _INDEX_THEN_PANEL_RE.match(line)
            if indexed:
                # 只留序号的话 _OPTION_RE 匹配不上（它要求序号后有内容），
                # 这一项会被当成不存在，编号断档 → 整组被连续性校验丢掉。
                # 填个占位符：文字确实拿不到（终端里就被面板遮住了），但
                # 编号和按钮必须保住，否则人少一个答案可点。
                out.append(f"{indexed.group(1).rstrip()} {PANEL_HIDDEN_LABEL}")
                continue
            if _LEADING_PANEL_RE.match(line):
                # 面板区内、整行以框线打头 → 是面板内容，丢掉
                if "┘" in line or "└" in line:
                    in_panel = False      # 面板底边，到此为止
                continue
        if line.strip() and not _LEADING_PANEL_RE.match(line):
            in_panel = False          # 遇到正常正文，面板结束
        out.append(line)
    return out


def _strip_table_pipes(line: str) -> str:
    """去掉表格行的竖线；并排的 diff 两列则拆成两行。

    行首尾的 │ 在手机上纯占地方；表格的列分隔换成空格，内容才不会挤成
    一坨。但**不能无条件替换**：宽终端下 Claude Code 把 diff 渲染成左右
    两列、中间一根 │ 隔开，替换成空格后两列就粘成了一行——

        13  import java.util.HashMap;      14 +import java.util.HashSet;

    行号错乱、两条 import 挤在一起，diff 根本没法读（用户报的「格式不对」）。

    靠列数分辨：`│ a │ b │` 那种成对闭合的多列才是表格（_is_table_row），
    压平；只有一根竖线的是并排面板，按它切成两行。这个判据比数空格可靠
    ——两者的竖线都可能顶在行首。
    """
    if "│" not in line and "┃" not in line:
        return line
    stripped = line.strip()
    if not _is_table_row(stripped):
        # 单根竖线 = 并排的两列，切开各占一行。行号得跟着自己那段代码走。
        # 缩进要留：diff 在卡片里走代码块，靠缩进对齐读，左列顶到行首而
        # 右列还缩进着的话，同一段 diff 的行号参差不齐，比粘连更晃眼。
        parts = [p.rstrip() for p in re.split(r"[│┃]", line)]
        parts = [p for p in parts if p.strip()]
        if len(parts) > 1:
            return "\n".join(parts)
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
    # 先合并软换行折断的 diff 行，再做别的——折断的续行没有行号，走到
    # 后面会被当成独立行处理，合完再处理就都是完整行了。
    raw_lines = _drop_panel_block(
        merge_diff_wraps((text or "").splitlines()))
    for line in raw_lines:
        line = _strip_scroll_hint(line)
        line = strip_preview_panel(line)
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
# 群名用的彩色符号。卡片里的 _STATUS_ICONS 是黑白的（正文里更克制），
# 群名要彩色：会话列表里灰扑扑的符号扫一眼分不出轻重。
# 颜色语义与 _STATUS_COLORS 一致（red/orange/green/grey）。
_STATUS_GLYPHS = {
    "blocked": "🔴",
    "working": "🟡",
    "done": "🟢",
    "idle": "⚪️",
    "unknown": "⚪️",
}
_IDLE_GLYPH = "⚪️"


def status_glyph(status: str) -> str:
    """状态 → 群名里的彩色符号。没见过的状态回落成「闲着」。"""
    return _STATUS_GLYPHS.get(status or "", _IDLE_GLYPH)


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


def find_bound_chat(bindings: dict[str, str], pane_id: str,
                    authorized) -> str | None:
    """这个 agent 已经有群了吗。

    曾经按群名精确匹配（find_existing_chat），但群名带上状态符号后会变：
    agent 从 working 变 done、群名从「🟡 x」变「🟢 x」，就认不出来了，
    于是给同一个 agent 重复建群。绑定表才是事实源。

    绑定还在但群已不在授权列表（群被解散了）时返回 None——当成没群。
    """
    for chat_id, bound_pane in bindings.items():
        if bound_pane != pane_id:
            continue
        if authorized and chat_id not in authorized:
            continue
        return chat_id
    return None


def plan_chat_provisioning(agents: list[dict],
                           bindings: dict[str, str],
                           authorized=None) -> list[dict]:
    """算出每个 agent 该用哪个群：已绑的复用，缺的才建。

    返回 [{pane_id, project, title, chat_id}]，chat_id 为空表示要新建。
    """
    ordered = index_agents(agents)
    markers = disambiguate_suffixes(ordered)
    plan = []
    for agent in ordered:
        pane_id = str(agent.get("pane_id") or "")
        project = agent.get("project") or agent.get("agent") or "agent"
        marker = markers.get(pane_id, "")
        title = chat_title_for(project, marker,
                              status=agent.get("status", ""))
        plan.append({
            "pane_id": pane_id,
            "project": project,
            "title": title,
            "chat_id": find_bound_chat(bindings, pane_id, authorized) or "",
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
# 也可能带 TUI 左边框：relay 推 blocked 时给的是原始抓屏，没走 clean_pane，
# `│ ❯ 1. 先停下` 这种边框还在。不认它的话整组选项解析不出来，卡片退回
# Yes/Trust/No，按钮和屏幕上问的对不上。
_OPTION_RE = re.compile(r"^[\s│┃|]*[❯>»\*]?\s*(\d{1,2})[.)．]\s+(\S.*)$")
# 从末尾往上找选择器，最多翻这么多行。够深以容纳多组多选项的长选择器，
# 真正的边界靠「连续正文块」判定，不靠行数。
_OPTION_SCAN_LINES = 200
# 选项后面允许跟几行缩进的描述文字——AskUserQuestion 每个选项都带一行说明，
# 一旦把它当成「选择器后面的正文」，整个选择器就被丢掉，卡片一个按钮都没有。
_OPTION_TRAILING_PROSE = 2
_MAX_OPTIONS = 9

# 选择器底部的操作提示行。真实抓屏长这样：
#   Enter to select · Tab/Arrow keys to navigate · Esc to cancel
# 它顶格且不含边框字符，会被当成「选择器已经翻过去了」的正文，把整组丢掉
# ——卡片上一个按钮都不剩。
#
# 短语抄自 herdr 自己的检测 manifest（它判 blocked 用的就是这些）：
#   ~/.local/state/herdr/agent-detection/remote/claude.toml → live_blocked_form
# 那份文件跟着 Claude 版本远程更新，比照单次抓屏归纳可靠。照抓屏猜会漏变体
# （最初就漏了 confirm、↑/↓、set as default 三种），漏一个，那个变体下的
# 选择器就整组丢掉。加新变体时对着 manifest 抄，别自己想。
# 原先要求「整行由已知短语拼满」，结果 Claude 加了一个 `n to add notes`
# 就整行认不出——选择器留在正文里白占额度，选项还可能整组丢掉（实测）。
#
# 短语抄自 herdr 自己的检测 manifest（它判 blocked 用的就是这些）：
#   ~/.local/state/herdr/agent-detection/remote/claude.toml → live_blocked_form
# 那份文件跟着 Claude 版本远程更新，比照单次抓屏归纳可靠。加新变体时对着
# manifest 抄，别自己想。
_SELECTOR_HINT_PHRASES = [
    r"enter to (?:select|confirm|set as default)",
    r"(?:tab/)?arrows? (?:keys )?to navigate",
    r"(?:↑/↓|↑↓|arrows?) to navigate",
    r"esc(?:ape)? to cancel",
    r"^press enter(?:\s+to\s+\w+)?$",
]
_KNOWN_HINT_RE = re.compile("|".join(_SELECTOR_HINT_PHRASES), re.I)
# 脚注里夹的未知短语（`n to add notes`、`s to skip`）长这样：<键> to <动作>。
# 只在「整行已经确认是脚注」之后用它放行未知段，不单独作为判据——否则
# 「navigate to the folder」这种正文会被当成脚注，正文从那行被切断。
_HINT_SEGMENT_RE = re.compile(
    r"^\s*[\w↑↓/+\-]+(?:\s+[\w/+\-]+){0,2}\s+to\s+[\w\s/-]+$", re.I)

# AskUserQuestion 选择器自带的固定尾项，跟着每一组走，不是 agent 问你的内容。
# 按下去会掉进自由输入框而不是选中什么，所以不该出现在卡片上。
# 单选框写作 `Type something.`（带句点），多选框写作 `Type something`（不带）。
_TUI_TAIL_OPTIONS = {"type something.", "type something",
                     "chat about this", "chat about this."}

# 多选框每项前面的复选标记：`[ ] 单测`、`[x] 已选`。它是状态而非选项文字，
# 留着会让按钮显示成「1. [ ] 单测」，白占本就不宽的按钮。
# 只认方括号里为空或单个勾选字符的形式——正文里的 `[bug]` 不能被误摘。
_CHECKBOX_RE = re.compile(r"^\[\s*[xX✓✔·*]?\s*\]\s*")


def strip_checkbox(text: str) -> str:
    """摘掉多选框选项前的 `[ ]` / `[x]` 标记。"""
    return _CHECKBOX_RE.sub("", (text or "").strip())


# 勾选态：`[✔] 单测` 里方括号内非空即已勾。
_CHECKED_RE = re.compile(r"^\[\s*[xX✓✔·*]\s*\]")


def checked_flags(text: str) -> list[bool]:
    """多选框各项当前是否已勾选，按屏幕顺序。

    单选框没有 `[ ]` 标记，返回空列表表示「不适用」——调用处据此区分
    「一个都没勾」和「这压根不是多选框」。
    """
    flags = []
    for group in detect_option_groups(text, keep_markers=True):
        flags = [bool(_CHECKED_RE.match(opt)) for opt in group["options"]]
    if not any(_CHECKBOX_RE.match(o)
               for g in detect_option_groups(text, keep_markers=True)
               for o in g["options"]):
        return []
    return flags


def detect_multiselect(text: str) -> bool:
    """这屏是多选框吗。

    多选框每项前带 `[ ]`；单选框没有。两者按键语义完全不同——多选按数字是
    切换勾选，单选按数字是选中并前进，卡片不能一视同仁。
    """
    return bool(checked_flags(text)) or any(
        _CHECKBOX_RE.match(o)
        for g in detect_option_groups(text, keep_markers=True)
        for o in g["options"])


# 多选提交后的 Review 页。实测抓屏：
#   Ready to submit your answers?
#   ❯ 1. Submit answers
#     2. Cancel
# 它自己也是个编号选择器，_push_next_group 读屏时会误当成「下一组问题」
# 推成卡片——人看到一张莫名其妙的「1. Submit answers / 2. Cancel」，
# 点下去等于替 agent 乱答。识别出来就别再推。
_REVIEW_PAGE_RE = re.compile(r"ready to submit your answers", re.I)
_REVIEW_OPTION_RE = re.compile(r"submit answers", re.I)


def is_review_page(text: str) -> bool:
    """这屏是多选提交后的 Review 确认页吗。"""
    body = text or ""
    if _REVIEW_PAGE_RE.search(body):
        return True
    # 提示语可能被裁掉，退而认选项本身。
    group = current_option_group(detect_option_groups(body))
    return bool(group and any(_REVIEW_OPTION_RE.search(o)
                              for o in group["options"]))


# 顶部 tab 栏：`←  ☒ 第一组  ☐ 第二组  ✔ Submit  →`
# ☒ 是已答，☐ 是未答。Submit 那项不算组。
_TAB_BAR_RE = re.compile(r"[←→].*[☐☒]")
# Claude 自己的未答警告。tab 栏被裁掉时靠它兜底。
_UNANSWERED_WARN_RE = re.compile(
    r"not answered all|未回答|还有.*未答", re.I)


def unanswered_tab_count(text: str) -> int:
    """tab 栏里还有几组没答（☐ 的个数）。没有 tab 栏就是 0。

    单组问题不渲染 tab 栏，别凭空判出未答组——那会让 _push_next_group
    对着单组问题乱按 Tab。
    """
    for line in (text or "").splitlines():
        if _TAB_BAR_RE.search(line):
            return line.count("☐")
    return 0


def review_has_unanswered(text: str) -> bool:
    """这个 Review 页上还有没答完的组吗。

    两个判据都要：tab 栏数 ☐ 最准，但它可能被窄屏裁掉，那时认 Claude
    自己打出的「You have not answered all questions」。
    """
    body = text or ""
    return unanswered_tab_count(body) > 0 or bool(_UNANSWERED_WARN_RE.search(body))


# Review 页里的答案汇总。真实抓屏：
#      ● 第一组：选择一个选项？
#        → A1
_REVIEW_QUESTION_RE = re.compile(r"^\s*●\s*(.+?)\s*$")
_REVIEW_ANSWER_RE = re.compile(r"^\s*→\s*(.+?)\s*$")


def review_answers(text: str) -> list[tuple[str, str]]:
    """从 Review 页里抽出 [(问题, 答案)]。不是 Review 页就返回空。"""
    pairs, pending = [], None
    for line in (text or "").splitlines():
        question = _REVIEW_QUESTION_RE.match(line)
        if question:
            pending = question.group(1)
            continue
        answer = _REVIEW_ANSWER_RE.match(line)
        if answer and pending:
            pairs.append((pending, answer.group(1)))
            pending = None
    return pairs


def build_review_submit_card(pane_id: str, project: str, generation: str,
                             content: str) -> dict:
    """全答完后的提交卡。

    不能把 Review 页原样当选项卡推：手机上就 `1. Submit answers /
    2. Cancel` 两个英文按钮，看不出自己答了什么，点下去等于替 agent 乱答
    ——那正是当初刻意跳过 Review 页的原因。

    所以专门做一张：答案汇总列出来，按钮写明「提交 / 取消」。按键仍是
    屏幕上的 1 和 2，跟 Review 页一一对应。
    """
    answers = review_answers(content)
    if answers:
        summary = "\n".join(f"**{q}**\n　→ {a}" for q, a in answers)
    else:
        summary = "（没解析出答案汇总，请在终端确认）"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text",
                      "content": f"✅ {project} 全部答完，等你提交"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
            {"tag": "action", "actions": [
                _button("✔ 提交答案", action_value(
                    "approval", pane_id, g=generation, k="1"), "primary"),
                _button("取消", action_value(
                    "approval", pane_id, g=generation, k="2"), "danger"),
            ]},
        ],
    }


# 底部状态栏。活着的 Claude Code 总会渲染其中之一——working 时是
# `esc to interrupt`，空闲时是 `manual mode on` / `Context ██░░`。
# 正在等你选的时候反而没有状态栏（选择器把它顶掉了），所以它不能单独
# 当「活着」的判据，必须和「有没有选择器」一起看。
_ALIVE_BAR_RE = re.compile(
    r"esc to interrupt|manual mode|bypass permissions|shift\+tab to cycle|"
    r"Context\s+[█░]|for shortcuts", re.I)


def looks_stuck(content: str) -> bool:
    """agent 是不是卡在了没法交互的状态。

    真机复现：AskUserQuestion 期间多按一个 Enter，工具被取消，屏幕上既没有
    选择器、也没有底部状态栏，打字也进不去。_push_next_group 走到
    `if not current: return` 就静默退出了，群里再没动静——用户看到的是
    「最后提交环节取消了、收不到卡片」，还不知道能做什么。

    三种状态靠两个维度区分（都是真机抓屏）：

        等你选      选择器 有 / 状态栏 无
        正常完成    选择器 无 / 状态栏 有
        卡死        选择器 无 / 状态栏 无   ← 只有这种要报

    空屏返回 False：那是读屏失败，判断不了状态。当成卡死会凭空去中断一个
    好好干活的 agent，比漏报糟得多。
    """
    body = (content or "").strip()
    if not body:
        return False
    if _ALIVE_BAR_RE.search(body):
        return False
    # 选择器还在就不是卡死——包括 Review 页，它也是选择器的一种。
    if is_review_page(body) or detect_option_groups(body):
        return False
    return True


def build_stuck_card(pane_id: str, project: str) -> dict:
    """卡死提示卡。

    光发一句「卡住了」没用——人在手机上，救不了。实测 C-c 能救回来（屏幕
    打出 `User declined to answer questions`，工具干净退出，状态栏回来），
    所以把中断按钮直接放卡上。Escape 试过，没用。
    """
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text",
                      "content": f"⚠️ {project} 卡住了"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "选择器没了，输入也进不去——多半是刚才那个工具被取消了。\n"
                "点下面的中断（Ctrl+C）能让它干净退出，然后就能接着发指令。"}},
            {"tag": "action", "actions": [
                _button("中断（Ctrl+C）", action_value("interrupt", pane_id),
                        "danger"),
            ]},
        ],
    }


def approval_keys(key: str, *, multiselect: bool) -> list[str]:
    """点一个选项按钮要发的按键序列：只发数字，不补 Enter。

    曾经给单选补过 Enter，理由是「数字只移光标、不提交」。那个观察在当时
    的 TUI 上成立，现在（实测 Claude Code v2.1.243）已经不成立了，而且补
    Enter 会造成两种**静默**的错答：

      多组 AskUserQuestion —— 数字键按下即答完并自动前进到下一组，多出来的
      Enter 又推进一格。三组问题答第一组后直接跳到第三组，第二组从没被问过；
      再点一次，整个工具被取消，agent 停在没有输入框的死状态。用户看到的是
      「最后提交环节被取消了、收不到确认卡片」。

      单组 AskUserQuestion / 工具审批框 —— 数字键本身就提交，Enter 落到了
      下一个界面上，等于替 agent 多按了一下。

    三种场景都实测过（见 tests 里的对照记录）：只发数字，全部正常。多选走
    multiselect_submit_steps（Tab → 等 → 1），与这里无关。

    multiselect 参数保留：多选的语义（数字=切换勾选）跟单选本就不同，调用方
    仍按它决定要不要走 _refresh_multiselect，签名不动免得调用方跟着改。
    """
    return [str(key)]


# Tab 切到 Review 页之后，等它渲染出来再按 1。
# 实测：一次性发 ["Tab","1"] 会停在 `Ready to submit your answers?` 上不动
# ——1 赶在 Review 页渲染完之前到达，被丢掉。隔开再发就提交成功。
MULTISELECT_SUBMIT_WAIT_S = 1.2


def multiselect_submit_steps() -> list[dict]:
    """提交多选的分步按键。每步 {"keys": [...], "wait": 发完等几秒}。

    实测（Claude Code v2.1.239）：数字键切换勾选，Enter **不提交**——它只
    切换光标所在项。Tab 进 Review 页（`1. Submit answers / 2. Cancel`），
    在那儿按 1 才真正提交。

    但两个键不能一次发完：Tab 切页要时间，紧跟着的 1 会在 Review 页渲染
    出来之前到达并被丢掉，人就卡在 Review 页上（实测复现）。所以拆两步，
    中间等一下。

    键名都在 relay 的 SAFE_KEYS 白名单里；发别名会被整条拒绝。
    """
    return [
        {"keys": ["Tab"], "wait": MULTISELECT_SUBMIT_WAIT_S},
        {"keys": ["1"], "wait": 0.0},
    ]


def multiselect_submit_keys() -> list[str]:
    """提交多选的按键，扁平版。

    保留给「一次性发完也无所谓」的调用方（比如只想看键名的测试）。真正
    提交请用 multiselect_submit_steps——它把 Tab 和 1 分开发，避免 1 丢失。
    """
    return [k for step in multiselect_submit_steps() for k in step["keys"]]


def detect_option_groups(text: str, keep_markers: bool = False) -> list[dict]:
    """认出所有「正在等你选」的组。

    AskUserQuestion 一次能问好几组，每组都从 1 重新编号；屏幕上通常只渲染
    当前 tab 那一组（见 current_option_group）。仍然解析多组，是因为屏幕上
    可能粘着上一组的残影，得靠「编号回到 1」把它们切开，否则两组的选项会
    连成一串，编号对不上屏幕。

    从末尾反向定位选择器区间，而不是截固定行数的窗口：窗口切在某组中间时，
    那组编号不从 1 起，会被连续性校验整组丢掉，卡片上就少选项。

    每组返回 {"question": 提问行, "options": [选项...]}。
    """
    located = _locate_selector(text)
    if not located:
        return []
    tail, start, last_option_at = located
    return _parse_groups(tail, start, last_option_at, keep_markers)


def _locate_selector(text: str) -> tuple[list[str], int, int] | None:
    """定位末尾的选择器区间，返回 (tail, 起点, 最后一个选项行)。

    解析选项和把选项从正文里摘掉（strip_selector）用的是同一个区间。分成
    两处各判一次的话，「什么算选择器」会慢慢长歪：一边认得的变体另一边不
    认得，卡片上就会出现「按钮有这项、正文里还重复一遍」的错位。
    """
    # 先剥并排 preview 面板。strip_preview_panel 只挂在 clean_pane 上，而
    # blocked 的 prompt 是 relay 直接推来的、没经过清洗（见 _notify_blocked），
    # 于是右列的框线混进了选项文字——实测选项变成
    # `补一行豁免（推荐）           ┌────────`，框线挤掉真正要判断的字。
    # 放在这里而不是各调用方：_locate_selector 是所有解析路径的公共入口，
    # 而 strip_preview_panel 是幂等的，显示路径重复剥一次结果不变。
    lines = [strip_preview_panel(l) for l in (text or "").splitlines()]
    if not lines:
        return None

    tail = lines[-_OPTION_SCAN_LINES:]
    last_option_at = _last_option_line(tail)
    if last_option_at < 0:
        return None
    # 选项要贴着输出末尾。允许后面跟几行缩进说明（每个选项自带一行描述），
    # 但跟着大段正文的是散文里的编号列表，不是选择器。
    if not _is_selector_tail(tail[last_option_at + 1:]):
        return None

    return tail, _selector_start(tail, last_option_at), last_option_at


def strip_selector(text: str) -> str:
    """把末尾的选择器整段摘掉，只留它上面的正文。

    选项已经渲染成按钮了，正文里再留一份就是重复；而 truncate_prompt 只保
    首尾各约 190 字，正文一长，中间的 `⋯ 省略 N 字 ⋯` 恰好盖住问题和选项
    ——屏幕上问的是什么反而看不见（见飞书截图）。摘掉选择器，省下的额度
    留给真正需要人判断的上文。
    """
    located = _locate_selector(text)
    if not located:
        return text or ""
    tail, start, _ = located
    lines = (text or "").splitlines()
    # start 是 tail 内的下标，换算回原文。
    cut = len(lines) - len(tail) + start
    return "\n".join(lines[:cut]).rstrip()


def _option_indent(line: str) -> int:
    """选项行的缩进格数，不含 TUI 左边框和 ❯ 光标。

    边框和光标都不算缩进：`│ \u276f 1. 先停下` 和 `│   2. 继续建群` 在屏幕上
    是对齐的同级选项，算进去就成了不同层级。
    """
    raw = re.sub(r"^[│┃|]+", "", (line or "").replace("\t", "    "))
    indent = len(raw) - len(raw.lstrip(" "))
    cursor = re.match(r"^\s*([❯>»\*])\s*", raw)
    # 光标占的位在别的行是空格，所以按同样宽度折算，保证同级对齐。
    return cursor.end() if cursor else indent


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
        if is_selector_hint(stripped):  # 提示行属于选择器自己
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


# 行首/行尾的 TUI 边框。relay 推 blocked 时给的是原始抓屏，没走 clean_pane，
# `│ Enter to select · Esc to cancel` 这种边框还在——不剥掉，提示行就认不出，
# _is_selector_tail 判定选择器已翻篇，整组选项被丢弃。
_BORDER_RE = re.compile(r"^[\s│┃|]+|[\s│┃|]+$")


def strip_border(line: str) -> str:
    """剥掉一行两端的 TUI 边框字符。"""
    return _BORDER_RE.sub("", line or "")


def is_selector_hint(stripped: str) -> bool:
    """这行是选择器底部的操作提示吗。

    提示行属于选择器自己，不是「选择器之后的正文」。把它当正文的话，
    _is_selector_tail 会判定选择器已翻篇，整组选项被丢弃。

    判据照 herdr 的 manifest（claude.toml 的 live_blocked_form）：
    确认短语 + 取消短语同时出现。此外要求每一段都长得像「x to y」的
    操作提示，否则正常句子里凑巧同时出现这两个词就会被误判，正文会
    从那行被切断。
    """
    line = strip_border(stripped or "").strip()
    if not line:
        return False
    segments = [s.strip() for s in re.split(r"[·•|]", line) if s.strip()]
    if not segments:
        return False
    # 每一段要么是 manifest 认得的已知短语，要么是「<键> to <动作>」形状的
    # 未知短语（`n to add notes`）。整行所有段都得过——只要有一段是普通
    # 正文，这行就不是脚注，不能从这里切断正文。
    if not all(_KNOWN_HINT_RE.search(s) or _HINT_SEGMENT_RE.match(s)
               for s in segments):
        return False
    # 至少要有一段是 manifest 认得的：否则「navigate to the folder」这类
    # 恰好符合「x to y」形状的正文会被误判成脚注。
    return any(_KNOWN_HINT_RE.search(s) for s in segments)


def is_tui_tail_option(text: str) -> bool:
    """这个选项是 TUI 的固定尾项（Type something / Chat about this）吗。

    整行相等才算：正常选项里也可能出现这些词（「Type something into the
    form」是真选项），只搜关键词会误伤。
    """
    return strip_checkbox(text).lower() in _TUI_TAIL_OPTIONS


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
        if not stripped or _is_tui_chrome(stripped) or is_selector_hint(stripped):
            prose_run = 0
            continue
        if line[:1] in (" ", "\t"):  # 选项的描述行，不是边界
            continue
        # 顶格正文：一行可能是某组的提问，连续多行就是选择器上方的输出了。
        prose_run += 1
        if prose_run > _OPTION_TRAILING_PROSE:
            break
    return start


def _parse_groups(lines: list[str], start: int, end: int,
                  keep_markers: bool = False) -> list[dict]:
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
            # 编号连续即可，不强求从 1 起：屏幕滚动时首项会被卷出去，
            # 只剩 `2. / 3.`。强求从 1 起会把整组丢掉，人一个选项都点不到。
            # 仍要求连续递增——散文里的编号列表往往跳号，靠这个挡住。
            if numbers == list(range(numbers[0], numbers[0] + len(numbers))):
                # 先按屏幕编号校验连续性，再摘掉固定尾项：尾项也占编号，
                # 提前摘掉会让 4/5 缺位，整组被连续性校验丢掉。
                # 尾项恒在末尾，摘掉后剩下的仍是 1..n，按钮发的数字依旧对得上屏幕。
                pairs = [(n, t if keep_markers else strip_checkbox(t))
                         for n, t in current if not is_tui_tail_option(t)]
                if len(pairs) >= 2:
                    pairs = pairs[:_MAX_OPTIONS]
                    groups.append({
                        "question": question.strip(),
                        "options": [t for _, t in pairs],
                        # 屏幕上的真实编号。不能用列表下标+1：屏幕滚动把首项
                        # 卷出去时只剩 2./3.，下标+1 会渲染成 1./2.，按钮发的
                        # 数字就跟屏幕错开一位——点了等于答另一个选项。
                        "numbers": [n for n, _ in pairs],
                    })
        current = []
        question = ""

    # 选项的基准缩进取区间内最浅的那一层。比它更深的编号行是选项自己的描述
    # 文字（AskUserQuestion 每项带一行说明，说明里可能自带「1. …2. …」），
    # 混进来会打乱编号序列，连续性校验把整组丢掉——卡片一个按钮都没有。
    # 用相对值而非绝对值：权限提示的选项本身就缩进 5 格（`⎿` 嵌套渲染），
    # 卡死一个绝对上限会把它们整组挡掉。
    base_indent = min(
        (_option_indent(lines[i]) for i in range(start, end + 1)
         if _OPTION_RE.match(lines[i])),
        default=0)

    for index in range(start, end + 1):
        line = lines[index]
        match = _OPTION_RE.match(line)
        if match and _option_indent(line) <= base_indent:
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
    {"group": "看", "name": "git", "args": "[序号]", "desc": "分支 + 未提交的文件"},
    {"group": "看", "name": "status", "args": "", "desc": "连接状态"},
    {"group": "看", "name": "digest", "args": "", "desc": "今日活动统计"},
    {"group": "看", "name": "usage", "args": "", "desc": "Claude 用量（5h 窗 + 本周）"},

    {"group": "干", "name": "send", "args": "<序号> <内容>", "desc": "发指令（也可直接打字）"},
    {"group": "干", "name": "trust", "args": "<序号>", "desc": "批准并总是允许"},
    {"group": "干", "name": "interrupt", "args": "<序号>", "desc": "中断（Ctrl+C）"},
    {"group": "干", "name": "clear", "args": "<序号>", "desc": "清空它的上下文"},
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


# 打错命令时的建议阈值。0.6 是 difflib 的惯例默认值：再低会开始把
# 无关的词硬认成命令，反而更迷惑人。
_SUGGEST_CUTOFF = 0.6


def suggest_command(text: str) -> str | None:
    """把形似命令的错拼映射到最接近的真命令；猜不出就返回 None。

    存在的理由不是便利，而是安全：parse_command 对不认识的命令一律降级成
    自由文本，于是「/raed 3」会被原样粘进终端当指令发给 agent。这里先把
    这类输入拦下来问一句。

    只处理以 / 开头的单词。自由文本必须放过——那才是指挥 agent 的正道。
    """
    import difflib

    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    head = stripped[1:].partition(" ")[0].split("@", 1)[0].lower()
    if not head or head in COMMANDS:
        return None
    # 路径不是打错的命令：/Users/... 这种一眼可分，别去猜。
    if "/" in head or "." in head:
        return None
    matches = difflib.get_close_matches(head, sorted(COMMANDS), n=1,
                                        cutoff=_SUGGEST_CUTOFF)
    return matches[0] if matches else None


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

    def chat_inventory(self) -> list[dict]:
        """机器人所在的全部群，一群一条 {name, chat_id}。

        按 chat_id 保留而不是按群名归并：同名群会互相覆盖，凭空「消失」。
        """
        from lark_oapi.api.im.v1 import ListChatRequest
        rows: list[tuple[str, str]] = []
        page_token = None
        for _ in range(10):  # 上限保护，别翻页翻到天荒地老
            builder = ListChatRequest.builder().page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            response = self.client.im.v1.chat.list(builder.build())
            if not response.success():
                raise RuntimeError(f"列群失败: {response.msg}")
            for item in (getattr(response.data, "items", None) or []):
                rows.append((getattr(item, "name", "") or "",
                             getattr(item, "chat_id", "") or ""))
            page_token = getattr(response.data, "page_token", None)
            if not getattr(response.data, "has_more", False):
                break
        inventory = chat_inventory(rows)
        dupes = duplicate_named_chats(inventory)
        if dupes:
            # 同名群通常是改名逻辑出问题的残留，值得留一行日志。
            log.warning("发现 %d 个同名群，可能是重复建群的残留: %s",
                        len(dupes), dupes[:20])
        return inventory

    def list_chats(self) -> dict[str, str]:
        """{群名: chat_id}，供按名字复用现成的群。

        同名群只留一个——要完整清单用 chat_inventory()。
        """
        return chat_name_index(self.chat_inventory())

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

    def add_bot_to_chat(self, chat_id: str, app_id: str) -> None:
        """把另一个应用的机器人拉进群。

        机器人不能用 open_id 加——open_id 是按应用隔离的，主应用拿到的
        observer open_id 在 im 接口那边会被判为 "open_id cross app"。
        按 app_id 加才是机器人的正路。
        """
        from lark_oapi.api.im.v1 import (CreateChatMembersRequest,
                                         CreateChatMembersRequestBody)
        request = (CreateChatMembersRequest.builder()
                   .chat_id(chat_id)
                   .member_id_type("app_id")
                   .request_body(CreateChatMembersRequestBody.builder()
                                 .id_list([app_id])
                                 .build())
                   .build())
        response = self.client.im.v1.chat_members.create(request)
        if not response.success():
            raise RuntimeError(f"拉机器人进群失败: {response.msg}")

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

    def set_chat_description(self, chat_id: str, description: str) -> None:
        """改群描述。

        群公告是 docx 类型、API 改不了（im/v1/chats 返回的字段里没有公告），
        描述是唯一能写的地方，显示在群信息页。
        """
        from lark_oapi.api.im.v1 import UpdateChatRequest, UpdateChatRequestBody
        request = (UpdateChatRequest.builder()
                   .chat_id(chat_id)
                   .request_body(UpdateChatRequestBody.builder()
                                 .description(description).build())
                   .build())
        response = self.client.im.v1.chat.update(request)
        if not response.success():
            raise RuntimeError(f"改群描述失败: {response.msg}")

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
        env_chats = set(parse_chat_list(chat_id))
        self.chat_store = ChatIdStore()
        self.chat_store.seed(env_chats)
        self.chat_ids = self.chat_store.all() or env_chats
        # 按人授权：自己发的消息在哪个群都放行，省掉手工登记 chat_id。
        self.user_ids = parse_user_ids(USER_IDS)
        # 本次进程已收养过的群，避免每条消息都去调一次拉人接口。
        self._adopted: set[str] = set()
        # 没有「主群」这个概念：通知只发显式绑过的群（见 chats_watching）。
        # 曾经有过主群回落，但主群自己也会被某个 pane 绑走，无主通知因此串群。
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
        # 群名改名节流器。启动时的群名基线在 main() 里填（要打 API）。
        self.renamer = ChatRenamer()
        # 群描述独立节流：与群名各算各的防抖，否则一个改了另一个被压住。
        self.describer = ChatRenamer()
        # 分支带 TTL 缓存：描述同步挂在 2 秒一帧的状态循环上。
        self.branches = BranchCache()
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

    def invite_observer(self, chat_id: str) -> None:
        """把 observer 机器人拉进新群。

        失败只记日志、不打断建群：群本身是可用的，缺 observer 只是少了
        质检——比建群整个失败要好得多。但必须记，否则这个群会永远处于
        「看着正常、其实没人检查」的状态。
        """
        # 实例属性只做覆盖（测试注入用）；为空时回落到模块常量，这样
        # patch 模块常量的既有测试仍然有效。
        app_id = getattr(self, "_observer_app_id", "") or OBSERVER_APP_ID
        if not app_id:
            return
        try:
            self.api.add_bot_to_chat(chat_id, app_id)
            log.info("observer 已加入新群 %s", chat_id)
        except Exception as exc:
            log.warning("observer 未能加入 %s: %s —— 该群不会被质检",
                        chat_id, scrub(exc))

    def adopt_chat(self, chat_id: str) -> None:
        """收养一个靠用户白名单放行的群：登记 + 拉 observer。

        只登记不拉 observer 的话，这个群会「看着正常、其实没人质检」——
        datapilot6 就是这么坏的（群建在自动拉 observer 之前）。按人授权
        省掉了手工登记，这里必须把 observer 一起补上，否则等于把那个坑
        从偶尔踩变成每次都踩。

        幂等：每条消息都会走到这里，已收养过的直接返回，不然会对同一个
        群反复调拉人接口。
        """
        chat_id = str(chat_id or "")
        if not chat_id or chat_id in self.chat_ids:
            return
        if chat_id in self._adopted:
            return
        self._adopted.add(chat_id)
        self.chat_ids.add(chat_id)
        self.chat_store.add(chat_id)
        log.info("收养新群 %s（发消息的人在用户白名单里）", chat_id)
        self.invite_observer(chat_id)

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
        # sender 一定要打：按人授权配错 open_id 的表现是「机器人装死」，
        # 没有这一行就只能靠猜（open_id 按应用隔离，很容易填错来源）。
        log.info("inbound message chat=%s sender=%s type=%s mention=%s text=%r",
                 ctx.chat_id, ctx.sender_open_id, ctx.chat_type,
                 ctx.mentioned_bot, ctx.text[:80])
        if not ctx.message_id or not self.seen.add(ctx.message_id):
            log.info("  dropped: duplicate or missing message_id")
            return  # 飞书会重推，去重后才处理
        if not should_handle(ctx, self.api.bot_open_id, self.chat_ids,
                             self.user_ids):
            log.info("  dropped: gate rejected (authorized=%s)", self.chat_ids or "any")
            return
        # 放行了但群没登记过：靠用户白名单进来的新群，收养它。
        self.adopt_chat(ctx.chat_id)
        self._dispatch(ctx)

    def on_card_action(self, event: dict) -> None:
        ctx = parse_card_action(event)
        log.info("inbound card action chat=%s value=%s",
                 ctx.chat_id if ctx else None, ctx.action if ctx else None)
        if ctx is None:
            log.info("  dropped: no action value")
            return
        if not should_handle(ctx, self.api.bot_open_id, self.chat_ids,
                             self.user_ids):
            log.info("  dropped: gate rejected")
            return
        self.adopt_chat(ctx.chat_id)
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
            # 文本列表 + 卡片都发，两条路都留着：
            #   - 列表带序号，能扫全局状态，也支撑 /read 3 这种直达；
            #   - 卡片能点，省掉手机上打字那一步。
            # 序号只能来自 format_agent_list（用 index_agents），不能按卡片
            # 按钮的位置标——picker 用 sorted_agents，两者顺序不一定相同。
            self.reply_text(ctx.chat_id, format_agent_list(self.agents))
            if not self.agents:
                return  # 空卡片只是个空壳。
            self.reply_card(ctx.chat_id, build_agent_picker_card(
                "agent_menu", self.agents,
                title=f"点一个直接操作（共 {len(self.agents)} 个）"))
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

        if command == "git":
            await self._handle_git(ctx, rest)
            return

        if command in ("read", "reply", "send", "trust", "interrupt", "clear"):
            await self._handle_agent_command(ctx, command, rest)

    async def _handle_agent_command(self, ctx: MessageContext, command: str, rest: str) -> None:
        pick_action = {
            "read": "read", "reply": "select_reply", "send": "select_send",
            "trust": "trust", "interrupt": "interrupt", "clear": "clear",
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
        elif command == "clear":
            await self._clear_context(ctx.chat_id, agent)
        elif command == "send":
            if not payload.strip():
                self._prompt_for_reply(ctx.chat_id, agent)
                return
            await send_text_to_relay(pane_id, payload.strip())
            self.audit(ctx.chat_id, "send", agent, payload.strip())
            self.set_active(ctx.chat_id, pane_id, agent.get("project"))
            self.reply_text(ctx.chat_id, f"→ 已发给 {agent.get('project')}")
            self._maybe_autowatch(ctx.chat_id, pane_id, agent.get("project") or "")

    async def _clear_context(self, chat_id: str, agent: dict) -> None:
        """清掉 agent 自己的对话上下文，等价于在终端里手打 /clear。

        走 send_text 而不是 send_keys：后者受 relay 的 SAFE_KEYS 白名单限制，
        "/clear" 这种非按键名会被整条拒绝，而用户看到的却是「已清空」——
        Telegram 版踩过同样的坑（见 send_keys_to_relay 的注释）。

        命令路径和卡片按钮共用这里，别再各写一份：/trust 就是分两处写的，
        两边文案已经不一致了。
        """
        await send_text_to_relay(agent["pane_id"], "/clear")
        self.audit(chat_id, "clear", agent)
        self.reply_text(chat_id, f"→ 已清空 {agent.get('project')} 的上下文")

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

    async def _handle_git(self, ctx: MessageContext, rest: str) -> None:
        """/git [序号] —— 分支 + 未提交的文件。

        不带序号时用本群绑的 agent：一群一 agent 是常态，每次还要报序号
        纯属多余。本群没绑又没给序号，才让人挑。
        """
        agent = None
        query = rest.strip()
        if query:
            agent = match_agent(self.agents, query)
            if agent is None:
                self.reply_text(ctx.chat_id, f"没有匹配 '{query}' 的 agent。")
                return
        else:
            pane_id = self._active.get(ctx.chat_id) or self.staged_pane(ctx.chat_id)
            if pane_id:
                agent = find_agent(self.agents, pane_id)
            if agent is None:
                if not self.agents:
                    self.reply_text(ctx.chat_id, "还没有 agent。")
                    return
                self.reply_card(ctx.chat_id, build_agent_picker_card(
                    "git", self.agents, title="/git — 选一个 agent"))
                return

        payload = await fetch_git_status(agent["pane_id"])
        project = agent.get("project") or agent.get("agent") or "?"
        self.reply_text(ctx.chat_id,
                        f"{project}\n{format_git_status(payload)}")

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
                self.api.set_chat_name(chat_id, UNBOUND_CHAT_NAME)
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
        plan = plan_chat_provisioning(
            self.agents, self.bindings.as_dict(), self.chat_ids)
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
            self.invite_observer(chat_id)
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
                # 这里**故意不展开**：watch 每隔几秒轮询一次，每轮都按 ↓
                # 会持续动 TUI 焦点，干扰正在干活的 agent。想看折叠的内容
                # 用 /read（那是一次性的）。
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

        # 形似命令的错拼在这里拦住。不拦的话 parse_command 已经把它降级成
        # 自由文本，接下来会被原样粘进终端——实际发生过：「/raed 3」进了
        # pane。猜不出来的照旧放行，免得挡住正常指挥。
        typo = suggest_command(text)
        if typo:
            self.reply_text(
                ctx.chat_id,
                f"没有 /{text.strip()[1:].partition(' ')[0]} 这个命令，"
                f"你是不是想用 **/{typo}**？\n"
                f"确认要把这行原样发给 agent 的话，去掉开头的 / 再发一次。",
            )
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
            # 多步骤选项卡：答完一组还有下一组，得接着推。点按钮那条路径
            # （_approve）一直有这一步，打数字这条漏了——AskUserQuestion 问
            # 两组时，用打字答完第一组后群里再没动静，看着就像坏了。
            await self._push_next_group(ctx.chat_id, pane_id)
            return

        # 超长就分段：以前直接抛 ValueError，群里静默，消息等于丢了。
        pieces = split_send_text(text)
        for piece in pieces:
            await send_text_to_relay(pane_id, piece)
        self.audit(ctx.chat_id, "send", agent, text)
        suffix = f"（{len(pieces)} 段）" if len(pieces) > 1 else ""
        self.reply_text(ctx.chat_id, f"→ 已发给 {project}{suffix}")
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

        if action == "agent_menu":
            # 列表上点一个 agent：摊开它能做的事，省掉手打命令那一步。
            self.reply_card(ctx.chat_id, build_agent_actions_card(agent))
        elif action == "read":
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
        elif action == "clear":
            await self._clear_context(ctx.chat_id, agent)
        elif action == "git":
            payload = await fetch_git_status(pane_id)
            project = agent.get("project") or agent.get("agent") or "?"
            self.reply_text(ctx.chat_id,
                            f"{project}\n{format_git_status(payload)}")
        elif action == "approval":
            await self._approve(ctx, data, pane_id)
        elif action == "submit":
            await self._submit_multiselect(ctx, data, pane_id)
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
        # 单选还要补 Enter——数字只是移光标高亮，不补就一直卡着不动。
        multiselect = bool(data.get("m"))
        await send_keys_to_relay(pane_id, approval_keys(key, multiselect=multiselect))
        self.audit(ctx.chat_id, "approve",
                   find_agent(self.agents, pane_id) or {"pane_id": pane_id},
                   f"选项 {key}")
        await asyncio.sleep(1.2)

        # 多选框里数字键只是「切换勾选」，人还要继续勾。此时不能把
        # approval_token 清掉——清了下一次点击就会被当成过期审批拒掉，
        # 于是只能勾中一项。改推一张刷新过勾选态的卡片，让人接着勾。
        # 单选已经补 Enter 提交了，不能走这条：屏幕上若恰好是下一道多选题，
        # 会被误当成「这一题还在勾」而重推卡片。
        if multiselect and await self._refresh_multiselect(ctx.chat_id, pane_id):
            return

        self.approval_tokens.pop(pane_id, None)
        self.reply_text(ctx.chat_id, f"已选 {key}")
        # 多组问题是逐组问的：答完这组，下一组才会显示出来。
        await self._push_next_group(ctx.chat_id, pane_id)

    async def _refresh_multiselect(self, chat_id: str, pane_id: str) -> bool:
        """还停在多选框上就重推一张带最新勾选态的卡片，返回是否推了。

        读 pane 失败时返回 False，退回单选那条路径：宁可把它当已答完，
        也别把人卡在一张永远不刷新的卡片上。
        """
        try:
            raw = await read_pane(pane_id)
        except Exception as exc:
            log.warning("multiselect refresh failed: %s", scrub(exc))
            return True  # 读不到就别动 token：保住卡片，人还能接着勾
        if is_pane_read_error(raw):
            # 读失败不代表人答完了。清掉 token 会让下一次点击被当成过期审批
            # 拒掉，人就卡死在这张卡片上——宁可留着卡片让人继续勾。
            log.warning("multiselect refresh: pane read failed")
            return True
        content = clean_pane(raw)
        if is_review_page(content):
            return False  # 已经翻到 Review 页，不是还在勾的多选框
        if not detect_multiselect(content):
            return False
        current = current_option_group(detect_option_groups(content))
        if not current:
            return False
        agent = find_agent(self.agents, pane_id) or {}
        generation = new_generation()
        self.approval_tokens[pane_id] = generation
        card_id = self.reply_card(chat_id, build_option_card(
            pane_id, agent.get("project") or "agent", current["options"],
            generation, question=current["question"],
            multiselect=True, checked=checked_flags(content)))
        self.remember(chat_id, card_id, pane_id)
        return True

    async def _submit_multiselect(self, ctx: MessageContext, data: dict,
                                  pane_id: str) -> None:
        """提交多选：Tab 进 Review 页，再按 1 确认。

        Enter 是不行的——实测它只切换光标所在项，不提交。
        """
        if not approval_is_current(self.approval_tokens, pane_id, data.get("g")):
            self.reply_text(
                ctx.chat_id,
                "那条审批属于更早的提示，请用最新一条 blocked 通知上的按钮。")
            return
        # 分两步发：Tab 切到 Review 页要时间，紧跟着的 1 会在它渲染出来
        # 之前到达并被丢掉，人就卡在 Review 页上。
        for step in multiselect_submit_steps():
            await send_keys_to_relay(pane_id, step["keys"])
            if step["wait"]:
                await asyncio.sleep(step["wait"])
        self.audit(ctx.chat_id, "approve",
                   find_agent(self.agents, pane_id) or {"pane_id": pane_id},
                   "提交多选")
        self.approval_tokens.pop(pane_id, None)
        self.reply_text(ctx.chat_id, "已提交")
        await asyncio.sleep(1.2)
        await self._push_next_group(ctx.chat_id, pane_id)

    async def _push_next_group(self, chat_id: str, pane_id: str) -> None:
        """答完一组后，若还有下一组就接着推。"""
        try:
            raw = await read_pane(pane_id)
        except Exception as exc:
            log.warning("next-group read failed: %s", scrub(exc))
            return
        if is_pane_read_error(raw):
            log.warning("next-group read failed: pane unreadable")
            return
        content = clean_pane(raw)
        if is_review_page(content):
            # Review 页本身绝不能推成卡片：人会看到「1. Submit answers /
            # 2. Cancel」，点下去等于替 agent 乱答。
            #
            # 但停在 Review 页**不等于**答完了。实测（用户报「第一组之后就
            # 卡了」）：两组问题答完第一组后，Claude Code 不会自动切到第二
            # 组，而是停在 Review 页并打出「You have not answered all
            # questions」，tab 栏里 ☐ 第二组 还空着。原先这里直接 return，
            # 群里就再没动静——看着就是卡住了。
            #
            # 有未答组就切过去，再读一次。用 Left 而不是 Tab——tab 栏
            # 两端的 `←  ☒ 第一组  ☐ 第二组  ✔ Submit  →` 就是提示用左右
            # 键切组，而 Review 页的焦点停在最右的 Submit 上，往左才回到
            # 未答那组。真机实测：Tab 按下去屏幕不动，Left 立刻切过去。
            # 只试一次：按完仍是 Review 页就停手，免得无限按下去。
            if not review_has_unanswered(content):
                # 全答完、停在 Review 页——第三步（提交）就卡在这里。
                # 推一张专用提交卡，别让人干等着。
                agent = find_agent(self.agents, pane_id) or {}
                generation = new_generation()
                self.approval_tokens[pane_id] = generation
                card_id = self.reply_card(chat_id, build_review_submit_card(
                    pane_id, agent.get("project") or "agent",
                    generation, content))
                self.remember(chat_id, card_id, pane_id)
                return
            try:
                await send_keys_to_relay(pane_id, ["Left"])
                raw = await read_pane(pane_id)
            except Exception as exc:
                log.warning("next-group tab failed: %s", scrub(exc))
                return
            if is_pane_read_error(raw):
                return
            content = clean_pane(raw)
            if is_review_page(content):
                return
        groups = detect_option_groups(content)
        current = current_option_group(groups)
        agent = find_agent(self.agents, pane_id) or {}
        project = agent.get("project") or "agent"
        if not current:
            # 没有下一组，通常是答完了——但也可能是工具被取消、agent 卡在
            # 没法交互的状态。原先两种都静默 return，卡住的那种群里再没
            # 动静，人只能干等。能认出来就推张卡，带上能救命的中断按钮。
            if looks_stuck(content):
                log.info("agent looks stuck after answering: %s", pane_id)
                card_id = self.reply_card(
                    chat_id, build_stuck_card(pane_id, project))
                self.remember(chat_id, card_id, pane_id)
            return
        generation = new_generation()
        self.approval_tokens[pane_id] = generation
        card_id = self.reply_card(chat_id, build_options_card(
            pane_id, project, current["options"], generation,
            question=current["question"], content=content))
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
        # 用主动展开的版本：TUI 折叠起来的消息在手机上点不了 (click)，
        # 不展开就永远看不到。这是用户主动 /read，按一下 ↓ 的干扰可接受。
        content = clean_pane(await read_pane_expanded(agent["pane_id"]))
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
                question=current["question"], content=content))
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

# 没绑 agent 的群叫这个：没有状态可显示，也不该顶着别人的符号。
UNBOUND_CHAT_NAME = "herdr"
_CHAT_TITLE_LIMIT = 60


# 未提交文件最多列这么多。几百个文件会把手机屏幕刷爆，而看列表的人真正
# 想知道的是「有没有、大概哪些」，不是逐个数。
GIT_FILE_LIMIT = 40
# 飞书群描述的长度上限。超了整个更新调用会失败，宁可截断。
CHAT_DESC_LIMIT = 100


# 分支缓存有效期。分支变化远比状态变化少，而状态循环每 2 秒一帧——
# 不缓存的话一个 agent 一天要查四万次 git，远程的还得走 SSH。
BRANCH_TTL_S = 300


class BranchCache:
    """pane -> 分支名，带过期。

    查失败也记账（存空串），否则「这不是 git 仓库」会导致每帧都去重试。
    """

    def __init__(self, ttl: float = BRANCH_TTL_S):
        self.ttl = ttl
        self._at: dict[str, tuple[str, float]] = {}

    def get(self, pane_id: str, now: float) -> str | None:
        hit = self._at.get(str(pane_id))
        if hit is None or now - hit[1] > self.ttl:
            return None
        return hit[0]

    def put(self, pane_id: str, branch: str, now: float) -> None:
        self._at[str(pane_id)] = (branch or "", now)

    def due(self, pane_id: str, now: float) -> bool:
        """该去查一次了吗。"""
        hit = self._at.get(str(pane_id))
        return hit is None or now - hit[1] > self.ttl

    def forget(self, pane_id: str) -> None:
        self._at.pop(str(pane_id), None)


def short_branch(branch: str) -> str:
    """去掉 `...origin/x` 这段跟踪信息。

    porcelain 的 `## feat/x...origin/feat/x` 在手机上占掉半行，而上游分支名
    几乎总是本地名的重复。
    """
    return (branch or "").split("...")[0].strip()


_TRACK_RE = re.compile(r"\b(ahead|behind)\s+(\d+)")


def parse_tracking(branch_line: str | None) -> dict:
    """拆开 porcelain 的分支行。

    形如 `feat/x...origin/feat/x [ahead 3, behind 1]`。这段信息 relay 早就
    原样带回来了（_parse_git_porcelain 存的是整行），不需要额外查 git。

    没有上游时（本地新分支从没 push 过）upstream 为空——这和「已同步」是
    两种完全不同的状态，调用方要能区分。
    """
    line = (branch_line or "").strip()
    head, _, rest = line.partition("...")
    upstream = ""
    if rest:
        # `origin/x [ahead 2]` —— 上游名到第一个空格为止
        upstream = rest.split(" ", 1)[0].strip()
    counts = {"ahead": 0, "behind": 0}
    for kind, num in _TRACK_RE.findall(line):
        try:
            counts[kind] = int(num)
        except ValueError:
            pass
    return {"branch": head.strip() or line, "upstream": upstream, **counts}


def format_tracking(track: dict) -> str:
    """把 ahead/behind 说成人话。

    刻意不用「未提交」这个词：ahead 是已经 commit、只是没 push，与未提交的
    文件是两件事，混在一起会让人以为改动还没存下来。
    """
    ahead = track.get("ahead") or 0
    behind = track.get("behind") or 0
    if not track.get("upstream"):
        # 没有上游 ≠ 已同步。一次都没推过的分支必须说出来。
        return "无上游分支（未推送过）"
    if ahead and behind:
        return f"已分叉：{ahead} 个未推送，落后 {behind} 个"
    if ahead:
        return f"{ahead} 个提交未推送"
    if behind:
        return f"落后远端 {behind} 个提交"
    return "与远端同步"


def tracking_badge(track: dict) -> str:
    """群描述用的紧凑标记：`↑2`、`↓1`、`↑3↓1`、`↑?`（无上游）。

    描述有 100 字上限且要塞分支/类型/路径，所以这里不用「N 个提交未推送」
    那种完整措辞，只留符号和数字。
    """
    if not track.get("upstream"):
        return "↑?"
    ahead = track.get("ahead") or 0
    behind = track.get("behind") or 0
    parts = []
    if ahead:
        parts.append(f"↑{ahead}")
    if behind:
        parts.append(f"↓{behind}")
    return "".join(parts)


def format_git_status(payload: dict | None) -> str:
    """把 relay 的 git_status 响应渲染成群里能看的文本。

    relay 那边已经解析好了 branch/files/clean（_parse_git_porcelain），这里
    只管排版，不重复实现 git。
    """
    if not isinstance(payload, dict) or not payload:
        return "(没拿到 git 状态)"
    if not payload.get("ok"):
        return f"git 失败: {payload.get('message') or '未知原因'}"

    track = parse_tracking(payload.get("branch", ""))
    branch = track["branch"]
    files = payload.get("files") or []
    head = f"⎇ {branch}" if branch else "⎇ (游离 HEAD)"
    # 未推送的提交要和未提交的文件并列显示：工作区干净但有 ahead 是最容易
    # 忘的状态，只说「干净」会让人以为活都交出去了。
    head = f"{head}\n↕ {format_tracking(track)}"
    if payload.get("clean") or not files:
        return f"{head}\n工作区干净，没有未提交的改动。"

    lines = [f"{head}\n未提交 {len(files)} 个文件："]
    for item in files[:GIT_FILE_LIMIT]:
        status = (item.get("status") or "?").strip()
        lines.append(f"  {status:<2} {item.get('path', '')}")
    if len(files) > GIT_FILE_LIMIT:
        lines.append(f"  …另有 {len(files) - GIT_FILE_LIMIT} 个（共 {len(files)}）")
    return "\n".join(lines)


def format_chat_description(agent: dict, branch: str = "") -> str:
    """群描述里维护 space 的额外信息：分支、路径、agent 类型。

    群公告是 docx 类型、API 改不了（im/v1/chats 里根本没有公告字段），
    群描述是唯一能写的地方，它显示在群信息页。

    分支没查到时**不写这一项**，而不是写「分支: 未知」——留空比显示一个
    可能过时的值好，看的人不会被误导。

    本地主机不写：本地是常态，占一行纯属噪音；远程必须写，否则会以为在
    本地跑，找错机器。
    """
    agent = agent or {}
    parts = []
    branch = short_branch(branch)
    if branch:
        parts.append(f"⎇ {branch}")
    kind = agent.get("agent") or ""
    if kind:
        parts.append(kind)
    host = (agent.get("host") or "").strip()
    if host and host != "local":
        parts.append(f"@{host}")
    cwd = agent.get("cwd") or ""
    if cwd:
        parts.append(cwd)
    text = " · ".join(parts)
    if len(text) > CHAT_DESC_LIMIT:
        # 超限就从尾部截——尾部是路径，前面的分支/类型信息更值钱。
        text = text[:CHAT_DESC_LIMIT - 1] + "…"
    return text


def chat_title_for(project: str, marker: str = "", status: str = "") -> str:
    """把群名改成「<状态符号> [标记] <项目>」，一眼看出这个群管的是谁、忙不忙。

    曾经用统一的「herdr · 」前缀，但每个群都一样，在会话列表这种窄地方
    纯属浪费。换成状态符号：同样的宽度，还能看出该先处理谁。

    重名 agent 必须带上标记：两个群都叫「🟡 yqg-dw-datapilot」的话，
    会话列表里根本切不对。

    标记放在项目名**前面**：会话列表宽度有限，尾部会被截掉，放后面等于
    看不见。项目名太长时宁可截项目名，也要保住符号和标记。

    status 为空表示调用方还不知道状态，此时不带符号——塞一个假符号
    比不带更糟。
    """
    marker = (marker or "").strip()
    head = f"{marker} " if marker else ""
    glyph = f"{status_glyph(status)} " if status else ""
    room = _CHAT_TITLE_LIMIT - len(glyph) - len(head)
    return glyph + head + (project or "?")[:max(1, room)]


def is_project_chat(name: str) -> bool:
    """这个群名是不是 herdr 的项目群。

    observer 靠这个过滤对账范围。原来的判据是 startswith("herdr")，
    前缀删掉后改看状态符号；「herdr」开头仍然认，一是覆盖 /unbind 之后
    的空闲群，二是改造期间新旧群名并存。
    """
    name = (name or "").lstrip()
    if not name:
        return False
    if name.startswith(UNBOUND_CHAT_NAME):
        return True
    return any(name.startswith(g) for g in set(_STATUS_GLYPHS.values()))


# 改名节流。实测每次改群名都会在群里留一条「XXX 修改群名为…」的系统
# 消息，而 relay 每 2 秒推一次状态（herdr_relay.POLL_INTERVAL）——
# 不节流的话状态抖几下就刷一屏，比原来浪费群名宽度的问题严重得多。
RENAME_DEBOUNCE_S = 30
# 同一群两次改名的最小间隔。兜底，防御没预料到的抖动模式。
RENAME_MIN_INTERVAL_S = 60


class ChatRenamer:
    """决定「现在该不该改这个群的名字」。

    决策与执行分离：decide() 是纯函数式的（now 从外面传进来，不读时钟），
    时间和 IO 都在外层，测试不需要 mock 时钟或网络。

    状态纯内存不落盘：落盘会多一份可能与飞书实际群名不一致的副本。
    启动时用 chat_inventory() 拉一次群名当基线即可。
    """

    def __init__(self, known_names: dict[str, str] | None = None):
        # 飞书上当前的群名。启动时拉一次当基线，之后跟着改名更新。
        self._known: dict[str, str] = dict(known_names or {})
        # chat_id -> (待定的目标名, 该目标名第一次出现的时刻)
        self._pending: dict[str, tuple[str, float]] = {}
        # chat_id -> 上次真正改名的时刻，用于最小间隔
        self._last_rename: dict[str, float] = {}

    def decide(self, chat_id: str, target_name: str,
               status: str, now: float) -> str | None:
        """该改成什么名字，或 None 表示按住不动。

        blocked 是唯一「要人立刻动手」的状态，不等防抖也不守最小间隔——
        等的那半分钟正是最该看见它的时候。其余状态走防抖 + 最小间隔。
        """
        chat_id = str(chat_id)
        if self._known.get(chat_id) == target_name:
            self._pending.pop(chat_id, None)
            return None

        if status == "blocked":
            return self._commit(chat_id, target_name, now)

        pending = self._pending.get(chat_id)
        if pending is None or pending[0] != target_name:
            # 目标名变了，防抖重新计时
            self._pending[chat_id] = (target_name, now)
            return None

        if now - pending[1] < RENAME_DEBOUNCE_S:
            return None

        last = self._last_rename.get(chat_id)
        if last is not None and now - last < RENAME_MIN_INTERVAL_S:
            return None

        return self._commit(chat_id, target_name, now)

    def _commit(self, chat_id: str, target_name: str,
                now: float) -> str:
        """记下「已经改成这个名字了」，返回该改的名字。"""
        self._known[chat_id] = target_name
        self._last_rename[chat_id] = now
        self._pending.pop(chat_id, None)
        return target_name

    def forget(self, chat_id: str) -> None:
        """群解散或解绑后清掉它的状态，别占着内存。"""
        chat_id = str(chat_id)
        self._known.pop(chat_id, None)
        self._pending.pop(chat_id, None)
        self._last_rename.pop(chat_id, None)


def chats_watching(bot: "LarkBot", pane_id: str) -> list[str]:
    """哪些群该收到这个 pane 的通知。只有显式绑过的群，没有回落。

    曾经有过「一个群都没绑就回落到主群」的设计，理由是通知不能丢。但主群
    不是中立的收件箱——它自己也会被某个 pane 绑走，于是无主通知就串了群：
    datapilot6（w1R:p1）没绑任何群，它的进展被倒进了「herdr · herdr-remote」
    群（那个群绑的是 w2B:p1），看的人会以为那是 herdr-remote 的输出。

    串群比丢通知更糟：丢了你还知道要去查，串了你会读到错的东西。没绑的
    agent 用 /agents 主动看。
    """
    return bound_chats(bot, pane_id)


def bound_chats(bot: "LarkBot", pane_id: str) -> list[str]:
    """显式绑到这个 pane 的群。空列表就意味着不发。"""
    return sorted(chat for chat, pane in bot._active.items() if pane == pane_id)


# --- relay 监听 ---

def _notify_blocked(bot: "LarkBot", msg: dict) -> None:
    pane_id = msg.get("pane_id")
    if not pane_id:
        return
    generation = new_generation()
    project = msg.get("project", "")
    prompt = msg.get("prompt", "")
    agent = msg.get("agent", "unknown")

    # 正文超长就拆开发，别砍掉——选择时判断依据在上文里，砍了只能盲选。
    # 摘掉选择器之后再拆：选项已经是按钮，正文里那份是重复的。
    group = current_option_group(detect_option_groups(prompt))
    body = strip_selector(prompt) if group else prompt
    pieces = split_prompt_cards(body)

    # 前置卡片只铺上文，按钮**只挂最后一张**：两组序号并存的话，点哪个都
    # 说不清答的是谁。
    lead_cards = [build_pane_card(project, agent, "blocked", piece)
                  for piece in pieces[:-1]]
    final_card = build_blocked_card(pane_id, agent, project, prompt,
                                   msg.get("options"), generation,
                                   body_override=pieces[-1] or " ")

    bot.approval_tokens[pane_id] = generation
    for chat_id in chats_watching(bot, pane_id):
        # 每条都 remember：用户回复哪一条都得知道发给哪个 pane。
        for card in lead_cards:
            bot.remember(chat_id, bot.reply_card(chat_id, card), pane_id)
        bot.remember(chat_id, bot.reply_card(chat_id, final_card), pane_id)


async def _sync_chat_descriptions(bot: "LarkBot", agents: list[dict],
                                  now: float | None = None) -> None:
    """把 space 的额外信息（分支、路径、agent 类型）写进群描述。

    群公告是 docx、API 改不了（im/v1/chats 里没有公告字段），群描述是唯一
    能写的地方，显示在群信息页。

    改描述和改群名一样会在群里留系统消息，所以复用同一套节流（describer
    是独立的 ChatRenamer 实例，与群名各算各的防抖）。

    分支要走 relay 查 git，所以套一层 TTL 缓存：状态循环每 2 秒一帧，
    不缓存的话一个 agent 一天四万次 git 调用。
    """
    if now is None:
        now = time.time()
    by_pane = {str(a.get("pane_id")): a for a in agents if a.get("pane_id")}
    for chat_id, pane_id in list(bot._active.items()):
        agent = by_pane.get(str(pane_id))
        if not agent:
            continue
        pane_id = str(pane_id)
        branch = bot.branches.get(pane_id, now)
        if branch is None and bot.branches.due(pane_id, now):
            # 先记账再查：查失败也别让下一帧立刻重试。
            bot.branches.put(pane_id, "", now)
            payload = await fetch_git_status(pane_id)
            branch = ""
            if payload.get("ok"):
                track = parse_tracking(payload.get("branch", ""))
                badge = tracking_badge(track)
                # 分支名后跟紧凑标记，缓存整串——描述要的就是这个成品。
                branch = f"{track['branch']} {badge}".strip() if badge else track["branch"]
            bot.branches.put(pane_id, branch, now)
        target = format_chat_description(agent, branch or "")
        if not target:
            continue
        # status 传空串：描述不像群名那样需要 blocked 抢占——描述里没有
        # 状态信息，抢了也看不出区别，只是多刷一条系统消息。
        desired = bot.describer.decide(chat_id, target, "", now)
        if not desired:
            continue
        try:
            bot.api.set_chat_description(chat_id, desired)
        except Exception as exc:
            log.warning("改群描述失败 %s: %s", chat_id, scrub(exc))


def _sync_chat_names(bot: "LarkBot", agents: list[dict],
                     now: float | None = None) -> None:
    """按当前状态刷各群群名，节流器说不改就不改。

    两份 agent 数据各有用处，别混：

    - `agents` 是这一帧刚到的，status 最新，但 agent_update 分支里只有一个。
    - `bot.agents` 是上一帧的全集（调用点在 bot.agents 赋值**之前**），
      status 旧，但重名消歧需要看全集才算得出标记。

    消歧只依赖 project/agent/cwd/workspace_id，不依赖 status，所以用旧全集
    算标记是安全的；agent 集合真变了下一帧就收敛。

    改名失败只记日志：群名是展示，不是功能前提（与 set_active 一致）。
    """
    if now is None:
        now = time.time()
    markers = disambiguate_suffixes(index_agents(bot.agents or agents))
    by_pane = {str(a.get("pane_id")): a for a in agents if a.get("pane_id")}
    for chat_id, pane_id in list(bot._active.items()):
        agent = by_pane.get(str(pane_id))
        if not agent:
            continue  # 这一帧没带这个 pane 的消息，下一帧再说
        project = agent.get("project") or agent.get("agent") or "agent"
        status = agent.get("status", "")
        target = chat_title_for(project, markers.get(str(pane_id), ""),
                                status=status)
        name = bot.renamer.decide(chat_id, target, status, now)
        if not name:
            continue
        try:
            bot.api.set_chat_name(chat_id, name)
        except Exception as exc:
            log.warning("rename chat failed: %s", scrub(exc))


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

        # 只有群绑着它才推：没绑就没有该收这条通知的地方，不发。
        if is_finish_transition(old_status, new_status) and chats_watching(bot, pane_id):
            # 读 pane 要走 relay 往返，不能卡住监听循环——丢到后台跑。
            asyncio.create_task(_notify_finished(bot, dict(agent)))
        bot.prev_statuses[pane_id] = new_status

    # 状态可能变了，群名跟上（节流器决定这次要不要真改）
    _sync_chat_names(bot, updated, now)
    # 描述要查 git，走 relay 往返——不能卡住监听循环。
    asyncio.create_task(_sync_chat_descriptions(bot, updated, now))


async def _notify_finished(bot: "LarkBot", agent: dict) -> None:
    """agent 停下来时主动推：带上输出，卡在选择器就补按钮。"""
    pane_id = agent.get("pane_id")
    project = agent.get("project") or agent.get("agent") or "agent"
    log.info("agent finished, pushing to Lark: %s (%s)", project, pane_id)
    try:
        # 干完活推一次，同样展开折叠的消息——这是一次性的，不像 watch
        # 那样反复按键。
        content = clean_pane(await read_pane_expanded(pane_id))
    except Exception as exc:
        log.warning("finish notify read failed: %s", scrub(exc))
        content = ""

    groups = detect_option_groups(content)
    generation = new_generation() if groups else None
    if generation:
        bot.approval_tokens[pane_id] = generation

    # chats_watching 现在只返回显式绑过的群，所以这里每个 chat_id 本来就是
    # 定向的，刷新绑定是安全的。曾经不是：那时会回落成「发给全部授权群」，
    # 循环里对每个群都 set_active，一次广播就把 16 个群全绑到同一个 pane，
    # 且绑定已落盘、重启也不自愈。回落既已去掉，这个坑的入口也就没了。
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
                question=current["question"], content=content))
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
    # 拉一次群名当节流器基线：不拉的话每次重启都要把所有群名重改一遍，
    # 每群刷一条系统消息。开发期重启频繁，群数又会长到十几个。
    try:
        bot.renamer = ChatRenamer(
            {item["chat_id"]: item.get("name", "")
             for item in api.chat_inventory()})
    except Exception as exc:
        log.warning("拉取群名基线失败，降级为空基线: %s", scrub(exc))
    _start_lark_thread(bot)
    loop.run_until_complete(relay_listener(bot))


if __name__ == "__main__":
    main()
