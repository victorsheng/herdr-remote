#!/usr/bin/env python3
"""P1 回归测试：事件路径与轮询路径的行为必须一致，且不得阻塞事件循环。

背景：relay 有两条状态获取路径——2 秒轮询（poll_loop）和 herdr hook 事件
（event_push，经 UDP 从 on_event.py 推入）。事件路径本意是加速，但它：
  1. 不发 Web Push——推送延迟因此仍被 2 秒轮询支配，hook 的意义被抵消
  2. 不做 blocked 去重——轮询路径有 last_statuses 跳变判断，事件路径没有，
     同一 pane 反复推 blocked 会重复广播、重复推送
另外 run_herdr_result 是同步 subprocess.run(timeout=15)，跑在 asyncio
事件循环线程里，N 个 SSH remote 最坏会串行卡死整个 relay。
"""
import asyncio
import inspect
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))

import herdr_relay


def _reset_state():
    herdr_relay.last_statuses.clear()
    herdr_relay.known_panes.clear()
    herdr_relay.pane_remote_map.clear()
    herdr_relay.agent_cache.clear()
    herdr_relay.event_queue = asyncio.Queue()


BLOCKED_EVENT = {
    "type": "agent_event",
    "pane_id": "w1:p1",
    "agent": "claude",
    "status": "blocked",
    "cwd": "/work/alpha",
    "project": "alpha",
    "host": "local",
}


class EventPathPushTests(unittest.IsolatedAsyncioTestCase):
    """事件路径必须与轮询路径一样发 Web Push。"""

    async def asyncSetUp(self):
        _reset_state()

    async def _drive_one_event(self, event):
        """把一个事件推进队列，跑一轮 event_push 后取消。"""
        herdr_relay.event_queue.put_nowait(event)
        task = asyncio.create_task(herdr_relay.event_push())
        # 让 event_push 处理完队列里这一条
        for _ in range(20):
            await asyncio.sleep(0)
            if herdr_relay.event_queue.empty():
                break
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_event_path_sends_web_push(self):
        with mock.patch.object(herdr_relay, "send_web_push",
                               new_callable=mock.AsyncMock) as push, \
             mock.patch.object(herdr_relay, "broadcast", new_callable=mock.AsyncMock), \
             mock.patch.object(herdr_relay, "read_pane_async",
                               new_callable=mock.AsyncMock, return_value="需要授权吗？"):
            await self._drive_one_event(dict(BLOCKED_EVENT))
        self.assertTrue(push.called,
                        "事件路径遇到 blocked 必须发 Web Push（此前只有轮询路径发）")

    async def test_event_path_dedupes_repeated_blocked(self):
        """同一 pane 连续两次 blocked 事件，只应广播/推送一次。"""
        with mock.patch.object(herdr_relay, "send_web_push",
                               new_callable=mock.AsyncMock) as push, \
             mock.patch.object(herdr_relay, "broadcast",
                               new_callable=mock.AsyncMock) as bcast, \
             mock.patch.object(herdr_relay, "read_pane_async",
                               new_callable=mock.AsyncMock, return_value="需要授权吗？"):
            await self._drive_one_event(dict(BLOCKED_EVENT))
            await self._drive_one_event(dict(BLOCKED_EVENT))

        blocked_casts = [c for c in bcast.await_args_list
                         if c.args and c.args[0].get("type") == "blocked"]
        self.assertEqual(len(blocked_casts), 1,
                         f"重复 blocked 事件应去重，实际广播 {len(blocked_casts)} 次")
        self.assertEqual(push.await_count, 1,
                         f"重复 blocked 事件应只推送一次，实际 {push.await_count} 次")

    async def test_event_path_records_status(self):
        """事件路径必须更新 last_statuses，否则与轮询路径互相打架：
        事件先报 blocked，轮询看到 last_statuses 没记录会再报一次。"""
        with mock.patch.object(herdr_relay, "send_web_push", new_callable=mock.AsyncMock), \
             mock.patch.object(herdr_relay, "broadcast", new_callable=mock.AsyncMock), \
             mock.patch.object(herdr_relay, "read_pane_async",
                               new_callable=mock.AsyncMock, return_value="x"):
            await self._drive_one_event(dict(BLOCKED_EVENT))
        self.assertEqual(herdr_relay.last_statuses.get("w1:p1"), "blocked",
                         "事件路径应记录状态到 last_statuses")

    async def test_unblock_via_event_clears_push(self):
        """事件路径报解除 blocked 时，应发 clear 推送关掉通知。"""
        with mock.patch.object(herdr_relay, "send_web_push",
                               new_callable=mock.AsyncMock) as push, \
             mock.patch.object(herdr_relay, "broadcast", new_callable=mock.AsyncMock), \
             mock.patch.object(herdr_relay, "read_pane_async",
                               new_callable=mock.AsyncMock, return_value="x"):
            await self._drive_one_event(dict(BLOCKED_EVENT))
            unblocked = {**BLOCKED_EVENT, "status": "working"}
            await self._drive_one_event(unblocked)

        clear_calls = [c for c in push.await_args_list if c.kwargs.get("clear")]
        self.assertEqual(len(clear_calls), 1,
                         "解除 blocked 应发一次 clear 推送")


