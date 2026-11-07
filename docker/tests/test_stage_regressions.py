"""Stage-specific regression contracts. Strict in-memory fakes; no live votes."""
from __future__ import annotations
import ast
import asyncio
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from .test_arena_image_hardening import _make_plugin, _plugin_module
from .test_battle_commands import (
    ACK, Client, Event, Host as ImageHost, battle_module, collect, image_components, text,
)
from .test_arena_direct_transport import FakePage, _image_model, _filler_models
from astrbot_plugin_arena_image import arena_direct, arena_relay, battle_reply, bridge_client, model_catalog, qq_delivery

STAGE = Path(__file__).resolve().parents[2]
PRESERVATION_FIXTURE = Path(__file__).with_name("fixtures") / "arena-preservation.json"


class Host(ImageHost):
    """Stage-local adapter for the real wrapper's cross-store arbitration hooks.

    Do not patch the shared ImageHost: other modules test the mixin in isolation.
    Bind production methods, not an always-false collision predicate, including
    the lazy controller needed to load persisted video receipts on first quote.
    """

    def __init__(self, directory, config=None, client=None):
        super().__init__(directory, config, client)
        plugin = _plugin_module().ArenaImagePlugin
        for name in ("_ambiguous_battle_quote", "_video_controller"):
            setattr(self, name, getattr(plugin, name).__get__(self, type(self)))


@asynccontextmanager
async def _tracked_task(coro):
    """Cancel and retrieve the child even when a barrier/assertion fails."""
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)


