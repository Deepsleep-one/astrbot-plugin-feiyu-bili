"""Offline contracts for the remote ASR executor (owner's desktop behind a tunnel).

The plugin must send identifiers only, must not download audio itself when a remote
endpoint is configured, and must report a dead endpoint instead of quietly running a
second model here.
"""
from __future__ import annotations

import asyncio
import json
import shutil
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

GOOD_SEGMENTS = [{"from_ms": i * 70000, "to_ms": i * 70000 + 68000,
                  "text": "泵站的进出水主管道要按设计标高排布并留出检修空间，这一段还讲了检修阀门的位置，第%d段。" % i}
                 for i in range(8)]
WEAK_SEGMENTS = [{"from_ms": 0, "to_ms": 900, "text": "嗯"},
                 {"from_ms": 900, "to_ms": 1800, "text": "啊"}]


class FakeContent:
    def __init__(self, body: bytes):
        self.body = body

    async def read(self, n: int = -1):
        return self.body[:n] if n and n > 0 else self.body


class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.content = FakeContent(body)
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return json.loads(self._body.decode("utf-8"))


class FakeSession:
    """Records what the plugin sent and replays scripted answers."""

    def __init__(self, health, answer):
        self.health = health
        self.answer = answer
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.gets.append((url, headers))
        if isinstance(self.health, Exception):
            raise self.health
        return FakeResponse(self.health[0], self.health[1])

    def post(self, url, json=None, headers=None, allow_redirects=True):
        self.posts.append({"url": url, "body": json, "headers": headers})
        if isinstance(self.answer, Exception):
            raise self.answer
        return FakeResponse(self.answer[0], self.answer[1])


def ok_body(segments=GOOD_SEGMENTS, duration_ms=600000):
    return json.dumps({"ok": True, "segments": segments, "duration_ms": duration_ms,
                       "language": "zh", "engine": "faster-whisper", "model": "small"},
                      ensure_ascii=False).encode("utf-8")


class RemoteAsrTests(unittest.IsolatedAsyncioTestCase):
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
        self.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="{}"))
        self.instance = plugin.FeiyuBiliPlugin(self.context, {})
        self.instance.config = {"data_dir": str(self.run_path), "asr_endpoint": "http://127.0.0.1:18081"}
        self.instance.store = self.store
        self.instance.rstore = self.rstore
        self.instance.client = support.Mock()
        self.instance.client.detail = AsyncMock(return_value={
            "title": "老城区排水管网", "owner": "某UP主", "duration": 600, "cid": 101, "pages": 1,
            "parts": [{"page": 1, "cid": 101, "part_title": "P1 管网", "duration": 600}],
            "aid": 42})
        self.instance.client.subtitle = AsyncMock(return_value={
            "text": "", "source": "none", "reason": "empty_subtitle", "lang": "",
            "segments": [], "chars": 0})
        self.instance.client.audio = AsyncMock(side_effect=AssertionError("remote must not download audio"))
        self.instance.media = support.Mock()
        self.instance.media.extract_audio = AsyncMock(side_effect=AssertionError("remote must not decode"))
        self.instance.browser = Mock(lock=asyncio.Lock(), run=AsyncMock(return_value={}))
        self.sessions = []

    async def asyncTearDown(self):
        self.instance.rstore.close()
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        shutil.rmtree(resolved, ignore_errors=True)

    def mock_http(self, health, answer):
        session = FakeSession(health, answer)
        self.sessions.append(session)

        def factory(**kwargs):
            return session

        fake = SimpleNamespace(ClientSession=factory, ClientTimeout=lambda **kw: None)
        item = patch.object(plugin, "_http", return_value=fake)
        item.start()
        self.addCleanup(item.stop)
        return session

    async def transcribe(self):
        reading, _, _, _ = await self.instance._open_part(support.Event(), BVID, 0, False)
        outcome = await self.instance._transcribe(str(support.Event().unified_msg_origin), reading,
                                                  explicit=True, automatic=False,
                                                  subtitle_reason="empty_subtitle")
        return reading, outcome

    def health_ok(self):
        return (200, json.dumps({"ok": True, "engine": "faster-whisper", "model": "small"}).encode())

    async def test_identifiers_only_and_answer_is_stored(self):
        session = self.mock_http(self.health_ok(), (200, ok_body()))
        reading, outcome = await self.transcribe()
        self.assertTrue(outcome["ok"], outcome)
        self.assertIn("泵站", outcome["text"])
        self.assertEqual(len(session.posts), 1)
        sent = session.posts[0]
        self.assertTrue(sent["url"].endswith("/transcribe"))
        self.assertEqual(sent["body"]["bvid"], BVID)
        self.assertEqual(sent["body"]["cid"], 101)
        flat = json.dumps(sent["body"])
        self.assertNotIn("audio", flat)
        self.assertNotIn("path", flat)
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)
        row = self.rstore.reading(reading["reading_id"], BINDING["umo"])
        self.assertEqual(row["evidence"], "asr_transcribed")

    async def test_no_local_audio_work_happens(self):
        self.mock_http(self.health_ok(), (200, ok_body()))
        await self.transcribe()
        self.instance.client.audio.assert_not_called()
        self.instance.media.extract_audio.assert_not_called()

    async def test_dead_endpoint_is_reported_and_costs_nothing(self):
        self.mock_http(OSError("refused"), (200, ok_body()))
        _, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "remote_missing")
        self.assertIn("连不上", outcome["note"])
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 0)

    async def test_busy_endpoint_is_counted_as_a_failed_run(self):
        session = self.mock_http(self.health_ok(), (429, b'{"ok": false, "reason": "busy"}'))
        _, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "remote_busy")
        self.assertEqual(self.rstore.asr_used(BINDING["umo"], asr.day_key()), 1)
        self.assertEqual(len(session.posts), 1)

    async def test_garbage_answer_is_not_success(self):
        self.mock_http(self.health_ok(), (200, b"<html>502</html>"))
        _, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "remote_bad_response")

    async def test_weak_remote_answer_cannot_support_a_like(self):
        self.mock_http(self.health_ok(), (200, ok_body(WEAK_SEGMENTS, 2000)))
        reading, outcome = await self.transcribe()
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["like_ok"])
        row = self.rstore.reading(reading["reading_id"], BINDING["umo"])
        self.assertEqual(row["evidence"], "asr_weak")

    async def test_remote_answer_over_the_duration_cap_is_refused(self):
        self.mock_http(self.health_ok(), (200, ok_body(duration_ms=1800 * 1000)))
        _, outcome = await self.transcribe()
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "decoded_too_long")


if __name__ == "__main__":
    unittest.main()
