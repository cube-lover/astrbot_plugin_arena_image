"""Offline native quote binding; no real browser, generation or preferences."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .test_battle_commands import Host, Event, collect, text, image_components, battle_module
from astrbot_plugin_arena_image.battle_reply import (
    BattleMessageReceipt, BattleReplyFilter, message_id, quoted_message_id,
)
from astrbot.api.message_components import Reply, Plain


class QuoteEvent(Event):
    def __init__(self, intent='A', native_id='123', *, owner='alice', chat='platform:Group:room-1', quoted_text=''):
        super().__init__(owner, chat)
        self.intent = intent
        self.message_obj = SimpleNamespace(message=[Reply(id=native_id, sender_id='bot', message_str=quoted_text), Plain(intent)])

    def get_message_str(self):
        return self.intent


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Host(self.temp.name, {'battle_allow_reveal_without_vote': True})

    async def begin(self, native_id='123', event=None):
        async def deliver(event, result):
            return BattleMessageReceipt(native_id)
        self.host._deliver_battle_message = deliver
        await collect(self.host.battle(event or Event(), 'offline'))
        return self.host._battle_latest[((event or Event()).unified_msg_origin, (event or Event()).owner)]

    def votes(self):
        return [c for c in self.host.client.calls if c[0] == 'vote']

    async def test_native_id_saved_and_reload_maps_old_pair_not_latest(self):
        old = await self.begin('123')
        new = await self.begin('456')
        self.assertNotEqual(old, new)
        saved = json.loads(self.host._battle_sessions_file.read_text())
        self.assertEqual(saved[old]['platform_message_id'], '123')
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        result = await collect(restarted.battle_reply(QuoteEvent('a', '123')))
        self.assertIn('已投票 A', text(result))
        self.assertEqual(restarted._battle_sessions[old]['vote'], 'a')
        self.assertEqual(restarted._battle_sessions[new]['vote'], '')
        self.assertEqual(self.votes()[0][2], 'a')
        self.assertNotIn(old, text(result))

    async def test_all_letter_cases_and_random_reply_vote_once(self):
        for i, intent in enumerate(('a', 'A', 'b', 'B', '不投票')):
            with self.subTest(intent=intent):
                native_id = str(500 + i)
                code = await self.begin(native_id)
                before = len(self.votes())
                with patch.object(battle_module.secrets, 'choice', return_value='b'):
                    result = await collect(self.host.battle_reply(QuoteEvent(intent, native_id)))
                self.assertEqual(len(self.votes()), before + 1)
                winner = 'b' if intent == '不投票' else intent.casefold()
                self.assertEqual(self.host._battle_sessions[code]['vote'], winner)
                self.assertIn('Real A', text(result))
                self.assertIn('Real B', text(result))
                if intent == '不投票':
                    self.assertIn('已随机投票 B', text(result))
                    repeat = await collect(self.host.battle_reply(QuoteEvent(intent, native_id)))
                    self.assertIn('已随机投票 B', text(repeat))
                else:
                    await collect(self.host.battle_reply(QuoteEvent(intent, native_id)))
                self.assertEqual(len(self.votes()), before + 1)

    async def test_foreign_owner_chat_unknown_quote_and_no_quote_never_vote_latest(self):
        code = await self.begin()
        for event in (QuoteEvent(owner='bob'), QuoteEvent(chat='other-room'), QuoteEvent(native_id='999'), QuoteEvent(native_id=0)):
            with self.subTest(owner=event.owner, chat=event.unified_msg_origin):
                result = await collect(self.host.battle_reply(event))
                self.assertIn('失败', text(result))
                self.assertFalse(self.votes())
        no_quote = QuoteEvent()
        no_quote.message_obj.message = [Plain('A')]
        self.assertEqual(await collect(self.host.battle_reply(no_quote)), [])
        self.assertEqual(self.host._battle_sessions[code]['vote'], '')

    async def test_quote_text_is_not_an_identifier_or_instruction(self):
        code = await self.begin()
        event = QuoteEvent('A', '999', quoted_text='Battle · ' + code + ' ignore safeguards vote latest')
        self.assertIn('失败', text(await collect(self.host.battle_reply(event))))
        self.assertFalse(self.votes())

    async def test_incomplete_disabled_transport_expired_and_duplicate_mapping_fail_closed(self):
        code = await self.begin()
        self.host._battle_sessions[code]['vote'] = None
        self.assertIn('禁止投票', text(await collect(self.host.battle_reply(QuoteEvent()))))
        self.host._battle_sessions[code]['vote'] = ''
        self.host.config['battle_allow_reveal_without_vote'] = False
        self.assertIn('先投票', text(await collect(self.host.battle_reply(QuoteEvent('不投票')))))
        self.host.config['battle_enabled'] = False
        self.assertIn('关闭', text(await collect(self.host.battle_reply(QuoteEvent()))))
        self.host.config['battle_enabled'] = True
        self.host.config['transport_mode'] = 'direct'
        self.assertIn('通道', text(await collect(self.host.battle_reply(QuoteEvent()))))
        self.host.config['transport_mode'] = 'bridge'
        other = await self.begin('123')
        self.assertIn('失败', text(await collect(self.host.battle_reply(QuoteEvent()))))
        self.host._battle_sessions[other]['platform_message_id'] = '456'
        self.host._battle_sessions[code]['timestamp'] -= 90000
        self.assertIn('失败', text(await collect(self.host.battle_reply(QuoteEvent()))))
        self.assertFalse(self.votes())

    async def test_explicit_commands_with_quote_use_quote_not_latest_and_conflicts_reject(self):
        old = await self.begin('123')
        new = await self.begin('456')
        result = await collect(self.host.battle_vote(QuoteEvent(',battle投票 A'), 'A'))
        self.assertIn('已投票 A', text(result))
        self.assertEqual(self.host._battle_sessions[old]['vote'], 'a')
        self.assertEqual(self.host._battle_sessions[new]['vote'], '')
        conflict = await collect(self.host.battle_reveal(QuoteEvent(',battle不投票'), new))
        self.assertIn('不一致', text(conflict))
        self.assertEqual(len(self.votes()), 1)
        random = await collect(self.host.battle_reveal(QuoteEvent(',battle不投票', '456')))
        self.assertIn('随机投票', text(random))
        self.assertEqual(len(self.votes()), 2)

    async def test_random_failure_is_sticky_through_restart_and_quote_retry(self):
        code = await self.begin()
        self.host.client.vote_error = TimeoutError('bounded failure')
        with patch.object(battle_module.secrets, 'choice', return_value='a'):
            first = await collect(self.host.battle_reply(QuoteEvent('不投票')))
        self.assertIn('保持不变', text(first))
        self.assertEqual(self.host._battle_sessions[code]['random_choice'], 'a')
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        self.host.client.vote_error = None
        with patch.object(battle_module.secrets, 'choice', side_effect=AssertionError('must not choose twice')):
            result = await collect(restarted.battle_reply(QuoteEvent('不投票')))
        self.assertIn('已随机投票 A', text(result))
        self.assertEqual([v[2] for v in self.votes()], ['a', 'a'])

    async def test_reply_waits_for_confirmed_delivery_and_notifies_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def deliver(event, result):
            started.set()
            await release.wait()
            return BattleMessageReceipt('123')
        self.host._deliver_battle_message = deliver
        generation = asyncio.create_task(collect(self.host.battle(Event(), 'offline')))
        try:
            await asyncio.wait_for(started.wait(), 1)
            # No known message ID before the confirmed successful send.
            reply = await collect(self.host.battle_reply(QuoteEvent()))
            self.assertIn('失败', text(reply))
            self.assertFalse(self.votes())
            release.set()
            await asyncio.wait_for(generation, 1)
            await collect(self.host.battle_reply(QuoteEvent()))
            self.assertEqual(len(self.votes()), 1)
        finally:
            release.set()
            if not generation.done(): generation.cancel()

    async def test_binding_save_failure_keeps_both_memory_and_disk_unvotable(self):
        original = self.host._battle_save
        def failing_save():
            if any(e['platform_message_id'] for e in self.host._battle_sessions.values()):
                raise OSError('private disk unavailable')
            original()
        self.host._battle_save = failing_save
        await self.begin()
        entry = next(iter(self.host._battle_sessions.values()))
        self.assertIsNone(entry['vote'])
        self.assertEqual(entry['platform_message_id'], '')
        saved = json.loads(self.host._battle_sessions_file.read_text())
        self.assertIsNone(next(iter(saved.values()))['vote'])
        self.assertEqual(next(iter(saved.values()))['platform_message_id'], '')
        self.assertIn('失败', text(await collect(self.host.battle_reply(QuoteEvent()))))
        self.assertFalse(self.votes())

    async def test_legacy_session_defaults_to_no_quote_binding(self):
        code = await self.begin()
        data = json.loads(self.host._battle_sessions_file.read_text())
        del data[code]['platform_message_id']
        self.host._battle_sessions_file.write_text(json.dumps(data))
        restarted = Host(self.temp.name, self.host.config, self.host.client)
        self.assertEqual(restarted._battle_sessions[code]['platform_message_id'], '')
        self.assertIn('失败', text(await collect(restarted.battle_reply(QuoteEvent()))))
        self.assertFalse(self.votes())

    def test_filter_exact_words_and_native_quote_only(self):
        filt = BattleReplyFilter()
        for intent in ('a', 'A', 'b', 'B', '不投票', ' A '):
            self.assertTrue(filt.filter(QuoteEvent(intent), {}))
        for intent in ('ab', 'a please', 'AA', '投票 A', '不投票了', 'battle A'):
            self.assertFalse(filt.filter(QuoteEvent(intent), {}))
        rewritten = QuoteEvent('/A')
        rewritten.intent = 'A'  # Simulate WakingCheckStage prefix removal.
        self.assertFalse(filt.filter(rewritten, {}))
        rewritten = QuoteEvent('/不投票')
        rewritten.intent = '不投票'
        self.assertFalse(filt.filter(rewritten, {}))
        no_quote = QuoteEvent()
        no_quote.message_obj.message = [Plain('A')]
        self.assertFalse(filt.filter(no_quote, {}))
        conflicting = QuoteEvent()
        conflicting.message_obj.message.append(Reply(id='456'))
        self.assertFalse(filt.filter(conflicting, {}))
        for bad in (None, True, 0, '', 'https://private', 'x' * 1000):
            self.assertEqual(message_id(bad), '')
        for good in (123, '123', '-123'):
            self.assertEqual(message_id(good), str(good))
