"""Anonymous image Battle protocol, shared by CDP and userscript transports.

Request contract observed 2026-11-06 in the existing server browser. No guessed
REST endpoint, model-ID sampling, synthetic preference votes or CAPTCHA bypass.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from .bridge_client import BridgeError

_FFLIGHT = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')


def flight_values(body: str) -> list[Any]:
    """Decode both streamed server actions and HTML's escaped Flight chunks."""
    chunks = []
    for match in _FFLIGHT.finditer(body or ""):
        try:
            chunks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    text = "".join(chunks) if chunks else str(body or "")
    result = []
    for line in text.splitlines():
        if ":" not in line:
            continue
        try:
            result.append(json.loads(line.partition(":")[2]))
        except json.JSONDecodeError:
            continue
    return result


def _objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


def action_result(body: str) -> dict[str, Any]:
    for value in flight_values(body):
        for obj in _objects(value):
            if isinstance(obj.get("success"), bool):
                return obj
    raise BridgeError("官网没有返回可识别的投票确认，未记录成功")


def parse_battle_stream(body: str) -> dict[str, dict[str, Any]]:
    # Reuse the proven single-lane image and error parser without merging B into A.
    from .arena_direct import parse_stream

    lanes = {"a": [], "b": []}
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if len(line) > 2 and line[0] in lanes and line[1:3] in {"0:", "2:", "3:", "d:"}:
            lanes[line[0]].append("a" + line[1:])
    return {side: parse_stream("\n".join(lines)) for side, lines in lanes.items()}


def battle_context(context: dict) -> dict[str, str]:
    if not isinstance(context, dict):
        raise BridgeError("对战会话无效")
    result = {}
    for field in ("evaluationSessionId", "messageAId", "messageBId"):
        try:
            result[field] = str(uuid.UUID(str(context[field])))
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise BridgeError("对战会话编号无效") from exc
    scope = str(context.get("browser_scope") or "")
    if not scope:
        raise BridgeError("对战会话缺少原始浏览器标识，请重新开始")
    result["browser_scope"] = scope
    return result


def revealed_names(data: dict, state_body: str = "") -> dict[str, str]:
    """Match actual message/model IDs. Never use catalog order or selected defaults."""
    models = {
        str(m.get("id")): m
        for m in data.get("revealedModels", [])
        if isinstance(m, dict)
    }
    for value in flight_values(state_body):
        for obj in _objects(value):
            for model in (
                obj.get("revealedModels", [])
                if isinstance(obj.get("revealedModels"), list)
                else []
            ):
                if isinstance(model, dict):
                    models[str(model.get("id"))] = model
    names = {}
    for side in ("a", "b"):
        row = models.get(str(data.get("model" + side.upper() + "Id") or ""))
        if row:
            name = str(row.get("displayName") or row.get("publicName") or "").strip()
            if name:
                names[side] = name
    return names


def verified_session(body: str, context: dict) -> dict:
    """Fail closed: a 200/login page is not evidence that feedback is absent."""
    candidates = []
    for value in flight_values(body):
        for obj in _objects(value):
            if obj.get("id") == context["evaluationSessionId"] and "pairwiseFeedbacks" in obj:
                candidates.append(obj)
    if not candidates:
        raise BridgeError("无法确认原始对战状态，未提交评价；请稍后重试")
    session = candidates[0]
    if any(row != session for row in candidates[1:]):
        raise BridgeError("原始对战状态不一致，未提交评价")
    messages = session.get("messages")
    feedbacks = session.get("pairwiseFeedbacks")
    if not isinstance(messages, list) or not isinstance(feedbacks, list):
        raise BridgeError("原始对战状态不完整，未提交评价")
    for side in ("a", "b"):
        matches = [m for m in messages if isinstance(m, dict) and m.get("id") == context["message" + side.upper() + "Id"]]
        if len(matches) != 1 or any((
            matches[0].get("evaluationSessionId") != context["evaluationSessionId"],
            matches[0].get("role") != "assistant",
            matches[0].get("participantPosition") != side,
        )):
            raise BridgeError("无法核对原始 A/B 消息，未提交评价")
    if any(not isinstance(f, dict) or not all(f.get(k) for k in ("messageAId", "messageBId", "value")) for f in feedbacks):
        raise BridgeError("原始评价状态无法解析，未提交评价")
    return session


