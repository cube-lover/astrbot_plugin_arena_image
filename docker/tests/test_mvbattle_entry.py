"""Offline mvbattle entry contracts; fake transport, no browser or platform calls."""
from __future__ import annotations

import ast
import asyncio
import base64
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from .test_arena_image_hardening import _plugin_module
from .test_battle_commands import Client, battle_module, collect, text
from .test_battle_reply import QuoteEvent as BaseQuoteEvent
from .test_raw_battle_scheduler import RawEvent, scheduler_consume
from .test_video_commands import ACK, FakeVideo, Host, video_module, videos

ROOT = Path(__file__).resolve().parents[2]
ALIASES = ("mvbattle", "视频battle", "videobattle", "竞技场视频对战")


class QuoteEvent(BaseQuoteEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopped = False
        self.stop_calls = 0
        self.delivered = []

    def is_stopped(self):
        return self.stopped

    def stop_event(self):
        self.stopped = True
        self.stop_calls += 1


class MvBattleEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.main = _plugin_module()
        with patch.object(self.main.StarTools, "get_data_dir", return_value=self.temp.name):
            self.plugin = self.main.ArenaImagePlugin(context=object(), config={})
        self.host = Host(Path(self.temp.name) / "fake-transport")
        self.plugin._video_battle_client = lambda: self.host.video_client
        for hook in ("_materialize_video_output", "_deliver_video_battle_message",
                     "_prune_video_outputs"):
            setattr(self.plugin, hook, getattr(self.host, hook))
        self.video = self.plugin._video_controller()
        self.video_patch = patch.object(video_module, "Video", FakeVideo)
        self.video_patch.start()
        self.addCleanup(self.video_patch.stop)

    def assert_released(self):
        self.assertEqual(self.video._active_generations, 0)
        self.assertFalse(self.video._battle_busy)
        self.assertFalse(self.video._battle_pending_outputs)
        self.assertFalse(self.video._generation_lock.locked())

    def commands(self):
        routes = {}
        for name, handler in vars(type(self.plugin)).items():
            if hasattr(handler, "arena_command"):
                for command in {handler.arena_command} | handler.arena_aliases:
                    self.assertNotIn(command, routes)
                    routes[command] = getattr(self.plugin, name)
        return routes

    async def begin(self, receipt="123"):
        self.host.receipt = self.main.BattleMessageReceipt(receipt)
        await collect(self.plugin.video_battle(RawEvent(), "offline prompt"))
        return next(reversed(self.video._battle_sessions))

    def test_exact_root_decorators_and_single_original_quote_filter(self):
        expected = {
            "video_battle": ("mvbattle", set(ALIASES[1:])),
            "video_battle_vote": ("mvbattle投票", {"视频battle投票", "videobattlevote"}),
            "video_battle_reveal": ("mvbattle揭晓", {
                "视频battle揭晓", "videobattlereveal", "视频battle不投票"}),
            "video_battle_resume": ("mvbattle读回", {"视频battle读回", "videobattleresume"}),
        }
        routes = self.commands()
        for name, (command, aliases) in expected.items():
            handler = getattr(self.plugin, name)
            self.assertEqual(handler.arena_command, command)
            self.assertEqual(handler.arena_aliases, aliases)
            for entry in {command} | aliases:
                self.assertEqual(routes[entry], handler)
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == "ArenaImagePlugin")
        quote_entries = [n.name for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                         and any(isinstance(d, ast.Call)
                                 and isinstance(d.func, ast.Attribute)
                                 and d.func.attr == "custom_filter"
                                 for d in n.decorator_list)]
        self.assertEqual(quote_entries, ["battle_reply_entry"])

    def test_raw_regex_case_and_boundaries(self):
        pattern = self.plugin.video_battle_plain_entry.arena_regex
        for alias in ALIASES:
            for spelling in (alias.lower(), alias.upper()):
                for suffix in ("", " prompt", "\tprompt"):
                    self.assertIsNotNone(re.match(pattern, spelling + suffix))
        for raw in ("/mvbattle prompt", ",mvbattle prompt", "xmvbattle prompt",
                    "mvbattle投票 A", "mvbattle揭晓", "mvbattle读回 ABCDEF",
                    "mvbattleextra", "videobattlevote A", "battle prompt", "A"):
            self.assertIsNone(re.match(pattern, raw))

    async def test_raw_alias_prompt_forwarding_and_stop_only_after_all_yields(self):
        calls, closed = [], []

        async def fake(incoming, prompt):
            calls.append((incoming, prompt))
            try:
                yield incoming.plain_result(ACK)
                yield incoming.plain_result("last")
            finally:
                closed.append(incoming)

        with patch.object(self.video, "video_battle", fake):
            for alias in ALIASES:
                event = RawEvent(alias.upper() + "\t  A/B: 保留  两个空格")
                generator = self.plugin.video_battle_plain_entry(event)
                try:
                    self.assertEqual((await anext(generator)).text, ACK)
                    self.assertFalse(event.is_stopped())
                    self.assertEqual((await anext(generator)).text, "last")
                    self.assertFalse(event.is_stopped())
                    with self.assertRaises(StopAsyncIteration):
                        await anext(generator)
                    self.assertEqual(event.stop_calls, 1)
                finally:
                    await generator.aclose()
                self.assertEqual(calls[-1], (event, "A/B: 保留  两个空格"))
                self.assertIs(closed[-1], event)

    async def test_woken_event_is_not_routed_twice(self):
        event = RawEvent("mvbattle prompt", woken=True)
        with patch.object(self.video, "video_battle", side_effect=AssertionError("duplicate")):
            await scheduler_consume(self.plugin.video_battle_plain_entry(event), event)
        self.assertFalse(event.delivered)
        self.assertEqual(event.stop_calls, 0)

    async def test_real_entry_ack_is_immediate_and_close_releases_inner_generator(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                event = RawEvent("MVBATTLE offline")
                collector = AsyncMock(side_effect=AssertionError("input before ACK"))
                with patch.object(self.plugin, "_collect_input_images", collector):
                    generator = (self.plugin.video_battle_plain_entry(event) if raw
                                 else self.plugin.video_battle(event, "offline"))
                    try:
                        self.assertEqual((await anext(generator)).text, ACK)
                        self.assertEqual(self.video._active_generations, 1)
                        collector.assert_not_awaited()
                        self.assertFalse(self.host.video_client.calls)
                        self.assertFalse(self.video._battle_sessions)
                        self.assertEqual(event.stop_calls, 0)
                    finally:
                        await generator.aclose()
                self.assert_released()
                self.assertEqual(event.stop_calls, 0)

    async def test_handler_task_cancel_closes_inner_generator_without_stop(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                started, closed = asyncio.Event(), asyncio.Event()
                event = RawEvent("mvbattle offline")

                async def blocked(_event, started=started, closed=closed):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        closed.set()

                with patch.object(self.plugin, "_collect_input_images", blocked):
                    generator = (self.plugin.video_battle_plain_entry(event) if raw
                                 else self.plugin.video_battle(event, "offline"))
                    self.assertEqual((await anext(generator)).text, ACK)
                    task = asyncio.create_task(anext(generator))
                    try:
                        await asyncio.wait_for(started.wait(), 1)
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        await generator.aclose()
                self.assertTrue(closed.is_set())
                self.assert_released()
                self.assertFalse(self.host.video_client.calls)
                self.assertEqual(event.stop_calls, 0)

    async def test_attached_and_quoted_image_forwarded_with_exact_prompt_after_ack(self):
        encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
        for quoted in (False, True):
            with self.subTest(quoted=quoted):
                event = RawEvent("MVBATTLE\t  animate A/B  slowly")
                image = self.main.Image(file="reference.png", base64_value=encoded)
                event.message_obj = SimpleNamespace(message=[] if quoted else [image])
                helper = AsyncMock(return_value=[image] if quoted else [])
                self.host.receipt = self.main.BattleMessageReceipt("456" if quoted else "123")
                with patch.object(self.main, "extract_quoted_message_images", helper):
                    generator = self.plugin.video_battle_plain_entry(event)
                    try:
                        self.assertEqual((await anext(generator)).text, ACK)
                        helper.assert_not_awaited()
                        self.assertEqual(event.stop_calls, 0)
                        await scheduler_consume(generator, event)
                    finally:
                        await generator.aclose()
                self.assertEqual(self.host.video_client.calls[-1], (
                    "begin", "animate A/B  slowly", [f"data:image/png;base64,{encoded}"]))
                if quoted:
                    helper.assert_awaited_once_with(event)
                self.assertEqual(event.stop_calls, 1)
                self.assert_released()

    async def test_vote_and_stop_wait_for_complete_native_receipt(self):
        self.host.delivery_release = asyncio.Event()
        event = RawEvent("mvbattle offline")
        generator = self.plugin.video_battle_plain_entry(event)
        self.assertEqual((await anext(generator)).text, ACK)
        task = asyncio.create_task(scheduler_consume(generator, event))
        try:
            await asyncio.wait_for(self.host.delivery_started.wait(), 1)
            entry = next(iter(self.video._battle_sessions.values()))
            self.assertIsNone(entry["vote"])
            self.assertEqual(entry["platform_message_id"], "")
            self.assertEqual(len(videos(self.host.native_results)), 2)
            self.assertEqual(event.stop_calls, 0)
            self.assertIn("失败", text(await collect(
                self.plugin.battle_reply_entry(QuoteEvent("A", "123")))))
            self.assertFalse([c for c in self.host.video_client.calls if c[0] == "vote"])
            self.host.delivery_release.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(entry["vote"], "")
            self.assertEqual(entry["platform_message_id"], "123")
            self.assertEqual(event.stop_calls, 1)
            self.assert_released()
        finally:
            self.host.delivery_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await generator.aclose()

    async def test_subcommands_delegate_unchanged_event_and_arguments(self):
        routes = self.commands()
        calls, closed = [], []
        for method, names, argument in (
            ("battle_vote", ("mvbattle投票", "视频battle投票", "videobattlevote"), "B ABCDEF"),
            ("battle_reveal", ("mvbattle揭晓", "视频battle揭晓", "videobattlereveal",
                               "视频battle不投票"), "ABCDEF"),
            ("video_resume", ("mvbattle读回", "视频battle读回", "videobattleresume"), "ABCDEF"),
        ):
            async def fake(event, value, method=method):
                calls.append((method, event, value))
                try:
                    yield event.plain_result(method)
                finally:
                    closed.append(method)

            with patch.object(self.video, method, fake):
                for name in names:
                    event = QuoteEvent(name + " " + argument)
                    self.assertEqual((await collect(routes[name](event, argument)))[0].text, method)
                    self.assertEqual(calls[-1], (method, event, argument))
                    self.assertEqual(closed[-1], method)

    async def test_resume_ack_close_closes_inner_generator(self):
        closed = []

        async def resume(event, code):
            try:
                yield event.plain_result("已收到，正在读取 " + code)
                raise AssertionError("read after handler closed")
            finally:
                closed.append(code)

        with patch.object(self.video, "video_resume", resume):
            generator = self.plugin.video_battle_resume(RawEvent(), "ABCDEF")
            try:
                self.assertIn("已收到", (await anext(generator)).text)
            finally:
                await generator.aclose()
        self.assertEqual(closed, ["ABCDEF"])

    async def test_double_native_video_receipt_and_exact_ab_quote_routing(self):
        image_client = Client()
        self.plugin._battle_client = lambda: image_client

        async def materialize(value):
            return self.plugin.output_dir / (value + ".png")

        async def deliver(_event, _result):
            return self.main.BattleMessageReceipt("900")

        self.plugin._materialize_output = materialize
        self.plugin._deliver_battle_message = deliver
        await collect(self.plugin.battle(RawEvent(), "image prompt"))
        image_code = next(iter(self.plugin._battle_sessions))
        old = await self.begin("123")
        latest = await self.begin("456")
        self.assertEqual(len(self.host.native_results), 2)
        result = self.host.native_results[0]
        self.assertEqual(len(videos([result])), 2)
        self.assertEqual([result.chain[i].text for i in (0, 2)], ["\nA\n", "\nB\n"])
        self.assertEqual(result.chain[-1].text, "\n引用本条回复 A / B 投票并揭晓。")
        self.assertEqual(self.video._battle_sessions[old]["platform_message_id"], "123")
        filt = self.plugin.battle_reply_entry.arena_custom_filter
        for intent, receipt, winner in (("a", "123", "a"), ("B", "456", "b")):
            event = QuoteEvent(intent, receipt)
            self.assertTrue(filt.filter(event, {}))
            await scheduler_consume(self.plugin.battle_reply_entry(event), event)
            self.assertIn("已投票 " + winner.upper(), text(event.delivered))
            self.assertTrue(event.stopped)
        self.assertEqual(self.video._battle_sessions[old]["vote"], "a")
        self.assertEqual(self.video._battle_sessions[latest]["vote"], "b")
        self.assertFalse([c for c in image_client.calls if c[0] == "vote"])
        await collect(self.plugin.battle_reply_entry(QuoteEvent("A", "123")))
        self.assertEqual(len([c for c in self.host.video_client.calls if c[0] == "vote"]), 2)
        await collect(self.plugin.battle_reply_entry(QuoteEvent("b", "900")))
        self.assertEqual(self.plugin._battle_sessions[image_code]["vote"], "b")
        self.assertEqual(len([c for c in image_client.calls if c[0] == "vote"]), 1)
        for intent in ("A please", "AB", "/A", "mvbattle A"):
            self.assertFalse(filt.filter(QuoteEvent(intent), {}))
        bare = QuoteEvent("A")
        bare.message_obj.message = [self.main.Plain("A")]
        self.assertFalse(filt.filter(bare, {}))
        self.assertIn("失败", text(await collect(
            self.plugin.battle_reply_entry(QuoteEvent("A", "999")))))
        self.assertEqual(len([c for c in self.host.video_client.calls if c[0] == "vote"]), 2)

    async def test_shared_random_reveal_and_receipt_collision_stay_fail_closed(self):
        code = await self.begin()
        self.assertIn("先投票", text(await collect(
            self.plugin.battle_reply_entry(QuoteEvent("不投票")))))
        await collect(self.plugin.battle_reveal_switch(RawEvent(), "开"))
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            result = await collect(self.plugin.battle_reply_entry(QuoteEvent("不投票")))
        self.assertIn("已随机投票 B", text(result))
        self.assertEqual(self.video._battle_sessions[code]["random_choice"], "b")
        await collect(self.plugin.battle_reply_entry(QuoteEvent("不投票")))
        self.assertEqual(len([c for c in self.host.video_client.calls if c[0] == "vote"]), 1)
        self.plugin._battle_sessions["FEDCBA"] = {
            "chat": RawEvent().unified_msg_origin, "platform_message_id": "123"}
        self.assertIn("未提交投票", text(await collect(
            self.plugin.battle_reply_entry(QuoteEvent("A")))))
        self.assertEqual(len([c for c in self.host.video_client.calls if c[0] == "vote"]), 1)
