"""Offline video client tests: one protected create, exact readback, no replay.

Terminal Flight fixtures are synthetic, omit modality/assistant metadata, and
use explicit success plus MP4 attachments. They are not live acceptance proof.
"""
import asyncio
import json
import traceback
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot_plugin_arena_image import arena_direct, video_client
from astrbot_plugin_arena_image.bridge_client import BridgeError
from .test_arena_direct_transport import (
    DirectTransportCase, FakeHTTPX, _flight, _png_bytes, _png_data_url,
)

EVALUATION_ID = '11111111-1111-7111-8111-111111111111'
MESSAGE_A_ID = '22222222-2222-7222-8222-222222222222'
MESSAGE_B_ID = '33333333-3333-7333-8333-333333333333'
USER_ID = '44444444-4444-7444-8444-444444444444'
REFERENCE_KEY = 'uploads/offline-video-reference.png'
REFERENCE_URL = 'https://storage.example.invalid/reference.png'
ATTACHMENT = {'name': REFERENCE_KEY, 'contentType': 'image/png', 'url': REFERENCE_URL}
CONTEXT = {
    'evaluationSessionId': EVALUATION_ID, 'messageAId': MESSAGE_A_ID,
    'messageBId': MESSAGE_B_ID, 'browser_scope': 'cdp:offline', 'modality': 'video',
}
URLS = {side: f'https://videos.example.invalid/{side}.mp4' for side in ('a', 'b')}


def terminal_session():
    return {
        'id': EVALUATION_ID, 'mode': 'battle', 'pairwiseFeedbacks': [],
        'messages': [
            {'id': USER_ID, 'evaluationSessionId': EVALUATION_ID, 'role': 'user',
             'status': 'success', 'experimental_attachments': [dict(ATTACHMENT)]},
            *[
                {'id': MESSAGE_A_ID if side == 'a' else MESSAGE_B_ID,
                 'evaluationSessionId': EVALUATION_ID, 'role': 'assistant',
                 'participantPosition': side, 'status': 'success',
                 'experimental_attachments': [{'contentType': 'video/mp4', 'url': URLS[side]}]}
                for side in ('a', 'b')
            ],
        ],
    }


def expected_result():
    return {
        'evaluation_id': EVALUATION_ID, 'context': {**CONTEXT, 'mode': 'battle'},
        'completed': True,
        'sides': {
            side: {'text': '', 'videos': [
                {'url': URLS[side], 'mimeType': 'video/mp4', 'thumbnailUrl': '',
                 'contentType': 'video/mp4'},
            ], 'error': False, 'finish_reason': '', 'status': 'success',
                'run_id': '', 'completed': True, 'finished': True}
            for side in ('a', 'b')
        },
    }


