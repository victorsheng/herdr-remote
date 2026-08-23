#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# ///
"""herdr_usage 的单测：周期边界与聚合口径。

边界算错的后果很具体：「还剩多久重置」会误导人按错的节奏干活，
所以这里重点测 5h 窗对齐和周锚点。
"""
import importlib.util
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta

_spec = importlib.util.spec_from_file_location(
    "us", os.path.join(os.path.dirname(__file__), "..", "relay", "herdr_usage.py"))
us = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(us)


def dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm).astimezone()


class WindowStartTests(unittest.TestCase):
    """5 小时窗按整点对齐（0/5/10/15/20 点）。

    不能用「从现在往前推 5 小时」——那样窗口永远滑动，
    「还剩多久重置」就没有意义了。
    """

    def test_aligns_to_block(self):
        self.assertEqual(us.window_start(dt(2026, 8, 23, 12, 50)).hour, 10)

    def test_start_of_block_is_itself(self):
        self.assertEqual(us.window_start(dt(2026, 8, 23, 10, 0)).hour, 10)

    def test_just_before_next_block(self):
        self.assertEqual(us.window_start(dt(2026, 8, 23, 14, 59)).hour, 10)

    def test_crosses_into_next_block(self):
        self.assertEqual(us.window_start(dt(2026, 8, 23, 15, 0)).hour, 15)

    def test_midnight_block(self):
        self.assertEqual(us.window_start(dt(2026, 8, 23, 3, 30)).hour, 0)

    def test_last_block_of_day(self):
        self.assertEqual(us.window_start(dt(2026, 8, 23, 23, 59)).hour, 20)

    def test_zeroes_minutes(self):
        w = us.window_start(dt(2026, 8, 23, 12, 50))
        self.assertEqual((w.minute, w.second), (0, 0))


class WeekStartTests(unittest.TestCase):
    """周锚点可配：Anthropic 的周额度按订阅日重置，不一定是周一。"""

    # 2026-08-23 是周日
    SUNDAY = dt(2026, 8, 23, 12, 0)

    def test_monday_anchor_default(self):
        self.assertEqual(us.week_start(self.SUNDAY, anchor=0).day, 17)

    def test_sunday_anchor(self):
        """周日起算时，周日本身就是起点。"""
        self.assertEqual(us.week_start(self.SUNDAY, anchor=6).day, 23)

    def test_wednesday_anchor(self):
        self.assertEqual(us.week_start(self.SUNDAY, anchor=2).day, 19)

    def test_anchor_on_its_own_weekday(self):
        monday = dt(2026, 8, 17, 9, 0)
        self.assertEqual(us.week_start(monday, anchor=0).day, 17)

    def test_zeroes_time(self):
        w = us.week_start(self.SUNDAY, anchor=0)
        self.assertEqual((w.hour, w.minute), (0, 0))

    def test_anchor_wraps(self):
        """anchor=7 等于 anchor=0，别让越界值算出奇怪的日期。"""
        self.assertEqual(us.week_start(self.SUNDAY, anchor=7),
                         us.week_start(self.SUNDAY, anchor=0))


class BucketTests(unittest.TestCase):
    """聚合口径：缓存读不计入总量。"""

    def _bucket(self, limit=0):
        return us.Bucket("测试", dt(2026, 8, 23), dt(2026, 8, 24), limit)

    USAGE = {"input_tokens": 100, "output_tokens": 200,
             "cache_read_input_tokens": 50000, "cache_creation_input_tokens": 300}

    def test_total_excludes_cache_read(self):
        """缓存读按折扣计费，算进来会让数字虚高十倍，看不出真实消耗。"""
        b = self._bucket()
        b.add(self.USAGE, "claude-opus-5", "proj", "s1")
        self.assertEqual(b.total, 600)          # 100+200+300，不含 50000

    def test_cache_read_still_tracked(self):
        b = self._bucket()
        b.add(self.USAGE, "claude-opus-5", "proj", "s1")
        self.assertEqual(b.cache_read, 50000)

    def test_accumulates(self):
        b = self._bucket()
        for _ in range(3):
            b.add(self.USAGE, "claude-opus-5", "proj", "s1")
        self.assertEqual(b.total, 1800)
        self.assertEqual(b.messages, 3)

    def test_sessions_deduped(self):
        b = self._bucket()
        b.add(self.USAGE, "m", "p", "same")
        b.add(self.USAGE, "m", "p", "same")
        self.assertEqual(len(b.sessions), 1)

    def test_pct_none_without_limit(self):
        """没配上限就不能算百分比——不假装知道额度。"""
        self.assertIsNone(self._bucket().pct)

    def test_pct_with_limit(self):
        b = self._bucket(limit=1000)
        b.add(self.USAGE, "m", "p", "s")
        self.assertAlmostEqual(b.pct, 60.0)

    def test_missing_fields_default_zero(self):
        b = self._bucket()
        b.add({}, "m", "p", "s")
        self.assertEqual(b.total, 0)

    def test_null_fields_treated_as_zero(self):
        """日志里字段可能是 null，不能让它把加法搞崩。"""
        b = self._bucket()
        b.add({"input_tokens": None, "output_tokens": 5}, "m", "p", "s")
        self.assertEqual(b.total, 5)


