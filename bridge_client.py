"""Small, dependency-light client for the LMArenaBridge OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import base64
import binascii
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes, urlparse

import httpx

DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_OUTPUT_IMAGE_BYTES = 64 * 1024 * 1024

# Upstream limiting is reported either as HTTP 429 or as an OpenAI-style error
# object carried inside an HTTP 200 response, so both codes and message text
# have to be inspected.
RATE_LIMIT_CODES = frozenset(
    {
        "rate_limit_exceeded",
        "rate_limited",
        "rate_limit",
        "too_many_requests",
        "http_429",
        "429",
    }
)
RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "请求过于频繁",
    "限流",
)
DEFAULT_RATE_LIMIT_RETRIES = 2
DEFAULT_RATE_LIMIT_MAX_WAIT = 30.0

# Bridge failures that are explicitly *not* an expired browser session.  These
# codes must win over the challenge-term heuristics in
# ``requires_interactive_auth``; otherwise an internal bridge error whose text
# mentions reCAPTCHA would tell the operator to re-verify a healthy login.
NON_INTERACTIVE_AUTH_CODES = frozenset(
    {
        "recaptcha_mint_failed",
    }
)

_IMAGE_URL_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".avif",
    ".svg",
    ".heic",
    ".heif",
)


class BridgeError(RuntimeError):
    """An HTTP or protocol error returned by LMArenaBridge."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.code = _payload_code(payload)
        self.retry_after = retry_after

    @property
    def is_rate_limited(self) -> bool:
        """Whether the error is an upstream/bridge rate limit worth retrying."""
        if self.status_code == 429:
            return True
        if self.code in RATE_LIMIT_CODES:
            return True
        text = str(self).casefold()
        return any(marker in text for marker in RATE_LIMIT_MARKERS)

    @property
    def requires_interactive_auth(self) -> bool:
        """Whether the error is consistent with an Arena/Cloudflare challenge."""
        if self.code in NON_INTERACTIVE_AUTH_CODES:
            return False
        if self.code in {
            "arena_verification_required",
            "interactive_auth_required",
            "cloudflare_challenge",
            "arena_auth_expired",
            "arena_auth_required",
            "arena_login_required",
            "authentication_error",
        }:
            return True
        text = str(self).casefold()
        challenge_terms = (
            "cloudflare",
            "turnstile",
            "recaptcha",
            "re-captcha",
            "cf clearance",
            "server may be blocked",
            "browser transport",
            "arena auth",
        )
        if any(term in text for term in challenge_terms) and self.status_code in {
            403,
            500,
            502,
            503,
        }:
            return True

        # The bridge returns OpenAI-compatible error objects for upstream
        # authentication failures.  Those responses normally have HTTP 200
        # at the bridge boundary, so ``status_code`` is None and the upstream
        # code is carried as ``http_401`` (or as numeric ``401`` for SSE
        # errors).  Distinguish this from an invalid *Bridge API key* by
        # requiring an Arena-auth-specific marker in the message.
        arena_auth_terms = (
            "lmarena auth token",
            "arena-auth",
            "arena auth",
            "auth token has expired",
            "auth token is invalid",
            "login required",
            "login_gate",
            "登录账号",
        )
        if any(term in text for term in arena_auth_terms):
            return True
        return self.code in {"http_401", "401"} and "unauthorized" in text

    @property
    def interactive_auth_url(self) -> str:
        return _payload_string(self.payload, "browser_url") or _payload_string(
            self.payload,
            "verification_url",
        )


def normalize_api_base(value: str) -> str:
    """Normalize a configured bridge URL to the ``/api/v1`` root."""
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("bridge_url 不能为空")

    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("bridge_url 必须是没有查询参数的 http(s) URL")

    if parsed.path.rstrip("/").endswith("/api/v1"):
        return normalized
    return f"{normalized}/api/v1"


