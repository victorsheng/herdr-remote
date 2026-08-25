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


# 选项卡的选项文字被并排的 preview 面板污染。真实故障（群里 21:34 那张卡）：
# 带 preview 的 AskUserQuestion 把选项和预览面板渲染成两列，解析器按行切，
# 右列的边框和别人的预览内容就混进了选项文字：
#     1. 前文单发，最后一条带选项     ┌────────────┐
#     2. 按钮卡在前，上文补在后       │ 【卡片 2/3】│
# 手机上看不出 1/2/3 是什么；更严重的是同一原因会**吃掉选项**（实测 3 个
# 只解析出 2 个），有答案根本点不到。
#
# 判据与 herdr_lark.strip_preview_panel 一致：框线出现在 ≥3 空格之后。
# observer 不 import herdr_lark（刻意的进程隔离），所以这里留一份副本。
# 与 herdr_lark.py 保持同步：那边改判据就往这里改。
_PANEL_IN_OPTION_RE = re.compile(r"\s{3,}[┌└│├┐┘┤─━]")
# herdr_lark 的 PANEL_HIDDEN_LABEL：选项文字被 preview 面板完全遮住时的
# 占位符。整串精确匹配，不搜子串——正常选项里提到「预览面板」这几个字
# 不该被误报。与 herdr_lark.py 保持同步：那边改文案就往这里改。
_HIDDEN_LABEL = "（选项文字被预览面板遮住）"
# 选项清单的行首序号：`1.` `2.` …（build_option_card 的渲染形态）
_OPTION_INDEX_RE = re.compile(r"^\s*\*{0,2}(\d+)[.．]\*{0,2}\s*$")


def _card_text_cells(content: dict) -> list[str]:
    """把卡片里所有文本元素摊平成字符串列表，保持出现顺序。"""
    out = []
    for row in (content or {}).get("elements") or []:
        cells = row if isinstance(row, list) else [row]
        for cell in cells:
            if not isinstance(cell, dict) or cell.get("tag") != "text":
                continue
            text = cell.get("text")
            if isinstance(text, dict):
                text = text.get("content")
            out.append(str(text or ""))
    return out


def _card_button_labels(content: dict) -> list[str]:
    out = []
    for row in (content or {}).get("elements") or []:
        cells = row if isinstance(row, list) else [row]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            if cell.get("tag") == "button":
                text = cell.get("text")
                if isinstance(text, dict):
                    text = text.get("content")
                out.append(str(text or ""))
            for act in cell.get("actions") or []:
                if isinstance(act, dict) and act.get("tag") == "button":
                    text = act.get("text")
                    if isinstance(text, dict):
                        text = text.get("content")
                    out.append(str(text or ""))
    return out


# 选项清单在一整段 lark_md 里时的行形态：`**2.** 乙方案`。
# 要求整行以序号打头（^），正文里的编号列表（"改动如下：\n1. 修了 a"）
# 也是这个形状，所以还得靠「至少两项 + 连续」才认，见 _inline_option_pairs。
_INLINE_OPTION_RE = re.compile(r"^\s*\*{0,2}(\d+)[.．]\*{0,2}\s+(.+)$")


def _inline_option_pairs(content: dict) -> list[tuple[str, str]]:
    """从「一整段 markdown」形态里抽选项。

    build_option_card 现造的卡片长这样（选项全在一个 div 里）：
        {"tag":"div","text":{"tag":"lark_md","content":"**2.** 乙方案\n**3.** 丙方案"}}
    而飞书 message.list 读回来会被拆成序号/文字交替的降级形态。两种都得认，
    否则拿现造的卡片自检时静默返回空——所有规则跳过，看着"没问题"其实
    是没检查。
    """
    best: list[tuple[str, str]] = []
    for row in (content or {}).get("elements") or []:
        cells = row if isinstance(row, list) else [row]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            text = cell.get("text")
            if isinstance(text, dict):
                text = text.get("content")
            if not isinstance(text, str) or "\n" not in text:
                continue
            pairs = []
            for line in text.splitlines():
                m = _INLINE_OPTION_RE.match(line)
                if m:
                    pairs.append((m.group(1), m.group(2).strip()))
                elif pairs:
                    pairs = []      # 中间插了非选项行，不是整段选项清单
                    break
            # 至少两项。**不要**在这里要求编号连续：跳号的选项清单正是
            # option_number_gap 要抓的目标，在这里挡掉就永远报不出来。
            # 防正文编号列表误报靠的是调用方——check_option_card 只在卡片
            # 有数字按钮时才做序号校验，而散文里的编号列表不会配按钮。
            if len(pairs) >= 2 and len(pairs) > len(best):
                best = pairs
    return best


