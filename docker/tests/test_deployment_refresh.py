"""Real ArenaPage.fetch control flow for explicit stale-deployment conflicts."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from astrbot_plugin_arena_image import arena_direct as direct
from tests.test_arena_direct_transport import _chunked_cookies, _session_token


class ScriptedPage(direct.ArenaPage):
    def __init__(self, responses, *, reload_ok=True):
        super().__init__(None, "FIXTURE", "https://arena.ai/text/direct")
        self.responses = list(responses)
        self.expressions = []
        self.reload = AsyncMock(return_value=reload_ok)
        self.mint_recaptcha = AsyncMock(return_value="NEW_VERIFICATION_TOKEN")
        self.cookies = AsyncMock(return_value=_chunked_cookies(_session_token()))

    async def evaluate(self, expression, *, timeout=None):
        self.expressions.append(expression)
        return self.responses.pop(0)


STALE = {"status": 409, "headers": {}, "text": '{"error":"refresh_required"}'}
SUCCESS = {"status": 200, "headers": {}, "text": '{"models":[]}'}


class DeploymentRefreshTest(unittest.TestCase):
    def setUp(self):
        direct._LOCK = asyncio.Lock()
        direct._ACTIONS = {"generateUploadUrl": "OLD_ACTION"}
        direct._ACTIONS_AT = 100
        direct._MODELS = [{"id": "KEEP_STORED_MODEL"}]
        direct._MODELS_AT = 100

    def test_only_explicit_409_or_header_triggers_refresh(self):
        self.assertTrue(direct.deployment_refresh_required(STALE))
        self.assertTrue(direct.deployment_refresh_required({
            "status": 409, "text": "",
            "headers": {"X-Arena-Refresh-Required": "1"},
        }))
        for response in [
            {"status": 409, "text": '{"error":"conflict"}'},
            {"status": 200, "text": STALE["text"]},
            {"status": 429, "headers": {"x-arena-refresh-required": "1"}},
            {"status": 409, "text": "not JSON"},
        ]:
            self.assertFalse(direct.deployment_refresh_required(response))

    def test_read_only_request_refreshes_original_page_and_retries_once(self):
        page = ScriptedPage([STALE, SUCCESS])
        result = asyncio.run(page.fetch("/nextjs-api/model-catalog", method="GET"))
        self.assertEqual(result["status"], 200)
        page.reload.assert_awaited_once()
        self.assertEqual(len(page.expressions), 2)
        self.assertEqual(direct._ACTIONS, {})
        self.assertEqual(direct._MODELS_AT, 0)
        self.assertEqual(direct._MODELS, [{"id": "KEEP_STORED_MODEL"}])
        page.mint_recaptcha.assert_not_awaited()

    def test_generation_keeps_request_identity_attachments_and_prompt(self):
        page = ScriptedPage([STALE, SUCCESS])
        payload = {
            "id": "EVALUATION_ID",
            "userMessageId": "USER_MESSAGE_ID",
            "modelAMessageId": "MODEL_MESSAGE_ID",
            "modelAId": "MODEL_ID",
            "mode": "direct-battle",
            "userMessage": {"content": "neutral apple", "experimental_attachments": [
                {"name": "reference.png", "url": "https://fixture.example/ref"},
            ]},
            "recaptchaV3Token": "OLD_VERIFICATION_TOKEN",
        }
        asyncio.run(page.fetch(direct.STREAM_CREATE_EVALUATION_PATH, body=json.dumps(payload)))
        self.assertEqual(len(page.expressions), 2)
        second = page.expressions[1]
        self.assertNotIn("OLD_VERIFICATION_TOKEN", second)
        for value in [
            "EVALUATION_ID", "USER_MESSAGE_ID", "MODEL_MESSAGE_ID", "MODEL_ID",
            "neutral apple", "reference.png", "https://fixture.example/ref",
            "NEW_VERIFICATION_TOKEN",
        ]:
            self.assertIn(value, second)
        page.mint_recaptcha.assert_awaited_once_with("chat_submit")
        page.reload.assert_awaited_once()

    def test_second_conflict_does_not_create_an_infinite_reload_loop(self):
        page = ScriptedPage([STALE, STALE, SUCCESS])
        result = asyncio.run(page.fetch("/nextjs-api/model-catalog", method="GET"))
        self.assertEqual(result["status"], 409)
        self.assertEqual(len(page.expressions), 2)
        page.reload.assert_awaited_once()

    def test_lost_login_after_reload_stops_before_second_generation(self):
        page = ScriptedPage([STALE, SUCCESS])
        page.cookies = AsyncMock(return_value=[])
        with self.assertRaises(direct.BridgeError) as caught:
            asyncio.run(page.fetch(direct.STREAM_CREATE_EVALUATION_PATH, body="{}"))
        self.assertEqual(caught.exception.code, "arena_auth_required")
        self.assertEqual(len(page.expressions), 1)
        page.mint_recaptcha.assert_not_awaited()

    def test_failed_reload_does_not_reissue_generation(self):
        page = ScriptedPage([STALE, SUCCESS], reload_ok=False)
        result = asyncio.run(page.fetch(direct.STREAM_CREATE_EVALUATION_PATH, body="{}"))
        self.assertEqual(result["status"], 409)
        self.assertEqual(len(page.expressions), 1)
        page.mint_recaptcha.assert_not_awaited()

    def test_unknown_conflict_timeout_or_success_never_retries(self):
        for status, body in [
            (409, '{"error":"already_exists"}'),
            (403, "forbidden"),
            (429, "too many requests"),
            (0, "FETCH_ERROR:Timeout"),
            (200, "success"),
        ]:
            page = ScriptedPage([{"status": status, "text": body}, SUCCESS])
            result = asyncio.run(page.fetch(direct.STREAM_CREATE_EVALUATION_PATH, body="{}"))
            self.assertEqual(result["status"], status)
            page.reload.assert_not_awaited()
            self.assertEqual(len(page.expressions), 1)

    def test_upload_action_conflict_uses_existing_action_id_rediscovery(self):
        page = ScriptedPage([STALE])
        with self.assertRaises(direct._StaleActionError):
            asyncio.run(page.fetch("/text/direct", headers={"Next-Action": "OLD_ACTION"}, body="[]"))
        # The upload pipeline owns its one reload + ID rescan; no nested retry.
        page.reload.assert_not_awaited()
        self.assertEqual(len(page.expressions), 1)

    def test_upstream_error_does_not_describe_this_as_login_failure(self):
        client = direct.ArenaDirectClient("http://arena-browser:9223")
        with self.assertRaises(direct.BridgeError) as caught:
            client._raise_upstream(
                status=409, body=STALE["text"], stream={}, headers={},
                state={"has_logged_in": True}, browser_url="",
                public_name="FIXTURE",
            )
        self.assertEqual(caught.exception.code, "arena_deployment_refresh_required")
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
