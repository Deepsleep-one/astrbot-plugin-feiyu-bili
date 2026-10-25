"""Plugin-level tests for growth: proposals need approval, kept views stay derived."""
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

KIND_VIEW = "bili_view"  # growth.KIND_VIEW, the memory link kind
PROPOSED = "BV0000000002"
JOURNAL_TEXT = json.dumps({
    "encounter": "夜里翻到这条", "material_facts": "管网要按标高排",
    "author_view": "作者强调落差", "ai_inference": "可能要泵站",
    "prior_judgment": "我以为管子粗就行", "revision": "我现在觉得落差才是关键",
    "omissions": "没提维护成本", "next_response": "下次先问维护",
}, ensure_ascii=False)


class FakeMemoryStore:
    """Only what the bili plugin needs; every write is recorded for inspection."""

    def __init__(self):
        self.added, self.forgotten = [], []
        self.records = {}

    def get_owner(self, platform, user_id, bot_instance):
        return dict(OWNER)

    def build_context(self, **kwargs):
        return {"state": "[已核验的主人私聊状态]", "memory": ""}

    def add_record(self, **kwargs):
        self.added.append(kwargs)
        rid = f"mem{len(self.added)}"
        self.records[rid] = dict(kwargs, id=rid)
        return self.records[rid]

    def forget_record(self, record_id, *, reason="", actor="owner"):
        self.forgotten.append((record_id, reason, actor))
        return record_id in self.records


class FakeClient:
    def __init__(self):
        self.searches = []
        self.details = []

    async def detail(self, bvid):
        self.details.append(bvid)
        return {"title": "老城区排水管网", "owner": "某UP", "duration": 600, "cid": 101,
                "pages": 2, "parts": [{"page": 1, "cid": 101, "part_title": "P1 管网",
                                       "duration": 300}]}

    async def subtitle(self, bvid, cid=None):
        return {"text": "管网要按标高排", "source": "bilibili_subtitle:part", "reason": "ok",
                "lang": "zh-CN", "segments": [{"from_ms": 0, "to_ms": 1200,
                                               "text": "管网要按标高排"}], "chars": 7}

    async def search(self, keyword, limit=5):
        self.searches.append((keyword, limit))
        return [{"bvid": PROPOSED, "title": f"{keyword}入门", "author": "某UP"}]

    async def close(self):
        return None


