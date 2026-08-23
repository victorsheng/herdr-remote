#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "lark-oapi>=1.4.0"]
# ///
"""飞书选择器端到端：单选补回车、多选勾多项再提交，用 observer 验收。

为什么要这个套件：选择器这条链路横跨四段——agent 渲染 TUI、relay 抓屏推
blocked、飞书渲染卡片、点击回来发按键。单测只能覆盖中间的解析，两头的
时序和渲染测不到，而实际踩的坑恰恰都在两头：

  - 卡片退化成 Yes/Trust/No（relay 抓屏时选择器还没渲染完）
  - 单选点了没反应（只发数字没补 Enter，光标移过去了但没提交）
  - 多选只能勾一项（第一次点击后 token 被清，后续点击当过期审批拒掉）

这些都得让真实 agent 出真实选择器才能复现，所以本套件会**驱动一个真实
pane**：往它发一句话让 agent 调 AskUserQuestion，然后全程用飞书那条路
操作它，最后读屏对答案。

用法:
    uv run tests/e2e_lark_select.py --pane w2A:p1
    uv run tests/e2e_lark_select.py --pane w2A:p1 --keep   # 结束不清场

前提:
    - relay 在跑，HERDR_RELAY 带 token
    - HERDR_LARK_OBSERVER_APP_ID / _APP_SECRET 已配（拉卡片用）
    - 目标 pane 是**空闲的 Claude agent**，且不是你自己所在的那个
      （自己给自己出题验不出来：AskUserQuestion 的答案走工具返回值，
       跟飞书卡片发按键是两条不同的通道）

它会往目标 pane 发指令并按键，跑完默认发 Esc 清场。

已知限制——卡片校验通常会跳过:
    agent 停在 AskUserQuestion 选择器上时，herdr 报的状态是 `working` 而
    不是 `blocked`（实测：herdr 报告状态: working）。relay 只在状态**转成**
    blocked 时才 announce_blocked，所以这种选择器不会推卡片到飞书。

    因此本套件真正验的是「按键语义 + 解析 + 本地卡片构造」这一段，飞书
    实际收到的卡片只在恰好有 blocked 推送时才能对上。要覆盖推送那一段，
    得让 agent 真的 blocked（比如触发一次工具权限确认）。
"""
import argparse
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
_STATE = Path(os.environ.get("TMPDIR", "/tmp")) / "herdr-lark-e2e-select"
_STATE.mkdir(exist_ok=True)
os.environ["HERDR_LARK_SEEN_PATH"] = str(_STATE / "seen.json")
os.environ["HERDR_LARK_BINDING_PATH"] = str(_STATE / "bindings.json")
os.environ["HERDR_LARK_CHATS_PATH"] = str(_STATE / "chats.json")

import herdr_lark as lk  # noqa: E402

# agent 渲染 TUI 选择器要时间，relay 还要轮询到状态变化才推 blocked。
# 给足余量：宁可慢，也别把「还没渲染完」误判成「解析不出来」。
SELECTOR_TIMEOUT_S = 40
# 按键之后等屏幕更新。TUI 重绘很快，但 relay 读屏走一趟往返。
SETTLE_S = 2.5

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


# --- 驱动目标 agent ---

SINGLE_PROMPT = (
    "调用 AskUserQuestion 工具，单选问题：'e2e 单选验证'，"
    "选项 'alpha'/'beta'/'gamma'，multiSelect 为 false。"
    "直接调用工具，不要做别的事，也不要自己替我回答。"
)
MULTI_PROMPT = (
    "调用 AskUserQuestion 工具，多选问题：'e2e 多选验证'，"
    "选项 'alpha'/'beta'/'gamma'/'delta'，multiSelect 为 true。"
    "直接调用工具，不要做别的事，也不要自己替我回答。"
)


