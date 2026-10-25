"""Offline contracts for the local ASR path: eligibility, budget, cleanup, likes.

Nothing here reaches the network, downloads weights or likes a real video: the
worker, ffmpeg and the B站 client are all replaced by test doubles. A pass here
means the *bookkeeping* is right, not that the real engine works on a real host.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import test_plugin as support

plugin, core = support.plugin, support.core
BVID = support.BVID
BINDING = support.BINDING
OWNER = support.OWNER
ReadOnlyMemoryDB = support.ReadOnlyMemoryDB
asr = plugin.asr

FAKE_WORKER = '''\
import json, sys, time
argv = sys.argv[1:]
out = argv[argv.index("--out") + 1]
mode = argv[argv.index("--mode") + 1] if "--mode" in argv else "ok"
if mode == "sleep":
    time.sleep(30)
if mode == "weak":
    segments = [{"from_ms": 0, "to_ms": 1000, "text": "嗯"},
                {"from_ms": 1000, "to_ms": 2000, "text": "啊"}]
else:
    base = "泵站的进出水主管道要按设计标高排布并留出检修空间，这一段还讲了检修阀门的位置"
    segments = [{"from_ms": i * 70000, "to_ms": i * 70000 + 68000,
                 "text": base + "，第%d段。" % i} for i in range(8)]
payload = {"ok": True, "segments": segments, "duration_ms": 600000,
           "language": "zh", "engine": "faster-whisper", "model": "small"}
open(out, "w", encoding="utf-8").write(json.dumps(payload, ensure_ascii=False))
print(json.dumps(payload, ensure_ascii=False))
'''

DECISION = json.dumps({"interested": True, "like": True, "share": True,
                       "note": "讲清楚了标高和检修空间", "message": "这条讲排水管网，你看不看"})


class FakeClient:
    """Read-only B站 double; the like endpoint counts calls but never sends anything."""

    def __init__(self, *, duration=600, subtitle=None):
        self.duration = duration
        self.subtitle_payload = subtitle if subtitle is not None else {
            "text": "", "source": "none", "reason": "empty_subtitle", "lang": "",
            "segments": [], "chars": 0}
        self.audio_calls = []
        self.downloads = []
        self.like_calls = []

    async def detail(self, bvid):
        return {"bvid": bvid, "title": "老城区排水管网", "owner": "某UP主",
                "duration": self.duration, "cid": 101, "pages": 1,
                "parts": [{"page": 1, "cid": 101, "part_title": "P1 管网",
                           "duration": self.duration}]}

    async def subtitle(self, bvid, cid=None):
        return dict(self.subtitle_payload)

    async def search(self, keyword, limit=5, **kwargs):
        return [{"bvid": BVID, "title": "老城区排水管网", "author": "某UP主",
                 "description": "讲排水", "url": ""}]

    async def audio(self, bvid, cid):
        self.audio_calls.append((bvid, cid))
        return {"bvid": bvid, "cid": cid, "kind": "dash_audio",
                "urls": ["https://cdn.example.invalid/audio.m4s"], "bandwidth": 64000}

    async def download_media(self, url, dest, *, max_bytes, timeout=None):
        self.downloads.append(url)
        Path(dest).write_bytes(b"\0" * 2048)
        return {"bytes": 2048, "sha256": "0" * 64}

    async def like(self, bvid):
        self.like_calls.append(bvid)
        return {"status": "liked"}

    async def close(self):
        return None


class AsrPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.run_root = Path(__file__).resolve().parent / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "browse.db")
        self.rstore = plugin.ReadingStore(self.run_path / "browse.db")
        self.store.set("binding", dict(BINDING))
        self.db = Mock(spec_set=ReadOnlyMemoryDB)
        self.db.get_owner.return_value = dict(OWNER)
        self.db.build_context.return_value = {"state": "", "memory": ""}
        self.memory = Mock(spec_set=["_identity", "_safe_store", "_persona"])
        self.memory._identity.side_effect = lambda event: dict(event.identity)
        self.memory._safe_store.return_value = self.db
        self.memory._persona.return_value = "现有人格，不是通用助手"
        self.meta = SimpleNamespace(name="astrbot_plugin_feiyu_memory", activated=True,
                                    star_cls=self.memory)
        self.context = Mock(spec_set=["get_all_stars", "persona_manager", "send_message",
                                     "get_current_chat_provider_id", "llm_generate"])
        self.context.get_all_stars.return_value = [self.meta]
        self.context.persona_manager = None
        self.context.send_message = AsyncMock(return_value=True)
        self.context.get_current_chat_provider_id = AsyncMock(return_value="current-chat-model")
        self.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text=DECISION))
        self.instance = plugin.FeiyuBiliPlugin(self.context, {})
        self.instance.config = {"data_dir": str(self.run_path)}
        self.instance.store = self.store
        self.instance.rstore = self.rstore
        self.instance.client = FakeClient()
        self.instance.media = plugin.MediaWorker(self.run_path / "media")
        self.instance.media.extract_audio = AsyncMock(return_value={"ok": True, "reason": "",
                                                                   "seconds": 600.0})
        self.instance.browser = Mock(lock=asyncio.Lock(),
                                     run=AsyncMock(return_value={"status": "ok", "read": 0,
                                                                 "sent": 0}))
        self.worker = self.run_path / "fake_worker.py"
        self.worker.write_text(FAKE_WORKER, encoding="utf-8")
        self.mode = "ok"
        self.probe_patch = patch.object(asr, "probe", return_value={"available": True,
                                                                   "engine": "faster-whisper"})
        self.args_patch = patch.object(asr, "worker_args", side_effect=self.fake_args)
        self.probe_patch.start()
        self.args_patch.start()

    async def asyncTearDown(self):
        self.args_patch.stop()
        self.probe_patch.stop()
        self.instance.rstore.close()
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        shutil.rmtree(resolved, ignore_errors=True)

    def fake_args(self, *, audio, out, model, compute_type, device, threads, language,
                  model_dir=""):
        return [sys.executable, "-B", str(self.worker), "--out", str(out),
                "--mode", self.mode, "--audio", str(audio)]

    async def read(self, video=BVID):
        reading, first, _, _ = await self.instance._open_part(support.Event(), video, 0, False)
        return reading, first

    async def transcribe(self, video=BVID):
        reading, _, _, _ = await self.instance._open_part(support.Event(), video, 0, False)
        outcome = await self.instance._transcribe(str(support.Event().unified_msg_origin), reading,
                                                  explicit=True, automatic=False,
                                                  subtitle_reason="empty_subtitle")
        return reading, outcome

    def job_dir(self, job_id):
        return self.instance._asr_root() / job_id

    # ---- eligibility and the explicit path -----------------------------
    async def test_explicit_read_without_subtitle_transcribes_then_deletes_audio(self):
        reading, outcome = await self.transcribe()
        self.assertTrue(outcome["ok"])
        self.assertIn("泵站", outcome["text"])
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)
        self.assertEqual(self.instance.client.audio_calls, [(BVID, 101)])
        row = self.rstore.reading(reading["reading_id"], BINDING["umo"])
        self.assertEqual(row["evidence"], "asr_transcribed")
        self.assertFalse(self.job_dir(outcome["job_id"]).exists())
        # The audio never survives the call, and it was never written outside the job dir.
        self.assertEqual(list((self.run_path / "media").rglob("*.m4s")), [])
        self.assertEqual(list((self.run_path / "media").rglob("*.wav")), [])

    async def test_read_with_subtitle_never_spends_a_slot(self):
        self.instance.client.subtitle_payload = {
            "text": "这一P有官方字幕", "source": "bilibili_subtitle:part", "reason": "ok",
            "lang": "zh", "segments": [{"from_ms": 0, "to_ms": 3000, "text": "有字幕"}],
            "chars": 7}
        await self.read()
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 0)
        self.assertEqual(self.instance.client.audio_calls, [])

    async def test_clip_of_thirty_minutes_or_more_is_skipped_whole(self):
        for duration in (1800, 3600):
            with self.subTest(duration=duration):
                self.instance.client.duration = duration
                _, _, _, transcript = await self.instance._open_part(support.Event(), BVID, 0, False)
                self.assertFalse(transcript["ok"])
                self.assertEqual(transcript["reason"], "too_long")
                self.assertIn("整条跳过", transcript["note"])
        self.assertEqual(self.instance.client.audio_calls, [])
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 0)

    async def test_just_under_thirty_minutes_is_allowed(self):
        self.instance.client.duration = 1799
        _, outcome = await self.transcribe()
        self.assertTrue(outcome["ok"])

    async def test_declared_length_over_the_cap_never_downloads(self):
        self.instance.client.duration = 2400
        _, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "too_long")
        self.assertEqual(self.instance.client.downloads, [])

    async def test_sixth_request_is_refused_and_stays_at_five(self):
        day = asr.day_key()
        for _ in range(5):
            self.assertTrue(self.rstore.reserve_asr(BINDING["umo"], day, 5))
        _, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "budget_exhausted")
        self.assertIn("额度", outcome["note"])
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], day), 5)
        self.assertEqual(self.instance.client.audio_calls, [])

    async def test_reentry_of_the_same_part_does_not_spend_twice(self):
        _, first = await self.transcribe()
        reading = self.rstore.latest_reading(BINDING["umo"])
        second = await self.instance._transcribe(BINDING["umo"], reading, explicit=True,
                                                 automatic=False,
                                                 subtitle_reason="empty_subtitle")
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)
        self.assertEqual(len(self.instance.client.audio_calls), 1)

    async def test_scheduled_background_rounds_cannot_start_asr(self):
        self.instance.config["asr_background"] = True
        outcome = await self.instance._transcribe_candidate(
            BVID, {"bvid": BVID, "title": "t", "author": "a"}, True, "empty_subtitle")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "automatic_task")
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 0)
        self.assertEqual(self.instance.client.audio_calls, [])

    async def test_subtitle_refusal_is_not_rerouted_through_audio(self):
        reading = self.rstore.begin_reading(reading_id=plugin.new_id("rd"), bvid=BVID, page=1,
                                            cid=101, revision=1, scope=BINDING["umo"],
                                            title="t", author="a", part_title="P1",
                                            duration=600, fetched_at=plugin.clock_now(),
                                            source_type="bilibili", subtitle_state="none",
                                            subtitle_reason="risk_control", evidence="metadata_only")
        outcome = await self.instance._transcribe(BINDING["umo"], reading, explicit=True,
                                                  automatic=False,
                                                  subtitle_reason="risk_control")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "subtitle_blocked")
        self.assertEqual(self.instance.client.audio_calls, [])

    # ---- durable budget ------------------------------------------------
    async def test_counts_survive_a_restart_and_reset_on_a_new_day(self):
        day = asr.day_key()
        self.assertTrue(self.rstore.reserve_asr(BINDING["umo"], day, 5))
        self.assertTrue(self.rstore.reserve_asr(BINDING["umo"], day, 5))
        reopened = plugin.ReadingStore(self.run_path / "browse.db")
        try:
            self.assertEqual(reopened.asr_used(BINDING["umo"], day), 2)
            self.assertEqual(reopened.asr_used(BINDING["umo"], "2000-01-01"), 0)
        finally:
            reopened.close()

    async def test_concurrent_reservations_cannot_exceed_the_cap(self):
        day = asr.day_key()
        allowed = [self.rstore.reserve_asr(BINDING["umo"], day, 5) for _ in range(7)]
        self.assertEqual(allowed, [True] * 5 + [False] * 2)
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], day), 5)

    async def test_budget_is_per_conversation(self):
        day = asr.day_key()
        for _ in range(5):
            self.rstore.reserve_asr(BINDING["umo"], day, 5)
        self.assertTrue(self.rstore.reserve_asr("qq:private:999", day, 5))

    # ---- failures, timeouts, cancellation ------------------------------
    async def test_worker_timeout_is_counted_and_cleaned_up(self):
        self.mode = "sleep"
        self.instance.config["asr_worker_timeout_s"] = 0.4
        reading, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "worker_timeout")
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)
        job = self.rstore.asr_job(outcome["job_id"])
        self.assertEqual(job["reason"], "worker_timeout")
        self.assertFalse(self.job_dir(outcome["job_id"]).exists())

    async def test_total_deadline_stops_the_whole_task(self):
        # The per-stage timeouts are generous on purpose; one overall deadline still caps
        # download + decode + inference together, and it is counted like any other attempt.
        self.mode = "sleep"
        self.instance.config["asr_worker_timeout_s"] = 30
        self.instance.config["asr_total_timeout_s"] = 0.4
        reading, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "total_timeout")
        job = self.rstore.asr_job(outcome["job_id"])
        self.assertEqual(job["state"], asr.STATE_TIMEOUT)
        self.assertFalse(self.job_dir(outcome["job_id"]).exists())
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)

    async def test_cancellation_marks_cancelled_and_cleans_up(self):
        self.mode = "sleep"
        self.instance.config["asr_worker_timeout_s"] = 30
        # Build the reading directly: going through _open_part would already have
        # transcribed it once, and the second call would just replay the cached job.
        reading = self.rstore.begin_reading(
            reading_id=plugin.new_id("rd"), bvid=BVID, page=1, cid=101,
            scope=BINDING["umo"], title="老城区排水管网", author="某UP主",
            part_title="P1 管网", duration=1799, fetched_at=plugin.clock_now(),
            source_type="bilibili_video", subtitle_state="none",
            subtitle_reason="empty_subtitle", evidence="metadata_only")
        task = asyncio.ensure_future(self.instance._transcribe(
            BINDING["umo"], reading, explicit=True, automatic=False,
            subtitle_reason="empty_subtitle"))
        await asyncio.sleep(0.6)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        job = self.rstore.asr_job_for(reading["reading_id"], reading["revision"])
        self.assertEqual(job["state"], asr.STATE_CANCELLED)
        self.assertFalse(self.job_dir(job["job_id"]).exists())
        # A paid-for attempt is still a used attempt.
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)

    async def test_leftover_directories_are_removed_at_startup(self):
        root = self.instance._asr_root()
        (root / "asr-deadbeef").mkdir(parents=True, exist_ok=True)
        (root / "asr-deadbeef" / "audio.m4s").write_bytes(b"x")
        outside = self.run_path / "keepme"
        outside.mkdir()
        removed = self.instance._cleanup_asr()
        self.assertEqual(removed, 1)
        self.assertFalse((root / "asr-deadbeef").exists())
        self.assertTrue(outside.exists())

    async def test_weak_transcript_is_labelled_and_cannot_be_used(self):
        self.mode = "weak"
        reading, outcome = await self.transcribe()
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["like_ok"])
        self.assertIn("证据不足", self.instance._format_asr(reading, outcome))
        row = self.rstore.reading(reading["reading_id"], BINDING["umo"])
        self.assertEqual(row["evidence"], "asr_weak")

    # ---- the like gate in the round itself -----------------------------
    def browser(self, like_ok=True, text="机器听的内容：他说泵站按标高排"):
        callback = AsyncMock(return_value={"ok": True, "text": text, "like_ok": like_ok,
                                           "block": "机器转写片段", "reason": ""})
        browser = core.Browser(self.store, self.instance.client,
                              llm=AsyncMock(return_value=DECISION),
                              sender=AsyncMock(return_value=True),
                              cfg={"topics": ["排水"], "videos_per_round": 1,
                                   "daily_likes": 3, "daily_rounds": 5},
                              transcribe=callback)
        browser.can_like = lambda automatic: True
        browser.can_send = lambda automatic: True
        return browser, callback

    async def test_qualified_transcript_may_support_a_like(self):
        browser, callback = self.browser(like_ok=True)
        result = await browser.run(target="排水", system="sys", memory="", topic="排水")
        callback.assert_awaited_once()
        self.assertEqual(result["asr_used"], 1)
        self.assertEqual(self.instance.client.like_calls, [BVID])

    async def test_weak_transcript_never_buys_a_like(self):
        browser, callback = self.browser(like_ok=False)
        result = await browser.run(target="排水", system="sys", memory="", topic="排水")
        callback.assert_awaited_once()
        self.assertEqual(result["liked"], 0)
        self.assertEqual(self.instance.client.like_calls, [])

    async def test_transcript_never_buys_sharing(self):
        browser, _ = self.browser(like_ok=True)
        result = await browser.run(target="排水", system="sys", memory="", topic="排水")
        self.assertEqual(result["sent"], 0)
        self.assertEqual(browser.sender.await_count, 0)

    async def test_failed_transcript_leaves_the_round_without_text(self):
        browser, _ = self.browser(text="")
        browser.transcribe = AsyncMock(return_value={"ok": False, "text": "", "like_ok": False,
                                                     "reason": "worker_timeout"})
        result = await browser.run(target="排水", system="sys", memory="", topic="排水")
        self.assertEqual(result["asr_failed"], 1)
        self.assertEqual(result["asr_reason"], "worker_timeout")
        self.assertEqual(result["liked"], 0)
        self.assertEqual(self.instance.client.like_calls, [])


if __name__ == "__main__":
    unittest.main()