class GrowthPluginTests(unittest.IsolatedAsyncioTestCase):
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

    def readings(self):
        return self.rstore.db.execute("SELECT bvid, page, scope FROM readings").fetchall()

    # ---- proposals ------------------------------------------------------
    async def test_proposals_come_from_interests_and_read_nothing(self):
        self.rstore.bump_interest(self.scope, "排水管网", 2)
        result = await self.command("提议")
        self.assertIn("我想读这几条", result[0])
        self.assertIn("排水管网入门", result[0])
        self.assertIn("等你点头", result[0])
        self.assertEqual(self.instance.client.searches, [("排水管网", 2)])
        self.assertEqual(self.readings(), [])
        self.context.llm_generate.assert_not_awaited()

    async def test_approving_one_proposal_reads_only_that_video(self):
        self.rstore.bump_interest(self.scope, "排水管网", 2)
        await self.command("提议")
        result = await self.command(f"读 1")
        self.assertIn("我提的第1条", result[0])
        self.assertEqual([r["bvid"] for r in self.readings()], [PROPOSED])
        pending = self.rstore.proposals(self.scope, state="pending")
        self.assertEqual(pending, [])
        self.assertEqual(self.instance.client.details, [PROPOSED])

    async def test_dismissed_proposal_is_never_read(self):
        self.rstore.bump_interest(self.scope, "排水管网", 2)
        await self.command("提议")
        self.assertIn("划掉了第1条", (await self.command("不读 1"))[0])
        self.assertIn("没有这个编号的提议", (await self.command("读 1"))[0])
        self.assertEqual(self.readings(), [])
        self.assertEqual(self.instance.client.details, [])

    async def test_unknown_index_does_not_touch_the_network(self):
        self.assertIn("没有这个编号的提议", (await self.command("读 9"))[0])
        self.assertEqual(self.instance.client.details, [])

    # ---- interests ------------------------------------------------------
    async def test_reading_a_part_raises_her_interest_in_that_part(self):
        await self.command(f"读 BV0000000001")
        topics = [r["topic"] for r in self.rstore.interests(self.scope)]
        self.assertEqual(topics, ["P1 管网"])
        self.assertIn("管网", self.rstore.interests(self.scope)[0]["topic"])

    # ---- derived views --------------------------------------------------
    async def test_kept_journal_writes_a_derived_view_not_an_assertion(self):
        await self.command(f"读 BV0000000001")
        journal = await self.command("手账")
        journal_id = journal[0].split("手账 ")[1].split("（待核对）")[0]
        kept = await self.command(f"收下 {journal_id}")
        self.assertIn("派生标注", kept[0])
        self.assertEqual(len(self.memory_db.added), 1)
        payload = self.memory_db.added[0]
        self.assertEqual(payload["epistemic_type"], "derived")
        self.assertEqual(payload["kind"], "fact")
        self.assertEqual(payload["owner_id"], "primary")
        self.assertEqual(payload["scope_type"], "owner_private")
        self.assertFalse(payload["fact_key"].startswith("feiyu.owner"))
        self.assertIn("不是主人的说法", payload["content"])
        self.assertIn("落差才是关键", payload["content"])
        self.assertEqual(payload["evidence"][0]["kind"], "bilibili_reading")
        self.assertIn(journal_id, payload["op_key"])
        self.assertTrue(self.rstore.memory_link(KIND_VIEW, journal_id))

    async def test_forgetting_a_reflection_also_forgets_the_memory_view(self):
        await self.command(f"读 BV0000000001")
        journal = await self.command("手账")
        journal_id = journal[0].split("手账 ")[1].split("（待核对）")[0]
        await self.command(f"收下 {journal_id}")
        rows = self.rstore.reflections(scope=self.scope, state="active", limit=5)
        self.assertEqual(len(rows), 1)
        result = await self.command(f"忘掉 {rows[0]['id']}")
        self.assertIn("一起忘掉了", result[0])
        self.assertEqual(self.memory_db.forgotten[0][0], "mem1")
        self.assertEqual(self.rstore.memory_link(KIND_VIEW, journal_id), "")

    async def test_correction_supersedes_the_view_instead_of_adding_a_second_opinion(self):
        await self.command(f"读 BV0000000001")
        journal = await self.command("手账")
        journal_id = journal[0].split("手账 ")[1].split("（待核对）")[0]
        await self.command(f"收下 {journal_id}")
        rows = self.rstore.reflections(scope=self.scope, state="active", limit=5)
        await self.command(f"更正 {rows[0]['id']} 我觉得它更像工程史")
        self.assertEqual(len(self.memory_db.added), 2)
        first, second = self.memory_db.added
        self.assertEqual(first["fact_key"], second["fact_key"])
        self.assertIn("工程史", second["content"])
        self.assertEqual(self.memory_db.forgotten, [])

    async def test_sync_off_keeps_the_view_local(self):
        self.instance.config["growth_memory_sync"] = False
        await self.command(f"读 BV0000000001")
        journal = await self.command("手账")
        journal_id = journal[0].split("手账 ")[1].split("（待核对）")[0]
        kept = await self.command(f"收下 {journal_id}")
        self.assertIn("没写入", kept[0])
        self.assertEqual(self.memory_db.added, [])

    async def test_status_reports_her_wants_without_reading_them(self):
        self.rstore.bump_interest(self.scope, "排水管网", 2)
        await self.command("提议")
        status = (await self.command("状态"))[0]
        self.assertIn("兴趣1条", status)
        self.assertIn("1条还等你点头", status)
        self.assertEqual(self.readings(), [])


if __name__ == "__main__":
    unittest.main()
