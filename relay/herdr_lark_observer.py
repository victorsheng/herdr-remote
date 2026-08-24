#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lark-oapi>=1.4.0", "websockets>=14.0"]
# ///
"""herdr-remote 飞书发送质检 —— 盯着 demo 机器人到底发出去了什么。

它不接指令、不碰 agent，只做一件事：把「relay 说该发什么」和「群里实际
发了什么」对上账，对不上就记下来。

    relay (ws) ──→ observer ←── 飞书群消息（demo 实际发出去的）
       期望                        实际
                     ↓
                  对账 → JSONL + 质检群

为什么单独一个进程、单独一个飞书应用：
  - 飞书长连接是「一个应用一条」。同一应用起两个进程会抢连接，事件随机
    投递，现象是「有时候有反应有时候没有」，两边日志都看不出异常。
  - 质检组件和被质检对象同生共死就失去了意义：demo 抛异常挂掉的那一刻，
    恰恰是最该留下记录的时刻。

设计与实施计划见:
  docs/superpowers/specs/2026-08-22-lark-client-design.md
"""
import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, asdict

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("herdr-lark-observer")

APP_ID = os.environ.get("HERDR_LARK_OBSERVER_APP_ID", "")
APP_SECRET = os.environ.get("HERDR_LARK_OBSERVER_APP_SECRET", "")
DOMAIN = os.environ.get("HERDR_LARK_DOMAIN", "feishu")
# 质检群：异常都报到这里，不污染项目群。空则只落盘。
QC_CHAT = os.environ.get("HERDR_LARK_QC_CHAT", "")

CONFIG_DIR = os.path.expanduser("~/.config/herdr-remote")
FINDINGS_PATH = os.environ.get(
    "HERDR_OBSERVER_LOG", os.path.join(CONFIG_DIR, "observer.jsonl"))
# 判过的 message_id 落盘，重启后不重复上报。
SEEN_PATH = os.environ.get(
    "HERDR_OBSERVER_SEEN", os.path.join(CONFIG_DIR, "observer_seen.json"))
# demo 的群↔pane 绑定。只读，用来判断「这条通知本来该不该发」：
# herdr_lark 去掉主群回落后，没有群绑着的 pane 一律不推，observer 得跟上
# 同一个口径，否则每次完成都判一次假漏发。路径与 herdr_lark.BINDING_PATH
# 保持一致——同机同目录，读同一份文件最省事，也不必新开 IPC。
LARK_BINDING_PATH = os.environ.get(
    "HERDR_LARK_BINDING_PATH", os.path.join(CONFIG_DIR, "lark_bindings.json"))

RELAY_WS = os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375")
RELAY_WS_SAFE = RELAY_WS.split("?", 1)[0]
_RELAY_TOKEN = RELAY_WS.split("token=", 1)[1] if "token=" in RELAY_WS else ""

# 期望产生消息后，等多久还没看到就判漏发。demo 要读 pane（走 relay 往返）
# 再渲染卡片，慢的时候要几秒；给足余量，免得把「慢」误判成「漏」。
GRACE_SECONDS = float(os.environ.get("HERDR_OBSERVER_GRACE", "25"))
# 多久扫一次群消息。
POLL_SECONDS = float(os.environ.get("HERDR_OBSERVER_POLL", "10"))
# 每次扫每个群看最近几条。
SCAN_PAGE = 20
# 比这更老的消息不判：启动时不要把群里的历史消息全过一遍。
MAX_MESSAGE_AGE = float(os.environ.get("HERDR_OBSERVER_MAX_AGE", "300"))


def scrub(value) -> str:
    """把密钥从任何将被记录或外发的字符串里抹掉。"""
    text = str(value)
    for secret in (_RELAY_TOKEN, APP_SECRET):
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


# --- 期望 ---

@dataclass
class Expectation:
    """relay 说「此刻该有一条消息发到某个群」。

    kind 决定校验哪些规则：
      finish  —— agent 停下来了，该发完成通知（带 pane 输出）
      blocked —— agent 在等人选，该发审批卡片（必须有按钮）
    """
    kind: str
    pane_id: str
    project: str
    born: float
    # 期望里带 options 就意味着「必须有一张能点的卡片」
    expect_card: bool = False
    options: list = field(default_factory=list)
    matched: bool = False