def _anonymous_upstream_error(status: int) -> BridgeError:
    # Raw upstream errors may identify the still-hidden model or provider.
    return BridgeError(
        f"匿名 Battle 上游请求失败（HTTP {status}），未揭晓模型；请稍后重试",
        status_code=status or None,
    )


class BattleProtocolMixin:
    async def begin_battle(
        self, *, prompt: str, images: list[str] | None = None
    ) -> dict:
        from .arena_direct import STREAM_CREATE_EVALUATION_PATH, _live_state, new_uuid7

        prompt = str(prompt or "").strip()
        images = [str(x).strip() for x in images or [] if str(x).strip()]
        if not prompt and not images:
            raise BridgeError("提示词和参考图不能同时为空")
        # One protected request only. No retry of ambiguous timeouts, no silent direct fallback.
        async with self._page(timeout=self.timeout) as page:
            state = await _live_state(page)
            self._require_session(state, self._link())
            attachments = [
                await self._upload_reference(page, value) for value in images
            ]
            token = await page.mint_recaptcha("chat_submit")
            context = {
                "evaluationSessionId": new_uuid7(),
                "messageAId": new_uuid7(),
                "messageBId": new_uuid7(),
                "browser_scope": str(page.cache_key),
            }
            payload = {
                "id": context["evaluationSessionId"],
                "mode": "battle",
                "userMessageId": new_uuid7(),
                "modelAMessageId": context["messageAId"],
                "modelBMessageId": context["messageBId"],
                "userMessage": {
                    "content": prompt or "请根据参考图生成图片",
                    "experimental_attachments": attachments,
                    "metadata": {},
                },
                "modality": "image",
                "recaptchaV3Token": token or "",
            }
            response = await page.fetch(
                STREAM_CREATE_EVALUATION_PATH,
                method="POST",
                body=json.dumps(payload, ensure_ascii=False),
                timeout=self.timeout,
            )
        body = str(response.get("text") or "")
        sides = parse_battle_stream(body)
        status = int(response.get("status") or 0)
        if status != 200:
            raise _anonymous_upstream_error(status)
        if not any(
            lane["images"] or lane["text"] or lane["error"] for lane in sides.values()
        ):
            raise BridgeError("匿名 Battle 返回空流，未生成可用结果")
        return {
            "evaluation_id": context["evaluationSessionId"],
            "sides": sides,
            "context": context,
        }

    def _battle_page(self, context: dict):
        # The userscript client overrides this to select the originating browser.
        return self._page(timeout=self.timeout)

    async def vote_battle(self, context: dict, *, winner: str) -> dict[str, str]:
        if winner not in {"a", "b"}:
            raise BridgeError("只能明确选择 A 或 B，未提交投票")
        return await self._battle_feedback(context, "model_" + winner)

    async def reveal_battle(self, context: dict) -> dict[str, str]:
        # Readback only. The command layer explicitly chooses and persists any
        # authorized random preference before calling vote_battle; never SKIP.
        return await self._battle_feedback(context, None)

    async def _battle_feedback(self, context: dict, value: str | None) -> dict[str, str]:
        from .arena_direct import _live_state, action_is_stale, _StaleActionError, _invalidate_deployment_caches

        context = battle_context(context)
        async with self._battle_page(context) as page:
            if str(page.cache_key) != context["browser_scope"]:
                raise BridgeError("原始 Battle 浏览器不可用，不能换账号提交投票")
            state = await _live_state(page)
            self._require_session(state, self._link())
            path = "/c/" + context["evaluationSessionId"]
            # Read only this session. Never infer names from pre-vote default selections.
            response = await page.fetch(path, method="GET", timeout=self.timeout)
            if int(response.get("status") or 0) != 200:
                raise BridgeError("无法读取原始 Battle 会话，未提交投票")
            state_body = str(response.get("text") or "")
            session = verified_session(state_body, context)
            feedbacks = session["pairwiseFeedbacks"]
            for feedback in feedbacks:
                if (
                    not isinstance(feedback, dict)
                    or feedback.get("messageAId") != context["messageAId"]
                    or feedback.get("messageBId") != context["messageBId"]
                ):
                    continue
                if value is not None and feedback.get("value") != value:
                    raise BridgeError("该 Battle 已评价或揭晓，不能更改盲测结果")
                names = revealed_names(feedback, state_body)
                if len(names) == 2:
                    return names
                if value is not None:
                    return {"success": True, "reveal_pending": True}
                raise BridgeError("官网已记录评价，但模型名未返回；不会重复提交")
            if value is None:
                raise BridgeError("官网尚未记录评价，不能直接揭晓；请先投票或使用已开启的随机投票揭晓")
            # Prefer this exact session's current build, not a stale open tab.
            action = ""
            sources = re.findall(r'<script[^>]+src=["\']([^"\']+)', state_body)
            # Route-specific modules also appear in Flight imports/preload links,
            # not only script tags. Read only same-origin immutable JS paths.
            sources += re.findall(r'/_next/static/[^"\s\\<>]*?\.js', state_body)
            from .arena_direct import _NEXT_ACTION_RE

            scripts = [src for src in dict.fromkeys(sources) if src.startswith("/_next/")]
            # Current Arena SSR emits over 100 chunks; a 60-chunk limit omits
            # the feedback module. Bound the complete scan by elapsed time too.
            async def discover_action():
                for src in scripts[:160]:
                    bundle = await page.fetch(
                        src, method="GET", timeout=min(self.timeout, 10.0)
                    )
                    if int(bundle.get("status") or 0) != 200:
                        continue
                    for ident, name in re.findall(_NEXT_ACTION_RE, str(bundle.get("text") or "")):
                        if name == "createPairwiseFeedback":
                            return ident
                return ""
            if scripts:
                try:
                    action = await asyncio.wait_for(discover_action(), timeout=min(self.timeout, 60.0))
                except asyncio.TimeoutError as exc:
                    raise BridgeError("读取官网投票接口超时，未提交评价；请稍后重试") from exc
            if not scripts:
                # The session state was verified; some renderers omit script tags.
                action = (await page.next_actions()).get("createPairwiseFeedback") or ""
            if not action:
                raise BridgeError("官网投票 Action 未找到，请刷新接入的 Arena 标签页")
            token = await page.mint_recaptcha("pairwise_feedback")
            body = [
                {
                    key: context[key]
                    for key in ("evaluationSessionId", "messageAId", "messageBId")
                }
                | {"value": value, "recaptchaV3Token": token or None}
            ]
            try:
                response = await page.fetch(
                    path,
                    method="POST",
                    headers={
                        "Accept": "text/x-component",
                        "Content-Type": "text/plain;charset=UTF-8",
                        "Next-Action": action,
                    },
                    body=json.dumps(body),
                    timeout=self.timeout,
                )
            except _StaleActionError as exc:
                # Explicit 409 rejected this operation. Refresh only this page;
                # caller retries with the persisted side and full state recheck.
                await page.reload()
                await _invalidate_deployment_caches(page)
                raise BridgeError("官网要求更新页面，已刷新原浏览器；投票未确认，请重试原操作") from exc
            status = int(response.get("status") or 0)
            text = str(response.get("text") or "")
            if action_is_stale(status, text):
                raise BridgeError(
                    "官网刚更新了投票接口，未确认成功；请刷新 Arena 后再试"
                )
            if status != 200:
                raise _anonymous_upstream_error(status)
            result = action_result(text)
            if not result["success"]:
                raise BridgeError("官网拒绝了投票，未记录成功；请稍后再试")
            data = result.get("data")
            data = data if isinstance(data, dict) else {}
            names = revealed_names(data)
            if len(names) != 2:
                try:
                    response = await page.fetch(path, method="GET", timeout=self.timeout)
                    if int(response.get("status") or 0) == 200:
                        refreshed = str(response.get("text") or "")
                        session = verified_session(refreshed, context)
                        names = revealed_names(data, refreshed)
                        if len(names) != 2:
                            for feedback in session["pairwiseFeedbacks"]:
                                if feedback.get("messageAId") == context["messageAId"] and feedback.get("messageBId") == context["messageBId"]:
                                    names = revealed_names(feedback, refreshed)
                except Exception:  # Confirmation must survive readback/network failure.
                    names = {}
            if len(names) != 2:
                return {"success": True, "reveal_pending": True}
            return names