async def wait_for_selector(pane_id: str, *, want_multi: bool,
                            timeout: float = SELECTOR_TIMEOUT_S) -> dict | None:
    """轮询到目标 pane 上出现选择器为止，返回解析结果。

    轮询而不是睡固定时长：agent 出题快慢差很多，睡短了必偶发失败，睡长了
    每轮都白等。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = await lk.read_pane(pane_id)
        if not lk.is_pane_read_error(raw):
            content = lk.clean_pane(raw)
            if not lk.is_review_page(content):
                group = lk.current_option_group(lk.detect_option_groups(content))
                if group and lk.detect_multiselect(content) == want_multi:
                    return {"group": group, "content": content}
        await asyncio.sleep(1.5)
    return None


async def drive(pane_id: str, prompt: str, *, want_multi: bool) -> dict | None:
    """让目标 agent 出一道选择题，等它停在选择器上。"""
    await lk.send_text_to_relay(pane_id, prompt)
    return await wait_for_selector(pane_id, want_multi=want_multi)


# --- observer 验收 ---

def observer_api():
    app_id = os.environ.get("HERDR_LARK_OBSERVER_APP_ID", "")
    secret = os.environ.get("HERDR_LARK_OBSERVER_APP_SECRET", "")
    if not (app_id and secret):
        return None
    import herdr_lark_observer as obs
    return obs.ObserverAPI(app_id, secret,
                           os.environ.get("HERDR_LARK_DOMAIN", "feishu"))


def card_buttons(card: dict) -> list[str]:
    """把卡片里所有按钮文字摊平。飞书的 action 可能嵌在 column_set 里。"""
    out = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("tag") == "button":
            text = node.get("text")
            label = text.get("content") if isinstance(text, dict) else text
            if label:
                out.append(str(label))
        for key in ("elements", "actions", "columns"):
            if key in node:
                walk(node[key])

    walk(card.get("elements", []))
    return out


def chat_ids(raw: str, api=None) -> list[str]:
    """要在哪些群里找卡片。

    HERDR_LARK_CHAT_ID 可能是逗号分隔的多个群，整串当 container_id 会被
    飞书拒（invalid container_id）。没显式指定就问 observer 要它可见的
    全部群——blocked 在没有绑定时是广播的，卡片落在哪个群不一定。
    """
    explicit = [c.strip() for c in (raw or "").split(",") if c.strip()]
    if explicit:
        return explicit
    if api is None:
        return []
    try:
        return list(api.list_chats().keys())
    except Exception:
        return []


def latest_option_card(api, chats: list[str], *, since: float) -> dict | None:
    """拿 since 之后最新的一张带选项按钮的卡片。

    卡片推到哪个群取决于绑定，逐个群找，找到就返回。
    """
    for chat_id in chats:
        try:
            messages = api.recent_messages(chat_id, limit=15)
        except Exception:
            continue  # 这个群读不了（没权限/id 失效）就看下一个
        for msg in messages:
            if msg["msg_type"] != "interactive" or msg["create_time"] < since:
                continue
            card = msg["content"]
            if not isinstance(card, dict):
                continue
            if any(b[:2].rstrip(".").isdigit() for b in card_buttons(card)):
                return card
    return None


# --- 用例 ---

async def test_single_select(pane_id: str, api, chats: list[str]) -> None:
    """单选：卡片选项要对得上屏幕，点一下要真的提交。"""
    print("\n[单选] 出题并等选择器")
    started = time.time()
    found = await drive(pane_id, SINGLE_PROMPT, want_multi=False)
    if not check("agent 停在单选选择器上", bool(found),
                 f"{SELECTOR_TIMEOUT_S}s 内没等到"):
        return
    options = found["group"]["options"]
    check(f"解析出 {len(options)} 个选项", len(options) >= 3, str(options))
    check("选项就是出题时给的", "alpha" in " ".join(options), str(options))

    # 卡片是 relay 推的，本地再构造一张对照——两者按钮该一致
    card = lk.build_blocked_card(pane_id, "claude", "e2e",
                                 found["content"], lk.TOOL_OPTIONS,
                                 lk.new_generation())
    labels = card_buttons(card)
    check("卡片没有退化成 Yes/Trust/No",
          not any("Trust (always)" in l for l in labels), str(labels[:4]))
    check("卡片按钮带上了真实选项",
          any("alpha" in l for l in labels), str(labels[:4]))
    check("单选卡片不出 Submit 按钮",
          not any("Submit" in l for l in labels), str(labels))

    if api and chats:
        pushed = latest_option_card(api, chats, since=started)
        if pushed is not None:
            plabels = card_buttons(pushed)
            check("飞书上实际收到的卡片也带真实选项",
                  any("alpha" in l for l in plabels), str(plabels[:4]))
        else:
            print("    ⓘ 群里没有对应卡片。已知原因：agent 停在 "
                  "AskUserQuestion 选择器上时 herdr 报的状态是 working "
                  "而非 blocked，relay 只在转 blocked 时才推卡片。")

    # 关键：点一下要真的提交，而不是只把光标移过去
    print("  点第 2 项（beta）")
    await lk.send_keys_to_relay(pane_id, lk.approval_keys("2", multiselect=False))
    await asyncio.sleep(SETTLE_S)
    after = lk.clean_pane(await lk.read_pane(pane_id))
    gone = lk.current_option_group(lk.detect_option_groups(after)) is None
    check("选择器已消失（回车生效，不是只高亮）", gone,
          "选择器还在——多半是没补 Enter")
    check("答案是 beta", "beta" in after, after[-160:])


async def test_multi_select(pane_id: str, api, chats: list[str]) -> None:
    """多选：勾多项后一次提交，且中途不能被当成已答完。"""
    print("\n[多选] 出题并等选择器")
    started = time.time()
    found = await drive(pane_id, MULTI_PROMPT, want_multi=True)
    if not check("agent 停在多选选择器上", bool(found),
                 f"{SELECTOR_TIMEOUT_S}s 内没等到"):
        return
    options = found["group"]["options"]
    check(f"解析出 {len(options)} 个选项", len(options) >= 4, str(options))
    check("识别为多选", lk.detect_multiselect(found["content"]))
    check("初始一个都没勾",
          not any(lk.checked_flags(found["content"])),
          str(lk.checked_flags(found["content"])))

    card = lk.build_blocked_card(pane_id, "claude", "e2e",
                                 found["content"], lk.TOOL_OPTIONS,
                                 lk.new_generation())
    labels = card_buttons(card)
    check("多选卡片带 Submit 按钮",
          any("Submit" in l for l in labels), str(labels))
    check(f"选项按钮齐全（{len(options)} 个）",
          sum(1 for l in labels if l[:1].isdigit()) == len(options),
          str(labels))

    if api and chats:
        pushed = latest_option_card(api, chats, since=started)
        if pushed is not None:
            plabels = card_buttons(pushed)
            check("飞书上实际收到的多选卡片带 Submit",
                  any("Submit" in l for l in plabels), str(plabels))
        else:
            print("    ⓘ 群里没有对应卡片，原因同上（herdr 报 working）")

    # 勾第 1、第 3 项：多选发数字不补 Enter。
    # 中间不读屏——每次读屏都是一趟 relay 往返，够久的话 TUI 状态会漂，
    # 之前就因此误判成「勾选丢了」。勾完一次性验。
    for key in ("1", "3"):
        print(f"  勾第 {key} 项")
        await lk.send_keys_to_relay(pane_id, lk.approval_keys(key, multiselect=True))
        await asyncio.sleep(SETTLE_S)

    mid = lk.clean_pane(await lk.read_pane(pane_id))
    check("勾完之后仍停在多选框（没被误当成已答完）",
          lk.detect_multiselect(mid), "选择器没了——数字键把它提交掉了？")
    flags = lk.checked_flags(mid)
    check("勾中了两项", sum(bool(f) for f in flags) == 2, str(flags))
    check("勾的是第 1 和第 3", flags[:3] == [True, False, True], str(flags))

    # 提交：Tab 进 Review 页，等它渲染出来再按 1。
    # 一次性发 ["Tab","1"] 会卡在 Review 页——1 到得太早被丢掉。
    print("  提交（Tab → 等 → 1）")
    for step in lk.multiselect_submit_steps():
        await lk.send_keys_to_relay(pane_id, step["keys"])
        if step["wait"]:
            await asyncio.sleep(step["wait"])
    await asyncio.sleep(SETTLE_S + 1)
    after = lk.clean_pane(await lk.read_pane(pane_id))
    check("选择器已消失（提交成功）",
          lk.current_option_group(lk.detect_option_groups(after)) is None,
          after[-160:])
    check("没有卡在 Review 页", not lk.is_review_page(after), after[-160:])
    both = "alpha" in after and "gamma" in after
    check("两项都提交上去了（alpha + gamma）", both, after[-200:])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pane", required=True,
                    help="目标 pane，如 w2A:p1（别用你自己所在的那个）")
    ap.add_argument("--chat", default="",
                    help="校验卡片的群 id（逗号分隔）；不给则扫 observer 可见的全部群")
    ap.add_argument("--keep", action="store_true", help="跑完不发 Esc 清场")
    ap.add_argument("--only", choices=("single", "multi"), help="只跑其中一项")
    args = ap.parse_args()

    print(f"飞书选择器 e2e  →  {lk.RELAY_WS_SAFE}")
    print(f"目标 pane: {args.pane}")

    try:
        api = observer_api()
    except Exception as exc:
        api = None
        print(f"  ⓘ observer 不可用（{lk.scrub(exc)[:60]}），跳过卡片校验")
    if api is None:
        print("  ⓘ 未配 HERDR_LARK_OBSERVER_APP_ID/_SECRET，跳过卡片校验")

    try:
        chats = chat_ids(args.chat, api)
        print(f"卡片校验群: {len(chats)} 个")
        cases = []
        if args.only != "multi":
            cases.append(test_single_select)
        if args.only != "single":
            cases.append(test_multi_select)
        for case in cases:
            try:
                await case(args.pane, api, chats)
            except Exception as exc:
                check(f"{case.__name__} 未抛异常", False, lk.scrub(exc)[:100])
    finally:
        if not args.keep:
            # 清场：agent 可能还停在某个选择器上，Esc 让它回到空闲，
            # 免得下一次跑（或你自己用）撞上残留状态。
            try:
                await lk.send_keys_to_relay(args.pane, ["Escape"])
            except Exception:
                pass

    print(f"\n{'='*46}")
    print(f"通过 {_passed}，失败 {_failed}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
