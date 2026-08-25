#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lark-oapi>=1.4.0", "websockets>=14.0"]
# ///
"""herdr-remote 运维面板 —— 把散在四处的群/绑定/agent 信息拼成一张表。

存在的理由是**聚合**。同一个群的信息现在散在四个地方：

    lark_chats.json     它授权了吗
    lark_bindings.json  绑了哪个 pane
    relay 的 agents     那个 pane 是什么项目、什么状态
    飞书 API            群叫什么名字
    observer.jsonl      质检报过它什么问题

要回答「这个群到底怎么回事」得手工翻四处，翻错一处结论就反了——排查
observer 盲区时就吃过这个亏：群在 chat.get 里看得见，读消息却报 230002，
于是所有质检在那些群里静默失效，日志上看不出任何异常。

只读。不提供任何写操作：BindingStore 只在 lark 启动时读一次文件，之后
全在内存，这边改了文件 lark 感知不到，而 lark 一落盘就把这边的修改盖掉。
要改绑定请用飞书里的 /unbind、/spaces——那条路径 lark 自己管着内存和盘。
"""
import asyncio
import html
import json
import logging
import os
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("herdr-web")

CONFIG_DIR = os.path.expanduser("~/.config/herdr-remote")
BINDING_PATH = os.environ.get(
    "HERDR_LARK_BINDING_PATH", os.path.join(CONFIG_DIR, "lark_bindings.json"))
CHATS_PATH = os.environ.get(
    "HERDR_LARK_CHATS_PATH", os.path.join(CONFIG_DIR, "lark_chats.json"))
FINDINGS_PATH = os.environ.get(
    "HERDR_OBSERVER_LOG", os.path.join(CONFIG_DIR, "observer.jsonl"))

PORT = int(os.environ.get("HERDR_WEB_PORT", "8377"))
# 只监听回环：这个页面把 chat_id、pane、项目名都摊开了，不该对外网开放。
# 远程看的话走 Tailscale 或 SSH 端口转发，别把它 bind 到 0.0.0.0。
HOST = os.environ.get("HERDR_WEB_HOST", "127.0.0.1")

RELAY_WS = os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375")
APP_ID = os.environ.get("HERDR_LARK_APP_ID", "")
APP_SECRET = os.environ.get("HERDR_LARK_APP_SECRET", "")
OBSERVER_APP_ID = os.environ.get("HERDR_LARK_OBSERVER_APP_ID", "")
OBSERVER_APP_SECRET = os.environ.get("HERDR_LARK_OBSERVER_APP_SECRET", "")
DOMAIN = os.environ.get("HERDR_LARK_DOMAIN", "feishu")

# 最近多少条 findings 参与汇总。全量会越读越慢，而运维关心的是「现在」。
FINDINGS_TAIL = int(os.environ.get("HERDR_WEB_FINDINGS_TAIL", "200"))


# --- 数据读取 ---

def load_json(path: str, default):
    """读一个 JSON 文件，坏了就当没有。

    这些文件是别的进程在写，随时可能读到半截或读到不存在。面板挂掉比
    显示旧数据糟得多，所以一律吞掉异常。

    default 每次都深拷一份：直接返回同一个对象的话，调用方改了它会污染
    下一次调用。
    """
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(default))


def load_findings(path: str = FINDINGS_PATH, tail: int = FINDINGS_TAIL) -> list[dict]:
    """读 observer 的质检记录（JSONL），只要最后 tail 条。"""
    try:
        with open(path) as fh:
            lines = fh.readlines()[-tail:]
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue        # 边写边读，最后一行可能是半截
    return out


# --- 聚合 ---

def build_chat_rows(*, chats, bindings, agents, names, observer_visible) -> list[dict]:
    """一个群一行，把四处的信息拼齐。

    status 的取值和含义：
        unbound   授权了但没绑 agent，通知不会发到这里
        dangling  绑定指向的 pane 已经不在了——残留，该清理
        其余      就是那个 agent 的实时状态（working/blocked/idle/done）
    """
    by_pane = {str(a.get("pane_id")): a for a in agents if a.get("pane_id")}
    rows = []
    for chat_id in sorted(set(str(c) for c in chats)):
        pane_id = bindings.get(chat_id)
        agent = by_pane.get(pane_id) if pane_id else None
        if not pane_id:
            status = "unbound"
        elif agent is None:
            status = "dangling"
        else:
            status = agent.get("status") or "unknown"
        rows.append({
            "chat_id": chat_id,
            # 拿不到群名就显示 id：空白会让人以为页面出错了。
            "name": names.get(chat_id) or chat_id,
            "pane_id": pane_id,
            "project": (agent or {}).get("project"),
            "agent": (agent or {}).get("agent"),
            "host": (agent or {}).get("host"),
            "status": status,
            "observer_visible": chat_id in observer_visible,
        })
    return rows