def _card_text_cells_have_index(content: dict) -> bool:
    """卡片里有独立的纯序号元素吗——那是读回来的降级形态的特征。

    这种形态本身足够特征化（正文不会被拆成 `2.` 一个独立元素），所以
    不必再要求配数字按钮。
    """
    return any(_OPTION_INDEX_RE.match(c) for c in _card_text_cells(content))


def parse_option_cells(content: dict) -> list[tuple[str, str]]:
    """从卡片里抽出 [(序号, 选项文字)]。不是选项卡就返回空。

    两种形态都认：
      - 读回来的降级形态：「序号元素 + 文字元素」交替，认到纯序号元素就
        把紧跟着的那个文本元素当它的选项文字
      - 现造的形态：选项全在一段 lark_md 里，逐行解析
    """
    cells = _card_text_cells(content)
    pairs = []
    for i, cell in enumerate(cells):
        m = _OPTION_INDEX_RE.match(cell)
        if m and i + 1 < len(cells):
            pairs.append((m.group(1), cells[i + 1]))
    return pairs or _inline_option_pairs(content)


def check_option_card(content: dict) -> list[dict]:
    """校验选项卡：选项文字要干净，按钮数要和选项数对得上。

    降级内容一律跳过——看不到真元素树，任何结论都是瞎猜，而假警报刷一屏
    之后人就不看质检群了（card_no_buttons 已经踩过这个坑）。
    """
    if not content or card_is_degraded(content):
        return []
    pairs = parse_option_cells(content)
    if not pairs:
        return []          # 不是选项卡（输出展示卡那类），这条规则不适用
    numeric_buttons = [b for b in _card_button_labels(content)
                       if b.strip().isdigit()]
    # 一整段 markdown 里的编号行，光看形状分不出「选项清单」和「正文里的
    # 编号列表」（"改动如下：1. 修了 a  2. 修了 b"）。用「有没有配数字按钮」
    # 区分：选项卡一定有，散文不会有。
    if not numeric_buttons and not _card_text_cells_have_index(content):
        return []

    problems = []
    for index, text in pairs:
        if text.strip() == _HIDDEN_LABEL:
            problems.append({
                "rule": "option_label_hidden",
                "detail": (f"选项 {index} 的文字被并排 preview 面板完全遮住，"
                           f"卡片上只剩占位符——这一项是什么人看不到"),
            })
        if _PANEL_IN_OPTION_RE.search(text):
            problems.append({
                "rule": "option_text_polluted",
                "detail": (f"选项 {index} 的文字里混进了并排 preview 面板的"
                           f"边框：{text.strip()[:60]!r}"),
            })

    # 按钮只放序号（见 herdr_lark.build_option_card），数字按钮应与选项一一对应。
    numeric = numeric_buttons
    if numeric and len(numeric) != len(pairs):
        problems.append({
            "rule": "option_button_mismatch",
            "detail": (f"正文列了 {len(pairs)} 个选项，却只有 {len(numeric)} 个"
                       f"数字按钮——有答案点不到"),
        })

    # 序号必须连续递增。不强求从 1 起——屏幕滚动会把首项卷出去，只剩 2./3.
    # 是正常的（herdr_lark 会把屏幕编号带下来）。跳号或颠倒才是问题：说明
    # 有选项被丢掉或渲染顺序乱了，人点不到或点错。
    body_nums = [int(n) for n, _ in pairs]
    if body_nums != list(range(body_nums[0], body_nums[0] + len(body_nums))):
        problems.append({
            "rule": "option_number_gap",
            "detail": (f"正文序号不是连续递增：{body_nums}——有选项被丢掉"
                       f"或顺序乱了"),
        })

    # 正文序号与按钮序号必须逐个相等。这是那个错位的直接特征：正文 2./3.
    # 而按钮 1./2.，卡片上看不出异常（1./2. 本身很正常），只有两边对比才
    # 发现得了——点「1」实际答的是屏幕 1 号，另一个选项。
    btn_nums = [int(b) for b in numeric]
    if btn_nums and len(btn_nums) == len(body_nums) and btn_nums != body_nums:
        problems.append({
            "rule": "option_number_mismatch",
            "detail": (f"正文序号 {body_nums} 与按钮序号 {btn_nums} 不一致"
                       f"——点下去答的是别的选项"),
        })
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