async def _wait_for_barrier(event, child, *, timeout=5):
    """Race the signal against child completion; expose the original exception."""
    async with _tracked_task(event.wait()) as signal:
        done, _ = await asyncio.wait(
            {signal, child}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if child in done:
            child.result()  # Never turn an early child exception into a timeout.
            if signal not in done and not event.is_set():
                raise AssertionError("child completed before signalling the barrier")
        if signal not in done and not event.is_set():
            raise TimeoutError(f"async barrier was not signalled within {timeout}s")
        await signal


def _preservation_fixture():
    """Public fingerprints only; no private baseline fallback or skipped checks."""
    fixture = json.loads(PRESERVATION_FIXTURE.read_text(encoding="utf-8"))
    if fixture["format_version"] != 1:
        raise ValueError("unsupported preservation fixture format")
    return fixture


def _ast_sha256(node):
    """Hash the full attribute-free AST identically on Python 3.10-3.12.

    Python 3.12 adds empty type_params fields to existing definitions. Omit
    only those empty fields, including nested definitions; retain non-empty
    type parameters and every other AST field. Do not mutate the caller's AST.
    """
    normalized = copy.deepcopy(node)
    for child in ast.walk(normalized):
        if getattr(child, "type_params", None) == []:
            child._fields = tuple(field for field in child._fields if field != "type_params")
    dumped = ast.dump(normalized, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def _class_method_fingerprints(path, classname):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.name: _ast_sha256(node) for cls in tree.body
            if isinstance(cls, ast.ClassDef) and cls.name == classname
            for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _function_fingerprints(path):
    """Include all functions, including nested functions with their full scope."""
    found = {}

    def visit(node, scope=()):
        for child in ast.iter_child_nodes(node):
            nested = scope
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                nested = (*scope, child.name)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found[".".join(nested)] = _ast_sha256(child)
            visit(child, nested)

    visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def _canonical_json_sha256(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_online_readme_prefix(testcase, readme):
    expected = _preservation_fixture()["online_baseline"]["readme_prefix"]
    length = expected["byte_length"]
    testcase.assertGreaterEqual(len(readme), length, "online README prefix truncated")
    testcase.assertEqual(hashlib.sha256(readme[:length]).hexdigest(), expected["sha256"],
                         "online README prefix changed")

class BarrierGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_exception_is_propagated_before_timeout(self):
        event = asyncio.Event()
        failure = RuntimeError("fixture failed before signalling")

        async def fail():
            raise failure

        async with _tracked_task(fail()) as child:
            with self.assertRaises(RuntimeError) as raised:
                await _wait_for_barrier(event, child, timeout=1)
            self.assertIs(raised.exception, failure)
        self.assertTrue(child.done())

    async def test_successful_child_without_signal_is_not_a_timeout(self):
        async def finish():
            return None

        async with _tracked_task(finish()) as child:
            with self.assertRaisesRegex(AssertionError, "before signalling"):
                await _wait_for_barrier(asyncio.Event(), child, timeout=1)

    async def test_successful_child_with_signal_is_accepted(self):
        event = asyncio.Event()

        async def finish():
            event.set()

        async with _tracked_task(finish()) as child:
            await _wait_for_barrier(event, child, timeout=1)
        self.assertTrue(child.done())

    async def test_timeout_cleans_pending_child_and_signal(self):
        before = asyncio.all_tasks()
        async with _tracked_task(asyncio.Event().wait()) as child:
            with self.assertRaisesRegex(TimeoutError, "not signalled"):
                await _wait_for_barrier(asyncio.Event(), child, timeout=0.02)
        self.assertTrue(child.cancelled())
        self.assertEqual(asyncio.all_tasks(), before)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Host(self.temp.name)
        self.event = Event()
        self.calls = []
        self.single_model_delivery = AsyncMock(
            side_effect=AssertionError('battle must not use the single-model image sender')
        )
        self.host._deliver_image = self.single_model_delivery
        self.addCleanup(self.single_model_delivery.assert_not_called)

    def code(self):
        return self.host._battle_latest[(self.event.unified_msg_origin, self.event.owner)]

    def entry(self):
        return self.host._battle_sessions[self.code()]

    def on_disk(self):
        return json.loads(self.host._battle_sessions_file.read_text(encoding='utf-8'))[self.code()]

    def assert_ack(self, result):
        self.assertEqual(result.text, ACK)
        self.assertEqual(result.chain, [])
        self.assertFalse(image_components([result]))

    def assert_group(self, result, names=('a-image', 'b-image')):
        self.assertEqual(result.text, 'Battle\n')
        self.assertEqual(
            [image.file for image in image_components([result])],
            [str(self.host.data_dir / (name + '.png')) for name in names],
        )
        self.assertEqual(
            [component.text for component in result.chain
             if isinstance(component, battle_module.Plain) and component.text.strip() in {'A', 'B'}],
            ['\nA\n', '\nB\n'],
        )
        self.assertIsInstance(result.chain[-1], battle_module.Plain)
        self.assertEqual(
            result.chain[-1].text,
            '\n引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。'
            if self.host.config.get('battle_allow_reveal_without_vote', False)
            else '\n引用本条回复 A / B 投票并揭晓。',
        )
        self.assertNotIn(self.code(), text([result]))

    def assert_cleanup(self):
        self.assertFalse(self.host._generation_lock.locked())
        self.assertFalse(self.host._battle_pending_outputs)
        self.assertFalse(self.host._battle_busy)
        self.assertFalse(self.host._battle_locks[self.code()].locked())
        self.assertEqual(self.host._active_generations, 0)

    async def run_battle(self, outcomes):
        async def deliver(event, result):
            self.assertIs(event, self.event)
            self.assertFalse(self.host._generation_lock.locked())
            self.assertIsNone(self.entry()['vote'])
            self.assertIsNone(self.on_disk()['vote'])
            self.assertEqual(self.entry()['platform_message_id'], '')
            self.assertEqual(self.on_disk()['platform_message_id'], '')
            self.assertTrue(self.host._battle_locks[self.code()].locked())
            self.assertTrue(self.host._battle_busy)
            self.assertEqual(
                self.host._battle_pending_outputs,
                {Path(image.file) for image in image_components([result])},
            )
            self.calls.append((event, result))
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        self.host._deliver_battle_message = deliver
        out = await collect(self.host.battle(self.event, 'prompt'))
        self.assertEqual(outcomes, [])
        self.assert_cleanup()
        return out

    async def test_confirmed_delivery_marks_votable_without_image_yields(self):
        out = await self.run_battle([None])
        self.assertEqual(len(self.calls), 1)
        self.assert_group(self.calls[0][1])
        self.assertEqual(len(out), 1)
        self.assert_ack(out[0])
        self.assertEqual(self.entry()['vote'], '')
        self.assertEqual(self.on_disk()['vote'], '')
        self.assertEqual(self.on_disk()['random_choice'], '')
        self.assertIsNone(self.on_disk()['names'])
        self.assertEqual(self.entry()['platform_message_id'], '')
        self.assertEqual(self.on_disk()['platform_message_id'], '')
        self.assertFalse(self.host._battle_pending_outputs)
        self.assertEqual(self.host.pruned, 1)

    async def test_confirmed_receipt_persists_only_native_id_and_survives_restart(self):
        self.host.client.response['sides']['a']['text'] = 'offline answer A'
        self.host.client.response['sides']['b']['text'] = 'offline answer B'
        out = await self.run_battle([battle_reply.BattleMessageReceipt('789012')])
        self.assertEqual(len(self.calls), 1)
        self.assert_group(self.calls[0][1])
        self.assertEqual(len(out), 1)
        self.assert_ack(out[0])
        entry = self.on_disk()
        self.assertEqual(set(entry), set(battle_module._SESSION_FIELDS))
        self.assertEqual(entry['context'], self.host.client.response['context'])
        self.assertEqual(entry['vote'], '')
        self.assertEqual(entry['random_choice'], '')
        self.assertIsNone(entry['names'])
        self.assertEqual(entry['platform_message_id'], '789012')
        self.assertEqual(self.entry()['platform_message_id'], '789012')
        saved = self.host._battle_sessions_file.read_text(encoding='utf-8')
        for content in ('prompt', 'a-image', 'b-image', 'offline answer A', 'offline answer B',
                        'SECRET-A', 'SECRET-B', 'sides', 'models', '引用本条回复', 'BattleMessageReceipt'):
            self.assertNotIn(content, saved)
        restarted = Host(self.temp.name, client=self.host.client)
        self.assertEqual(restarted._battle_sessions[self.code()]['platform_message_id'], '789012')
        self.assertEqual(restarted._battle_reply_code(self.event, '789012'), self.code())
        self.assertEqual(self.host.pruned, 1)
        self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])

    async def test_binding_save_failure_clears_in_memory_eligibility_and_id_without_resend(self):
        real_save = self.host._battle_save
        def fail_binding_save():
            if any(entry.get('platform_message_id') == '789012'
                   for entry in self.host._battle_sessions.values()):
                raise OSError('binding persistence unavailable')
            real_save()

        with patch.object(self.host, '_battle_save', side_effect=fail_binding_save):
            out = await self.run_battle([battle_reply.BattleMessageReceipt('789012')])
        self.assertEqual(len(self.calls), 1)
        self.assert_group(self.calls[0][1])
        self.assertEqual(len(out), 2)
        self.assert_ack(out[0])
        self.assertFalse(image_components(out))
        self.assertIn('对战失败', text(out))
        self.assertIsNone(self.entry()['vote'])
        self.assertEqual(self.entry()['platform_message_id'], '')
        self.assertIsNone(self.on_disk()['vote'])
        self.assertEqual(self.on_disk()['platform_message_id'], '')
        self.event.get_message_str = lambda: 'A'
        self.event.message_obj = SimpleNamespace(message=[battle_reply.Reply(id='789012'), battle_module.Plain('A')])
        restarted = Host(self.temp.name, {'battle_allow_reveal_without_vote': True}, self.host.client)
        for host in (self.host, restarted):
            quoted = await collect(host.battle_reply(self.event))
            self.assertIn('引用投票失败', text(quoted))
            self.assertNotIn('已投票', text(quoted))
        self.event.message_obj = SimpleNamespace(message=[])
        for host in (self.host, restarted):
            for generator in (host.battle_vote(self.event, 'A'), host.battle_reveal(self.event)):
                blocked = await collect(generator)
                self.assertIn('禁止投票', text(blocked))
        self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])

    async def test_reply_votes_for_receipt_bound_session_not_latest_or_quote_content(self):
        await self.run_battle([battle_reply.BattleMessageReceipt('789012')])
        first = self.code()
        await self.run_battle([battle_reply.BattleMessageReceipt('789013')])
        latest = self.code()
        self.assertNotEqual(first, latest)
        self.assertEqual(len(self.calls), 2)
        self.event.get_message_str = lambda: ' A '
        self.event.message_obj = SimpleNamespace(message=[
            battle_reply.Reply(id='789012', chain=[battle_module.Plain('B 不投票 ' + latest)]),
            battle_module.Plain(' A '),
        ])
        voted = await collect(self.host.battle_reply(self.event))
        self.assertEqual(text(voted), '已投票 A。\nBattle 已揭晓\nA：Real A\nB：Real B')
        self.assertNotIn(first, text(voted))
        self.assertNotIn(latest, text(voted))
        records = json.loads(self.host._battle_sessions_file.read_text(encoding='utf-8'))
        self.assertEqual(records[first]['vote'], 'a')
        self.assertEqual(records[latest]['vote'], '')
        self.assertEqual([x[2] for x in self.host.client.calls if x[0] == 'vote'], ['a'])
        self.assertEqual(len([x for x in self.host.client.calls if x[0] == 'reveal']), 1)
        restarted = Host(self.temp.name, client=self.host.client)
        self.event.get_message_str = lambda: '不投票'
        self.event.message_obj.message[-1] = battle_module.Plain('不投票')
        with (patch.object(restarted, '_battle_client', side_effect=AssertionError('cache only')),
              patch.object(battle_module.secrets, 'choice') as choose):
            revealed = await collect(restarted.battle_reply(self.event))
        choose.assert_not_called()
        self.assertEqual(text(revealed), 'Battle 已揭晓\nA：Real A\nB：Real B')
        self.assertEqual([x[2] for x in self.host.client.calls if x[0] == 'vote'], ['a'])

    async def test_invalid_confirmed_receipt_id_fails_closed_without_resend(self):
        for native_id in ('', '0', True, 'not-an-id', '1' * 21,
                          'https://offline.invalid/image?token=do-not-echo'):
            with self.subTest(native_id=native_id):
                self.calls.clear()
                out = await self.run_battle([battle_reply.BattleMessageReceipt(native_id)])
                self.assertEqual(len(self.calls), 1)
                self.assert_group(self.calls[0][1])
                self.assertEqual(len(out), 2)
                self.assert_ack(out[0])
                self.assertFalse(image_components(out))
                self.assertIn('平台消息标识无效', text(out))
                self.assertNotIn('do-not-echo', text(out))
                self.assertIsNone(self.entry()['vote'])
                self.assertIsNone(self.on_disk()['vote'])
                self.assertEqual(self.on_disk()['platform_message_id'], '')
                for generator in (self.host.battle_vote(self.event, 'A'),
                                  self.host.battle_reveal(self.event)):
                    blocked = await collect(generator)
                    self.assertIn('禁止投票', text(blocked))
        self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])

    async def test_uncertain_with_text_cannot_vote_or_yield_same_image(self):
        self.host.client.response['sides']['a']['text'] = 'text does not confirm image'
        failure = _plugin_module()._ImageDeliveryFailure('private-model token=do-not-echo', 1, True)
        out = await self.run_battle([failure])
        self.assertEqual(len(self.calls), 1)
        self.assert_group(self.calls[0][1])
        self.assertIn('text does not confirm image', text([self.calls[0][1]]))
        self.assertEqual(len(out), 2)
        self.assert_ack(out[0])
        self.assertFalse(image_components(out))
        self.assertIn('发送回执不确定', text(out))
        self.assertNotIn('private-model', text(out))
        self.assertNotIn('do-not-echo', text(out))
        self.assertIsNone(self.entry()['vote'])
        self.assertIsNone(self.on_disk()['vote'])
        restarted = Host(self.temp.name, {'battle_allow_reveal_without_vote': True}, self.host.client)
        for host in (self.host, restarted):
            blocked = await collect(host.battle_vote(self.event, 'A'))
            self.assertIn('禁止投票', text(blocked))
            blocked = await collect(host.battle_reveal(self.event))
            self.assertIn('禁止投票', text(blocked))
        self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])

    async def test_rejection_and_unknown_returns_never_trigger_fallback(self):
        for failure in (_plugin_module()._ImageDeliveryFailure('rejected', 3, False),
                        False, 'legacy-error', object(), True, 0, [], {}, {'message_id': 789012}):
            with self.subTest(outcome=type(failure).__name__):
                self.calls.clear()
                out = await self.run_battle([failure])
                self.assertEqual(len(self.calls), 1)
                self.assert_group(self.calls[0][1])
                self.assertEqual(len(out), 2)
                self.assert_ack(out[0])
                self.assertFalse(image_components(out))
                self.assertIn('本场禁止投票', text(out))
                reason = '平台明确拒收图片' if getattr(failure, 'uncertain', True) is False else '发送回执不确定'
                self.assertIn(reason, text(out))
                self.assertIsNone(self.entry()['vote'])
                self.assertIsNone(self.on_disk()['vote'])
                # Each iteration must check its latest session, not an earlier failure.
                self.assertIsNone(self.host._battle_sessions[self.code()]['vote'])
                blocked = await collect(self.host.battle_vote(self.event, 'A'))
                self.assertIn('禁止投票', text(blocked))
        self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])

    async def test_raised_delivery_error_is_uncertain_and_never_resends(self):
        out = await self.run_battle([TimeoutError('private-model token=do-not-echo')])
        self.assertEqual(len(self.calls), 1)
        self.assert_group(self.calls[0][1])
        self.assertEqual(len(out), 2)
        self.assert_ack(out[0])
        self.assertFalse(image_components(out))
        self.assertIn('发送回执不确定', text(out))
        self.assertNotIn('private-model', text(out))
        self.assertNotIn('do-not-echo', text(out))
        self.assertIsNone(self.on_disk()['vote'])
        blocked = await collect(self.host.battle_vote(self.event, 'B'))
        self.assertIn('禁止投票', text(blocked))
        self.assertFalse([x for x in self.host.client.calls if x[0] == 'vote'])

    async def test_no_direct_sender_yields_one_group_after_lock_release(self):
        async def fallback(event, result):
            self.assertIs(event, self.event)
            self.assertFalse(self.host._generation_lock.locked())
            self.assertIsNone(self.entry()['vote'])
            self.assertIsNone(self.on_disk()['vote'])
            self.calls.append((event, result))
            return ''
        self.host._deliver_battle_message = fallback
        generator = self.host.battle(self.event, 'prompt')
        with patch.object(
            self.host, '_collect_input_images', wraps=self.host._collect_input_images,
        ) as collect_images:
            ack = await anext(generator)
            collect_images.assert_not_awaited()
        self.assert_ack(ack)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.client.calls, [])
        self.assertEqual(self.host.materialized, [])
        self.assertFalse(self.host._battle_sessions)
        self.assertFalse(self.host._battle_latest)
        self.assertFalse(self.host._battle_pending_outputs)
        self.assertFalse(self.host._generation_lock.locked())
        comparison = await anext(generator)
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0][1], comparison)
        self.assert_group(comparison)
        self.assertEqual(len(image_components([comparison])), 2)
        self.assertIsNone(self.on_disk()['vote'])
        self.assertEqual(
            self.host._battle_pending_outputs,
            {Path(image.file) for image in image_components([comparison])},
        )
        self.assertTrue(self.host._battle_locks[self.code()].locked())
        await generator.aclose()
        self.assertIsNone(self.entry()['vote'])
        self.assertIsNone(self.on_disk()['vote'])
        self.assert_cleanup()
        restarted = Host(self.temp.name, client=self.host.client)
        blocked = await collect(restarted.battle_vote(self.event, 'A'))
        self.assertIn('禁止投票', text(blocked))
        self.assertFalse([x for x in self.host.client.calls if x[0] == 'vote'])

    async def test_fallback_vote_eligibility_waits_for_group_yield_to_resume(self):
        async def fallback(event, result):
            self.assertFalse(self.host._generation_lock.locked())
            self.assertIsNone(self.entry()['vote'])
            self.assertIsNone(self.on_disk()['vote'])
            self.calls.append((event, result))
            return ''
        self.host._deliver_battle_message = fallback
        generator = self.host.battle(self.event, 'prompt')
        with patch.object(
            self.host, '_collect_input_images', wraps=self.host._collect_input_images,
        ) as collect_images:
            ack = await anext(generator)
            collect_images.assert_not_awaited()
        self.assert_ack(ack)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.host.client.calls, [])
        self.assertEqual(self.host.materialized, [])
        self.assertFalse(self.host._battle_sessions)
        self.assertFalse(self.host._battle_latest)
        self.assertFalse(self.host._battle_pending_outputs)
        self.assertFalse(self.host._generation_lock.locked())
        comparison = await anext(generator)
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0][1], comparison)
        self.assert_group(comparison)
        self.assertIsNone(self.entry()['vote'])
        self.assertIsNone(self.on_disk()['vote'])
        self.assertTrue(self.host._battle_busy)
        self.assertTrue(self.host._battle_pending_outputs)
        self.assertTrue(self.host._battle_locks[self.code()].locked())
        with self.assertRaises(StopAsyncIteration):
            await anext(generator)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.entry()['vote'], '')
        self.assertEqual(self.on_disk()['vote'], '')
        self.assert_cleanup()
        self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])

    async def test_uncertain_group_with_multiple_candidates_does_not_resend(self):
        self.host.config['max_output_images'] = 2
        self.host.client.response['sides']['a']['images'] = ['a-image', 'another-a', 'unused-a']
        self.host.client.response['sides']['b']['images'] = ['b-image', 'another-b', 'unused-b']
        out = await self.run_battle([_plugin_module()._ImageDeliveryFailure('timeout', 1, True)])
        self.assertEqual(len(self.calls), 1)
        self.assert_group(self.calls[0][1], ('a-image', 'another-a', 'b-image', 'another-b'))
        self.assertEqual(len(image_components([self.calls[0][1]])), 4)
        self.assertEqual(self.host.materialized, ['a-image', 'another-a', 'b-image', 'another-b'])
        self.assertEqual(len(out), 2)
        self.assert_ack(out[0])
        self.assertFalse(image_components(out))
        self.assertIn('发送回执不确定', text(out))
        self.assertIsNone(self.on_disk()['vote'])

    async def test_another_generation_can_start_during_slow_delivery(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def deliver(event, result):
            self.assertIs(event, self.event)
            self.assertFalse(self.host._generation_lock.locked())
            self.assertIsNone(self.on_disk()['vote'])
            self.calls.append((event, result))
            started.set()
            await release.wait()
            return None
        self.host._deliver_battle_message = deliver
        first = asyncio.create_task(collect(self.host.battle(self.event, 'first')))
        try:
            await _wait_for_barrier(started, first)
            self.assertEqual(len(self.calls), 1)
            self.assert_group(self.calls[0][1])
            async with self.host._generation_lock:
                self.assertEqual(
                    self.host._battle_pending_outputs,
                    {Path(image.file) for image in image_components([self.calls[0][1]])},
                )
                self.assertTrue(self.host._battle_busy)
                self.assertTrue(self.host._battle_locks[self.code()].locked())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertIsNone(self.entry()['vote'])
            self.assertIsNone(self.on_disk()['vote'])
            self.assert_cleanup()
        finally:
            if not first.done():
                first.cancel()
                try:
                    await asyncio.wait_for(first, timeout=5)
                except asyncio.CancelledError:
                    pass

    async def test_vote_waits_for_group_delivery_confirmation(self):
        started, release, vote_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def deliver(event, result):
            self.assertFalse(self.host._generation_lock.locked())
            self.assertIsNone(self.entry()['vote'])
            self.assertIsNone(self.on_disk()['vote'])
            self.calls.append((event, result))
            started.set()
            await release.wait()
            return None
        self.host._deliver_battle_message = deliver
        battle = asyncio.create_task(collect(self.host.battle(self.event, 'prompt')))
        vote = None
        try:
            await _wait_for_barrier(started, battle)
            guard = self.host._battle_guard
            def signal_guard(code):
                vote_waiting.set()
                return guard(code)
            with patch.object(self.host, '_battle_guard', side_effect=signal_guard):
                vote = asyncio.create_task(collect(self.host.battle_vote(self.event, 'A')))
                await _wait_for_barrier(vote_waiting, vote)
                self.assertFalse(vote.done())
                self.assertFalse([x for x in self.host.client.calls if x[0] in {'vote', 'reveal'}])
                self.assertIsNone(self.on_disk()['vote'])
                release.set()
                out = await asyncio.wait_for(battle, timeout=5)
                self.assertEqual(len(out), 1)
                self.assert_ack(out[0])
                voted = await asyncio.wait_for(vote, timeout=5)
            self.assertEqual(len(self.calls), 1)
            self.assert_group(self.calls[0][1])
            self.assertIn('已投票 A', text(voted))
            self.assertEqual([x[0] for x in self.host.client.calls], ['begin', 'vote', 'reveal'])
            self.assertEqual(self.on_disk()['vote'], 'a')
            self.assert_cleanup()
        finally:
            for task in (battle, vote):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=5)
                    except asyncio.CancelledError:
                        pass

class LifecycleAndOnlineDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_and_userscript_both_start_and_stop(self):
        main,p=_make_plugin(self, {'transport_mode':'userscript'})
        relay=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),host='offline',port=1,prefix='/test',public_url='')
        p._relay=relay
        p._openai_proxy=SimpleNamespace(_enabled=lambda:True,start=AsyncMock(),stop=AsyncMock(),address='offline')
        await p.initialize()
        relay.start.assert_awaited_once();p._openai_proxy.start.assert_awaited_once()
        await p.terminate()
        relay.stop.assert_awaited_once();p._openai_proxy.stop.assert_awaited_once()
        self.assertIsNone(p._relay)

    async def test_openai_failure_still_stops_relay_and_relay_start_failure_does_not_skip_openai(self):
        _,p=_make_plugin(self, {'transport_mode':'userscript'})
        relay=SimpleNamespace(start=AsyncMock(side_effect=bridge_client.BridgeError('offline bind failure')),stop=AsyncMock())
        p._relay=relay
        p._openai_proxy=SimpleNamespace(_enabled=lambda:True,start=AsyncMock(),stop=AsyncMock(side_effect=RuntimeError('cleanup')),address='offline')
        await p.initialize()
        p._openai_proxy.start.assert_awaited_once()
        with self.assertRaises(RuntimeError):await p.terminate()
        relay.stop.assert_awaited_once()

    async def test_real_online_helper_timeout_is_uncertain_not_retried(self):
        main,p=_make_plugin(self, {'send_retry_delay':0})
        path=p.output_dir/'offline.png';path.write_bytes(b'offline')
        event=SimpleNamespace(send=AsyncMock(side_effect=TimeoutError('receipt timeout')))
        with patch.object(main,'prepare_qq_image_sender',return_value=None):
            result=await p._deliver_image(event,path,'anonymous A')
        self.assertIsInstance(result,main._ImageDeliveryFailure)
        self.assertTrue(result.uncertain);self.assertEqual(result.attempts,1)
        event.send.assert_awaited_once()

    async def test_real_online_helper_only_retries_explicit_qq_rejection(self):
        main,p=_make_plugin(self, {'send_retry_delay':0})
        path=p.output_dir/'offline.png';path.write_bytes(b'offline')
        rejection=RuntimeError('EventChecker failed NodeIKernelMsgService/sendMsg result:120')
        event=SimpleNamespace(send=AsyncMock(side_effect=[rejection, None]))
        with patch.object(main,'prepare_qq_image_sender',return_value=None):
            result=await p._deliver_image(event,path,'anonymous A')
        self.assertIsNone(result);self.assertEqual(event.send.await_count,2)

    async def test_pending_battle_outputs_not_pruned_by_other_generations(self):
        _,p=_make_plugin(self, {'max_saved_outputs':4})
        path=p.output_dir/'pending.png';path.write_bytes(b'pending')
        p._battle_pending_outputs.add(path)
        for i in range(8):(p.output_dir/f'other-{i}.png').write_bytes(b'other')
        p._prune_outputs()
        self.assertTrue(path.exists())