# --- 校验规则 ---

# 敏感串：这些原样出现在群消息里就是 scrub 失效。
def sensitive_patterns() -> list[tuple[str, str]]:
    pats = []
    if _RELAY_TOKEN:
        pats.append(("relay_token", _RELAY_TOKEN))
    if APP_SECRET:
        pats.append(("observer_app_secret", APP_SECRET))
    demo_secret = os.environ.get("HERDR_LARK_APP_SECRET", "")
    if demo_secret:
        pats.append(("demo_app_secret", demo_secret))
    return pats


# U+FFFD 替换字符：编码坏了的典型痕迹。
_MOJIBAKE = "�"
# 截断标记后面还跟着内容，说明截断位置算错了。
_TRUNCATED_MARK = "…"
# 审计行的形状：<图标> <动作>  <项目> (<pane>)
_AUDIT_RE = re.compile(r"^[→✓🔓⛔✚🏠✂🗑·]\s+\w+\s+\S+\s+\([^)]+\)")


def check_text(text: str) -> list[dict]:
    """对一条实际发出的消息做内容校验。返回问题列表，空表示没毛病。"""
    problems = []
    body = text or ""

    if not body.strip():
        problems.append({"rule": "empty_message",
                         "detail": "消息体为空，手机上看到的是一条空白"})

    for name, secret in sensitive_patterns():
        if secret and secret in body:
            problems.append({"rule": "secret_leak",
                             "detail": f"{name} 原样出现在群消息里，scrub 失效"})

    if _MOJIBAKE in body:
        problems.append({"rule": "mojibake",
                         "detail": "出现 U+FFFD 替换字符，编码坏了"})

    # 审计行超长没截断：format_audit 限 200 字符 + 前缀，宽松放到 400。
    if _AUDIT_RE.match(body) and len(body) > 400:
        problems.append({"rule": "audit_not_truncated",
                         "detail": f"审计行 {len(body)} 字符未截断"})

    # 截断标记出现在开头以外的位置且后面还有大段内容 —— 位置算错了
    idx = body.find(_TRUNCATED_MARK)
    if idx > 0 and len(body) - idx > 3000:
        problems.append({"rule": "truncation_misplaced",
                         "detail": "截断标记后仍有大段内容，截断位置算错"})

    return problems


# 飞书 message.list 对 schema 2.0 卡片只返回渲染降级版，元素树拿不到：
#   {"title": "...", "elements": [[{"tag":"img"...},
#                                  {"tag":"text","text":"请升级至最新版本客户端，以查看内容"}]]}
# 所以「这张卡片有没有按钮」这个问题，用读消息的方式根本回答不了。
# 飞书 API 拿不到卡片元素树时的降级文案。不止一种，实测抓到过这两句：
#   - 请升级至最新版本客户端，以查看内容
#   - 卡片内容不支持查看，请在飞书客户端查看
# 只认一种的话，另一种会被当成真元素树，判成「没有按钮」——observer 一
# 启动就把历史卡片刷成一屏假警报（实测：连报 10 条 card_no_buttons）。
# 加新变体时照抓屏原文抄，别自己改写措辞。
_CARD_DEGRADED_MARKS = (
    "请升级至最新版本客户端",
    "卡片内容不支持查看",
)


def card_is_degraded(content: dict) -> bool:
    """这份卡片内容是不是 API 的降级返回（拿不到真实元素树）。

    实际踩过：拿降级内容去判「有没有 button」，把正常的 DONE 卡片和
    审批卡片全判成死卡片——一晚上刷 8 条假警报。降级内容一律跳过。
    """
    raw = json.dumps(content, ensure_ascii=False)
    return any(mark in raw for mark in _CARD_DEGRADED_MARKS)