class FormatTests(unittest.TestCase):
    def test_human_scales(self):
        self.assertEqual(us.human(999), "999")
        self.assertIn("K", us.human(1500))
        self.assertIn("M", us.human(2_000_000))
        self.assertIn("B", us.human(3_000_000_000))

    def test_bar_bounds(self):
        self.assertEqual(us.bar(0), "○" * 10)
        self.assertEqual(us.bar(100), "●" * 10)

    def test_bar_clamps_over_100(self):
        """超额时别画出比条更长的东西。"""
        self.assertEqual(len(us.bar(250)), 10)

    def test_short_model_strips_date(self):
        self.assertEqual(us.short_model("claude-haiku-4-5-20251001"), "haiku-4-5")

    def test_short_model_keeps_plain(self):
        self.assertEqual(us.short_model("claude-opus-5"), "opus-5")

    def test_short_project(self):
        self.assertEqual(
            us.short_project("-Users-victor-code-github-herdr-remote"), "remote")

    def test_duration_days(self):
        self.assertIn("天", us.fmt_duration(2 * 86400 + 3600))

    def test_duration_minutes_only(self):
        self.assertIn("分", us.fmt_duration(600))

    def test_duration_never_negative(self):
        """周期已经过去时，别显示负数。"""
        self.assertEqual(us.fmt_duration(-500), "0分")


class CollectTests(unittest.TestCase):
    """端到端：造几条日志，看两个周期各自算对没。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-test-")
        self.proj = os.path.join(self.tmp, "-Users-me-demo")
        os.makedirs(self.proj)

    def _write(self, name, entries):
        path = os.path.join(self.proj, f"{name}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for ts, out in entries:
                fh.write(json.dumps({
                    "timestamp": ts.astimezone().isoformat(),
                    "message": {"model": "claude-opus-5",
                                "usage": {"input_tokens": 0, "output_tokens": out,
                                          "cache_creation_input_tokens": 0,
                                          "cache_read_input_tokens": 0}},
                }) + "\n")
        return path

    def test_splits_window_and_week(self):
        now = dt(2026, 8, 23, 12, 0)          # 周日 12:00，5h 窗是 10:00–15:00
        self._write("s1", [
            (dt(2026, 8, 23, 11, 0), 100),    # 窗内 + 周内
            (dt(2026, 8, 23, 9, 0), 200),     # 窗外 + 周内
            (dt(2026, 8, 18, 9, 0), 400),     # 窗外 + 周内（周一起算）
        ])
        with unittest.mock.patch.object(us, "WEEK_ANCHOR", 0):
            data = us.collect(now=now, root=self.tmp)
        self.assertEqual(data["window"].total, 100)
        self.assertEqual(data["week"].total, 700)

    def test_ignores_entries_before_week(self):
        now = dt(2026, 8, 23, 12, 0)
        self._write("s1", [(dt(2026, 8, 10, 9, 0), 999)])   # 上上周
        with unittest.mock.patch.object(us, "WEEK_ANCHOR", 0):
            data = us.collect(now=now, root=self.tmp)
        self.assertEqual(data["week"].total, 0)

    def test_handles_malformed_lines(self):
        """日志可能被写坏（进程被 kill），不能让统计整个失败。"""
        path = self._write("s1", [(dt(2026, 8, 23, 11, 0), 100)])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"usage": broken json\n')
        data = us.collect(now=dt(2026, 8, 23, 12, 0), root=self.tmp)
        self.assertEqual(data["window"].total, 100)

    def test_empty_dir_is_zero_not_crash(self):
        data = us.collect(now=dt(2026, 8, 23, 12, 0),
                          root=tempfile.mkdtemp(prefix="empty-"))
        self.assertEqual(data["week"].total, 0)

    def test_report_renders(self):
        self._write("s1", [(dt(2026, 8, 23, 11, 0), 100)])
        text = us.format_report(us.collect(now=dt(2026, 8, 23, 12, 0), root=self.tmp))
        self.assertIn("5 小时窗", text)
        self.assertIn("本周", text)

    def test_report_says_limits_unknown(self):
        """没配上限时必须说清楚，别让人以为百分比只是没显示出来。"""
        self._write("s1", [(dt(2026, 8, 23, 11, 0), 100)])
        with unittest.mock.patch.object(us, "LIMIT_5H", 0), \
             unittest.mock.patch.object(us, "LIMIT_WEEK", 0):
            text = us.format_report(us.collect(now=dt(2026, 8, 23, 12, 0),
                                               root=self.tmp))
        self.assertIn("额度上限不在本地日志里", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
