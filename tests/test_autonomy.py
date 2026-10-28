"""v0.2 offline autonomy contracts: real Store/Browser, mocked external effects.

All state lives in ordinary mkdir/UUID .runs children; never loads the HTTP client.
Run with the existing AstrBot Python via unittest discover -s 05_B站浏览/tests.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "astrbot_plugin_feiyu_bili"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load_module("_autonomy_core", "core.py")
exploration = load_module("_autonomy_exploration", "exploration.py")
MOMENT = datetime(2026, 10, 24, 12, tzinfo=core.TZ)
BVS = ["BV" + str(i).zfill(10) for i in range(1, 20)]
TARGET = "offline-owner-session"
FEED = {"source": "feed", "reason": "看看推荐", "rest_minutes": 60}


def video(i=0):
    return {"bvid": BVS[i], "title": "离线视频", "author": "公开作者", "description": "公开简介"}


def decision(**changes):
    value = dict(interested=True, like=True, share=True, note="字幕中的具体内容", message="这个有意思。")
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)


class AutonomyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(__file__).resolve().parent / ".runs"
        self.path = self.root / uuid4().hex
        self.path.mkdir(parents=True)
        self.store = core.Store(self.path / "offline.db")
        self.client = Mock(spec_set=["recommendations", "related", "search", "subtitle", "like"])
        for name in ("recommendations", "related", "search"):
            setattr(self.client, name, AsyncMock(return_value=[video()]))
        self.client.subtitle = AsyncMock(return_value={"text": "实际字幕内容"})
        self.client.like = AsyncMock(return_value={"status": "liked"})
        self.llm = AsyncMock(return_value=decision())
        self.sender = AsyncMock(return_value=True)
        self.browser = core.Browser(self.store, self.client, self.llm, self.sender,
                                    {"daily_rounds": 16, "daily_likes": 3, "daily_shares": 2})
        self.browser.can_like = lambda automatic: True
        self.clock_patch = patch.object(core, "now", return_value=MOMENT)
        self.clock = self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.sleep_patch = patch.object(core.asyncio, "sleep", new_callable=AsyncMock)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    async def asyncTearDown(self):
        self.store.close()
        resolved = self.path.resolve()
        self.assertEqual(resolved.parent, self.root.resolve())
        self.assertEqual(len(resolved.name), 32)
        shutil.rmtree(resolved)

    async def test_run_accepts_a_transcribe_callback_without_mutating_like_gates(self):
        """Regression: the plugin passes transcribe= into run(); an unknown kwarg killed
        every automatic round with TypeError while the browser was a Mock in tests."""
        self.client.subtitle = AsyncMock(return_value={"text": "", "reason": "empty_subtitle"})
        seen = []

        async def cb(bvid, item, automatic, reason):
            seen.append((bvid, automatic, reason))
            return {"ok": True, "text": "机器听写的内容", "like_ok": False, "block": "机器转写",
                    "segments": [], "quality": {"like_ok": False, "reasons": ["too_little_text"]}}

        result = await self.browser.run(target=TARGET, system="人格", memory="", topic="离线",
                                       automatic=True, transcribe=cb)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][2], "empty_subtitle")
        self.assertEqual(result["asr_used"], 1)
        # 转写给了文字，但 like_ok=False：既不能点赞，也不能因此分享。
        self.assertEqual(result["liked"], 0)
        self.assertEqual(self.client.like.await_count, 0)

    def rows(self, table):
        self.assertIn(table, ("settings", "videos", "deliveries", "rounds", "likes", "suggestions"))
        return [dict(row) for row in self.store.db.execute("SELECT * FROM " + table)]

    async def run_browser(self, **kwargs):
        return await self.browser.run(target=TARGET, system="离线人格", plan=kwargs.pop("plan", FEED), **kwargs)

    def remember_seed(self):
        self.store.remember(video(), "历史", "subtitle", dict(note="真实种子", interested=True))

    async def test_feed_plan_uses_recommendations_not_rotating_search(self):
        planner = AsyncMock(return_value=json.dumps(FEED, ensure_ascii=False))
        plan = await exploration.plan_exploration(planner, "你喜欢历史", [], ["硬件"])
        result = await self.run_browser(plan=plan)
        self.assertEqual(result["read"], 1)
        self.client.recommendations.assert_awaited_once_with(limit=12)
        self.client.search.assert_not_awaited()
        self.client.related.assert_not_awaited()
        self.assertIsNone(self.store.get("topic_index"))

    async def test_related_planning_and_execution_require_real_history_seed(self):
        self.remember_seed()
        raw = dict(source="related", reason="继续看历史", rest_minutes=90, seed_bvid=BVS[0])
        planner = AsyncMock(return_value=json.dumps(raw, ensure_ascii=False))
        plan = await exploration.plan_exploration(planner, "你喜欢历史", self.store.history(), [])
        self.assertEqual(plan, raw)
        self.client.related.return_value = [video(1)]
        result = await self.run_browser(plan=plan)
        self.assertEqual(result["read"], 1)
        self.client.related.assert_awaited_once_with(BVS[0], limit=12)
        self.client.search.assert_not_awaited()
        for seed in (BVS[2], "av123", BVS[0] + "\n"):
            with self.subTest(seed=seed):
                with self.assertRaisesRegex(ValueError, "种子未经核验"):
                    await self.run_browser(plan={**raw, "seed_bvid": seed})
        self.client.related.assert_awaited_once()

    async def test_autonomous_browsing_continues_after_first_share(self):
        self.client.recommendations.return_value = [video(i) for i in range(5)]
        result = await self.run_browser(automatic=True)
        self.assertEqual((result["read"], result["liked"], result["sent"]), (3, 3, 1))
        self.assertEqual(self.client.subtitle.await_count, 3)
        self.assertEqual(len(result["preview"]), 1)
        self.sender.assert_awaited_once()

    async def test_malformed_autonomous_decision_skips_item_without_losing_prior_actions(self):
        self.client.recommendations.return_value = [video(0), video(1), video(2)]
        self.llm.side_effect = [decision(share=False), '{bad json', decision(share=False, like=False)]
        result = await self.run_browser(automatic=True)
        self.assertEqual((result['status'], result['read'], result['liked'], result['decision_errors']), ('partial', 3, 1, 1))
        self.client.like.assert_awaited_once_with(BVS[0])
        self.assertFalse(self.store.seen(BVS[1]))
        self.assertTrue(self.store.seen(BVS[2]))
        self.assertEqual(self.rows('rounds')[0]['status'], 'partial')
        self.sender.assert_not_awaited()

    async def test_all_malformed_autonomous_decisions_fail_closed_without_pausing_future_rounds(self):
        self.client.recommendations.return_value = [video(0), video(1)]
        self.llm.return_value = decision(like='true')
        result = await self.run_browser(automatic=True)
        self.assertEqual((result['status'], result['decision_errors']), ('partial', 2))
        self.assertEqual(self.rows('likes'), [])
        self.assertEqual(self.rows('deliveries'), [])
        self.assertEqual(self.rows('videos'), [])
        self.client.like.assert_not_awaited()
        self.sender.assert_not_awaited()
        self.llm.return_value = decision(share=False, like=False)
        result = await self.run_browser(automatic=True)
        self.assertEqual(result['status'], 'ok')

    async def test_like_and_share_are_independent(self):
        self.client.recommendations.return_value = [video(0), video(1), video(2)]
        self.llm.side_effect = [decision(like=True, share=False),
                                decision(like=False, share=True),
                                decision(interested=False, like=True, share=False)]
        result = await self.run_browser()
        self.assertEqual((result["read"], result["liked"], result["sent"]), (3, 1, 1))
        self.client.like.assert_awaited_once_with(BVS[0])
        self.assertEqual(self.rows("deliveries")[0]["bvid"], BVS[1])

    async def test_no_subtitle_overrides_both_model_actions(self):
        self.client.subtitle.return_value = {"text": "", "reason": "missing"}
        result = await self.run_browser(automatic=True)
        self.assertEqual((result["no_subtitle"], result["liked"], result["sent"], result["deferred"]), (1, 0, 0, 0))
        self.assertEqual(result["preview"], [])
        self.client.like.assert_not_awaited()
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("likes"), [])
        self.assertEqual(self.rows("suggestions"), [])
        self.assertEqual(self.rows("videos")[0]["source"], "metadata_only")

    async def test_like_quota_and_duplicates_persist_across_reopen(self):
        self.browser.cfg["daily_likes"] = 1
        self.client.recommendations.return_value = [video(), video(), video(1)]
        self.llm.return_value = decision(share=False)
        result = await self.run_browser()
        self.assertEqual((result["read"], result["liked"]), (2, 1))
        self.client.like.assert_awaited_once_with(BVS[0])
        self.store.close()
        self.store = core.Store(self.path / "offline.db")
        self.browser.store = self.store
        self.assertFalse(self.store.reserve_like(BVS[0], "duplicate", 10))
        self.assertFalse(self.store.reserve_like(BVS[2], "quota", 1))
        self.clock.return_value = MOMENT + timedelta(days=1)
        self.assertFalse(self.store.reserve_like(BVS[0], "next-day duplicate", 10))
        self.client.recommendations.return_value = [video(2)]
        self.assertEqual((await self.run_browser())["liked"], 1)

    async def test_like_timeout_is_uncertain_reserved_before_call_and_never_retried(self):
        async def timeout(_):
            self.assertEqual(self.rows("likes")[0]["status"], "reserved")
            raise asyncio.TimeoutError("offline timeout")
        self.client.like.side_effect = timeout
        self.llm.return_value = decision(share=False)
        result = await self.run_browser()
        self.assertEqual((result["liked"], result["like_uncertain"]), (0, 1))
        self.assertEqual(self.rows("likes")[0]["status"], "uncertain")
        self.assertEqual(self.store.daily_likes(), 1)
        # Reproduce another candidate read without relying on videos deduplication.
        with self.store.db:
            self.store.db.execute("DELETE FROM videos")
        await self.run_browser()
        self.client.like.assert_awaited_once()

    async def test_like_receipts_only_known_states_are_successful(self):
        for i, (answer, status, liked, uncertain) in enumerate([
            ({"status": "already_liked"}, "already_liked", 0, 0),
            ({"status": "uncertain"}, "uncertain", 0, 1),
            (True, "uncertain", 0, 1),
            ({"status": "success"}, "uncertain", 0, 1),
        ]):
            with self.subTest(answer=answer):
                self.clock.return_value = MOMENT + timedelta(days=i)
                self.client.recommendations.return_value = [video(i)]
                self.client.like.return_value = answer
                self.llm.return_value = decision(share=False)
                result = await self.run_browser()
                self.assertEqual((result["liked"], result["like_uncertain"]), (liked, uncertain))
                self.assertEqual(self.rows("likes")[-1]["status"], status)

    async def test_dry_run_is_zero_write_zero_like_zero_send_for_every_source(self):
        self.remember_seed()
        self.client.related.return_value = [video(1)]
        self.client.recommendations.return_value = [video(1)]
        self.client.search.return_value = [video(1)]
        tables = ("settings", "videos", "deliveries", "rounds", "likes", "suggestions")
        before = {table: self.rows(table) for table in tables}
        for plan in (FEED, {"source": "related", "seed_bvid": BVS[0]}, {"source": "search", "query": "历史"}, None):
            with self.subTest(plan=plan):
                result = await self.run_browser(plan=plan, automatic=True, dry_run=True)
                self.assertEqual((result["read"], result["liked"], result["sent"]), (1, 0, 0))
                self.assertEqual(len(result["preview"]), 1)
                self.assertEqual({table: self.rows(table) for table in tables}, before)
        self.client.like.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_night_browses_and_likes_but_queues_qq_until_daytime(self):
        self.clock.return_value = MOMENT.replace(hour=23)
        result = await self.run_browser(automatic=True)
        self.assertEqual((result["read"], result["liked"], result["sent"], result["deferred"]), (1, 1, 0, 1))
        self.assertEqual(self.store.daily_count(), 0)
        self.sender.assert_not_awaited()
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        self.clock.return_value = (MOMENT + timedelta(days=1)).replace(hour=9)
        self.assertEqual(await self.browser.flush_pending(TARGET), 1)
        self.assertEqual(self.rows("suggestions")[0]["status"], "sent")
        self.sender.assert_awaited_once_with(TARGET, result["preview"][0])
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)

    async def test_flush_rejects_changed_target_and_expired_suggestions(self):
        self.store.offer(BVS[0], "old-target", "wrong target")
        self.store.offer(BVS[1], TARGET, "expired")
        with self.store.db:
            self.store.db.execute("UPDATE suggestions SET created_at=? WHERE bvid=?",
                                  ((MOMENT - timedelta(hours=25)).isoformat(), BVS[1]))
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        states = {r["bvid"]: r["status"] for r in self.rows("suggestions")}
        self.assertEqual(states, {BVS[0]: "target_changed", BVS[1]: "expired"})
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])

    async def test_flush_respects_quota_cooldown_and_one_item_per_tick(self):
        self.browser.cfg.update({"quiet_start": 0, "quiet_end": 0})
        self.store.offer(BVS[0], TARGET, "first")
        self.clock.return_value = MOMENT + timedelta(seconds=1)
        self.store.offer(BVS[1], TARGET, "second")
        self.assertEqual(await self.browser.flush_pending(TARGET), 1)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        self.clock.return_value = MOMENT + timedelta(hours=3)
        self.assertEqual(await self.browser.flush_pending(TARGET), 1)
        self.clock.return_value = MOMENT + timedelta(hours=6)
        self.store.offer(BVS[2], TARGET, "quota blocked")
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        self.assertEqual(self.store.daily_count(), 2)
        self.assertEqual(self.store.pending()[0]["bvid"], BVS[2])
        self.assertEqual(self.sender.await_count, 2)

    async def test_flush_skips_already_attempted_bvid_instead_of_starving_queue(self):
        self.browser.cfg.update({"quiet_start": 0, "quiet_end": 0})
        self.store.offer(BVS[0], TARGET, "new eligible")
        self.clock.return_value = MOMENT + timedelta(seconds=1)
        self.store.offer(BVS[1], TARGET, "duplicate pending")
        did = self.store.reserve(video(1), TARGET, "previous uncertain", daily_limit=3)
        self.store.delivery_result(did, "uncertain")
        self.clock.return_value = MOMENT + timedelta(hours=3)
        self.assertEqual(await self.browser.flush_pending(TARGET), 1)
        states = {row["bvid"]: row["status"] for row in self.rows("suggestions")}
        self.assertEqual(states, {BVS[0]: "sent", BVS[1]: "already_attempted"})
        self.sender.assert_awaited_once_with(TARGET, "new eligible")
        self.assertEqual(self.store.daily_count(), 2)

    async def test_queue_gate_is_independent_from_immediate_send_gate(self):
        self.browser.can_send = lambda automatic: False
        self.browser.can_queue = lambda automatic: True
        result = await self.run_browser(automatic=True)
        self.assertEqual((result["liked"], result["sent"], result["deferred"]), (1, 0, 1))
        self.assertEqual(self.store.pending()[0]["bvid"], BVS[0])
        self.client.recommendations.return_value = [video(1)]
        self.browser.can_queue = lambda automatic: False
        result = await self.run_browser(automatic=True)
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(len(self.store.pending()), 1)
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])

    async def test_flush_false_receipt_consumes_quota_without_retry(self):
        self.store.offer(BVS[0], TARGET, "uncertain offer")
        self.sender.return_value = False
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        self.assertEqual(self.rows("suggestions")[0]["status"], "uncertain")
        self.assertEqual(self.store.daily_count(), 1)
        self.clock.return_value = MOMENT + timedelta(hours=3)
        self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        self.sender.assert_awaited_once()

    async def test_share_daily_quota_exhaustion_does_not_stop_browsing_or_likes(self):
        self.browser.cfg["daily_shares"] = 0
        self.client.recommendations.return_value = [video(i) for i in range(3)]
        result = await self.run_browser(automatic=True)
        self.assertEqual((result["read"], result["liked"], result["sent"]), (3, 3, 0))
        self.assertEqual(self.client.like.await_count, 3)
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])

    async def test_like_gate_rechecked_after_model_revokes_permission(self):
        permission = {"enabled": True}
        self.browser.can_like = lambda automatic: permission["enabled"]
        async def revoke(*_):
            permission["enabled"] = False
            return decision(share=False)
        self.llm.side_effect = revoke
        result = await self.run_browser(automatic=True)
        self.assertEqual(result["liked"], 0)
        self.client.like.assert_not_awaited()
        self.assertEqual(self.rows("likes"), [])

    async def test_reserved_and_uncertain_likes_each_consume_daily_quota(self):
        for i, status in enumerate(("reserved", "uncertain", "already_liked")):
            with self.subTest(status=status):
                self.clock.return_value = MOMENT + timedelta(days=i)
                self.assertTrue(self.store.reserve_like(BVS[i], "audit reservation", 1))
                self.store.like_result(BVS[i], status)
                self.client.recommendations.return_value = [video(i + 5)]
                self.llm.return_value = decision(share=False)
                self.browser.cfg["daily_likes"] = 1
                self.assertEqual((await self.run_browser())["liked"], 0)
                self.assertEqual(self.store.daily_likes(), 1)
        self.client.like.assert_not_awaited()

    async def test_pending_queue_is_bounded_and_busy_flush_does_no_work(self):
        for i in range(7):
            self.clock.return_value = MOMENT + timedelta(seconds=i)
            self.store.offer(BVS[i], TARGET, "queued")
        self.assertEqual(len(self.store.pending()), 5)
        self.assertEqual(len([r for r in self.rows("suggestions") if r["status"] == "superseded"]), 2)
        async with self.browser.lock:
            self.assertEqual(await self.browser.flush_pending(TARGET), 0)
        self.sender.assert_not_awaited()
        self.assertEqual(self.store.daily_count(), 0)

    async def test_like_timeout_is_bounded_at_thirty_seconds(self):
        original = asyncio.wait_for
        timeouts = []
        async def bounded(awaitable, timeout):
            timeouts.append(timeout)
            if timeout == 30:
                # Await a real fake client once, then simulate the timeout boundary.
                await awaitable
                raise asyncio.TimeoutError
            return await original(awaitable, timeout)
        self.llm.return_value = decision(share=False)
        with patch.object(core.asyncio, "wait_for", side_effect=bounded):
            result = await self.run_browser()
        self.assertEqual(timeouts, [240, 30])
        self.assertEqual(result["like_uncertain"], 1)
        self.client.like.assert_awaited_once()
        self.assertEqual(self.rows("likes")[0]["status"], "uncertain")

    async def test_cancellation_marks_inflight_like_uncertain_and_releases_lock(self):
        entered = asyncio.Event()
        async def hold_like(_):
            entered.set()
            await asyncio.Event().wait()
        self.client.like.side_effect = hold_like
        task = asyncio.create_task(self.run_browser(automatic=True))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.rows("likes")[0]["status"], "uncertain")
        self.assertEqual(self.rows("rounds")[0]["status"], "error")
        self.assertFalse(self.browser.lock.locked())
        self.sender.assert_not_awaited()



class SharePacingTests(unittest.IsolatedAsyncioTestCase):
    """Five shares a day, unlocked gradually instead of burst at breakfast."""

    async def asyncSetUp(self):
        self.root = Path(__file__).resolve().parent / ".runs"
        self.path = self.root / uuid4().hex
        self.path.mkdir(parents=True)
        self.store = core.Store(self.path / "offline.db")
        self.client = Mock(spec_set=["recommendations", "related", "search", "subtitle", "like"])
        self.sender = AsyncMock(return_value=True)
        self.browser = core.Browser(self.store, self.client, AsyncMock(), self.sender,
                                    {"daily_shares": 5, "daily_rounds": 10,
                                     "share_cooldown_minutes": 120,
                                     "quiet_start": 23, "quiet_end": 9})
        self.browser.can_send = lambda automatic: True
        self._sent = 0

    async def asyncTearDown(self):
        self.store.close()
        shutil.rmtree(self.path, ignore_errors=True)

    def at(self, hour, minute=0):
        return datetime(2026, 10, 25, hour, minute, tzinfo=core.TZ)

    def deliver(self, hour, count=1, minute=0):
        """Record `count` deliveries at that hour (as if they had really gone out)."""
        for _ in range(count):
            with patch.object(core, "now", return_value=self.at(hour, minute)):
                did = self.store.reserve(video(self._sent), TARGET, "x", 5)
                self.assertIsNotNone(did)
                self.store.delivery_result(did, "sent")
                self._sent += 1

    def test_morning_allows_only_the_earned_slot(self):
        with patch.object(core, "now", return_value=self.at(10)):
            self.assertTrue(self.browser._send_window(True))
        self.deliver(10)
        with patch.object(core, "now", return_value=self.at(10, 30)):
            self.assertFalse(self.browser._send_window(True))   # cooldown not the reason

    def test_later_in_the_day_the_next_slot_unlocks(self):
        self.deliver(10)
        with patch.object(core, "now", return_value=self.at(12, 30)):
            self.assertTrue(self.browser._send_window(True))     # 2nd slot earned, cooldown passed
        self.deliver(12, minute=30)
        with patch.object(core, "now", return_value=self.at(13)):
            self.assertFalse(self.browser._send_window(True))    # 3rd slot not earned yet
        with patch.object(core, "now", return_value=self.at(15, 30)):
            self.assertTrue(self.browser._send_window(True))

    def test_evening_all_five_can_go_out_but_not_six(self):
        self.deliver(10); self.deliver(12, minute=30); self.deliver(15, minute=30); self.deliver(18, minute=30)
        with patch.object(core, "now", return_value=self.at(21)):
            self.assertTrue(self.browser._send_window(True))
        self.deliver(21)
        with patch.object(core, "now", return_value=self.at(22, 50)):
            self.assertFalse(self.browser._send_window(True))

    def test_quiet_hours_and_manual_paths(self):
        with patch.object(core, "now", return_value=self.at(23, 30)):
            self.assertFalse(self.browser._send_window(True))
        with patch.object(core, "now", return_value=self.at(3)):
            self.assertFalse(self.browser._send_window(True))
        with patch.object(core, "now", return_value=self.at(23, 30)):
            self.assertTrue(self.browser._send_window(False))     # 主人要求时不受静默限制

    async def test_delivery_reservation_caps_at_five(self):
        for index, hour in enumerate((10, 12, 15, 18, 21)):
            with patch.object(core, "now", return_value=self.at(hour)):
                status = await self.browser._deliver({"bvid": video(index)["bvid"]}, TARGET, "这个有意思。")
                self.assertEqual(status, "sent")
        with patch.object(core, "now", return_value=self.at(22)):
            status = await self.browser._deliver({"bvid": video(6)["bvid"]}, TARGET, "这个有意思。")
            self.assertEqual(status, "quota_or_duplicate")
            # 计数必须也在同一时刻读：`daily_count()` 默认按真实"今天"取日，
            # 不 patch 就会随运行日期漂移（这个测试曾在写它的当天通过、三天后失败）。
            self.assertEqual(self.store.daily_count(), 5)


if __name__ == "__main__":
    unittest.main()
