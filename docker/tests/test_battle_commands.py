"""Offline command/session contract tests. No browser, server or real transport."""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .test_arena_image_hardening import (
    FakeEvent,
    _install_astrbot_stubs,
    _make_plugin,
    _plugin_module,
)

_install_astrbot_stubs()
battle_module = importlib.import_module("astrbot_plugin_arena_image.battle_commands")
BattleCommandsMixin = battle_module.BattleCommandsMixin
ACK = "已收到，正在生成 A/B 对战图片。"
FOOTER_RANDOM_OFF = "\n引用本条回复 A / B 投票并揭晓。"
FOOTER_RANDOM_ON = "\n引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。"
SESSION_FIELDS = {
    "context", "evaluation_id", "owner", "chat", "timestamp", "vote", "names",
    "transport_mode", "random_choice", "platform_message_id",
}


class Event(FakeEvent):
    def __init__(self, owner="alice", chat="platform:Group:room-1"):
        super().__init__(with_send=False)
        self.owner = owner
        self.unified_msg_origin = chat

    def get_sender_id(self):
        return self.owner


class Client:
    def __init__(self):
        self.response = {
            "evaluation_id": "eval-1",
            "context": {
                "opaque": {"turn": [7, "internal-session"]},
                "transport_mode": "opaque-protocol-value",
            },
            "sides": {
                "a": {"text": "", "images": ["a-image"], "error": ""},
                "b": {"text": "", "images": ["b-image"], "error": ""},
            },
            # This field must never be displayed or persisted by begin.
            "models": {"a": "SECRET-A", "b": "SECRET-B"},
        }
        self.calls = []
        self.begin_error = self.vote_error = self.reveal_error = None
        self.vote_result = None
        self.names = {"a": "Real A", "b": "Real B"}
        self.vote_gate = None
        self.vote_started = asyncio.Event()

    async def begin_battle(self, *, prompt, images):
        self.calls.append(("begin", prompt, images))
        if self.begin_error:
            raise self.begin_error
        return copy.deepcopy(self.response)

    async def vote_battle(self, context, *, winner):
        self.calls.append(("vote", copy.deepcopy(context), winner))
        context["mutated_by_client"] = True
        self.vote_started.set()
        if self.vote_gate:
            await self.vote_gate.wait()
        if self.vote_error:
            raise self.vote_error
        return self.vote_result

    async def reveal_battle(self, context):
        self.calls.append(("reveal", copy.deepcopy(context)))
        if self.reveal_error:
            raise self.reveal_error
        return self.names


class Host(BattleCommandsMixin):
    def __init__(self, directory, config=None, client=None):
        self.data_dir = Path(directory)
        self.config = dict(config or {})
        self.client = client or Client()
        self._generation_lock = asyncio.Lock()
        self._active_generations = 0
        self._last_generation_seconds = 0.0
        self.materialized = []
        self.bad_images = set()
        self.input_images = []
        self.pruned = 0
        self._init_battles()

    def _battle_client(self):
        return self.client

    async def _collect_input_images(self, _event):
        return self.input_images

    async def _materialize_output(self, value):
        self.materialized.append(value)
        if value in self.bad_images:
            raise RuntimeError("signed https://images.example/private?token=secret")
        return self.data_dir / (value + ".png")

    def _prune_outputs(self):
        self.pruned += 1


async def collect(generator):
    return [result async for result in generator]


def text(results):
    return "\n".join(
        getattr(result, "text", "")
        + "".join(
            component.text for component in result.chain
            if isinstance(component, battle_module.Plain)
        )
        for result in results
    )


def image_components(results):
    return [
        component for result in results for component in result.chain
        if isinstance(component, battle_module.Image)
    ]


class BattleCommandsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Host(self.temp.name)
        self.event = Event()

    async def begin(self, event=None, host=None):
        host = host or self.host
        event = event or self.event
        results = await collect(host.battle(event, "test prompt"))
        code = host._battle_latest[(event.unified_msg_origin, event.owner)]
        return code, results

    def calls(self, kind):
        return [call for call in self.host.client.calls if call[0] == kind]

    def disk(self):
        return json.loads(self.host._battle_sessions_file.read_text(encoding="utf-8"))

    async def test_both_sides_sent_and_only_metadata_persisted(self):
        code, results = await self.begin()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].text, ACK)
        result = results[1]
        self.assertEqual(result.text, "Battle\n")
        self.assertNotIn(code, result.text)
        self.assertEqual(
            [type(component) for component in result.chain],
            [battle_module.Plain, battle_module.Image, battle_module.Plain,
             battle_module.Image, battle_module.Plain],
        )
        self.assertEqual(result.chain[0].text, "\nA\n")
        self.assertEqual(result.chain[2].text, "\nB\n")
        images = image_components(results)
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0].file, str(self.host.data_dir / "a-image.png"))
        self.assertEqual(images[1].file, str(self.host.data_dir / "b-image.png"))
        self.assertEqual(result.chain[-1].text, FOOTER_RANDOM_OFF)
        self.assertNotIn(code, result.chain[-1].text)
        self.assertEqual(self.host.materialized, ["a-image", "b-image"])
        self.assertEqual(self.host.pruned, 1)
        self.assertEqual(self.host._active_generations, 0)
        self.assertRegex(code, r"^[0-9A-F]{6}$")
        self.assertNotIn("SECRET", text(results))
        self.assertNotIn("Real A", text(results))
        entry = self.disk()[code]
        self.assertEqual(set(entry), SESSION_FIELDS)
        self.assertEqual(set(entry), set(battle_module._SESSION_FIELDS))
        self.assertEqual(entry["context"], self.host.client.response["context"])
        self.assertEqual(entry["transport_mode"], "bridge")
        self.assertEqual(entry["vote"], "")
        self.assertIn("投票并揭晓", text(results))
        self.assertNotIn("不投票", text(results))
        self.assertNotIn("随机投票", text(results))
        self.assertNotIn("/battle不投票", text(results))
        self.assertNotIn("/battle投票", text(results))
        self.assertNotIn("/battle揭晓", text(results))
        self.assertNotIn("battle投票 A/B", text(results))
        self.assertNotIn("对战编号", text(results))
        self.assertNotIn(code, text(results))
        self.assertEqual(entry["random_choice"], "")
        self.assertEqual(entry["platform_message_id"], "")
        self.assertEqual(self.host._battle_sessions[code]["platform_message_id"], "")
        self.assertIsNone(entry["names"])
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))
        saved = self.host._battle_sessions_file.read_text(encoding="utf-8")
        for private in ("a-image", "b-image", "test prompt", "SECRET-A", "sides", "models"):
            self.assertNotIn(private, saved)
        self.assertFalse(list(self.host.data_dir.glob("*.tmp")))

    async def test_enabled_random_reveal_has_exact_compact_reply_footer(self):
        self.host.config["battle_allow_reveal_without_vote"] = True
        code, results = await self.begin()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].text, ACK)
        self.assertEqual(results[1].text, "Battle\n")
        self.assertEqual(len(image_components(results)), 2)
        self.assertEqual(
            [type(component) for component in results[1].chain],
            [battle_module.Plain, battle_module.Image, battle_module.Plain,
             battle_module.Image, battle_module.Plain],
        )
        self.assertEqual(results[1].chain[0].text, "\nA\n")
        self.assertEqual(results[1].chain[2].text, "\nB\n")
        self.assertIsInstance(results[1].chain[-1], battle_module.Plain)
        self.assertEqual(results[1].chain[-1].text, FOOTER_RANDOM_ON)
        self.assertNotIn(code, results[1].text)
        self.assertNotIn(code, results[1].chain[-1].text)
        self.assertIn("回复“不投票”随机投票并揭晓", text(results))
        self.assertNotIn("battle投票 A/B", text(results))
        self.assertNotIn("/battle投票", text(results))
        self.assertNotIn("/battle不投票", text(results))
        self.assertNotIn("/battle揭晓", text(results))
        self.assertNotIn(code, text(results))
        self.assertEqual(self.disk()[code]["vote"], "")
        self.assertEqual(self.disk()[code]["random_choice"], "")
        self.assertEqual(self.disk()[code]["platform_message_id"], "")
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_compact_reply_footer_is_independent_of_command_prefix(self):
        for prefix, enabled in ((",", False), (",", True), ("/", False), ("/", True)):
            with self.subTest(prefix=prefix, enabled=enabled):
                self.host.config["battle_allow_reveal_without_vote"] = enabled
                if prefix == ",":
                    self.host._battle_command_prefix = lambda event: ","
                elif hasattr(self.host, "_battle_command_prefix"):
                    del self.host._battle_command_prefix
                code, results = await self.begin()
                self.assertEqual(len(results), 2)
                self.assertEqual(results[0].text, ACK)
                self.assertEqual(results[1].text, "Battle\n")
                self.assertEqual(len(image_components(results)), 2)
                self.assertEqual(
                    [type(component) for component in results[1].chain],
                    [battle_module.Plain, battle_module.Image, battle_module.Plain,
                     battle_module.Image, battle_module.Plain],
                )
                footer = results[1].chain[-1].text
                self.assertEqual(footer, FOOTER_RANDOM_ON if enabled else FOOTER_RANDOM_OFF)
                self.assertEqual("不投票" in footer, enabled)
                self.assertEqual("随机投票" in footer, enabled)
                self.assertNotIn(code, results[1].text)
                self.assertNotIn(code, footer)
                self.assertNotIn("battle投票 A/B", text(results))
                for command in ("battle投票", "battle揭晓", "battle不投票"):
                    self.assertNotIn(prefix + command, text(results))
                self.assertEqual(self.disk()[code]["vote"], "")
                self.assertEqual(self.disk()[code]["random_choice"], "")
                self.assertFalse(self.calls("vote"))
                self.assertFalse(self.calls("reveal"))

    async def test_header_and_footer_hide_codes_when_another_session_is_latest(self):
        self.host._battle_command_prefix = lambda event: ","
        first, _ = await self.begin()
        latest, _ = await self.begin()
        self.assertNotEqual(first, latest)
        self.assertEqual(set(self.host._battle_sessions), {first, latest})
        self.assertEqual(
            self.host._battle_latest[(self.event.unified_msg_origin, self.event.owner)], latest
        )
        sides = [
            {"paths": [], "text": "answer " + side, "error": False, "had_images": False}
            for side in ("A", "B")
        ]
        # Native reply/session binding belongs in the dedicated reply tests.
        for enabled in (False, True):
            self.host.config["battle_allow_reveal_without_vote"] = enabled
            for code in (first, latest):
                with self.subTest(enabled=enabled, code=code):
                    result, eligible = self.host._battle_comparison_result(self.event, code, sides)
                    self.assertTrue(eligible)
                    self.assertEqual(result.text, "Battle\n")
                    self.assertEqual(
                        [type(component) for component in result.chain],
                        [battle_module.Plain] * 5,
                    )
                    footer = FOOTER_RANDOM_ON if enabled else FOOTER_RANDOM_OFF
                    self.assertEqual([component.text for component in result.chain], [
                        "\nA\n", "answer A\n", "\nB\n", "answer B\n", footer,
                    ])
                    self.assertEqual(image_components([result]), [])
                    self.assertEqual("不投票" in result.chain[-1].text, enabled)
                    self.assertEqual("随机投票" in result.chain[-1].text, enabled)
                    for hidden_code in (first, latest):
                        self.assertNotIn(hidden_code, result.text)
                        self.assertNotIn(hidden_code, result.chain[-1].text)
                    self.assertNotIn(",battle", text([result]))
        self.assertEqual(
            self.host._battle_latest[(self.event.unified_msg_origin, self.event.owner)], latest
        )
        for code in (first, latest):
            self.assertEqual(self.disk()[code]["vote"], "")
            self.assertEqual(self.disk()[code]["random_choice"], "")
            self.assertEqual(self.disk()[code]["platform_message_id"], "")
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_text_helper_includes_plain_components_without_counting_them_as_images(self):
        result = self.event.plain_result("Battle\n")
        result.chain.extend([battle_module.Plain("\nA\n"), battle_module.Plain("answer A\n")])
        self.assertEqual(text([result]), "Battle\n\nA\nanswer A\n")
        self.assertEqual(image_components([result]), [])
        result.chain.append(battle_module.Image.fromFileSystem("a-image.png"))
        self.assertEqual([image.file for image in image_components([result])], ["a-image.png"])

    async def test_output_cap_is_per_side_and_failed_candidate_retries(self):
        self.host.config["max_output_images"] = 2
        self.host.client.response["sides"]["a"]["images"] = ["bad", "a1", "a2", "a3"]
        self.host.client.response["sides"]["b"]["images"] = ["b1", "b2", "b3"]
        self.host.client.response["sides"]["a"]["text"] = "answer A"
        self.host.client.response["sides"]["b"]["text"] = "answer B"
        self.host.bad_images.add("bad")
        code, results = await self.begin()
        self.assertEqual(self.host.materialized, ["bad", "a1", "a2", "b1", "b2"])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].text, ACK)
        self.assertEqual(results[1].text, "Battle\n")
        self.assertNotIn(code, results[1].text)
        images = image_components(results)
        self.assertEqual(len(images), 4)
        self.assertEqual(
            [image.file for image in images],
            [str(self.host.data_dir / (name + ".png")) for name in ("a1", "a2", "b1", "b2")],
        )
        chain = results[1].chain
        self.assertEqual(
            [type(component) for component in chain],
            [battle_module.Plain, battle_module.Image, battle_module.Image,
             battle_module.Plain, battle_module.Plain, battle_module.Image,
             battle_module.Image, battle_module.Plain, battle_module.Plain],
        )
        self.assertEqual(chain[0].text, "\nA\n")
        self.assertEqual(chain[3].text, "answer A\n")
        self.assertEqual(chain[4].text, "\nB\n")
        self.assertEqual(chain[7].text, "answer B\n")
        self.assertEqual(chain[-1].text, FOOTER_RANDOM_OFF)
        self.assertNotIn(code, chain[-1].text)
        self.assertEqual(self.disk()[code]["vote"], "")

    async def test_minimum_one_image_per_side_even_when_cap_zero(self):
        self.host.config["max_output_images"] = 0
        await self.begin()
        self.assertEqual(self.host.materialized, ["a-image", "b-image"])

    async def test_explicit_owner_and_chat_isolation(self):
        code, _ = await self.begin()
        for foreign in (
            Event("bob"),
            Event("alice", "platform:Group:room-2"),
            Event("bob", "platform:Group:room-2"),
        ):
            vote = await collect(self.host.battle_vote(foreign, "A " + code))
            reveal = await collect(self.host.battle_reveal(foreign, code))
            self.assertIn("不属于当前聊天及发起人", text(vote))
            self.assertIn("不属于当前聊天及发起人", text(reveal))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_latest_is_per_chat_and_owner_including_restart(self):
        first, _ = await self.begin()
        bob = Event("bob")
        bob_code, _ = await self.begin(bob)
        other_room = Event("alice", "platform:Group:room-2")
        room_code, _ = await self.begin(other_room)
        latest, _ = await self.begin()
        restarted = Host(self.temp.name, client=self.host.client)
        self.assertEqual(restarted._battle_latest[(self.event.unified_msg_origin, "alice")], latest)
        self.assertEqual(restarted._battle_latest[(bob.unified_msg_origin, "bob")], bob_code)
        self.assertEqual(
            restarted._battle_latest[(other_room.unified_msg_origin, "alice")], room_code
        )
        await collect(restarted.battle_vote(self.event, "b"))
        self.assertEqual(restarted._battle_sessions[latest]["vote"], "b")
        self.assertEqual(restarted._battle_sessions[first]["vote"], "")

    async def test_vote_then_reveal_and_repeated_vote_rejected(self):
        code, _ = await self.begin()
        result = await collect(self.host.battle_vote(self.event, "A " + code.lower()))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "已投票 A。\nBattle 已揭晓\nA：Real A\nB：Real B")
        self.assertNotIn(code, text(result))
        self.assertIn("已投票 A", text(result))
        self.assertIn("Real A", text(result))
        self.assertEqual([call[0] for call in self.host.client.calls], ["begin", "vote", "reveal"])
        self.assertEqual(self.calls("vote")[0][1], self.host.client.response["context"])
        self.assertEqual(self.calls("reveal")[0][1], self.host.client.response["context"])
        repeated = await collect(self.host.battle_vote(self.event, "B " + code))
        self.assertIn("已揭晓", text(repeated))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(self.disk()[code]["vote"], "a")
        self.assertEqual(self.disk()[code]["names"], self.host.client.names)
        revealed = await collect(self.host.battle_reveal(self.event, code))
        self.assertEqual(len(revealed), 1)
        self.assertEqual(revealed[0].text, "Battle 已揭晓\nA：Real A\nB：Real B")
        self.assertNotIn(code, text(revealed))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(len(self.calls("reveal")), 1)

    async def test_concurrent_votes_are_serialized(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        first = asyncio.create_task(collect(self.host.battle_vote(self.event, "A " + code)))
        await self.host.client.vote_started.wait()
        second = asyncio.create_task(collect(self.host.battle_vote(self.event, "B " + code)))
        self.host.client.vote_gate.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(len(self.calls("reveal")), 1)
        self.assertIn("已揭晓", text(results[1]))

    async def test_concurrent_reveal_waits_for_vote_success(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        vote = asyncio.create_task(collect(self.host.battle_vote(self.event, "A " + code)))
        await self.host.client.vote_started.wait()
        reveal = asyncio.create_task(collect(self.host.battle_reveal(self.event, code)))
        self.assertFalse(self.calls("reveal"))
        self.host.client.vote_gate.set()
        results = await asyncio.gather(vote, reveal)
        self.assertIn("已揭晓", text(results[1]))
        self.assertEqual(len(self.calls("reveal")), 1)

    async def test_vote_error_does_not_save_success_or_reveal(self):
        code, _ = await self.begin()
        self.host.client.vote_error = RuntimeError("upstream unavailable")
        result = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("投票失败", text(result))
        self.assertEqual(self.host._battle_sessions[code]["vote"], "")
        self.assertEqual(self.disk()[code]["vote"], "")
        self.assertFalse(self.calls("reveal"))
        blocked = await collect(self.host.battle_reveal(self.event))
        self.assertIn("请先投票", text(blocked))
        self.host.client.vote_error = None
        success = await collect(self.host.battle_vote(self.event, "B"))
        self.assertIn("已投票 B", text(success))

    async def test_explicit_upstream_failure_is_not_a_vote_success(self):
        code, _ = await self.begin()
        for failure in (False, {"success": False}, {"error": "transport failure"}):
            self.host.client.vote_result = failure
            result = await collect(self.host.battle_vote(self.event, "A"))
            self.assertIn("上游未确认投票成功", text(result))
            self.assertEqual(self.disk()[code]["vote"], "")
            self.assertFalse(self.calls("reveal"))

    async def test_vote_confirmation_pending_names_persists_before_reveal_and_restart(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"success": True, "reveal_pending": True}
        self.host.client.reveal_error = RuntimeError("names temporarily unavailable")
        original_reveal = self.host.client.reveal_battle

        async def inspect_reveal(context):
            self.assertEqual(self.disk()[code]["vote"], "a")
            return await original_reveal(context)

        self.host.client.reveal_battle = inspect_reveal
        result = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("投票已成功", text(result))
        self.assertEqual(self.disk()[code]["vote"], "a")
        restarted = Host(self.temp.name, client=self.host.client)
        repeated = await collect(restarted.battle_vote(self.event, "B"))
        self.assertIn("已经投票", text(repeated))
        self.assertEqual(len(self.calls("vote")), 1)
        self.host.client.reveal_error = None
        revealed = await collect(restarted.battle_reveal(self.event))
        self.assertIn("Real A", text(revealed))
        self.assertEqual(self.disk()[code]["names"], self.host.client.names)

    async def test_vote_confirmation_with_names_uses_cache_without_second_reveal(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"success": True, "a": "Real A", "b": "Real B"}
        self.host.client.reveal_error = RuntimeError("must not request reveal")
        result = await collect(self.host.battle_vote(self.event, "B"))
        self.assertIn("已投票 B", text(result))
        self.assertEqual(self.disk()[code]["vote"], "b")
        self.assertEqual(self.disk()[code]["names"], self.host.client.names)
        self.assertFalse(self.calls("reveal"))

    async def test_confirmed_vote_survives_invalid_names(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"success": True, "a": 123, "b": "Real B"}
        result = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("投票已成功", text(result))
        self.assertEqual(self.disk()[code]["vote"], "a")
        self.assertIsNone(self.disk()[code]["names"])
        restarted = Host(self.temp.name, client=self.host.client)
        await collect(restarted.battle_vote(self.event, "A"))
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_text_fallback_cannot_make_failed_image_lane_votable(self):
        self.host.bad_images.add("a-image")
        self.host.client.response["sides"]["a"]["text"] = "fallback text"
        code, result = await self.begin()
        self.assertIn("fallback text", text(result))
        self.assertIn("A 路失败", text(result))
        self.assertIsNone(self.disk()[code]["vote"])
        restarted = Host(self.temp.name, client=self.host.client)
        vote = await collect(restarted.battle_vote(self.event, "A"))
        self.assertIn("禁止投票", text(vote))
        self.assertFalse(self.calls("vote"))

    async def test_legitimate_text_only_lanes_still_work_without_image_candidates(self):
        for side in ("a", "b"):
            self.host.client.response["sides"][side]["images"] = []
            self.host.client.response["sides"][side]["text"] = "text " + side
        code, _ = await self.begin()
        self.assertEqual(self.disk()[code]["vote"], "")

    async def test_vote_success_reveal_failure_and_restart_retry(self):
        code, _ = await self.begin()
        self.host.client.reveal_error = RuntimeError("name service failed")
        result = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("投票已成功", text(result))
        self.assertEqual(self.disk()[code]["vote"], "a")
        self.assertIsNone(self.disk()[code]["names"])
        restarted = Host(self.temp.name, client=self.host.client)
        again = await collect(restarted.battle_vote(self.event, "B"))
        self.assertIn("已经投票", text(again))
        self.assertEqual(len(self.calls("vote")), 1)
        self.host.client.reveal_error = None
        revealed = await collect(restarted.battle_reveal(self.event))
        self.assertEqual(len(revealed), 1)
        self.assertEqual(revealed[0].text, "Battle 已揭晓\nA：Real A\nB：Real B")
        self.assertNotIn(code, text(revealed))
        self.assertIn("Real B", text(revealed))
        self.assertEqual(self.disk()[code]["names"], self.host.client.names)

    async def test_reveal_without_vote_defaults_off_then_random_vote_enabled(self):
        code, _ = await self.begin()
        blocked = await collect(self.host.battle_reveal(self.event, code))
        self.assertIn("请先投票", text(blocked))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))
        await collect(self.host.battle_reveal_switch(self.event, "开"))
        with patch.object(battle_module.secrets, "choice", return_value="a"):
            revealed = await collect(self.host.battle_reveal(self.event))
        self.assertEqual(len(revealed), 1)
        self.assertEqual(
            revealed[0].text, "已随机投票 A。\nBattle 已揭晓\nA：Real A\nB：Real B"
        )
        self.assertNotIn(code, text(revealed))
        self.assertIn("已随机投票 A", text(revealed))
        self.assertIn("Real A", text(revealed))
        self.assertEqual(self.disk()[code]["vote"], "a")
        self.assertEqual(self.disk()[code]["random_choice"], "a")
        blocked_vote = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("已揭晓", text(blocked_vote))
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_restart_random_reveal_stays_unvotable_and_source_cached(self):
        code, _ = await self.begin()
        self.host.config["battle_allow_reveal_without_vote"] = True
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            await collect(self.host.battle_reveal(self.event, code))
        restarted = Host(self.temp.name, client=self.host.client)
        reveal = await collect(restarted.battle_reveal(self.event))
        self.assertEqual(len(reveal), 1)
        self.assertEqual(
            reveal[0].text, "已随机投票 B。\nBattle 已揭晓\nA：Real A\nB：Real B"
        )
        self.assertNotIn(code, text(reveal))
        self.assertIn("已随机投票 B", text(reveal))
        self.assertIn("Real A", text(reveal))
        vote = await collect(restarted.battle_vote(self.event, "A"))
        self.assertIn("已揭晓", text(vote))
        self.assertEqual(len(self.calls("reveal")), 1)
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_side_error_is_failure_even_with_image_and_anonymous_error_hidden(self):
        self.host.client.response["sides"]["a"]["error"] = (
            "SECRET-A request token=secret https://images.example/private"
        )
        code, results = await self.begin()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].text, ACK)
        self.assertEqual(len(image_components(results)), 2)
        self.assertIn("A 路失败", text(results))
        self.assertNotIn("B 路失败", text(results))
        self.assertNotIn("SECRET", text(results))
        self.assertNotIn("token", text(results))
        self.assertIsNone(self.disk()[code]["vote"])
        vote = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("禁止投票", text(vote))
        reveal = await collect(self.host.battle_reveal(self.event))
        self.assertIn("禁止投票", text(reveal))
        self.host.config["battle_allow_reveal_without_vote"] = True
        reveal = await collect(self.host.battle_reveal(self.event))
        self.assertIn("禁止投票", text(reveal))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_unpresentable_side_blocks_vote_after_restart(self):
        self.host.bad_images.add("b-image")
        code, result = await self.begin()
        self.assertIn("B 路失败", text(result))
        self.assertNotIn("signed", text(result))
        self.assertIsNone(self.disk()[code]["vote"])
        restarted = Host(self.temp.name, client=self.host.client)
        vote = await collect(restarted.battle_vote(self.event, "B"))
        self.assertIn("禁止投票", text(vote))
        self.assertFalse(self.calls("vote"))

    async def test_both_sides_failed_cannot_random_vote_or_reveal(self):
        self.host.client.response["sides"] = {}
        code, results = await self.begin()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].text, ACK)
        self.assertEqual(results[1].text, "Battle\n")
        self.assertNotIn(code, results[1].text)
        self.assertEqual(image_components(results), [])
        self.assertIn("A 路失败", text(results))
        self.assertIn("B 路失败", text(results))
        self.assertEqual(
            [type(component) for component in results[1].chain],
            [battle_module.Plain] * 5,
        )
        self.assertEqual(results[1].chain[-1].text, "\n本场禁止投票，请重新发起。")
        self.assertNotIn(code, results[1].chain[-1].text)
        self.assertNotIn("引用本条", results[1].chain[-1].text)
        self.assertNotIn("可用 /battle揭晓", text(results))
        self.assertNotIn("/battle不投票", text(results))
        self.assertNotIn("/battle投票", text(results))
        self.assertNotIn("/battle揭晓", text(results))
        self.assertIsNone(self.disk()[code]["vote"])
        self.host.config["battle_allow_reveal_without_vote"] = True
        result = await collect(self.host.battle_reveal(self.event, code))
        self.assertIn("禁止投票", text(result))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_text_only_sides_are_presentable(self):
        self.host.client.response["sides"] = {
            "a": {"text": "answer A", "images": [], "error": ""},
            "b": {"text": "answer B", "images": [], "error": ""},
        }
        code, result = await self.begin()
        self.assertIn("answer A", text(result))
        self.assertIn("answer B", text(result))
        self.assertEqual(self.disk()[code]["vote"], "")
        self.assertFalse(self.host.materialized)

    async def test_expiry_on_lookup_and_restart(self):
        self.host.config["battle_session_ttl"] = 10
        with patch.object(battle_module.time, "time", return_value=1000):
            code, _ = await self.begin()
        with patch.object(battle_module.time, "time", return_value=1010):
            result = await collect(self.host.battle_vote(self.event, "A " + code))
            self.assertIn("已过期", text(result))
            self.assertNotIn(code, self.disk())
            restarted = Host(self.temp.name, {"battle_session_ttl": 10})
            self.assertFalse(restarted._battle_sessions)
        self.assertFalse(self.calls("vote"))

    async def test_restart_drops_expired_records_before_any_command(self):
        with patch.object(battle_module.time, "time", return_value=1000):
            await self.begin()
        with patch.object(battle_module.time, "time", return_value=90000):
            restarted = Host(self.temp.name)
        self.assertFalse(restarted._battle_sessions)
        self.assertFalse(restarted._battle_latest)

    async def test_transport_mode_preserved_and_changed_channel_refused(self):
        self.host.config["transport_mode"] = "direct"
        code, _ = await self.begin()
        restarted = Host(self.temp.name, {"transport_mode": "userscript"}, self.host.client)
        for command, args in (
            (restarted.battle_vote, "A " + code),
            (restarted.battle_reveal, code),
        ):
            result = await collect(command(self.event, args))
            self.assertIn("传输通道已切换", text(result))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))
        self.assertEqual(
            restarted._battle_sessions[code]["context"], self.host.client.response["context"]
        )
        restarted.config["transport_mode"] = "direct"
        result = await collect(restarted.battle_vote(self.event, "A"))
        self.assertIn("已投票 A", text(result))

    async def test_ack_precedes_input_collection_lock_and_upstream_request(self):
        collected = []

        async def inputs(_event):
            collected.append(True)
            return ["reference"]

        self.host._collect_input_images = inputs
        generator = self.host.battle(self.event, "prompt")
        try:
            ack = await asyncio.wait_for(anext(generator), 1)
            self.assertEqual(ack.text, ACK)
            self.assertFalse(image_components([ack]))
            self.assertEqual(self.host._active_generations, 1)
            self.assertFalse(collected)
            self.assertFalse(self.host._generation_lock.locked())
            self.assertFalse(self.host.client.calls)
            self.assertFalse(self.host.materialized)
            self.assertFalse(self.host._battle_sessions)
            final = await collect(generator)
            self.assertEqual(len(final), 1)
            self.assertEqual(len(image_components(final)), 2)
            self.assertEqual(self.calls("begin"), [("begin", "prompt", ["reference"])])
            self.assertEqual(collected, [True])
            self.assertEqual(self.host._active_generations, 0)
        finally:
            await generator.aclose()

    async def test_closing_idle_ack_releases_reservation_without_generation(self):
        generator = self.host.battle(self.event, "prompt")
        self.assertEqual((await anext(generator)).text, ACK)
        await generator.aclose()
        self.assertEqual(self.host._active_generations, 0)
        self.assertFalse(self.host.client.calls)
        self.assertFalse(self.host._battle_sessions)
        self.assertFalse(self.host._battle_pending_outputs)

    async def test_cancellation_after_ack_waiting_for_lock_releases_reservation(self):
        await self.host._generation_lock.acquire()
        generator = self.host.battle(self.event, "prompt")
        self.assertEqual((await anext(generator)).text, ACK)
        task = asyncio.create_task(collect(generator))
        try:
            await asyncio.sleep(0)
            self.assertFalse(self.host.client.calls)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(self.host._active_generations, 0)
            self.assertTrue(self.host._generation_lock.locked())
            self.assertFalse(self.host._battle_sessions)
        finally:
            self.host._generation_lock.release()
            await generator.aclose()

    async def test_cancellation_during_generation_after_ack_releases_lock(self):
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def blocked_begin(**kwargs):
            self.host.client.calls.append(("begin", kwargs["prompt"], kwargs["images"]))
            entered.set()
            await gate.wait()
            return self.host.client.response

        self.host.client.begin_battle = blocked_begin
        generator = self.host.battle(self.event, "prompt")
        self.assertEqual((await anext(generator)).text, ACK)
        task = asyncio.create_task(collect(generator))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertTrue(self.host._generation_lock.locked())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(len(self.calls("begin")), 1)
            self.assertEqual(self.host._active_generations, 0)
            self.assertFalse(self.host._generation_lock.locked())
            self.assertFalse(self.host._battle_sessions)
        finally:
            if not task.done():
                task.cancel()
            await generator.aclose()

    async def test_second_task_at_first_ack_receives_queue_notice_only(self):
        first = self.host.battle(self.event, "first")
        second = self.host.battle(Event("bob"), "second")
        try:
            self.assertEqual((await anext(first)).text, ACK)
            notice = await anext(second)
            self.assertEqual(notice.text, "前面还有 1 个出图任务，对战已排队。")
            self.assertEqual(self.host._active_generations, 2)
            self.assertFalse(self.host.client.calls)
            one = await collect(first)
            two = await collect(second)
            self.assertEqual(len(one), 1)
            self.assertEqual(len(two), 1)
            self.assertEqual([c[1] for c in self.calls("begin")], ["first", "second"])
            self.assertEqual(self.host._active_generations, 0)
        finally:
            await first.aclose()
            await second.aclose()

    async def test_queue_cap_and_shared_generation_lock(self):
        self.host.config["max_queue_depth"] = 2
        self.host._active_generations = 2
        full = await collect(self.host.battle(self.event, "prompt"))
        self.assertIn("排队已满", text(full))
        self.assertEqual(len(full), 1)
        self.assertNotIn(ACK, text(full))
        self.assertEqual(self.host._active_generations, 2)
        self.assertFalse(self.calls("begin"))
        self.host._active_generations = 1
        await self.host._generation_lock.acquire()
        generator = self.host.battle(self.event, "prompt")
        notice = await anext(generator)
        self.assertEqual(notice.text, "前面还有 1 个出图任务，对战已排队。")
        self.assertEqual(self.host._active_generations, 2)
        task = asyncio.create_task(collect(generator))
        # No network happens while the other generation owns the shared lock.
        self.assertFalse(self.calls("begin"))
        self.host._generation_lock.release()
        await task
        self.assertEqual(len(self.calls("begin")), 1)
        self.assertEqual(self.host._active_generations, 1)

    async def test_cancelling_queued_generator_releases_reservation(self):
        self.host._active_generations = 1
        generator = self.host.battle(self.event, "prompt")
        await anext(generator)
        self.assertEqual(self.host._active_generations, 2)
        await generator.aclose()
        self.assertEqual(self.host._active_generations, 1)

    async def test_half_delivered_results_never_become_votable(self):
        generator = self.host.battle(self.event, "prompt")
        ack = await anext(generator)
        self.assertEqual(ack.text, ACK)
        self.assertFalse(self.host._battle_sessions)
        first = await anext(generator)
        code = next(iter(self.host._battle_sessions))
        self.assertEqual(first.text, "Battle\n")
        self.assertNotIn(code, first.text)
        self.assertEqual(len(image_components([first])), 2)
        self.assertEqual(
            [type(component) for component in first.chain],
            [battle_module.Plain, battle_module.Image, battle_module.Plain,
             battle_module.Image, battle_module.Plain],
        )
        self.assertEqual(first.chain[0].text, "\nA\n")
        self.assertEqual(first.chain[2].text, "\nB\n")
        self.assertEqual(first.chain[-1].text, FOOTER_RANDOM_OFF)
        self.assertNotIn(code, first.chain[-1].text)
        self.assertFalse(self.host._generation_lock.locked())
        self.assertIsNone(self.disk()[code]["vote"])
        await generator.aclose()
        self.assertEqual(self.host._active_generations, 0)
        self.assertFalse(self.host._battle_pending_outputs)
        self.assertFalse(self.host._battle_busy)
        restarted = Host(self.temp.name, client=self.host.client)
        result = await collect(restarted.battle_vote(self.event, "A"))
        self.assertIn("禁止投票", text(result))
        self.assertFalse(self.calls("vote"))

    async def test_begin_and_input_failures_have_chinese_messages_and_no_sessions(self):
        self.host.client.begin_error = RuntimeError("upstream unavailable")
        result = await collect(self.host.battle(self.event, "prompt"))
        self.assertIn("对战失败", text(result))
        self.assertFalse(self.host._battle_sessions)
        self.assertEqual(self.host._active_generations, 0)

        async def bad_input(_event):
            raise RuntimeError("input unavailable")

        self.host._collect_input_images = bad_input
        result = await collect(self.host.battle(self.event, "prompt"))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].text, ACK)
        self.assertIn("开始对战失败", text(result))
        self.assertEqual(len(self.calls("begin")), 1)
        self.assertEqual(self.host._active_generations, 0)

    async def test_invalid_context_is_rejected_without_changing_valid_session(self):
        code, _ = await self.begin()
        for invalid in (
            None,
            [],
            {"bad": object()},
            {"bad": float("nan")},
            {"big": "x" * battle_module.MAX_CONTEXT_BYTES},
        ):
            self.host.client.response["context"] = invalid
            result = await collect(self.host.battle(self.event, "prompt"))
            self.assertIn("对战失败", text(result))
            self.assertEqual(list(self.host._battle_sessions), [code])
            self.assertEqual(list(self.disk()), [code])

    async def test_corrupted_file_is_not_overwritten_on_begin(self):
        file = self.host._battle_sessions_file
        file.write_text("not JSON", encoding="utf-8")
        restarted = Host(self.temp.name)
        result = await collect(restarted.battle(self.event, "prompt"))
        self.assertIn("读取对战会话失败", text(result))
        self.assertEqual(len(result), 1)
        self.assertNotIn(ACK, text(result))
        self.assertEqual(self.host._active_generations, 0)
        self.assertEqual(file.read_text(encoding="utf-8"), "not JSON")
        self.assertFalse(restarted.client.calls)

    async def test_bad_saved_records_are_ignored_and_extra_fields_not_rewritten(self):
        code, _ = await self.begin()
        records = self.disk()
        records[code]["raw_response"] = {"images": ["private"]}
        records["000001"] = {**records[code], "transport_mode": ""}
        records["000002"] = {**records[code], "names": {"a": "only one"}}
        records["000003"] = {**records[code], "timestamp": float("nan")}
        self.host._battle_sessions_file.write_text(json.dumps(records), encoding="utf-8")
        restarted = Host(self.temp.name)
        self.assertEqual(list(restarted._battle_sessions), [code])
        restarted._battle_save()
        self.assertNotIn("raw_response", self.disk()[code])
        expected = self.disk()[code]
        self.assertEqual(set(expected), SESSION_FIELDS)
        self.assertEqual(expected["platform_message_id"], "")
        for missing in (
            ("platform_message_id",), ("random_choice",),
            ("random_choice", "platform_message_id"),
        ):
            with self.subTest(missing=missing):
                legacy = {key: value for key, value in expected.items() if key not in missing}
                self.host._battle_sessions_file.write_text(
                    json.dumps({code: legacy}), encoding="utf-8"
                )
                restarted = Host(self.temp.name)
                self.assertEqual(restarted._battle_load_error, "")
                self.assertEqual(list(restarted._battle_sessions), [code])
                self.assertEqual(set(restarted._battle_sessions[code]), SESSION_FIELDS)
                self.assertEqual(restarted._battle_sessions[code], expected)
                self.assertEqual(restarted._battle_sessions[code]["random_choice"], "")
                self.assertEqual(restarted._battle_sessions[code]["platform_message_id"], "")
                self.assertEqual(
                    restarted._battle_latest[(self.event.unified_msg_origin, self.event.owner)],
                    code,
                )
                restarted._battle_save()
                self.assertEqual(self.disk(), {code: expected})
                self.assertFalse(restarted.client.calls)

    async def test_session_count_is_bounded_and_latest_rebuilt(self):
        with patch.object(battle_module, "MAX_BATTLE_SESSIONS", 2):
            first, _ = await self.begin()
            second, _ = await self.begin(Event("bob"))
            third, _ = await self.begin()
            self.assertEqual(set(self.host._battle_sessions), {second, third})
            self.assertNotIn(first, self.disk())
            self.assertEqual(
                self.host._battle_latest[(self.event.unified_msg_origin, "alice")], third
            )
            self.assertNotIn(first, self.host._battle_locks)

    async def test_save_is_atomic_and_old_json_survives_replace_failure(self):
        code, _ = await self.begin()
        original = self.host._battle_sessions_file.read_bytes()
        self.host._battle_sessions[code]["vote"] = "a"
        with (
            patch.object(battle_module.os, "replace", side_effect=OSError("disk unavailable")),
            self.assertRaises(OSError),
        ):
            self.host._battle_save()
        self.assertEqual(self.host._battle_sessions_file.read_bytes(), original)
        self.assertFalse(list(self.host.data_dir.glob("*.tmp")))

    async def test_cached_reveal_retries_persistence_after_disk_failure(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"success": True, "reveal_pending": True}
        self.host.client.reveal_error = RuntimeError("names unavailable")
        await collect(self.host.battle_vote(self.event, "A"))
        self.host.client.reveal_error = None
        real_save = self.host._battle_save

        def fail_names_save():
            if self.host._battle_sessions[code]["names"] is not None:
                raise OSError("disk unavailable")
            real_save()

        with patch.object(self.host, "_battle_save", side_effect=fail_names_save):
            result = await collect(self.host.battle_reveal(self.event))
        self.assertIn("揭晓失败", text(result))
        self.assertEqual(self.host._battle_sessions[code]["names"], self.host.client.names)
        self.assertIsNone(self.disk()[code]["names"])
        vote = await collect(self.host.battle_vote(self.event, "A"))
        self.assertIn("已揭晓", text(vote))
        reads = len(self.calls("reveal"))
        result = await collect(self.host.battle_reveal(self.event))
        self.assertIn("Real A", text(result))
        self.assertEqual(self.disk()[code]["names"], self.host.client.names)
        self.assertEqual(len(self.calls("reveal")), reads)
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_capacity_does_not_evict_busy_vote(self):
        with patch.object(battle_module, "MAX_BATTLE_SESSIONS", 1):
            code, _ = await self.begin()
            self.host.client.vote_gate = asyncio.Event()
            vote = asyncio.create_task(collect(self.host.battle_vote(self.event, "A")))
            await self.host.client.vote_started.wait()
            result = await collect(self.host.battle(Event("bob"), "prompt"))
            self.assertIn("会话已满", text(result))
            self.assertEqual(list(self.host._battle_sessions), [code])
            self.host.client.vote_gate.set()
            voted = await vote
            self.assertIn("已投票 A", text(voted))

    async def test_cancelling_upstream_vote_preserves_local_unvoted_state(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        vote = asyncio.create_task(collect(self.host.battle_vote(self.event, "A")))
        await self.host.client.vote_started.wait()
        vote.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await vote
        self.assertEqual(self.disk()[code]["vote"], "")
        self.assertFalse(self.host._battle_busy)
        self.assertFalse(self.calls("reveal"))

    async def test_reveal_failure_or_incomplete_names_not_recorded(self):
        code, _ = await self.begin()
        self.host.config["battle_allow_reveal_without_vote"] = True
        for names in (None, {"a": "Real A"}, {"a": "", "b": "Real B"}, {"a": 123, "b": "Real B"}):
            self.host.client.names = names
            with patch.object(battle_module.secrets, "choice", return_value="a"):
                result = await collect(self.host.battle_reveal(self.event))
            self.assertIn("投票已成功", text(result))
            self.assertIsNone(self.host._battle_sessions[code]["names"])
            self.assertIsNone(self.disk()[code]["names"])
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(self.disk()[code]["vote"], "a")

    async def test_configuration_switch_sync_async_query_and_failure_rollback(self):
        saved = []
        self.host.save_config = lambda: saved.append(dict(self.host.config))
        result = await collect(self.host.battle_reveal_switch(self.event, "开"))
        self.assertIn("开", text(result))
        self.assertTrue(saved[-1]["battle_allow_reveal_without_vote"])
        result = await collect(self.host.battle_reveal_switch(self.event, "查询"))
        self.assertEqual(len(saved), 1)
        self.assertIn("开", text(result))

        async def async_save():
            saved.append(dict(self.host.config))

        self.host.save_config = async_save
        await collect(self.host.battle_reveal_switch(self.event, "关"))
        self.assertFalse(saved[-1]["battle_allow_reveal_without_vote"])

        def fail_save():
            raise OSError("disk unavailable")

        self.host.save_config = fail_save
        result = await collect(self.host.battle_reveal_switch(self.event, "开"))
        self.assertIn("修改揭晓开关失败", text(result))
        self.assertFalse(self.host.config["battle_allow_reveal_without_vote"])

    async def test_config_object_save_config_is_supported(self):
        class Config(dict):
            saved = False

            async def save_config(self):
                self.saved = True

        self.host.config = Config()
        await collect(self.host.battle_reveal_switch(self.event, "开"))
        self.assertTrue(self.host.config.saved)

    async def test_disabled_commands_do_not_contact_transport(self):
        self.host.config["battle_enabled"] = False
        for command, arg in (
            (self.host.battle, "prompt"),
            (self.host.battle_vote, "A"),
            (self.host.battle_reveal, ""),
        ):
            result = await collect(command(self.event, arg))
            self.assertIn("功能已关闭", text(result))
            self.assertEqual(len(result), 1)
            self.assertNotIn(ACK, text(result))
        self.assertFalse(self.host.client.calls)

    async def test_argument_usage_and_image_only_default_prompt(self):
        result = await collect(self.host.battle(self.event))
        self.assertIn("用法", text(result))
        self.assertEqual(len(result), 1)
        self.assertNotIn(ACK, text(result))
        self.assertEqual(self.host._active_generations, 0)
        self.assertFalse(self.host.client.calls)
        for arg in ("", "C", "A code extra"):
            result = await collect(self.host.battle_vote(self.event, arg))
            self.assertIn("用法", text(result))
        result = await collect(self.host.battle_reveal(self.event, "code extra"))
        self.assertIn("用法", text(result))
        result = await collect(self.host.battle_reveal_switch(self.event, "invalid"))
        self.assertIn("用法", text(result))
        self.host.input_images = ["data:image/png;base64,fixture"]
        await collect(self.host.battle(self.event))
        self.assertEqual(self.calls("begin")[0][1], "根据参考图生成一张更精致的图片")
        self.assertEqual(self.calls("begin")[0][2], self.host.input_images)

    async def test_missing_chat_or_owner_fails_closed(self):
        for event in (Event(owner=""), Event(chat=""), FakeEvent()):
            result = await collect(self.host.battle(event, "prompt"))
            self.assertIn("无法识别", text(result))
            self.assertEqual(len(result), 1)
            self.assertNotIn(ACK, text(result))
        self.assertFalse(self.host.client.calls)

    async def test_safe_error_formatter_does_not_echo_urls_credentials_or_context(self):
        for raw, hidden in (
            ("fail https://example/private?token=abc", "example"),
            ("fail Bearer topsecret", "topsecret"),
            ("fail api_key=topsecret", "topsecret"),
            ("fail access_token:topsecret", "topsecret"),
            ("fail {'opaque': 'internal-session'}", "internal-session"),
        ):
            self.assertNotIn(hidden, battle_module._display_error(RuntimeError(raw)))
        self.assertLessEqual(len(battle_module._display_error(RuntimeError("x" * 1000))), 301)

    async def test_real_host_generation_helpers_are_compatible(self):
        main, plugin = _make_plugin(self, {"max_output_images": 1})

        class IntegratedHost(main.ArenaImagePlugin):
            def _battle_client(self):
                return client

        plugin.__class__ = IntegratedHost
        plugin._init_battles()
        client = Client()
        # Real output helper decodes this minimal still PNG locally.
        import base64

        fixture = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
        client.response["sides"]["a"]["images"] = [fixture]
        client.response["sides"]["b"]["images"] = [fixture]
        results = await collect(plugin.battle(self.event, "prompt"))
        self.assertEqual(len(results), 2, text(results))
        self.assertEqual(results[0].text, ACK)
        images = image_components(results)
        self.assertEqual(len(images), 2, text(results))
        self.assertTrue(all(Path(image.file).is_file() for image in images))
        self.assertEqual(plugin._active_generations, 0)

    def test_command_decorators_and_admin_switch(self):
        for method, name, aliases in (
            ("battle", "battle", {"竞技场battle", "竞技场对战"}),
            ("battle_vote", "battle投票", {"battlevote", "竞技场投票"}),
            ("battle_reveal", "battle揭晓", {"battlereveal", "竞技场揭晓", "battle不投票"}),
        ):
            handler = getattr(_plugin_module().ArenaImagePlugin, method)
            self.assertEqual(handler.arena_command, name)
            self.assertEqual(handler.arena_aliases, aliases)
            self.assertIsNone(getattr(handler, "arena_permission", None))
        self.assertEqual(_plugin_module().ArenaImagePlugin.battle_reveal_switch.arena_permission, "admin")
        self.assertEqual(_plugin_module().ArenaImagePlugin.battle_reveal_switch.arena_command, "battle揭晓开关")


if __name__ == "__main__":
    unittest.main()
