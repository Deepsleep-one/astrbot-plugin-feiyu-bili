"""M6-A: the read-only bridge (`BiliReadPortV1`) the companion plugin talks to.

Everything here is offline: no network, no ffmpeg, no AstrBot runtime, and the
database is a throwaway file under `tests/_tmp/`. The point of these checks is
narrow and boring on purpose:

- the snapshot has the shape the companion's whitelist expects;
- it never overstates evidence ("拿到过条目" is not "读过");
- it writes nothing at all;
- it never raises into the chat, and never hands the caller a real file handle.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

TESTS = Path(__file__).resolve().parent
PLUGIN = TESTS.parent / "plugin" / "astrbot_plugin_feiyu_bili"
TMP = TESTS / "_tmp" / "read_port"

SCOPE = "qq-snowluma:FriendMessage:2327106185"
OTHER_SCOPE = "qq-snowluma:FriendMessage:9999999999"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load("feiyu_read_port_store", "reading_store.py")
rp = _load("feiyu_read_port", "read_port.py")


class FakeKV:
    """正好够用的 KV：不看实现，只回答值。"""

    def __init__(self, values=None, counts=None):
        self.values = dict(values or {})
        self.counts = dict(counts or {})

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        raise AssertionError("只读桥不许写 KV")

    def daily_count(self, day=None):
        return self.counts.get("shares", 0)

    def daily_likes(self, day=None):
        return self.counts.get("likes", 0)

    def round_count(self, day=None):
        return self.counts.get("rounds", 0)


class FakePlugin:
    def __init__(self, store, kv=None, config=None, likes=True):
        self.rstore = store
        self.store = kv
        self.config = dict(config or {})
        self._likes = likes

    def _likes_enabled(self):
        return self._likes


def _config(**over):
    base = {"daily_likes": 3, "daily_shares": 2, "asr_daily_limit": 4,
            "share_cooldown_minutes": 120, "quiet_start": 23, "quiet_end": 9}
    base.update(over)
    return base


class BridgeTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True, exist_ok=True)
        self.store = rs.ReadingStore(TMP / "browse.db")
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(TMP, ignore_errors=True)

    # ---- 造数据 --------------------------------------------------------
    def _reading(self, bvid="BV1xx", page=1, evidence="text_read",
                 subtitle_state="official", duration=600.0, scope=SCOPE):
        return self.store.begin_reading(
            reading_id=f"rd-{bvid}-{page}", bvid=bvid, page=page, cid=12345,
            scope=scope, title="标题", author="UP", duration=duration,
            fetched_at="2026-10-25T12:00:00", subtitle_state=subtitle_state,
            evidence=evidence,
        )

    def _segments(self, reading, upto_ms):
        self.store.add_segments(reading["reading_id"], reading["revision"],
                                [{"from_ms": 0, "to_ms": upto_ms, "text": "一段字幕"}])

    def _facade(self, **over):
        plugin = FakePlugin(self.store, kv=over.pop("kv", FakeKV()),
                            config=over.pop("config", _config()),
                            likes=over.pop("likes", True))
        return rp.BiliReadFacade(plugin)

    _DEFAULT = object()

    def _snap(self, *, kv=_DEFAULT, config=_DEFAULT, **query):
        """`kv` / `config` 是给插件实例的，不是查询参数——别混进 query 里。"""
        query.setdefault("scope", SCOPE)
        facade = self._facade(
            kv=FakeKV() if kv is self._DEFAULT else kv,
            config=_config() if config is self._DEFAULT else config,
        )
        return facade.snapshot(query)

    def _journal(self, journal_id="j1", scope=SCOPE):
        """认识是挂在 journal 上、journal 挂在 reading 上的：先把这条链造出来。"""
        reading = self._reading(f"BV-{journal_id}", scope=scope)
        self.store.add_journal(journal_id, reading["reading_id"], reading["revision"],
                               {"encounter": "遇见"}, {"reading_id": reading["reading_id"]})
        return reading

    # ---- 形状 ----------------------------------------------------------
    def test_shape_and_version(self):
        snap = self._snap()
        self.assertEqual(snap["version"], rp.SNAPSHOT_VERSION)
        self.assertEqual(snap["scope"], SCOPE)
        for key in ("readings", "reflections", "interests", "proposals"):
            self.assertIsInstance(snap[key], list)
        self.assertIsInstance(snap["runtime"], dict)
        self.assertIsInstance(snap["notes"], list)

    def test_evidence_is_never_overstated(self):
        full = self._reading("BVfull")
        self._segments(full, 600_000)
        meta = self._reading("BVmeta", evidence="metadata_only")
        snap = self._snap()
        by_bvid = {item["bvid"]: item for item in snap["readings"]}
        self.assertEqual(by_bvid["BVfull"]["evidence"], "official_subtitle")
        self.assertEqual(by_bvid["BVfull"]["coverage"], "full")
        self.assertEqual(by_bvid["BVfull"]["subtitle_state"], "official")
        # 只有条目、没读到文本：既不是读过，也不算看完整
        self.assertEqual(by_bvid["BVmeta"]["evidence"], "metadata_only")
        self.assertEqual(by_bvid["BVmeta"]["coverage"], "none")

    def test_unknown_evidence_falls_back_to_metadata_only(self):
        self._reading("BVweird", evidence="something_new")
        snap = self._snap()
        self.assertEqual(snap["readings"][0]["evidence"], "metadata_only")
        self.assertEqual(snap["readings"][0]["coverage"], "none")

    def test_partial_coverage(self):
        half = self._reading("BVhalf")
        self._segments(half, 300_000)
        snap = self._snap()
        self.assertEqual(snap["readings"][0]["coverage"], "partial")

    def test_no_duration_means_partial(self):
        unknown = self._reading("BVnodur", duration=None)
        self._segments(unknown, 600_000)
        snap = self._snap()
        self.assertEqual(snap["readings"][0]["coverage"], "partial")

    def test_asr_evidence_maps_to_machine_transcript(self):
        reading = self._reading("BVasr", evidence="asr_transcribed", subtitle_state="asr")
        self._segments(reading, 600_000)
        snap = self._snap()
        self.assertEqual(snap["readings"][0]["evidence"], "machine_transcript")
        self.assertEqual(snap["readings"][0]["subtitle_state"], "asr")

    # ---- 作用域 --------------------------------------------------------
    def test_other_scope_is_not_returned(self):
        self._reading("BVmine")
        self._reading("BVtheirs", scope=OTHER_SCOPE)
        snap = self._snap()
        self.assertEqual([item["bvid"] for item in snap["readings"]], ["BVmine"])

    def test_every_item_carries_the_caller_scope(self):
        self._reading("BVmine")
        snap = self._snap()
        self.assertEqual(snap["readings"][0]["scope"], SCOPE)

    # ---- 认识 / 兴趣 / 提议 --------------------------------------------
    def test_reflection_keeps_pointer_to_memory(self):
        self._journal("j1")
        self.store.add_reflection(kind="view", topic="明朝", old_view="以前觉得闷",
                                  new_view="原来制度史也能读得下去", journal_id="j1",
                                  refs={"reading_id": "rd-BV1-1"})
        self.store.link_memory("bili_view", "j1", "r-deadbeef")
        snap = self._snap()
        item = snap["reflections"][0]
        self.assertEqual(item["reflection_id"], "1")
        self.assertEqual(item["text"], "原来制度史也能读得下去")
        self.assertEqual(item["source"], "reflection")  # 不能是 journal：那会被当成旧手账回填
        self.assertEqual(item["memory_id"], "r-deadbeef")
        self.assertEqual(item["source_refs"], ["rd-BV1-1"])
        self.assertEqual(item["scope"], SCOPE)
        self.assertTrue(item["updated_at"])

    def test_withdrawn_reflection_not_returned(self):
        self._journal("j2")
        rid = self.store.add_reflection(kind="view", topic="t", old_view="a",
                                        new_view="b", journal_id="j2", refs={})
        self.store.withdraw_reflection(rid)
        self.assertEqual(self._snap()["reflections"], [])

    def test_reflection_without_memory_link_is_empty_string(self):
        self._journal("j3")
        self.store.add_reflection(kind="view", topic="t", old_view="a",
                                  new_view="b", journal_id="j3", refs={})
        self.assertEqual(self._snap()["reflections"][0]["memory_id"], "")

    def test_interests_are_topic_and_weight_only(self):
        self.store.bump_interest(SCOPE, "制度史", 2.0)
        item = self._snap()["interests"][0]
        self.assertEqual(item["topic"], "制度史")
        self.assertEqual(item["weight"], 2.0)
        self.assertNotIn("scope", item)  # 兴趣只交公开主题与权重
        self.assertTrue(item["updated_at"])

    def test_only_pending_proposals(self):
        self.store.add_proposal("p-keep", SCOPE, "BVkeep", title="留着")
        self.store.add_proposal("p-drop", SCOPE, "BVdrop", state="approved")
        snap = self._snap()
        self.assertEqual([p["proposal_id"] for p in snap["proposals"]], ["p-keep"])
        self.assertEqual(snap["proposals"][0]["revision"], 1)
        self.assertEqual(snap["proposals"][0]["scope"], SCOPE)

    # ---- 运行摘要 ------------------------------------------------------
    def test_runtime_reads_live_limits(self):
        kv = FakeKV({"auto": True, "next_wake": "2026-10-25T12:30:00",
                     "last_error": json.dumps({"at": "2026-10-25T11:00:00",
                                               "type": "TimeoutError",
                                               "message": "含 token=abc 的原文"})},
                    {"likes": 2, "shares": 1, "rounds": 4})
        self.store.reserve_asr(SCOPE, rp._local_day(), limit=9)
        snap = self._snap(kv=kv, config=_config(daily_shares=5, asr_daily_limit=9))
        runtime = snap["runtime"]
        self.assertTrue(runtime["auto"])
        self.assertTrue(runtime["likes_enabled"])
        self.assertEqual(runtime["round"], 4)
        self.assertEqual(runtime["likes_used"], 2)
        self.assertEqual(runtime["likes_limit"], 3)
        self.assertEqual(runtime["shares_used"], 1)
        self.assertEqual(runtime["shares_limit"], 5)
        self.assertEqual(runtime["asr_used"], 1)
        self.assertEqual(runtime["asr_limit"], 9)
        self.assertEqual(runtime["quiet_start"], 23)
        self.assertEqual(runtime["quiet_end"], 9)
        self.assertEqual(runtime["share_cooldown_minutes"], 120)
        # 只留错误类型，原文（含凭证）一个字都不许带出去
        self.assertEqual(runtime["last_error_type"], "TimeoutError")
        self.assertNotIn("token", json.dumps(snap, ensure_ascii=False))

    def test_runtime_without_kv_store(self):
        snap = self._snap(kv=None)
        self.assertEqual(snap["runtime"]["round"], 0)
        self.assertEqual(snap["runtime"]["next_wake"], "")

    # ---- 只读 ----------------------------------------------------------
    def test_snapshot_writes_nothing(self):
        self._reading("BV1")
        self.store.add_reflection(kind="view", topic="t", old_view="a",
                                  new_view="b", journal_id="j4", refs={})
        self.store.bump_interest(SCOPE, "x", 1.0)
        self.store.add_proposal("p-1", SCOPE, "BV1")

        def fingerprint():
            db = self.store.db
            tables = [r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            counts = {name: db.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                      for name in tables}
            return counts, db.total_changes, db.execute("PRAGMA user_version").fetchone()[0]

        before = fingerprint()
        for _ in range(3):
            self._snap()
        after = fingerprint()
        self.assertEqual(before, after)

    # ---- 不抛 ----------------------------------------------------------
    def test_no_scope_returns_empty_with_note(self):
        self._reading("BV1")
        snap = self._facade().snapshot({})
        self.assertEqual(snap["readings"], [])
        self.assertTrue(any("scope" in note for note in snap["notes"]))

    def test_garbage_query_never_raises(self):
        for garbage in (None, "text", 42, {"max_readings": "多" * 400}, {"scope": SCOPE,
                                                                        "max_readings": -5}):
            snap = self._facade().snapshot(garbage)
            self.assertIsInstance(snap, dict)
            self.assertEqual(snap["version"], rp.SNAPSHOT_VERSION)

    def test_limits_are_clamped(self):
        for index in range(6):
            self._reading(f"BV{index}")
        self.assertEqual(len(self._snap(max_readings=2)["readings"]), 2)
        self.assertEqual(len(self._snap(max_readings=999)["readings"]), 6)
        self.assertEqual(len(self._snap(max_readings=-1)["readings"]), 1)

    def test_missing_store_returns_note(self):
        plugin = FakePlugin(None, kv=FakeKV(), config=_config())
        snap = rp.BiliReadFacade(plugin).snapshot({"scope": SCOPE})
        self.assertEqual(snap["readings"], [])
        self.assertTrue(snap["notes"])

    # ---- 静态边界 ------------------------------------------------------
    def test_module_is_offline_and_write_free(self):
        source = (PLUGIN / "read_port.py").read_text(encoding="utf-8")
        for forbidden in ("sqlite3", "aiohttp", "httpx", "requests", "subprocess",
                          "os.remove", "shutil", "urlopen", "socket"):
            self.assertNotIn(forbidden, source, f"只读桥里不该出现 {forbidden}")

    def test_facade_only_exposes_snapshot(self):
        facade = self._facade()
        public = [name for name in dir(facade) if not name.startswith("_")]
        self.assertEqual(public, ["plugin", "snapshot"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