# herdr_lark.STATUS_LABELS 的值。build_pane_card 会把它们作为独立的文本元素
# 放在卡片第一行，这是「输出展示卡片」的结构签名。
# 与 herdr_lark.py 保持同步：那边加状态就往这里加。
_PANE_CARD_LABELS = ("DONE", "WORKING", "IDLE", "NEEDS YOU")

# herdr_lark.py 的 _STATUS_GLYPHS 的值 + UNBOUND_CHAT_NAME。
# observer 是独立进程、不 import herdr_lark，所以这里留一份副本。
# 与 herdr_lark.py 保持同步：那边改符号就往这里改。
_PROJECT_CHAT_GLYPHS = ("🔴", "🟡", "🟢", "⚪️")
_UNBOUND_CHAT_NAME = "herdr"


def is_project_chat(name: str) -> bool:
    """这个群是不是 herdr 的项目群。

    群名以状态符号开头（herdr_lark 的 chat_title_for 生成），或者是
    没绑 agent 的「herdr」。别的群（比如有人把机器人拉进了闲聊群）
    里的消息不参与对账。

    判错的后果是静默的：把项目群判成无关群，漏发检测什么都不查了
    但也不报错。所以 tests 里有一条断言符号表与 herdr_lark 一致。
    """
    name = (name or "").lstrip()
    if not name:
        return False
    if name.startswith(_UNBOUND_CHAT_NAME):
        return True
    return any(name.startswith(g) for g in _PROJECT_CHAT_GLYPHS)


def card_is_output_only(content: dict) -> bool:
    """这张卡片是不是「只展示输出」的那类，本来就不该有按钮。

    herdr_lark.build_pane_card 生成的完成/进展卡片按设计不含 button——它只是
    把 pane 输出贴出来看。拿「交互卡片都该有按钮」去要求它，就会刷假警报：
    实测 66 条质检记录里 54 条都是这个形态。

    判据用结构不用文案：状态标签（DONE / WORKING / …）作为独立文本元素出现，
    是 build_pane_card 的签名。文案会改，这个结构不会。

    审批卡片和选择卡片不在此列——它们必须有按钮，缺了就是真问题。
    """
    for row in (content or {}).get("elements") or []:
        # 飞书降级返回把元素套成二维数组，真实卡片是一维；两种都走一遍。
        cells = row if isinstance(row, list) else [row]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            if cell.get("tag") != "text":
                continue
            if (cell.get("text") or "").strip() in _PANE_CARD_LABELS:
                return True
    return False


def card_has_buttons(content: dict) -> bool:
    """卡片里有没有能点的按钮。

    只在拿到**真实**元素树时才有意义；降级内容请先用 card_is_degraded
    过滤掉，否则结论没有意义。
    """
    raw = json.dumps(content, ensure_ascii=False)
    return '"button"' in raw or '"tag": "button"' in raw


# --- 落盘 ---