class GroupedDeliveryHelperTests(unittest.IsolatedAsyncioTestCase):
    def group(self, plugin, event):
        paths = [plugin.output_dir / (name + '.png') for name in ('a1', 'a2', 'b1', 'b2')]
        for path in paths:
            path.write_bytes(b'offline image fixture')
        sides = [
            {'paths': paths[:2], 'text': 'answer A', 'error': False, 'had_images': True},
            {'paths': paths[2:], 'text': 'answer B', 'error': False, 'had_images': True},
        ]
        result, eligible = plugin._battle_comparison_result(event, 'ABC123', sides)
        self.assertTrue(eligible)
        self.assertEqual(result.text, 'Battle\n')
        # Shared FakeResult stores the header separately; native plain_result
        # puts that Plain component in the chain handed to event.send.
        result.chain.insert(0, battle_module.Plain(result.text))
        footer = ('\n引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。'
                  if plugin.config.get('battle_allow_reveal_without_vote', False)
                  else '\n引用本条回复 A / B 投票并揭晓。')
        self.assertEqual(self.signature(result.chain), [
            ('plain', 'Battle\n'), ('plain', '\nA\n'),
            ('image', str(paths[0])), ('image', str(paths[1])), ('plain', 'answer A\n'),
            ('plain', '\nB\n'), ('image', str(paths[2])), ('image', str(paths[3])),
            ('plain', 'answer B\n'), ('plain', footer),
        ])
        self.assertNotIn('ABC123', text([result]))
        return result

    def signature(self, components):
        signature = []
        for component in components:
            if isinstance(component, battle_module.Plain):
                signature.append(('plain', component.text))
            elif isinstance(component, battle_module.Image):
                signature.append(('image', component.file))
            else:
                self.fail(f'unexpected grouped component: {type(component).__name__}')
        return signature

    def sender(self, main, result, outcomes):
        expected, seen = self.signature(result.chain), []
        async def send(message):
            self.assertIsInstance(message, main.MessageChain)
            seen.append(self.signature(message.chain))
            self.assertEqual(seen[-1], expected)
            self.assertEqual(len(image_components([message])), 4)
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return AsyncMock(side_effect=send), seen, expected

    def qq_event(self, main, result, outcomes):
        from astrbot.api.event import AstrMessageEvent

        expected = self.signature(result.chain)
        messages = [{'type': kind, 'value': value} for kind, value in expected]
        calls = []
        case = self

        class API:
            def __init__(self):
                self._timeout_sec = 10
                self.connection = object()

            async def call_action(scoped, action, **kwargs):
                case.assertIsNot(scoped, original)
                case.assertIs(scoped.connection, original.connection)
                case.assertEqual(scoped._timeout_sec, 105)
                case.assertEqual(action, 'send_group_msg')
                case.assertEqual(kwargs['group_id'], 123456)
                case.assertEqual(kwargs['timeout'], 90000)
                case.assertEqual(kwargs['message'], messages)
                calls.append((action, kwargs))
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        async def parse(chain):
            self.assertIsInstance(chain, main.MessageChain)
            self.assertEqual(self.signature(chain.chain), expected)
            return messages

        original = API()
        event_type = type('OfflineQQEvent', (AstrMessageEvent,), {})
        event = event_type()
        event.bot = SimpleNamespace(_api=original)
        event.message_obj = SimpleNamespace(raw_message={})
        event.get_group_id = lambda: '123456'
        event.get_sender_id = lambda: '654321'
        event._parse_onebot_json = AsyncMock(side_effect=parse)
        event.send = AsyncMock(side_effect=AssertionError('QQ must not fall back to event.send'))
        module = ModuleType('astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event')
        module.AiocqhttpMessageEvent = event_type
        return event, module, original, calls

    async def test_real_scoped_qq_success_returns_receipt_with_native_id(self):
        main, plugin = _make_plugin(self, {'qq_image_send_timeout': 90, 'send_max_attempts': 3})
        for native_id, enabled in ((789012, False), ('-789013', True)):
            with self.subTest(native_id=native_id, enabled=enabled):
                plugin.config['battle_allow_reveal_without_vote'] = enabled
                result = self.group(plugin, Event())
                expected = self.signature(result.chain)
                outcomes = [{'message_id': native_id}]
                event, module, original, calls = self.qq_event(main, result, outcomes)
                bookkeeping = AsyncMock()
                with (patch.dict(sys.modules, {module.__name__: module}),
                      patch.object(module.AiocqhttpMessageEvent.__bases__[0], 'send',
                                   bookkeeping, create=True),
                      patch.object(main, 'prepare_qq_image_sender',
                                   wraps=qq_delivery.prepare_qq_image_sender) as prepare):
                    outcome = await plugin._deliver_battle_message(event, result)
                self.assertIsInstance(outcome, battle_reply.BattleMessageReceipt)
                self.assertEqual(outcome.message_id, str(native_id))
                prepare.assert_called_once_with(event, 90.0)
                self.assertEqual(len(calls), 1)
                event._parse_onebot_json.assert_awaited_once()
                event.send.assert_not_awaited()
                bookkeeping.assert_awaited_once()
                self.assertIs(bookkeeping.await_args.args[0], event)
                self.assertEqual(self.signature(bookkeeping.await_args.args[1].chain), expected)
                self.assertEqual(original._timeout_sec, 10)
                self.assertEqual(outcomes, [])
                self.assertEqual(self.signature(result.chain), expected)

    async def test_real_scoped_qq_missing_or_invalid_native_id_is_uncertain_without_fallback_or_retry(self):
        main, plugin = _make_plugin(self, {'qq_image_send_timeout': 90, 'send_max_attempts': 3})
        for receipt, acknowledged in ((None, False), ({}, False), ({'status': 'async'}, False),
                                      ({'message_id': 0}, False), ({'message_id': True}, True),
                                      ({'message_id': 'not-an-id'}, True),
                                      ({'message_id': '1' * 21}, True),
                                      ({'message_id': 'https://offline.invalid/image?token=do-not-echo'}, True)):
            with self.subTest(receipt=receipt):
                result = self.group(plugin, Event())
                expected = self.signature(result.chain)
                outcomes = [receipt, {'message_id': 789012}]
                event, module, original, calls = self.qq_event(main, result, outcomes)
                bookkeeping = AsyncMock()
                with (patch.dict(sys.modules, {module.__name__: module}),
                      patch.object(module.AiocqhttpMessageEvent.__bases__[0], 'send',
                                   bookkeeping, create=True),
                      patch.object(main, 'prepare_qq_image_sender',
                                   wraps=qq_delivery.prepare_qq_image_sender)):
                    outcome = await plugin._deliver_battle_message(event, result)
                self.assertIsInstance(outcome, main._ImageDeliveryFailure)
                self.assertTrue(outcome.uncertain)
                self.assertEqual(outcome.attempts, 1)
                self.assertEqual(len(calls), 1)
                event._parse_onebot_json.assert_awaited_once()
                event.send.assert_not_awaited()
                if acknowledged:
                    bookkeeping.assert_awaited_once()
                else:
                    bookkeeping.assert_not_awaited()
                self.assertNotIn('do-not-echo', outcome.message)
                self.assertEqual(original._timeout_sec, 10)
                self.assertEqual(outcomes, [{'message_id': 789012}])
                self.assertEqual(self.signature(result.chain), expected)

    async def test_confirmed_group_sends_once_with_header_labels_text_images_and_footer(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 3, 'send_retry_delay': 0})
        event = Event()
        result = self.group(plugin, event)
        outcomes = [None]
        event.send, seen, expected = self.sender(main, result, outcomes)
        with patch.object(main, 'prepare_qq_image_sender', return_value=None):
            outcome = await plugin._deliver_battle_message(event, result)
        self.assertIsNone(outcome)
        event.send.assert_awaited_once()
        self.assertEqual(seen, [expected])
        self.assertEqual(outcomes, [])
        self.assertEqual(self.signature(result.chain), expected)

    async def test_group_timeout_is_uncertain_and_never_retries_or_drops_components(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 3, 'send_retry_delay': 0})
        event = Event()
        result = self.group(plugin, event)
        outcomes = [TimeoutError('private-model token=do-not-echo'), None]
        event.send, seen, expected = self.sender(main, result, outcomes)
        with (patch.object(main, 'prepare_qq_image_sender', return_value=None),
              self.assertLogs('arena-image-test', level='WARNING') as logs):
            outcome = await plugin._deliver_battle_message(event, result)
        self.assertIsInstance(outcome, main._ImageDeliveryFailure)
        self.assertTrue(outcome.uncertain)
        self.assertEqual(outcome.attempts, 1)
        event.send.assert_awaited_once()
        self.assertEqual(seen, [expected])
        self.assertEqual(outcomes, [None])
        self.assertEqual(self.signature(result.chain), expected)
        for hidden in ('private-model', 'do-not-echo', 'answer A', 'answer B', 'ABC123'):
            self.assertNotIn(hidden, outcome.message)
            self.assertNotIn(hidden, '\n'.join(logs.output))

    async def test_explicit_qq_rejection_retries_the_complete_group_without_image_only_plans(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 3, 'send_retry_delay': 0})
        event = Event()
        result = self.group(plugin, event)
        rejection = RuntimeError('EventChecker failed NodeIKernelMsgService/sendMsg result:120')
        outcomes = [rejection, {'message_id': 234567}]
        qq_sender, seen, expected = self.sender(main, result, outcomes)
        event.send = AsyncMock(side_effect=AssertionError('must not fall back after a QQ send'))
        with (patch.object(main, 'prepare_qq_image_sender', return_value=qq_sender),
              patch.object(plugin, '_send_plans', side_effect=AssertionError('single-model plans')) as plans,
              patch.object(plugin, '_smaller_send_copy', side_effect=AssertionError('must keep originals')) as smaller):
            outcome = await plugin._deliver_battle_message(event, result)
        self.assertIsInstance(outcome, battle_reply.BattleMessageReceipt)
        self.assertEqual(outcome.message_id, '234567')
        self.assertEqual(qq_sender.await_count, 2)
        event.send.assert_not_awaited()
        self.assertEqual(seen, [expected, expected])
        self.assertEqual(outcomes, [])
        self.assertEqual(self.signature(result.chain), expected)
        plans.assert_not_called()
        smaller.assert_not_called()

    async def test_exhausted_qq_rejections_fail_closed_with_all_components_on_each_attempt(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 2, 'send_retry_delay': 0})
        event = Event()
        result = self.group(plugin, event)
        rejection = RuntimeError('EventChecker failed NodeIKernelMsgService/sendMsg result:120')
        outcomes = [rejection, rejection]
        event.send, seen, expected = self.sender(main, result, outcomes)
        with patch.object(main, 'prepare_qq_image_sender', return_value=None):
            outcome = await plugin._deliver_battle_message(event, result)
        self.assertIsInstance(outcome, main._ImageDeliveryFailure)
        self.assertFalse(outcome.uncertain)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(event.send.await_count, 2)
        self.assertEqual(seen, [expected, expected])
        self.assertEqual(outcomes, [])
        self.assertEqual(self.signature(result.chain), expected)

    async def test_timeout_after_rejection_stops_before_another_group_attempt(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 3, 'send_retry_delay': 0})
        event = Event()
        result = self.group(plugin, event)
        rejection = RuntimeError('EventChecker failed NodeIKernelMsgService/sendMsg result:120')
        outcomes = [rejection, TimeoutError('receipt timeout'), None]
        event.send, seen, expected = self.sender(main, result, outcomes)
        with patch.object(main, 'prepare_qq_image_sender', return_value=None):
            outcome = await plugin._deliver_battle_message(event, result)
        self.assertIsInstance(outcome, main._ImageDeliveryFailure)
        self.assertTrue(outcome.uncertain)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(event.send.await_count, 2)
        self.assertEqual(seen, [expected, expected])
        self.assertEqual(outcomes, [None])

    async def test_unknown_errors_and_retcode_1200_do_not_retry_the_group(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 3, 'send_retry_delay': 0})
        for failure in (ConnectionError('connection lost'), RuntimeError('unknown transport failure'),
                        RuntimeError('sendMsg retcode:1200')):
            with self.subTest(error=type(failure).__name__):
                event = Event()
                result = self.group(plugin, event)
                outcomes = [failure, None]
                event.send, seen, expected = self.sender(main, result, outcomes)
                with patch.object(main, 'prepare_qq_image_sender', return_value=None):
                    outcome = await plugin._deliver_battle_message(event, result)
                self.assertIsInstance(outcome, main._ImageDeliveryFailure)
                self.assertTrue(outcome.uncertain)
                self.assertEqual(outcome.attempts, 1)
                event.send.assert_awaited_once()
                self.assertEqual(seen, [expected])
                self.assertEqual(outcomes, [None])

    async def test_only_missing_direct_sender_returns_empty_fallback(self):
        main, plugin = _make_plugin(self)
        event = Event()
        result = self.group(plugin, event)
        expected = self.signature(result.chain)
        with patch.object(main, 'prepare_qq_image_sender', return_value=None) as prepare:
            self.assertEqual(await plugin._deliver_battle_message(event, result), '')
            prepare.assert_not_called()
            for invalid in (False, '', object()):
                with self.subTest(sender=type(invalid).__name__):
                    event.send = invalid
                    outcome = await plugin._deliver_battle_message(event, result)
                    self.assertIsInstance(outcome, main._ImageDeliveryFailure)
                    self.assertTrue(outcome.uncertain)
                    self.assertEqual(outcome.attempts, 0)
                    prepare.assert_not_called()
        self.assertEqual(self.signature(result.chain), expected)

    async def test_group_sender_cancellation_propagates_without_retry(self):
        main, plugin = _make_plugin(self, {'send_max_attempts': 3, 'send_retry_delay': 0})
        event = Event()
        result = self.group(plugin, event)
        outcomes = [asyncio.CancelledError(), None]
        event.send, seen, expected = self.sender(main, result, outcomes)
        with (patch.object(main, 'prepare_qq_image_sender', return_value=None),
              self.assertRaises(asyncio.CancelledError)):
            await plugin._deliver_battle_message(event, result)
        event.send.assert_awaited_once()
        self.assertEqual(seen, [expected])
        self.assertEqual(outcomes, [None])
        self.assertEqual(self.signature(result.chain), expected)

    async def test_command_prefix_compatibility_and_compact_footer_independence(self):
        _, plugin = _make_plugin(self, {'wake_prefix': ['!']})
        event = Event()
        for core, prefix in (({'wake_prefix': ['/', ',']}, ','), ({'wake_prefix': ','}, ','),
                             ({'wake_prefix': ['', '']}, '/'), ({'wake_prefix': []}, '/'),
                             ({}, '/'), ({'wake_prefix': None}, '/'),
                             ({'wake_prefix': ['!']}, '!')):
            for enabled in (False, True):
                with self.subTest(core=core, enabled=enabled):
                    origins = []
                    def get_config(umo=None):
                        origins.append(umo)
                        return core
                    plugin.context = SimpleNamespace(get_config=get_config)
                    plugin.config['battle_allow_reveal_without_vote'] = enabled
                    self.assertEqual(plugin._battle_command_prefix(event), prefix)
                    self.assertEqual(origins, [event.unified_msg_origin])
                    with patch.object(plugin, '_battle_prefix',
                                      side_effect=AssertionError('footer must not read command prefix')) as legacy_prefix:
                        result, eligible = plugin._battle_comparison_result(event, 'ABC123', [
                            {'paths': [], 'text': 'answer A', 'error': False, 'had_images': False},
                            {'paths': [], 'text': 'answer B', 'error': False, 'had_images': False},
                        ])
                    legacy_prefix.assert_not_called()
                    self.assertTrue(eligible)
                    self.assertEqual(origins, [event.unified_msg_origin])
                    self.assertEqual(result.text, 'Battle\n')
                    self.assertEqual(result.chain[-1].text,
                                     '\n引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。'
                                     if enabled else '\n引用本条回复 A / B 投票并揭晓。')
                    self.assertNotIn('ABC123', text([result]))
                    self.assertNotIn('battle投票 A/B', text([result]))
                    vote = await collect(plugin.battle_vote(event, 'invalid'))
                    reveal = await collect(plugin.battle_reveal(event, 'too many'))
                    switch = await collect(plugin.battle_reveal_switch(event, 'invalid'))
                    self.assertEqual(text(vote), f'用法：{prefix}battle投票 A/B [编号]')
                    self.assertEqual(text(reveal),
                                     f'用法：{prefix}battle揭晓 [编号] 或 {prefix}battle不投票 [编号]')
                    self.assertEqual(text(switch), f'用法：{prefix}battle揭晓开关 开/关/查询（管理员）')
        def unavailable(umo=None):
            raise RuntimeError('core config unavailable')
        for context in (object(), SimpleNamespace(get_config=unavailable)):
            plugin.context = context
            self.assertEqual(plugin._battle_command_prefix(event), '/')


