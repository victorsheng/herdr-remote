#!/usr/bin/env python3
"""Cursor paste+Enter 残留：send_text 后必须 settle，避免跟进框留下原文。

复现：pane send-text 后无间隔立刻 send-keys Enter 时，Cursor Agent 会提交
prompt，但 → 跟进框仍保留同一段文字；下一次远程输入会拼到残留后面。
Claude/Codex 无此问题。relay 在 cursor 的 send_text 路径上插入短 settle。
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class CursorPasteSettleTests(unittest.TestCase):
    def test_is_cursor_agent_names(self):
        self.assertTrue(herdr_relay.is_cursor_agent("cursor"))
        self.assertTrue(herdr_relay.is_cursor_agent("Cursor"))
        self.assertTrue(herdr_relay.is_cursor_agent("cursor-agent"))
        self.assertFalse(herdr_relay.is_cursor_agent("claude"))
        self.assertFalse(herdr_relay.is_cursor_agent("codex"))
        self.assertFalse(herdr_relay.is_cursor_agent(""))
        self.assertFalse(herdr_relay.is_cursor_agent(None))

    def test_settle_sleeps_only_for_cursor(self):
        with patch.object(herdr_relay.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            asyncio.run(herdr_relay.settle_after_paste("cursor"))
            sleep.assert_awaited_once_with(herdr_relay.CURSOR_PASTE_SETTLE_S)

        with patch.object(herdr_relay.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            asyncio.run(herdr_relay.settle_after_paste("claude"))
            sleep.assert_not_awaited()

    def test_settle_duration_is_enough(self):
        # 实测 ≥100ms 可稳定清空；常量不得低于此门槛，否则回归会再次残留。
        self.assertGreaterEqual(herdr_relay.CURSOR_PASTE_SETTLE_S, 0.1)

    def test_send_text_path_calls_settle(self):
        src = (ROOT / "relay" / "herdr_relay.py").read_text()
        self.assertIn("settle_after_paste", src)
        # send_text 分支在 paste 之后必须 settle；用相对位置约束，避免只 import 不用。
        send_text_idx = src.index('elif msg_type == "send_text":')
        settle_idx = src.index("await settle_after_paste(agent_name)", send_text_idx)
        # send_text 之后的下一分支随功能增减会变；只要 settle 落在本分支内即可。
        next_branch = src.index("elif msg_type ==", settle_idx + 1)
        self.assertLess(settle_idx, next_branch,
                        "settle_after_paste 必须落在 send_text 分支内")


if __name__ == "__main__":
    unittest.main()
