#!/usr/bin/env python3
"""blocked 推送带的 prompt 不能把选择器截断。

实测复现（w2A:p1，AskUserQuestion 单选）：announce_blocked 把抓到的屏幕
截成 content[:500] 再广播，最后一行正好被腰斩成

    Enter to select · ↑/↓ to nav

半截提示行 is_selector_hint 认不出，客户端的 _is_selector_tail 就判定
「选择器已经翻过去了」，整组选项被丢弃，卡片回落成 Yes/Trust/No——按钮
和屏幕上问的对不上，点了等于替 agent 乱答。

500 字符对中文尤其紧：一屏选择器带上每项的描述行，很容易就超。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay  # noqa: E402

# 真实抓屏（截断前）。末尾那行提示是选择器的一部分，丢了它整组就废。
REAL_SELECTOR = """\
  ⎿  · 收尾确认 → 结束
  Ran 1 shell command
⏺ 收尾确认返回 "结束"，cal 2026 已执行，输出如上。
  <!-- AI_DIALOG_SUMMARY: 收尾确认选择「结束」，并执行 cal 2026 输出全年日历。 -->
✻ Cogitated for 14s
❯ 执行 shell 命令：cal 2027。只执行，不解释。
  Ran 1 shell command
⏺ 已执行。
  <!-- AI_DIALOG_SUMMARY: 执行 cal 2027，输出全年日历。 -->
✻ Brewed for 4s
❯ 调用 AskUserQuestion 工具，单选：'blocked 推送验证'，选项 'yes'/'no'，multiSelect false。直接调用，别自己回答。
 ☐ 推送验证
blocked 推送验证
❯ 1. yes
     选择 yes。
  2. no
     选择 no。
  3. Type something.
  4. Chat about this
Enter to select · ↑/↓ to navigate · Esc to cancel"""


class BlockedPromptLimitTests(unittest.TestCase):
    def test_limit_is_a_named_constant(self):
        """截断长度得是个有名字的常量，不能散落成魔法数字。

        两条路径（轮询、事件）各写一个 500，改一处漏一处。
        """
        self.assertTrue(hasattr(herdr_relay, "BLOCKED_PROMPT_LIMIT"))

    def test_limit_fits_a_real_selector(self):
        """额度要装得下一屏真实选择器，否则提示行会被腰斩。"""
        self.assertGreaterEqual(
            herdr_relay.BLOCKED_PROMPT_LIMIT, len(REAL_SELECTOR),
            "额度小于实测抓屏长度，选择器仍会被截断")

    def test_hint_line_survives_truncation(self):
        """截断之后，选择器底部的提示行必须还完整。"""
        clipped = REAL_SELECTOR[:herdr_relay.BLOCKED_PROMPT_LIMIT]
        self.assertIn("Esc to cancel", clipped,
                      "提示行被截断——客户端会判定选择器已翻篇，整组丢弃")

    def test_options_survive_truncation(self):
        clipped = REAL_SELECTOR[:herdr_relay.BLOCKED_PROMPT_LIMIT]
        for want in ("1. yes", "2. no"):
            self.assertIn(want, clipped)

    def test_no_bare_500_left_in_source(self):
        """源码里不该再有裸的 content[:500]。"""
        src = (ROOT / "relay" / "herdr_relay.py").read_text(encoding="utf-8")
        self.assertNotIn("content[:500]", src,
                         "还有硬编码的 500，改一处漏一处")

    def test_both_paths_use_the_constant(self):
        """轮询和事件两条路径都要用同一个常量。"""
        src = (ROOT / "relay" / "herdr_relay.py").read_text(encoding="utf-8")
        uses = len(re.findall(r"content\[:BLOCKED_PROMPT_LIMIT\]", src))
        self.assertGreaterEqual(uses, 2, f"只有 {uses} 处用了常量，应有 2 处")


if __name__ == "__main__":
    unittest.main()