class OnlinePreservationTests(unittest.TestCase):
    def test_sensitive_online_methods_are_ast_identical(self):
        baseline = _preservation_fixture()["online_baseline"]["class_methods"]
        old = baseline["main.py"]["ArenaImagePlugin"]
        new = _class_method_fingerprints(STAGE / "main.py", "ArenaImagePlugin")
        for name in ['_deliver_image','_send_plans','_smaller_send_copy','_materialize_output','_fetch_model_health','_fetch_models','_model_health_text','_model_list_text']:
            self.assertEqual(old[name],new[name], name)
        old = baseline["arena_direct.py"]["ArenaDirectClient"]
        new = _class_method_fingerprints(STAGE / "arena_direct.py", "ArenaDirectClient")
        for name in ['_raise_upstream','_generate','_require_session','_auth_message','model_name_catalog','_resolve','_pick_variant']:
            self.assertEqual(old[name],new[name],name)
        old = baseline["arena_direct.py"]["ArenaPage"]
        new = _class_method_fingerprints(STAGE / "arena_direct.py", "ArenaPage")
        for name in ['refresh_session','cookies','model_table','reload','mint_recaptcha']:
            self.assertEqual(old[name],new[name],name)

    def test_schema_only_adds_relay_battle_and_userscript_option(self):
        old = _preservation_fixture()["online_baseline"]["schema_key_sha256"]
        new=json.loads((STAGE/'_conf_schema.json').read_text(encoding='utf-8'))
        for name, digest in old.items():
            if name != 'transport_mode':
                self.assertEqual(digest, _canonical_json_sha256(new[name]), name)
        self.assertEqual(new['transport_mode']['options'],['bridge','direct','userscript'])
        self.assertEqual(len([n for n in new if n.startswith('battle_')]),3)
        self.assertTrue(new['relay_secret']['secret'])
        _assert_online_readme_prefix(self, (STAGE / 'README.md').read_bytes())

    def test_stable_browser_scope_drops_url_credentials_query_fragment(self):
        first=arena_direct.cdp_browser_scope('http://user:secret@offline.invalid:9223/?token=secret#x')
        second=arena_direct.cdp_browser_scope('http://offline.invalid:9223')
        self.assertEqual(first,second);self.assertNotIn('secret',first)

    def test_qq_api_timeout_copy_preserves_original_and_connections(self):
        class API:pass
        original=API();original._timeout_sec=10;original.connection=object()
        scoped=qq_delivery._scoped_api(original,120)
        self.assertEqual(original._timeout_sec,10);self.assertEqual(scoped._timeout_sec,120)
        self.assertIs(scoped.connection,original.connection)

class HistoryAndCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_adapter_and_browser_scoping_preserve_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            relay=arena_relay.RelayServer(host='127.0.0.1',port=1,secret='offline-fixture-secret',data_dir=directory)
            client=arena_relay.ArenaUserscriptClient(relay,data_dir=directory)
            a=arena_relay.RelayPage(relay,arena_relay.RelayBrowser('offline-a'))
            b=arena_relay.RelayPage(relay,arena_relay.RelayBrowser('offline-b'))
            a.fetch=AsyncMock(return_value={'status':200,'headers':{},'text':json.dumps({'entries':[], 'pagination':{'hasMore':False},'cookie':'do-not-keep'})})
            ca=client._history_for_page(a);cb=client._history_for_page(b)
            self.assertIsNot(ca,cb);self.assertNotEqual(ca.path,cb.path)
            read=await ca._read(a,'/api/history/unified?limit=50',10)
            self.assertNotIn('cookie',read['body'])
            self.assertEqual(read['body']['entries'],[])
            with self.assertRaises(ValueError):await ca._read(a,'/api/not-allowed',10)
            self.assertEqual(a.fetch.await_count,1)

    async def test_invalidation_is_scoped_and_keeps_other_browser(self):
        arena_direct.reset_caches();arena_direct._LOCK=asyncio.Lock()
        a=SimpleNamespace(cache_key='offline-a');b=SimpleNamespace(cache_key='offline-b')
        for page in (a,b):arena_direct.cache_scope(page).update(actions={'a':'id'},actions_at=1,models=[{'id':'model'}],models_at=1)
        await arena_direct._invalidate_deployment_caches(a)
        self.assertFalse(arena_direct.cache_scope(a)['actions'])
        self.assertEqual(arena_direct.cache_scope(a)['models'],[{'id':'model'}])
        self.assertEqual(arena_direct.cache_scope(b)['actions'],{'a':'id'})

    async def test_explicit_409_refresh_preserves_ids_and_only_refreshes_token(self):
        page=arena_direct.ArenaPage(None,'session','https://arena.ai/text/direct',cache_key='offline')
        page.evaluate=AsyncMock(side_effect=[{'status':409,'headers':{'x-arena-refresh-required':'1'},'text':'{}'}, {'status':200,'headers':{},'text':'done'}])
        page.reload=AsyncMock(return_value=True);page.mint_recaptcha=AsyncMock(return_value='new-offline-token')
        body={'id':'same-evaluation','modelAMessageId':'same-message','recaptchaV3Token':'old-offline-token'}
        with patch.object(arena_direct,'_live_state',AsyncMock(return_value={'has_arena_auth':True,'has_logged_in':True,'recaptcha_action':'chat_submit'})):
            out=await page.fetch(arena_direct.STREAM_CREATE_EVALUATION_PATH,body=json.dumps(body))
        self.assertEqual(out['status'],200);page.reload.assert_awaited_once()
        second=page.evaluate.await_args_list[1].args[0]
        self.assertIn('same-evaluation',second);self.assertIn('same-message',second)
        self.assertIn('new-offline-token',second);self.assertNotIn('old-offline-token',second)


class RandomRevealTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=STAGE.parent)
        self.addCleanup(self.temp.cleanup)
        self.host = Host(self.temp.name, {"battle_allow_reveal_without_vote": True})
        self.event = Event()

    async def begin(self, *, host=None, event=None):
        host = host or self.host
        event = event or self.event
        results = await collect(host.battle(event, "offline comparison"))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].text, ACK)
        self.assertEqual(results[0].chain, [])
        self.assertFalse(image_components(results[:1]))
        code = host._battle_latest[(event.unified_msg_origin, event.owner)]
        return code, results

    async def optout(self, code="", *, host=None, event=None):
        # The actual command wrapper handles both /battle揭晓 and its alias.
        return await collect(_plugin_module().ArenaImagePlugin.battle_reveal(
            host or self.host, event or self.event, code
        ))

    def calls(self, kind):
        return [call for call in self.host.client.calls if call[0] == kind]

    def disk(self, code):
        return json.loads(self.host._battle_sessions_file.read_text(encoding="utf-8"))[code]

    async def assert_random_choice(self, side):
        code, _ = await self.begin()
        real_vote = self.host.client.vote_battle
        real_reveal = self.host.client.reveal_battle

        async def inspect_vote(context, *, winner):
            self.assertEqual(winner, side)
            self.assertEqual(self.disk(code)["random_choice"], side)
            self.assertEqual(self.disk(code)["vote"], "")
            self.assertIsNone(self.disk(code)["names"])
            return await real_vote(context, winner=winner)

        async def inspect_reveal(context):
            self.assertEqual(self.disk(code)["vote"], side)
            return await real_reveal(context)

        self.host.client.vote_battle = inspect_vote
        self.host.client.reveal_battle = inspect_reveal
        with patch.object(battle_module.secrets, "choice", return_value=side) as choose:
            results = await self.optout(code.lower())
        choose.assert_called_once_with(("a", "b"))
        self.assertIn(f"已随机投票 {side.upper()}", text(results))
        self.assertEqual(text(results),
                         f"已随机投票 {side.upper()}。\nBattle 已揭晓\nA：Real A\nB：Real B")
        self.assertNotIn(code, text(results))
        self.assertIn("Real A", text(results))
        self.assertNotIn("免投", text(results))
        self.assertNotIn("跳过", text(results))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(len(self.calls("reveal")), 1)
        self.assertEqual(self.calls("vote")[0][1], self.host.client.response["context"])
        self.assertEqual(self.disk(code)["context"], self.host.client.response["context"])
        self.assertEqual(self.disk(code)["random_choice"], side)
        self.assertEqual(self.disk(code)["vote"], side)

    async def test_real_host_helpers_reject_cross_store_quote_without_voting(self):
        from .test_battle_reply import QuoteEvent

        plugin = _plugin_module().ArenaImagePlugin
        for name in ("_ambiguous_battle_quote", "_video_controller"):
            self.assertIs(getattr(self.host, name).__func__, getattr(plugin, name))
        self.assertNotIn("_video_battles", vars(self.host))
        self.assertFalse(self.host._ambiguous_battle_quote(self.event))
        self.assertNotIn("_video_battles", vars(self.host))
        code, _ = await self.begin()
        entry = self.host._battle_sessions[code]
        entry["platform_message_id"] = "123"
        self.host._battle_save()
        controller = self.host._video_controller()
        self.assertIs(self.host._video_controller(), controller)
        self.assertEqual(controller.data_dir, self.host.data_dir / "video_battles")
        controller._battle_sessions["ABCDEF"] = entry.copy()
        quote = QuoteEvent("不投票", "123")
        self.assertTrue(self.host._ambiguous_battle_quote(quote))
        with patch.object(battle_module.secrets, "choice") as choose:
            result = await self.optout(event=quote)
        choose.assert_not_called()
        self.assertIn("未提交投票", text(result))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))
        controller._battle_sessions["ABCDEF"]["chat"] = "other-room"
        self.assertFalse(self.host._ambiguous_battle_quote(quote))

    async def test_random_a_saved_before_network_and_vote_mark_before_names(self):
        await self.assert_random_choice("a")

    async def test_random_b_saved_before_network_and_vote_mark_before_names(self):
        await self.assert_random_choice("b")

    async def test_disabled_switch_never_selects_or_posts_even_with_pending_choice(self):
        code, _ = await self.begin()
        for config, choice in (({}, ""), ({"battle_allow_reveal_without_vote": False}, ""),
                               ({"battle_allow_reveal_without_vote": False}, "b")):
            self.host.config = config
            self.host._battle_sessions[code]["random_choice"] = choice
            self.host._battle_save()
            with patch.object(battle_module.secrets, "choice") as choose:
                results = await self.optout(code)
            self.assertIn("请先投票", text(results))
            choose.assert_not_called()
            self.assertEqual(self.disk(code)["random_choice"], choice)
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_restart_after_network_failure_reuses_saved_choice(self):
        code, _ = await self.begin()
        self.host.client.vote_error = RuntimeError("upstream unavailable")
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            failed = await self.optout(code)
        self.assertIn("随机选择 B 保持不变", text(failed))
        self.assertEqual(self.disk(code)["random_choice"], "b")
        self.assertEqual(self.disk(code)["vote"], "")
        self.assertFalse(self.calls("reveal"))
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        self.host.client.vote_error = None
        with patch.object(battle_module.secrets, "choice") as choose:
            results = await self.optout(host=restarted)
        choose.assert_not_called()
        self.assertIn("已随机投票 B", text(results))
        self.assertEqual([call[2] for call in self.calls("vote")], ["b", "b"])
        self.assertEqual(self.disk(code)["vote"], "b")

    async def test_pending_and_confirmed_name_failures_preserve_comma_retry_prefix(self):
        self.host._battle_command_prefix = lambda event: ","
        code, _ = await self.begin()
        self.host.client.vote_error = TimeoutError("vote pending")
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            pending = await self.optout(code)
        self.assertIn(f"请继续 ,battle不投票 {code}：", text(pending))
        self.assertNotIn("揭晓失败：battle", text(pending))
        self.assertEqual(self.disk(code)["random_choice"], "b")
        self.assertEqual(self.disk(code)["vote"], "")
        self.assertFalse(self.calls("reveal"))

        self.host.client.vote_error = None
        self.host.client.vote_result = {"success": True, "reveal_pending": True}
        self.host.client.reveal_error = RuntimeError("names unavailable")
        with patch.object(battle_module.secrets, "choice") as choose:
            confirmed = await self.optout(code)
        choose.assert_not_called()
        self.assertIn(f"不要重复投票，可重试 ,battle不投票 {code}：", text(confirmed))
        self.assertNotIn("揭晓失败：battle", text(confirmed))
        self.assertIn("投票已成功", text(confirmed))
        self.assertEqual(self.disk(code)["vote"], "b")
        self.assertIsNone(self.disk(code)["names"])
        self.assertEqual(len(self.calls("vote")), 2)
        self.assertEqual(len(self.calls("reveal")), 1)

    async def test_timeout_keeps_choice_and_manual_same_or_other_side_is_blocked(self):
        code, _ = await self.begin()
        self.host.client.vote_error = TimeoutError("uncertain response")
        with patch.object(battle_module.secrets, "choice", return_value="a"):
            results = await self.optout(code)
        self.assertIn("尚未确认成功", text(results))
        self.assertNotIn("已随机投票", text(results))
        self.assertEqual(self.disk(code)["random_choice"], "a")
        for side in ("A", "B"):
            blocked = await collect(self.host.battle_vote(self.event, side + " " + code))
            self.assertIn("不能改为手动 A/B", text(blocked))
            self.assertIn("/battle不投票 " + code, text(blocked))
        self.assertEqual(len(self.calls("vote")), 1)
        self.host.client.vote_error = None
        with patch.object(battle_module.secrets, "choice") as choose:
            await self.optout(code)
        choose.assert_not_called()
        self.assertEqual([call[2] for call in self.calls("vote")], ["a", "a"])

    async def test_cancellation_keeps_durable_choice_and_releases_lock(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            async with _tracked_task(self.optout(code)) as task:
                await _wait_for_barrier(self.host.client.vote_started, task)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
        self.assertEqual(self.disk(code)["random_choice"], "b")
        self.assertEqual(self.disk(code)["vote"], "")
        self.assertFalse(self.host._battle_busy)
        self.assertFalse(self.host._battle_locks[code].locked())
        self.assertFalse(self.calls("reveal"))
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        self.host.client.vote_gate.set()
        with patch.object(battle_module.secrets, "choice") as choose:
            await self.optout(host=restarted)
        choose.assert_not_called()
        self.assertEqual([call[2] for call in self.calls("vote")], ["b", "b"])

    async def test_parallel_optouts_submit_once_and_cache_keeps_random_source(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        with patch.object(battle_module.secrets, "choice", return_value="a") as choose:
            async with _tracked_task(self.optout(code)) as first:
                await _wait_for_barrier(self.host.client.vote_started, first)
                async with _tracked_task(self.optout(code)) as second:
                    self.host.client.vote_gate.set()
                    results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
        choose.assert_called_once()
        self.assertTrue(all("已随机投票 A" in text(result) for result in results))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(len(self.calls("reveal")), 1)
        restarted = Host(self.temp.name, client=self.host.client)
        with (patch.object(restarted, "_battle_client", side_effect=AssertionError("cache only")),
              patch.object(battle_module.secrets, "choice") as choose):
            again = await self.optout(host=restarted)
        choose.assert_not_called()
        self.assertIn("已随机投票 A", text(again))
        self.assertEqual(self.disk(code)["vote"], "a")

    async def test_manual_command_waits_for_random_lock_and_cannot_conflict(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        self.host.client.vote_error = TimeoutError("vote uncertain")
        guard = self.host._battle_guard
        manual_waiting = asyncio.Event()

        def signal_guard(requested):
            manual_waiting.set()
            return guard(requested)

        with patch.object(battle_module.secrets, "choice", return_value="a"):
            async with _tracked_task(self.optout(code)) as random_vote:
                await _wait_for_barrier(self.host.client.vote_started, random_vote)
                with patch.object(self.host, "_battle_guard", side_effect=signal_guard):
                    async with _tracked_task(collect(self.host.battle_vote(self.event, "B " + code))) as manual:
                        await _wait_for_barrier(manual_waiting, manual)
                        self.assertFalse(manual.done())
                        self.assertEqual(len(self.calls("vote")), 1)
                        self.host.client.vote_gate.set()
                        await asyncio.wait_for(random_vote, timeout=5)
                        rejected = await asyncio.wait_for(manual, timeout=5)
        self.assertIn("继续 /battle不投票", text(rejected))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(self.disk(code)["random_choice"], "a")
        self.assertEqual(self.disk(code)["vote"], "")

    async def test_random_waits_for_manual_vote_then_only_reads_names(self):
        code, _ = await self.begin()
        self.host.client.vote_gate = asyncio.Event()
        async with _tracked_task(collect(self.host.battle_vote(self.event, "B " + code))) as manual:
            await _wait_for_barrier(self.host.client.vote_started, manual)
            with patch.object(battle_module.secrets, "choice") as choose:
                async with _tracked_task(self.optout(code)) as random_vote:
                    self.host.client.vote_gate.set()
                    _, revealed = await asyncio.wait_for(asyncio.gather(manual, random_vote), timeout=5)
        choose.assert_not_called()
        self.assertNotIn("已随机投票", text(revealed))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(self.disk(code)["random_choice"], "")

    async def test_failed_and_incomplete_sessions_never_select_or_post_after_restart(self):
        for sides in ({}, {"a": {"text": "only A"}},
                      {"a": {"text": "failed", "error": "hidden-model"}, "b": {"text": "B"}}):
            self.host.client.response["sides"] = sides
            code, results = await self.begin()
            self.assertIsNone(self.disk(code)["vote"])
            self.assertEqual(results[1].text, "Battle\n")
            self.assertEqual(results[1].chain[-1].text, "\n本场禁止投票，请重新发起。")
            self.assertNotIn("引用本条回复", text(results))
            self.assertNotIn("/battle不投票", text(results))
            self.assertNotIn("可用 /battle揭晓", text(results))
            restarted = Host(self.temp.name, self.host.config, self.host.client)
            with patch.object(battle_module.secrets, "choice") as choose:
                blocked = await self.optout(code, host=restarted)
            choose.assert_not_called()
            self.assertIn("禁止投票", text(blocked))
        # Closing during delivery is also incomplete, even with two good sides.
        self.host.client.response = Client().response
        previous_codes = set(self.host._battle_sessions)
        generator = self.host.battle(self.event, "incomplete delivery")
        ack = await anext(generator)
        self.assertEqual(ack.text, ACK)
        self.assertEqual(ack.chain, [])
        self.assertFalse(image_components([ack]))
        self.assertEqual(set(self.host._battle_sessions), previous_codes)
        comparison = await anext(generator)
        code = self.host._battle_latest[(self.event.unified_msg_origin, self.event.owner)]
        self.assertNotIn(code, previous_codes)
        self.assertEqual(comparison.text, "Battle\n")
        self.assertEqual(len(image_components([comparison])), 2)
        self.assertEqual(
            [type(component) for component in comparison.chain],
            [battle_module.Plain, battle_module.Image, battle_module.Plain,
             battle_module.Image, battle_module.Plain],
        )
        self.assertEqual(comparison.chain[0].text, "\nA\n")
        self.assertEqual(comparison.chain[2].text, "\nB\n")
        self.assertEqual(
            comparison.chain[-1].text,
            "\n引用本条回复 A / B 投票；回复“不投票”随机投票并揭晓。",
        )
        self.assertNotIn(code, text([comparison]))
        self.assertIsNone(self.host._battle_sessions[code]["vote"])
        self.assertIsNone(self.disk(code)["vote"])
        await generator.aclose()
        self.assertIsNone(self.disk(code)["vote"])
        with patch.object(battle_module.secrets, "choice") as choose:
            blocked = await self.optout(code)
        choose.assert_not_called()
        self.assertIn("禁止投票", text(blocked))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_choice_save_failure_means_zero_network_and_no_redraw_on_retry(self):
        code, _ = await self.begin()
        original = self.host._battle_sessions_file.read_bytes()
        with (patch.object(battle_module.secrets, "choice", return_value="b"),
              patch.object(battle_module.os, "replace", side_effect=OSError("disk unavailable"))):
            results = await self.optout(code)
        self.assertIn("保存尚未确认成功", text(results))
        self.assertEqual(self.host._battle_sessions_file.read_bytes(), original)
        self.assertEqual(self.host._battle_sessions[code]["random_choice"], "b")
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))
        with patch.object(battle_module.secrets, "choice") as choose:
            result = await self.optout(code)
        choose.assert_not_called()
        self.assertIn("已随机投票 B", text(result))
        self.assertEqual(self.calls("vote")[0][2], "b")
        self.assertFalse(list(self.host.data_dir.glob("*.tmp")))

    async def test_confirmed_vote_save_failure_blocks_names_then_retries_without_post(self):
        code, _ = await self.begin()
        real_save = self.host._battle_save

        def fail_confirmation_save():
            if self.host._battle_sessions[code]["vote"] in {"a", "b"}:
                raise OSError("disk unavailable")
            real_save()

        self.host.client.vote_result = {"success": True, "a": "Real A", "b": "Real B"}
        with (patch.object(battle_module.secrets, "choice", return_value="a"),
              patch.object(self.host, "_battle_save", side_effect=fail_confirmation_save),
              patch.object(self.host, "_battle_validate_names") as validate):
            results = await self.optout(code)
        validate.assert_not_called()
        self.assertIn("已随机投票 A", text(results))
        self.assertIn("投票已成功", text(results))
        self.assertEqual(self.host._battle_sessions[code]["vote"], "a")
        self.assertEqual(self.disk(code)["vote"], "")
        self.assertEqual(self.disk(code)["random_choice"], "a")
        self.assertFalse(self.calls("reveal"))
        real_reveal = self.host.client.reveal_battle

        async def inspect_reveal(context):
            self.assertEqual(self.disk(code)["vote"], "a")
            return await real_reveal(context)

        self.host.client.reveal_battle = inspect_reveal
        with patch.object(battle_module.secrets, "choice") as choose:
            recovered = await self.optout(code)
        choose.assert_not_called()
        self.assertIn("Real A", text(recovered))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(self.disk(code)["vote"], "a")

    async def test_reveal_pending_confirmation_survives_restart_switch_off_no_post(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"success": True, "reveal_pending": True}
        self.host.client.reveal_error = RuntimeError("names unavailable")
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            results = await self.optout(code)
        self.assertIn("投票已成功", text(results))
        self.assertEqual(self.disk(code)["vote"], "b")
        self.assertIsNone(self.disk(code)["names"])
        restarted = Host(self.temp.name, client=self.host.client)
        blocked = await collect(restarted.battle_vote(self.event, "A " + code))
        self.assertIn("已经投票", text(blocked))
        self.host.client.reveal_error = None
        with patch.object(battle_module.secrets, "choice") as choose:
            recovered = await self.optout(code, host=restarted)
        choose.assert_not_called()
        self.assertIn("已随机投票 B", text(recovered))
        self.assertEqual(len(self.calls("vote")), 1)
        self.assertEqual(self.disk(code)["names"], self.host.client.names)

    async def test_vote_names_result_uses_cache_without_reveal(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"a": "Real A", "b": "Real B"}
        self.host.client.reveal_error = AssertionError("must use names cache")
        with patch.object(battle_module.secrets, "choice", return_value="a"):
            results = await self.optout(code)
        self.assertIn("已随机投票 A", text(results))
        self.assertFalse(self.calls("reveal"))
        self.assertEqual(self.disk(code)["names"], self.host.client.names)
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_invalid_names_do_not_undo_confirmed_random_vote(self):
        code, _ = await self.begin()
        self.host.client.vote_result = {"success": True, "a": 123, "b": "Real B"}
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            result = await self.optout(code)
        self.assertIn("投票已成功", text(result))
        self.assertEqual(self.disk(code)["vote"], "b")
        self.assertIsNone(self.disk(code)["names"])
        restarted = Host(self.temp.name, client=self.host.client)
        recovered = await self.optout(code, host=restarted)
        self.assertIn("已随机投票 B", text(recovered))
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_explicit_failure_keeps_choice_and_never_reads_names(self):
        code, _ = await self.begin()
        for failure in (False, {"success": False}, {"error": "upstream failure"}):
            self.host.client.vote_result = failure
            with patch.object(battle_module.secrets, "choice", return_value="a"):
                result = await self.optout(code)
            self.assertIn("上游未确认投票成功", text(result))
            self.assertNotIn("已随机投票", text(result))
            self.assertEqual(self.disk(code)["random_choice"], "a")
            self.assertEqual(self.disk(code)["vote"], "")
        self.assertEqual([call[2] for call in self.calls("vote")], ["a", "a", "a"])
        self.assertFalse(self.calls("reveal"))

    async def test_cross_owner_or_chat_cannot_select_vote_read_or_disclose_source(self):
        code, _ = await self.begin()
        self.host._battle_sessions[code]["random_choice"] = "b"
        self.host._battle_save()
        original = self.host._battle_sessions_file.read_bytes()
        for event in (Event("bob"), Event(chat="platform:Group:room-2"),
                      Event("bob", "platform:Group:room-2"), Event(owner=""), Event(chat="")):
            with patch.object(battle_module.secrets, "choice") as choose:
                results = await self.optout(code, event=event)
            choose.assert_not_called()
            self.assertNotIn("随机选择 B", text(results))
            self.assertNotIn("Real", text(results))
        self.assertEqual(self.host._battle_sessions_file.read_bytes(), original)
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_latest_lookup_for_random_vote_is_owner_scoped(self):
        alice, _ = await self.begin()
        bob = Event("bob")
        bob_code, _ = await self.begin(event=bob)
        with patch.object(battle_module.secrets, "choice", return_value="a"):
            await self.optout(event=bob)
        self.assertEqual(self.disk(bob_code)["vote"], "a")
        self.assertEqual(self.disk(alice)["vote"], "")
        self.assertEqual(self.disk(alice)["random_choice"], "")

    async def test_legacy_sessions_missing_optional_fields_remain_compatible(self):
        code, _ = await self.begin()
        record = self.disk(code)
        for missing in (("random_choice",), ("platform_message_id",),
                        ("random_choice", "platform_message_id")):
            with self.subTest(missing=missing):
                legacy = {key: value for key, value in record.items() if key not in missing}
                self.host._battle_sessions_file.write_text(json.dumps({code: legacy}), encoding="utf-8")
                restarted = Host(self.temp.name, self.host.config, self.host.client)
                entry = restarted._battle_sessions[code]
                self.assertEqual(set(entry), set(battle_module._SESSION_FIELDS))
                self.assertEqual(entry["random_choice"], "")
                self.assertEqual(entry["platform_message_id"], "")
                self.assertEqual(entry["vote"], "")
                self.assertIsNone(entry["names"])
        with patch.object(battle_module.secrets, "choice", return_value="b"):
            results = await self.optout(code, host=restarted)
        self.assertIn("已随机投票 B", text(results))
        self.assertEqual(self.disk(code)["random_choice"], "b")
        self.assertEqual(self.disk(code)["platform_message_id"], "")

    async def test_legacy_revealed_without_vote_cache_never_gets_random_vote(self):
        code, _ = await self.begin()
        records = json.loads(self.host._battle_sessions_file.read_text(encoding="utf-8"))
        records[code].pop("random_choice")
        records[code].pop("platform_message_id")
        records[code]["names"] = self.host.client.names
        self.host._battle_sessions_file.write_text(json.dumps(records), encoding="utf-8")
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        with patch.object(battle_module.secrets, "choice") as choose:
            results = await self.optout(code, host=restarted)
        choose.assert_not_called()
        self.assertIn("Real A", text(results))
        self.assertNotIn("已随机投票", text(results))
        self.assertEqual(self.disk(code)["vote"], "")
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_invalid_or_conflicting_choice_records_are_ignored(self):
        code, _ = await self.begin()
        records = json.loads(self.host._battle_sessions_file.read_text(encoding="utf-8"))
        for i, changes in enumerate(({"random_choice": "c"}, {"random_choice": None},
                                     {"random_choice": "a", "vote": "b"},
                                     {"random_choice": "a", "vote": None}), 1):
            records[f"{i:06X}"] = {**records[code], **changes}
        self.host._battle_sessions_file.write_text(json.dumps(records), encoding="utf-8")
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        self.assertEqual(list(restarted._battle_sessions), [code])

    async def test_invalid_platform_message_ids_are_ignored_without_retaining_message_content(self):
        code, _ = await self.begin()
        records = {code: self.disk(code)}
        valid_ids = ('789012', '-789013')
        invalid_ids = (None, True, 789012, '0', ' 789012 ', 'not-an-id', '1' * 21,
                       'https://offline.invalid/image?token=do-not-echo')
        accepted = [code]
        for i, native_id in enumerate((*valid_ids, *invalid_ids), 1):
            key = f'{i:06X}'
            if key == code:
                key = 'FFFFFF'
            records[key] = {**records[code], 'platform_message_id': native_id,
                            'quoted_text': 'do-not-keep quoted payload',
                            'message_content': 'do-not-keep image payload'}
            if native_id in valid_ids:
                accepted.append(key)
        self.host._battle_sessions_file.write_text(json.dumps(records), encoding='utf-8')
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        self.assertEqual(set(restarted._battle_sessions), set(accepted))
        self.assertEqual(restarted._battle_sessions[code]['platform_message_id'], '')
        for key, native_id in zip(accepted[1:], valid_ids):
            self.assertEqual(restarted._battle_sessions[key]['platform_message_id'], native_id)
        restarted._battle_save()
        cleaned = json.loads(self.host._battle_sessions_file.read_text(encoding='utf-8'))
        for entry in cleaned.values():
            self.assertEqual(set(entry), set(battle_module._SESSION_FIELDS))
        self.assertNotIn('do-not-keep', self.host._battle_sessions_file.read_text(encoding='utf-8'))
        self.assertFalse(self.calls('vote'))
        self.assertFalse(self.calls('reveal'))

    async def test_transport_mismatch_does_not_redraw_or_post_pending_choice(self):
        self.host.config["transport_mode"] = "direct"
        code, _ = await self.begin()
        self.host._battle_sessions[code]["random_choice"] = "b"
        self.host._battle_save()
        restarted = Host(self.temp.name, {**self.host.config, "transport_mode": "userscript"},
                         self.host.client)
        with patch.object(battle_module.secrets, "choice") as choose:
            results = await self.optout(code, host=restarted)
        choose.assert_not_called()
        self.assertIn("传输通道已切换", text(results))
        self.assertEqual(self.disk(code)["random_choice"], "b")
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_mode_change_after_confirmation_preserves_mark_without_read(self):
        code, _ = await self.begin()
        real_vote = self.host.client.vote_battle

        async def change_mode(context, *, winner):
            result = await real_vote(context, winner=winner)
            self.host.config["transport_mode"] = "direct"
            return result

        self.host.client.vote_battle = change_mode
        with patch.object(battle_module.secrets, "choice", return_value="a"):
            result = await self.optout(code)
        self.assertIn("传输通道已切换", text(result))
        self.assertEqual(self.disk(code)["vote"], "a")
        self.assertFalse(self.calls("reveal"))
        self.host.config["transport_mode"] = "bridge"
        await self.optout(code)
        self.assertEqual(len(self.calls("vote")), 1)

    async def test_expired_session_cannot_start_random_vote(self):
        self.host.config["battle_session_ttl"] = 10
        with patch.object(battle_module.time, "time", return_value=1000):
            code, _ = await self.begin()
        with (patch.object(battle_module.time, "time", return_value=1010),
              patch.object(battle_module.secrets, "choice") as choose):
            result = await self.optout(code)
        choose.assert_not_called()
        self.assertIn("已过期", text(result))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_busy_random_vote_not_evicted_or_expired_while_awaiting(self):
        self.host.config["battle_session_ttl"] = 10
        with (patch.object(battle_module, "MAX_BATTLE_SESSIONS", 1),
              patch.object(battle_module.time, "time", return_value=1000)):
            code, _ = await self.begin()
            self.host.client.vote_gate = asyncio.Event()
            with patch.object(battle_module.secrets, "choice", return_value="a"):
                async with _tracked_task(self.optout(code)) as task:
                    await _wait_for_barrier(self.host.client.vote_started, task)
                    with patch.object(battle_module.time, "time", return_value=1010):
                        self.host._battle_trim()
                        self.assertIn(code, self.host._battle_sessions)
                        self.assertEqual(self.disk(code)["random_choice"], "a")
                    blocked = await collect(self.host.battle(Event("bob"), "another comparison"))
                    self.assertIn("会话已满", text(blocked))
                    self.host.client.vote_gate.set()
                    result = await asyncio.wait_for(task, timeout=5)
            self.assertIn("已随机投票 A", text(result))
            self.assertEqual(len(self.calls("vote")), 1)
            self.assertFalse(self.host._battle_busy)

    async def test_config_save_failure_rolls_back_to_default_off_zero_post(self):
        code, _ = await self.begin()
        for config in ({}, {"battle_allow_reveal_without_vote": False}):
            self.host.config = config.copy()
            original = self.host.config.copy()
            with patch.object(self.host, "save_config", create=True,
                              side_effect=OSError("config unavailable")):
                result = await collect(self.host.battle_reveal_switch(self.event, "开"))
            self.assertIn("修改揭晓开关失败", text(result))
            self.assertEqual(self.host.config, original)
            with patch.object(battle_module.secrets, "choice") as choose:
                result = await self.optout(code)
            choose.assert_not_called()
            self.assertIn("请先投票", text(result))
        self.assertFalse(self.calls("vote"))
        self.assertFalse(self.calls("reveal"))

    async def test_switch_copy_discloses_actual_random_preference(self):
        result = await collect(self.host.battle_reveal_switch(self.event, "查询"))
        self.assertIn("随机投票揭晓开关：开", text(result))
        self.assertIn("/battle不投票", text(result))
        self.assertIn("会提交随机 A/B 偏好票", text(result))
        await collect(self.host.battle_reveal_switch(self.event, "关"))
        result = await collect(self.host.battle_reveal_switch(self.event, "查询"))
        self.assertIn("随机投票揭晓开关：关", text(result))
        self.assertIn("不会提交随机票", text(result))


class BattleMergeContractTests(unittest.TestCase):
    def test_alias_is_same_unprivileged_reveal_handler_switch_remains_admin(self):
        plugin = _plugin_module().ArenaImagePlugin
        self.assertEqual(plugin.battle_reveal.arena_command, "battle揭晓")
        self.assertEqual(plugin.battle_reveal.arena_aliases,
                         {"battlereveal", "竞技场揭晓", "battle不投票"})
        self.assertIsNone(getattr(plugin.battle_reveal, "arena_permission", None))
        self.assertEqual(plugin.battle_reveal_switch.arena_permission, "admin")

    def test_schema_keeps_default_off_and_discloses_random_vote(self):
        schema = json.loads((STAGE / "_conf_schema.json").read_text(encoding="utf-8"))
        option = schema["battle_allow_reveal_without_vote"]
        self.assertIs(option["default"], False)
        self.assertEqual(option["type"], "bool")
        self.assertIn("随机投票揭晓开关", option["description"])
        self.assertIn("/battle不投票", option["description"])
        self.assertIn("偏好票", option["description"])
        self.assertNotIn("尝试跳过", option["description"])

    def test_readme_append_preserves_online_baseline_and_has_current_contract(self):
        readme = (STAGE / "README.md").read_bytes()
        _assert_online_readme_prefix(self, readme)
        battle = readme.decode("utf-8").split("## 0.7.16-battle：匿名 A/B 对战", 1)[1]
        self.assertIn("/battle不投票 [编号]", battle)
        self.assertIn("明确提交一次偏好票", battle)
        self.assertIn("失败、超时或取消后保留原选择", battle)
        self.assertNotIn("可允许尝试官网跳过评价", battle)

    def test_names_message_is_compact_without_session_code(self):
        names = {"a": "Real A", "b": "Real B"}
        for code in ("ABC123", "DEF456"):
            with self.subTest(code=code):
                self.assertEqual(battle_module.BattleCommandsMixin._battle_names_message(code, names),
                                 "Battle 已揭晓\nA：Real A\nB：Real B")

    def test_release_registration_version_does_not_change_method_fingerprints(self):
        # The release decorator is module/class metadata, not a method body.
        tree = ast.parse((STAGE / "main.py").read_text(encoding="utf-8"))
        plugin = next(node for node in tree.body
                      if isinstance(node, ast.ClassDef) and node.name == "ArenaImagePlugin")
        registration = next(node for node in plugin.decorator_list
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "register")
        self.assertEqual(ast.literal_eval(registration.args[3]), "0.7.18-battle")
        metadata = (STAGE / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("\nversion: 0.7.18-battle\n", metadata)
        # No broad decorator-stripping or fixture rewrite: method ASTs remain exact.
        baseline = _preservation_fixture()["reply_baseline"]["main_function_sha256"]
        current = _function_fingerprints(STAGE / "main.py")
        self.assertEqual(len(baseline), 86)
        self.assertEqual({name for name in baseline if baseline[name] != current[name]},
                         {"ArenaImagePlugin._deliver_battle_message", "ArenaImagePlugin.battle_vote", "ArenaImagePlugin.battle_reveal"})

    def test_main_functions_preserved_against_deployed_reply_baseline(self):
        before = _preservation_fixture()["reply_baseline"]["main_function_sha256"]
        after = _function_fingerprints(STAGE / "main.py")
        self.assertEqual(len(before), 86)
        self.assertTrue(before.keys() <= after.keys(), "deployed main functions removed")
        self.assertEqual(set(after) - set(before), {'ArenaImagePlugin.video_battle_reveal', 'ArenaImagePlugin._deliver_video_battle_message', 'ArenaImagePlugin.battle_reply_entry', 'ArenaImagePlugin.video_battle', 'ArenaImagePlugin._battle_reply_generator', 'ArenaImagePlugin._video_battle_client', 'ArenaImagePlugin._prune_video_outputs', 'ArenaImagePlugin.video_battle_vote', 'ArenaImagePlugin._video_controller', 'ArenaImagePlugin.video_battle_plain_entry', 'ArenaImagePlugin._materialize_video_output', 'ArenaImagePlugin._battle_reply_generator.ambiguous', 'ArenaImagePlugin._ambiguous_battle_quote', 'ArenaImagePlugin.video_battle_resume'})
        # Lifecycle/transport changes from v0.7.12 were already deployed before
        # this patch. Against the actual immutable v0.7.15 snapshot, only the
        # grouped sender changes, to retain its confirmed native receipt ID.
        # The release-only @register version is outside function fingerprints;
        # do not weaken full method hashes or change the public baseline fixture.
        changed = {name for name in before if before[name] != after[name]}
        self.assertEqual(changed, {"ArenaImagePlugin._deliver_battle_message", "ArenaImagePlugin.battle_vote", "ArenaImagePlugin.battle_reveal"})

    def test_existing_single_model_methods_are_ast_identical_to_online_baseline(self):
        before = _preservation_fixture()["online_baseline"]["class_methods"]["main.py"]["ArenaImagePlugin"]
        after = _class_method_fingerprints(STAGE / "main.py", "ArenaImagePlugin")
        # Other delegated transport/lifecycle changes predate this command task.
        # Check the single-model catalog, prompts, image input and delivery routes.
        protected = (
            "_bridge_url", "_input_max_bytes", "_output_max_bytes", "_send_max_bytes",
            "_send_max_attempts", "_send_retry_delay", "_is_private_chat", "_rate_limit_hint",
            "_session_key", "_load_selection", "_save_selection", "_clean_preset",
            "_load_presets", "_save_presets", "_find_preset", "_expand_preset",
            "_preset_names_hint", "_fetch_models", "_fetch_model_health", "_model_id",
            "_resolve_model", "_model_kind", "_model_created_text", "_health_age_text",
            "_model_health_text", "_model_is_stealth", "_model_list_text", "_model_name_list_text",
            "list_model_names", "list_models", "list_stealth_models", "switch_model",
            "list_presets", "add_preset", "delete_preset", "bind_preset_model", "preset_image",
            "unified_image", "text_to_image", "image_to_image", "_interactive_auth_payload",
            "_collect_input_images", "_send_plans", "_smaller_send_copy", "_deliver_image",
            "_materialize_output",
        )
        for name in protected:
            self.assertEqual(before[name], after[name], name)
