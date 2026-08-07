#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets", "zeroconf", "pywebpush"]
# ///
"""pane_zoom / narrow_mode 的回归测试。

这块能力此前完全缺失：relay 只接了 pane list / read / send-keys / send-text，
herdr 提供的整块布局控制（zoom/split/resize/layout）一个都没接。

窄屏模式的由来：herdr 的 89 个 socket API 方法里没有"设置终端列宽"的接口，
真实 cols/rows 由 attach 的 PTY 客户端决定。但 pane 变窄时 agent 的 TUI 会
响应 SIGWINCH 重排——实测 wB:p1 从 133 列分屏后变 67 列，读回内容的最大列宽
从 132 降到 64。所以靠"分出一个陪衬 pane"来挤窄，让 agent 自己排成窄版。

最容易写错的一点：herdr 在"操作未生效"时同样返回退出码 0。实测单 pane 上
`zoom --on` 得到 changed:false / reason:single_pane、退出码 0——若照搬
send_keys 的"看 returncode 回 ok:true"写法，UI 会报出假的成功。故 zoom 的
ok 必须取自 JSON 的 changed 字段。

测试数据全部取自真实 herdr 输出（herdr 0.x, macOS），不是手工编造的结构。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


# --- 真实 herdr 输出样本 ---------------------------------------------------

# 多 pane 下 zoom --on：生效
ZOOM_CHANGED = json.dumps({"id": "cli:pane:zoom", "result": {"type": "pane_zoom", "zoom": {
    "changed": True, "focus_changed": True, "focused_pane_id": "wM:p1",
    "layout": {"focused_pane_id": "wM:p1",
               "panes": [{"focused": True, "pane_id": "wM:p1"},
                         {"focused": False, "pane_id": "wM:p2"}],
               "splits": [{"direction": "down", "id": "split_0_root", "ratio": 0.5}],
               "tab_id": "wM:t1", "workspace_id": "wM", "zoomed": True},
    "pane_id": "wM:p1", "zoom_changed": True, "zoomed": True}}})

# 已缩放时再 zoom --on：退出码仍为 0，但未生效
ZOOM_ALREADY = json.dumps({"id": "cli:pane:zoom", "result": {"type": "pane_zoom", "zoom": {
    "changed": False, "focus_changed": False, "focused_pane_id": "wM:p1",
    "layout": {"panes": [{"pane_id": "wM:p1"}, {"pane_id": "wM:p2"}],
               "splits": [{"id": "split_0_root"}], "zoomed": True},
    "pane_id": "wM:p1", "reason": "already_zoomed",
    "zoom_changed": False, "zoomed": True}}})

# 单 pane 上 zoom --on：herdr 明确拒绝，但退出码依然是 0
ZOOM_SINGLE_PANE = json.dumps({"id": "cli:pane:zoom", "result": {"type": "pane_zoom", "zoom": {
    "changed": False, "focus_changed": False, "focused_pane_id": "wM:p1",
    "layout": {"panes": [{"focused": True, "pane_id": "wM:p1"}],
               "splits": [], "zoomed": False},
    "pane_id": "wM:p1", "reason": "single_pane",
    "zoom_changed": False, "zoomed": False}}})

# split 的返回结构与 zoom 完全不同：没有 layout / changed，只有新 pane
SPLIT_OK = json.dumps({"id": "cli:pane:split", "result": {"type": "pane_info", "pane": {
    "agent_status": "unknown", "cwd": "/Users/victor/code-tools/niuma",
    "focused": True, "pane_id": "wM:p2", "tab_id": "wM:t1",
    "workspace_id": "wM"}}})


class ZoomResultParsingTests(unittest.TestCase):
    """zoom 的成败必须取自 changed，而不是退出码。"""

    def test_changed_zoom_reports_success(self):
        ok, info = herdr_relay.parse_layout_result(ZOOM_CHANGED, "zoom")
        self.assertTrue(ok)
        self.assertTrue(info["changed"], "生效的 zoom 应报 changed")
        self.assertTrue(info["zoomed"], "zoomed 应反映缩放后的真实状态")

    def test_already_zoomed_is_not_success(self):
        # 退出码为 0，但操作没生效——UI 不该显示"已缩放"的成功提示
        ok, info = herdr_relay.parse_layout_result(ZOOM_ALREADY, "zoom")
        self.assertTrue(ok)
        self.assertFalse(info["changed"], "重复 zoom 不应报成功")
        self.assertEqual(info["reason"], "already_zoomed")

    def test_single_pane_rejection_has_readable_note(self):
        # 单 pane 是最常见的情形（实测 14 个 tab 全是单 pane），
        # 必须给出可读解释而不是静默失败或假成功
        ok, info = herdr_relay.parse_layout_result(ZOOM_SINGLE_PANE, "zoom")
        self.assertTrue(ok)
        self.assertFalse(info["changed"])
        self.assertEqual(info["reason"], "single_pane")
        self.assertIn(info["reason"], herdr_relay.LAYOUT_REASONS,
                      "single_pane 必须有对应的中文说明")

    def test_layout_structure_exposed(self):
        _, info = herdr_relay.parse_layout_result(ZOOM_CHANGED, "zoom")
        self.assertEqual(info["pane_count"], 2)
        self.assertEqual(info["split_count"], 1)

    def test_single_pane_has_no_splits(self):
        _, info = herdr_relay.parse_layout_result(ZOOM_SINGLE_PANE, "zoom")
        self.assertEqual(info["pane_count"], 1)
        self.assertEqual(info["split_count"], 0)

    def test_malformed_output_rejected(self):
        # herdr 超时时 run_herdr_async 返回空串；不能把它当成功
        for bad in ("", "not json", "{}", '{"result": null}',
                    '{"result": {"zoom": "not a dict"}}', None):
            ok, info = herdr_relay.parse_layout_result(bad, "zoom")
            self.assertFalse(ok, f"畸形输出应被拒: {bad!r}")
            self.assertEqual(info, {})

    def test_wrong_key_rejected(self):
        # 用错 key 解析必须失败，而不是静默返回默认值
        ok, _ = herdr_relay.parse_layout_result(ZOOM_CHANGED, "split")
        self.assertFalse(ok)


class SplitResultParsingTests(unittest.TestCase):
    """split 返回 pane_info，结构与 zoom 不同，需单独解析。"""

    def test_new_pane_id_extracted(self):
        self.assertEqual(herdr_relay.parse_split_result(SPLIT_OK), "wM:p2")

    def test_malformed_output_returns_empty(self):
        for bad in ("", "not json", "{}", '{"result": null}',
                    '{"result": {"pane": "x"}}', '{"result": {"pane": {}}}', None):
            self.assertEqual(herdr_relay.parse_split_result(bad), "",
                             f"畸形输出应返回空串: {bad!r}")

    def test_non_string_pane_id_rejected(self):
        bad = json.dumps({"result": {"pane": {"pane_id": {"$ne": None}}, "type": "pane_info"}})
        self.assertEqual(herdr_relay.parse_split_result(bad), "")

    def test_zoom_output_not_mistaken_for_split(self):
        # 两个命令返回结构不同，交叉解析必须失败而非误判成功
        self.assertEqual(herdr_relay.parse_split_result(ZOOM_CHANGED), "")


class LayoutPanesParsingTests(unittest.TestCase):
    """pane layout 输出解析：窄屏模式据此判断是否已分屏。"""

    LAYOUT_SPLIT = json.dumps({"result": {"type": "pane_layout", "layout": {
        "panes": [{"pane_id": "wB:p1", "rect": {"width": 67, "height": 47}},
                  {"pane_id": "wB:p2", "rect": {"width": 66, "height": 47}}],
        "splits": [{"id": "split_0_root", "direction": "right", "ratio": 0.5}]}}})

    LAYOUT_SINGLE = json.dumps({"result": {"type": "pane_layout", "layout": {
        "panes": [{"pane_id": "wB:p1", "rect": {"width": 133, "height": 47}}],
        "splits": []}}})

    def test_split_layout_widths(self):
        # 实测：133 列分屏后变 67，agent 的 TUI 随之重排到 64 列
        panes = herdr_relay.parse_layout_panes(self.LAYOUT_SPLIT)
        self.assertEqual(panes, [("wB:p1", 67), ("wB:p2", 66)])

    def test_single_pane_layout(self):
        panes = herdr_relay.parse_layout_panes(self.LAYOUT_SINGLE)
        self.assertEqual(panes, [("wB:p1", 133)])

    def test_malformed_returns_empty(self):
        for bad in ("", "not json", "{}", '{"result": null}',
                    '{"result": {"layout": "x"}}', None):
            self.assertEqual(herdr_relay.parse_layout_panes(bad), [],
                             f"畸形输出应返回空列表: {bad!r}")

    def test_non_dict_pane_entries_skipped(self):
        bad = json.dumps({"result": {"layout": {"panes": ["x", {"pane_id": "wB:p1", "rect": {}}]}}})
        self.assertEqual(herdr_relay.parse_layout_panes(bad), [("wB:p1", 0)])


class LayoutMessageContractTests(unittest.TestCase):
    """消息处理分支的输入校验：mode 直接进命令行，必须白名单。"""

    def _handler_source(self):
        return (ROOT / "relay" / "herdr_relay.py").read_text(encoding="utf-8")

    def _narrow_branch(self, src):
        """截取 narrow_mode 分支全文（到下一个 elif msg_type 为止）。

        按固定字符数截窗口会随注释增删而失效，这里按分支边界取。
        """
        start = src.index('msg_type == "narrow_mode"')
        nxt = src.find('elif msg_type ==', start + 10)
        return src[start:nxt if nxt != -1 else len(src)]

    def test_zoom_mode_is_allowlisted(self):
        src = self._handler_source()
        self.assertIn('mode not in ("toggle", "on", "off")', src,
                      "zoom mode 未做白名单校验，存在参数注入风险")

    def test_both_verify_pane_is_known(self):
        # 与 send_keys / send_text 一致：未知 pane_id 必须拒绝，
        # 否则可对任意 pane 施加布局操作
        src = self._handler_source()
        for msg_type in ('pane_zoom', 'narrow_mode'):
            start = src.index(f'msg_type == "{msg_type}"')
            nxt = src.find('elif msg_type ==', start + 10)
            window = src[start:nxt if nxt != -1 else len(src)]
            self.assertIn("known_panes", window,
                          f"{msg_type} 未校验 pane_id 是否已知")

    def test_both_are_audited(self):
        src = self._handler_source()
        self.assertIn('audit("pane_zoom"', src, "布局操作必须进审计日志")
        self.assertIn('audit("narrow_mode"', src, "布局操作必须进审计日志")

    def test_narrow_mode_tracks_companion_pane(self):
        # 关闭窄屏时必须只关自己开的陪衬 pane。若靠"关掉同 tab 里另一个 pane"
        # 来推断，会误关用户在 Mac 上手动开的 pane。
        src = self._handler_source()
        self.assertIn("narrow_companions", src,
                      "窄屏模式必须记录陪衬 pane，不能靠推断")
        window = self._narrow_branch(src)
        self.assertIn("narrow_companions[pane_id] = companion", window,
                      "开启窄屏后必须记下陪衬 pane")
        self.assertIn("narrow_companions.pop(pane_id, None)", window,
                      "关闭窄屏后必须清掉映射，否则再也开不了")

    def test_narrow_mode_does_not_steal_focus(self):
        # 挤窄是为手机端可读，不该把 Mac 上的焦点抢到陪衬 shell 上
        src = self._handler_source()
        self.assertIn('"--no-focus"', self._narrow_branch(src),
                      "陪衬 pane 不应抢焦点")

    def test_zoom_ok_derives_from_changed_not_returncode(self):
        # 这是本文件最重要的一条：退出码 0 不等于操作生效
        src = self._handler_source()
        idx = src.index('"command": "pane_zoom"')
        window = src[idx:idx + 200]
        self.assertIn('info["changed"]', window,
                      'pane_zoom 的 ok 必须取自 changed，否则单 pane 时会假报成功')


if __name__ == "__main__":
    unittest.main(verbosity=2)