class NonBlockingSubprocessTests(unittest.TestCase):
    """herdr 调用不得在事件循环里同步阻塞。"""

    def test_async_runner_exists(self):
        self.assertTrue(hasattr(herdr_relay, "run_herdr_async"),
                        "应提供 run_herdr_async 供协程使用")
        self.assertTrue(inspect.iscoroutinefunction(herdr_relay.run_herdr_async),
                        "run_herdr_async 必须是协程函数")

    def test_read_pane_async_exists(self):
        self.assertTrue(hasattr(herdr_relay, "read_pane_async"),
                        "应提供 read_pane_async")
        self.assertTrue(inspect.iscoroutinefunction(herdr_relay.read_pane_async))

    def test_get_all_agents_async_exists(self):
        self.assertTrue(hasattr(herdr_relay, "get_all_agents_async"),
                        "轮询也要走异步，否则多 remote 仍会卡住事件循环")
        self.assertTrue(inspect.iscoroutinefunction(herdr_relay.get_all_agents_async))

    def test_hot_paths_do_not_call_sync_runner(self):
        """poll_loop / _poll_once / event_push / handle_client 这些协程里
        不应再出现同步的 run_herdr / read_pane / get_all_agents 调用。"""
        src = (ROOT / "relay" / "herdr_relay.py").read_text()
        tree = __import__("ast").parse(src)
        ast = __import__("ast")
        hot = {"_poll_once", "event_push", "handle_client"}
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.name not in hot:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                name = fn.id if isinstance(fn, ast.Name) else None
                if name in {"run_herdr", "run_herdr_result", "read_pane", "get_all_agents"}:
                    offenders.append(f"{node.name} -> {name}")
        self.assertEqual(offenders, [],
                         f"协程里仍有同步 herdr 调用，会阻塞事件循环: {offenders}")


class AsyncRunnerBehaviourTests(unittest.IsolatedAsyncioTestCase):
    """异步 runner 的行为要与同步版一致：失败吞掉返回空串、超时不抛。"""

    async def test_returns_stdout(self):
        with mock.patch.object(herdr_relay, "HERDR", "/bin/echo"):
            out = await herdr_relay.run_herdr_async("hello")
        self.assertEqual(out, "hello")

    async def test_swallows_failure(self):
        with mock.patch.object(herdr_relay, "HERDR", "/nonexistent/herdr-binary"):
            out = await herdr_relay.run_herdr_async("pane", "list")
        self.assertEqual(out, "", "调用失败应返回空串而非抛异常")

    async def test_concurrent_calls_do_not_serialize(self):
        """两个各 sleep 0.3s 的调用并发跑，总耗时应明显小于 0.6s。
        同步 subprocess.run 会串行，异步不会。"""
        with mock.patch.object(herdr_relay, "HERDR", "/bin/sleep"):
            start = asyncio.get_event_loop().time()
            await asyncio.gather(
                herdr_relay.run_herdr_async("0.3"),
                herdr_relay.run_herdr_async("0.3"),
            )
            elapsed = asyncio.get_event_loop().time() - start
        self.assertLess(elapsed, 0.55,
                        f"并发调用被串行化了，耗时 {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