# herdr_lark.STATUS_LABELS 的值。build_pane_card 把它放在卡片第一个 div 里，
# 形如 "<font color='green'>DONE</font> · claude"，这是「输出展示卡片」的签名。
# 与 herdr_lark.py 保持同步：那边加状态就往这里加。
#
# 曾经这里写的是 ("DONE","WORKING","IDLE","NEEDS YOU") ——「NEEDS YOU」在
# herdr_lark 里根本不存在，而真有的「BLOCKED」反倒漏了。更要紧的是判据
# 找错了地方：按 tag=="text" 的独立元素做全串相等，而真卡片的标签是包在
# div/lark_md 里、还裹着 <font> 和 " · claude" 后缀，于是**一个都匹配不上**，
# 所有 pane 卡片都被判成「交互卡片没按钮」。线上 115 条 card_no_buttons
# 就是它，只不过后来被 card_is_degraded 挡在前面，看着像修好了。
_PANE_CARD_LABELS = ("DONE", "WORKING", "IDLE", "BLOCKED")

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


# 状态标签行的形状：可能裹着 <font>，后面跟 " · <agent 名>"。
# 只认**行首**的标签，免得输出正文里出现 "DONE" 就把审批卡豁免掉。
_PANE_LABEL_RE = re.compile(
    r"^\s*(?:<font[^>]*>)?\s*(" + "|".join(_PANE_CARD_LABELS) + r")\b")


