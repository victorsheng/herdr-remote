#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""统计 Claude Code 的 token 用量，按两个计费周期分别汇总。

数据源是 ~/.claude/projects/**/*.jsonl —— Claude Code 自己写的会话日志，
每条助手消息都带 usage 明细。

两个周期：
  5 小时滚动窗 —— 短期限流的窗口。Claude Code 的 5h 窗按整点对齐，
                  从「当前时刻所属的那个 5 小时块」起算。
  周           —— 周额度。按本地时区的周一 00:00 起算。

一个重要限制：**额度上限不在本地任何文件里**（stats-cache.json 和
policy-limits.json 都没有），只有 Anthropic 服务端知道。所以这里只算得出
绝对用量；想看「用了百分之几」，得自己把上限填进环境变量：

    HERDR_USAGE_5H_LIMIT=...      5 小时窗的 token 上限
    HERDR_USAGE_WEEK_LIMIT=...    周 token 上限

没填就只显示绝对值，不假装算得出百分比。
"""
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

PROJECTS_DIR = os.path.expanduser(
    os.environ.get("HERDR_CLAUDE_PROJECTS", "~/.claude/projects"))

# 5 小时窗的长度。Claude Code 用的是整点对齐的滚动窗。
WINDOW_HOURS = 5

# 周额度从哪天起算。Anthropic 的周期按订阅日走，不一定是周一，
# 所以做成可配：0=周一 … 6=周日。
WEEK_ANCHOR = int(os.environ.get("HERDR_USAGE_WEEK_ANCHOR", "0") or 0) % 7

# 自配上限。留空则不显示百分比。
def _int_env(name: str) -> int:
    raw = (os.environ.get(name) or "").replace("_", "").replace(",", "").strip()
    try:
        return int(float(raw)) if raw else 0
    except ValueError:
        return 0


LIMIT_5H = _int_env("HERDR_USAGE_5H_LIMIT")
LIMIT_WEEK = _int_env("HERDR_USAGE_WEEK_LIMIT")

# 扫多少天以内的文件。周统计最多要 7 天，留点余量。
SCAN_DAYS = 9


# --- 周期边界 ---

def window_start(now: datetime, hours: int = WINDOW_HOURS) -> datetime:
    """当前时刻所属的 5 小时块的起点。

    按整点对齐（0/5/10/15/20 点），和 Claude Code 的窗口口径一致——
    不是「从现在往前推 5 小时」，那样窗口永远滑动，看不出「还剩多久重置」。
    """
    block = (now.hour // hours) * hours
    return now.replace(hour=block, minute=0, second=0, microsecond=0)


def week_start(now: datetime, anchor: int = None) -> datetime:
    """本周期起点。

    anchor 是起算的星期（0=周一），默认周一。Anthropic 的周额度按订阅日
    重置，未必是周一，所以允许改——重置点对不上的话，「还剩多久」会误导人。
    """
    anchor = WEEK_ANCHOR if anchor is None else anchor % 7
    back = (now.weekday() - anchor) % 7
    start = now - timedelta(days=back)
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def fmt_duration(seconds: float) -> str:
    """把「还有多久」说成人话。"""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分"


# --- 聚合 ---

class Bucket:
    """一个周期内的用量累计。"""

    def __init__(self, name: str, start: datetime, end: datetime, limit: int = 0):
        self.name = name
        self.start = start
        self.end = end
        self.limit = limit
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.messages = 0
        self.by_model: Counter = Counter()
        self.by_project: Counter = Counter()
        self.sessions: set = set()

    def add(self, usage: dict, model: str, project: str, session: str) -> None:
        self.messages += 1
        self.input += usage.get("input_tokens", 0) or 0
        self.output += usage.get("output_tokens", 0) or 0
        self.cache_read += usage.get("cache_read_input_tokens", 0) or 0
        self.cache_write += usage.get("cache_creation_input_tokens", 0) or 0
        # 按模型看构成：opus 和 haiku 的额度消耗完全不是一个量级
        self.by_model[model or "?"] += (usage.get("output_tokens", 0) or 0)
        if project:
            self.by_project[project] += (usage.get("output_tokens", 0) or 0)
        if session:
            self.sessions.add(session)

    @property
    def total(self) -> int:
        """算额度时看的总量：新增输入 + 输出 + 缓存写。

        缓存读不计——它按折扣计费，和「烧掉多少额度」不是一个口径，
        把它算进来会让数字虚高十倍，看不出真实消耗。
        """
        return self.input + self.output + self.cache_write

    @property
    def pct(self) -> float | None:
        if not self.limit:
            return None
        return 100.0 * self.total / self.limit

    def remaining_seconds(self, now: datetime) -> float:
        return (self.end - now).total_seconds()


def iter_usage(since: datetime, root: str = PROJECTS_DIR):
    """吐出 (时间, usage, 模型, 项目名, session_id)。

    只扫 mtime 在窗口内的文件：1290 个日志、近 700MB，全量扫一遍要几十秒，
    而我们最多只关心 9 天内的。
    """
    cutoff_ts = since.timestamp()
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < cutoff_ts:
                continue
        except OSError:
            continue
        project = os.path.basename(os.path.dirname(path))
        session = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    # 先做字符串预筛：json.loads 是这里的热点，
                    # 大部分行（用户消息、工具结果）压根没有 usage。
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    ts = parse_ts(rec.get("timestamp"))
                    if ts is None or ts < since:
                        continue
                    yield ts, usage, msg.get("model") or "?", project, session
        except OSError:
            continue


def parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        # 日志里是 2026-08-20T08:58:37.583Z
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None


def collect(now: datetime | None = None, root: str = PROJECTS_DIR) -> dict:
    """扫日志，填两个周期的桶。"""
    now = now or datetime.now().astimezone()
    w_start = window_start(now)
    wk_start = week_start(now)

    window = Bucket("5 小时窗", w_start,
                    w_start + timedelta(hours=WINDOW_HOURS), LIMIT_5H)
    week = Bucket("本周", wk_start, wk_start + timedelta(days=7), LIMIT_WEEK)

    since = min(w_start, wk_start)
    scanned = 0
    for ts, usage, model, project, session in iter_usage(
            since - timedelta(days=1), root):
        scanned += 1
        if wk_start <= ts < week.end:
            week.add(usage, model, project, session)
        if w_start <= ts < window.end:
            window.add(usage, model, project, session)
    return {"now": now, "window": window, "week": week, "scanned": scanned}


# --- 渲染 ---

def human(n: int) -> str:
    """token 数写成人能读的量级。"""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def bar(pct: float, width: int = 10) -> str:
    """进度条。用实心/空心圆点，等宽字体下对齐好，手机上也看得清。"""
    filled = max(0, min(width, round(width * pct / 100)))
    return "●" * filled + "○" * (width - filled)


def short_model(name: str) -> str:
    """claude-opus-5 → opus-5，claude-haiku-4-5-20251001 → haiku-4-5。"""
    n = (name or "?").replace("claude-", "")
    parts = n.split("-")
    # 去掉尾部的日期段
    while parts and parts[-1].isdigit() and len(parts[-1]) == 8:
        parts.pop()
    return "-".join(parts) or "?"


def short_project(name: str) -> str:
    """-Users-victor-code-github-herdr-remote → herdr-remote。"""
    return (name or "?").strip("-").split("-")[-1] or "?"


def render_bucket(b: Bucket, now: datetime, top_models: int = 3) -> list[str]:
    lines = []
    head = f"{b.name}   {human(b.total)} tokens"
    pct = b.pct
    if pct is not None:
        head = f"{b.name}   {bar(pct)} {pct:.0f}%"
    lines.append(head)

    span = f"{b.start:%m/%d %H:%M} 起"
    if b.limit:
        lines.append(f"   {human(b.total)} / {human(b.limit)}")
    lines.append(f"   {span} · {fmt_duration(b.remaining_seconds(now))}后重置")
    lines.append(f"   {b.messages} 条消息 · {len(b.sessions)} 个会话")

    if b.by_model:
        parts = [f"{short_model(m)} {human(v)}"
                 for m, v in b.by_model.most_common(top_models)]
        lines.append("   " + " · ".join(parts))
    return lines


def format_report(data: dict) -> str:
    now = data["now"]
    out = ["Claude 用量", ""]
    out += render_bucket(data["window"], now)
    out.append("")
    out += render_bucket(data["week"], now)

    week = data["week"]
    if week.by_project:
        out.append("")
        out.append("本周项目 Top3")
        for p, v in week.by_project.most_common(3):
            out.append(f"   {short_project(p)}  {human(v)}")

    if not (data["window"].limit or week.limit):
        out.append("")
        out.append("额度上限不在本地日志里，只能显示绝对用量。")
        out.append("想看百分比，设 HERDR_USAGE_5H_LIMIT / _WEEK_LIMIT。")
    return "\n".join(out)


def format_detail(data: dict) -> str:
    """带缓存明细的完整版，命令行用。"""
    lines = [format_report(data), "", "明细（本周）"]
    w = data["week"]
    lines += [
        f"   新增输入   {human(w.input)}",
        f"   输出       {human(w.output)}",
        f"   缓存写入   {human(w.cache_write)}",
        f"   缓存读取   {human(w.cache_read)}  (折扣计费，不计入总量)",
        "",
        f"   扫描 {data['scanned']} 条助手消息",
    ]
    return "\n".join(lines)


def main() -> None:
    started = time.time()
    data = collect()
    detail = "--detail" in sys.argv or "-v" in sys.argv
    print(format_detail(data) if detail else format_report(data))
    if detail:
        print(f"   耗时 {time.time() - started:.1f}s")


if __name__ == "__main__":
    main()