class VideoClientCase(DirectTransportCase):
    def setUp(self):
        super().setUp()
        self.client = video_client.ArenaVideoClient('http://offline.invalid:9223', timeout=10)
        self.page = SimpleNamespace(
            cache_key='cdp:offline', evaluate=AsyncMock(),
            fetch=AsyncMock(side_effect=AssertionError('unplanned page fetch forbidden')),
            mint_recaptcha=AsyncMock(return_value='offline-live-mint'),
        )
        self.opened = []
        self.events = []
        self.saved = []

        @asynccontextmanager
        async def fake_page(*args, **kwargs):
            self.opened.append((args, kwargs))
            yield self.page

        self._patch(self.client, '_page', fake_page)
        self._patch(self.client, '_battle_page', fake_page)
        self.live_state = AsyncMock(return_value={
            'has_arena_auth': True, 'has_logged_in': True, 'session_expired': False,
        })
        self._patch(video_client, '_live_state', self.live_state)
        self._patch(video_client, 'new_uuid7', Mock(side_effect=[
            EVALUATION_ID, MESSAGE_A_ID, MESSAGE_B_ID, USER_ID,
        ]))

    def prepare_battle(self):
        async def upload(page, image):
            self.assertIs(page, self.page)
            self.assertEqual(image, _png_data_url())
            self.events.append('upload')
            return dict(ATTACHMENT)

        async def save(context):
            self.saved.append(dict(context))
            self.events.append('save')

        async def fetch(page, path, *, method='GET', body=None, timeout=30.0):
            self.assertIs(page, self.page)
            if method == 'POST':
                # Persisted context must exist before the protected create.
                self.assertEqual(self.saved, [CONTEXT])
                self.events.append('create')
                return {'status': 200, 'text': 'ad:{"finishReason":"stop"}\nbd:{"finishReason":"stop"}'}
            self.assertEqual(path, '/c/' + EVALUATION_ID)
            self.assertIsNone(body)
            self.events.append('read')
            return {'status': 200, 'text': _flight(terminal_session())}

        self.client._upload_reference = AsyncMock(side_effect=upload)
        self.client._video_context_callback = AsyncMock(side_effect=save)
        self.client._video_fetch = AsyncMock(side_effect=fetch)

    def assert_create_payload(self, *, attachments):
        calls = self.client._video_fetch.await_args_list
        create = calls[0]
        self.assertEqual(create.args, (self.page, arena_direct.STREAM_CREATE_EVALUATION_PATH))
        self.assertEqual(create.kwargs['method'], 'POST')
        self.assertEqual(create.kwargs['timeout'], self.client.timeout)
        sent = json.loads(create.kwargs['body'])
        self.assertEqual(sent, {
            'id': EVALUATION_ID, 'mode': 'battle', 'modality': 'video',
            'userMessageId': USER_ID, 'modelAMessageId': MESSAGE_A_ID,
            'modelBMessageId': MESSAGE_B_ID, 'recaptchaV3Token': 'offline-live-mint',
            'userMessage': {'content': '参考图动起来',
                            'experimental_attachments': attachments, 'metadata': {}},
        })
        for field in ('modelAId', 'modelBId', 'modelA', 'modelB', 'cx'):
            self.assertNotIn(field, sent)
        ids = [sent[key] for key in ('id', 'userMessageId', 'modelAMessageId', 'modelBMessageId')]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(uuid.UUID(value).version == 7 for value in ids))

    def assert_sanitized(self, error):
        public = str(error) + repr(error) + json.dumps(error.payload) + ''.join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        for marker in ('secret', 'Hidden Provider', 'storage.example.invalid', 'FETCH_ERROR'):
            self.assertNotIn(marker, public)

    def test_protected_fetch_does_not_use_reloading_page_fetch(self):
        self.page.evaluate.return_value = {'status': 409, 'text': '{}'}
        result = self.run_async(self.client._video_fetch(
            self.page, arena_direct.STREAM_CREATE_EVALUATION_PATH,
            method='POST', body='{}', timeout=10,
        ))
        self.assertEqual(result, {
            'status': 409, 'headers': {}, 'text': '', 'error': {'code': 'http_409'},
        })
        self.page.fetch.assert_not_awaited()
        self.page.evaluate.assert_awaited_once()
        expression = self.page.evaluate.await_args.args[0]
        self.assertIn('AbortController', expression)
        self.assertIn('signal: c.signal', expression)
        self.assertIn('10000', expression)
        self.assertIn('finally { clearTimeout(timer); }', expression)
        self.assertIn("credentials: 'include'", expression)
        self.assertNotIn('String(e)', expression)
        self.assertEqual(self.page.evaluate.await_args.kwargs, {'timeout': 15.0})

    def test_timeout_is_sanitized_and_not_retried(self):
        self.page.evaluate.side_effect = RuntimeError('https://storage.example.invalid/?signature=secret')
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client._video_fetch(
                self.page, arena_direct.STREAM_CREATE_EVALUATION_PATH, method='POST', body='{}',
            ))
        self.assert_sanitized(caught.exception)
        self.assertEqual(caught.exception.code, 'video_transport_unconfirmed')
        self.page.evaluate.assert_awaited_once()

    def test_actual_fetch_http_errors_keep_only_observed_status(self):
        for status in (429, 403, 404, 503):
            with self.subTest(status=status):
                self.page.evaluate.reset_mock()
                self.page.evaluate.return_value = {
                    'status': status, 'text': 'Hidden Provider https://storage.example.invalid/?signature=secret',
                    'headers': {'set-cookie': 'secret'}, 'error': {'code': 'cloudflare_challenge'},
                }
                result = self.run_async(self.client._video_fetch(self.page, '/c/' + EVALUATION_ID))
                self.assertEqual(result, {
                    'status': status, 'headers': {}, 'text': '', 'error': {'code': f'http_{status}'},
                })
                self.page.evaluate.assert_awaited_once()
                self.page.fetch.assert_not_awaited()

    def test_page_fetch_error_is_sanitized_without_body_reason_guessing(self):
        self.page.evaluate.return_value = {
            'status': 0, 'text': 'FETCH_ERROR: secret Hidden Provider',
            'headers': {'set-cookie': 'secret'},
        }
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client._video_fetch(self.page, '/c/' + EVALUATION_ID))
        self.assertEqual(caught.exception.code, 'video_transport_unconfirmed')
        self.assertIsNone(caught.exception.status_code)
        self.assert_sanitized(caught.exception)
        self.page.evaluate.assert_awaited_once()

    def test_explicit_page_abort_has_sanitized_timeout_classification(self):
        self.page.evaluate.return_value = {
            'status': 0, 'text': 'secret', 'error': {'code': 'video_fetch_timeout'},
        }
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client._video_fetch(self.page, '/c/' + EVALUATION_ID))
        self.assertEqual(caught.exception.code, 'video_fetch_timeout')
        self.assert_sanitized(caught.exception)
        self.page.evaluate.assert_awaited_once()

    def test_invalid_fetch_responses_raise_sanitized_errors(self):
        for response in (None, [], {}, {'status': 'secret'}, {'status': True},
                         {'status': 600}, {'status': 200, 'text': {'secret': True}}):
            with self.subTest(response=response):
                self.page.evaluate.reset_mock()
                self.page.evaluate.return_value = response
                with self.assertRaises(BridgeError) as caught:
                    self.run_async(self.client._video_fetch(self.page, '/c/' + EVALUATION_ID))
                self.assertEqual(caught.exception.code, 'video_response_invalid')
                self.assert_sanitized(caught.exception)
                self.page.evaluate.assert_awaited_once()

    def test_successful_fetch_preserves_flight_for_exact_validation(self):
        response = {'status': 200, 'headers': {}, 'text': _flight(terminal_session())}
        self.page.evaluate.return_value = response
        self.assertEqual(self.run_async(self.client._video_fetch(self.page, '/c/' + EVALUATION_ID)), response)

    def test_exact_flight_session_only(self):
        session = terminal_session()
        self.assertNotIn('modality', session)
        self.assertEqual(video_client._session_from_page(_flight(session), EVALUATION_ID), session)
        with self.assertRaises(BridgeError):
            video_client._session_from_page(_flight(session), USER_ID)
        with self.assertRaises(BridgeError):
            video_client._session_from_page(
                _flight(session) + '2:' + json.dumps({**session, 'mode': 'direct'}), EVALUATION_ID,
            )

    def test_cancellation_propagates(self):
        self.page.evaluate.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            self.run_async(self.client._video_fetch(self.page, arena_direct.STREAM_CREATE_EVALUATION_PATH))
        self.page.evaluate.assert_awaited_once()

    def test_oversized_or_deep_flight_fails_at_extraction(self):
        with self.assertRaises(BridgeError):
            video_client._session_from_page('x' * (8 * 1024 * 1024 + 1), 'unknown')
        value = {'child': None}
        for _ in range(40):
            value = {'child': value}
        with self.assertRaises(BridgeError):
            video_client._session_from_page('a:' + json.dumps(value), 'unknown')

    def test_begin_reference_upload_once_anonymous_payload_and_saved_context(self):
        self.prepare_battle()
        result = self.run_async(self.client.begin_battle(prompt=' 参考图动起来 ', images=[_png_data_url()]))
        self.assertEqual(result, expected_result())
        self.client._upload_reference.assert_awaited_once_with(self.page, _png_data_url())
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)
        self.assertEqual(self.events, ['upload', 'save', 'create', 'read'])
        self.assert_create_payload(attachments=[ATTACHMENT])
        self.assertEqual(self.client._video_fetch.await_count, 2)
        self.page.mint_recaptcha.assert_awaited_once_with('chat_submit')
        self.live_state.assert_awaited_once_with(self.page)
        self.page.evaluate.assert_not_awaited()
        self.page.fetch.assert_not_awaited()

    def test_begin_real_uploader_receives_image_and_storage_key_enters_payload(self):
        # Exercise the inherited uploader, with only storage/PUT I/O faked.
        real_upload = self.client._upload_reference
        self.prepare_battle()
        self.client._upload_reference = AsyncMock(wraps=real_upload)
        self.page.fetch = AsyncMock(side_effect=[
            {'status': 200, 'text': json.dumps({
                'uploadUrl': 'https://storage.example.invalid/put', 'key': REFERENCE_KEY,
            })},
            {'status': 200, 'text': json.dumps({'url': REFERENCE_URL})},
        ])
        httpx = FakeHTTPX()
        self._patch(arena_direct, 'httpx', httpx)
        result = self.run_async(self.client.begin_battle(prompt='参考图动起来', images=[_png_data_url()]))
        self.assertEqual(result, expected_result())
        self.client._upload_reference.assert_awaited_once_with(self.page, _png_data_url())
        self.assertEqual(len(httpx.puts), 1)
        self.assertEqual(httpx.puts[0]['size'], len(_png_bytes()))
        self.assertEqual([call.args[0] for call in self.page.fetch.await_args_list], [
            '/api/storage/generate-upload-url', '/api/storage/get-signed-url',
        ])
        self.assertEqual(json.loads(self.page.fetch.await_args_list[1].kwargs['body']), {'key': REFERENCE_KEY})
        self.assert_create_payload(attachments=[ATTACHMENT])
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)
        self.assertEqual(self.events, ['save', 'create', 'read'])

    def test_begin_without_reference_does_not_upload(self):
        self.prepare_battle()
        result = self.run_async(self.client.begin_battle(prompt='参考图动起来'))
        self.assertEqual(result, expected_result())
        self.client._upload_reference.assert_not_awaited()
        self.assert_create_payload(attachments=[])
        self.assertEqual(self.events, ['save', 'create', 'read'])

    def test_begin_explicit_429_has_no_retry_or_readback(self):
        self.prepare_battle()
        self.client._video_fetch.side_effect = None
        # Opaque synthetic body: do not claim this is the observed live reason.
        self.client._video_fetch.return_value = {'status': 429, 'text': 'x' * 37}
        self.client.rate_limit_retries = 5
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client.begin_battle(prompt='参考图动起来', images=[_png_data_url()]))
        error = caught.exception
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.code, 'http_429')
        self.assertTrue(error.is_rate_limited)
        self.assertFalse(error.requires_interactive_auth)
        self.assertIn('HTTP 429', str(error))
        self.assertNotIn('x' * 37, str(error))
        self.assert_sanitized(error)
        self.client._video_fetch.assert_awaited_once()
        self.client._upload_reference.assert_awaited_once_with(self.page, _png_data_url())
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)
        self.assertEqual(self.saved, [CONTEXT])
        self.assert_create_payload(attachments=[ATTACHMENT])

    def test_begin_actual_429_fetch_classification_reaches_caller(self):
        real_fetch = self.client._video_fetch
        self.prepare_battle()
        self.client._video_fetch = AsyncMock(wraps=real_fetch)
        self.page.evaluate.return_value = {
            'status': 429, 'text': 'secret Hidden Provider', 'headers': {'set-cookie': 'secret'},
        }
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client.begin_battle(prompt='参考图动起来', images=[_png_data_url()]))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.code, 'http_429')
        self.assertTrue(caught.exception.is_rate_limited)
        self.assert_sanitized(caught.exception)
        self.page.evaluate.assert_awaited_once()
        self.page.fetch.assert_not_awaited()
        self.client._video_fetch.assert_awaited_once()
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)
        self.assertEqual(self.events, ['upload', 'save'])

    def test_begin_cancelled_create_keeps_context_already_saved(self):
        self.prepare_battle()

        async def cancel(*args, **kwargs):
            self.assertEqual(self.saved, [CONTEXT])
            self.assertEqual(kwargs['method'], 'POST')
            self.events.append('create')
            raise asyncio.CancelledError()

        self.client._video_fetch.side_effect = cancel
        with self.assertRaises(asyncio.CancelledError):
            self.run_async(self.client.begin_battle(prompt='参考图动起来', images=[_png_data_url()]))
        self.assertEqual(self.saved, [CONTEXT])
        self.assertEqual(self.events, ['upload', 'save', 'create'])
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)
        self.client._video_fetch.assert_awaited_once()
        self.client._upload_reference.assert_awaited_once_with(self.page, _png_data_url())

    def test_failed_context_callback_prevents_create(self):
        self.prepare_battle()
        self.client._video_context_callback.side_effect = RuntimeError('offline save failed')
        with self.assertRaisesRegex(RuntimeError, 'offline save failed'):
            self.run_async(self.client.begin_battle(prompt='参考图动起来', images=[_png_data_url()]))
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)
        self.client._video_fetch.assert_not_awaited()
        self.client._upload_reference.assert_awaited_once()

    def test_max_one_reference_rejected_before_browser_or_upload(self):
        self.prepare_battle()
        with self.assertRaisesRegex(BridgeError, '只接受一张参考图'):
            self.run_async(self.client.begin_battle(
                prompt='参考图动起来', images=[_png_data_url(), _png_data_url()],
            ))
        self.assertEqual(self.opened, [])
        self.live_state.assert_not_awaited()
        self.page.mint_recaptcha.assert_not_awaited()
        self.client._upload_reference.assert_not_awaited()
        self.client._video_context_callback.assert_not_awaited()
        self.client._video_fetch.assert_not_awaited()

    def test_http_200_without_exact_session_is_not_completion(self):
        self.prepare_battle()
        self.client._video_fetch.side_effect = [
            {'status': 200, 'text': 'ad:{"finishReason":"stop"}\nbd:{"finishReason":"stop"}'},
            {'status': 200, 'text': _flight({**terminal_session(), 'id': USER_ID})},
        ]
        with self.assertRaises(BridgeError):
            self.run_async(self.client.begin_battle(prompt='参考图动起来'))
        self.assertEqual(self.client._video_fetch.await_count, 2)
        self.assertEqual([call.kwargs.get('method', 'GET')
                          for call in self.client._video_fetch.await_args_list], ['POST', 'GET'])

    def test_begin_readback_429_preserves_classification_without_retry(self):
        self.prepare_battle()
        self.client._video_fetch.side_effect = [
            {'status': 200, 'text': ''}, {'status': 429, 'text': 'secret Hidden Provider'},
        ]
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client.begin_battle(prompt='参考图动起来'))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertTrue(caught.exception.is_rate_limited)
        self.assert_sanitized(caught.exception)
        self.assertEqual(self.client._video_fetch.await_count, 2)

    def test_read_video_battle_uses_exact_flight_with_omitted_modality(self):
        self.prepare_battle()
        result = self.run_async(self.client.read_video_battle(CONTEXT))
        self.assertEqual(result, expected_result())
        self.client._video_fetch.assert_awaited_once_with(self.page, '/c/' + EVALUATION_ID)
        self.client._upload_reference.assert_not_awaited()
        self.client._video_context_callback.assert_not_awaited()
        self.page.mint_recaptcha.assert_not_awaited()

    def test_read_video_battle_429_preserves_classification(self):
        self.prepare_battle()
        self.client._video_fetch.side_effect = None
        self.client._video_fetch.return_value = {'status': 429, 'text': 'secret Hidden Provider'}
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client.read_video_battle(CONTEXT))
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.code, 'http_429')
        self.assertTrue(caught.exception.is_rate_limited)
        self.assert_sanitized(caught.exception)
        self.client._video_fetch.assert_awaited_once_with(self.page, '/c/' + EVALUATION_ID)

    def test_resume_404_still_requires_terminal_exact_session_readback(self):
        self.prepare_battle()
        pending = terminal_session()
        for message in pending['messages'][1:]:
            message['status'] = 'pending'
            message['metadata'] = {'videoRun': {'runId': 'offline-run-' + message['participantPosition']}}
        self.client._video_fetch.side_effect = [
            {'status': 200, 'text': ''},
            {'status': 200, 'text': _flight(pending)},
            {'status': 404, 'text': '', 'error': {'code': 'http_404'}},
            {'status': 200, 'text': _flight(terminal_session())},
        ]
        result = self.run_async(self.client.begin_battle(prompt='参考图动起来'))
        self.assertEqual(result, expected_result())
        calls = self.client._video_fetch.await_args_list
        self.assertEqual([call.args[1] for call in calls], [
            arena_direct.STREAM_CREATE_EVALUATION_PATH, '/c/' + EVALUATION_ID,
            '/nextjs-api/stream/resume-video-workflow/' + EVALUATION_ID, '/c/' + EVALUATION_ID,
        ])
        self.assertEqual([call.kwargs.get('method', 'GET') for call in calls],
                         ['POST', 'GET', 'GET', 'GET'])
        self.client._video_context_callback.assert_awaited_once_with(CONTEXT)

    def test_session_gate_is_not_bypassed(self):
        self.prepare_battle()
        self.live_state.return_value = {'has_arena_auth': True, 'has_logged_in': False}
        with self.assertRaises(BridgeError) as caught:
            self.run_async(self.client.begin_battle(prompt='参考图动起来', images=[_png_data_url()]))
        self.assertEqual(caught.exception.code, 'arena_login_required')
        self.live_state.assert_awaited_once_with(self.page)
        self.client._upload_reference.assert_not_awaited()
        self.page.mint_recaptcha.assert_not_awaited()
        self.client._video_context_callback.assert_not_awaited()
        self.client._video_fetch.assert_not_awaited()

    def test_original_browser_scope_mismatch_prevents_readback(self):
        self.prepare_battle()
        self.page.cache_key = 'cdp:another-offline-browser'
        with self.assertRaisesRegex(BridgeError, '不能换账号读回'):
            self.run_async(self.client.read_video_battle(CONTEXT))
        self.client._video_fetch.assert_not_awaited()
        self.live_state.assert_not_awaited()