def _error_object(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    error = payload.get("error")
    if isinstance(error, dict):
        return error
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return detail
    return {}


def _payload_code(payload: Any) -> str:
    value = _error_object(payload).get("code")
    return str(value or "").strip().casefold()


def _payload_string(payload: Any, key: str) -> str:
    value = _error_object(payload).get(key)
    return str(value or "").strip()


def _error_message(payload: Any, fallback: str) -> str:
    error = _error_object(payload)
    if error:
        message = error.get("message") or error.get("detail")
        if message:
            return str(message)
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return fallback


def _parse_retry_after(value: Any) -> float | None:
    """Read a ``Retry-After`` style hint expressed in seconds."""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def _retry_after_from(response: Any, payload: Any) -> float | None:
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
        try:
            hint = _parse_retry_after(headers.get(key))
        except AttributeError:
            hint = None
        if hint:
            return hint
    error = _error_object(payload)
    for key in ("retry_after", "retryAfter", "retry_after_seconds"):
        hint = _parse_retry_after(error.get(key))
        if hint:
            return hint
    if isinstance(payload, dict):
        return _parse_retry_after(payload.get("retry_after"))
    return None


def looks_like_image_url(value: str) -> bool:
    """Whether a bare URL is shaped like a direct image reference."""
    text = str(value or "").strip()
    if not text:
        return False
    lowered = text.casefold()
    if lowered.startswith("data:image/"):
        return True
    path = urlparse(lowered).path
    if path.endswith(_IMAGE_URL_SUFFIXES):
        return True
    # Signed CDN links often keep the extension in a query parameter or use a
    # dedicated image path segment instead of a suffix.
    if any(f"{suffix}?" in lowered or f"{suffix}&" in lowered for suffix in _IMAGE_URL_SUFFIXES):
        return True
    return any(
        marker in lowered
        for marker in ("/image", "image/", "/images/", "img", "attachment", "blob")
    )


def _sniff_mime(raw: bytes) -> str | None:
    """Detect the most common image formats without optional image libraries."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"

    # SVG responses often begin with an XML declaration or a UTF-8 BOM.
    prefix = raw[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if prefix.startswith(b"<svg") or (prefix.startswith(b"<?xml") and b"<svg" in prefix):
        return "image/svg+xml"
    return None


def guess_image_mime(source: str | None = None, raw: bytes = b"") -> str:
    """Infer an image MIME type from bytes or a path/URL."""
    detected = _sniff_mime(raw)
    if detected:
        return detected

    text = str(source or "")
    suffix = Path(urlparse(text).path).suffix.lower()
    guessed = mimetypes.types_map.get(suffix)
    if guessed and guessed.startswith("image/"):
        return guessed
    guessed, _ = mimetypes.guess_type(text, strict=False)
    if guessed and guessed.startswith("image/"):
        return guessed
    return "image/png"


def _decode_base64_payload(value: str) -> bytes:
    compact = re.sub(r"\s+", "", value)
    if not compact:
        return b""

    # A few adapters omit padding. Restoring it is safe after validating the
    # alphabet and makes the helper work with both common base64 conventions.
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        # URL-safe base64 is occasionally used by message adapters.
        try:
            return base64.urlsafe_b64decode(compact)
        except (ValueError, binascii.Error) as exc:
            raise BridgeError("图片 Base64 数据无效") from exc


def _decode_data_uri(value: str) -> tuple[bytes, str | None]:
    if "," not in value:
        raise BridgeError("图片 Data URI 格式无效")

    header, encoded = value.split(",", 1)
    if not header.lower().startswith("data:"):
        raise BridgeError("图片 Data URI 格式无效")

    metadata = header[5:].split(";")
    mime = metadata[0].strip().lower() or None
    is_base64 = any(part.strip().lower() == "base64" for part in metadata[1:])
    if is_base64:
        return _decode_base64_payload(encoded), mime
    return unquote_to_bytes(encoded), mime


def _select_image_mime(
    *,
    source: str | None,
    raw: bytes,
    explicit_mime: str | None,
) -> str:
    explicit = str(explicit_mime or "").split(";", 1)[0].strip().lower()
    detected = _sniff_mime(raw)

    if explicit and not explicit.startswith("image/"):
        if detected:
            return detected
        raise BridgeError(f"不支持的图片类型：{explicit}")
    if explicit:
        return explicit
    if detected:
        return detected
    return guess_image_mime(source, raw)


def decode_image_value(
    value: str | bytes,
    *,
    source: str | None = None,
    mime_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> tuple[bytes, str]:
    """Decode an image URL/data URI/base64 value and return ``(bytes, mime)``."""
    if isinstance(value, bytes):
        raw = value
        embedded_mime = None
    else:
        text = str(value or "").strip()
        if not text:
            raise BridgeError("图片内容为空")
        if text.lower().startswith("data:"):
            raw, embedded_mime = _decode_data_uri(text)
        else:
            if text.lower().startswith("base64://"):
                text = text[9:]
            raw = _decode_base64_payload(text)
            embedded_mime = None

    if not raw:
        raise BridgeError("图片内容为空")
    if len(raw) > max_bytes:
        raise BridgeError(f"图片超过大小限制（{max_bytes // 1024 // 1024} MB）")

    mime = _select_image_mime(
        source=source,
        raw=raw,
        explicit_mime=mime_type or embedded_mime,
    )
    if not mime.startswith("image/"):
        raise BridgeError(f"不支持的图片类型：{mime}")
    return raw, mime


def data_uri_from_base64(
    value: str | bytes,
    *,
    source: str | None = None,
    mime_type: str | None = None,
    max_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> str:
    """Build a validated image data URI from raw/base64 input."""
    raw, mime = decode_image_value(
        value,
        source=source,
        mime_type=mime_type,
        max_bytes=max_bytes,
    )
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text is None:
                    text = item.get("content")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(parts)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return ""


def response_text(payload: Any) -> str:
    """Extract assistant text from an OpenAI-compatible response."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else first
    return _content_text(message.get("content"))


_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?:<)?((?:https?://[^)\s>]+)|(?:data:image/[^)\s>]+))(?:>)?\s*\)",
    re.IGNORECASE,
)
_DATA_URI_RE = re.compile(
    r"data:image/[a-z0-9.+-]+(?:;[a-z0-9=._-]+)*,[a-z0-9%+/=_-]+",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _append_url(result: list[str], value: Any) -> None:
    if not isinstance(value, str):
        return
    candidate = value.strip().rstrip(".,;:!?")
    lower = candidate.lower()
    if lower.startswith(("http://", "https://", "data:image/")) and candidate not in result:
        result.append(candidate)


def _append_base64(
    result: list[str],
    value: Any,
    *,
    mime_type: str | None = None,
) -> None:
    if not isinstance(value, (str, bytes)):
        return
    try:
        uri = data_uri_from_base64(value, mime_type=mime_type)
        if uri not in result:
            result.append(uri)
    except BridgeError:
        return


def _append_image_candidate(
    result: list[str],
    value: Any,
    *,
    mime_type: str | None = None,
    raw_base64: bool = False,
) -> None:
    """Append one image-shaped response value, accepting common API variants."""
    if isinstance(value, (list, tuple)):
        for item in value:
            _append_image_candidate(result, item, mime_type=mime_type)
        return

    if isinstance(value, dict):
        media_type = value.get("mime_type") or value.get("media_type") or mime_type
        if value.get("b64_json") is not None:
            _append_base64(result, value.get("b64_json"), mime_type=media_type)
        if value.get("base64") is not None:
            _append_base64(result, value.get("base64"), mime_type=media_type)

        # OpenAI, Anthropic, and several gateway formats use one of these
        # nested fields for an image URL or source object.
        for key in ("image_url", "url", "image", "source"):
            if key in value:
                nested = value[key]
                if isinstance(nested, dict):
                    nested_media_type = (
                        nested.get("media_type") or nested.get("mime_type") or media_type
                    )
                    if nested.get("data") is not None:
                        _append_base64(
                            result,
                            nested.get("data"),
                            mime_type=nested_media_type,
                        )
                    _append_image_candidate(
                        result,
                        nested,
                        mime_type=nested_media_type,
                    )
                else:
                    _append_image_candidate(
                        result,
                        nested,
                        mime_type=media_type,
                    )
        return

    if raw_base64:
        _append_base64(result, value, mime_type=mime_type)
    else:
        _append_url(result, value)


def _extract_images_from_text(
    text: str,
    result: list[str],
    weak: list[str] | None = None,
) -> None:
    for match in _MARKDOWN_IMAGE_RE.findall(text):
        _append_url(result, match)
    for match in _DATA_URI_RE.findall(text):
        _append_url(result, match)
    for match in _URL_RE.findall(text):
        if looks_like_image_url(match):
            _append_url(result, match)
            continue
        # A bare link inside prose is usually a citation, not the generated
        # image.  Keep it as a last-resort candidate only.
        candidate = str(match).strip().rstrip(".,;:!?")
        if weak is not None and candidate not in result:
            _append_url(weak, candidate)


def _extract_images_from_content(
    content: Any,
    result: list[str],
    weak: list[str] | None = None,
) -> None:
    if isinstance(content, str):
        _extract_images_from_text(content, result, weak)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                _extract_images_from_text(item, result, weak)
            elif isinstance(item, dict):
                _append_image_candidate(result, item)
                text = item.get("text")
                if isinstance(text, str):
                    _extract_images_from_text(text, result, weak)
    elif isinstance(content, dict):
        _append_image_candidate(result, content)
        text = content.get("text")
        if isinstance(text, str):
            _extract_images_from_text(text, result, weak)


def image_urls(payload: Any) -> list[str]:
    """Extract generated image URLs/data URIs from common response shapes.

    Structured fields, markdown images and data URIs are treated as reliable.
    Bare links found in prose are only returned when nothing better exists, so
    a text answer containing a citation is not mistaken for an image.
    """
    result: list[str] = []
    weak: list[str] = []
    if not isinstance(payload, dict):
        return result

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") if isinstance(choice.get("message"), dict) else choice
            if isinstance(message, dict):
                _extract_images_from_content(message.get("content"), result, weak)
                for key in ("images", "image", "data"):
                    if key in message:
                        _append_image_candidate(result, message[key])
            for key in ("images", "image", "data"):
                if key in choice:
                    _append_image_candidate(result, choice[key])

    # Image-generation APIs commonly return a top-level data/images/output
    # array rather than a chat message.
    for key in ("data", "images", "output"):
        if key in payload:
            value = payload[key]
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("b64_json") is not None:
                        _append_base64(
                            result,
                            item.get("b64_json"),
                            mime_type=item.get("mime_type") or item.get("media_type"),
                        )
                    else:
                        _append_image_candidate(result, item)
            else:
                _append_image_candidate(result, value)

    response = payload.get("response")
    if isinstance(response, str):
        _extract_images_from_text(response, result, weak)
    if result:
        return result
    return weak


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def model_is_image_capable(model: dict[str, Any]) -> tuple[bool, bool]:
    """Return ``(output_image, input_image)`` from bridge model metadata."""
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}

    output = capabilities.get("outputCapabilities")
    if not isinstance(output, dict):
        output = capabilities.get("output_capabilities")
    if not isinstance(output, dict):
        output = {}

    inputs = capabilities.get("inputCapabilities")
    if not isinstance(inputs, dict):
        inputs = capabilities.get("input_capabilities")
    if not isinstance(inputs, dict):
        inputs = {}

    output_image = _as_bool(model.get("output_image")) or _as_bool(output.get("image"))
    input_image = _as_bool(model.get("input_image")) or _as_bool(inputs.get("image"))
    return output_image, input_image


class ArenaBridgeClient:
    """Async client used by the AstrBot plugin."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 300,
        *,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_max_wait: float = DEFAULT_RATE_LIMIT_MAX_WAIT,
    ) -> None:
        self.base_url = normalize_api_base(base_url)
        self.api_key = str(api_key or "").strip()
        self.timeout = max(10.0, float(timeout))
        self.rate_limit_retries = max(0, int(rate_limit_retries))
        self.rate_limit_max_wait = max(0.0, float(rate_limit_max_wait))

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _rate_limit_delay(self, exc: BridgeError, attempt: int) -> float:
        """Honour ``Retry-After`` when present, else back off exponentially."""
        hint = exc.retry_after or 0.0
        if hint <= 0:
            hint = min(self.rate_limit_max_wait, 6.0 * (2**attempt))
        return max(1.0, min(self.rate_limit_max_wait, hint))

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        attempts = self.rate_limit_retries + 1
        last_error: BridgeError | None = None
        for attempt in range(attempts):
            try:
                return await self._request_once(method, path, **kwargs)
            except BridgeError as exc:
                if not exc.is_rate_limited or attempt == attempts - 1:
                    raise
                last_error = exc
                await asyncio.sleep(self._rate_limit_delay(exc, attempt))
        if last_error is not None:  # pragma: no cover - defensive
            raise last_error
        raise BridgeError("Bridge 请求未产生结果")  # pragma: no cover - defensive

    async def _request_once(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                headers=self._headers(),
            ) as client:
                response = await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise BridgeError(f"连接 Bridge 失败：{exc}") from exc

        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}

        if response.status_code >= 400:
            raise BridgeError(
                _error_message(payload, f"Bridge 返回 HTTP {response.status_code}"),
                status_code=response.status_code,
                payload=payload,
                retry_after=_retry_after_from(response, payload),
            )
        if isinstance(payload, dict) and payload.get("error") and not payload.get("choices"):
            raise BridgeError(
                _error_message(payload, "Bridge 返回错误"),
                payload=payload,
                retry_after=_retry_after_from(response, payload),
            )
        return payload

    async def list_models(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "models")
        data = payload.get("data") if isinstance(payload, dict) else None
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def health(self) -> dict[str, Any]:
        payload = await self._request("GET", "health")
        return payload if isinstance(payload, dict) else {}

    async def model_health(self) -> dict[str, Any]:
        payload = await self._request("GET", "model-health")
        return payload if isinstance(payload, dict) else {}

    async def start_interactive_auth(self) -> dict[str, Any]:
        payload = await self._request("POST", "interactive-auth/start")
        return payload if isinstance(payload, dict) else {}

    async def interactive_auth_status(self, session_id: str) -> dict[str, Any]:
        clean_id = str(session_id or "").strip()
        if not clean_id:
            raise BridgeError("验证会话编号为空")
        payload = await self._request(
            "GET",
            f"interactive-auth/status/{clean_id}",
        )
        return payload if isinstance(payload, dict) else {}

    async def latest_interactive_auth_status(self) -> dict[str, Any]:
        payload = await self._request("GET", "interactive-auth/status")
        return payload if isinstance(payload, dict) else {}

    async def complete(
        self,
        *,
        model: str,
        prompt: str,
        images: list[str] | None = None,
    ) -> dict[str, Any]:
        clean_model = str(model or "").strip()
        clean_prompt = str(prompt or "").strip()
        image_values = [str(image).strip() for image in (images or []) if str(image).strip()]
        if not clean_model:
            raise BridgeError("模型名不能为空")
        if not clean_prompt and not image_values:
            raise BridgeError("提示词和参考图不能同时为空")

        if image_values:
            content: Any = [
                {
                    "type": "text",
                    "text": clean_prompt or "请根据参考图生成图片",
                }
            ]
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {"url": image},
                }
                for image in image_values
            )
        else:
            content = clean_prompt

        return await self._request(
            "POST",
            "chat/completions",
            json={
                "model": clean_model,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            },
        )
