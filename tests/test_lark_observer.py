#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["lark-oapi>=1.4.0", "websockets>=14.0"]
# ///
"""herdr_lark_observer 的单测：校验规则与对账逻辑。

重点测「不要造假漏发」——质检工具误报比漏报更糟：一旦开始刷假警报，
人就不看它了，等于白做。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
import unittest.mock

# 落盘路径指到临时目录，别污染真实配置
_TMP = tempfile.mkdtemp(prefix="observer-test-")
os.environ["HERDR_OBSERVER_LOG"] = os.path.join(_TMP, "observer.jsonl")
os.environ.setdefault("HERDR_LARK_OBSERVER_APP_ID", "cli_test")
os.environ.setdefault("HERDR_LARK_OBSERVER_APP_SECRET", "secret_test_value_32chars_xxxxxx")

_spec = importlib.util.spec_from_file_location(
    "ob", os.path.join(os.path.dirname(__file__), "..", "relay", "herdr_lark_observer.py"))
ob = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ob)


class _EverythingBound(frozenset):
    """任何 pane 都算「绑着」。"""

    def __contains__(self, item):
        return True


_ALL_PANES = _EverythingBound()


def make_obs(*args, **kwargs):
    """构造 Observer，并默认「所有 pane 都绑着群」。

    绝大多数测试关心的是对账与漏发判定，与绑定无关。绑定过滤是另一件事，
    由 BindingAwarenessTests 专门盯着——那里用真实文件。
    """
    obs = ob.Observer(*args, **kwargs)
    obs.bound_panes = lambda: _ALL_PANES
    return obs


class ScrubTests(unittest.TestCase):
    """密钥绝不能进日志或质检群。"""

    def test_scrubs_relay_token(self):
        with unittest.mock.patch.object(ob, "_RELAY_TOKEN", "tok-secret"):
            self.assertNotIn("tok-secret", ob.scrub("连接失败 tok-secret"))

    def test_plain_text_untouched(self):
        self.assertEqual(ob.scrub("普通报错"), "普通报错")


class CheckTextTests(unittest.TestCase):
    """内容校验：四类规则。"""

    def test_clean_text_has_no_problems(self):
        self.assertEqual(ob.check_text("✅ tailcale (claude) 停下来了\n\n跑完了"), [])

    def test_empty_message_flagged(self):
        rules = [p["rule"] for p in ob.check_text("   ")]
        self.assertIn("empty_message", rules)

    def test_secret_leak_flagged(self):
        with unittest.mock.patch.object(ob, "_RELAY_TOKEN", "tok-leak-me"):
            rules = [p["rule"] for p in ob.check_text("用 tok-leak-me 登录")]
            self.assertIn("secret_leak", rules)

    def test_mojibake_flagged(self):
        rules = [p["rule"] for p in ob.check_text("测试 �� 乱码")]
        self.assertIn("mojibake", rules)

    def test_normal_cjk_not_flagged_as_mojibake(self):
        """正常中文不能被当成乱码——这是最容易误报的地方。"""
        rules = [p["rule"] for p in ob.check_text("继续改这个函数，跑一下测试")]
        self.assertNotIn("mojibake", rules)

    def test_audit_line_over_limit_flagged(self):
        long_line = "→ send  proj (w1:p1)  " + "x" * 500
        rules = [p["rule"] for p in ob.check_text(long_line)]
        self.assertIn("audit_not_truncated", rules)

    def test_short_audit_line_ok(self):
        rules = [p["rule"] for p in ob.check_text("→ send  proj (w1:p1)  改一下")]
        self.assertEqual(rules, [])

    def test_long_pane_output_is_not_audit_line(self):
        """pane 输出本来就长，不能套审计行的长度规则。"""
        rules = [p["rule"] for p in ob.check_text("✅ proj 停下来了\n\n" + "y" * 2000)]
        self.assertNotIn("audit_not_truncated", rules)


class CardCheckTests(unittest.TestCase):
    """卡片必须有能点的按钮，否则手机上是死卡片。"""

    def test_card_with_button(self):
        card = {"elements": [{"tag": "action", "actions": [{"tag": "button"}]}]}
        self.assertTrue(ob.card_has_buttons(card))

    def test_card_without_button(self):
        card = {"elements": [{"tag": "div", "text": {"content": "只有文字"}}]}
        self.assertFalse(ob.card_has_buttons(card))

    def test_empty_card(self):
        self.assertFalse(ob.card_has_buttons({}))


class OptionCardIntegrityTests(unittest.TestCase):
    """选项卡的选项必须干净、齐全，且按钮和选项对得上。

    真实故障（群里 2026-08-24 21:34 那张卡）：带 preview 的 AskUserQuestion
    把选项和预览面板并排渲染成两列，解析器按行切，右列的边框和别人的预览
    内容混进了选项文字：

        1. 前文单发，最后一条带选项     ┌──────────────┐
        2. 按钮卡在前，上文补在后       │ 【卡片 2/3】 │

    手机上根本看不出 1/2/3 是什么。更严重的是同一原因会**吃掉选项**——
    实测 3 个选项只解析出 2 个，有答案根本点不到，人只能盲选或放弃。

    这两种都要报：污染是「看不懂」，数量不符是「点不到」。
    """

    # 群里那张真实污染卡片的元素结构（选项序号与文字交替出现）
    POLLUTED = {"elements": [
        [{"tag": "text", "text": "⏺ 两个发送点。"}],
        [{"tag": "text", "text": "超长拆多条，正文怎么分配？"}],
        [{"tag": "text", "text": "1."},
         {"tag": "text", "text": " 前文单发，最后一条带选项     ┌────────────┐\n"},
         {"tag": "text", "text": "2."},
         {"tag": "text", "text": " 按钮卡在前，上文补在后       │ 【卡片 2/3】 │\n"},
         {"tag": "text", "text": "3."},
         {"tag": "text", "text": " 只发一条，上文放折叠区       │ 【卡片 3/3】 │"}],
        [{"tag": "button", "text": "1"}, {"tag": "button", "text": "2"},
         {"tag": "button", "text": "3"}],
        [{"tag": "button", "text": "Open output & reply"}],
    ]}

    CLEAN = {"elements": [
        [{"tag": "text", "text": "超长拆多条，正文怎么分配？"}],
        [{"tag": "text", "text": "1."}, {"tag": "text", "text": " 前文单发\n"},
         {"tag": "text", "text": "2."}, {"tag": "text", "text": " 按钮卡在前\n"},
         {"tag": "text", "text": "3."}, {"tag": "text", "text": " 只发一条"}],
        [{"tag": "button", "text": "1"}, {"tag": "button", "text": "2"},
         {"tag": "button", "text": "3"}],
    ]}

    # 按钮少一个：有答案点不到
    SHORT_BUTTONS = {"elements": [
        [{"tag": "text", "text": "1."}, {"tag": "text", "text": " 甲\n"},
         {"tag": "text", "text": "2."}, {"tag": "text", "text": " 乙\n"},
         {"tag": "text", "text": "3."}, {"tag": "text", "text": " 丙"}],
        [{"tag": "button", "text": "1"}, {"tag": "button", "text": "2"}],
    ]}

    def _rules(self, card):
        return {p["rule"] for p in ob.check_option_card(card)}

    def test_clean_card_has_no_problem(self):
        """主路径：正常选项卡一个问题都不该报，否则又是刷屏。"""
        self.assertEqual(ob.check_option_card(self.CLEAN), [])

    def test_polluted_option_text_is_reported(self):
        self.assertIn("option_text_polluted", self._rules(self.POLLUTED))

    def test_pollution_detail_names_the_option(self):
        """报告要能让人一眼看出是哪条选项坏了。

        这张卡三条选项都被污染，所以三条都该报——报告里要带序号和原文
        片段，光说「有选项坏了」没法定位。
        """
        problems = [p for p in ob.check_option_card(self.POLLUTED)
                    if p["rule"] == "option_text_polluted"]
        self.assertEqual(len(problems), 3, problems)
        detail = problems[0]["detail"]
        self.assertIn("选项 1", detail)
        self.assertIn("前文单发", detail)     # 带上原文才能定位

    def test_button_count_mismatch_is_reported(self):
        self.assertIn("option_button_mismatch", self._rules(self.SHORT_BUTTONS))

    def test_mismatch_detail_shows_both_counts(self):
        problems = [p for p in ob.check_option_card(self.SHORT_BUTTONS)
                    if p["rule"] == "option_button_mismatch"]
        self.assertIn("3", problems[0]["detail"])
        self.assertIn("2", problems[0]["detail"])

    def test_non_option_card_is_skipped(self):
        """输出展示卡片没有选项清单，不该被这条规则碰到。"""
        card = {"elements": [[{"tag": "text", "text": "DONE"},
                              {"tag": "text", "text": " · claude"}]]}
        self.assertEqual(ob.check_option_card(card), [])

    def test_degraded_card_is_skipped(self):
        """降级内容看不到元素树，任何结论都是瞎猜——别造假警报。"""
        degraded = {"elements": [[{"tag": "text",
                                   "text": "请升级至最新版本客户端，以查看内容"}]]}
        self.assertEqual(ob.check_option_card(degraded), [])

    def test_empty_card_is_skipped(self):
        self.assertEqual(ob.check_option_card({}), [])

    def test_table_in_option_text_is_not_pollution(self):
        """选项本身就在讲表格时不能误报——它是正文，不是并排面板。

        判据是「框线前有大段空白」，表格的竖线紧贴内容，两者可区分。
        """
        card = {"elements": [
            [{"tag": "text", "text": "1."},
             {"tag": "text", "text": " 用 │ 分隔的表格\n"},
             {"tag": "text", "text": "2."},
             {"tag": "text", "text": " 用逗号分隔"}],
            [{"tag": "button", "text": "1"}, {"tag": "button", "text": "2"}],
        ]}
        self.assertNotIn("option_text_polluted", self._rules(card))


class HiddenOptionLabelTests(unittest.TestCase):
    """选项文字被 preview 面板遮住时，卡片上是占位符——这是信息缺失，要报。

    herdr_lark 遇到「序号后面直接是面板」时会填一个占位符，保住编号和
    按钮（不填的话整组选项会被连续性校验丢掉，人一个都点不到）。

    但占位符意味着**那一项到底是什么，人看不到**。卡片本身「结构完整」，
    所以别的规则都判干净——只有专门认这个占位符才发现得了。
    """

    @staticmethod
    def _card(labels):
        cells = []
        for i, label in enumerate(labels, 1):
            cells.append({"tag": "text", "text": f"{i}."})
            cells.append({"tag": "text", "text": f" {label}\n"})
        return {"elements": [
            cells,
            [{"tag": "button", "text": str(i)} for i in range(1, len(labels) + 1)],
        ]}

    def test_hidden_label_is_reported(self):
        card = self._card(["甲", "乙", "（选项文字被预览面板遮住）"])
        rules = {p["rule"] for p in ob.check_option_card(card)}
        self.assertIn("option_label_hidden", rules)

    def test_detail_names_the_option(self):
        card = self._card(["甲", "乙", "（选项文字被预览面板遮住）"])
        problems = [p for p in ob.check_option_card(card)
                    if p["rule"] == "option_label_hidden"]
        self.assertIn("3", problems[0]["detail"])

    def test_normal_card_is_clean(self):
        """主路径：没有占位符就别报。"""
        self.assertEqual(ob.check_option_card(self._card(["甲", "乙", "丙"])), [])

    def test_multiple_hidden_all_reported(self):
        card = self._card(["甲", "（选项文字被预览面板遮住）",
                           "（选项文字被预览面板遮住）"])
        problems = [p for p in ob.check_option_card(card)
                    if p["rule"] == "option_label_hidden"]
        self.assertEqual(len(problems), 2)

    def test_prose_mentioning_panel_is_not_flagged(self):
        """正常选项里提到「预览面板」这几个字时不能误报。"""
        card = self._card(["改进预览面板布局", "保持现状"])
        rules = {p["rule"] for p in ob.check_option_card(card)}
        self.assertNotIn("option_label_hidden", rules)


class OptionCardFormShapesTests(unittest.TestCase):
    """选项清单有两种形态，都得认得，否则整条规则静默失效。

    发送时（build_option_card 现造的卡片）选项在**一整段 lark_md** 里：
        {"tag":"div","text":{"tag":"lark_md","content":"**2.** 乙方案\\n**3.** 丙方案"}}

    而飞书 message.list **读回来**时被拆成序号/文字交替的降级形态：
        {"tag":"text","text":"2."}, {"tag":"text","text":" 乙方案\\n"}

    只认后者的话，observer 扫群能用（读回来的），但拿现造的卡片自检就
    静默返回空——所有基于选项的规则（污染、按钮数、序号）全部跳过，
    看起来"没问题"其实是没检查。这是反向验证时抓出来的。
    """

    INLINE = {"elements": [
        {"tag": "div", "text": {"tag": "lark_md",
                                "content": "**2.** 乙方案\n**3.** 丙方案"}},
        {"tag": "action", "actions": [
            {"tag": "button", "text": {"tag": "plain_text", "content": "2"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "3"}}]},
    ]}

    def test_inline_markdown_form_is_parsed(self):
        self.assertEqual(ob.parse_option_cells(self.INLINE),
                         [("2", "乙方案"), ("3", "丙方案")])

    def test_inline_form_aligned_is_clean(self):
        self.assertEqual(ob.check_option_card(self.INLINE), [])

    def test_inline_form_mismatch_is_caught(self):
        """本轮的漏检：现造卡片错位时必须报出来。"""
        bad = json.loads(json.dumps(self.INLINE))
        for act in bad["elements"][1]["actions"]:
            act["text"]["content"] = str(int(act["text"]["content"]) - 1)
        self.assertIn("option_number_mismatch",
                      {p["rule"] for p in ob.check_option_card(bad)})

    def test_inline_form_gap_is_caught(self):
        bad = json.loads(json.dumps(self.INLINE))
        bad["elements"][0]["text"]["content"] = "**1.** 甲\n**3.** 丙"
        bad["elements"][1]["actions"] = [
            {"tag": "button", "text": {"tag": "plain_text", "content": "1"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "3"}}]
        self.assertIn("option_number_gap",
                      {p["rule"] for p in ob.check_option_card(bad)})

    def test_inline_form_pollution_is_caught(self):
        bad = json.loads(json.dumps(self.INLINE))
        bad["elements"][0]["text"]["content"] = (
            "**1.** 甲方案     ┌────────────┐\n**2.** 乙方案     │ 预览 │")
        bad["elements"][1]["actions"] = [
            {"tag": "button", "text": {"tag": "plain_text", "content": "1"}},
            {"tag": "button", "text": {"tag": "plain_text", "content": "2"}}]
        self.assertIn("option_text_polluted",
                      {p["rule"] for p in ob.check_option_card(bad)})

    def test_prose_numbers_are_not_options(self):
        """正文里的编号列表不能被当成选项清单——那会到处误报。"""
        card = {"elements": [{"tag": "div", "text": {"tag": "lark_md",
                "content": "改动如下：\n1. 修了 a\n2. 修了 b\n然后跑了测试。"}}]}
        self.assertEqual(ob.check_option_card(card), [])


class OptionNumberOrderTests(unittest.TestCase):
    """选项序号必须连续递增，且和按钮一一对应，否则点了等于乱答。

    真实故障：_parse_groups 的连续性校验「不强求从 1 起」，屏幕滚动把首项
    卷出去时只剩 2./3.，而卡片按列表下标+1 渲染成 1./2.——序号跟屏幕错开
    一位，点「1」实际答的是屏幕 1 号（另一个选项）。

    这条规则是事后防线：修完 lark 侧还要盯着，因为错位在卡片上看不出异常
    （1./2./3. 本身很正常），只有跟按钮对比、看是否跳号才发现得了。
    """

    @staticmethod
    def _card(body_nums, button_nums):
        cells = []
        for n in body_nums:
            cells.append({"tag": "text", "text": f"{n}."})
            cells.append({"tag": "text", "text": f" 选项{n}\n"})
        return {"elements": [
            cells,
            [{"tag": "button", "text": str(n)} for n in button_nums],
        ]}

    def _rules(self, card):
        return {p["rule"] for p in ob.check_option_card(card)}

    def test_aligned_card_is_clean(self):
        """主路径：序号和按钮一致就不该报。"""
        self.assertEqual(ob.check_option_card(self._card([1, 2, 3], [1, 2, 3])), [])

    def test_body_and_buttons_disagree_is_reported(self):
        """正文写 2./3.，按钮却是 1./2.——正是那个错位。"""
        rules = self._rules(self._card([2, 3], [1, 2]))
        self.assertIn("option_number_mismatch", rules)

    def test_mismatch_detail_shows_both_sides(self):
        problems = [p for p in ob.check_option_card(self._card([2, 3], [1, 2]))
                    if p["rule"] == "option_number_mismatch"]
        self.assertTrue(problems)
        detail = problems[0]["detail"]
        self.assertIn("2", detail)
        self.assertIn("1", detail)

    def test_non_sequential_body_is_reported(self):
        """正文里跳号：1. 3. 4. —— 中间那项丢了，人点不到。"""
        self.assertIn("option_number_gap",
                      self._rules(self._card([1, 3, 4], [1, 3, 4])))

    def test_out_of_order_body_is_reported(self):
        """顺序颠倒：2. 1. 3. —— 渲染顺序乱了。"""
        self.assertIn("option_number_gap",
                      self._rules(self._card([2, 1, 3], [2, 1, 3])))

    def test_starting_from_two_is_allowed_when_buttons_agree(self):
        """屏幕滚动导致从 2 起是**正常**的，只要按钮跟着一起从 2 起。

        这是修复后的正确形态，绝不能报——否则每次滚动都刷一条假警报。
        """
        self.assertEqual(ob.check_option_card(self._card([2, 3], [2, 3])), [])

    def test_degraded_card_is_skipped(self):
        degraded = {"elements": [[{"tag": "text",
                                   "text": "请升级至最新版本客户端，以查看内容"}]]}
        self.assertEqual(ob.check_option_card(degraded), [])

    def test_card_without_buttons_only_checks_body(self):
        """没有数字按钮时只校验正文本身的连续性，不报 mismatch。"""
        card = {"elements": [[
            {"tag": "text", "text": "1."}, {"tag": "text", "text": " 甲\n"},
            {"tag": "text", "text": "2."}, {"tag": "text", "text": " 乙"}]]}
        self.assertNotIn("option_number_mismatch", self._rules(card))


class OptionCardCheckWiringTests(unittest.TestCase):
    """校验函数必须真的接进 _check_message，光有函数等于没加。"""

    def _observer(self, tag):
        obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(),
                          seen=ob.SeenStore(
                              os.path.join(_TMP, f"opt-{tag}-{time.time()}.json")))
        obs.report = unittest.mock.MagicMock()
        return obs

    def _check(self, obs, content, mid):
        obs._check_message("oc_1", "herdr · herdr-remote", {
            "message_id": mid, "msg_type": "interactive",
            "create_time": time.time(), "sender": "app",
            "content": content, "text": ""}, time.time())

    def test_polluted_card_is_reported(self):
        obs = self._observer("polluted")
        self._check(obs, OptionCardIntegrityTests.POLLUTED, "om_polluted")
        obs.report.assert_called_once()
        rules = {p["rule"] for p in obs.report.call_args[0][0]["problems"]}
        self.assertIn("option_text_polluted", rules)

    def test_button_mismatch_is_reported(self):
        obs = self._observer("mismatch")
        self._check(obs, OptionCardIntegrityTests.SHORT_BUTTONS, "om_mismatch")
        obs.report.assert_called_once()
        rules = {p["rule"] for p in obs.report.call_args[0][0]["problems"]}
        self.assertIn("option_button_mismatch", rules)

    def test_clean_option_card_is_not_reported(self):
        """主路径：正常选项卡不上报，否则每张选项卡都刷一条。"""
        obs = self._observer("clean")
        self._check(obs, OptionCardIntegrityTests.CLEAN, "om_clean")
        obs.report.assert_not_called()

    def test_output_card_is_not_reported(self):
        """DONE 那类展示卡没有选项清单，不该被这条规则碰到。"""
        obs = self._observer("output")
        self._check(obs, {"elements": [[{"tag": "text", "text": "DONE"},
                                        {"tag": "text", "text": " · claude"}]]},
                    "om_output")
        obs.report.assert_not_called()


class DegradedCardTests(unittest.TestCase):
    """飞书 message.list 对 schema 2.0 卡片只给降级内容。

    实际踩过：拿降级内容判「有没有 button」，把正常的 DONE 卡片和审批
    卡片全判成死卡片，刷了 8 条假警报。
    """

    # 真实抓到的降级返回原文
    DEGRADED = {"title": "herdr-remote 需要确认",
                "elements": [[{"tag": "img", "image_key": "img_v3_02ad_x"},
                              {"tag": "text",
                               "text": "请升级至最新版本客户端，以查看内容"},
                              {"tag": "text", "text": ""}]]}

    def test_recognizes_degraded(self):
        self.assertTrue(ob.card_is_degraded(self.DEGRADED))

    def test_real_card_not_degraded(self):
        real = {"elements": [{"tag": "action", "actions": [{"tag": "button"}]}]}
        self.assertFalse(ob.card_is_degraded(real))

    def test_degraded_card_produces_no_finding(self):
        """降级卡片必须完全不上报——这是修掉假警报的关键。"""
        obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(),
                          seen=ob.SeenStore(os.path.join(_TMP, f"s1-{time.time()}.json")))
        obs.report = unittest.mock.MagicMock()
        obs._check_message("oc_1", "herdr · herdr-remote", {
            "message_id": "om_degraded", "msg_type": "interactive",
            "create_time": time.time(), "sender": "app",
            "content": self.DEGRADED, "text": ""}, time.time())
        obs.report.assert_not_called()

    # 第二种降级文案（实测抓到，与上面那种并存）。只认一种的话，这种
    # 会被当成真元素树，判成「没有按钮」——observer 一启动就刷一屏假警报。
    DEGRADED_ALT = {"title": "", "elements": [[
        {"tag": "text", "text": "卡片内容不支持查看，请在飞书客户端查看"}]]}

    def test_recognizes_alternate_degraded_wording(self):
        self.assertTrue(ob.card_is_degraded(self.DEGRADED_ALT))

    def test_alternate_degraded_produces_no_finding(self):
        obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(),
                          seen=ob.SeenStore(os.path.join(_TMP, f"s2-{time.time()}.json")))
        obs.report = unittest.mock.MagicMock()
        obs._check_message("oc_1", "herdr · herdr-remote", {
            "message_id": "om_degraded_alt", "msg_type": "interactive",
            "create_time": time.time(), "sender": "app",
            "content": self.DEGRADED_ALT, "text": ""}, time.time())
        obs.report.assert_not_called()

    def test_pane_output_containing_word_button_not_treated_as_button(self):
        """pane 输出正文里出现 button 这个词，不等于卡片有按钮。

        实际踩过：一张 DONE 卡片的输出正文里含 'button'，于是被判「有按钮」，
        纯属巧合。降级内容一律跳过，才不会得出这种结论。
        """
        degraded_with_word = {"title": "✅ p", "elements": [[
            {"tag": "text", "text": "请升级至最新版本客户端，以查看内容"},
            {"tag": "text", "text": "代码里提到了 button 这个词"}]]}
        self.assertTrue(ob.card_is_degraded(degraded_with_word))


class FinishTransitionTests(unittest.TestCase):
    """判定口径必须和 herdr_lark.py 的 is_finish_transition 完全一致。

    口径不一致会造成系统性错位：它不推的我却在等，全变成假漏发。
    """

    def setUp(self):
        self.obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore())

    def _agent(self, status, pane="w1:p1"):
        return {"pane_id": pane, "project": "proj", "status": status}

    def test_working_to_idle_creates_expectation(self):
        self.obs.track([self._agent("working")])
        self.obs.track([self._agent("idle")])
        self.assertEqual(len(self.obs.pending), 1)

    def test_blocked_to_done_creates_expectation(self):
        self.obs.track([self._agent("blocked")])
        self.obs.track([self._agent("done")])
        self.assertEqual(len(self.obs.pending), 1)

    def test_first_sighting_creates_nothing(self):
        """启动时一屋子 idle agent 不该各判一条漏发。"""
        self.obs.track([self._agent("idle")])
        self.assertEqual(len(self.obs.pending), 0)

    def test_working_to_blocked_creates_nothing(self):
        """转 blocked 走的是审批卡片路径，不是完成通知。"""
        self.obs.track([self._agent("working")])
        self.obs.track([self._agent("blocked")])
        self.assertEqual(len(self.obs.pending), 0)

    def test_idle_to_idle_creates_nothing(self):
        self.obs.track([self._agent("idle")])
        self.obs.track([self._agent("idle")])
        self.assertEqual(len(self.obs.pending), 0)


class MatchingTests(unittest.TestCase):
    """对账：消息能划掉期望。"""

    def setUp(self):
        self.obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore())

    def _msg(self, text="", msg_type="text", content=None):
        return {"message_id": "m1", "msg_type": msg_type,
                "create_time": time.time(), "sender": "app",
                "content": content if content is not None else {"text": text},
                "text": text}

    def test_matching_text_satisfies_expectation(self):
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "tailcale"})
        self.obs._match("oc_1", "herdr · tailcale", self._msg("✅ tailcale 停下来了"))
        self.assertTrue(self.obs.pending[0].matched)

    def test_unrelated_text_does_not_satisfy(self):
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "tailcale"})
        self.obs._match("oc_1", "herdr · tailcale", self._msg("✅ niuma 停下来了"))
        self.assertFalse(self.obs.pending[0].matched)

    def test_text_does_not_satisfy_card_expectation(self):
        """等的是可点卡片，来一条纯文本不算满足——这正是「少发」的形态。"""
        self.obs.note_expectation("blocked", {"pane_id": "w1:p1", "project": "tailcale"},
                                  options=["1. Yes"])
        self.obs._match("oc_1", "herdr · tailcale", self._msg("tailcale 需要确认"))
        self.assertFalse(self.obs.pending[0].matched)

    def test_card_satisfies_card_expectation(self):
        self.obs.note_expectation("blocked", {"pane_id": "w1:p1", "project": "tailcale"},
                                  options=["1. Yes"])
        card = {"elements": [{"tag": "action", "actions": [{"tag": "button"}]}],
                "header": {"title": {"content": "tailcale"}}}
        self.obs._match("oc_1", "herdr · tailcale", self._msg(msg_type="interactive", content=card))
        self.assertTrue(self.obs.pending[0].matched)


class SweepTests(unittest.TestCase):
    """漏发判定：过了宽限期才算，且只算一次。"""

    def setUp(self):
        self.obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore())

    def test_within_grace_not_reported(self):
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "p"})
        self.obs._sweep_expired(time.time())
        self.assertEqual(self.obs.stats["missing"], 0)
        self.assertEqual(len(self.obs.pending), 1)

    def test_past_grace_reported_as_missing(self):
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "p"})
        self.obs._sweep_expired(time.time() + ob.GRACE_SECONDS + 1)
        self.assertEqual(self.obs.stats["missing"], 1)

    def test_card_expectation_reported_as_card_missing(self):
        self.obs.note_expectation("blocked", {"pane_id": "w1:p1", "project": "p"},
                                  options=["1. Yes"])
        self.obs._sweep_expired(time.time() + ob.GRACE_SECONDS + 1)
        self.assertEqual(self.obs.stats["card_missing"], 1)

    def test_reported_once_not_repeatedly(self):
        """判过一次就从队列摘掉，否则每轮扫描都刷同一条警报。"""
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "p"})
        future = time.time() + ob.GRACE_SECONDS + 1
        self.obs._sweep_expired(future)
        self.obs._sweep_expired(future)
        self.assertEqual(self.obs.stats["missing"], 1)

    def test_matched_expectation_not_reported(self):
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "p"})
        self.obs.pending[0].matched = True
        self.obs._sweep_expired(time.time() + ob.GRACE_SECONDS + 1)
        self.assertEqual(self.obs.stats["missing"], 0)


class StoreTests(unittest.TestCase):
    """落盘：一行一条，能被 grep 和回放。"""

    def test_writes_jsonl(self):
        path = os.path.join(_TMP, f"t-{time.time()}.jsonl")
        store = ob.FindingStore(path)
        store.write({"verdict": "missing", "project": "p"})
        store.write({"verdict": "content", "project": "q"})
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["verdict"], "missing")

    def test_adds_timestamps(self):
        path = os.path.join(_TMP, f"ts-{time.time()}.jsonl")
        store = ob.FindingStore(path)
        store.write({"verdict": "missing"})
        rec = json.loads(open(path, encoding="utf-8").read().strip())
        self.assertIn("ts", rec)
        self.assertIn("ts_iso", rec)

    def test_sample_is_scrubbed(self):
        """报「密钥泄露」时，样本本身不能把密钥又抄进落盘文件。

        实际踩过：抓到 token 泄露，却把带 token 的原文存进了 observer.jsonl。
        """
        path = os.path.join(_TMP, f"scrub-{time.time()}.jsonl")
        store = ob.FindingStore(path)
        with unittest.mock.patch.object(ob, "_RELAY_TOKEN", "tok-must-not-persist"):
            store.write({"verdict": "content",
                         "sample": "用 tok-must-not-persist 登录"})
        body = open(path, encoding="utf-8").read()
        self.assertNotIn("tok-must-not-persist", body)
        self.assertIn("<redacted>", body)

    def test_cjk_not_escaped(self):
        """落盘要能直接肉眼看，中文不能变成 \\uXXXX。"""
        path = os.path.join(_TMP, f"cjk-{time.time()}.jsonl")
        store = ob.FindingStore(path)
        store.write({"note": "漏发了"})
        self.assertIn("漏发了", open(path, encoding="utf-8").read())


class SeenStoreTests(unittest.TestCase):
    """去重表必须落盘：重启后不能把历史消息重判一遍。

    实际踩过：seen_messages 只放内存，服务重启三次，同一条消息报了三次。
    """

    def _store(self, limit=5000):
        return ob.SeenStore(os.path.join(_TMP, f"seen-{time.time()}.json"), limit)

    def test_first_add_is_new(self):
        self.assertTrue(self._store().add("om_1"))

    def test_second_add_is_not_new(self):
        st = self._store()
        st.add("om_1")
        self.assertFalse(st.add("om_1"))

    def test_survives_restart(self):
        path = os.path.join(_TMP, f"persist-{time.time()}.json")
        ob.SeenStore(path).add("om_keep")
        self.assertFalse(ob.SeenStore(path).add("om_keep"))

    def test_empty_id_not_stored(self):
        self.assertFalse(self._store().add(""))

    def test_evicts_oldest_past_limit(self):
        st = self._store(limit=4)
        for i in range(6):
            st.add(f"om_{i}")
        self.assertLessEqual(len(st), 4)

    def test_old_message_not_added_to_seen(self):
        """太老的消息要跳过，且不能占用去重表配额。

        否则启动时一次扫描就塞进 20×群数 个陈旧 id，把真正该记的挤出去。
        """
        seen = self._store()
        obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore(), seen=seen)
        obs._check_message("oc_1", "herdr · p", {
            "message_id": "om_ancient", "msg_type": "text",
            "create_time": time.time() - ob.MAX_MESSAGE_AGE - 100,
            "sender": "app", "content": {"text": "老消息"}, "text": "老消息"},
            time.time())
        self.assertNotIn("om_ancient", seen)

    def test_same_message_reported_once_across_scans(self):
        """连扫两遍，同一条坏消息只报一次。"""
        seen = self._store()
        obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore(), seen=seen)
        obs.report = unittest.mock.MagicMock()
        msg = {"message_id": "om_bad", "msg_type": "text",
               "create_time": time.time(), "sender": "app",
               "content": {"text": "坏 � 消息"}, "text": "坏 � 消息"}
        obs._check_message("oc_1", "herdr · p", msg, time.time())
        obs._check_message("oc_1", "herdr · p", msg, time.time())
        self.assertEqual(obs.report.call_count, 1)


class ChatScopedMatchingTests(unittest.TestCase):
    """对账必须校验群：A 群的消息不能划掉 B 群 agent 的期望。

    不校验的话，真实漏发会被掩盖成「已满足」——质检彻底失效。
    """

    def setUp(self):
        self.obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore(),
                               seen=ob.SeenStore(os.path.join(_TMP, f"c-{time.time()}.json")))

    def _msg(self, text):
        return {"message_id": "m1", "msg_type": "text", "create_time": time.time(),
                "sender": "app", "content": {"text": text}, "text": text}

    def test_matching_chat_satisfies(self):
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "tailcale"})
        self.obs._match("oc_t", "herdr · tailcale", self._msg("✅ tailcale 停下来了"))
        self.assertTrue(self.obs.pending[0].matched)

    def test_other_project_chat_does_not_satisfy(self):
        """niuma 群里出现了 tailcale 字样，也不该划掉 tailcale 的期望。"""
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "tailcale"})
        self.obs._match("oc_n", "herdr · niuma", self._msg("刚才在 niuma 里跑完了"))
        self.assertFalse(self.obs.pending[0].matched)

    def test_non_herdr_chat_ignored(self):
        """机器人被拉进闲聊群，那里的消息不参与对账。"""
        self.obs.note_expectation("finish", {"pane_id": "w1:p1", "project": "tailcale"})
        self.obs._match("oc_x", "午饭吃什么", self._msg("✅ tailcale 停下来了"))
        self.assertFalse(self.obs.pending[0].matched)


class SkipSelfTests(unittest.TestCase):
    """质检群里自己发的报告，不能再被当成待检消息——否则自我循环。"""

    def test_qc_chat_skipped_in_scan(self):
        api = unittest.mock.MagicMock()
        api.recent_messages.return_value = []
        obs = make_obs(api, ob.FindingStore(), qc_chat="oc_qc")
        obs.chats = {"oc_qc": "herdr · 质检", "oc_proj": "herdr · proj"}
        obs.scan_once()
        scanned = [c.args[0] for c in api.recent_messages.call_args_list]
        self.assertNotIn("oc_qc", scanned)
        self.assertIn("oc_proj", scanned)


class ReportTests(unittest.TestCase):
    def test_send_failure_does_not_raise(self):
        """质检群发不出去，也不能把 observer 搞挂。"""
        api = unittest.mock.MagicMock()
        api.send_text.side_effect = RuntimeError("boom")
        obs = make_obs(api, ob.FindingStore(), qc_chat="oc_qc")
        obs.report({"verdict": "missing", "project": "p"})

    def test_missing_finding_shows_pane_and_kind(self):
        text = ob.format_finding({"verdict": "missing", "kind": "finish",
                                  "project": "tailcale", "pane_id": "w1:p1",
                                  "note": "25s 内没出现"})
        self.assertIn("tailcale", text)
        self.assertIn("w1:p1", text)
        self.assertIn("finish", text)

    def test_content_finding_shows_chat_not_bogus_pane(self):
        """内容异常是从消息反查的，没有 pane —— 别打出「? (?)」和「期望: None」。"""
        text = ob.format_finding({
            "verdict": "content", "chat": "herdr · tailcale",
            "problems": [{"rule": "mojibake", "detail": "编码坏了"}],
            "sample": "测试 乱码"})
        self.assertIn("herdr · tailcale", text)
        self.assertIn("mojibake", text)
        self.assertNotIn("? (?)", text)
        self.assertNotIn("None", text)

    def test_sample_newlines_flattened(self):
        """样本里的换行会把一条报告撑成好几屏，压成一行。"""
        text = ob.format_finding({"verdict": "content", "chat": "g",
                                  "sample": "第一行\n第二行"})
        self.assertNotIn("第一行\n第二行", text)
        self.assertIn("⏎", text)


class ProjectChatDetectionTests(unittest.TestCase):
    """哪些群参与对账。

    群名前缀改成状态符号后，原来的 startswith("herdr") 会把所有项目群
    判成无关群——漏发检测直接失效，而且是静默失效（不报错，只是什么都
    不检查了），所以必须有测试兜着。
    """

    def test_recognizes_status_glyph_chats(self):
        for name in ("🔴 datapilot", "🟡 datapilot",
                     "🟢 datapilot", "⚪️ datapilot"):
            self.assertTrue(ob.is_project_chat(name), name)

    def test_recognizes_unbound_and_legacy_names(self):
        self.assertTrue(ob.is_project_chat("herdr"))
        self.assertTrue(ob.is_project_chat("herdr · datapilot"))

    def test_rejects_unrelated_chat(self):
        """有人把机器人拉进闲聊群，那儿的消息不该参与对账。"""
        self.assertFalse(ob.is_project_chat("盛大宝123"))
        self.assertFalse(ob.is_project_chat(""))

    def test_glyph_table_matches_herdr_lark(self):
        """符号表两处各一份，必须一致——不一致会静默漏掉整类群。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "lk_for_glyph_check",
            os.path.join(os.path.dirname(__file__), "..",
                         "relay", "herdr_lark.py"))
        lk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lk)
        self.assertEqual(set(ob._PROJECT_CHAT_GLYPHS),
                         set(lk._STATUS_GLYPHS.values()))


