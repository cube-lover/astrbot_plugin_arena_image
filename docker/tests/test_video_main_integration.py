"""Narrow integration: preserve image handlers, route only exact video receipts."""
import ast
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .test_arena_image_hardening import _make_plugin, _plugin_module
from .test_battle_reply import QuoteEvent
from .test_battle_commands import collect, text


class VideoMainIntegrationCase(unittest.TestCase):
    def setUp(self):
        self.main, self.plugin = _make_plugin(self)
        self.video = SimpleNamespace(_battle_sessions={})
        self.plugin._video_controller = lambda: self.video
        self.plugin._battle_sessions = {}

    def test_video_receipt_routes_to_video_only(self):
        event = QuoteEvent('A', '123')
        chat = event.unified_msg_origin
        self.video._battle_sessions = {'ABCDEF': {'chat':chat, 'platform_message_id':'123'}}
        called = []
        async def reply(ev):
            called.append(ev)
            yield ev.plain_result('video')
        self.video.battle_reply = reply
        import asyncio
        result = asyncio.run(collect(self.plugin._battle_reply_generator(event)))
        self.assertIn('video', text(result))
        self.assertEqual(called, [event])

    def test_receipt_collision_fails_closed(self):
        event = QuoteEvent('A', '123')
        entry={'chat':event.unified_msg_origin,'platform_message_id':'123'}
        self.video._battle_sessions={'ABCDEF':entry}
        self.plugin._battle_sessions={'FEDCBA':entry.copy()}
        import asyncio
        result=asyncio.run(collect(self.plugin._battle_reply_generator(event)))
        self.assertIn('未提交投票', text(result))

    def test_no_video_match_keeps_image_dispatch(self):
        event = QuoteEvent('A', '123')
        sentinel=object()
        with patch.object(self.main.BattleCommandsMixin,'battle_reply',return_value=sentinel) as image:
            self.assertIs(self.plugin._battle_reply_generator(event),sentinel)
            image.assert_called_once_with(self.plugin,event)

    def test_single_original_quote_entry_not_competing_filter(self):
        root=Path(__file__).resolve().parents[2]
        tree=ast.parse((root/'main.py').read_text(encoding='utf-8'))
        cls=next(x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=='ArenaImagePlugin')
        entries=[n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and any(
            isinstance(d,ast.Call) and isinstance(d.func,ast.Attribute) and d.func.attr=='custom_filter'
            and any(isinstance(a,ast.Name) and a.id=='BattleReplyFilter' for a in d.args) for d in n.decorator_list)]
        self.assertEqual([n.name for n in entries],['battle_reply_entry'])

    def test_explicit_image_command_rejects_ambiguous_quote(self):
        event = QuoteEvent('A', '123')
        entry={'chat':event.unified_msg_origin, 'platform_message_id':'123'}
        self.video._battle_sessions={'ABCDEF':entry}
        self.plugin._battle_sessions={'FEDCBA':entry.copy()}
        import asyncio
        with patch.object(self.main.BattleCommandsMixin,'battle_vote',side_effect=AssertionError('ambiguous vote')):
            result=asyncio.run(collect(self.plugin.battle_vote(event, 'A FEDCBA')))
        self.assertIn('未提交投票', text(result))