def orphan_agents(*, agents, bindings) -> list[dict]:
    """没有任何群绑着的 agent。

    它们的完成/审批通知**发不出去**——chats_watching 没有回落设计，
    没绑就是不发。列出来才知道有哪些 agent 处于「静默」状态。
    """
    bound = {str(v) for v in bindings.values()}
    return [a for a in agents
            if a.get("pane_id") and str(a["pane_id"]) not in bound]


def summarize_findings(rows) -> dict:
    """质检结果按规则汇总。内容类按 rule 分，其余按 verdict/kind 分。"""
    counter = Counter()
    for row in rows:
        if not isinstance(row, dict):
            continue
        problems = row.get("problems")
        if row.get("verdict") == "content" and problems:
            for problem in problems:
                counter[problem.get("rule") or "unknown"] += 1
        else:
            counter[f"{row.get('verdict') or 'unknown'}/{row.get('kind')}"] += 1
    return dict(counter)


# --- 渲染 ---

_STATUS_BADGE = {
    "working": ("运行中", "#1f6feb"),
    "blocked": ("等你选", "#d1242f"),
    "done": ("已完成", "#1a7f37"),
    "idle": ("空闲", "#57606a"),
    "unbound": ("未绑定", "#9a6700"),
    "dangling": ("绑定失效", "#d1242f"),
}


def _esc(value) -> str:
    """群名和项目名都是外部输入，不转义就是 XSS。"""
    return html.escape(str(value if value is not None else ""))


def render_page(data: dict) -> str:
    chats = data.get("chats") or []
    orphans = data.get("orphans") or []
    findings = data.get("findings") or {}

    if chats:
        chat_rows = "\n".join(_render_chat_row(row) for row in chats)
        chat_table = f"""<table>
<thead><tr><th>群</th><th>chat_id</th><th>绑定 pane</th><th>项目</th>
<th>状态</th><th>质检覆盖</th></tr></thead>
<tbody>{chat_rows}</tbody></table>"""
    else:
        chat_table = "<p class='empty'>还没有授权的群。在飞书里发 /spaces 建群。</p>"

    if orphans:
        orphan_rows = "\n".join(
            f"<tr><td><code>{_esc(a.get('pane_id'))}</code></td>"
            f"<td>{_esc(a.get('project'))}</td><td>{_esc(a.get('agent'))}</td>"
            f"<td>{_esc(a.get('status'))}</td></tr>" for a in orphans)
        orphan_html = f"""<h2>没有群绑着的 agent（{len(orphans)}）</h2>
<p class='hint'>它们的通知发不出去——没绑群就不推送，得用 /agents 主动看。</p>
<table><thead><tr><th>pane</th><th>项目</th><th>agent</th><th>状态</th></tr></thead>
<tbody>{orphan_rows}</tbody></table>"""
    else:
        orphan_html = ""

    if findings:
        finding_rows = "\n".join(
            f"<tr><td><code>{_esc(k)}</code></td><td>{v}</td></tr>"
            for k, v in sorted(findings.items(), key=lambda kv: -kv[1]))
        finding_html = f"""<h2>最近的质检发现</h2>
<table><thead><tr><th>规则</th><th>次数</th></tr></thead>
<tbody>{finding_rows}</tbody></table>"""
    else:
        finding_html = "<h2>最近的质检发现</h2><p class='empty'>没有记录。</p>"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>herdr 运维面板</title>
