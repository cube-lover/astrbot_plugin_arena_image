"""Initialize persistent bridge configuration, then execute the container command."""

from __future__ import annotations

import json
import os
import secrets
import sys
from contextlib import suppress
from pathlib import Path


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _rpm_from_environment() -> int:
    try:
        return max(1, min(100_000, int(os.environ.get("LM_BRIDGE_RPM", "30"))))
    except (TypeError, ValueError):
        return 30


def _write_api_key_file(path: Path, api_key: str) -> None:
    """Keep the effective key discoverable without placing it in container logs."""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(f"{api_key}\n", encoding="utf-8")
    temporary.replace(path)
    with suppress(OSError):
        os.chmod(path, 0o600)


def initialize() -> None:
    data_dir = Path(os.environ.get("LM_BRIDGE_DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    models_path = data_dir / "models.json"
    api_key_path = data_dir / "api-key.txt"
    config = _load(config_path)

    admin_password = os.environ.get("LM_BRIDGE_ADMIN_PASSWORD", "").strip()
    api_key = os.environ.get("LM_BRIDGE_API_KEY", "").strip()

    config.setdefault("password", admin_password or "admin")
    if admin_password and config.get("password") in (None, "", "admin"):
        config["password"] = admin_password

    api_keys = config.get("api_keys")
    if not isinstance(api_keys, list):
        api_keys = []

    normalized_api_keys = [
        entry
        for entry in api_keys
        if isinstance(entry, dict) and str(entry.get("key") or "").strip()
    ]
    # Zero-config mode: when LM_BRIDGE_API_KEY is empty and no keys exist yet,
    # leave api_keys empty so the service allows anonymous access from the
    # Docker-internal network (the Bridge only binds 127.0.0.1 externally).
    if api_key:
        if not normalized_api_keys:
            normalized_api_keys.append(
                {
                    "name": "AstrBot",
                    "key": api_key,
                    "rpm": _rpm_from_environment(),
                    "created": 0,
                }
            )
    config["api_keys"] = normalized_api_keys

    config.setdefault("auth_token", "")
    if not isinstance(config.get("auth_tokens"), list):
        config["auth_tokens"] = []
    config.setdefault("cf_clearance", "")
    if not isinstance(config.get("usage_stats"), dict):
        config["usage_stats"] = {}
    config.setdefault("persist_arena_auth_cookie", True)

    _write_atomic(config_path, config)
    if not models_path.exists():
        _write_atomic(models_path, [])
    _write_api_key_file(api_key_path, str(normalized_api_keys[0]["key"]).strip())

    print(f"LMArenaBridge data directory: {data_dir}")
    print(f"Effective API key is stored at: {api_key_path}")


if __name__ == "__main__":
    initialize()
    command = sys.argv[1:] or ["python", "-m", "src.main"]
    os.execvp(command[0], command)
