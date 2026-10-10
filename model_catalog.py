"""Recover image model metadata already visible in the account's sessions."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

LOGGER = logging.getLogger(__name__)
REFRESH_SECONDS = 1800.0
FAILURE_BACKOFF_SECONDS = 300.0
MAX_STORED_MODELS = 512
_CATALOGS: dict[tuple[str, str], "HistoryModelCatalog"] = {}

_READ_JSON_JS = r"""
(async () => {
  try {
    const response = await fetch(%(path)s, {
      method: 'GET',
      headers: {'Accept': 'application/json'},
      credentials: 'include',
      signal: AbortSignal.timeout(%(timeout_ms)d)
    });
    const result = {
      status: response.status,
      retry_after: response.headers.get('retry-after'),
      body: null
    };
    if (!response.ok) return result;
    const body = await response.json();
    if (%(history)s) {
      if (!Array.isArray(body.entries)) return result;
      result.body = {
        entries: body.entries.map(e => ({
          id: e.id, modelAId: e.modelAId, modelBId: e.modelBId,
          modelAOrganization: e.modelAOrganization,
          modelBOrganization: e.modelBOrganization
        })),
        pagination: body.pagination
      };
    } else if (Array.isArray(body.revealedModels)) {
      result.body = {revealedModels: body.revealedModels};
    }
    return result;
  } catch (_) {
    return {status: 0, body: null};
  }
})()
"""


def _uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def historical_image_model(value: Any) -> dict[str, Any] | None:
    """Only keep model metadata, never the surrounding session or its content."""
    if not isinstance(value, dict) or value.get("organization"):
        return None
    model_id = _uuid(value.get("id"))
    name = str(value.get("publicName") or "").strip()
    caps = value.get("capabilities")
    if not model_id or not name or len(name) > 256 or not isinstance(caps, dict):
        return None
    if "userSelectable" in value and not isinstance(value["userSelectable"], bool):
        return None
    output = caps.get("outputCapabilities") or caps.get("output_capabilities")
    inputs = caps.get("inputCapabilities") or caps.get("input_capabilities") or {}
    if not isinstance(output, dict) or not _truthy(output.get("image")):
        return None
    if not isinstance(inputs, dict):
        inputs = {}
    result = {
        "id": model_id,
        "publicName": name,
        "capabilities": {
            "inputCapabilities": copy.deepcopy(inputs),
            "outputCapabilities": copy.deepcopy(output),
        },
        "history_discovered": True,
        "catalog_source": "session_history",
    }
    for key in ("displayName", "userSelectable", "provider", "name"):
        if key in value:
            result[key] = value[key]
    # Do not infer availability from a successful metadata request.
    if value.get("userSelectable") is False:
        result["userSelectable"] = False
    return result


def history_catalog(endpoint: str, data_dir: Path | None) -> "HistoryModelCatalog":
    path = data_dir / "arena_history_models.json" if data_dir else None
    key = (str(endpoint), str(path or ""))
    if key not in _CATALOGS:
        _CATALOGS[key] = HistoryModelCatalog(path)
    return _CATALOGS[key]


class HistoryModelCatalog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.models: dict[str, dict[str, Any]] = {}
        self.next_refresh_at = 0.0
        self.complete = False
        self.last_status = 0
        self._cursor: str | None = None
        self._pending: dict[str, set[str]] = {}
        self._scheduled: set[str] = set()
        self._seen_cursors: set[str] = set()
        self._end_reached = False
        self._lock = asyncio.Lock()
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return
            for value in payload.get("models", [])[:MAX_STORED_MODELS]:
                model = historical_image_model(value)
                if model:
                    self.models[model["id"]] = model
            self.last_status = int(payload.get("last_status") or 0)
            self.next_refresh_at = float(payload.get("next_refresh_at") or 0)
            if self.last_status != 429:
                self.next_refresh_at = min(
                    self.next_refresh_at, time.time() + REFRESH_SECONDS
                )
        except (OSError, ValueError, TypeError):
            return

    def _save(self) -> None:
        if self.path is None:
            return
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "version": 1,
            "next_refresh_at": self.next_refresh_at,
            "last_status": self.last_status,
            "models": list(self.models.values())[-MAX_STORED_MODELS:],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except OSError:
            LOGGER.warning("Arena historical model cache could not be saved")

    def merge(self, public: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # A current row, including an explicitly disabled row, wins over history.
        live = [m for m in public if isinstance(m, dict) and not m.get("history_discovered")]
        current_ids = {str(m.get("id") or "") for m in live}
        current_names = {str(m.get("publicName") or "").casefold() for m in live}
        historical = {
            str(m.get("id") or ""): m
            for m in public
            if isinstance(m, dict) and m.get("history_discovered")
        }
        historical.update(self.models)
        extras = [
            m for key, m in historical.items()
            if key not in current_ids
            and str(m.get("publicName") or "").casefold() not in current_names
        ]
        return list(live) + copy.deepcopy(extras)

    async def _read(self, page: Any, path: str, timeout: float) -> dict[str, Any]:
        is_history = path.startswith("/api/history/unified?")
        if not is_history and not (
            path.startswith("/api/evaluation/")
            and _uuid(path.removeprefix("/api/evaluation/"))
        ):
            raise ValueError("Unexpected model discovery endpoint")
        expression = _READ_JSON_JS % {
            "path": json.dumps(path),
            "timeout_ms": max(1000, int(timeout * 1000)),
            "history": "true" if is_history else "false",
        }
        result = await page.evaluate(expression, timeout=timeout + 3)
        return result if isinstance(result, dict) else {"status": 0, "body": None}

    def _backoff(self, response: dict[str, Any]) -> None:
        self.last_status = int(response.get("status") or 0)
        wait = FAILURE_BACKOFF_SECONDS
        if self.last_status == 429:
            try:
                wait = max(wait, float(response.get("retry_after") or 0))
            except (ValueError, TypeError):
                try:
                    until = parsedate_to_datetime(str(response.get("retry_after")))
                    wait = max(wait, until.timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        self.next_refresh_at = time.time() + wait

    async def recover(
        self,
        page: Any,
        public: list[dict[str, Any]],
        *,
        force: bool = False,
        max_pages: int = 4,
        max_sessions: int = 6,
        budget_seconds: float = 15.0,
    ) -> dict[str, Any]:
        async with self._lock:
            if time.time() < self.next_refresh_at and (
                not force or self.last_status == 429
            ):
                return self.status()
            if self.complete:
                self._cursor = None
                self._pending.clear()
                self._scheduled.clear()
                self._seen_cursors.clear()
                self._end_reached = False
                self.complete = False
            known = {str(m.get("id") or "") for m in public if isinstance(m, dict)}
            known.update(self.models)
            pages = sessions = 0
            deadline = time.monotonic() + max(1.0, budget_seconds)
            failed = False
            try:
                while time.monotonic() < deadline:
                    timeout = max(1.0, min(10.0, deadline - time.monotonic()))
                    if self._pending:
                        if sessions >= max_sessions:
                            break
                        session_id = next(iter(self._pending))
                        ids = self._pending[session_id]
                        if ids.issubset(known):
                            self._pending.pop(session_id)
                            continue
                        response = await self._read(
                            page, "/api/evaluation/" + session_id, timeout
                        )
                        sessions += 1
                        status = int(response.get("status") or 0)
                        if status in (404, 410):
                            self._pending.pop(session_id)
                            self._scheduled.difference_update(ids)
                            continue
                        body = response.get("body")
                        if status != 200 or not isinstance(body, dict) or not isinstance(
                            body.get("revealedModels"), list
                        ):
                            self._backoff(response)
                            failed = True
                            break
                        self._pending.pop(session_id)
                        for value in body["revealedModels"]:
                            model = historical_image_model(value)
                            if model:
                                self.models[model["id"]] = model
                                known.add(model["id"])
                        self._scheduled.difference_update(ids - known)
                        self.last_status = 200
                    else:
                        if self._end_reached or pages >= max_pages:
                            break
                        query = {
                            "limit": 50,
                            "type": "evaluation",
                            "modality": "image",
                            "includeArchived": "true",
                        }
                        if self._cursor:
                            query["cursor"] = self._cursor
                        response = await self._read(
                            page, "/api/history/unified?" + urlencode(query), timeout
                        )
                        pages += 1
                        body = response.get("body")
                        if response.get("status") != 200 or not isinstance(body, dict) or not isinstance(
                            body.get("entries"), list
                        ):
                            self._backoff(response)
                            failed = True
                            break
                        for entry in body["entries"]:
                            if not isinstance(entry, dict) or not _uuid(entry.get("id")):
                                continue
                            ids = {
                                _uuid(entry.get(f"model{side}Id"))
                                for side in ("A", "B")
                                if not entry.get(f"model{side}Organization")
                            } - {""}
                            missing = ids - known - self._scheduled
                            if missing:
                                self._pending[_uuid(entry["id"])] = missing
                                self._scheduled.update(missing)
                        pagination = body.get("pagination")
                        if not isinstance(pagination, dict):
                            self._backoff({"status": 0})
                            failed = True
                            break
                        cursor = pagination.get("cursor")
                        if (
                            not pagination.get("hasMore") or not isinstance(cursor, str)
                            or not cursor or cursor in self._seen_cursors
                        ):
                            self._end_reached = True
                        else:
                            self._cursor = cursor
                            self._seen_cursors.add(cursor)
                        self.last_status = 200
                    await asyncio.sleep(0.25)
            except Exception:
                # Discovery is optional; a timeout must not remove public models.
                self._backoff({"status": 0})
                failed = True
            self.complete = self._end_reached and not self._pending
            if not failed:
                self.next_refresh_at = time.time() + (
                    REFRESH_SECONDS if self.complete else 30.0
                )
            self._save()
            return {**self.status(), "history_pages": pages, "session_reads": sessions}

    def status(self) -> dict[str, Any]:
        return {
            "recovered_variants": len(self.models),
            "history_scan_complete": self.complete,
            "last_status": self.last_status,
            "next_refresh_at": self.next_refresh_at,
            "scope": "public_and_account_history",
        }