<style>
  body {{ font: 14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;
         margin: 0; padding: 24px; background: #f6f8fa; color: #1f2328; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 15px; margin: 28px 0 8px; }}
  .meta {{ color: #57606a; font-size: 12px; margin-bottom: 20px; }}
  .hint {{ color: #57606a; font-size: 12px; margin: 0 0 8px; }}
  .empty {{ color: #57606a; background: #fff; border: 1px solid #d0d7de;
            border-radius: 6px; padding: 16px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border: 1px solid #d0d7de; border-radius: 6px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #d0d7de; }}
  th {{ background: #f6f8fa; font-weight: 600; font-size: 12px; color: #57606a; }}
  tr:last-child td {{ border-bottom: none; }}
  code {{ font: 12px ui-monospace,SFMono-Regular,Menlo,monospace;
          background: #f6f8fa; padding: 1px 5px; border-radius: 4px; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 999px;
            font-size: 12px; color: #fff; }}
  .blind {{ color: #d1242f; font-weight: 600; }}
  .ok {{ color: #1a7f37; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0d1117; color: #e6edf3; }}
    table, .empty {{ background: #161b22; border-color: #30363d; }}
    th {{ background: #161b22; color: #8b949e; }}
    th, td {{ border-color: #30363d; }}
    code {{ background: #0d1117; }}
  }}
</style></head><body>
<h1>herdr 运维面板</h1>
<div class="meta">只读 · 生成于 {_esc(data.get('generated_at'))} ·
  改绑定请在飞书里用 /unbind、/spaces</div>
<h2>飞书群（{len(chats)}）</h2>
{chat_table}
{orphan_html}
{finding_html}
</body></html>"""


def _render_chat_row(row: dict) -> str:
    label, color = _STATUS_BADGE.get(row.get("status"),
                                     (row.get("status") or "?", "#57606a"))
    if row.get("observer_visible"):
        coverage = "<span class='ok'>已覆盖</span>"
    else:
        # 这是排查时反复踩到的坑，值得在页面上直说而不是留个叉。
        coverage = ("<span class='blind' title='observer 不在这个群里，"
                    "所有质检规则在这里静默失效'>质检盲区</span>")
    return (f"<tr><td>{_esc(row.get('name'))}</td>"
            f"<td><code>{_esc(row.get('chat_id'))}</code></td>"
            f"<td><code>{_esc(row.get('pane_id') or '—')}</code></td>"
            f"<td>{_esc(row.get('project') or '—')}</td>"
            f"<td><span class='badge' style='background:{color}'>"
            f"{_esc(label)}</span></td>"
            f"<td>{coverage}</td></tr>")


# --- 外部数据采集 ---
#
# 三个来源各自容错：relay 可能没起、飞书可能限流、observer 应用可能没配。
# 任何一个挂掉都只让那一列显示「未知」，不能拖垮整个页面——运维面板在
# 出问题的时候最该能打开。

async def fetch_agents(timeout: float = 5.0) -> list[dict]:
    """从 relay 拿 agent 快照。拿不到就返回空表。"""
    try:
        import websockets
        async with websockets.connect(RELAY_WS, open_timeout=timeout) as ws:
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                msg = json.loads(raw)
                if msg.get("type") == "agents":
                    return msg.get("agents") or []
    except Exception as exc:
        log.warning("拿 agent 列表失败: %s", exc)
    return []


def _lark_client(app_id: str, app_secret: str):
    import lark_oapi as lark
    domain = lark.FEISHU_DOMAIN if DOMAIN == "feishu" else lark.LARK_DOMAIN
    return (lark.Client.builder().app_id(app_id).app_secret(app_secret)
            .domain(domain).build())


def _list_chats(app_id: str, app_secret: str) -> dict:
    """{chat_id: 群名}。注意方向——别和 bindings 的 {chat: pane} 搞混。"""
    if not app_id or not app_secret:
        return {}
    try:
        from lark_oapi.api.im.v1 import ListChatRequest
        client = _lark_client(app_id, app_secret)
        out, token = {}, None
        while True:
            builder = ListChatRequest.builder().page_size(100)
            if token:
                builder = builder.page_token(token)
            resp = client.im.v1.chat.list(builder.build())
            if not resp.success():
                break
            for item in (resp.data.items or []):
                out[item.chat_id] = item.name or ""
            token = getattr(resp.data, "page_token", None)
            if not token or not getattr(resp.data, "has_more", False):
                break
        return out
    except Exception as exc:
        log.warning("拿群列表失败: %s", exc)
        return {}


def fetch_chat_names() -> dict:
    return _list_chats(APP_ID, APP_SECRET)


def fetch_observer_visible() -> set:
    """observer 能看到（= 是成员）的群。

    用 chat.list 而不是 chat.get：后者对同租户的任何群都返回成功，哪怕
    bot 不在群里——真正决定「能不能读消息」的是成员身份，而 chat.list
    只返回 bot 所在的群。这个区别是排查盲区时用错误码 230002 确认的。
    """
    return set(_list_chats(OBSERVER_APP_ID, OBSERVER_APP_SECRET))


def collect() -> dict:
    """跑一遍采集，拼出页面要的全部数据。"""
    bindings = load_json(BINDING_PATH, {})
    if not isinstance(bindings, dict):
        bindings = {}
    chats = load_json(CHATS_PATH, [])
    if not isinstance(chats, list):
        chats = []
    agents = asyncio.run(fetch_agents())
    names = fetch_chat_names()
    visible = fetch_observer_visible()
    # 绑定里出现、但不在授权列表里的群也要显示——那种「绑了却没授权」的
    # 状态最容易被忽略，而它意味着消息发得出去、指令收不回来。
    known = set(str(c) for c in chats) | set(bindings)
    return {
        "chats": build_chat_rows(chats=known, bindings=bindings, agents=agents,
                                 names=names, observer_visible=visible),
        "orphans": orphan_agents(agents=agents, bindings=bindings),
        "findings": summarize_findings(load_findings()),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


# --- HTTP ---

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            body = render_page(collect()).encode("utf-8")
        except Exception:
            log.exception("渲染失败")
            body = "<h1>面板出错了，看服务日志</h1>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # 运维面板要看实时状态，缓存了就是骗人。
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("运维面板: http://%s:%d", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
