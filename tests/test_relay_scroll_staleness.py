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
