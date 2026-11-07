"""Scope arbitration does not recreate browsers or consume ordinary state."""
import asyncio
import unittest
from astrbot_plugin_arena_image.video_runtime import browser_lock

class VideoRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_endpoint_serializes_and_cancellation_releases(self):
        a=browser_lock('http://offline.invalid:9223')
        self.assertIs(a,browser_lock('http://offline.invalid:9223/'))
        self.assertIsNot(a,browser_lock('http://other.invalid:9223'))
        started=asyncio.Event()
        async def waiter():
            async with browser_lock('http://offline.invalid:9223'):
                started.set()
        async with a:
            task=asyncio.create_task(waiter())
            await asyncio.sleep(0)
            self.assertFalse(started.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        async with a:self.assertFalse(a._waiters and any(not x.done() for x in a._waiters))
