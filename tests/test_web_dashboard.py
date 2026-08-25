#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""herdr_web 的单测：把散在各处的群/绑定/agent 信息聚合成一张表。

这个服务存在的理由就是聚合——同一个群的信息现在散在四个地方：
  lark_chats.json     它授权了吗
  lark_bindings.json  绑了哪个 pane
  relay 的 agents     那个 pane 是什么项目、什么状态
  飞书 API            群叫什么名字
observer.jsonl        质检有没有报过它

要回答「这个群到底怎么回事」得手工翻四处，翻错一处结论就反了——前面
排查 observer 盲区时就吃过这个亏（群看得见但读不了消息）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

_TMP = tempfile.mkdtemp(prefix="web-test-")
os.environ["HERDR_LARK_BINDING_PATH"] = os.path.join(_TMP, "bindings.json")
os.environ["HERDR_LARK_CHATS_PATH"] = os.path.join(_TMP, "chats.json")
os.environ["HERDR_OBSERVER_LOG"] = os.path.join(_TMP, "observer.jsonl")

import herdr_web


def _write(path, payload):
    with open(path, "w") as fh:
        json.dump(payload, fh)


class LoadJsonTests(unittest.TestCase):
    """读盘的容错。这些文件是别的进程在写，随时可能读到半截或不存在。"""

    def test_missing_file_returns_default(self):
        self.assertEqual(
            herdr_web.load_json(os.path.join(_TMP, "nope.json"), {}), {})

    def test_broken_json_returns_default(self):
        path = os.path.join(_TMP, "broken.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(herdr_web.load_json(path, {}), {})

    def test_reads_valid_payload(self):
        path = os.path.join(_TMP, "ok.json")
        _write(path, {"a": "b"})
        self.assertEqual(herdr_web.load_json(path, {}), {"a": "b"})

    def test_default_is_not_shared(self):
        """默认值不能是同一个对象——调用方改了会污染下一次。"""
        first = herdr_web.load_json(os.path.join(_TMP, "nope.json"), {})
        first["dirty"] = 1
        self.assertEqual(
            herdr_web.load_json(os.path.join(_TMP, "nope.json"), {}), {})


class ChatRowTests(unittest.TestCase):
    """一个群一行，把四处的信息拼齐。"""

    CHATS = ["oc_bound", "oc_idle"]
    BINDINGS = {"oc_bound": "w1:p1"}
    AGENTS = [
        {"pane_id": "w1:p1", "project": "alpha", "agent": "claude",
         "status": "working", "host": "local"},
        {"pane_id": "w9:p1", "project": "orphan", "agent": "codex",
         "status": "idle", "host": "local"},
    ]
    NAMES = {"oc_bound": "🟡 alpha", "oc_idle": "herdr · 闲置"}

    def _rows(self, **kw):
        return herdr_web.build_chat_rows(
            chats=kw.get("chats", self.CHATS),
            bindings=kw.get("bindings", self.BINDINGS),
            agents=kw.get("agents", self.AGENTS),
            names=kw.get("names", self.NAMES),
            observer_visible=kw.get("observer_visible", {"oc_bound"}),
        )

    def test_one_row_per_chat(self):
        self.assertEqual(len(self._rows()), 2)

    def test_bound_chat_carries_agent_detail(self):
        row = next(r for r in self._rows() if r["chat_id"] == "oc_bound")
        self.assertEqual(row["pane_id"], "w1:p1")
        self.assertEqual(row["project"], "alpha")
        self.assertEqual(row["status"], "working")

    def test_unbound_chat_has_no_agent(self):
        row = next(r for r in self._rows() if r["chat_id"] == "oc_idle")
        self.assertIsNone(row["pane_id"])
        self.assertEqual(row["status"], "unbound")

    def test_name_is_resolved(self):
        row = next(r for r in self._rows() if r["chat_id"] == "oc_bound")
        self.assertEqual(row["name"], "🟡 alpha")

    def test_unknown_name_falls_back_to_id(self):
        """拿不到群名时显示 id，别显示空白——空白让人以为出错了。"""
        rows = herdr_web.build_chat_rows(
            chats=["oc_x"], bindings={}, agents=[], names={},
            observer_visible=set())
        self.assertIn("oc_x", rows[0]["name"])

    def test_observer_blind_spot_is_flagged(self):
        """observer 进不去的群要标出来——那里所有质检都不生效。

        这是排查时反复踩到的盲区：群看得见（chat.get 成功），但读消息
        报 230002，于是 observer 的检查在那些群里静默失效。
        """
        rows = self._rows()
        bound = next(r for r in rows if r["chat_id"] == "oc_bound")
        idle = next(r for r in rows if r["chat_id"] == "oc_idle")
        self.assertTrue(bound["observer_visible"])
        self.assertFalse(idle["observer_visible"])

    def test_dangling_binding_is_flagged(self):
        """绑定指向的 pane 已经不在了——那是残留，该清理。"""
        rows = herdr_web.build_chat_rows(
            chats=["oc_gone"], bindings={"oc_gone": "w404:p1"},
            agents=self.AGENTS, names={}, observer_visible=set())
        self.assertEqual(rows[0]["status"], "dangling")

    def test_rows_are_sorted_stably(self):
        """顺序要稳定，否则每次刷新行都在跳。"""
        first = [r["chat_id"] for r in self._rows()]
        second = [r["chat_id"] for r in self._rows()]
        self.assertEqual(first, second)


class OrphanAgentTests(unittest.TestCase):
    """没有群绑着的 agent 也要列出来——它的通知发不出去。"""

    def test_lists_agents_without_a_chat(self):
        orphans = herdr_web.orphan_agents(
            agents=ChatRowTests.AGENTS, bindings=ChatRowTests.BINDINGS)
        self.assertEqual([a["pane_id"] for a in orphans], ["w9:p1"])

    def test_bound_agents_are_excluded(self):
        orphans = herdr_web.orphan_agents(
            agents=ChatRowTests.AGENTS,
            bindings={"oc_a": "w1:p1", "oc_b": "w9:p1"})
        self.assertEqual(orphans, [])


class FindingSummaryTests(unittest.TestCase):
    """observer 的质检结果，按规则汇总。"""

    def test_counts_by_rule(self):
        rows = [
            {"verdict": "content", "problems": [{"rule": "a"}, {"rule": "b"}]},
            {"verdict": "content", "problems": [{"rule": "a"}]},
            {"verdict": "missing", "kind": "finish"},
        ]
        summary = herdr_web.summarize_findings(rows)
        self.assertEqual(summary["a"], 2)
        self.assertEqual(summary["b"], 1)
        self.assertEqual(summary["missing/finish"], 1)

    def test_empty_input(self):
        self.assertEqual(herdr_web.summarize_findings([]), {})

    def test_tolerates_malformed_rows(self):
        """observer.jsonl 是边写边读的，可能读到半截行。

        两条都缺 verdict，归到同一个 unknown 桶里——重点是别抛异常，
        面板挂掉比显示一个 unknown 糟得多。
        """
        self.assertEqual(herdr_web.summarize_findings([{}, {"verdict": None}]),
                         {"unknown/None": 2})

    def test_ignores_non_dict_rows(self):
        self.assertEqual(herdr_web.summarize_findings(["garbage", None]), {})


class RenderTests(unittest.TestCase):
    """页面渲染。不测样式，只测「该出现的信息都在」。"""

    def test_renders_chat_ids(self):
        html = herdr_web.render_page({
            "chats": [{"chat_id": "oc_1", "name": "群一", "pane_id": "w1:p1",
                       "project": "alpha", "agent": "claude",
                       "status": "working", "observer_visible": True}],
            "orphans": [], "findings": {}, "generated_at": "2026-08-25 10:00",
        })
        self.assertIn("oc_1", html)
        self.assertIn("群一", html)
        self.assertIn("alpha", html)

    def test_escapes_html(self):
        """群名是外部输入，不转义就是 XSS。"""
        html = herdr_web.render_page({
            "chats": [{"chat_id": "oc_1", "name": "<script>bad()</script>",
                       "pane_id": None, "project": None, "agent": None,
                       "status": "unbound", "observer_visible": False}],
            "orphans": [], "findings": {}, "generated_at": "x",
        })
        self.assertNotIn("<script>bad()", html)
        self.assertIn("&lt;script&gt;", html)

    def test_flags_blind_spot_visibly(self):
        html = herdr_web.render_page({
            "chats": [{"chat_id": "oc_1", "name": "群", "pane_id": None,
                       "project": None, "agent": None, "status": "unbound",
                       "observer_visible": False}],
            "orphans": [], "findings": {}, "generated_at": "x",
        })
        self.assertIn("质检盲区", html)

    def test_empty_state_is_explained(self):
        """一个群都没有时给句人话，别只留一张空表。"""
        html = herdr_web.render_page({
            "chats": [], "orphans": [], "findings": {}, "generated_at": "x"})
        self.assertIn("没有", html)


if __name__ == "__main__":
    unittest.main()
