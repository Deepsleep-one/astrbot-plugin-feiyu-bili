"""Offline plugin integration contracts, using explicit minimal AstrBot stubs.

Run: python -B -m unittest discover -s 05_B站浏览/tests -p test_plugin.py -v
Only this test module installs stubs, and only while importing main.py. The
memory facade exposes reads only: any attempted memory mutation fails the test.
"""
from __future__ import annotations

import asyncio
import json
import importlib.util
import shutil
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch
from uuid import uuid4


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugin" / "astrbot_plugin_feiyu_bili"


def load_plugin():
    package_name = "_feiyu_bili_plugin_offline_test"
    modules = {}
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
                 "astrbot.core", "astrbot.core.star", "astrbot.core.star.filter",
                 "astrbot.core.star.filter.command", package_name):
        module = types.ModuleType(name)
        module.__path__ = []
        modules[name] = module
    modules[package_name].__path__ = [str(PLUGIN_DIR)]

    def decorator(*args, **kwargs):
        return lambda decorated: decorated

    class Star:
        def __init__(self, context):
            self.context = context

    class MessageChain:
        def message(self, text):
            self.text = text
            return self

    modules["astrbot.api"].logger = Mock()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = MessageChain
    modules["astrbot.api.event"].filter = SimpleNamespace(command=decorator, on_llm_request=decorator, llm_tool=decorator)
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    modules["astrbot.api.star"].register = decorator
    modules["astrbot.core.star.filter.command"].GreedyStr = str
    client_module = types.ModuleType(package_name + ".bili_client")
    client_module.BiliClient = Mock(name="OfflineBiliClient")
    modules[client_module.__name__] = client_module
    # Register the real core under the synthetic package without executing the
    # plugin package initializer or importing aiohttp/AstrBot.
    core_spec = importlib.util.spec_from_file_location(package_name + ".core", PLUGIN_DIR / "core.py")
    assert core_spec is not None and core_spec.loader is not None
    core_module = importlib.util.module_from_spec(core_spec)
    core_spec.loader.exec_module(core_module)
    modules[core_spec.name] = core_module
    spec = importlib.util.spec_from_file_location(package_name + ".main", PLUGIN_DIR / "main.py")
    assert spec is not None and spec.loader is not None
    plugin_module = importlib.util.module_from_spec(spec)
    modules[spec.name] = plugin_module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin_module)
    return plugin_module, core_module


plugin, core = load_plugin()
BVID = "BV0000000001"
OWNER = {"owner_id": "primary", "owner_label": "主人"}
BINDING = {"platform": "qq", "user_id": "12345", "bot_instance": "bot-1", "umo": "qq:private:12345"}


class ReadOnlyMemoryDB:
    """Spec deliberately excludes every write/connection/raw-SQL operation."""
    def get_owner(self, platform, user_id, bot_instance):
        raise NotImplementedError

    def build_context(self, **kwargs):
        raise NotImplementedError


