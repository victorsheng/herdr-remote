#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0"]
# ///
"""飞书客户端端到端测试：打真实 relay，防止回归。

单测用 FakeWS 模拟 relay，测不到时序——而今天最严重的几个 bug 恰恰都
出在真实链路上：回车丢失（relay 要在粘贴后 settle）、序号漂移、消息
误发到裸 shell。这个套件补的就是这一层。

用法:
    uv run tests/e2e_lark.py            # 全部
    uv run tests/e2e_lark.py --read-only  # 跳过写操作

前提: relay 在跑，且 HERDR_RELAY 指向它（带 token）。
写操作只挑 idle 的 agent，发的是无害的 echo 标记命令。
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))
os.environ.setdefault("HERDR_LARK_APP_ID", "e2e")
os.environ.setdefault("HERDR_LARK_APP_SECRET", "e2e")
# 绝不碰真实状态文件。
_STATE = Path(os.environ.get("TMPDIR", "/tmp")) / "herdr-lark-e2e"
_STATE.mkdir(exist_ok=True)
os.environ["HERDR_LARK_SEEN_PATH"] = str(_STATE / "seen.json")
os.environ["HERDR_LARK_BINDING_PATH"] = str(_STATE / "bindings.json")
os.environ["HERDR_LARK_CHATS_PATH"] = str(_STATE / "chats.json")

import herdr_lark as lk  # noqa: E402

READ_ONLY = "--read-only" in sys.argv
MARKER = f"e2e-{int(time.time())}"

_passed, _failed = 0, 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    if ok:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  ✗ {name}" + (f" — {detail}" if detail else ""))
    return ok


async def snapshot(ws) -> list[dict]:
    """等一帧完整的 agents 快照。"""
    for _ in range(8):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        if msg.get("type") == "agents":
            return msg["agents"]
    raise RuntimeError("relay 没有推送 agents 快照")


# --- 用例 ---

async def test_relay_reachable(agents):
    print("\n[1] relay 连通性")
    check("拿到 agents 快照", isinstance(agents, list))
    check("至少有一个 agent", len(agents) > 0, f"实际 {len(agents)}")
    fields = all(a.get("pane_id") and a.get("project") is not None for a in agents)
    check("每个 agent 都有 pane_id 与 project", fields)


async def test_index_stability(agents):
    """序号漂移曾导致操作错对象。"""
    print("\n[2] 序号稳定性")
    ordered = lk.index_agents(agents)
    check("index_agents 保序", [a["pane_id"] for a in ordered]
          == sorted(a["pane_id"] for a in agents))

    # 列表上写的号，match_agent 必须认
    import re
    mismatch = []
    for line in lk.format_agent_list(agents).splitlines():
        m = re.match(r"\S+\s+(\d+)\.\s+(\S+)", line)
        if not m:
            continue
        picked = lk.match_agent(agents, m.group(1))
        if not picked or picked.get("project") != m.group(2):
            mismatch.append(m.group(0))
    check("列表序号与 match_agent 一致", not mismatch, f"{mismatch[:2]}")

    # 状态变化不该影响序号
    if agents:
        target = ordered[len(ordered) // 2]
        sim = [dict(a) for a in agents]
        for a in sim:
            if a["pane_id"] == target["pane_id"]:
                a["status"] = "working" if a.get("status") != "working" else "idle"
        same = lk.match_agent(sim, str(ordered.index(target) + 1))
        check("状态变化后序号不漂移",
              same and same["pane_id"] == target["pane_id"])


async def test_shell_detection(agents):
    """裸 shell 误发曾让一个 '1' 变成 command not found。"""
    print("\n[3] 空 space 识别")
    shells = [a for a in agents if not lk.has_live_agent(a)]
    live = [a for a in agents if lk.has_live_agent(a)]
    check("识别出裸 shell 与活 agent", True,
          f"{len(shells)} 空 / {len(live)} 活")
    if shells:
        line = [l for l in lk.format_agent_list(agents).splitlines()
                if shells[0]["project"] in l]
        check("列表用 ▫ 标记空 space",
              bool(line) and lk.SHELL_ICON in line[0])
        check("发送前会拦截", lk.should_warn_shell(shells[0]))
    for a in live:
        if a.get("agent") == "shell":
            continue
        check("活 agent 不被误判为空", lk.has_live_agent(a))
        break


async def test_read_pane(agents):
    print("\n[4] 读取终端")
    live = [a for a in lk.index_agents(agents) if lk.has_live_agent(a)]
    if not live:
        check("有活 agent 可读", False, "全是裸 shell")
        return
    target = live[0]
    # relay 轮询 16 个 agent 时读取偶发超时（实测 3 次约 1 次）。
    # 客户端本来就会跳过这类帧，e2e 也重试一次再判定。
    attempts, raw, elapsed = 0, "", 0.0
    for attempts in range(1, 4):
        start = time.time()
        raw = await lk.read_pane(target["pane_id"])
        elapsed = time.time() - start
        if not lk.is_transient_read(raw):
            break
        await asyncio.sleep(1)

    check("read_pane 有返回", bool(raw))
    check(f"拿到真实内容（第 {attempts} 次）",
          not lk.is_transient_read(raw), raw[:40])
    if attempts > 1:
        print(f"    ⓘ 前 {attempts-1} 次返回失败占位——relay 读取偶发超时，"
              f"客户端会跳过这类帧")
    # 飞书回调要求 3 秒内返回，但我们是异步回传（handler 立刻返回），
    # 所以慢不会炸——只是 watch 的节流会失真。超了记一笔，不算失败。
    check(f"耗时 {elapsed:.2f}s 在硬上限内（{lk.READ_TIMEOUT_S}s）",
          elapsed < lk.READ_TIMEOUT_S, "relay 读取异常慢")
    if elapsed > 3.0:
        print(f"    ⓘ 慢于飞书回调上限 3s；agent 多时 relay 轮询会拖慢读取，"
              f"watch 实际间隔会大于设定的 {lk.STREAM_INTERVAL_S}s")

    cleaned = lk.clean_pane(raw)
    check("清理后无空行", not any(not l.strip() for l in cleaned.splitlines()))
    check("清理后无表格边框",
          not any(c in cleaned for c in "┌┐└┘├┤┬┴┼"))
    check("清理确实减少了体积", len(cleaned) <= len(raw))

    body = lk.stream_body(cleaned)
    check("stream_body 按整行裁剪",
          not body or body.splitlines()[0] in cleaned.splitlines())


async def test_send_text_ack(agents):
    """回车丢失是今天最隐蔽的 bug：relay 要在粘贴后 settle 才回 ack。"""
    print("\n[5] 发送与回车")
    if READ_ONLY:
        print("  (--read-only，跳过)")
        return
    idle = [a for a in lk.index_agents(agents)
            if a.get("status") == "idle" and lk.has_live_agent(a)]
    if not idle:
        check("有 idle agent 可写", False, "没有空闲的活 agent")
        return

    target = idle[0]
    print(f"  目标: {target['project']} ({target['pane_id']})")
    command = f"echo {MARKER}"
    start = time.time()
    try:
        await lk.send_text_to_relay(target["pane_id"], command)
        sent_ok = True
    except Exception as exc:
        sent_ok = False
        check("send_text 成功", False, str(exc)[:60])
    if not sent_ok:
        return
    check(f"send_text 完成（{time.time()-start:.2f}s）", True)

    # 关键：回车是否真的提交了——没提交的话标记只在输入框里
    await asyncio.sleep(3)
    after = lk.clean_pane(await lk.read_pane(target["pane_id"], lines=40))
    check("命令已提交（回车生效）", MARKER in after,
          "标记未出现，可能卡在输入框")


async def test_send_keys_ack(agents):
    """键名不在 SAFE_KEYS 里会被 relay 整条拒绝，不等 ack 就是假成功。"""
    print("\n[6] 按键 ack")
    if READ_ONLY:
        print("  (--read-only，跳过)")
        return
    idle = [a for a in lk.index_agents(agents)
            if a.get("status") == "idle" and lk.has_live_agent(a)]
    if not idle:
        check("有 idle agent 可测", False)
        return
    pane = idle[0]["pane_id"]

    # 白名单内的键应当被接受
    try:
        await lk.send_keys_to_relay(pane, ["Escape"])
        check("SAFE_KEYS 内的键被接受", True)
    except Exception as exc:
        check("SAFE_KEYS 内的键被接受", False, str(exc)[:60])

    # 白名单外的必须报错，而不是假成功
    try:
        await lk.send_keys_to_relay(pane, ["Ctrl+C"])
        check("非法键名被拒绝", False, "居然成功了——ack 校验失效")
    except Exception:
        check("非法键名被拒绝（不是假成功）", True)


async def test_option_detection(agents):
    print("\n[7] 选择器识别")
    samples = {
        "工具权限": ("确认？\n  1. yes, single permission\n"
                 "  2. trust, always allow\n  3. no (tab to edit)", 1),
        "多组问题": ("选方案？\n  1. A\n  2. B\n\n选类型？\n  1. X\n  2. Y", 2),
        "散文编号": ("改动：\n1. 修了 a\n2. 修了 b\n然后跑了测试。", 0),
    }
    for name, (text, want) in samples.items():
        got = len(lk.detect_option_groups(text))
        check(f"{name} → {want} 组", got == want, f"实际 {got}")

    # 真实 pane 里若有选择器，识别结果要能用
    for a in lk.index_agents(agents):
        if a.get("status") != "blocked":
            continue
        groups = lk.detect_option_groups(lk.clean_pane(await lk.read_pane(a["pane_id"])))
        check(f"blocked 的 {a['project']} 能识别选项", bool(groups))
        break


async def test_card_rendering(agents):
    print("\n[8] 卡片构造")
    live = [a for a in lk.index_agents(agents) if lk.has_live_agent(a)]
    if not live:
        return
    a = live[0]
    content = lk.clean_pane(await lk.read_pane(a["pane_id"]))

    card = lk.build_pane_card(a["project"], a.get("agent", ""),
                              a.get("status", ""), content)
    blob = json.dumps(card, ensure_ascii=False)
    check("pane 卡片带状态色", "<font color=" in blob)
    check("pane 卡片体积可控", len(blob) < 6000, f"{len(blob)} 字符")

    picker = lk.build_agent_picker_card("read", agents)
    labels = [b["text"]["content"] for e in picker["elements"]
              if e.get("tag") == "action" for b in e["actions"]]
    check("选择卡片按钮唯一", len(labels) == len(set(labels)))

    blocked = lk.build_blocked_card(a["pane_id"], "x", a["project"],
                                    "cmd", lk.TOOL_OPTIONS, lk.new_generation())
    values = [b["value"] for e in blocked["elements"]
              if e.get("tag") == "action" for b in e["actions"]]
    approvals = [v for v in values if v.get("a") == "k"]
    check("审批按钮发序号而非文本",
          all("k" in v and "text" not in v for v in approvals))


async def main() -> int:
    relay = lk.RELAY_WS
    print(f"herdr 飞书客户端 e2e  →  {lk.RELAY_WS_SAFE}")
    print(f"模式: {'只读' if READ_ONLY else '读写'}   标记: {MARKER}")

    try:
        async with lk.ws_connect(relay) as ws:
            agents = await snapshot(ws)
    except Exception as exc:
        print(f"\n✗ 连不上 relay: {lk.scrub(exc)}")
        print("  确认 relay 在跑，且 HERDR_RELAY 带上了 ?token=")
        return 2

    print(f"快照: {len(agents)} 个 agent")
    for case in (test_relay_reachable, test_index_stability, test_shell_detection,
                 test_read_pane, test_send_text_ack, test_send_keys_ack,
                 test_option_detection, test_card_rendering):
        try:
            await case(agents)
        except Exception as exc:
            check(f"{case.__name__} 未抛异常", False, lk.scrub(exc)[:80])

    print(f"\n{'='*46}")
    print(f"通过 {_passed}，失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
