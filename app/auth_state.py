"""Validated Douyin authentication snapshots; never log their contents."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from app.browser import _normalize_cookies
from app.config import ConfigError


def douyin_domain(domain) -> bool:
    if not isinstance(domain, str):
        return False
    domain = domain.removeprefix(".").lower()
    return re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*douyin\.com", domain) is not None


def douyin_origin(origin) -> bool:
    if not isinstance(origin, str):
        return False
    try:
        parsed = urlsplit(origin)
        return (parsed.scheme == "https" and douyin_domain(parsed.hostname)
                and not parsed.username and not parsed.password
                and parsed.port is None and not parsed.path
                and not parsed.query and not parsed.fragment
                and origin == f"https://{parsed.hostname}")
    except ValueError:
        return False


def normalize_storage_state(raw) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("cookies"), list):
        raise ConfigError("DOUYIN_STORAGE_STATE 必须包含 Cookie 数组")
    cookies = _normalize_cookies(raw["cookies"])
    if any(not douyin_domain(cookie["domain"]) for cookie in cookies):
        raise ConfigError("登录状态仅允许抖音域名")
    origins = raw.get("origins", [])
    if not isinstance(origins, list):
        raise ConfigError("登录状态 origins 必须为数组")
    normalized = []
    for origin in origins:
        if not isinstance(origin, dict) or not douyin_origin(origin.get("origin")):
            raise ConfigError("登录状态仅允许抖音 HTTPS origin")
        local = origin.get("localStorage", [])
        if not isinstance(local, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str) for item in local
        ):
            raise ConfigError("登录状态 localStorage 格式无效")
        item = {"origin": origin["origin"], "localStorage": local}
        if "indexedDB" in origin:
            if not isinstance(origin["indexedDB"], list):
                raise ConfigError("登录状态 IndexedDB 格式无效")
            item["indexedDB"] = origin["indexedDB"]
        normalized.append(item)
    return {"cookies": cookies, "origins": normalized}


def write_storage_state(path: Path, state: dict) -> None:
    """Same-directory replace: interruption cannot truncate the previous snapshot."""
    state = normalize_storage_state(state)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".douyin-auth-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def capture_storage_state(context) -> dict:
    raw = await context.storage_state(indexed_db=True)
    return normalize_storage_state({
        "cookies": [c for c in raw["cookies"] if douyin_domain(c.get("domain"))],
        "origins": [o for o in raw.get("origins", []) if douyin_origin(o.get("origin"))],
    })
