"""Plugin-level tests for the read-only danmaku path: audience noise, never evidence.

Danmaku are timestamped but authorless. These tests pin the two things that could go
wrong quietly: treating a danmaku as the uploader's words, and letting a danmaku sample
become a profile of the people in the room.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import test_plugin as support  # noqa: E402  (runs the astrbot stub loader)

plugin = support.plugin
core = support.core
Event = support.Event
BINDING = support.BINDING
OWNER = support.OWNER
BVID = support.BVID

JOURNAL_TEXT = json.dumps({
    "encounter": "夜里翻到这条", "material_facts": "有观众在刷泵站",
    "author_view": "作者强调落差", "ai_inference": "可能要泵站",
    "prior_judgment": "我以为管子粗就行", "revision": "我现在觉得落差才是关键",
    "omissions": "没提维护成本", "next_response": "下次先问维护",
}, ensure_ascii=False)


class FakeMemoryStore:
    def __init__(self):
        self.added, self.forgotten = [], []

    def get_owner(self, platform, user_id, bot_instance):
        return dict(OWNER)

    def build_context(self, **kwargs):
        return {"state": "[已核验的主人私聊状态]", "memory": ""}

    def add_record(self, **kwargs):
        self.added.append(kwargs)
        return dict(kwargs, id=f"mem{len(self.added)}")

    def forget_record(self, record_id, *, reason="", actor="owner"):
        self.forgotten.append((record_id, reason, actor))
        return True


class FakeClient:
    """Danmaku are injected, so a test can see exactly what the plugin did with them."""

    def __init__(self):
        self.calls, self.danmaku_calls = [], []

    async def detail(self, bvid):
        self.calls.append(("detail", bvid))
        return {"title": "老城区排水管网", "owner": "某UP", "duration": 600, "cid": 101,
                "pages": 1, "parts": [{"page": 1, "cid": 101, "part_title": "P1 管网",
                                       "duration": 600}]}

    async def subtitle(self, bvid, cid=None):
        return {"text": "管网要按标高排", "source": "bilibili_subtitle:part", "reason": "ok",
                "lang": "zh-CN", "segments": [{"from_ms": 0, "to_ms": 1200,
                                               "text": "管网要按标高排"}], "chars": 7}

    async def danmaku(self, bvid, cid=None, *, limit=40):
        self.danmaku_calls.append((bvid, cid, limit))
        return {"bvid": bvid, "cid": cid, "source": "bilibili_danmaku", "reason": "ok",
                "total": 812, "bucket_ms": 30000, "sampled": 3, "items": [
                    {"at_ms": 65000, "text": "泵站那段呢", "count": 4},
                    {"at_ms": 120000, "text": "这里说反了吧", "count": 1},
                    {"at_ms": 240000, "text": "前方高能", "count": 1}]}

    async def close(self):
        return None


class DanmakuPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.run_root = Path(__file__).resolve().parent / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "browse.db")
        self.rstore = plugin.ReadingStore(self.run_path / "browse.db")
        self.memory_db = FakeMemoryStore()
        self.memory = Mock(spec_set=["_identity", "_safe_store", "_persona"])
        self.memory._identity.side_effect = lambda event: dict(event.identity)
        self.memory._safe_store.return_value = self.memory_db
        self.memory._persona.return_value = "现有人格，不是通用助手"
        self.meta = SimpleNamespace(name="astrbot_plugin_feiyu_memory", activated=True,
                                    star_cls=self.memory)
        self.context = Mock(spec_set=["get_all_stars", "persona_manager", "send_message",
                                      "get_current_chat_provider_id", "llm_generate"])
        self.context.get_all_stars.return_value = [self.meta]
        self.context.persona_manager = None
        self.context.send_message = AsyncMock(return_value=True)
        self.context.get_current_chat_provider_id = AsyncMock(return_value="current-chat-model")
        self.context.llm_generate = AsyncMock(
            return_value=SimpleNamespace(completion_text=JOURNAL_TEXT))
        self.instance = plugin.FeiyuBiliPlugin(self.context, {})
        self.instance.store = self.store
        self.instance.rstore = self.rstore
        self.instance.client = FakeClient()
        self.instance.browser = Mock()
        self.instance.browser.lock = asyncio.Lock()
        self.instance.browser.run = AsyncMock(return_value={"status": "ok", "read": 0, "sent": 0})
        self.instance.media = plugin.MediaWorker(self.run_path / "media")
        self.instance.media.ffmpeg = None
        self.instance.media.ffprobe = None
        self.store.set("binding", dict(BINDING))
        self.instance.task = None
        self.scope = BINDING["umo"]

    async def asyncTearDown(self):
        self.rstore.close()
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        shutil.rmtree(resolved)

    async def command(self, action, event=None):
        return [text async for text in self.instance.command(event or Event(), action)]

    async def read(self, video=BVID):
        return await self.instance.read_tool(Event(), video, 1, False)

    async def test_danmaku_command_shows_time_heat_and_caveat(self):
        self.assertIn("管网", await self.read())
        text = (await self.command(f"弹幕 {BVID}"))[0]
        self.assertIn("只读了 3 条抽样", text)
        self.assertIn("01:05", text)
        self.assertIn("×4 泵站那段呢", text)
        self.assertIn("只有时间没有作者", text)
        self.assertIn("不是作者观点也不是画面", text)
        self.assertIn("能解析出 812 条", text)
        self.assertEqual(self.instance.client.danmaku_calls, [(BVID, 101, 40)])

    async def test_danmaku_switch_off_sends_no_request(self):
        self.instance.config = {"danmaku_enabled": False}
        text = (await self.command(f"弹幕 {BVID}"))[0]
        self.assertIn("弹幕读取被关掉了", text)
        self.assertEqual(self.instance.client.danmaku_calls, [])
        self.assertEqual(self.instance.client.calls, [])

    async def test_blank_video_uses_the_latest_reading(self):
        await self.read()
        text = (await self.command("弹幕"))[0]
        self.assertIn("老城区排水管网", text)
        self.assertEqual(self.instance.client.danmaku_calls[0][0], BVID)

    async def test_blank_video_without_reading_says_read_first(self):
        text = (await self.command("弹幕"))[0]
        self.assertIn("还没有读过任何一P", text)
        self.assertEqual(self.instance.client.danmaku_calls, [])

    async def test_non_owner_cannot_read_danmaku(self):
        text = (await self.command(f"弹幕 {BVID}", Event(owner=False)))[0]
        self.assertIn("只在已绑定的主人私聊开放", text)
        self.assertEqual(self.instance.client.danmaku_calls, [])

    async def test_empty_sample_explains_the_reason(self):
        async def empty(bvid, cid=None, *, limit=40):
            return {"bvid": bvid, "cid": cid, "source": "bilibili_danmaku", "reason": "empty",
                    "items": [], "total": 0, "bucket_ms": 30000, "sampled": 0}
        self.instance.client.danmaku = empty
        await self.read()
        text = (await self.command("弹幕"))[0]
        self.assertIn("没有可解析的弹幕", text)

    async def test_closed_danmaku_says_the_feature_is_off(self):
        async def closed(bvid, cid=None, *, limit=40):
            return {"bvid": bvid, "cid": cid, "source": "bilibili_danmaku", "reason": "closed",
                    "items": [], "total": 0, "bucket_ms": 30000, "sampled": 0}
        self.instance.client.danmaku = closed
        await self.read()
        text = (await self.command("弹幕"))[0]
        self.assertIn("弹幕功能已被关闭", text)

    async def test_journal_prompt_keeps_danmaku_as_audience_noise(self):
        await self.read()
        await self.command(f"弹幕 {BVID}")
        await self.command("手账")
        seen = self.context.llm_generate.await_args.kwargs
        prompt = seen["prompt"]
        self.assertIn("弹幕，受众反应，不是作者观点", prompt)
        self.assertIn("没有作者身份，只有时间", prompt)
        self.assertIn("泵站那段呢", prompt)
        self.assertIn("×4", prompt)
        self.assertIn("有观众在刷", seen["system_prompt"])
        self.assertIn("不许当成画面或作者的话", seen["system_prompt"])

    async def test_store_keeps_only_time_and_text_and_replaces_one_revision(self):
        reading = plugin.open_reading
        record = {"reading_id": plugin.new_id("rd"), "bvid": BVID, "page": 1, "cid": 101,
                  "scope": self.scope, "title": "t",
                  "author": "u", "part_title": "p", "duration": 600, "source_type": "x",
                  "subtitle_state": "timed", "subtitle_reason": "", "evidence": "text_read",
                  "fetched_at": plugin.clock_now()}
        row = self.rstore.begin_reading(**record)
        items = [{"at_ms": 1000, "text": "甲" * 80, "count": 9, "uid": "9", "nickname": "某人"},
                 {"at_ms": 2000, "text": "   ", "count": 1},
                 {"at_ms": 3000, "text": "丙", "count": 0}]
        self.assertEqual(self.rstore.add_danmaku(row["reading_id"], 1, items, "bilibili_danmaku"), 2)
        rows = self.rstore.danmaku(row["reading_id"], 1)
        self.assertEqual([r["text"] for r in rows], ["甲" * 60, "丙"])
        self.assertEqual([r["repeated"] for r in rows], [9, 1])
        self.assertNotIn("uid", rows[0])
        self.assertNotIn("nickname", str(rows))
        self.rstore.add_danmaku(row["reading_id"], 1, [{"at_ms": 5000, "text": "新的"}], "")
        self.assertEqual([r["text"] for r in self.rstore.danmaku(row["reading_id"], 1)], ["新的"])


if __name__ == "__main__":
    unittest.main()
