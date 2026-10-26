"""M6-D: the share outbox — the hand-off book that B站 writes and companion reads.

The properties worth testing are narrow:

- the same ``generation`` never becomes two different payloads (a conflict is an
  error, not a silent overwrite);
- a ticket can only be claimed once at a time, and a claim that goes stale can be
  taken back instead of being stuck forever;
- ``unknown`` is its own state and never gets replayed as "not sent yet";
- none of this touches ``deliveries`` (the old path keeps its own book until the
  hand-off is actually switched on), and nothing here sends anything.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent / "plugin" / "astrbot_plugin_feiyu_bili"
TMP = TESTS / "_tmp" / "share_outbox"

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 25, 14, 0, tzinfo=TZ)

SCOPE = "qq-snowluma:FriendMessage:2327106185"
OTHER_SCOPE = "qq-snowluma:FriendMessage:9999999999"
BV = "BV1xx411c7mD"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ob = _load("feiyu_share_outbox", "share_outbox.py")
# 让 share_port 的裸文件加载复用同一个 outbox 模块，异常类型才是同一个
import sys

sys.modules["_feiyu_outbox_for_share_port"] = ob
sp = _load("feiyu_share_port", "share_port.py")


def plan(**over):
    base = dict(
        generation="g-1",
        scope=SCOPE,
        bvid=BV,
        target=SCOPE,
        message="这个人的视频，你可能想看。",
    )
    base.update(over)
    return base


class OutboxCase(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self.path = TMP / f"{self._testMethodName}.db"
        if self.path.exists():
            self.path.unlink()
        for suffix in ("-wal", "-shm"):
            leftover = Path(str(self.path) + suffix)
            if leftover.exists():
                leftover.unlink()
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.box = ob.ShareOutbox(self.db)

    def tearDown(self):
        self.db.close()

    # ------------------------------------------------------------ 入参校验
    def test_missing_generation_is_refused(self):
        with self.assertRaises(ob.ShareOutboxError):
            self.box.enqueue(**plan(generation=""))

    def test_missing_target_is_refused(self):
        with self.assertRaises(ob.ShareOutboxError):
            self.box.enqueue(**plan(target=""))

    def test_empty_message_is_refused(self):
        with self.assertRaises(ob.ShareOutboxError):
            self.box.enqueue(**plan(message="   "))

    def test_bad_bvid_is_refused(self):
        with self.assertRaises(ob.ShareOutboxError):
            self.box.enqueue(**plan(bvid="av12345"))

    # ------------------------------------------------------------ 入队与去重
    def test_first_enqueue_is_pending(self):
        row = self.box.enqueue(**plan(), now=NOW)
        self.assertEqual(row["state"], ob.STATE_PENDING)
        self.assertEqual(row["attempts"], 0)
        self.assertFalse(row["duplicate"])
        self.assertEqual(len(row["payload_hash"]), 32)
        self.assertEqual(row["deadline"], (NOW + timedelta(minutes=30)).isoformat(timespec="seconds"))

    def test_same_payload_twice_is_a_duplicate_not_a_second_row(self):
        self.box.enqueue(**plan(), now=NOW)
        again = self.box.enqueue(**plan(), now=NOW)
        self.assertTrue(again["duplicate"])
        self.assertEqual(self.box.counts()["total"], 1)

    def test_same_generation_with_other_payload_conflicts(self):
        self.box.enqueue(**plan(), now=NOW)
        with self.assertRaises(ob.ShareConflict):
            self.box.enqueue(**plan(message="换一句话"), now=NOW)
        self.assertEqual(self.box.get("g-1")["message"], "这个人的视频，你可能想看。")

    def test_payload_hash_moves_when_any_field_moves(self):
        a = self.box.enqueue(**plan(), now=NOW)["payload_hash"]
        b = ob.payload_hash(scope=SCOPE, bvid=BV, page=1, revision=0,
                            target=SCOPE, message="另一句")
        self.assertNotEqual(a, b)

    # ------------------------------------------------------------ 认领
    def test_claim_moves_one_ticket_to_claimed(self):
        self.box.enqueue(**plan(), now=NOW)
        got = self.box.claim(SCOPE, now=NOW)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["state"], ob.STATE_CLAIMED)
        self.assertEqual(got[0]["attempts"], 1)
        self.assertTrue(got[0]["claim_token"])

    def test_claim_twice_yields_nothing_the_second_time(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.claim(SCOPE, now=NOW)
        self.assertEqual(self.box.claim(SCOPE, now=NOW), [])

    def test_claim_is_scoped(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.enqueue(**plan(generation="g-2", scope=OTHER_SCOPE, target=OTHER_SCOPE), now=NOW)
        got = self.box.claim(OTHER_SCOPE, now=NOW)
        self.assertEqual([r["generation"] for r in got], ["g-2"])

    def test_expired_pending_never_gets_claimed(self):
        self.box.enqueue(**plan(), now=NOW, deadline=(NOW - timedelta(minutes=1)).isoformat())
        self.assertEqual(self.box.claim(SCOPE, now=NOW), [])
        self.assertEqual(self.box.get("g-1")["state"], ob.STATE_EXPIRED)

    def test_stale_claim_is_released_and_can_be_taken_again(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.claim(SCOPE, now=NOW, ttl_minutes=15)
        later = NOW + timedelta(minutes=20)
        again = self.box.claim(SCOPE, now=later, ttl_minutes=15)
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["generation"], "g-1")
        self.assertEqual(again[0]["attempts"], 2)

    # ------------------------------------------------------------ 回执
    def test_ack_sent_lands_on_acked(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.claim(SCOPE, now=NOW)
        row = self.box.ack("g-1", ob.OUTCOME_SENT, detail="fake:1", now=NOW)
        self.assertEqual(row["state"], ob.STATE_ACKED)
        self.assertEqual(row["outcome"], ob.OUTCOME_SENT)

    def test_ack_unknown_is_its_own_state(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.claim(SCOPE, now=NOW)
        row = self.box.ack("g-1", ob.OUTCOME_UNKNOWN, now=NOW)
        self.assertEqual(row["state"], ob.STATE_UNKNOWN)
        self.assertFalse(row["duplicate"])

    def test_ack_twice_keeps_the_first_verdict(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.claim(SCOPE, now=NOW)
        self.box.ack("g-1", ob.OUTCOME_UNKNOWN, now=NOW)
        again = self.box.ack("g-1", ob.OUTCOME_SENT, now=NOW)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["state"], ob.STATE_UNKNOWN)

    def test_ack_rejects_unknown_outcome_name(self):
        self.box.enqueue(**plan(), now=NOW)
        with self.assertRaises(ob.ShareOutboxError):
            self.box.ack("g-1", "delivered")

    def test_ack_without_ticket_is_refused(self):
        with self.assertRaises(ob.ShareOutboxError):
            self.box.ack("nope", ob.OUTCOME_SENT)

    # ------------------------------------------------------------ 账本本身
    def test_counts_add_up(self):
        self.box.enqueue(**plan(), now=NOW)
        self.box.enqueue(**plan(generation="g-2"), now=NOW)
        self.box.claim(SCOPE, now=NOW, limit=2)
        self.box.ack("g-1", ob.OUTCOME_SENT, now=NOW)
        counts = self.box.counts()
        self.assertEqual(counts[ob.STATE_ACKED], 1)
        self.assertEqual(counts[ob.STATE_CLAIMED], 1)
        self.assertEqual(counts["total"], 2)

    def test_expire_only_touches_overdue_rows(self):
        self.box.enqueue(**plan(), now=NOW, deadline=(NOW - timedelta(minutes=5)).isoformat())
        self.box.enqueue(**plan(generation="g-2"), now=NOW)
        self.assertEqual(self.box.expire(now=NOW), 1)
        self.assertEqual(self.box.get("g-1")["state"], ob.STATE_EXPIRED)
        self.assertEqual(self.box.get("g-2")["state"], ob.STATE_PENDING)

    def test_pending_listing_is_scoped_and_ordered(self):
        self.box.enqueue(**plan(generation="g-b"), now=NOW + timedelta(minutes=1))
        self.box.enqueue(**plan(generation="g-a"), now=NOW)
        rows = self.box.pending(SCOPE)
        self.assertEqual([r["generation"] for r in rows], ["g-a", "g-b"])

    # ------------------------------------------------------------ 不越界
    def test_outbox_does_not_touch_the_old_delivery_tables(self):
        self.db.executescript(
            "CREATE TABLE deliveries(id INTEGER PRIMARY KEY,bvid TEXT NOT NULL UNIQUE,day TEXT,"
            "target TEXT,status TEXT,message TEXT,created_at TEXT);"
        )
        before = self.db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        self.box.enqueue(**plan(), now=NOW)
        self.box.claim(SCOPE, now=NOW)
        self.box.ack("g-1", ob.OUTCOME_SENT, now=NOW)
        after = self.db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
        self.assertEqual(before, after)

    def test_outbox_refuses_to_invent_a_database(self):
        with self.assertRaises(ob.ShareOutboxError):
            ob.ShareOutbox(None)

    # ------------------------------------------------------------ 交接面
    def test_port_reports_it_does_not_send(self):
        port = sp.BiliSharePort(
            type("Fake", (), {"store": type("FakeStore", (), {"db": self.db})})(),
            outbox=self.box,
        )
        self.assertFalse(port.status()["sends_here"])
        self.assertEqual(port.status()["port"], sp.SHARE_PORT_NAME)

    def test_port_submit_and_claim_roundtrip(self):
        port = sp.BiliSharePort(
            type("Fake", (), {"store": type("FakeStore", (), {"db": self.db})})(),
            outbox=self.box,
        )
        row = port.submit(plan())
        self.assertEqual(row["state"], ob.STATE_PENDING)
        got = port.claim({"scope": SCOPE, "limit": 1})
        self.assertEqual([r["generation"] for r in got], ["g-1"])
        acked = port.ack({"generation": "g-1", "outcome": "sent", "detail": "ok"})
        self.assertEqual(acked["state"], ob.STATE_ACKED)

    def test_port_submit_needs_the_required_fields(self):
        port = sp.BiliSharePort(
            type("Fake", (), {"store": type("FakeStore", (), {"db": self.db})})(),
            outbox=self.box,
        )
        with self.assertRaises(ob.ShareOutboxError):
            port.submit({"generation": "g-1"})

    def test_plan_helper_computes_the_same_hash(self):
        made = sp.plan_of(generation="g-1", scope=SCOPE, bvid=BV, target=SCOPE,
                          message="这个人的视频，你可能想看。")
        self.assertEqual(made["payload_hash"], self.box.enqueue(**plan(), now=NOW)["payload_hash"])

    # ------------------------------------------------------------ 静态边界
    def test_no_send_or_network_code_lives_here(self):
        for name in ("share_outbox.py", "share_port.py"):
            text = (PLUGIN / name).read_text(encoding="utf-8")
            for bad in ("aiohttp", "httpx", "requests.", "subprocess", "urlopen",
                        "socket", "asyncio"):
                self.assertNotIn(bad, text, f"{name} 不该出现 {bad}")
            for bad in ("self.sender(", ".send_message("):
                self.assertNotIn(bad, text, f"{name} 不该自己发送")


if __name__ == "__main__":
    unittest.main()
