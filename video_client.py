"""Server-browser anonymous video Battle; never selects or resamples models.

The create envelope was observed in the existing official browser. Video session
readback uses the exact /c/<id> Flight state: the JSON evaluation endpoint was
not available in the observed deployment. No captured challenge token or client
interaction value is persisted or replayed.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .arena_direct import (
    ArenaDirectClient, STREAM_CREATE_EVALUATION_PATH, _live_state,
    _page_fetch_js, new_uuid7,
)
from .arena_battle import flight_values
from .bridge_client import BridgeError


def _video_http_error(status: int, message: str) -> BridgeError:
    # Classify only the observed HTTP status, never opaque body text or a
    # guessed challenge/provider reason. Controllers can recognize HTTP 429.
    return BridgeError(
        f'{message}（HTTP {status}），不会自动重发。',
        status_code=status or None,
        payload={'error': {'code': f'http_{status}' if status else 'video_transport_unconfirmed'}},
    )


def _session_from_page(body: str, session_id: str) -> dict[str, Any]:
    if not isinstance(body, str) or len(body) > 8 * 1024 * 1024:
        raise BridgeError('视频会话响应超过读取限制。')
    if len(body.encode('utf-8')) > 8 * 1024 * 1024:
        raise BridgeError('视频会话响应超过读取限制。')
    candidates = []
    try:
        values = flight_values(body)
    except (RecursionError, ValueError):
        raise BridgeError('视频会话数据无效。') from None
    stack = [(value, 0) for value in values]
    nodes = 0
    while stack:
        obj, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 100000:
            raise BridgeError('视频会话结构超过读取限制。')
        if isinstance(obj, dict):
            if obj.get('id') == session_id and isinstance(obj.get('messages'), list):
                candidates.append(obj)
            stack.extend((value, depth + 1) for value in obj.values())
        elif isinstance(obj, list):
            stack.extend((value, depth + 1) for value in obj)
    if not candidates or any(row != candidates[0] for row in candidates[1:]):
        raise BridgeError('无法核对原始视频 Battle 状态，未重新生成。')
    return candidates[0]


class ArenaVideoClient(ArenaDirectClient):
    """Separate video transport; inherited image methods are left unchanged."""

    async def _video_fetch(self, page, path, *, method='GET', body=None, timeout=30.0):
        # Bypass ArenaPage.fetch's deployment replay for a protected create.
        # A timeout/cancel may leave a real job running; never repeat its POST.
        expression = _page_fetch_js(path, method=method, body=body,
                                    headers={'Content-Type': 'application/json'} if body else {})
        # Bound the page-side fetch too; a CDP timeout alone cannot cancel it.
        expression = expression.replace(
            "(async () => { try {", "(async () => { let timer; try {",
        ).replace(
            "try { const r = await fetch(",
            "try { const c = new AbortController(); "
            f"timer = setTimeout(() => c.abort(), {int(timeout * 1000)}); "
            "const r = await fetch(",
        ).replace(
            ", credentials: 'include'", ", signal: c.signal, credentials: 'include'",
        ).replace(
            "catch (e) { return {status: 0, headers: {}, text: 'FETCH_ERROR:' + String(e)}; }",
            "catch (e) { return {status: 0, headers: {}, text: '', error: {code: "
            "e.name === 'AbortError' ? 'video_fetch_timeout' : 'video_transport_unconfirmed'}}; } "
            "finally { clearTimeout(timer); }",
        )
        try:
            result = await page.evaluate(expression, timeout=timeout + 5.0)
        except Exception:
            raise BridgeError(
                '视频接口响应未确认，不会自动重复生成。',
                payload={'error': {'code': 'video_transport_unconfirmed'}},
            ) from None
        if (
            not isinstance(result, dict)
            or type(result.get('status')) is not int
            or result['status'] != 0 and not 100 <= result['status'] <= 599
        ):
            raise BridgeError(
                '视频接口响应无效，不会自动重复生成。',
                payload={'error': {'code': 'video_response_invalid'}},
            )
        status = result['status']
        if not status:
            details = result.get('error')
            code = 'video_fetch_timeout' if (
                isinstance(details, dict) and details.get('code') == 'video_fetch_timeout'
            ) else 'video_transport_unconfirmed'
            raise BridgeError(
                '视频接口响应未确认，不会自动重复生成。', payload={'error': {'code': code}},
            )
        if status != 200:
            # Preserve status for create/readback, and permit the existing
            # resume-404 -> exact readback flow. No failed body/headers escape.
            return {'status': status, 'headers': {}, 'text': '',
                    'error': {'code': f'http_{status}'}}
        if not isinstance(result.get('text'), str):
            raise BridgeError(
                '视频接口响应无效，不会自动重复生成。',
                status_code=status, payload={'error': {'code': 'video_response_invalid'}},
            )
        return result

    async def begin_battle(self, *, prompt: str, images: list[str] | None = None) -> dict:
        from .arena_video import parse_video_stream, validate_video_session

        prompt = str(prompt or '').strip()
        if not prompt:
            raise BridgeError('请输入视频画面描述。')
        if len(prompt) > 4000:
            raise BridgeError('视频描述过长，请缩短到 4000 字以内。')
        if len(images or []) > 1:
            raise BridgeError('视频 Battle 当前只接受一张参考图。')
        async with self._page(timeout=self.timeout) as page:
            self._require_session(await _live_state(page), self._link())
            attachments = [await self._upload_reference(page, image) for image in images or []]
            token = await page.mint_recaptcha('chat_submit')
            context = {
                'evaluationSessionId': new_uuid7(), 'messageAId': new_uuid7(),
                'messageBId': new_uuid7(), 'browser_scope': str(page.cache_key),
                'modality': 'video',
            }
            payload = {
                'id': context['evaluationSessionId'], 'mode': 'battle',
                'userMessageId': new_uuid7(), 'modelAMessageId': context['messageAId'],
                'modelBMessageId': context['messageBId'],
                'userMessage': {'content': prompt, 'experimental_attachments': attachments, 'metadata': {}},
                'modality': 'video', 'recaptchaV3Token': token or '',
            }
            # Client interaction cx is not fabricated or copied from a past UI
            # request. Acceptance of this programmatic create requires live QA.
            callback = getattr(self, '_video_context_callback', None)
            if callback is not None:
                await callback(context)
            response = await self._video_fetch(
                page, STREAM_CREATE_EVALUATION_PATH, method='POST',
                body=json.dumps(payload, ensure_ascii=False), timeout=self.timeout,
            )
            status = int(response.get('status') or 0)
            if status != 200:
                raise _video_http_error(status, '视频 Battle 创建未确认')
            # Stream artifacts are hints only; exact persisted session proves
            # ownership, lane binding, and terminal status before delivery.
            parse_video_stream(str(response.get('text') or ''))
            deadline = time.monotonic() + min(180.0, self.timeout)
            resumed = False
            while True:
                read = await self._video_fetch(
                    page, '/c/' + context['evaluationSessionId'], timeout=min(30.0, self.timeout),
                )
                status = int(read.get('status') or 0)
                if status != 200:
                    raise _video_http_error(status, '无法读取原始视频会话')
                session = _session_from_page(str(read.get('text') or ''), context['evaluationSessionId'])
                validated = validate_video_session(session, context)
                sides = validated['sides']
                if all(side.get('completed') or side.get('error') for side in sides.values()):
                    return {**validated, 'sides': {
                        key: {**side, 'finished': side['completed'], 'error': bool(side['error']), 'videos': [
                            {**video, 'contentType': video['mimeType']} for video in side['videos']
                        ]} for key, side in sides.items()
                    }}
                if not resumed and any(side.get('run_id') for side in sides.values()):
                    resumed = True
                    # This GET continues the SAME workflow only. Even a 404
                    # 'No active video workflow' requires exact session readback.
                    await self._video_fetch(
                        page, '/nextjs-api/stream/resume-video-workflow/' + context['evaluationSessionId'],
                        timeout=self.timeout,
                    )
                    continue
                if time.monotonic() >= deadline:
                    raise BridgeError('视频仍在生成，当前等待已结束；未自动重发。')
                await asyncio.sleep(5.0)

    async def read_video_battle(self, context: dict) -> dict:
        """Read one already-created session, never start another generation."""
        from .arena_video import validate_video_session
        from .arena_battle import battle_context
        checked = battle_context(context)
        async with self._battle_page(checked) as page:
            if str(page.cache_key) != checked['browser_scope']:
                raise BridgeError('原始视频浏览器不可用，不能换账号读回。')
            self._require_session(await _live_state(page), self._link())
            response = await self._video_fetch(page, '/c/' + checked['evaluationSessionId'])
            status = int(response.get('status') or 0)
            if status != 200:
                raise _video_http_error(status, '读取原始视频会话失败')
            session = _session_from_page(str(response.get('text') or ''), checked['evaluationSessionId'])
            validated = validate_video_session(session, {**checked, 'modality': 'video'})
            return {**validated, 'sides': {
                key: {**side, 'finished': side['completed'], 'error': bool(side['error']), 'videos': [
                    {**video, 'contentType': video['mimeType']} for video in side['videos']
                ]} for key, side in validated['sides'].items()
            }}
