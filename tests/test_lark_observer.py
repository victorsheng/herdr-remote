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
        self.obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore())

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
        self.obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore())

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
        self.obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore())

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
        obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(), seen=seen)
        obs._check_message("oc_1", "herdr · p", {
            "message_id": "om_ancient", "msg_type": "text",
            "create_time": time.time() - ob.MAX_MESSAGE_AGE - 100,
            "sender": "app", "content": {"text": "老消息"}, "text": "老消息"},
            time.time())
        self.assertNotIn("om_ancient", seen)

    def test_same_message_reported_once_across_scans(self):
        """连扫两遍，同一条坏消息只报一次。"""
        seen = self._store()
        obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(), seen=seen)
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
        self.obs = ob.Observer(unittest.mock.MagicMock(), ob.FindingStore(),
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
        obs = ob.Observer(api, ob.FindingStore(), qc_chat="oc_qc")
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
        obs = ob.Observer(api, ob.FindingStore(), qc_chat="oc_qc")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
