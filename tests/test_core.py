"""Offline core contract tests; run with python -m unittest discover -s tests -v.

Only the standard library is used. Load core.py by filename so the plugin's
AstrBot-dependent package initializer is never imported. No network, AstrBot,
credentials, running client, or deployment is needed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import unittest
from uuid import uuid4
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


CORE_PATH = Path(__file__).resolve().parents[1] / "plugin" / "astrbot_plugin_feiyu_bili" / "core.py"
SPEC = importlib.util.spec_from_file_location("feiyu_bili_core_offline_test", CORE_PATH)
assert SPEC is not None and SPEC.loader is not None
core = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(core)

MOMENT = datetime(2026, 10, 24, 12, 0, tzinfo=core.TZ)
BVS = ["BV" + str(index).zfill(10) for index in range(1, 11)]


def video(index=0, **overrides):
    item = {"bvid": BVS[index], "title": "离线测试视频", "author": "测试作者",
            "description": "离线简介", "url": "https://untrusted.invalid/not-canonical"}
    item.update(overrides)
    return item


def decision(**overrides):
    value = {"interested": True, "share": True, "note": "字幕笔记", "message": "这个内容值得聊聊。"}
    value.update(overrides)
    return json.dumps(value, ensure_ascii=False)


class DecisionTests(unittest.TestCase):
    def test_valid_strict_fields(self):
        parsed = core.parse_decision(decision(interested=False, share=False))
        self.assertIs(parsed["interested"], False)
        self.assertIs(parsed["share"], False)

    def test_boolean_fields_reject_truthy_values_and_missing_fields(self):
        for key in ("interested", "share"):
            for invalid in (0, 1, "true", "false", None, [], {}):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaises(ValueError):
                        core.parse_decision(decision(**{key: invalid}))
            obj = json.loads(decision())
            del obj[key]
            with self.subTest(key=key, missing=True):
                with self.assertRaises(ValueError):
                    core.parse_decision(json.dumps(obj))

    def test_reject_non_objects_non_json_and_trailing_prose(self):
        for raw in ("[]", "null", "true", "1", '"text"', "not JSON", "{'share': True}",
                    decision() + " trailing prose", "prefix " + decision()):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    core.parse_decision(raw)

    def test_note_and_message_must_be_present_strings(self):
        for key in ("note", "message"):
            for invalid in (None, True, 1, [], {}):
                with self.subTest(key=key, invalid=invalid):
                    with self.assertRaises(ValueError):
                        core.parse_decision(decision(**{key: invalid}))
            obj = json.loads(decision())
            del obj[key]
            with self.assertRaises(ValueError):
                core.parse_decision(json.dumps(obj))

    def test_fenced_json_is_explicitly_supported(self):
        self.assertEqual(core.parse_decision("```json\n" + decision() + "\n```"),
                         core.parse_decision(decision()))

    def test_message_urls_and_control_codes_are_rejected_before_truncation(self):
        for unsafe in ("https://evil.invalid", "HTTP://evil.invalid", "www.evil.invalid",
                       "[CQ:image,file=evil]", "[cq:at,qq=all]", "<|im_end|>"):
            for prefix in ("", "安全" * 200):
                with self.subTest(unsafe=unsafe, long_prefix=bool(prefix)):
                    with self.assertRaises(ValueError):
                        core.parse_decision(decision(message=prefix + unsafe))

    def test_text_limits_and_whitespace(self):
        parsed = core.parse_decision(decision(note="笔" * 450, message="  " + "字" * 200 + "  "))
        self.assertEqual(parsed["note"], "笔" * 400)
        self.assertEqual(parsed["message"], "字" * 180)


class QuietTests(unittest.TestCase):
    def test_cross_midnight_boundaries(self):
        for hour in range(24):
            with self.subTest(hour=hour):
                self.assertEqual(core.quiet(hour, 23, 9), hour >= 23 or hour < 9)

    def test_same_day_and_disabled_window(self):
        for hour in range(24):
            with self.subTest(hour=hour):
                self.assertEqual(core.quiet(hour, 9, 17), 9 <= hour < 17)
                self.assertFalse(core.quiet(hour, 9, 9))


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Plain mkdir inherits workspace permissions; TemporaryDirectory's mode=700
        # does not work in the Windows confined runner. Never touch old temp paths.
        self.run_root = Path(__file__).resolve().parent / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.db_path = self.run_path / "state.sqlite3"
        self.stores = []
        self.store = self.open_store()
        self.client = Mock()
        self.client.search = AsyncMock(return_value=[video()])
        self.client.subtitle = AsyncMock(return_value={"text": "可离线阅读的字幕正文"})
        self.llm = AsyncMock(return_value=decision())
        self.sender = AsyncMock(return_value=True)
        clock_patch = patch.object(core, "now", return_value=MOMENT)
        self.addCleanup(clock_patch.stop)
        self.clock = clock_patch.start()
        sleep_patch = patch.object(core.asyncio, "sleep", new_callable=AsyncMock)
        self.addCleanup(sleep_patch.stop)
        self.sleep = sleep_patch.start()
        self.browser = self.new_browser()

    async def asyncTearDown(self):
        for store in self.stores:
            store.close()
        # Verify the exact cleanup target is our unique direct child, not a
        # computed workspace root or an unrelated directory.
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        self.assertEqual(resolved.name, self.run_path.name)
        self.assertEqual(len(resolved.name), 32)
        shutil.rmtree(resolved)

    def open_store(self):
        store = core.Store(self.db_path)
        self.stores.append(store)
        return store

    def new_browser(self, store=None, **cfg):
        return core.Browser(store or self.store, self.client, self.llm, self.sender,
                            {"daily_rounds": 6, **cfg})

    async def run_browser(self, browser=None, **kwargs):
        return await (browser or self.browser).run(target="offline-target", system="离线人格", **kwargs)

    def rows(self, table):
        if table not in ("settings", "videos", "deliveries", "rounds"):
            raise ValueError("invalid test table")
        return [dict(row) for row in self.store.db.execute("SELECT * FROM " + table)]

    async def test_success_sends_one_canonical_bv_url_not_api_url(self):
        result = await self.run_browser()
        self.assertEqual((result["read"], result["sent"]), (1, 1))
        self.sender.assert_awaited_once_with(
            "offline-target", "这个内容值得聊聊。\n离线测试视频\nhttps://www.bilibili.com/video/" + BVS[0])
        self.assertEqual(self.rows("deliveries")[0]["status"], "sent")
        self.assertEqual(self.rows("rounds")[0]["status"], "ok")

    async def test_invalid_bvid_candidates_never_reach_subtitle_or_sender(self):
        invalid = ("av123", "bv0000000001", BVS[0] + "?p=1", BVS[0] + "/", BVS[0] + "\n",
                   "https://www.bilibili.com/video/" + BVS[0], "BV123", "BV00000000_1")
        self.client.search.return_value = [video(bvid=value) for value in invalid]
        result = await self.run_browser()
        self.assertEqual(result["status"], "all_seen")
        self.client.subtitle.assert_not_awaited()
        self.llm.assert_not_awaited()
        self.sender.assert_not_awaited()

    async def test_seen_persists_across_actual_close_and_reopen(self):
        await self.run_browser()
        self.store.close()
        self.stores.remove(self.store)
        self.store = self.open_store()
        self.assertTrue(self.store.seen(BVS[0]))
        result = await self.run_browser(self.new_browser())
        self.assertEqual(result["status"], "all_seen")
        self.assertEqual(result["read"], 0)
        self.sender.assert_awaited_once()
        self.llm.assert_awaited_once()
        self.assertEqual(len(self.store.history()), 1)

    async def test_not_shared_video_is_also_persistently_seen(self):
        self.llm.return_value = decision(share=False)
        await self.run_browser()
        reopened = self.open_store()
        self.assertTrue(reopened.seen(BVS[0]))
        await self.run_browser(self.new_browser(reopened))
        self.llm.assert_awaited_once()
        self.sender.assert_not_awaited()

    async def test_duplicate_search_entries_are_only_read_once(self):
        self.client.search.return_value = [video(), video(), video()]
        self.llm.return_value = decision(share=False)
        result = await self.run_browser()
        self.assertEqual(result["read"], 1)
        self.client.subtitle.assert_awaited_once()

    async def test_no_subtitle_overrides_model_share(self):
        for subtitle in ({"text": "", "reason": "not available"}, {}):
            with self.subTest(subtitle=subtitle):
                self.client.search.return_value = [video(index=len(self.rows("videos")))]
                self.client.subtitle.return_value = subtitle
                result = await self.run_browser()
                self.assertEqual(result["no_subtitle"], 1)
                self.assertEqual(result["sent"], 0)
                self.assertEqual(result["preview"], [])
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])
        self.assertTrue(all(row["source"] == "metadata_only" for row in self.rows("videos")))
        prompt_data = json.loads(self.llm.await_args.args[1].split("\n", 1)[1])
        self.assertEqual(prompt_data["source"], "metadata_only")

    async def test_dry_run_does_not_write_any_table_or_send(self):
        self.store.set("topic_index", 2)
        before = {table: self.rows(table) for table in ("settings", "videos", "deliveries", "rounds")}
        result = await self.run_browser(dry_run=True)
        self.assertEqual((result["read"], result["sent"]), (1, 0))
        self.assertEqual(len(result["preview"]), 1)
        self.sender.assert_not_awaited()
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows, table)
        self.assertFalse(self.store.seen(BVS[0]))
        # A real run after preview still processes and delivers the video.
        self.assertEqual((await self.run_browser())["sent"], 1)

    async def test_daily_quota_exhausted_does_not_send_or_reserve(self):
        browser = self.new_browser(daily_shares=1)
        await self.run_browser(browser)
        self.client.search.return_value = [video(1)]
        result = await self.run_browser(browser)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.store.daily_count(), 1)
        self.assertTrue(self.store.seen(BVS[1]))
        self.sender.assert_awaited_once()

    async def test_zero_daily_quota_disables_sends(self):
        result = await self.run_browser(self.new_browser(daily_shares=0))
        self.assertEqual(result["sent"], 0)
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])

    async def test_reserved_failed_uncertain_all_consume_quota_and_never_rereserve(self):
        for index, status in enumerate(("reserved", "failed", "uncertain")):
            with self.subTest(status=status):
                day = MOMENT + timedelta(days=index)
                self.clock.return_value = day
                item = video(index)
                did = self.store.reserve(item, "offline-target", "reserved message", daily_limit=1)
                self.assertIsNotNone(did)
                self.store.delivery_result(did, status)
                reopened = self.open_store()
                self.assertEqual(reopened.daily_count(), 1)
                self.assertIsNone(reopened.reserve(item, "offline-target", "retry", daily_limit=1))
                self.assertIsNone(reopened.reserve(video(index + 1), "offline-target", "new", daily_limit=1))
                # A reservation is made before the network call, even if no seen row exists.
                self.client.search.return_value = [item]
                result = await self.run_browser(self.new_browser(reopened, daily_shares=1))
                self.assertEqual(result["sent"], 0)
        self.sender.assert_not_awaited()

    async def test_bvid_reservation_is_unique_even_on_another_day(self):
        self.assertIsNotNone(self.store.reserve(video(), "target", "message"))
        self.clock.return_value = MOMENT + timedelta(days=1)
        self.assertEqual(self.store.daily_count(), 0)
        self.assertIsNone(self.store.reserve(video(), "different-target", "retry"))

    async def test_sender_exception_marks_uncertain_consumes_quota_no_restart_resend(self):
        self.sender.side_effect = RuntimeError("offline send failed")
        with self.assertRaisesRegex(RuntimeError, "offline send failed"):
            await self.run_browser(self.new_browser(daily_shares=1))
        self.assertEqual(self.rows("deliveries")[0]["status"], "uncertain")
        self.assertEqual(self.rows("rounds")[0]["status"], "error")
        reopened = self.open_store()
        self.assertEqual(reopened.daily_count(), 1)
        self.sender.side_effect = None
        await self.run_browser(self.new_browser(reopened, daily_shares=1))
        self.client.search.return_value = [video(1)]
        await self.run_browser(self.new_browser(reopened, daily_shares=1))
        self.sender.assert_awaited_once()

    async def test_false_send_is_uncertain_not_success(self):
        self.sender.return_value = False
        result = await self.run_browser()
        self.assertEqual(result["status"], "send_uncertain")
        self.assertEqual(result["sent"], 0)
        self.assertEqual(self.rows("deliveries")[0]["status"], "uncertain")
        self.assertEqual(self.store.daily_count(), 1)
        await self.run_browser()
        self.sender.assert_awaited_once()

    async def test_only_literal_true_is_send_success(self):
        for index, value in enumerate((None, 1, "true")):
            with self.subTest(value=value):
                self.clock.return_value = MOMENT + timedelta(days=index)
                self.client.search.return_value = [video(index)]
                self.sender.return_value = value
                result = await self.run_browser()
                self.assertEqual(result["status"], "send_uncertain")
                self.assertEqual(result["sent"], 0)
        self.assertTrue(all(row["status"] == "uncertain" for row in self.rows("deliveries")))

    async def test_failed_rounds_count_towards_daily_limit(self):
        browser = self.new_browser(daily_rounds=2)
        self.client.search.side_effect = RuntimeError("search offline error")
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "search offline error"):
                await self.run_browser(browser)
        result = await self.run_browser(browser)
        self.assertEqual(result["status"], "round_limit")
        self.assertEqual(self.client.search.await_count, 2)
        self.assertEqual([row["status"] for row in self.rows("rounds")], ["error", "error"])
        self.assertEqual(self.store.last_round(), MOMENT)
        self.sender.assert_not_awaited()

    async def test_round_quota_resets_on_next_day(self):
        browser = self.new_browser(daily_rounds=1)
        self.client.search.return_value = []
        self.assertEqual((await self.run_browser(browser))["status"], "empty")
        self.assertEqual((await self.run_browser(browser))["status"], "round_limit")
        self.clock.return_value = MOMENT + timedelta(days=1)
        self.assertEqual((await self.run_browser(browser))["status"], "empty")
        self.assertEqual(len(self.rows("rounds")), 2)

    async def test_automatic_quiet_window_across_calendar_days(self):
        moments = (MOMENT.replace(hour=23), (MOMENT + timedelta(days=1)).replace(hour=0),
                   (MOMENT + timedelta(days=1)).replace(hour=8),
                   (MOMENT + timedelta(days=1)).replace(hour=9))
        for index, moment in enumerate(moments):
            with self.subTest(moment=moment):
                self.clock.return_value = moment
                self.client.search.return_value = [video(index)]
                result = await self.run_browser(automatic=True)
                self.assertEqual(result["sent"], 1 if index == 3 else 0)
        self.sender.assert_awaited_once()
        self.assertEqual(len(self.rows("deliveries")), 1)
        self.assertEqual(len(self.rows("videos")), 4)

    async def test_manual_round_can_send_in_quiet_window(self):
        self.clock.return_value = MOMENT.replace(hour=23)
        self.assertEqual((await self.run_browser(automatic=False))["sent"], 1)

    async def test_concurrent_run_returns_busy_without_extra_work(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked_search(*args, **kwargs):
            entered.set()
            await release.wait()
            return [video()]

        self.client.search.side_effect = blocked_search
        first = asyncio.create_task(self.run_browser())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = await self.run_browser()
            self.assertEqual(second, {"status": "busy", "read": 0, "sent": 0, "liked": 0})
            self.assertEqual(len(self.rows("rounds")), 1)
            self.client.search.assert_awaited_once()
        finally:
            release.set()
            first_result = await asyncio.wait_for(first, timeout=2)
        self.assertEqual(first_result["sent"], 1)
        self.sender.assert_awaited_once()
        self.assertFalse(self.browser.lock.locked())

    async def test_at_most_one_recommendation_per_round(self):
        self.client.search.return_value = [video(index) for index in range(5)]
        result = await self.run_browser()
        self.assertEqual((result["read"], result["sent"], len(result["preview"])), (1, 1, 1))
        self.client.subtitle.assert_awaited_once()
        self.llm.assert_awaited_once()
        self.sender.assert_awaited_once()

    async def test_failed_send_does_not_try_a_second_candidate(self):
        self.client.search.return_value = [video(index) for index in range(5)]
        self.sender.return_value = False
        result = await self.run_browser()
        self.assertEqual((result["read"], result["sent"], len(result["preview"])), (1, 0, 1))
        self.sender.assert_awaited_once()
        self.llm.assert_awaited_once()

    async def test_reservation_denied_does_not_try_a_second_candidate(self):
        self.client.search.return_value = [video(index) for index in range(5)]
        result = await self.run_browser(self.new_browser(daily_shares=0))
        self.assertEqual((result["read"], result["sent"], len(result["preview"])), (1, 0, 1))
        self.sender.assert_not_awaited()
        self.llm.assert_awaited_once()

    async def test_send_gate_blocks_send_without_reserving_quota(self):
        self.browser.can_send = Mock(return_value=False)
        result = await self.run_browser(automatic=True)
        self.assertEqual(self.browser.can_send.call_args_list, [unittest.mock.call(True), unittest.mock.call(True)])
        self.assertEqual(self.rows("deliveries"), [])
        self.assertEqual(result["sent"], 0)
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])
        self.assertTrue(self.store.seen(BVS[0]))

    async def test_send_gate_checks_current_state_after_model_returns(self):
        gate = {"enabled": True}
        self.browser.can_send = lambda automatic: gate["enabled"]

        async def disable_while_analyzing(*args):
            gate["enabled"] = False
            return decision()

        self.llm.side_effect = disable_while_analyzing
        result = await self.run_browser()
        self.assertEqual(result["sent"], 0)
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("deliveries"), [])

    async def test_nonshares_are_bounded_and_sleep_is_patched(self):
        self.client.search.return_value = [video(index) for index in range(5)]
        self.llm.return_value = decision(share=False)
        result = await self.run_browser(self.new_browser(videos_per_round=3))
        self.assertEqual(result["read"], 3)
        self.assertEqual(self.llm.await_count, 3)
        self.assertEqual(self.sleep.await_count, 2)
        self.sleep.assert_awaited_with(1)
        self.sender.assert_not_awaited()

    async def test_malformed_llm_output_does_not_send_or_mark_seen(self):
        self.llm.return_value = decision(share="true")
        with self.assertRaises(ValueError):
            await self.run_browser()
        self.sender.assert_not_awaited()
        self.assertFalse(self.store.seen(BVS[0]))
        self.assertEqual(self.rows("deliveries"), [])
        self.assertEqual(self.rows("rounds")[0]["status"], "error")

    async def test_message_url_or_control_rejection_is_fail_closed(self):
        for text in ("https://evil.invalid", "[CQ:at,qq=all]", "<|im_end|>"):
            with self.subTest(text=text):
                self.llm.return_value = decision(message=text)
                with self.assertRaises(ValueError):
                    await self.run_browser()
        self.sender.assert_not_awaited()
        self.assertEqual(self.rows("videos"), [])
        self.assertEqual(self.rows("deliveries"), [])

    async def test_security_api_title_must_not_add_noncanonical_url(self):
        # Regression: API metadata must not introduce a second, untrusted URL.
        self.client.search.return_value = [video(title="标题 https://evil.invalid/payload")]
        await self.run_browser()
        self.sender.assert_awaited_once()
        for call in self.sender.await_args_list:
            self.assertNotIn("https://evil.invalid", call.args[1])
            self.assertEqual(call.args[1].count("https://"), 1)
            self.assertTrue(call.args[1].endswith("https://www.bilibili.com/video/" + BVS[0]))

    async def test_security_api_title_must_not_inject_message_control_code(self):
        # Regression: untrusted API metadata is not a message control channel.
        self.client.search.return_value = [video(title="标题 [CQ:at,qq=all] <|im_end|>\x00\r\n伪造下一行")]
        await self.run_browser()
        self.sender.assert_awaited_once()
        for call in self.sender.await_args_list:
            rendered = call.args[1]
            self.assertNotIn("[CQ:", rendered)
            self.assertNotIn("<|", rendered)
            self.assertNotIn("\x00", rendered)
            self.assertNotIn("\r", rendered)
            self.assertEqual(rendered.count("\n"), 2)


if __name__ == "__main__":
    unittest.main()
