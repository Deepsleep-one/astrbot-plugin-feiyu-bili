"""Offline tests for growth.py: interests, proposals and derived views."""
from __future__ import annotations

import shutil
import sys
import unittest
import uuid
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin" / "astrbot_plugin_feiyu_bili"
sys.path.insert(0, str(PLUGIN))

import growth  # noqa: E402
from reading_store import ReadingStore  # noqa: E402


class FakeClient:
    def __init__(self, hits=None, fail=()):
        self.hits = hits or {}
        self.fail = set(fail)
        self.calls = []

    async def search(self, keyword, limit=5):
        self.calls.append((keyword, limit))
        if keyword in self.fail:
            raise RuntimeError("boom")
        return [dict(h) for h in self.hits.get(keyword, [])][:limit]


class FakeMemoryStore:
    def __init__(self):
        self.added, self.forgotten = [], []
        self.records = {}

    def add_record(self, **kwargs):
        self.added.append(kwargs)
        rid = f"m{len(self.added)}"
        self.records[rid] = dict(kwargs, id=rid)
        return self.records[rid]

    def forget_record(self, record_id, *, reason="", actor="owner"):
        self.forgotten.append((record_id, reason, actor))
        return record_id in self.records


class StoreCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / f"_tmp_growth_{uuid.uuid4().hex[:8]}"
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = ReadingStore(self.root / "browse.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def reading(self, **over):
        row = {"reading_id": "rd1", "revision": 1, "bvid": "BV0000000001", "page": 1,
               "title": "老城区排水管网", "part_title": "P1 管网", "duration": 600}
        row.update(over)
        return row

    def journal(self, **fields):
        data = {"encounter": "夜里看到这条", "material_facts": "管网按标高排",
                "author_view": "作者强调落差", "ai_inference": "可能需要泵站",
                "prior_judgment": "我以前以为只要管子粗", "revision": "我现在觉得落差才是关键",
                "omissions": "没讲维护成本", "next_response": "下次先问维护"}
        data.update(fields)
        return {"journal_id": "jnl1", "reading_id": "rd1", "revision": 1,
                "fields": data, "refs": {"title": "老城区排水管网"}}


class TopicTests(StoreCase):
    def test_clean_topic_strips_noise_and_limits(self):
        self.assertEqual(growth.clean_topic("  管网，要按【标高】排。 "), "管网 要按 标高 排")
        self.assertEqual(len(growth.clean_topic("长" * 80)), 24)
        self.assertEqual(growth.clean_topic(None), "")
        self.assertNotIn("\x07", growth.clean_topic("a\x07b"))

    def test_topic_key_ignores_case_space_and_punctuation(self):
        self.assertEqual(growth.topic_key("SMT 贴片,工艺"), growth.topic_key("smt贴片工艺"))
        self.assertNotEqual(growth.topic_key("管网"), growth.topic_key("泵站"))

    def test_topic_from_prefers_the_part_that_was_read(self):
        self.assertEqual(growth.topic_from(self.reading(), {"part_title": "P2 泵站"}), "P2 泵站")
        self.assertEqual(growth.topic_from({"title": "老城区排水管网"}), "老城区排水管网")
        self.assertEqual(growth.topic_from({"title": "B站视频"}), "")

    def test_interest_line_is_hers_not_the_owners(self):
        self.assertEqual(growth.interest_line(self.store, "s1"), "")
        self.store.bump_interest("s1", "排水管网", 4)
        self.store.bump_interest("s1", "泵站", 1)
        line = growth.interest_line(self.store, "s1", 2)
        self.assertIn("我最近比较在意", line)
        self.assertIn("排水管网（反复看）", line)
        self.assertIn("泵站（看过一次）", line)
        self.assertEqual(growth.interest_line(self.store, "other"), "")

    def test_interest_weight_accumulates_per_scope(self):
        self.store.bump_interest("s1", "管网", 1)
        row = self.store.bump_interest("s1", "管网", 2)
        self.assertEqual(row["weight"], 3.0)
        self.assertEqual(self.store.bump_interest("s2", "管网", 1)["weight"], 1.0)
        with self.assertRaises(ValueError):
            self.store.bump_interest("s1", "   ")


class ProposeTests(StoreCase):
    async def test_proposals_come_from_interests_and_stay_pending(self):
        self.store.bump_interest("s1", "排水管网", 3)
        client = FakeClient({"排水管网": [
            {"bvid": "BV0000000002", "title": "管网标高怎么定", "author": "某UP"},
            {"bvid": "BV0000000003", "title": "泵站设计入门", "author": "另一个UP"},
        ]})
        rows = await growth.propose(client, self.store, "s1", ["排水管网"], limit=2)
        self.assertEqual([r["bvid"] for r in rows], ["BV0000000002", "BV0000000003"])
        self.assertTrue(all(r["state"] == "pending" for r in rows))
        self.assertIn("排水管网", rows[0]["reason"])
        self.assertEqual(client.calls, [("排水管网", 2)])

    async def test_already_read_and_pending_are_not_offered_again(self):
        self.store.begin_reading(reading_id="rd1", bvid="BV0000000002", page=1, cid=1,
                                 revision=1, scope="s1", title="t", author="a", part_title="p",
                                 duration=1, fetched_at="2026-10-25T00:00:00",
                                 source_type="subtitle", subtitle_state="timed",
                                 subtitle_reason="ok", evidence="text_read")
        self.store.add_proposal("prop0", "s1", "BV0000000003")
        client = FakeClient({"排水管网": [
            {"bvid": "BV0000000002", "title": "已读过的"},
            {"bvid": "BV0000000003", "title": "已在待读里的"},
            {"bvid": "BV0000000004", "title": "新的"},
        ]})
        rows = await growth.propose(client, self.store, "s1", ["排水管网"], per_term=3)
        self.assertEqual([r["bvid"] for r in rows], ["BV0000000004"])

    async def test_one_failing_term_does_not_kill_the_rest(self):
        client = FakeClient({"泵站": [{"bvid": "BV0000000005", "title": "泵站"}]}, fail=("管网",))
        rows = await growth.propose(client, self.store, "s1", ["管网", "泵站"])
        self.assertEqual([r["bvid"] for r in rows], ["BV0000000005"])

    async def test_limit_is_respected_and_terms_deduped(self):
        client = FakeClient({"管网": [{"bvid": f"BV00000000{i:02d}", "title": f"t{i}"} for i in range(3, 9)]})
        rows = await growth.propose(client, self.store, "s1", ["管网", "管网", "  "], limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(client.calls), 1)

    def test_proposal_index_matches_what_the_owner_sees(self):
        self.store.add_proposal("p1", "s1", "BV0000000001", title="第一条")
        self.store.add_proposal("p2", "s1", "BV0000000002", title="第二条")
        self.store.set_proposal_state("p1", "dismissed")
        self.assertEqual(self.store.proposal_at("s1", 1)["proposal_id"], "p2")
        self.assertIsNone(self.store.proposal_at("s1", 2))
        self.assertIsNone(self.store.proposal_at("other", 1))

    def test_format_proposals_numbering_and_empty(self):
        rows = [{"title": "管网标高", "author": "某UP", "bvid": "BV0000000002",
                 "reason": "来自我最近在意的「排水管网」"}]
        text = growth.format_proposals(rows)
        self.assertIn("1. 管网标高 —— 某UP", text)
        self.assertIn("理由：来自我最近在意的「排水管网」", text)
        self.assertIn("没有想读的", growth.format_proposals([]))


class ViewTests(StoreCase):
    def test_payload_is_derived_with_evidence_never_asserted(self):
        payload = growth.view_payload(owner_id="primary", journal=self.journal(),
                                      reflection={"new_view": "落差才是关键"},
                                      reading=self.reading(), page={"part_title": "P1 管网"})
        self.assertEqual(payload["kind"], "fact")
        self.assertEqual(payload["epistemic_type"], "derived")
        self.assertEqual(payload["scope_type"], "owner_private")
        self.assertEqual(payload["owner_id"], "primary")
        self.assertTrue(payload["fact_key"].startswith("feiyu.bili.view:"))
        self.assertEqual(payload["supersede_epistemic"], ("derived",))
        self.assertIn("不是主人的说法", payload["content"])
        self.assertIn("落差才是关键", payload["content"])
        self.assertLessEqual(len(payload["content"]), 1900)
        self.assertEqual(payload["evidence"][0]["kind"], "bilibili_reading")
        self.assertIn("rd1", payload["evidence"][0]["ref"])
        self.assertEqual(payload["op_key"], "bili-view:jnl1")

    def test_no_view_without_a_revision(self):
        self.assertIsNone(growth.view_payload(owner_id="primary", journal=self.journal(revision=""),
                                              reflection=None, reading=self.reading()))

    def test_write_links_and_forget_tombstones(self):
        db = FakeMemoryStore()
        payload = growth.view_payload(owner_id="primary", journal=self.journal(), reflection=None,
                                      reading=self.reading())
        memory_id = growth.write_view(db, self.store, payload, "jnl1")
        self.assertEqual(memory_id, "m1")
        self.assertEqual(self.store.memory_link(growth.KIND_VIEW, "jnl1"), "m1")
        self.assertTrue(growth.forget_view(db, self.store, "jnl1", reason="主人让我忘掉"))
        self.assertEqual(db.forgotten[0][0], "m1")
        self.assertEqual(self.store.memory_link(growth.KIND_VIEW, "jnl1"), "")

    def test_write_without_store_is_a_no_op(self):
        self.assertEqual(growth.write_view(None, self.store, {"content": "x"}, "jnl1"), "")
        self.assertFalse(growth.forget_view(None, self.store, "jnl1"))


if __name__ == "__main__":
    unittest.main()
