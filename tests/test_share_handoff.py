"""M6-D 真接线：B站把分享写进 outbox，自己一条都不发。

这里有三个不能退让的点：

- 交接打开时，`Browser._deliver()` **不许**调 `sender`：发送权已经交出去了，
  哪怕交接面坏了也只记 `handoff_unavailable`，绝不偷偷自己发；
- 额度还是照旧先占（`reserve` 是原子的），否则交接就等于把限额抹掉了；
- 没配交接时行为一字不变，老路径照常发——这是可回退的保证。

离线运行：不联网、不读凭证、不启 AstrBot。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent / "plugin" / "astrbot_plugin_feiyu_bili"
sys.path.insert(0, str(TESTS))
import test_plugin as support  # noqa: E402

core = support.core
plugin = support.plugin
BVID = "BV0000000001"
TARGET = "qq-snowluma:FriendMessage:2327106185"
MOMENT = datetime(2026, 10, 25, 14, 0, tzinfo=core.TZ)


class HandoffDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """`_deliver()` 在交接打开时只写账，不发消息。"""

    async def asyncSetUp(self):
        self.run_path = TESTS / "_tmp" / "handoff"
        self.run_path.mkdir(parents=True, exist_ok=True)
        self.db = self.run_path / f"state-{id(self)}.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(self.db) + suffix)
            if stale.exists():
                stale.unlink()
        self.store = core.Store(self.db)
        self.sender = AsyncMock(return_value=True)
        clock = patch.object(core, "now", return_value=MOMENT)
        clock.start()
        self.addCleanup(clock.stop)
        self.browser = core.Browser(self.store, Mock(), AsyncMock(), self.sender,
                                    {"daily_shares": 5, "daily_rounds": 6})

    async def asyncTearDown(self):
        self.store.close()

    def statuses(self):
        rows = self.store.db.execute("SELECT bvid,status FROM deliveries ORDER BY id").fetchall()
        return [(row["bvid"], row["status"]) for row in rows]

    async def test_handoff_enqueues_and_never_sends(self):
        calls = []

        async def handoff(item, target, message):
            calls.append((item.get("bvid"), target, message))
            return True

        self.browser.handoff = handoff
        status = await self.browser._deliver({"bvid": BVID}, TARGET, "今天看到一条，想给你。")
        self.assertEqual(status, "handoff")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], BVID)
        self.assertEqual(calls[0][1], TARGET)
        self.assertIn("想给你", calls[0][2])
        self.assertEqual(self.sender.await_count, 0)  # 这一条根本不走发送
        self.assertEqual(self.statuses(), [(BVID, "handoff")])
        self.assertEqual(self.store.daily_count(MOMENT.date()), 1)  # 额度照旧先占

    async def test_handoff_returning_false_does_not_fall_back(self):
        self.browser.handoff = AsyncMock(return_value=False)
        status = await self.browser._deliver({"bvid": BVID}, TARGET, "正文")
        self.assertEqual(status, "handoff_unavailable")
        self.assertEqual(self.sender.await_count, 0)  # 绝不由 B站补发
        self.assertEqual(self.statuses(), [(BVID, "handoff_unavailable")])

    async def test_handoff_raising_marks_failed_and_still_does_not_send(self):
        async def boom(item, target, message):
            raise RuntimeError("outbox 写不进去")

        self.browser.handoff = boom
        with self.assertRaises(RuntimeError):
            await self.browser._deliver({"bvid": BVID}, TARGET, "正文")
        self.assertEqual(self.sender.await_count, 0)
        self.assertEqual(self.statuses(), [(BVID, "handoff_failed")])

    async def test_without_handoff_the_old_path_still_sends(self):
        status = await self.browser._deliver({"bvid": BVID}, TARGET, "正文")
        self.assertEqual(status, "sent")
        self.assertEqual(self.sender.await_count, 1)
        self.assertEqual(self.statuses(), [(BVID, "sent")])

    async def test_duplicate_bvid_is_still_refused_before_handoff(self):
        self.browser.handoff = AsyncMock(return_value=True)
        await self.browser._deliver({"bvid": BVID}, TARGET, "正文")
        again = await self.browser._deliver({"bvid": BVID}, TARGET, "正文")
        self.assertEqual(again, "quota_or_duplicate")
        self.assertEqual(self.browser.handoff.await_count, 1)


class HandoffWriterTests(unittest.IsolatedAsyncioTestCase):
    """插件这一侧的 `_handoff_share()`：写 outbox、幂等、坏了只返回 False。"""

    async def asyncSetUp(self):
        self.run_path = TESTS / "_tmp" / "handoff"
        self.run_path.mkdir(parents=True, exist_ok=True)
        self.db = self.run_path / f"outbox-{id(self)}.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(self.db) + suffix)
            if stale.exists():
                stale.unlink()
        from importlib import util

        spec = util.spec_from_file_location("_feiyu_outbox_handoff", PLUGIN / "share_outbox.py")
        module = util.module_from_spec(spec)
        self.outbox_module = module
        sys.modules["_feiyu_outbox_handoff"] = module
        sys.modules["_feiyu_core_handoff"] = core
        spec.loader.exec_module(module)
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.outbox = module.ShareOutbox(self.conn)
        self.instance = object.__new__(plugin.FeiyuBiliPlugin)
        self.instance.config = {}
        self.instance.outbox = self.outbox
        self.instance.store = None
        clock = patch.object(core, "now", return_value=MOMENT)
        clock.start()
        self.addCleanup(clock.stop)

    async def asyncTearDown(self):
        self.conn.close()

    async def test_generation_is_stable_within_a_day(self):
        first = self.instance._share_generation(TARGET, 1)
        second = self.instance._share_generation(TARGET, 1)
        other_page = self.instance._share_generation(TARGET, 2)
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_page)
        self.assertTrue(first.startswith("g"))
        self.assertEqual(len(first), 24)

    async def test_handoff_writes_a_pending_row(self):
        ok = await self.instance._handoff_share({"bvid": BVID}, TARGET, "想给你看这个。")
        self.assertTrue(ok)
        rows = self.outbox.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bvid"], BVID)
        self.assertEqual(rows[0]["scope"], TARGET)
        self.assertEqual(rows[0]["state"], "pending")

    async def test_same_day_same_video_is_not_written_twice(self):
        await self.instance._handoff_share({"bvid": BVID}, TARGET, "想给你看这个。")
        await self.instance._handoff_share({"bvid": BVID}, TARGET, "想给你看这个。")
        self.assertEqual(len(self.outbox.rows()), 1)
        self.assertEqual(self.outbox.counts().get("pending", 0), 1)

    async def test_empty_body_is_refused_locally(self):
        ok = await self.instance._handoff_share({"bvid": BVID}, TARGET, "   ")
        self.assertFalse(ok)
        self.assertEqual(self.outbox.rows(), [])

    async def test_broken_outbox_only_returns_false(self):
        class Boom:
            def enqueue(self, **kwargs):
                raise RuntimeError("库是只读的")

        self.instance.outbox = Boom()
        ok = await self.instance._handoff_share({"bvid": BVID}, TARGET, "正文")
        self.assertFalse(ok)


class SingleSenderTests(unittest.IsolatedAsyncioTestCase):
    """只剩一个发送者：配置说要交接，就一条都不许自己发。"""

    async def asyncSetUp(self):
        self.run_path = TESTS / "_tmp" / "handoff"
        self.run_path.mkdir(parents=True, exist_ok=True)
        self.db = self.run_path / f"single-{id(self)}.sqlite3"
        for suffix in ("", "-wal", "-shm"):
            stale = Path(str(self.db) + suffix)
            if stale.exists():
                stale.unlink()
        self.store = core.Store(self.db)
        self.sender = AsyncMock(return_value=True)
        clock = patch.object(core, "now", return_value=MOMENT)
        clock.start()
        self.addCleanup(clock.stop)

    async def asyncTearDown(self):
        self.store.close()

    def browser(self, cfg):
        return core.Browser(self.store, Mock(), AsyncMock(), self.sender, cfg)

    async def test_config_says_handoff_but_hook_missing_means_no_send(self):
        browser = self.browser({"daily_shares": 5, "daily_rounds": 6, "share_handoff": True})
        status = await browser._deliver({"bvid": BVID}, TARGET, "正文")
        self.assertEqual(status, "handoff_unavailable")
        self.assertEqual(self.sender.await_count, 0)  # 宁可不发，也不自己发

    async def test_without_handoff_config_the_old_path_still_sends(self):
        browser = self.browser({"daily_shares": 5, "daily_rounds": 6})
        status = await browser._deliver({"bvid": BVID}, TARGET, "正文")
        self.assertEqual(status, "sent")
        self.assertEqual(self.sender.await_count, 1)  # 老行为一字不变

    async def test_flush_pending_hands_off_every_queued_item(self):
        browser = self.browser({"daily_shares": 5, "daily_rounds": 6})
        browser._send_window = lambda automatic: True
        queued = []

        async def handoff(item, target, message):
            queued.append(item.get("bvid"))
            return True

        browser.handoff = handoff
        self.store.offer("BV0000000002", TARGET, "第二条")
        self.store.offer("BV0000000003", TARGET, "第三条")
        handled = await browser.flush_pending(TARGET)
        self.assertEqual(sorted(queued), ["BV0000000002", "BV0000000003"])
        self.assertEqual(handled, 2)
        self.assertEqual(self.sender.await_count, 0)

    async def test_flush_pending_without_handoff_sends_all_not_just_one(self):
        browser = self.browser({"daily_shares": 5, "daily_rounds": 6})
        browser._send_window = lambda automatic: True
        self.store.offer("BV0000000002", TARGET, "第二条")
        self.store.offer("BV0000000003", TARGET, "第三条")
        handled = await browser.flush_pending(TARGET)
        self.assertEqual(handled, 2)  # 以前命中第一条就 return，队列越攒越长
        self.assertEqual(self.sender.await_count, 2)

    async def test_flush_pending_stops_when_handoff_is_unavailable(self):
        browser = self.browser({"daily_shares": 5, "daily_rounds": 6})
        browser._send_window = lambda automatic: True
        browser.handoff = AsyncMock(return_value=False)
        self.store.offer("BV0000000002", TARGET, "第二条")
        self.store.offer("BV0000000003", TARGET, "第三条")
        handled = await browser.flush_pending(TARGET)
        self.assertEqual(handled, 0)
        self.assertEqual(self.sender.await_count, 0)
        # 剩下的那条不该被一起标成失败，留到下次
        left = [row["bvid"] for row in self.store.pending()]
        self.assertIn("BV0000000003", left)


class SourceBoundaryTests(unittest.TestCase):
    """静态边界：交接这一层不联网、不发消息、不碰 deliveries。"""

    def test_handoff_files_stay_offline(self):
        for name in ("core.py", "share_outbox.py", "share_port.py"):
            text = (PLUGIN / name).read_text(encoding="utf-8")
            for bad in ("aiohttp", "httpx", "requests.", "urlopen", "socket."):
                self.assertNotIn(bad, text, f"{name} 不该出现 {bad}")
        core_text = (PLUGIN / "core.py").read_text(encoding="utf-8")
        self.assertEqual(core_text.count("self.sender("), 1, "发送点仍然只有一处")
        outbox_text = (PLUGIN / "share_outbox.py").read_text(encoding="utf-8")
        for bad_sql in ("FROM deliveries", "INTO deliveries", "UPDATE deliveries"):
            self.assertNotIn(bad_sql, outbox_text, "outbox 不碰 deliveries 表")


if __name__ == "__main__":
    unittest.main()
