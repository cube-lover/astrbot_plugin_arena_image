import unittest
from unittest.mock import AsyncMock, patch

import httpx

from tests._stream_test_utils import BaseBridgeTest, FakeStreamContext, FakeStreamResponse

_MODEL = "test-search-model"
_PROMPT = "draw the same thing"


class TestStatelessRequestStartsNewSession(BaseBridgeTest):
    """A request without assistant history must never continue a cached Arena session."""

    def _fake_stream(self, calls: list[tuple[str, str, dict]]):
        def fake_stream(_self, method, url, json=None, headers=None, timeout=None):  # noqa: ARG001
            calls.append((str(method), str(url), dict(json or {})))
            return FakeStreamContext(
                FakeStreamResponse(
                    status_code=200,
                    headers={},
                    text='a0:"Hello"\nad:{"finishReason":"stop"}\n',
                )
            )

        return fake_stream

    async def _post(self, client: httpx.AsyncClient, messages: list[dict]) -> httpx.Response:
        return await client.post(
            "/api/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={"model": _MODEL, "messages": messages, "stream": True},
            timeout=30.0,
        )

    async def _run(self, conversations: list[list[dict]]) -> list[tuple[str, str, dict]]:
        calls: list[tuple[str, str, dict]] = []

        with patch.object(self.main, "get_models") as get_models_mock, patch.object(
            self.main,
            "refresh_recaptcha_token",
            AsyncMock(return_value="recaptcha-token"),
        ), patch.object(
            httpx.AsyncClient,
            "stream",
            new=self._fake_stream(calls),
        ), patch("src.main.print"):
            get_models_mock.return_value = [
                {
                    "publicName": _MODEL,
                    "id": "model-id",
                    "organization": "test-org",
                    "capabilities": {
                        "inputCapabilities": {"text": True},
                        "outputCapabilities": {"search": True},
                    },
                }
            ]

            transport = httpx.ASGITransport(app=self.main.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for messages in conversations:
                    response = await self._post(client, messages)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("[DONE]", response.text)

        return calls

    async def test_repeated_identical_prompt_creates_a_new_session_each_time(self) -> None:
        one_shot = [{"role": "user", "content": _PROMPT}]
        calls = await self._run([one_shot, one_shot, one_shot])

        self.assertEqual(len(calls), 3)
        for index, (method, url, payload) in enumerate(calls):
            with self.subTest(call=index):
                # Same prompt hashes to the same conversation_id, but each call must still
                # open its own Arena evaluation session instead of appending to the last one.
                self.assertEqual(method, "POST")
                self.assertIn(self.main.STREAM_CREATE_EVALUATION_PATH, url)
                self.assertNotIn("post-to-evaluation", url)
                self.assertNotIn("retry-evaluation-session-message", url)
                self.assertEqual(payload.get("modelAId"), "model-id")

        session_ids = [payload.get("id") for _, _, payload in calls]
        self.assertEqual(len(set(session_ids)), 3, f"session ids reused: {session_ids}")

    async def test_client_supplied_history_still_continues_the_session(self) -> None:
        calls = await self._run(
            [
                [{"role": "user", "content": _PROMPT}],
                [
                    {"role": "user", "content": _PROMPT},
                    {"role": "assistant", "content": "Hello"},
                    {"role": "user", "content": "and now zoom out"},
                ],
            ]
        )

        self.assertEqual(len(calls), 2)
        self.assertIn(self.main.STREAM_CREATE_EVALUATION_PATH, calls[0][1])

        follow_up_method, follow_up_url, follow_up_payload = calls[1]
        self.assertEqual(follow_up_method, "POST")
        self.assertIn("post-to-evaluation", follow_up_url)
        self.assertTrue(follow_up_url.endswith(str(calls[0][2].get("id"))))
        self.assertEqual(follow_up_payload.get("id"), calls[0][2].get("id"))

    async def test_stale_cached_session_is_dropped_for_a_stateless_request(self) -> None:
        calls = await self._run([[{"role": "user", "content": _PROMPT}]])
        self.assertEqual(len(calls), 1)

        stored = self.main.chat_sessions.get("test-key") or {}
        self.assertEqual(len(stored), 1, "the first call should still be cached for real follow-ups")

        calls = await self._run([[{"role": "user", "content": _PROMPT}]])
        stored_after = self.main.chat_sessions.get("test-key") or {}
        self.assertEqual(len(stored_after), 1, "the stale entry must be replaced, not accumulated")
        self.assertEqual(
            stored_after[next(iter(stored_after))]["conversation_id"],
            calls[0][2].get("id"),
        )


if __name__ == "__main__":
    unittest.main()