def card_is_output_only(content: dict) -> bool:
    """这张卡片是不是「只展示输出」的那类，本来就不该有按钮。

    herdr_lark.build_pane_card 生成的完成/进展卡片按设计不含 button——它只是
    把 pane 输出贴出来看。拿「交互卡片都该有按钮」去要求它，就会刷假警报：
    实测 66 条质检记录里 54 条都是这个形态。

    判据用结构不用文案：状态标签开头的那一行是 build_pane_card 的签名。
    真实卡片长这样（标签在 div 的 lark_md 里，裹着颜色、带 agent 后缀）：
        {"tag":"div","text":{"tag":"lark_md",
         "content":"<font color='green'>DONE</font> · claude"}}
    所以既要看 div/lark_md 的 content，也要容忍 <font> 包裹和后缀；只比
    「整个文本元素 == DONE」是匹配不上任何真卡片的。

    只认行首的标签：输出正文里出现 "DONE" 不该让审批卡蒙混过关。

    审批卡片和选择卡片不在此列——它们必须有按钮，缺了就是真问题。
    """
    for row in (content or {}).get("elements") or []:
        # 飞书降级返回把元素套成二维数组，真实卡片是一维；两种都走一遍。
        cells = row if isinstance(row, list) else [row]
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            text = cell.get("text")
            if isinstance(text, dict):
                text = text.get("content")
            if not isinstance(text, str):
                continue
            if _PANE_LABEL_RE.match(text):
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

    几类结论字段不一样，别硬套一个模板：
      漏发/缺卡片   —— 有 pane/project/等了多久，没有具体消息
      内容异常     —— 有群名/消息样本，没有 pane（是从消息反查的）
      群没在质检   —— 只有 chat_id 和它绑的 pane，连群名都拿不到
                      （observer 不在群里，list_chats 里就没有它）
    """
    icons = {"missing": "🚨", "content": "⚠️", "card_missing": "🔘",
             "unmonitored_chat": "👁", "ok": "✅"}
    verdict = record.get("verdict", "?")
    icon = icons.get(verdict, "·")

    if verdict in ("missing", "card_missing"):
        head = (f"{icon} {verdict}  {record.get('project') or '?'} "
                f"({record.get('pane_id') or '?'})")
        lines = [head, f"   期望: {record.get('kind') or '?'}"]
    elif verdict == "unmonitored_chat":
        head = f"{icon} {verdict}  {record.get('chat_id') or '?'}"
        lines = [head, f"   绑着: {record.get('pane_id') or '?'}"]
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
        # 已经报过「observer 不在群里」的群，避免每轮重复刷
        self._warned_chats: set = set()
        self.stats = {"expect": 0, "matched": 0, "missing": 0,
                      "content": 0, "card_missing": 0, "checked": 0,
                      # 绑定表里有、observer 看不见的群。>0 就意味着有群
                      # 在「看着正常、其实没人检查」的状态。
                      "unmonitored": 0,
                      # 没绑群、本来就不该发的。这个数大不是坏事，但突然
                      # 变大意味着有人的绑定被清掉了。
                      "skipped_unbound": 0}

    # --- 期望侧：从 relay 事件产生 ---

    def bindings(self) -> dict:
        """绑定表 {chat_id: pane_id}。每次都重读文件。

        读不到、格式不对都当「空表」：质检工具不能因为一个坏文件挂掉。
        反过来假设「都绑着」更糟——那会把所有静默的 pane 全判成漏发。
        """
        try:
            with open(self.binding_path) as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("绑定表读不出，当作没有绑定: %s", scrub(exc))
            return {}
        if not isinstance(payload, dict):
            log.warning("绑定表结构不对，当作没有绑定")
            return {}
        return {str(k): str(v) for k, v in payload.items()
                if isinstance(v, str)}

    def monitored_panes(self) -> set:
        """observer **看得见的群**所绑的 pane。

        这是判断「该不该等一条消息」的正确口径，比 bound_panes() 严格。
        observer 是独立的第二个飞书应用，只能读自己所在的群；绑定表里的群
        如果没把 observer 拉进去（老群漏拉、有人把它移出去），那个群的消息
        永远扫不到，期望就永远划不掉——每次 finish/blocked 都判一次假漏发。

        实际踩过：datapilot6（w1R:p1）的群建在「建群自动拉 observer」之前，
        绑定表里有、observer 不在群里，于是连报 70 条假 missing/card_missing。
        真正的问题是「这个群没在质检」，靠 unmonitored_bindings() 单独报。
        """
        visible = set(self.chats or {})
        # 群列表还没拉到时不要收窄口径：那会把所有 pane 判成「不该等」，
        # 质检静默停摆。宁可沿用宽口径，多报也比不报好。
        if not visible:
            return self.bound_panes()
        return {pane for chat, pane in self.bindings().items()
                if chat in visible}

    def unmonitored_bindings(self) -> dict:
        """绑定表里有、但 observer 看不见的群 {chat_id: pane_id}。

        这种缺失是静默的：群看着一切正常，质检其实关着。必须显式报出来，
        否则它只会伪装成一堆看不懂的 missing。
        """
        visible = set(self.chats or {})
        if not visible:
            return {}
        return {chat: pane for chat, pane in self.bindings().items()
                if chat not in visible}

    def bound_panes(self) -> set:
        """绑定表里被某个群绑着的 pane，不管 observer 看不看得见那个群。

        判「该不该发」用这个；判「该不该等」要用更严的 monitored_panes()。
        """
        return set(self.bindings().values())

    def note_expectation(self, kind: str, agent: dict,
                         options: list | None = None) -> None:
        pane_id = agent.get("pane_id") or "?"
        # 只等**我们扫得到的群**里该出现的消息。两种情况都要跳过：
        #   没有群绑着它 —— herdr_lark 根本不推（主群回落已去掉）
        #   绑的群 observer 不在里面 —— 推了也扫不到，等于永远划不掉
        # 后者靠 report_unmonitored() 单独报，不能混在 missing 里：那会把
        # 「这个群没在质检」伪装成几十条「消息漏发」，方向完全错。
        if pane_id not in self.monitored_panes():
            log.info("跳过期望: %s %s (%s) 没有被质检的群绑着它",
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
        # 先自检：绑定表里有没有 observer 进不去的群。放在对账之前，这样
        # 「质检没覆盖到」会先于它引发的一堆 missing 出现在质检群里。
        self.report_unmonitored()
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

        # 去重只挡**内容校验**，不能挡对账。一条消息的内容判一次就够了，
        # 但它能满足的期望可能还没产生：期望来自 relay 的 ws 帧，消息来自
        # 飞书轮询，两条路各有延迟，谁先到都可能。先扫到消息就把它记进
        # seen、然后 return 的话，稍后到达的那个期望永远划不掉——白等一个
        # 宽限期，然后报一条假 missing。
        fresh = self.seen_messages.add(mid)
        if not fresh:
            # 判过内容了，但仍要参与对账
            self._match(chat_id, name, msg)
            return

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
            # 选项卡的选项完整性单独判：它和「有没有按钮」是两件事——选项卡
            # 有按钮，但选项文字可能被并排的 preview 面板污染，或者按钮数
            # 少于选项数（有答案点不到）。降级判断在函数内部做。
            problems.extend(check_option_card(msg["content"]))

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

        群必须对得上，否则 A 群的消息会划掉 B 群 agent 的期望，把真实漏发
        掩盖成「已满足」。对群有两道判据，优先用精确的那个：

          绑定表 —— chat_id 直接查出这个群绑着哪个 pane，和 exp.pane_id
                    比。这是 herdr_lark 决定「往哪发」用的同一份真相，
                    所以是精确的。
          群名   —— 绑定表里查不到这个群时的回落，按项目名。

        为什么不能只按项目名：重名 agent 是设计内的常态（herdr_lark 的
        chat_title_for 专门加了 [w1R] 这类标记来区分两个同名项目的群）。
        只比项目名的话，「🟡 [w1R] datapilot6」群里的消息会划掉 w29:p1 的
        期望——两个 pane 的项目名一模一样，谁在 pending 里排前面就划谁。
        结果是一边被假报漏发，另一边的真漏发被悄悄吃掉。

        群内再按项目名匹配消息内容。这不是精确匹配（pane 输出里可能偶然
        出现项目名），但方向是安全的：宁可放过，不要造假漏发。
        """
        if not self._chat_covers(name, chat_id):
            return
        # 这个群绑着哪个 pane。查得到就用它做精确判据，查不到回落到群名。
        chat_pane = self.bindings().get(str(chat_id))
        blob = json.dumps(msg["content"], ensure_ascii=False)
        for exp in self.pending:
            if exp.matched:
                continue
            if not exp.project:
                continue
            if chat_pane is not None:
                if exp.pane_id != chat_pane:
                    continue      # 这个群不是发给它的
            elif exp.project not in name:
                continue          # 回落：群名里没点到这个项目
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

    # --- 自检 ---

    def report_unmonitored(self) -> None:
        """绑定表里有、observer 却看不见的群，每个报一次。

        这类故障是静默的：群看着一切正常，卡片也确实发出去了，但没有任何
        人在检查它。唯一的外在表现是一堆看不懂的 missing——那正是我们要
        避免的误导。所以显式报出来，并说清楚该怎么修。

        每个群只报一次（记在 _warned_chats 里）：这是配置问题，不是每轮
        都值得刷一条的事件；重复刷屏和不报一样会让人不看质检群。
        """
        for chat, pane in sorted(self.unmonitored_bindings().items()):
            if chat in self._warned_chats:
                continue
            self._warned_chats.add(chat)
            self.stats["unmonitored"] += 1
            self.report({
                "verdict": "unmonitored_chat",
                "chat_id": chat, "pane_id": pane,
                "note": ("绑定表说这个群绑着 %s，但 observer 不在群里——"
                         "该群的消息扫不到，质检对它是静默关闭的。"
                         "把 observer 机器人拉进群即可。" % pane),
            })

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
                f"已检消息 {s['checked']} · 未绑跳过 {s['skipped_unbound']} · "
                f"未被质检的群 {s['unmonitored']}")


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