class BindingAwarenessTests(unittest.TestCase):
    """没有群绑着的 pane 本来就不该发通知，别报成漏发。

    herdr_lark 去掉了主群回落：通知只发显式绑过的群，一个都没绑就不发。
    observer 若还按「状态一变就该发」建期望，datapilot6 这种没绑过的
    agent 每次干完活都会被判一次 missing——实测刷了一屏假警报。

    判定口径必须跟着 chats_watching 走，否则对账系统性错位。
    """

    def setUp(self):
        self.path = os.path.join(_TMP, f"bind-{id(self)}.json")
        self.obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(),
                               binding_path=self.path)

    def _write(self, mapping):
        with open(self.path, "w") as fh:
            json.dump(mapping, fh)

    def _finish(self, pane_id):
        """跑一次 working → idle 的完成迁移。"""
        self.obs.track([{"pane_id": pane_id, "status": "working", "project": "p"}])
        self.obs.track([{"pane_id": pane_id, "status": "idle", "project": "p"}])

    def test_bound_pane_still_expected(self):
        """绑了的照常对账——主路径不能被削弱。"""
        self._write({"oc_a": "w1:p1"})
        self._finish("w1:p1")
        self.assertEqual(len(self.obs.pending), 1)

    def test_unbound_pane_creates_no_expectation(self):
        self._write({"oc_a": "w1:p1"})
        self._finish("w9:p1")          # 没有任何群绑 w9:p1
        self.assertEqual(self.obs.pending, [])

    def test_no_bindings_at_all_expects_nothing(self):
        self._write({})
        self._finish("w1:p1")
        self.assertEqual(self.obs.pending, [])

    def test_missing_binding_file_expects_nothing(self):
        """绑定文件还不存在时别凭空造期望。"""
        self._finish("w1:p1")
        self.assertEqual(self.obs.pending, [])

    def test_picks_up_new_binding_without_restart(self):
        """用户 /read 换绑后，observer 不重启也要跟上。"""
        self._write({})
        self._finish("w1:p1")
        self.assertEqual(self.obs.pending, [])
        self._write({"oc_a": "w2:p1"})
        self._finish("w2:p1")
        self.assertEqual(len(self.obs.pending), 1)

    def test_unreadable_bindings_do_not_crash(self):
        """文件坏了就当没绑，不能让质检进程挂掉。"""
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self._finish("w1:p1")
        self.assertEqual(self.obs.pending, [])


