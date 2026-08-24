#!/usr/bin/env python3
"""抓屏时终端处于回滚状态，抓到的内容就不是最新的。

真实故障：群里发出的消息末尾拼着 `Jump to bottom (click) ↓`——那是终端
右下角的滚动提示符，只在视口不在底部时渲染。它出现就等于「底部还有内容
没抓到」，消息内容是残缺的。实测 424 条群消息里命中 7 条。

只把提示符滤掉是错的：症状消失，内容照样缺，而且再也看不出哪条不可信。
所以这里做的是**标注**——herdr 一直在 `pane get` 里返回
`scroll.offset_from_bottom`，>0 就是权威的「视口不在底部」判据，比正则
匹配 UI 文案可靠（文案会随版本改）。

不主动滚回底部：herdr 没有滚动命令（send-keys 不支持 PageUp），而且滚动
会改变用户正在看的画面。
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class ScrollOffsetTests(unittest.TestCase):
    """pane_scroll_offset：从 herdr 读出视口偏移。"""

    def test_reads_offset_from_pane_get(self):
        payload = ('{"result":{"pane":{"scroll":'
                   '{"offset_from_bottom":12,"max_offset_from_bottom":71}}}}')
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value=payload)):
            got = asyncio.run(herdr_relay.pane_scroll_offset("w1:p1"))
        self.assertEqual(got, 12)

    def test_at_bottom_is_zero(self):
        payload = '{"result":{"pane":{"scroll":{"offset_from_bottom":0}}}}'
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value=payload)):
            self.assertEqual(asyncio.run(herdr_relay.pane_scroll_offset("w1:p1")), 0)

    def test_missing_scroll_field_is_zero(self):
        """老版本 herdr 不给 scroll 字段时按「在底部」处理，不要凭空报警。"""
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value='{"result":{"pane":{}}}')):
            self.assertEqual(asyncio.run(herdr_relay.pane_scroll_offset("w1:p1")), 0)

    def test_garbage_output_is_zero(self):
        """herdr 报错或输出非 JSON 时不能炸——抓屏是主路径。"""
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value="boom: not json")):
            self.assertEqual(asyncio.run(herdr_relay.pane_scroll_offset("w1:p1")), 0)

    def test_exception_is_zero(self):
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(side_effect=RuntimeError("gone"))):
            self.assertEqual(asyncio.run(herdr_relay.pane_scroll_offset("w1:p1")), 0)


class StalenessMarkTests(unittest.TestCase):
    """回滚状态下抓到的内容要带上标注。"""

    PANE_TEXT = "line one\nline two\n  5. Chat about this"

    def _read(self, offset):
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value=self.PANE_TEXT)), \
             mock.patch.object(herdr_relay, "pane_scroll_offset",
                               new=mock.AsyncMock(return_value=offset)):
            return asyncio.run(herdr_relay.read_pane_async("w1:p1"))

    def test_at_bottom_has_no_mark(self):
        """主路径：在底部就是正常内容，一个字都不加。"""
        out = self._read(0)
        self.assertNotIn(herdr_relay.STALE_VIEWPORT_MARK, out)
        self.assertIn("line one", out)

    def test_scrolled_back_is_marked(self):
        out = self._read(12)
        self.assertIn(herdr_relay.STALE_VIEWPORT_MARK, out)

    def test_mark_goes_first(self):
        """标注必须在最前面——手机上只看得到开头几行。"""
        out = self._read(12)
        self.assertTrue(out.startswith(herdr_relay.STALE_VIEWPORT_MARK),
                        f"标注没在开头: {out[:80]!r}")

    def test_content_is_still_delivered(self):
        """标注是加料不是替换：内容再残缺也比不给强。"""
        out = self._read(12)
        self.assertIn("line one", out)
        self.assertIn("5. Chat about this", out)

    def test_mark_mentions_staleness_in_chinese(self):
        """给人看的，得说清「可能不是最新」。"""
        self.assertIn("最新", herdr_relay.STALE_VIEWPORT_MARK)


if __name__ == "__main__":
    unittest.main()


class PaneReadLineCapTests(unittest.TestCase):
    """客户端能请求的行数必须和超时阈值匹配，否则请求必然失败。

    真实故障（relay.log 里 41 次 `herdr call timed out`）：代码允许客户端
    请求最多 5000 行，但 run_herdr_async 的超时固定 15 秒。实测在本机：

        50 行 = 1.0s    200 行 = 7.1s
       300 行 = 11.0s   800 行 = 16s（撞上限）

    约 36ms/行，15 秒只够 ~400 行。5000 那个上限完全是虚的——请求必超时，
    而且超时的 lines 会被记进 pane_subs，push_subscribed_panes 每轮拿同一个
    必失败的行数重试，白烧 CPU 和 SSH 往返（日志里的成簇超时就是这么来的）。
    """

    def test_cap_is_below_the_timeout_budget(self):
        """上限 × 每行耗时要留在超时阈值内，别定个一定失败的数。"""
        self.assertLessEqual(herdr_relay.PANE_READ_MAX_LINES, 400)

    def test_cap_is_still_useful(self):
        """也不能收得太狠——一屏终端 47 行，得装得下几屏历史。"""
        self.assertGreaterEqual(herdr_relay.PANE_READ_MAX_LINES, 150)

    def test_clamp_rejects_oversized_request(self):
        self.assertEqual(herdr_relay.clamp_pane_lines(5000),
                         herdr_relay.PANE_READ_MAX_LINES)

    def test_clamp_keeps_reasonable_request(self):
        self.assertEqual(herdr_relay.clamp_pane_lines(50), 50)

    def test_clamp_floors_at_one(self):
        self.assertEqual(herdr_relay.clamp_pane_lines(0), 1)
        self.assertEqual(herdr_relay.clamp_pane_lines(-5), 1)

    def test_clamp_handles_garbage(self):
        """客户端传了非数字不能让 relay 炸——这是主循环。"""
        self.assertEqual(herdr_relay.clamp_pane_lines("abc"), 30)
        self.assertEqual(herdr_relay.clamp_pane_lines(None), 30)

    def test_clamp_default_matches_previous_behaviour(self):
        self.assertEqual(herdr_relay.clamp_pane_lines("abc", default=99), 99)


class InputSuggestionTests(unittest.TestCase):
    """输入框里置灰的补全建议，要能提取出来带到卡片上。

    终端在光标后置灰显示建议，按 Tab 接受：

        ❯ /rev·iew-changes        （· 之后是置灰的建议）

    问题是 relay 主链路抓的是**纯文本**，置灰信息在那一层就没了——
    `/rev` 和 `iew-changes` 连成 `/review-changes`，分不出哪段是人打的、
    哪段是建议。所以要另走一次 `--ansi` 抓屏，靠颜色码切开。

    刻意不改主抓屏：现有的 clean_pane、选项解析、选择器定位全都假设输入
    是纯文本，改成带转义序列会牵一发动全身。独立通道拿建议，主链路一行
    不用动。
    """

    # 真实 --ansi 抓屏的结构：正常段用 \x1b[0m 复位，置灰段带 dim/灰色码
    ANSI_WITH_SUGGESTION = (
        "\x1b[0m\x1b[38;2;102;102;102m❯ \x1b[0m/rev"
        "\x1b[2m\x1b[38;2;102;102;102miew-changes\x1b[0m\r")
    ANSI_NO_SUGGESTION = "\x1b[0m\x1b[38;2;102;102;102m❯ \x1b[0m/review\x1b[0m\r"

    def test_extracts_the_dim_tail(self):
        self.assertEqual(
            herdr_relay.input_suggestion(self.ANSI_WITH_SUGGESTION),
            "iew-changes")

    def test_no_suggestion_returns_empty(self):
        self.assertEqual(herdr_relay.input_suggestion(self.ANSI_NO_SUGGESTION), "")

    def test_empty_input_returns_empty(self):
        self.assertEqual(herdr_relay.input_suggestion(""), "")

    def test_plain_text_returns_empty(self):
        """没有 ANSI 码时不该瞎猜——那正是主链路的形态。"""
        self.assertEqual(herdr_relay.input_suggestion("❯ /review-changes"), "")

    def test_ignores_dim_outside_the_input_line(self):
        """状态栏也是灰的，不能把它当成建议。

        判据是「在 ❯ 输入行上」，不是「全屏找灰色」——状态栏那些
        `Context ███ 25%` 全是灰的，全抓出来就是一堆噪音。
        """
        screen = ("\x1b[0m\x1b[2m\x1b[38;2;102;102;102mContext ███ 25%\x1b[0m\r\n"
                  + self.ANSI_NO_SUGGESTION)
        self.assertEqual(herdr_relay.input_suggestion(screen), "")

    def test_picks_the_last_prompt_line(self):
        """一屏里可能有历史提示符，只认最后那行（当前输入）。"""
        screen = ("\x1b[0m❯ 旧的一行\x1b[0m\r\n" + self.ANSI_WITH_SUGGESTION)
        self.assertEqual(herdr_relay.input_suggestion(screen), "iew-changes")

    def test_strips_trailing_whitespace(self):
        raw = ("\x1b[0m❯ \x1b[0m/rev\x1b[2miew-changes   \x1b[0m\r")
        self.assertEqual(herdr_relay.input_suggestion(raw), "iew-changes")

    # 真机抓到的误报（w2A:p1，用户输入框里待发的内容）：
    #   ❯ \x1b[0m\x1b[2m再来一次，两组问题都设 multiSelect true\x1b[0m
    # 整行都是灰的——Claude Code 把**待发送的输入**也渲染成灰色。
    # 补全建议不长这样：它一定跟在已输入的文字后面，前面有非灰的前缀。
    ANSI_PENDING_INPUT = ("❯ \x1b[0m\x1b[2m再来一次，两组问题都设 multiSelect true"
                          "\x1b[0m\r")

    def test_pending_input_is_not_a_suggestion(self):
        """整行全灰 = 用户待发的输入，不是补全建议。

        真机误报：卡片上会显示「建议：再来一次…（按 Tab 接受）」，完全
        是误导——那是用户自己打的字。
        """
        self.assertEqual(
            herdr_relay.input_suggestion(self.ANSI_PENDING_INPUT), "")

    def test_suggestion_needs_a_typed_prefix(self):
        """有非灰前缀才算建议：`/rev` 是打的，`iew-changes` 是建议。"""
        self.assertEqual(
            herdr_relay.input_suggestion(self.ANSI_WITH_SUGGESTION),
            "iew-changes")

    def test_ignores_pure_padding(self):
        """行尾补空格也是 dim 的，别把空白当建议。"""
        raw = "\x1b[0m❯ \x1b[0m/review\x1b[2m      \x1b[0m\r"
        self.assertEqual(herdr_relay.input_suggestion(raw), "")


class SuggestionDeliveryTests(unittest.TestCase):
    """建议要真的跟着 pane 内容送出去，光有提取函数等于没加。"""

    PANE_TEXT = "line one\n❯ /review-changes"

    def _read(self, suggestion):
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value=self.PANE_TEXT)), \
             mock.patch.object(herdr_relay, "pane_scroll_offset",
                               new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(herdr_relay, "pane_input_suggestion",
                               new=mock.AsyncMock(return_value=suggestion)):
            return asyncio.run(herdr_relay.read_pane_async("w1:p1"))

    def test_suggestion_is_appended(self):
        out = self._read("iew-changes")
        self.assertIn("iew-changes", out)
        self.assertIn(herdr_relay.SUGGESTION_MARK, out)

    def test_no_suggestion_adds_nothing(self):
        """主路径：没有建议时一个字都不加。"""
        out = self._read("")
        self.assertNotIn(herdr_relay.SUGGESTION_MARK, out)
        self.assertEqual(out, self.PANE_TEXT)

    def test_content_is_preserved(self):
        out = self._read("iew-changes")
        self.assertIn("line one", out)

    def test_suggestion_goes_last(self):
        """建议挂在末尾：它是补充信息，不该挤在正文前面。"""
        out = self._read("iew-changes")
        self.assertTrue(out.splitlines()[-1].startswith(herdr_relay.SUGGESTION_MARK))

    def test_mark_mentions_tab(self):
        """得让人知道按 Tab 能接受。"""
        self.assertIn("Tab", herdr_relay.SUGGESTION_MARK)

    def test_suggestion_failure_does_not_break_read(self):
        """取建议失败绝不能拖垮抓屏——那是主路径。"""
        with mock.patch.object(herdr_relay, "run_herdr_async",
                               new=mock.AsyncMock(return_value=self.PANE_TEXT)), \
             mock.patch.object(herdr_relay, "pane_scroll_offset",
                               new=mock.AsyncMock(return_value=0)), \
             mock.patch.object(herdr_relay, "pane_input_suggestion",
                               new=mock.AsyncMock(side_effect=RuntimeError("boom"))):
            out = asyncio.run(herdr_relay.read_pane_async("w1:p1"))
        self.assertIn("line one", out)
        self.assertNotIn(herdr_relay.SUGGESTION_MARK, out)
