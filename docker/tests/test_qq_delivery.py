"""Scoped deadlines, routing, positive receipts and no uncertain fallback."""
import asyncio
import functools
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from astrbot_plugin_arena_image.qq_delivery import (
    _scoped_api,
    prepare_qq_image_sender,
)


class LeafApi:
    def __getattr__(self, name):
        return functools.partial(self.call_action, name)

    def __init__(self, result=None, delay=0, error=None):
        self._timeout_sec = 0.01
        self.calls = []
        self.result = {"message_id": 42} if result is None else result
        self.delay = delay
        self.error = error
        self._api_clients = {}

    async def call_action(self, action, **params):
        self.calls.append((action, params, self._timeout_sec))
        await asyncio.wait_for(asyncio.sleep(self.delay), self._timeout_sec)
        if self.error:
            raise self.error
        return self.result


class Unified:
    def __getattr__(self, name):
        return functools.partial(self.call_action, name)

    def __init__(self, leaf):
        self._http_api = leaf
        self._wsr_api = leaf

    async def call_action(self, action, **params):
        return await self._wsr_api.call_action(action, **params)


class Event:
    def __init__(self, api, group="123", self_id="456"):
        self.bot = SimpleNamespace(_api=api)
        self.group = group
        self.message_obj = SimpleNamespace(
            raw_message={"self_id": self_id}, self_id=self_id,
        )
        self.sent_bookkeeping = False

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return "789"

    async def _parse_onebot_json(self, chain):
        return chain


class BaseEvent:
    async def send(self, chain):
        self.sent_bookkeeping = True


class QQDeliveryTest(unittest.TestCase):
    def setUp(self):
        adapter = ModuleType("fake_adapter")
        adapter.AiocqhttpMessageEvent = Event
        api = ModuleType("fake_api")
        api.AstrMessageEvent = BaseEvent
        self.modules = patch.dict(sys.modules, {
            "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event": adapter,
            "astrbot.api.event": api,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_copies_both_deadlines_but_shares_live_connections(self):
        leaf = LeafApi()
        original = Unified(leaf)
        scoped = _scoped_api(original, 135)
        self.assertIsNot(scoped, original)
        for name in ("_http_api", "_wsr_api"):
            copied = getattr(scoped, name)
            self.assertEqual(copied._timeout_sec, 135)
            self.assertIs(copied._api_clients, leaf._api_clients)
        self.assertEqual(leaf._timeout_sec, 0.01)
        self.assertIs(original._wsr_api, leaf)

    def test_delayed_positive_receipt_succeeds_once_after_old_deadline(self):
        leaf = LeafApi(delay=0.05)
        event = Event(Unified(leaf))
        sender = prepare_qq_image_sender(event, 120)
        chain = [{"type": "image", "data": {"file": "base64://FIXTURE"}}]
        self.assertEqual(asyncio.run(sender(chain)), {"message_id": 42})
        self.assertTrue(event.sent_bookkeeping)
        self.assertEqual(len(leaf.calls), 1)
        action, params, client_timeout = leaf.calls[0]
        self.assertEqual(action, "send_group_msg")
        self.assertEqual(params["group_id"], 123)
        self.assertEqual(params["self_id"], "456")
        self.assertEqual(params["timeout"], 120000)
        self.assertEqual(client_timeout, 135)
        self.assertEqual(params["message"], chain)
        self.assertEqual(leaf._timeout_sec, 0.01)

    def test_private_routing_and_missing_receipt_remains_uncertain(self):
        leaf = LeafApi(result={})
        event = Event(leaf, group="")
        with self.assertRaisesRegex(RuntimeError, "confirmed message_id"):
            asyncio.run(prepare_qq_image_sender(event, 120)([{"type": "image"}]))
        self.assertEqual(len(leaf.calls), 1)
        action, params, _ = leaf.calls[0]
        self.assertEqual(action, "send_private_msg")
        self.assertEqual(params["user_id"], 789)
        self.assertNotIn("group_id", params)
        self.assertFalse(event.sent_bookkeeping)

    def test_timeout_never_calls_a_second_transport(self):
        leaf = LeafApi(error=TimeoutError("late receipt"))
        event = Event(Unified(leaf))
        with self.assertRaises(TimeoutError):
            asyncio.run(prepare_qq_image_sender(event, 120)([{"type": "image"}]))
        self.assertEqual(len(leaf.calls), 1)
        self.assertFalse(event.sent_bookkeeping)

    def test_other_platform_and_unsupported_adapter_keep_framework_sender(self):
        self.assertIsNone(prepare_qq_image_sender(object(), 120))
        self.assertIsNone(prepare_qq_image_sender(Event(object()), 120))

    def test_preserves_larger_existing_deadline(self):
        leaf = LeafApi()
        leaf._timeout_sec = 300
        self.assertEqual(_scoped_api(leaf, 135)._timeout_sec, 300)

    def test_two_concurrent_sends_cannot_change_shared_client_timeout(self):
        leaf = LeafApi(delay=0.01)
        first = prepare_qq_image_sender(Event(leaf), 120)
        second = prepare_qq_image_sender(Event(leaf), 180)

        async def run():
            await asyncio.gather(first([{"type": "image"}]), second([{"type": "image"}]))

        asyncio.run(run())
        self.assertEqual({r[2] for r in leaf.calls}, {135, 195})
        self.assertEqual(leaf._timeout_sec, 0.01)


if __name__ == "__main__":
    unittest.main()
