"""M5: background vision is screened first, recorded as a shadow decision, and never
buys a like or a share. Offline only — no network, no ffmpeg, no AstrBot runtime."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import unittest
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent / "plugin" / "astrbot_plugin_feiyu_bili"

sys.path.insert(0, str(TESTS))
import test_plugin as support  # noqa: E402

core = support.core
Event = support.Event
BINDING = support.BINDING

MOMENT = datetime(2026, 10, 25, 12, 0, tzinfo=core.TZ)


def load_reading_store():
    spec = importlib.util.spec_from_file_location("feiyu_m5_store", PLUGIN / "reading_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = load_reading_store()


def segments(count=30):
    return [{"from_ms": i * 1000, "to_ms": i * 1000 + 900, "text": f"第{i + 1}句：管网按标高排"} for i in range(count)]


def video(index=0, **overrides):
    item = {"bvid": "BV" + str(index).zfill(10), "title": "离线测试视频", "author": "测试作者",
            "description": "离线简介", "url": "https://untrusted.invalid/not-canonical"}
    item.update(overrides)
    return item


def decision(**overrides):
    value = {"interested": True, "share": False, "note": "字幕笔记", "message": ""}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class ShadowStoreTests(unittest.TestCase):
    def setUp(self):
        self.run_path = TESTS / f"_tmp_m5_{uuid4().hex}"
        self.run_path.mkdir(parents=True, exist_ok=True)
        self.store = rs.ReadingStore(self.run_path / "browse.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.run_path, ignore_errors=True)

    def test_schema_is_three_and_shadow_roundtrips(self):
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
        row = self.store.add_shadow("vd1", "qq:private:1", "BV0000000001", title="管网",
                                    author="某UP", topic="排水", subtitle_state="timed",
                                    interested=True, verdict=rs.SHADOW_WOULD_LOOK)
        self.assertEqual(row["verdict"], rs.SHADOW_WOULD_LOOK)
        self.assertEqual(row["interested"], "1")
        self.assertEqual(self.store.shadow("vd1", "qq:private:1")["title"], "管网")

    def test_scope_isolation_and_verdict_filters(self):
        self.store.add_shadow("a", "owner-a", "BV0000000001", verdict=rs.SHADOW_WOULD_LOOK)
        self.store.add_shadow("b", "owner-a", "BV0000000002", verdict=rs.SHADOW_SKIP, reason="not_interested")
        self.store.add_shadow("c", "owner-b", "BV0000000003", verdict=rs.SHADOW_WOULD_LOOK)
        self.assertEqual(len(self.store.shadows("owner-a")), 2)
        self.assertEqual([r["decision_id"] for r in self.store.shadows("owner-a", verdict=rs.SHADOW_SKIP)], ["b"])
        self.assertEqual(self.store.shadow_count("owner-a"), 2)
        self.assertEqual(self.store.shadow_count("owner-a", rs.SHADOW_WOULD_LOOK), 1)
        self.assertIsNone(self.store.shadow("a", "owner-b"))

    def test_first_candidate_is_oldest_would_look_only(self):
        self.store.add_shadow("skip1", "s", "BV0000000009", verdict=rs.SHADOW_SKIP)
        self.store.add_shadow("first", "s", "BV0000000001", verdict=rs.SHADOW_WOULD_LOOK)
        self.store.add_shadow("second", "s", "BV0000000002", verdict=rs.SHADOW_WOULD_LOOK)
        self.assertEqual(self.store.first_candidate("s")["decision_id"], "first")
        self.store.update_shadow("first", verdict=rs.SHADOW_LOOKED, frames=4, note="r1 rev1")
        self.assertEqual(self.store.first_candidate("s")["decision_id"], "second")
        self.assertEqual(self.store.shadow("first", "s")["frames"], 4)

    def test_update_shadow_ignores_none_and_reports_reason(self):
        self.store.add_shadow("x", "s", "BV0000000001", verdict=rs.SHADOW_WOULD_LOOK)
        self.store.update_shadow("x", verdict=rs.SHADOW_LOOK_FAILED, reason="decoder_missing")
        row = self.store.shadow("x", "s")
        self.assertEqual((row["verdict"], row["reason"], row["frames"]), (rs.SHADOW_LOOK_FAILED, "decoder_missing", 0))

    def test_recent_jobs_joins_readings_and_scope(self):
        reading = self.store.begin_reading(reading_id="r1", bvid="BV0000000001", page=1, cid=101,
                                           revision=0, scope="s", title="管网", author="某UP",
                                           part_title="P1", duration=600, fetched_at=MOMENT.isoformat(),
                                           source_type="subtitle", subtitle_state="timed",
                                           subtitle_reason="", evidence="text_read", updated_at=MOMENT.isoformat())
        self.store.begin_job("job1", reading["reading_id"], 0, 1, "playurl", {"frames": 4})
        self.store.finish_job("job1", "completed", "", {"frames": 3})
        rows = self.store.recent_jobs("s", 5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "管网")
        self.assertEqual(rows[0]["state"], "completed")
        self.assertEqual(self.store.recent_jobs("other", 5), [])


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    """core.Browser reports screening facts; the recorder cannot change like/share."""

    async def asyncSetUp(self):
        self.run_root = TESTS / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "state.sqlite3")
        self.client = Mock()
        self.client.search = AsyncMock(return_value=[video(1)])
        self.client.subtitle = AsyncMock(return_value={"text": "可离线阅读的字幕正文", "segments": segments()})
        self.llm = AsyncMock(return_value=decision())
        self.sender = AsyncMock(return_value=True)
        clock = patch.object(core, "now", return_value=MOMENT)
        self.addCleanup(clock.stop)
        clock.start()
        sleep = patch.object(core.asyncio, "sleep", new_callable=AsyncMock)
        self.addCleanup(sleep.stop)
        sleep.start()
        self.browser = core.Browser(self.store, self.client, self.llm, self.sender, {"daily_rounds": 6})

    async def asyncTearDown(self):
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        self.assertEqual(len(resolved.name), 32)
        shutil.rmtree(resolved)

    async def run_browser(self, **kwargs):
        return await self.browser.run(target="offline-target", system="离线人格", **kwargs)

    async def test_observer_sees_screening_state_and_interest(self):
        seen = []
        await self.run_browser(topic="管网", observer=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["bvid"], "BV0000000001")
        self.assertEqual(seen[0]["subtitle_state"], "timed")
        self.assertTrue(seen[0]["interested"])
        self.assertTrue(seen[0]["read"])

    async def test_missing_subtitle_is_reported_as_no_text(self):
        self.client.subtitle = AsyncMock(return_value={"text": "", "reason": "no_subtitle_or_login_required"})
        seen = []
        result = await self.run_browser(topic="管网", observer=seen.append)
        self.assertEqual(seen[0]["subtitle_state"], "none")
        self.assertFalse(seen[0]["read"])
        self.assertEqual(result["no_subtitle"], 1)

    async def test_broken_recorder_cannot_block_or_change_the_round(self):
        def boom(_item):
            raise RuntimeError("recorder exploded")
        result = await self.run_browser(topic="管网", observer=boom)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["read"], 1)
        # A share decision still has to pass the unchanged subtitle/send gates.
        self.assertEqual(result["sent"], 0)

    async def test_observer_never_upgrades_like_eligibility(self):
        self.client.subtitle = AsyncMock(return_value={"text": "", "reason": "no_subtitle_or_login_required"})
        self.llm = AsyncMock(return_value=decision(interested=True, like=True, share=False))
        self.browser.llm = self.llm
        self.browser.can_like = lambda automatic: True
        seen = []
        result = await self.run_browser(topic="管网", observer=seen.append)
        self.assertEqual(result["liked"], 0)
        self.client.like.assert_not_called()


class PluginShadowTests(unittest.IsolatedAsyncioTestCase):
    """The plugin records shadow decisions without downloading, and upgrades at most once."""

    async def asyncSetUp(self):
        self.support = support
        self.plugin = support.plugin
        self.run_root = TESTS / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "browse.db")
        self.db = Mock(spec_set=support.ReadOnlyMemoryDB)
        self.db.get_owner = Mock(return_value=support.OWNER)
        self.db.build_context = Mock(return_value={"state": "离线状态", "memory": "只读兴趣参考"})
        self.memory = Mock(spec_set=["_identity", "_safe_store", "_persona"])
        self.memory._identity.side_effect = lambda event: dict(event.identity)
        self.memory._safe_store.return_value = self.db
        self.memory._persona.return_value = "现有人格，不是通用助手"
        meta = SimpleNamespace(name="astrbot_plugin_feiyu_memory", activated=True, star_cls=self.memory)
        self.context = Mock()
        self.context.get_all_stars = Mock(return_value=[meta])
        self.context.persona_manager = None
        self.context.send_message = AsyncMock(True)
        self.context.get_current_chat_provider_id = AsyncMock(return_value="current-chat-model")
        self.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        self.context.provider_manager = None
        self.instance = self.plugin.FeiyuBiliPlugin(self.context, {"auto_like": True, "daily_likes": 3})
        self.instance.store = self.store
        self.instance.rstore = rs.ReadingStore(self.run_path / "browse.db")
        self.instance.browser = Mock()
        self.instance.browser.lock = asyncio.Lock()
        self.instance.browser.run = AsyncMock(return_value={"status": "ok", "read": 1, "sent": 0, "liked": 0})
        self.instance.client = Mock()
        self.store.set("binding", dict(BINDING))
        self.store.set("auto", True)

    async def asyncTearDown(self):
        self.instance.rstore.close()
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        self.assertEqual(len(resolved.name), 32)
        shutil.rmtree(resolved)

    def verdicts(self, scope=BINDING["umo"]):
        return [(r["bvid"], r["verdict"], r["reason"]) for r in self.instance.rstore.shadows(scope, limit=10)]

    def test_screening_gates_are_explicit(self):
        self.instance.media = None
        self.assertEqual(self.instance._vision_verdict({"read": True, "interested": True}),
                         (rs.SHADOW_SKIP, "decoder_missing"))
        self.instance.media = SimpleNamespace(available=True)
        self.assertEqual(self.instance._vision_verdict({"read": False, "interested": True}),
                         (rs.SHADOW_SKIP, "no_text"))
        self.assertEqual(self.instance._vision_verdict({"read": True, "interested": False}),
                         (rs.SHADOW_SKIP, "not_interested"))
        self.assertEqual(self.instance._vision_verdict({"read": True, "interested": True}),
                         (rs.SHADOW_WOULD_LOOK, ""))
        self.instance.config["vision_enabled"] = False
        self.assertEqual(self.instance._vision_verdict({"read": True, "interested": True}),
                         (rs.SHADOW_SKIP, "vision_off"))

    def test_shadow_is_recorded_without_any_network_or_model_call(self):
        self.instance.media = SimpleNamespace(available=True)
        self.instance._record_shadow(BINDING["umo"], {"bvid": "BV0000000001", "title": "管网", "author": "某UP",
                                                     "topic": "排水", "subtitle_state": "timed",
                                                     "interested": True, "read": True})
        self.assertEqual(self.verdicts(), [("BV0000000001", rs.SHADOW_WOULD_LOOK, "")])
        self.instance.client.playurl.assert_not_called()
        self.context.llm_generate.assert_not_awaited()

    def test_shadow_recording_can_be_switched_off(self):
        self.instance.media = SimpleNamespace(available=True)
        self.instance.config["auto_vision_shadow"] = False
        self.instance.config["auto_vision"] = False
        self.assertEqual(self.instance._record_shadow(BINDING["umo"], {"bvid": "BV0000000001", "read": True,
                                                                      "interested": True}), "")
        self.assertEqual(self.verdicts(), [])

    async def test_auto_look_can_still_be_switched_off(self):
        self.instance.media = SimpleNamespace(available=True)
        self.instance.config["auto_vision"] = False
        self.instance.rstore.add_shadow("vd1", BINDING["umo"], "BV0000000001", interested=True)
        self.assertIsNone(await self.instance._maybe_auto_look(BINDING["umo"]))
        self.assertEqual(self.instance.rstore.shadow("vd1", BINDING["umo"])["verdict"], rs.SHADOW_WOULD_LOOK)

    async def test_auto_look_is_on_by_default_and_takes_one_candidate(self):
        self.instance.media = SimpleNamespace(available=True)
        self.instance.config.pop("auto_vision", None)
        self.instance.rstore.add_shadow("vd1", BINDING["umo"], "BV0000000001", interested=True)
        self.instance.rstore.add_shadow("vd2", BINDING["umo"], "BV0000000002", interested=True)
        await self.instance._maybe_auto_look(BINDING["umo"])
        self.assertNotEqual(self.instance.rstore.shadow("vd1", BINDING["umo"])["verdict"],
                            rs.SHADOW_WOULD_LOOK)
        self.assertEqual(self.instance.rstore.shadow("vd2", BINDING["umo"])["verdict"],
                         rs.SHADOW_WOULD_LOOK)

    async def test_auto_look_without_decoder_does_not_consume_the_candidate(self):
        self.instance.config["auto_vision"] = True
        self.instance.media = SimpleNamespace(available=False)
        self.instance.rstore.add_shadow("vd1", BINDING["umo"], "BV0000000001", interested=True)
        self.assertIsNone(await self.instance._maybe_auto_look(BINDING["umo"]))
        self.assertEqual(self.instance.rstore.shadow("vd1", BINDING["umo"])["verdict"], rs.SHADOW_WOULD_LOOK)

    async def test_status_and_workbench_report_shadow_state(self):
        self.instance.media = SimpleNamespace(available=True)
        self.instance._record_shadow(BINDING["umo"], {"bvid": "BV0000000001", "title": "管网",
                                                     "subtitle_state": "timed", "read": True,
                                                     "interested": True})
        status = "\n".join([t async for t in self.instance.command(Event(), "状态")])
        self.assertIn("后台视觉：开（每轮最多升级1条）", status)
        self.assertIn("筛选记录1条", status)
        self.assertIn("评论：可读第一页8条", status)
        workbench = "\n".join([t async for t in self.instance.command(Event(), "任务")])
        self.assertIn("后台视觉：开（每轮最多升级1条，媒体并发1）", workbench)
        self.assertIn("本来会看", workbench)
        self.assertIn("还没有画面任务", workbench)

    async def test_workbench_shows_job_state_and_model(self):
        reading = self.instance.rstore.begin_reading(
            reading_id="r1", bvid="BV0000000001", page=1, cid=101, revision=0, scope=BINDING["umo"],
            title="管网", author="某UP", part_title="P1", duration=600,
            fetched_at=MOMENT.isoformat(), source_type="subtitle", subtitle_state="timed",
            subtitle_reason="", evidence="visual_observed", updated_at=MOMENT.isoformat())
        self.instance.rstore.begin_job("job1", reading["reading_id"], 0, 1, "playurl", {"frames": 4})
        self.instance.rstore.finish_job("job1", "completed", "", {"frames": 3})
        self.instance.rstore.add_batch("vb1", "job1", reading["reading_id"], 0, "xiaomi/mimo-v2.6-flash",
                                       "[]", "visual_observed", "", {"images": 3})
        text = "\n".join([t async for t in self.instance.command(Event(), "任务")])
        self.assertIn("管网", text)
        self.assertIn("completed", text)
        self.assertIn("得到3帧", text)
        self.assertIn("xiaomi/mimo-v2.6-flash", text)
        self.assertIn("visual_observed", text)


    async def test_run_records_screening_and_never_downloads_when_off(self):
        self.instance.media = SimpleNamespace(available=True)
        await self.instance._run(topic="管网")
        kwargs = self.instance.browser.run.await_args.kwargs
        self.assertIn("observer", kwargs)
        kwargs["observer"]({"bvid": "BV0000000001", "title": "管网", "read": True,
                            "interested": True, "subtitle_state": "timed"})
        self.assertEqual(self.verdicts(), [("BV0000000001", rs.SHADOW_WOULD_LOOK, "")])
        self.instance.client.playurl.assert_not_called()
        self.context.llm_generate.assert_not_awaited()

    async def test_run_wakes_auto_look_only_for_settled_automatic_rounds(self):
        self.instance.media = SimpleNamespace(available=True)
        self.instance._maybe_auto_look = AsyncMock(return_value=None)
        await self.instance._run(topic="管网", automatic=True)
        self.instance._maybe_auto_look.assert_awaited_once_with(BINDING["umo"])
        # 主人手动刷 / 试刷不是后台活动，不唤醒画面升级。
        self.instance._maybe_auto_look.reset_mock()
        await self.instance._run(topic="管网")
        self.instance._maybe_auto_look.assert_not_awaited()
        # 自动浏览已关闭时连唤醒都不做。
        self.store.set("auto", False)
        await self.instance._run(topic="管网", automatic=True)
        self.instance._maybe_auto_look.assert_not_awaited()


class AutonomyResilienceTests(unittest.IsolatedAsyncioTestCase):
    """A transient error must not end autonomy for the day, and the daily cap is ten."""

    async def asyncSetUp(self):
        self.plugin = support.plugin
        self.run_root = TESTS / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "browse.db")
        db = Mock(spec_set=support.ReadOnlyMemoryDB)
        db.get_owner = Mock(return_value=support.OWNER)
        db.build_context = Mock(return_value={"state": "离线状态", "memory": ""})
        memory = Mock(spec_set=["_identity", "_safe_store", "_persona"])
        memory._identity.side_effect = lambda event: dict(event.identity)
        memory._safe_store.return_value = db
        memory._persona.return_value = "现有人格，不是通用助手"
        meta = SimpleNamespace(name="astrbot_plugin_feiyu_memory", activated=True, star_cls=memory)
        self.context = Mock()
        self.context.get_all_stars = Mock(return_value=[meta])
        self.context.persona_manager = None
        self.context.send_message = AsyncMock(True)
        self.context.get_current_chat_provider_id = AsyncMock(return_value="current-chat-model")
        self.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        self.instance = self.plugin.FeiyuBiliPlugin(self.context, {})
        self.instance.store = self.store
        self.instance.rstore = rs.ReadingStore(self.run_path / "browse.db")
        self.instance.browser = Mock()
        self.instance.browser.lock = asyncio.Lock()
        self.instance.browser.flush_pending = AsyncMock(0)
        self.instance.browser.run = AsyncMock(return_value={"status": "ok", "read": 1, "sent": 0, "liked": 0})
        self.instance.client = Mock()
        self.store.set("binding", dict(BINDING))
        self.store.set("auto", True)

    async def asyncTearDown(self):
        self.instance.rstore.close()
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        shutil.rmtree(resolved, ignore_errors=True)

    async def test_first_failure_backs_off_instead_of_stopping(self):
        self.instance._after_failure(RuntimeError("人格未能读取，拒绝使用通用助手替代"), 1)
        self.assertTrue(self.store.get("auto"), "一次瞬时失败不该关掉自主活动")
        due = datetime.fromisoformat(self.store.get("next_wake"))
        minutes = (due - core.now()).total_seconds() / 60
        # 退避至少 25 分钟；上限不能写死 40——22:30 之后重试会落进 23–9 的免打扰，
        # 代码会把它顺延到次日 9:00，那是正确行为，不是失败。
        self.assertGreaterEqual(minutes, 25)
        self.assertLessEqual(minutes, 24 * 60)
        self.assertFalse(core.quiet(due.hour, 23, 9), "退避后的时间不该落在免打扰时段")
        error = json.loads(self.store.get("last_error"))
        self.assertEqual(error["type"], "RuntimeError")
        self.assertIn("人格未能读取", error["message"])

    async def test_third_consecutive_failure_pauses_and_keeps_reason(self):
        for attempt in (1, 2):
            self.instance._after_failure(RuntimeError("网络没连上B站"), attempt)
            self.assertTrue(self.store.get("auto"))
        self.instance._after_failure(RuntimeError("网络没连上B站"), 3)
        self.assertFalse(self.store.get("auto"), "连续 3 次失败才暂停")
        self.assertIn("网络没连上B站", json.loads(self.store.get("last_error"))["message"])

    async def test_daily_round_cap_is_ten_by_default(self):
        from datetime import datetime as dt, timedelta as td
        past = (dt.now(core.TZ) - td(minutes=5)).isoformat()
        for _ in range(9):
            self.store.begin_round(False, 16)
        self.store.set("next_wake", past)
        await self.instance._tick()
        self.assertEqual(self.instance.browser.run.await_count, 1, "第 9 轮仍该跑")

        self.instance.browser.run.reset_mock()
        self.store.begin_round(False, 16)  # 第 10 轮用掉，到了上限
        self.store.set("next_wake", past)
        await self.instance._tick()
        self.assertEqual(self.instance.browser.run.await_count, 0, "默认上限是 10 轮，第 10 轮后不再跑")


if __name__ == "__main__":
    unittest.main(verbosity=2)