class Event:
    def __init__(self, *, owner=True, scene="private", text="B站视频", umo=None):
        self.identity = {"is_owner": owner, "scene": scene, "owner": OWNER if owner else None,
                         "platform": "qq", "user_id": "12345", "bot_instance": "bot-1"}
        self.unified_msg_origin = umo or BINDING["umo"]
        self.text = text

    def plain_result(self, text):
        return text

    def get_message_str(self):
        return self.text


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.run_root = Path(__file__).resolve().parent / ".runs"
        self.run_path = self.run_root / uuid4().hex
        self.run_path.mkdir(parents=True)
        self.store = core.Store(self.run_path / "browse.db")
        self.db = Mock(spec_set=ReadOnlyMemoryDB)
        self.db.get_owner.return_value = dict(OWNER)
        self.db.build_context.return_value = {"state": "[已核验的主人私聊状态]", "memory": "只读兴趣参考"}
        self.memory = Mock(spec_set=["_identity", "_safe_store", "_persona"])
        self.memory._identity.side_effect = lambda event: dict(event.identity)
        self.memory._safe_store.return_value = self.db
        self.memory._persona.return_value = "现有人格，不是通用助手"
        self.meta = SimpleNamespace(name="astrbot_plugin_feiyu_memory", activated=True, star_cls=self.memory)
        self.context = Mock(spec_set=["get_all_stars", "persona_manager", "send_message",
                                     "get_current_chat_provider_id", "llm_generate"])
        self.context.get_all_stars.return_value = [self.meta]
        self.context.persona_manager = None
        self.context.send_message = AsyncMock(return_value=True)
        self.context.get_current_chat_provider_id = AsyncMock(return_value="current-chat-model")
        self.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text=" {} "))
        self.instance = plugin.FeiyuBiliPlugin(self.context, {})
        self.instance.store = self.store
        self.instance.browser = Mock()
        self.instance.browser.lock = asyncio.Lock()
        self.instance.browser.run = AsyncMock(return_value={"status": "ok", "read": 1, "sent": 0})
        self.instance.client = Mock()
        self.instance.client.close = AsyncMock()
        self.tasks = []

    async def asyncTearDown(self):
        if self.instance.task is not None:
            self.tasks.append(self.instance.task)
        for task in set(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*set(self.tasks), return_exceptions=True)
        # Fail if the integration attempted any operation except these reads.
        self.assertTrue(all(c[0] in {"get_owner", "build_context"} for c in self.db.mock_calls))
        if getattr(self.instance, "rstore", None) is not None:
            self.instance.rstore.close()
            self.instance.rstore = None
        self.store.close()
        resolved = self.run_path.resolve()
        self.assertEqual(resolved.parent, self.run_root.resolve())
        self.assertEqual(len(resolved.name), 32)
        shutil.rmtree(resolved)

    async def command(self, action, event=None):
        return [text async for text in self.instance.command(event or Event(), action)]

    def bind(self):
        self.store.set("binding", dict(BINDING))

    def add_history(self, title="离线标题", note="私人浏览笔记"):
        self.store.remember({"bvid": BVID, "title": title}, "topic", "subtitle",
                            {"note": note, "interested": True})

    async def test_all_commands_reject_nonowner_before_side_effects(self):
        for action in ("", "绑定", "开启", "关闭", "点赞开启", "点赞关闭", "刷 历史", "试刷", "状态", "记录", "点赞记录"):
            with self.subTest(action=action):
                result = await self.command(action, Event(owner=False))
                self.assertEqual(result, ["这个功能只在已绑定的主人私聊开放。"])
        self.assertIsNone(self.store.get("binding"))
        self.assertIsNone(self.store.get("auto"))
        self.instance.browser.run.assert_not_awaited()
        self.db.get_owner.assert_not_called()
        self.context.send_message.assert_not_awaited()

    async def test_all_commands_reject_owner_in_group(self):
        for action in ("绑定", "开启", "关闭", "点赞开启", "点赞关闭", "刷", "试刷", "状态", "记录", "点赞记录"):
            with self.subTest(action=action):
                self.assertEqual(await self.command(action, Event(scene="group")),
                                 ["这个功能只在已绑定的主人私聊开放。"])
        self.instance.browser.run.assert_not_awaited()
        self.assertIsNone(self.store.get("binding"))
        self.context.send_message.assert_not_awaited()

    async def test_missing_disabled_or_unloaded_memory_plugin_fails_closed(self):
        for stars in ([], [SimpleNamespace(name=self.meta.name, activated=False, star_cls=self.memory)],
                      [SimpleNamespace(name=self.meta.name, activated=True, star_cls=None)]):
            with self.subTest(stars=stars):
                self.context.get_all_stars.return_value = stars
                self.assertEqual(await self.command("绑定"), ["主人记忆插件未加载，拒绝浏览/发送"])
        self.assertIsNone(self.store.get("binding"))

    async def test_bind_records_verified_identity_and_forces_auto_false(self):
        self.store.set("auto", True)
        result = await self.command("绑定")
        self.assertIn("接收会话绑好了", result[0])
        self.assertEqual(self.store.get("binding"), BINDING)
        self.assertIs(self.store.get("auto"), False)
        self.instance.browser.run.assert_not_awaited()
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.db.mock_calls, [])

    async def test_cannot_rebind_while_browser_busy(self):
        self.bind()
        async with self.instance.browser.lock:
            result = await self.command("绑定", Event(umo="qq:private:other-session"))
        self.assertIn("等这一轮结束", result[0])
        self.assertEqual(self.store.get("binding"), BINDING)

    async def test_enable_requires_current_owner_binding(self):
        self.assertEqual(await self.command("开启"), ["先在QQ私聊发送 /刷站 绑定"])
        self.bind()
        self.db.get_owner.return_value = None
        self.assertEqual(await self.command("开启"), ["主人绑定已失效，自动投递已停止"])
        self.assertIsNone(self.store.get("auto"))

    async def test_enable_respects_plugin_master_switch(self):
        self.bind()
        self.instance.config["enabled"] = False
        self.assertIn("总开关目前关闭", (await self.command("开启"))[0])
        self.assertIsNone(self.store.get("auto"))

    async def test_enable_only_writes_browsing_settings_not_memory(self):
        self.bind()
        self.assertIn("本鱼会自己逛了", (await self.command("开启"))[0])
        self.assertIsNotNone(self.store.get("next_wake"))
        self.assertIs(self.store.get("auto"), True)
        self.assertEqual(self.db.mock_calls, [call.get_owner("qq", "12345", "bot-1")])

    async def test_stop_cancels_inflight_automatic_task_before_ack(self):
        self.store.set("auto", True)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        hold = asyncio.Event()

        async def automatic_work():
            started.set()
            try:
                await hold.wait()
            finally:
                cancelled.set()

        first = asyncio.create_task(automatic_work())
        self.tasks.append(first)
        self.instance.task = first
        await asyncio.wait_for(started.wait(), timeout=2)
        # Replacement loop stays idle and does no real sleeping or browsing.
        self.instance._loop = AsyncMock(side_effect=hold.wait)
        response = await self.command("关闭")
        self.assertIn("当前在途浏览也已停止", response[0])
        self.assertTrue(cancelled.is_set())
        self.assertTrue(first.cancelled())
        self.assertIs(self.store.get("auto"), False)
        self.assertIsNot(self.instance.task, first)
        self.assertFalse(self.instance.task.done())
        self.instance.browser.run.assert_not_awaited()

    async def test_stop_without_background_task_is_safe(self):
        self.store.set("auto", True)
        self.assertIn("自主刷站关了", (await self.command("关闭"))[0])
        self.assertIs(self.store.get("auto"), False)
        self.assertIsNotNone(self.instance.task)
        self.assertFalse(self.instance.task.done())

    async def test_send_requires_exact_bound_origin(self):
        self.bind()
        for target in ("qq:group:12345", "qq:private:other", "", BINDING["umo"] + "\n"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(RuntimeError, "接收会话不一致"):
                    await self.instance._send(target, "message")
        self.context.send_message.assert_not_awaited()

    async def test_send_rechecks_revoked_owner(self):
        self.bind()
        self.db.get_owner.return_value = None
        with self.assertRaisesRegex(RuntimeError, "主人绑定已失效"):
            await self.instance._send(BINDING["umo"], "message")
        self.context.send_message.assert_not_awaited()

    async def test_send_missing_memory_store_fails_closed(self):
        self.bind()
        self.memory._safe_store.return_value = None
        with self.assertRaisesRegex(RuntimeError, "主人绑定已失效"):
            await self.instance._send(BINDING["umo"], "message")
        self.context.send_message.assert_not_awaited()

    async def test_send_uses_message_chain_and_preserves_false_receipt(self):
        self.bind()
        self.context.send_message.return_value = False
        self.assertIs(await self.instance._send(BINDING["umo"], "正文"), False)
        args = self.context.send_message.await_args.args
        self.assertEqual(args[0], BINDING["umo"])
        self.assertEqual(args[1].text, "正文")
        self.db.get_owner.assert_called_once_with("qq", "12345", "bot-1")

    async def test_bundle_reads_only_verified_private_owner_memory(self):
        self.bind()
        binding, system, memory = await self.instance._bundle()
        self.assertEqual(binding, BINDING)
        self.assertEqual(system, "现有人格，不是通用助手\n\n[已核验的主人私聊状态]")
        self.assertEqual(memory, "只读兴趣参考")
        self.assertEqual(self.db.get_owner.call_args_list,
                         [call("qq", "12345", "bot-1"), call("qq", "12345", "bot-1")])
        self.db.build_context.assert_called_once()
        kwargs = self.db.build_context.call_args.kwargs
        self.assertIs(kwargs["is_owner"], True)
        self.assertEqual(kwargs["scene"], "private")
        self.assertEqual(kwargs["owner"], OWNER)
        self.assertEqual(kwargs["user_id"], "12345")
        self.assertEqual(kwargs["cfg"]["max_memory_chars"], 1000)
        self.assertEqual({c[0] for c in self.db.mock_calls}, {"get_owner", "build_context"})

    async def test_bundle_uses_current_session_persona_when_available(self):
        self.bind()
        mgr = SimpleNamespace(get_default_persona_v3=AsyncMock(return_value={"prompt": "当前会话人格"}))
        self.context.persona_manager = mgr
        _, system, _ = await self.instance._bundle()
        self.assertTrue(system.startswith("当前会话人格\n"))
        mgr.get_default_persona_v3.assert_awaited_once_with(BINDING["umo"])
        self.memory._persona.assert_not_called()

    async def test_missing_persona_refuses_generic_assistant_fallback(self):
        self.bind()
        self.memory._persona.return_value = ""
        with self.assertRaisesRegex(RuntimeError, "人格未能读取"):
            await self.instance._bundle()
        self.db.build_context.assert_not_called()

    async def test_llm_uses_current_provider_no_tools_and_revalidates_binding(self):
        self.bind()
        result = await self.instance._llm("人格", "资料")
        self.assertEqual(result, "{}")
        self.context.get_current_chat_provider_id.assert_awaited_once_with(umo=BINDING["umo"])
        self.context.llm_generate.assert_awaited_once_with(
            chat_provider_id="current-chat-model", system_prompt="人格", prompt="资料", tools=None)
        self.assertEqual(self.db.mock_calls, [call.get_owner("qq", "12345", "bot-1")])

    async def test_llm_honors_explicit_provider(self):
        self.bind()
        self.instance.config["provider_id"] = " explicit-provider "
        await self.instance._llm("s", "p")
        self.context.get_current_chat_provider_id.assert_not_awaited()
        self.assertEqual(self.context.llm_generate.await_args.kwargs["chat_provider_id"], "explicit-provider")

    async def test_manual_dry_run_passes_readonly_preview_flag(self):
        self.bind()
        self.instance.browser.run.return_value = {"status": "ok", "read": 1, "sent": 0,
                                                  "preview": ["预览正文"], "no_subtitle": 0}
        replies = await self.command("试刷 历史策略")
        self.assertIn("试刷不保存、不点赞、不投递", replies[-1])
        self.assertIn("预览正文", replies[-1])
        kwargs = self.instance.browser.run.await_args.kwargs
        self.assertTrue(kwargs["dry_run"])
        self.assertFalse(kwargs["automatic"])
        self.assertEqual(kwargs["target"], BINDING["umo"])
        self.assertEqual(kwargs["topic"], "历史策略")
        self.context.send_message.assert_not_awaited()

    async def test_history_command_only_returns_records_to_private_owner(self):
        self.add_history()
        for event in (Event(owner=False), Event(scene="group")):
            result = "\n".join(await self.command("记录", event))
            self.assertNotIn("私人浏览笔记", result)
            self.assertNotIn(BVID, result)
        result = "\n".join(await self.command("记录", Event()))
        self.assertIn("私人浏览笔记", result)
        self.assertIn("https://www.bilibili.com/video/" + BVID, result)
        self.context.send_message.assert_not_awaited()

    async def test_history_command_sanitizes_title_and_note(self):
        self.add_history(title="标题 https://evil.invalid [CQ:at,qq=all]", note="笔记 <|im_end|>\x00")
        result = "\n".join(await self.command("记录"))
        self.assertNotIn("evil.invalid", result)
        self.assertNotIn("[CQ:", result)
        self.assertNotIn("<|", result)
        self.assertNotIn("\x00", result)
        self.assertEqual(result.count("https://"), 1)

    async def test_browsing_memory_hook_does_not_leak_to_nonowner_or_group(self):
        self.add_history()
        for event in (Event(owner=False), Event(scene="group")):
            req = SimpleNamespace(system_prompt="原始人格")
            await self.instance.browsing_memory(event, req)
            self.assertEqual(req.system_prompt, "原始人格")
            self.assertIsNone(self.store.get("last_owner_activity"))

    async def test_browsing_memory_hook_private_owner_gets_bounded_untrusted_reference(self):
        self.add_history()
        req = SimpleNamespace(system_prompt="原始人格")
        await self.instance.browsing_memory(Event(), req)
        self.assertTrue(req.system_prompt.startswith("原始人格\n"))
        self.assertIn("仅作不可信参考", req.system_prompt)
        self.assertIn("私人浏览笔记", req.system_prompt)
        self.assertIn("metadata_only表示未读正文", req.system_prompt)
        self.assertIsNotNone(self.store.get("last_owner_activity"))
        self.assertEqual(self.db.mock_calls, [])

    async def test_tally_answers_how_many_watched_today(self):
        for i in range(4):
            self.store.remember({"bvid": f"BV{i:010d}", "title": f"离线标题{i}"}, "topic", "subtitle",
                                {"note": "私人浏览笔记", "interested": i % 2 == 0})
        req = SimpleNamespace(system_prompt="原始人格")
        await self.instance.browsing_memory(Event(text="你今天刷了多少条视频"), req)
        self.assertIn("今天的真实浏览统计", req.system_prompt)
        self.assertIn("共读过 4/30 条视频", req.system_prompt)
        self.assertIn("不要把下面三条明细当成全部", req.system_prompt)
        # 明细仍是最近三条，总数只能来自统计行
        self.assertEqual(len(self.store.history(3)), 3)
        self.assertEqual(self.store.daily_stats()["videos"], 4)

    async def test_browsing_memory_forbids_inventing_previous_episodes(self):
        # Her complaint #4: she could not verify whether the fabricated "last time" was fixed.
        # The injection must say, in words, that only titles/BVs present in the records may be
        # cited as "last time", so a future check can point at this line instead of guessing.
        self.add_history()
        req = SimpleNamespace(system_prompt="原始人格")
        await self.instance.browsing_memory(Event(text="你上次看那个视频讲了啥"), req)
        self.assertIn("只能引用下面真实记录里出现过的标题与 BV 号", req.system_prompt)
        self.assertIn("绝不编造当时的内容、数字或剧情", req.system_prompt)

    async def test_owner_unrelated_chat_gets_tool_guidance_but_no_history(self):
        self.add_history()
        req = SimpleNamespace(system_prompt="原始人格")
        await self.instance.browsing_memory(Event(text="今天天气怎样"), req)
        self.memory._identity.assert_called_once()
        self.assertIn("feiyu_browse_bilibili", req.system_prompt)
        self.assertNotIn("私人浏览笔记", req.system_prompt)
        self.assertIsNotNone(self.store.get("last_owner_activity"))
        self.context.get_all_stars.return_value = []
        req = SimpleNamespace(system_prompt="原始人格")
        await self.instance.browsing_memory(Event(), req)
        self.assertEqual(req.system_prompt, "原始人格")

    async def test_errors_do_not_echo_credentials_or_model_content(self):
        self.bind()
        self.instance._run = AsyncMock(side_effect=RuntimeError("secret-token-and-private-memory"))
        result = "\n".join(await self.command("刷"))
        self.assertNotIn("secret-token", result)
        self.assertNotIn("private-memory", result)
        self.assertIn("没有当作成功点赞/分享", result)

    async def test_initialize_installs_live_send_gate_without_enabling_auto(self):
        self.instance.config["data_dir"] = str(self.run_path)
        hold = asyncio.Event()
        self.instance._loop = AsyncMock(side_effect=hold.wait)
        with patch.object(plugin, "Store", return_value=self.store) as store_factory, \
                patch.object(plugin, "BiliClient", return_value=self.instance.client):
            await self.instance.initialize()
        store_factory.assert_called_once_with(self.run_path / "browse.db")
        self.assertIsNone(self.store.get("auto"))
        self.assertTrue(self.instance.browser.can_send(False))
        self.assertFalse(self.instance.browser.can_send(True))
        self.assertFalse(self.instance.browser.can_queue(True))
        self.assertFalse(self.instance.browser.can_like(False))
        self.bind()
        self.store.set("likes_enabled", True)
        self.store.set("auto", True)
        self.assertTrue(self.instance.browser.can_like(True))
        self.assertTrue(self.instance.browser.can_send(True))
        self.assertTrue(self.instance.browser.can_queue(True))
        self.store.set("last_owner_activity", core.now().isoformat())
        self.assertFalse(self.instance.browser.can_send(True))
        self.assertTrue(self.instance.browser.can_queue(True))
        self.store.set("auto", False)
        self.assertFalse(self.instance.browser.can_queue(True))
        self.instance.config["enabled"] = False
        self.assertFalse(self.instance.browser.can_send(False))
        self.assertFalse(self.instance.browser.can_send(True))
        self.assertFalse(self.instance.browser.can_like(False))
        self.assertFalse(self.instance.browser.can_queue(True))

    async def test_automatic_loop_delegates_to_tick_and_sleep_is_patched(self):
        self.instance._tick = AsyncMock()
        with patch.object(plugin.asyncio, "sleep", new_callable=AsyncMock,
                          side_effect=[None, asyncio.CancelledError]) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await self.instance._loop()
        self.instance._tick.assert_awaited_once_with()
        self.assertEqual(sleep.await_count, 2)
        sleep.assert_awaited_with(60)

    async def test_automatic_loop_error_backs_off_without_echoing_exception(self):
        self.store.set("auto", True)
        self.instance._tick = AsyncMock(side_effect=RuntimeError("private-token"))
        with patch.object(plugin.asyncio, "sleep", new_callable=AsyncMock,
                             side_effect=[None, asyncio.CancelledError]), \
                patch.object(plugin.logger, "warning") as warning:
            with self.assertRaises(asyncio.CancelledError):
                await self.instance._loop()
        # 一次瞬时错误只退避：不会把当日自主活动直接关死。
        self.assertIs(self.store.get("auto"), True)
        self.assertTrue(self.store.get("next_wake"))
        self.assertNotIn("private-token", str(warning.call_args))
        self.assertEqual(warning.call_args.args[-1], "RuntimeError")
        # 原文留在库里给主人看，日志里只出现异常类型。
        self.assertIn("private-token", json.loads(self.store.get("last_error"))["message"])

    async def test_tick_delays_first_wake_and_flushes_before_round_quota(self):
        self.bind()
        self.store.set("auto", True)
        self.instance.browser.flush_pending = AsyncMock(return_value=0)
        self.instance._run = AsyncMock()
        with patch.object(plugin, "next_wake", return_value="planned-next-wake") as wake:
            await self.instance._tick()
        self.instance.browser.flush_pending.assert_awaited_once_with(BINDING["umo"])
        wake.assert_called_once_with(45)
        self.assertEqual(self.store.get("next_wake"), "planned-next-wake")
        self.instance._run.assert_not_awaited()
        self.instance.config["daily_rounds"] = 1
        self.store.begin_round(True, 1)
        await self.instance._tick()
        self.assertEqual(self.instance.browser.flush_pending.await_count, 2)
        self.instance._run.assert_not_awaited()

    async def test_tick_due_browses_even_with_share_quota_exhausted(self):
        self.bind()
        self.store.set("auto", True)
        moment = core.now().replace(hour=12)
        self.store.set("next_wake", moment.isoformat())
        self.instance.config["daily_shares"] = 0
        self.instance.browser.flush_pending = AsyncMock(return_value=0)
        self.instance._run = AsyncMock(return_value={"status": "ok", "read": 1, "liked": 1, "sent": 0})
        with patch.object(plugin, "now", return_value=moment):
            await self.instance._tick()
        self.instance._run.assert_awaited_once_with(automatic=True)
        self.db.get_owner.return_value = None
        with self.assertRaisesRegex(RuntimeError, "绑定已失效"):
            await self.instance._tick()
        self.instance._run.assert_awaited_once()

    async def test_can_like_respects_live_revoke_switches_and_automatic_flag(self):
        self.bind()
        self.instance.config["auto_like"] = True
        self.assertTrue(self.instance._can_like(False))
        self.assertFalse(self.instance._can_like(True))
        self.store.set("auto", True)
        self.assertTrue(self.instance._can_like(True))
        self.store.set("likes_enabled", False)
        self.assertFalse(self.instance._can_like(False))
        self.store.set("likes_enabled", True)
        self.db.get_owner.return_value = None
        self.assertFalse(self.instance._can_like(False))
        self.db.get_owner.return_value = OWNER
        self.instance.config["enabled"] = False
        self.assertFalse(self.instance._can_like(False))

    async def test_stop_and_like_disable_cancel_tracked_manual_jobs(self):
        self.bind()
        for command in ("关闭", "点赞关闭"):
            with self.subTest(command=command):
                entered = asyncio.Event()
                async def active():
                    task = asyncio.current_task()
                    self.instance._active_jobs.add(task)
                    try:
                        entered.set()
                        await asyncio.Event().wait()
                    finally:
                        self.instance._active_jobs.discard(task)
                task = asyncio.create_task(active())
                self.tasks.append(task)
                await asyncio.wait_for(entered.wait(), timeout=2)
                await self.command(command)
                self.assertTrue(task.cancelled())
                self.assertEqual(self.instance._active_jobs, set())
                self.assertIsNotNone(self.instance.task)
                self.tasks.append(self.instance.task)
                if command == "点赞关闭":
                    self.assertFalse(self.instance._likes_enabled())

    async def test_explore_tool_rejects_nonowner_group_wrong_target_and_disabled(self):
        self.bind()
        self.instance._run = AsyncMock()
        for event in (Event(owner=False), Event(scene="group"), Event(umo="another-session")):
            await self.instance.explore_tool(event)
        self.instance.config["enabled"] = False
        self.assertIn("总开关", await self.instance.explore_tool(Event()))
        self.instance._run.assert_not_awaited()

    async def test_run_autonomous_plan_gets_public_only_inputs_and_dry_run_no_settings(self):
        self.bind()
        self.add_history(note="不能交给规划器的私密笔记")
        self.memory._persona.return_value = "你喜欢历史和策略\n身份私密不传\n现有人格"
        plan = {"source": "feed", "reason": "探索", "rest_minutes": 60}
        before = list(self.store.db.execute("SELECT * FROM settings"))
        with patch.object(plugin, "plan_exploration", new_callable=AsyncMock, return_value=plan) as planner:
            await self.instance._run(dry_run=True)
        args = planner.await_args.args
        self.assertEqual(args[1], "你喜欢历史和策略")
        self.assertNotIn("私密", str(args[1:]))
        self.assertNotIn("note", args[2][0])
        kwargs = self.instance.browser.run.await_args.kwargs
        self.assertEqual(kwargs["plan"], plan)
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(list(self.store.db.execute("SELECT * FROM settings")), before)
        self.assertEqual(self.instance._active_jobs, set())

    async def test_terminate_cancels_loop_and_closes_client_and_store(self):
        entered = asyncio.Event()
        hold = asyncio.Event()

        async def background():
            entered.set()
            await hold.wait()

        task = asyncio.create_task(background())
        self.tasks.append(task)
        self.instance.task = task
        await asyncio.wait_for(entered.wait(), timeout=2)
        with patch.object(self.store, "close", wraps=self.store.close) as close:
            await self.instance.terminate()
            close.assert_called_once()
        self.assertTrue(task.cancelled())
        self.instance.client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