class PaneCardHasNoButtonsTests(unittest.TestCase):
    """输出展示卡片本来就没有按钮，不该判成死卡片。

    实测：66 条质检记录里 54 条是 card_no_buttons，全部落在「✅ herdr-remote」
    这类完成通知上。而 herdr_lark.build_pane_card 按设计就不含 button——
    它只是把 pane 输出贴出来看，没有可点的东西。

    规则原来的前提「交互卡片都该有按钮」是错的：只有审批/选择卡片才该有。
    """

    # 真实抓到的完成通知卡片（截短）
    DONE_CARD = {"title": "✅ herdr-remote", "elements": [
        [{"tag": "text", "text": "DONE"}, {"tag": "text", "text": " · claude"}],
        [{"tag": "text", "text": "```\n跑完了，565 passed\n```"}]]}

    # 真实抓到的选择卡片：这个才该有按钮
    OPTIONS_CARD = {"title": "⌨︎ yqg-dw-datapilot6 正在等你选", "elements": [
        [{"tag": "text", "text": "另外有两件事需要你定："}],
        [{"tag": "text", "text": "1."}, {"tag": "text", "text": " 保留归档表\n"}],
        [{"tag": "button", "text": "1", "type": "primary"},
         {"tag": "button", "text": "2", "type": "default"}]]}

    def test_done_card_is_output_only(self):
        self.assertTrue(ob.card_is_output_only(self.DONE_CARD))

    def test_options_card_is_not_output_only(self):
        self.assertFalse(ob.card_is_output_only(self.OPTIONS_CARD))

    def test_working_status_also_output_only(self):
        card = {"title": "▶ p", "elements": [
            [{"tag": "text", "text": "WORKING"}, {"tag": "text", "text": " · claude"}]]}
        self.assertTrue(ob.card_is_output_only(card))

    def test_plain_card_without_status_label_not_exempt(self):
        """没有状态标签的卡片不在豁免范围，免得豁免开得过大。"""
        card = {"title": "p", "elements": [
            [{"tag": "text", "text": "随便一段文字"}]]}
        self.assertFalse(ob.card_is_output_only(card))

    def test_done_card_produces_no_finding(self):
        """回归防线：完成卡片不能再刷 card_no_buttons。"""
        obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore(),
                       seen=ob.SeenStore(os.path.join(_TMP, f"pc-{time.time()}.json")))
        obs.report = unittest.mock.MagicMock()
        obs._check_message("oc_1", "herdr · herdr-remote", {
            "message_id": "om_done_card", "msg_type": "interactive",
            "create_time": time.time(), "sender": "app",
            "content": self.DONE_CARD, "text": ""}, time.time())
        obs.report.assert_not_called()

    def test_options_card_without_buttons_still_reported(self):
        """真问题不能被这个豁免掩盖：该有按钮的卡片没按钮，仍要报。"""
        broken = {"title": "⌨︎ p 正在等你选", "elements": [
            [{"tag": "text", "text": "选一个："}],
            [{"tag": "text", "text": "1."}, {"tag": "text", "text": " 方案甲\n"}]]}
        self.assertFalse(ob.card_is_output_only(broken))
        obs = make_obs(unittest.mock.MagicMock(), ob.FindingStore(),
                       seen=ob.SeenStore(os.path.join(_TMP, f"pb-{time.time()}.json")))
        obs.report = unittest.mock.MagicMock()
        obs._check_message("oc_1", "herdr · p", {
            "message_id": "om_broken_options", "msg_type": "interactive",
            "create_time": time.time(), "sender": "app",
            "content": broken, "text": ""}, time.time())
        obs.report.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