class SeenStore:
    """记住已经判过的 message_id，且要落盘。

    只放内存的话，服务一重启就把群里的历史消息重判一遍——实际发生过：
    同一条消息被报了 3 次，质检群刷屏，人就开始忽略它了。
    """

    def __init__(self, path: str = None, limit: int = 5000):
        self.path = path or SEEN_PATH
        self.limit = limit
        self._ids: "OrderedDict[str, None]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                for mid in json.load(fh):
                    self._ids[mid] = None
            log.info("已加载 %d 个判过的 message_id", len(self._ids))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            pass

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(list(self._ids), fh)
        except Exception as exc:
            log.warning("去重落盘失败: %s", scrub(exc))

    def add(self, message_id: str) -> bool:
        """没见过就记下并返回 True；见过返回 False。"""
        if not message_id or message_id in self._ids:
            return False
        self._ids[message_id] = None
        if len(self._ids) > self.limit:
            for _ in range(self.limit // 2):
                self._ids.popitem(last=False)
        self._save()
        return True

    def __contains__(self, message_id) -> bool:
        return message_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


class FindingStore:
    """质检结论一行一条追加落盘。

    JSONL 而不是数据库：出问题时要能直接 grep/tail，也方便事后回放。
    """

    def __init__(self, path: str = FINDINGS_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._count = 0

    def write(self, record: dict) -> None:
        record.setdefault("ts", time.time())
        record.setdefault("ts_iso", time.strftime("%Y-%m-%dT%H:%M:%S",
                                                  time.localtime(record["ts"])))
        # 样本里可能正好带着泄露的密钥——那是我们要报的问题，但不能
        # 因为报它而把密钥又抄进落盘文件和质检群里。
        if record.get("sample"):
            record["sample"] = scrub(record["sample"])
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._count += 1
        except Exception as exc:
            log.warning("落盘失败: %s", scrub(exc))

    def __len__(self) -> int:
        return self._count


# --- 飞书侧 ---

class ObserverAPI:
    """只用两个能力：读群消息、往质检群发消息。"""

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu"):
        import lark_oapi as lark
        self._lark = lark
        dom = lark.FEISHU_DOMAIN if domain == "feishu" else lark.LARK_DOMAIN
        self.client = (lark.Client.builder()
                       .app_id(app_id).app_secret(app_secret)
                       .domain(dom).log_level(lark.LogLevel.WARNING).build())

    def list_chats(self) -> dict:
        """{chat_id: name}——注意方向，别和 herdr_lark.py 的 {name: id} 搞反。"""
        from lark_oapi.api.im.v1 import ListChatRequest
        out, token = {}, None
        while True:
            b = ListChatRequest.builder().page_size(100)
            if token:
                b = b.page_token(token)
            resp = self.client.im.v1.chat.list(b.build())
            if not resp.success():
                raise RuntimeError(f"列群失败: {resp.code} {resp.msg}")
            for item in (resp.data.items or []):
                out[item.chat_id] = item.name or ""
            token = getattr(resp.data, "page_token", None)
            if not token or not getattr(resp.data, "has_more", False):
                break
        return out

    def recent_messages(self, chat_id: str, limit: int = SCAN_PAGE) -> list[dict]:
        """拉一个群最近的消息，新的在前。"""
        from lark_oapi.api.im.v1 import ListMessageRequest
        req = (ListMessageRequest.builder()
               .container_id_type("chat").container_id(chat_id)
               .sort_type("ByCreateTimeDesc").page_size(limit).build())
        resp = self.client.im.v1.message.list(req)
        if not resp.success():
            raise RuntimeError(f"读群消息失败: {resp.code} {resp.msg}")
        out = []
        for item in (resp.data.items or []):
            try:
                content = json.loads(item.body.content) if item.body else {}
            except (json.JSONDecodeError, TypeError):
                content = {}
            out.append({
                "message_id": item.message_id,
                "msg_type": item.msg_type,
                "create_time": int(item.create_time or 0) / 1000.0,
                "sender": getattr(item.sender, "id", "") or "",
                "content": content,
                "text": content.get("text", "") if isinstance(content, dict) else "",
            })
        return out

    def send_text(self, chat_id: str, text: str) -> str:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        req = (CreateMessageRequest.builder().receive_id_type("chat_id")
               .request_body(CreateMessageRequestBody.builder()
                             .receive_id(chat_id).msg_type("text")
                             .content(json.dumps({"text": text}, ensure_ascii=False))
                             .build())
               .build())
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            raise RuntimeError(f"发送失败: {resp.code} {resp.msg}")
        return resp.data.message_id


# --- 质检主体 ---

def format_finding(record: dict) -> str:
    """一条质检结论，发到质检群的样子。

    两类结论字段不一样，别硬套一个模板：
      漏发/缺卡片 —— 有 pane/project/等了多久，没有具体消息
      内容异常   —— 有群名/消息样本，没有 pane（是从消息反查的）
    """
    icons = {"missing": "🚨", "content": "⚠️", "card_missing": "🔘", "ok": "✅"}
    verdict = record.get("verdict", "?")
    icon = icons.get(verdict, "·")

    if verdict in ("missing", "card_missing"):
        head = (f"{icon} {verdict}  {record.get('project') or '?'} "
                f"({record.get('pane_id') or '?'})")
        lines = [head, f"   期望: {record.get('kind') or '?'}"]
    else:
        head = f"{icon} {verdict}  {record.get('chat') or '?'}"
        lines = [head]

    for p in record.get("problems", []):
        lines.append(f"   规则 {p.get('rule')}: {p.get('detail')}")
    if record.get("sample"):
        sample = str(record["sample"]).replace("\n", " ⏎ ")[:120]
        lines.append(f"   样本: {sample}")
    if record.get("note"):
        lines.append(f"   {record['note']}")
    return "\n".join(lines)


class Observer:
    """把期望和实际对上账。"""

    def __init__(self, api: ObserverAPI, store: FindingStore, qc_chat: str = "",
                 seen: "SeenStore | None" = None, binding_path: str = None):
        self.api = api
        self.store = store
        self.qc_chat = qc_chat
        # demo 的绑定表，每次判定时重读：用户 /read 换绑后不该等 observer 重启。
        self.binding_path = binding_path or LARK_BINDING_PATH
        # 待核对的期望
        self.pending: list[Expectation] = []
        # pane_id -> 上一次看到的状态，用来认出「停下来了」
        self.prev_statuses: dict = {}
        # 已经判过的 message_id，落盘，重启后不重判
        self.seen_messages = seen if seen is not None else SeenStore()
        # chat_id -> name
        self.chats: dict = {}
        self.stats = {"expect": 0, "matched": 0, "missing": 0,
                      "content": 0, "card_missing": 0, "checked": 0,
                      # 没绑群、本来就不该发的。这个数大不是坏事，但突然
                      # 变大意味着有人的绑定被清掉了。
                      "skipped_unbound": 0}

    # --- 期望侧：从 relay 事件产生 ---

    def bound_panes(self) -> set:
        """当前被某个群绑着的 pane。每次都重读文件。

        缓存住的话，用户 /read 换绑后 observer 会拿着过期的绑定判一阵子，
        而期望与实际错位正是最容易出假警报的地方。文件很小，重读的成本
        远低于误报的代价。

        读不到、格式不对都当「没有绑定」：质检工具不能因为一个坏文件挂掉。
        反过来假设「都绑着」更糟——那会把所有静默的 pane 全判成漏发。
        """
        try:
            with open(self.binding_path) as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return set()
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("绑定表读不出，当作没有绑定: %s", scrub(exc))
            return set()
        if not isinstance(payload, dict):
            log.warning("绑定表结构不对，当作没有绑定")
            return set()
        return {str(v) for v in payload.values() if isinstance(v, str)}

    def note_expectation(self, kind: str, agent: dict,
                         options: list | None = None) -> None:
        pane_id = agent.get("pane_id") or "?"
        # 没有群绑着它，herdr_lark 就不会推（主群回落已去掉）。这里跟着同
        # 一个口径，否则每次状态变化都判一次假漏发——实测 datapilot6 没绑
        # 任何群，反复被判 missing。
        if pane_id not in self.bound_panes():
            log.info("跳过期望: %s %s (%s) 没有群绑着它",
                     kind, agent.get("project") or "?", pane_id)
            self.stats["skipped_unbound"] += 1
            return
        exp = Expectation(
            kind=kind, pane_id=pane_id,
            project=agent.get("project") or agent.get("agent") or "?",
            born=time.time(),
            expect_card=bool(options), options=list(options or []),
        )
        self.pending.append(exp)
        self.stats["expect"] += 1
        log.info("期望 +1: %s %s (%s)%s", kind, exp.project, pane_id,
                 " 需卡片" if exp.expect_card else "")

    def track(self, agents: list) -> None:
        """看 agent 状态变化，认出该产生消息的时刻。

        判定口径必须和 herdr_lark.py 的 is_finish_transition 一致，
        否则对账会系统性错位：它不推的我却在等，全变成假漏发。
        """
        for agent in agents:
            pane_id = agent.get("pane_id")
            if not pane_id:
                continue
            new_status = agent.get("status", "unknown")
            old_status = self.prev_statuses.get(pane_id)
            # 首次见到不算（启动时一屋子 idle 不该各推一条）
            if (old_status in ("working", "blocked")
                    and new_status in ("idle", "done")
                    and old_status != new_status):
                self.note_expectation("finish", agent)
            self.prev_statuses[pane_id] = new_status

    # --- 实际侧：扫群消息 ---

    def scan_once(self) -> None:
        """扫一遍群消息，把期望划掉，同时对每条新消息做内容校验。"""
        if not self.chats:
            return
        now = time.time()
        for chat_id, name in self.chats.items():
            if chat_id == self.qc_chat:
                continue          # 别把自己的质检报告当成待检消息
            try:
                messages = self.api.recent_messages(chat_id)
            except Exception as exc:
                log.warning("扫群失败 %s: %s", name, scrub(exc))
                continue
            for msg in messages:
                self._check_message(chat_id, name, msg, now)
        self._sweep_expired(now)

    def _check_message(self, chat_id: str, name: str, msg: dict, now: float) -> None:
        mid = msg["message_id"]
        # 太老的消息直接跳过，且不记进去重表——否则启动时一次扫描就把
        # 去重表塞满 20×群数 个陈旧 id，把真正该记的挤出去。
        if msg["create_time"] and now - msg["create_time"] > MAX_MESSAGE_AGE:
            return
        if not self.seen_messages.add(mid):
            return          # 判过了

        self.stats["checked"] += 1
        problems = []
        if msg["msg_type"] == "text":
            problems = check_text(msg["text"])
        elif msg["msg_type"] == "interactive":
            # 降级返回看不到元素树，任何结论都是瞎猜——跳过，别造假警报。
            # 输出展示卡片（DONE / WORKING 那类）按设计就没有按钮，也跳过：
            # 它们占了群里绝大多数卡片，误判一次就刷一屏。
            if (not card_is_degraded(msg["content"])
                    and not card_is_output_only(msg["content"])):
                if not card_has_buttons(msg["content"]):
                    problems.append({"rule": "card_no_buttons",
                                     "detail": "交互卡片里没有任何按钮，手机上点不了"})

        if problems:
            self.stats["content"] += 1
            self.report({
                "verdict": "content", "chat": name, "chat_id": chat_id,
                "message_id": mid, "msg_type": msg["msg_type"],
                "problems": problems,
                "sample": (msg["text"] or "")[:200],
            })

        # 划掉期望：这条消息把某个 pane 的期望满足了吗
        self._match(chat_id, name, msg)

    def _match(self, chat_id: str, name: str, msg: dict) -> None:
        """把消息和待核对的期望配上。

        只认**群名里点名了这个项目**的群：一个群绑一个 agent，群名就是
        「herdr · <项目>」。不校验群的话，A 群的消息会划掉 B 群 agent 的
        期望，把真实漏发掩盖成「已满足」。

        群内再按项目名匹配消息。这不是精确匹配（pane 输出里可能偶然出现
        项目名），但方向是安全的：宁可放过，不要造假漏发。
        """
        if not self._chat_covers(name, chat_id):
            return
        blob = json.dumps(msg["content"], ensure_ascii=False)
        for exp in self.pending:
            if exp.matched:
                continue
            if not exp.project:
                continue
            # 两个条件都要满足：
            #   群要对得上——A 群的消息不能划掉 B 群 agent 的期望
            #   消息内容里要点到这个项目——群名对但内容说的是别的事，不算
            if exp.project not in name:
                continue
            if exp.project not in blob:
                continue
            if exp.expect_card and msg["msg_type"] != "interactive":
                continue          # 等的是卡片，文本不算
            exp.matched = True
            self.stats["matched"] += 1
            log.info("期望已满足: %s %s (群 %s)", exp.kind, exp.project, name)
            break

    def _chat_covers(self, chat_name: str, chat_id: str) -> bool:
        """这个群是不是 herdr 的项目群。判据见模块级 is_project_chat。"""
        return is_project_chat(chat_name)

    def _sweep_expired(self, now: float) -> None:
        """过了宽限期还没被满足的，判漏发。"""
        keep = []
        for exp in self.pending:
            if exp.matched:
                continue
            if now - exp.born < GRACE_SECONDS:
                keep.append(exp)
                continue
            verdict = "card_missing" if exp.expect_card else "missing"
            self.stats[verdict] += 1
            self.report({
                "verdict": verdict, "kind": exp.kind,
                "pane_id": exp.pane_id, "project": exp.project,
                "waited_s": round(now - exp.born, 1),
                "note": ("relay 说该发但群里 %.0fs 内没出现" % (now - exp.born)),
            })
        self.pending = keep

    # --- 输出 ---

    def report(self, record: dict) -> None:
        record.setdefault("verdict", "content")
        self.store.write(record)
        log.warning("质检发现: %s", json.dumps(record, ensure_ascii=False)[:300])
        if not self.qc_chat:
            return
        try:
            self.api.send_text(self.qc_chat, format_finding(record))
        except Exception as exc:
            log.warning("质检群发送失败: %s", scrub(exc))

    def summary(self) -> str:
        s = self.stats
        return (f"期望 {s['expect']} · 已满足 {s['matched']} · 漏发 {s['missing']} · "
                f"缺卡片 {s['card_missing']} · 内容异常 {s['content']} · "
                f"已检消息 {s['checked']} · 未绑跳过 {s['skipped_unbound']}")


# --- 循环 ---

async def relay_listener(obs: Observer) -> None:
    """连 relay，只读不写。"""
    import websockets
    while True:
        try:
            async with websockets.connect(RELAY_WS, max_size=None) as ws:
                log.info("已连上 relay %s", RELAY_WS_SAFE)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    kind = msg.get("type")
                    if kind == "agents":
                        obs.track(msg.get("agents", []))
                    elif kind == "agent_update":
                        agent = msg.get("agent") or {}
                        if agent.get("pane_id"):
                            obs.track([agent])
                    elif kind == "blocked":
                        # blocked 该产生一张带按钮的审批卡片
                        obs.note_expectation("blocked", {
                            "pane_id": msg.get("pane_id"),
                            "project": msg.get("project", ""),
                            "agent": msg.get("agent", ""),
                        }, options=msg.get("options") or ["*"])
        except Exception as exc:
            log.warning("relay 断开: %s，5 秒后重连", scrub(exc))
        await asyncio.sleep(5)


async def scan_loop(obs: Observer) -> None:
    """周期性扫群对账。飞书 API 是同步的，丢到线程里跑，别卡住事件循环。"""
    while True:
        try:
            await asyncio.to_thread(obs.scan_once)
        except Exception as exc:
            log.warning("对账失败: %s", scrub(exc))
        await asyncio.sleep(POLL_SECONDS)


async def refresh_chats(obs: Observer) -> None:
    """群列表会变（建群/退群），定期刷。"""
    while True:
        try:
            obs.chats = await asyncio.to_thread(obs.api.list_chats)
            log.info("监视 %d 个群", len(obs.chats))
        except Exception as exc:
            log.warning("列群失败: %s", scrub(exc))
        await asyncio.sleep(300)


async def heartbeat(obs: Observer) -> None:
    """每 10 分钟把统计打进日志，便于确认它还活着、在干活。"""
    while True:
        await asyncio.sleep(600)
        log.info("质检统计: %s", obs.summary())


async def amain() -> None:
    if not APP_ID or not APP_SECRET:
        raise SystemExit("缺少 HERDR_LARK_OBSERVER_APP_ID / _APP_SECRET")
    api = ObserverAPI(APP_ID, APP_SECRET, DOMAIN)
    store = FindingStore()
    obs = Observer(api, store, QC_CHAT)
    log.info("observer 启动：落盘 %s，质检群 %s",
             FINDINGS_PATH, QC_CHAT or "(未配置，只落盘)")
    obs.chats = await asyncio.to_thread(api.list_chats)
    log.info("监视 %d 个群", len(obs.chats))
    await asyncio.gather(relay_listener(obs), scan_loop(obs),
                         refresh_chats(obs), heartbeat(obs))


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
