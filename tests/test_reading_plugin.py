"""Plugin-level reading loop: paged subtitles, honest frame failures, journal → recognition.

Run: python -B test_reading_plugin.py   (from 05_B站浏览/tests)
Reuses the offline AstrBot stubs from test_plugin.py; the network client is a fake
that records calls, and the decoder is absent on purpose so the "no frames" path is
the one under test here.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import test_plugin as support

plugin = support.plugin
core = support.core
Event = support.Event
BVID = support.BVID
BINDING = support.BINDING
OWNER = support.OWNER

JOURNAL = {
    "encounter": "主人在私聊里让我读这个P。",
    "material_facts": "讲了老城区排水管网的改造顺序。",
    "author_view": "作者认为先做雨污分流再做路面。",
    "ai_inference": "我推断工期压力主要在雨季前。",
    "prior_judgment": "我以前以为只是挖开换管。",
    "revision": "现在知道要先算清上下游标高，不然改了也倒灌。",
    "omissions": "没讲造价，也没提居民安置。",
    "next_response": "下次可以问问主人所在城市有没有在改。",
}


class FakeClient:
    """Only the two calls a text read needs; media calls must never happen without a decoder."""

    def __init__(self, *, segments=None, subtitle=None):
        self.segments = segments if segments is not None else []
        self.fixed_subtitle = subtitle
        self.detail_calls = []
        self.subtitle_calls = []

    async def detail(self, bvid):
        self.detail_calls.append(bvid)
        return {"title": "老城区排水管网", "owner": "某UP主", "duration": 600, "cid": 101,
                "pages": 60, "parts": [{"page": 1, "cid": 101, "part_title": "P1 管网", "duration": 600}]}

    async def subtitle(self, bvid, cid=None):
        self.subtitle_calls.append((bvid, cid))
        if self.fixed_subtitle is not None:
            return dict(self.fixed_subtitle)
        return {"text": " ".join(s["text"] for s in self.segments),
                "source": "bilibili_subtitle:part", "reason": "ok", "lang": "ai-zh",
                "segments": [dict(s) for s in self.segments], "chars": 999}

    async def playurl(self, bvid, cid):
        raise AssertionError("playurl must not be called when no decoder is installed")

    async def download_media(self, url, dest, **kwargs):
        raise AssertionError("download_media must not be called when no decoder is installed")

    async def close(self):
        return None


def talking_segments(count=60):
    return [{"from_ms": i * 1000, "to_ms": i * 1000 + 900, "text": f"第{i + 1}句：管网要按标高排"} for i in range(count)]


class ReadingPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.run_root = Path(__file__).resolve().parent / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "browse.db")
        self.db = Mock(spec_set=support.ReadOnlyMemoryDB)
        self.db.get_owner.return_value = dict(OWNER)
        self.db.build_context.return_value = {"state": "s", "memory": "m"}
        self.memory = Mock(spec_set=["_identity", "_safe_store", "_persona"])
        self.memory._identity.side_effect = lambda event: dict(event.identity)
        self.memory._safe_store.return_value = self.db
        self.memory._persona.return_value = "现有人格，不是通用助手"
        meta = support.SimpleNamespace(name="astrbot_plugin_feiyu_memory", activated=True, star_cls=self.memory)
        self.context = Mock(spec_set=["get_all_stars", "persona_manager", "send_message",
                                      "get_current_chat_provider_id", "llm_generate"])
        self.context.get_all_stars.return_value = [meta]
        self.context.persona_manager = None
        self.context.send_message = AsyncMock(return_value=True)
        self.context.get_current_chat_provider_id = AsyncMock(return_value="current-chat-model")
        self.context.llm_generate = AsyncMock(return_value=support.SimpleNamespace(
            completion_text=json.dumps(JOURNAL, ensure_ascii=False)))
        self.instance = plugin.FeiyuBiliPlugin(self.context, {})
        self.instance.store = self.store
        self.instance.rstore = plugin.ReadingStore(self.run_path / "browse.db")
        self.instance.browser = Mock()
        self.instance.browser.lock = asyncio.Lock()
        self.instance.browser.run = AsyncMock(return_value={"status": "ok", "read": 0, "sent": 0})
        self.client = FakeClient(segments=talking_segments())
        self.instance.client = self.client
        self.instance.media = plugin.MediaWorker(self.run_path / "media")
        self.instance.media.ffmpeg = None  # deterministic: this test only covers "no decoder"
        self.instance.media.ffprobe = None
        self.store.set("binding", dict(BINDING))

    async def asyncTearDown(self):
        if self.instance.task is not None:
            self.instance.task.cancel()
            await asyncio.gather(self.instance.task, return_exceptions=True)
        self.assertFalse([c for c in self.db.mock_calls if c[0] not in {"get_owner", "build_context"}])
        if self.instance.rstore is not None:
            self.instance.rstore.close()
            self.instance.rstore = None
        self.store.close()
        shutil.rmtree(self.run_path.resolve())

    async def command(self, action, event=None):
        return [text async for text in self.instance.command(event or Event(), action)]

    def scope(self):
        return BINDING["umo"]

    async def read(self, event=None, deep=False):
        return await self.instance.read_tool(event or Event(), BVID, 1, deep)

    # ---- reading -------------------------------------------------------
    async def test_read_pages_real_timelines_and_hands_back_a_cursor(self):
        text = await self.read()
        self.assertIn("老城区排水管网", text)
        self.assertIn("[1] 00:00 第1句", text)
        self.assertIn("有真实时间轴：是", text)
        match = re.search(r'feiyu_read_more\(cursor="([^"]+)"\)', text)
        self.assertIsNotNone(match, text)
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        record = self.instance.rstore.reading(reading_id, self.scope())
        self.assertEqual(record["evidence"], "text_read")
        self.assertEqual(record["subtitle_state"], "timed")
        self.assertEqual(len(self.instance.rstore.segments(reading_id, 1)), 60)

        cursor, seen = match.group(1), []
        while cursor:
            page = await self.instance.read_more_tool(Event(), cursor)
            seen.extend(re.findall(r"第(\d+)句", page))
            nxt = re.search(r'feiyu_read_more\(cursor="([^"]+)"\)', page)
            cursor = nxt.group(1) if nxt else None
        self.assertEqual(seen, [str(i) for i in range(25, 61)])

    async def test_read_more_rejects_a_forged_or_foreign_cursor(self):
        text = await self.read()
        cursor = re.search(r'feiyu_read_more\(cursor="([^"]+)"\)', text).group(1)
        reading_id, revision, offset, signature = cursor.split(".")
        # Flip a signature character deterministically: `cursor[:-3] + "abc"` is flaky,
        # because a real signature that already ends in "abc" would stay valid.
        flip = "0" if signature[0] != "0" else "1"
        forged = ".".join([reading_id, revision, offset, flip + signature[1:]])
        self.assertIn("游标不可用", await self.instance.read_more_tool(Event(), forged))
        moved = ".".join([reading_id, revision, str(int(offset) + 24), signature])
        self.assertIn("游标不可用", await self.instance.read_more_tool(Event(), moved))
        foreign = await self.instance.read_more_tool(Event(umo="qq:private:99999"), cursor)
        self.assertIn("游标不可用", foreign)

    async def test_deep_read_without_decoder_admits_it_saw_nothing(self):
        text = await self.read(deep=True)
        self.assertIn("画面不可用：这台机器上没有 ffmpeg 解码器", text)
        self.assertIn("不得描述画面内容", text)
        self.assertNotIn("看到：", text)
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        record = self.instance.rstore.reading(reading_id, self.scope())
        self.assertEqual(record["evidence"], "visual_unavailable")
        self.assertEqual(self.instance.rstore.frames_for(reading_id, 1), [])
        self.assertEqual(self.client.detail_calls, [BVID])
        # Reading is owner-triggered: it never pushes to QQ and never spends like/quota budget.
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.store.daily_likes(), 0)
        self.assertEqual(self.store.round_count(), 0)

    async def test_part_without_subtitles_is_reported_not_invented(self):
        self.client.fixed_subtitle = {"text": "", "source": "none", "reason": "login_required",
                                      "lang": "", "segments": [], "chars": 0}
        text = await self.read()
        self.assertIn("这个P没有可用的官方字幕", text)
        self.assertIn("不要编造内容", text)
        self.assertIn("login_required", text)
        # No transcript was possible either, and the reply says so instead of pretending.
        self.assertIn("没有转写", text)
        reading_id = re.search(r"reading_id=(rd_[0-9a-f]+)", text).group(1)
        self.assertEqual(self.instance.rstore.reading(reading_id, self.scope())["evidence"], "metadata_only")

    async def test_reading_is_refused_for_non_owner_and_leaves_no_record(self):
        refusal = await self.read(Event(owner=False))
        self.assertEqual(refusal, "这个功能只在已绑定的主人私聊开放。")
        grouped = await self.read(Event(scene="group"))
        self.assertEqual(grouped, "这个功能只在已绑定的主人私聊开放。")
        self.assertEqual(self.client.detail_calls, [])

    # ---- journal and recognition --------------------------------------
    async def test_journal_keep_revise_and_forget(self):
        text = await self.read()
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        written = await self.instance.journal_tool(Event(), reading_id)
        self.assertIn("手账已写入待核对区：jnl_", written)
        for label in ("遇见与处境", "材料事实", "遗漏", "下次如何回应"):
            self.assertIn(label, written)
        journal_id = re.search(r"jnl_[0-9a-f]+", written).group(0)
        pending = self.instance.rstore.journals(scope=self.scope(), state="pending_review", limit=5)
        self.assertEqual([j["journal_id"] for j in pending], [journal_id])

        kept = await self.command(f"收下 {journal_id}")
        self.assertIn("认识变化", kept[0])
        self.assertEqual(self.instance.rstore.journal(journal_id)["state"], "selected")
        active = self.instance.rstore.reflections(scope=self.scope(), state="active", limit=10)
        self.assertEqual(len(active), 1)
        self.assertIn("上下游标高", active[0]["new_view"])
        self.assertNotIn("主人", active[0]["new_view"])  # only the model-written revision lands here

        listed = await self.command("认识")
        self.assertIn(f"#{active[0]['id']}", listed[0])
        self.assertIn("上下游标高", listed[0])

        corrected = await self.command(f"更正 {active[0]['id']} 我现在觉得它更偏工程史")
        self.assertIn("记下了", corrected[0])
        self.assertEqual(self.instance.rstore.reflection(active[0]["id"])["state"], "superseded")
        now = self.instance.rstore.reflections(scope=self.scope(), state="active", limit=10)
        self.assertEqual(len(now), 1)
        self.assertIn("工程史", now[0]["new_view"])

        forgotten = await self.command(f"忘掉 {now[0]['id']}")
        self.assertIn("不再作为我的认识", forgotten[0])
        self.assertEqual(self.instance.rstore.reflections(scope=self.scope(), state="active", limit=10), [])

    async def test_journal_is_not_written_when_the_model_answers_garbage(self):
        text = await self.read()
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        self.context.llm_generate.return_value = support.SimpleNamespace(completion_text="我觉得这个视频挺好的，说得很对。")
        out = await self.instance.journal_tool(Event(), reading_id)
        self.assertIn("没有写成手账", out)
        self.assertEqual(self.instance.rstore.journals(scope=self.scope(), state=None, limit=5), [])

    async def test_discarding_a_journal_keeps_it_out_of_recall(self):
        text = await self.read()
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        written = await self.instance.journal_tool(Event(), reading_id)
        journal_id = re.search(r"jnl_[0-9a-f]+", written).group(0)
        dropped = await self.command(f"不记 {journal_id}")
        self.assertIn("不会进入以后的回忆", dropped[0])
        self.assertEqual(self.instance.rstore.journal(journal_id)["state"], "skipped")

        request = support.SimpleNamespace(system_prompt="")
        await self.instance.browsing_memory(Event(text="排水管网的标高怎么算"), request)
        self.assertIn("真实活动能力", request.system_prompt)
        self.assertNotIn("以前读过的相关材料", request.system_prompt)

    async def test_selected_journal_becomes_recallable_memory(self):
        text = await self.read()
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        written = await self.instance.journal_tool(Event(), reading_id)
        journal_id = re.search(r"jnl_[0-9a-f]+", written).group(0)
        await self.command(f"收下 {journal_id}")

        request = support.SimpleNamespace(system_prompt="")
        await self.instance.browsing_memory(Event(text="你还记得排水管网的标高吗"), request)
        self.assertIn("以前读过的相关材料与认识", request.system_prompt)
        self.assertIn("上下游标高", request.system_prompt)
        self.assertIn("不当作新证据", request.system_prompt)

    async def test_other_session_cannot_see_or_keep_this_journal(self):
        text = await self.read()
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        written = await self.instance.journal_tool(Event(), reading_id)
        journal_id = re.search(r"jnl_[0-9a-f]+", written).group(0)
        other = Event(umo="qq:private:99999")
        self.assertIn("不属当前会话", (await self.command(f"收下 {journal_id}", other))[0])
        self.assertIn("不在当前会话", await self.instance.journal_tool(other, reading_id))
        self.assertEqual(self.instance.rstore.journal(journal_id)["state"], "pending_review")

    async def test_status_tells_the_owner_frames_are_unavailable(self):
        status = (await self.command("状态"))[0]
        self.assertIn("缺 ffmpeg", status)
        self.assertIn("手账待核对0条", status)

    async def test_reopening_the_store_keeps_schema_and_cursors_valid(self):
        text = await self.read()
        cursor = re.search(r'feiyu_read_more\(cursor="([^"]+)"\)', text).group(1)
        reading_id = re.search(r"\[阅读 (rd_[0-9a-f]+)", text).group(1)
        self.instance.rstore.close()
        self.instance.rstore = plugin.ReadingStore(self.run_path / "browse.db")
        self.assertEqual(self.instance.rstore.db.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertIsNotNone(self.instance.rstore.reading(reading_id, self.scope()))
        # A restart must not invalidate a cursor the model was already given.
        self.assertEqual(self.instance.rstore.read_cursor(cursor, self.scope()), (reading_id, 1, 24))
        again = await self.instance.read_more_tool(Event(), cursor)
        self.assertIn("第25句", again)


class VisionCapabilityTests(unittest.IsolatedAsyncioTestCase):
    """A provider that cannot take images must never be allowed to 'look'."""

    def build(self, *, modalities=None, config=None, boom=False):
        context = Mock()
        if boom:
            context.provider_manager.get_provider_by_id = AsyncMock(side_effect=RuntimeError("boom"))
        else:
            provider = Mock()
            provider.provider_config = {} if modalities is None else {"modalities": modalities}
            context.provider_manager.get_provider_by_id = AsyncMock(return_value=provider)
        context.get_current_chat_provider_id = AsyncMock(return_value="chat-model")
        instance = plugin.FeiyuBiliPlugin(context, config or {})
        instance.store = Mock()
        instance.store.get = Mock(return_value={"umo": "qq:private:12345"})
        return instance

    async def test_text_only_provider_blocks_visual(self):
        instance = self.build(modalities=["text"])
        self.assertEqual(await instance._vision_ready(), ("chat-model", "provider_no_image"))

    async def test_image_capable_or_undeclared_provider_passes(self):
        for modalities in (["text", "image"], [], None):
            with self.subTest(modalities=modalities):
                instance = self.build(modalities=modalities)
                self.assertEqual(await instance._vision_ready(), ("chat-model", ""))

    async def test_configured_provider_id_wins(self):
        instance = self.build(modalities=["text"], config={"provider_id": "pinned-vl"})
        self.assertEqual(await instance._vision_ready(), ("pinned-vl", "provider_no_image"))

    async def test_provider_manager_failure_does_not_block(self):
        instance = self.build(boom=True)
        self.assertEqual(await instance._vision_ready(), ("chat-model", ""))

    async def test_vision_provider_id_beats_the_chat_provider(self):
        instance = self.build(modalities=["text", "image"],
                              config={"provider_id": "chat-only",
                                      "vision_provider_id": "xiaomi/mimo-v2.6-flash"})
        self.assertEqual(await instance._vision_ready(), ("xiaomi/mimo-v2.6-flash", ""))

    async def test_blank_vision_provider_falls_back_to_chat_provider(self):
        instance = self.build(modalities=["text"],
                              config={"provider_id": "chat-only", "vision_provider_id": "  "})
        self.assertEqual(await instance._vision_ready(), ("chat-only", "provider_no_image"))

    def test_reason_is_human_readable(self):
        self.assertIn("不支持看图", plugin.VISION_REASONS["provider_no_image"])
        self.assertIn("ffmpeg", plugin.VISION_REASONS["decoder_missing"])


if __name__ == "__main__":
    unittest.main()
