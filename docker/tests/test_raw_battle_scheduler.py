"""AstrBot 4.27 scheduler contract: stop is checked BEFORE yielded result send."""
from __future__ import annotations
import unittest
from unittest.mock import patch
from .test_arena_image_hardening import _make_plugin
from .test_battle_commands import Event, Client, BattleCommandsMixin, ACK, image_components, text

class RawEvent(Event):
    def __init__(self, message='battle offline cat', *, woken=False):
        super().__init__()
        self.message = message
        self.is_at_or_wake_command = woken
        self.stopped = False
        self.stop_calls = 0
        self.delivered = []

    def get_message_str(self): return self.message
    def is_stopped(self): return self.stopped
    def stop_event(self):
        self.stop_calls += 1
        self.stopped = True

async def scheduler_consume(generator, event):
    try:
        async for result in generator:
            if event.is_stopped():
                break
            event.delivered.append(result)
    finally:
        await generator.aclose()

class RawSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_battle_outputs_are_sent_before_stop_with_real_command_mixin(self):
        _, plugin = _make_plugin(self)
        client = Client()
        plugin._battle_client = lambda: client
        async def no_inputs(event): return []
        async def materialize(value): return plugin.data_dir/(value+'.png')
        plugin._collect_input_images = no_inputs
        plugin._materialize_output = materialize
        event = RawEvent('BATTLE offline cat')
        await scheduler_consume(plugin.battle_plain_entry(event), event)
        self.assertEqual([x[0] for x in client.calls], ['begin'])
        self.assertEqual(client.calls[0][1], 'offline cat')
        self.assertEqual(len(event.delivered), 2)
        self.assertEqual(event.delivered[0].text, ACK)
        self.assertFalse(image_components([event.delivered[0]]))
        self.assertEqual(sum(bool(image_components([x])) for x in event.delivered), 1)
        images = image_components(event.delivered)
        self.assertEqual(len(images), 2)
        self.assertEqual([image.file for image in images], [
            str(plugin.data_dir / 'a-image.png'), str(plugin.data_dir / 'b-image.png'),
        ])
        code = next(iter(plugin._battle_sessions))
        self.assertEqual(event.delivered[1].text, 'Battle\n')
        self.assertTrue(text([event.delivered[1]]).startswith('Battle\n'))
        self.assertEqual(event.delivered[1].chain[-1].text,
                         '\n引用本条回复 A / B 投票并揭晓。')
        self.assertNotIn(code, text(event.delivered))
        self.assertEqual(event.stop_calls, 1)
        self.assertEqual(next(iter(plugin._battle_sessions.values()))['vote'], '')
        self.assertFalse(plugin._battle_pending_outputs)
        self.assertFalse(plugin._battle_busy)
        self.assertFalse(plugin._generation_lock.locked())

    async def test_grouped_raw_result_is_not_votable_or_stopped_until_yield_resumes(self):
        _, plugin = _make_plugin(self)
        client = Client()
        plugin._battle_client = lambda: client
        async def no_inputs(event): return []
        async def materialize(value): return plugin.data_dir / (value + '.png')
        plugin._collect_input_images = no_inputs
        plugin._materialize_output = materialize
        event = RawEvent()
        generator = plugin.battle_plain_entry(event)
        try:
            ack = await anext(generator)
            self.assertEqual(ack.text, ACK)
            self.assertFalse(image_components([ack]))
            self.assertFalse(plugin._battle_sessions)
            self.assertFalse(client.calls)
            self.assertFalse(event.is_stopped())
            self.assertEqual(plugin._active_generations, 1)
            result = await anext(generator)
            self.assertEqual(len(image_components([result])), 2)
            code = next(iter(plugin._battle_sessions))
            self.assertEqual(result.text, 'Battle\n')
            self.assertIsNone(plugin._battle_sessions[code]['vote'])
            self.assertFalse(event.is_stopped())
            self.assertEqual(event.stop_calls, 0)
            self.assertFalse(plugin._generation_lock.locked())
            self.assertTrue(plugin._battle_pending_outputs)
            self.assertTrue(plugin._battle_busy)
            with self.assertRaises(StopAsyncIteration):
                await anext(generator)
            self.assertEqual(plugin._battle_sessions[code]['vote'], '')
            self.assertEqual(event.stop_calls, 1)
            self.assertFalse(plugin._battle_pending_outputs)
            self.assertFalse(plugin._battle_busy)
        finally:
            await generator.aclose()

    async def test_real_wrappers_close_inner_generator_immediately_after_ack(self):
        for raw in (True, False):
            with self.subTest(raw=raw):
                _, plugin = _make_plugin(self)
                client = Client()
                plugin._battle_client = lambda: client
                async def forbidden_input(_event):
                    raise AssertionError('reference download before ACK close')
                plugin._collect_input_images = forbidden_input
                event = RawEvent()
                generator = (plugin.battle_plain_entry(event) if raw
                             else plugin.battle(event, 'prompt'))
                self.assertEqual((await anext(generator)).text, ACK)
                self.assertEqual(plugin._active_generations, 1)
                await generator.aclose()
                self.assertEqual(plugin._active_generations, 0)
                self.assertFalse(client.calls)
                self.assertFalse(plugin._battle_sessions)
                self.assertFalse(plugin._battle_pending_outputs)
                self.assertFalse(plugin._battle_busy)
                self.assertFalse(plugin._generation_lock.locked())
                self.assertEqual(event.stop_calls, 0)

    async def test_quoted_reply_outputs_send_before_stop_no_duplicate_vote(self):
        from .test_battle_reply import QuoteEvent
        _, plugin = _make_plugin(self)
        client = Client()
        plugin._battle_client = lambda: client
        async def no_inputs(event): return []
        async def materialize(value): return plugin.data_dir/(value+'.png')
        plugin._collect_input_images = no_inputs
        plugin._materialize_output = materialize
        event = RawEvent()
        await scheduler_consume(plugin.battle_plain_entry(event), event)
        code = next(iter(plugin._battle_sessions))
        plugin._battle_sessions[code]['platform_message_id'] = '123'
        plugin._battle_save()
        incoming = RawEvent('A', woken=True)
        incoming.message_obj = QuoteEvent().message_obj
        await scheduler_consume(plugin.battle_reply_entry(incoming), incoming)
        self.assertEqual(len(incoming.delivered), 1)
        self.assertIn('已投票 A', text(incoming.delivered))
        self.assertEqual(incoming.stop_calls, 1)
        self.assertEqual(len([c for c in client.calls if c[0]=='vote']), 1)
        self.assertFalse(plugin._battle_busy)

    async def test_stop_only_after_final_yield_is_resumed(self):
        _, plugin = _make_plugin(self)
        event = RawEvent()
        async def fake_battle(host, incoming, prompt):
            yield incoming.plain_result('first')
            yield incoming.plain_result('last')
        with patch.object(BattleCommandsMixin, 'battle', fake_battle):
            gen = plugin.battle_plain_entry(event)
            self.assertEqual((await anext(gen)).text, 'first')
            self.assertFalse(event.is_stopped())
            self.assertEqual((await anext(gen)).text, 'last')
            self.assertFalse(event.is_stopped())
            with self.assertRaises(StopAsyncIteration): await anext(gen)
            self.assertTrue(event.is_stopped())

    async def test_woken_event_does_not_run_raw_route_or_stop(self):
        _, plugin = _make_plugin(self)
        event = RawEvent(woken=True)
        async def forbidden(*args):
            raise AssertionError('ordinary command already owns woken event')
            yield
        with patch.object(BattleCommandsMixin, 'battle', forbidden):
            await scheduler_consume(plugin.battle_plain_entry(event),event)
        self.assertFalse(event.delivered)
        self.assertEqual(event.stop_calls,0)

    async def test_cancelled_raw_handler_does_not_prematurely_stop(self):
        _, plugin = _make_plugin(self)
        event = RawEvent()
        async def fake_battle(host,incoming,prompt):
            yield incoming.plain_result('first')
            yield incoming.plain_result('last')
        with patch.object(BattleCommandsMixin,'battle',fake_battle):
            gen=plugin.battle_plain_entry(event)
            await anext(gen)
            await gen.aclose()
        self.assertEqual(event.stop_calls,0)
