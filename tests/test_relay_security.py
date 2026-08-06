#!/usr/bin/env python3
"""P0 修复的回归测试：事件入口校验、token 比较、Telegram 中断键名。

这三项都不是能力缺失而是坏了的行为：
  1. agent_event / UDP / HTTP ?d= 三个入口零 schema 校验，配合默认空 token
     可被同网段任意设备伪造 blocked 事件，诱导用户在通知里点 "Trust always"。
  2. token 用 != 比较，存在时序侧信道。
  3. Telegram /interrupt 发 "Ctrl+c"，relay 白名单只认 "C-c"，会被拒；
     且发完不读响应直接报成功，用户看到假的 "Sent Ctrl+C"。
"""
import ast
import asyncio
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


class EventValidationTests(unittest.TestCase):
    """事件入口必须校验 schema，拒绝畸形/伪造事件。"""

    def test_valid_event_accepted(self):
        ok, reason = herdr_relay.validate_agent_event({
            "pane_id": "w1:p1", "agent": "claude", "status": "blocked",
            "cwd": "/work/a", "project": "a",
        })
        self.assertTrue(ok, f"合法事件不应被拒: {reason}")

    def test_non_dict_rejected(self):
        for bad in ([], "x", 42, None):
            ok, _ = herdr_relay.validate_agent_event(bad)
            self.assertFalse(ok, f"非 dict 应被拒: {bad!r}")

    def test_missing_pane_id_rejected(self):
        ok, _ = herdr_relay.validate_agent_event({"agent": "claude", "status": "idle"})
        self.assertFalse(ok, "缺 pane_id 应被拒")

    def test_unknown_status_rejected(self):
        # 状态值必须是已知枚举，否则伪造者可注入任意状态串扰乱客户端分组
        ok, _ = herdr_relay.validate_agent_event({
            "pane_id": "w1:p1", "status": "'; DROP TABLE--",
        })
        self.assertFalse(ok, "未知 status 应被拒")

    def test_oversized_field_rejected(self):
        # 无长度上限时，伪造者可用超长 prompt 撑爆客户端渲染或推送体积
        ok, _ = herdr_relay.validate_agent_event({
            "pane_id": "w1:p1", "status": "blocked", "prompt": "x" * 100_000,
        })
        self.assertFalse(ok, "超长字段应被拒")

    def test_non_string_pane_id_rejected(self):
        ok, _ = herdr_relay.validate_agent_event({"pane_id": {"$ne": None}, "status": "idle"})
        self.assertFalse(ok, "非字符串 pane_id 应被拒")


class TokenComparisonTests(unittest.TestCase):
    """token 比较必须走 secrets.compare_digest，避免时序侧信道。"""

    def test_uses_constant_time_compare(self):
        src = (ROOT / "relay" / "herdr_relay.py").read_text()
        self.assertIn("compare_digest", src,
                      "token 比较应使用 secrets.compare_digest 而非 != ")

    def test_no_naive_token_inequality(self):
        src = (ROOT / "relay" / "herdr_relay.py").read_text()
        self.assertNotRegex(src, r"if\s+token\s*!=\s*AUTH_TOKEN",
                            "不应再有裸的 token != AUTH_TOKEN 比较")

    def test_compare_helper_behaviour(self):
        self.assertTrue(herdr_relay.token_matches("abc", "abc"))
        self.assertFalse(herdr_relay.token_matches("abc", "abd"))
        self.assertFalse(herdr_relay.token_matches("", "abc"))
        # 空配置意味着未启用鉴权，调用方自行决定是否放行；
        # 这里只保证空 token 不会意外匹配到非空值
        self.assertFalse(herdr_relay.token_matches("abc", ""))


class TelegramInterruptTests(unittest.TestCase):
    """/interrupt 必须用 relay 认识的键名，且必须读 relay 的响应。"""

    def setUp(self):
        self.src = (ROOT / "relay" / "herdr_telegram.py").read_text()

    def test_no_unknown_ctrl_key_name(self):
        # relay 的 SAFE_KEYS 只有 "C-c"，"Ctrl+c" 会被整条拒绝
        self.assertNotIn('"Ctrl+c"', self.src,
                         '不应再出现 relay 不认识的 "Ctrl+c"，应为 "C-c"')

    def test_interrupt_key_in_relay_whitelist(self):
        # 从源码里抽出所有传给 send_keys 的键名，逐一比对 relay 白名单
        used = set(re.findall(r'"keys":\s*\[([^\]]*)\]', self.src))
        for group in used:
            for item in re.findall(r'"([^"]+)"', group):
                self.assertIn(item, herdr_relay.SAFE_KEYS,
                              f'键名 {item!r} 不在 relay 的 SAFE_KEYS 中，会被拒绝')

    def test_interrupt_paths_read_relay_response(self):
        """两处 interrupt 都必须走会读 ack 的 send_keys_to_relay，
        而不是自己 connect 完就报成功。"""
        tree = ast.parse(self.src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(self.src, node) or ""
            if "interrupt" not in seg.lower():
                continue
            # 裸建连接发 send_keys 却不检查响应 = 假成功
            if '"type": "send_keys"' in seg and "send_keys_to_relay" not in seg:
                offenders.append(node.name)
        self.assertEqual(offenders, [],
                         f"这些函数发 send_keys 但不读 relay 响应，会报假成功: {offenders}")


class EventQueueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """三个入口共用同一个校验函数，畸形事件不得入队。"""

    async def test_udp_drops_invalid_event(self):
        herdr_relay.event_queue = asyncio.Queue()
        proto = herdr_relay.UDPPlugin()
        proto.datagram_received(json.dumps({"pane_id": "w1:p1", "status": "bogus"}).encode(),
                                ("127.0.0.1", 1234))
        self.assertTrue(herdr_relay.event_queue.empty(), "畸形 UDP 事件不应入队")

    async def test_udp_accepts_valid_event(self):
        herdr_relay.event_queue = asyncio.Queue()
        proto = herdr_relay.UDPPlugin()
        good = {"pane_id": "w1:p1", "agent": "claude", "status": "blocked",
                "cwd": "/w/a", "project": "a"}
        proto.datagram_received(json.dumps(good).encode(), ("127.0.0.1", 1234))
        self.assertFalse(herdr_relay.event_queue.empty(), "合法 UDP 事件应入队")


if __name__ == "__main__":
    unittest.main()
