"""Mocked session refresh must finish before any daily reservation or send."""
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import safe_main as runner
from app.browser import AuthenticationError
from app.config import ConfigError
from app.douyin import PageOperationError
from app.durable_state import StateError
from app.models import Settings


def fake_state(value):
    return {
        "cookies": [{"name": "sessionid", "value": value, "domain": ".douyin.com"}],
        "origins": [{"origin": "https://www.douyin.com", "localStorage": [
            {"name": "auth", "value": value + "_LOCAL"}], "indexedDB": []}],
    }


@pytest.fixture
def execution(monkeypatch):
    for name in ("GITHUB_EVENT_NAME", "REFRESH_LOGIN_STATE"):
        monkeypatch.delenv(name, raising=False)
    events = []
    settings_seen = []
    latest = fake_state("FAKE_ENCRYPTED_CURRENT_COOKIE")
    refreshed = fake_state("FAKE_REFRESHED_COOKIE")
    settings = Settings(
        task_config_path=Path("config.json"),
        storage_state=json.dumps(fake_state("FAKE_OLD_BOOTSTRAP_COOKIE")),
        cookie=json.dumps(fake_state("FAKE_LEGACY_COOKIE")["cookies"]),
        headless=True, browser_path=None, artifacts_dir=Path("artifacts"), trace=False,
    )
    page = SimpleNamespace(goto=AsyncMock(side_effect=lambda *a, **k: events.append("goto")))
    context = object()
    session = SimpleNamespace(page=page, context=context)

    @asynccontextmanager
    async def browser(selected_settings):
        events.append("browser")
        settings_seen.append(selected_settings)
        yield session

    store = SimpleNamespace(
        load=MagicMock(side_effect=lambda: events.append("load") or latest),
        save=MagicMock(side_effect=lambda state: events.append("save")),
    )
    store_factory = MagicMock(return_value=store)
    monkeypatch.setattr(runner, "EncryptedSessionStore", SimpleNamespace(from_env=store_factory), raising=False)
    monkeypatch.setattr(runner, "open_douyin", browser)
    monkeypatch.setattr(runner, "check_login", AsyncMock(side_effect=lambda p: events.append("check_login")))
    monkeypatch.setattr(runner, "first_visible", AsyncMock(side_effect=lambda *a: events.append("first_visible")))
    capture = AsyncMock(side_effect=lambda c: events.append("capture") or refreshed)
    monkeypatch.setattr(runner, "capture_storage_state", capture, raising=False)
    chat = SimpleNamespace(
        resolved_uid="uidAAAA", expected_uid="uidAAAA",
        open_target=AsyncMock(side_effect=lambda *a, **k: events.append("open_target")),
        confirm_identity=AsyncMock(),
        message_input=AsyncMock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value=""))),
    )
    monkeypatch.setattr(runner, "SafeChat", MagicMock(return_value=chat))
    send = AsyncMock(side_effect=lambda *a: events.append("send"))
    monkeypatch.setattr(runner, "send_once", send)
    monkeypatch.setattr(runner, "today", lambda: "2026-10-24")
    monkeypatch.setenv("DOUYIN_CONFIG", "{}")
    task = SimpleNamespace(
        targets=[SimpleNamespace(name="Fake friend")], target_open_timeout_seconds=1,
        interval_min=0, interval_max=0,
    )
    candidates = [(SimpleNamespace(content="Fake local test message"),)]
    journal = SimpleNamespace(
        contains=MagicMock(side_effect=lambda day: events.append("contains") or False),
        reserve=MagicMock(side_effect=lambda day: events.append("reserve") or True),
        finish=MagicMock(side_effect=lambda *a: events.append("finish")),
    )
    return SimpleNamespace(
        settings=settings, task=task, candidates=candidates, uids=["uidAAAA"],
        store=store, store_factory=store_factory, latest=latest, refreshed=refreshed,
        settings_seen=settings_seen, events=events, session=session, context=context,
        capture=capture, chat=chat, journal=journal, send=send,
    )


async def execute(harness, *, dry_run=True):
    return await runner.execute(harness.settings, harness.task, harness.candidates, harness.uids,
                                dry_run=dry_run, journal=harness.journal)


@pytest.mark.asyncio
async def test_latest_encrypted_state_replaces_old_cookie_and_bootstrap_state(execution):
    h = execution
    assert await execute(h) == 0
    h.store_factory.assert_called_once_with()
    h.store.load.assert_called_once_with()
    assert len(h.settings_seen) == 1
    assert json.loads(h.settings_seen[0].storage_state) == h.latest
    assert h.settings_seen[0].cookie is None
    # Input settings are immutable; only the selected browser credentials change.
    assert "FAKE_OLD_BOOTSTRAP_COOKIE" in h.settings.storage_state
    h.capture.assert_awaited_once_with(h.context)
    h.store.save.assert_called_once_with(h.refreshed)
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_is_saved_once_after_login_and_before_preflight_reservation_and_send(execution):
    h = execution
    assert await execute(h, dry_run=False) == 0
    expected = ["contains", "load", "browser", "goto", "check_login", "first_visible",
                "capture", "save", "open_target", "reserve", "send", "finish"]
    positions = [h.events.index(label) for label in expected]
    assert positions == sorted(positions)
    h.capture.assert_awaited_once_with(h.context)
    h.store.save.assert_called_once_with(h.refreshed)
    h.journal.reserve.assert_called_once_with("2026-10-24")
    h.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmed_missing_encrypted_state_keeps_bootstrap_and_saves_refresh(execution):
    h = execution
    h.store.load.side_effect = None
    h.store.load.return_value = None
    assert await execute(h) == 0
    assert h.settings_seen == [h.settings]
    h.capture.assert_awaited_once_with(h.context)
    h.store.save.assert_called_once_with(h.refreshed)


@pytest.mark.asyncio
async def test_session_load_failure_does_not_open_browser_reserve_or_send(execution, capsys):
    h = execution
    h.store.load.side_effect = StateError("PRIVATE_STORE_FAILURE")
    with pytest.raises(StateError):
        await execute(h, dry_run=False)
    assert h.settings_seen == []
    h.capture.assert_not_awaited()
    h.store.save.assert_not_called()
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()
    assert "PRIVATE_" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_failed_login_never_captures_or_saves_session(execution, capsys):
    h = execution
    runner.check_login.side_effect = AuthenticationError("PRIVATE_AUTH_FAILURE")
    with pytest.raises(AuthenticationError):
        await execute(h, dry_run=False)
    runner.first_visible.assert_not_awaited()
    h.capture.assert_not_awaited()
    h.store.save.assert_not_called()
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()
    assert "PRIVATE_" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_search_box_not_ready_never_captures_or_saves_session(execution):
    h = execution
    runner.first_visible.side_effect = PageOperationError("Not ready")
    with pytest.raises(PageOperationError):
        await execute(h, dry_run=False)
    h.capture.assert_not_awaited()
    h.store.save.assert_not_called()
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_save_failure_stops_before_target_preflight_and_daily_reservation(execution, capsys):
    h = execution
    h.store.save.side_effect = StateError("PRIVATE_STORE_WRITE_FAILURE")
    with pytest.raises(StateError):
        await execute(h, dry_run=False)
    h.capture.assert_awaited_once_with(h.context)
    h.store.save.assert_called_once_with(h.refreshed)
    h.chat.open_target.assert_not_awaited()
    h.journal.reserve.assert_not_called()
    h.journal.finish.assert_not_called()
    h.send.assert_not_awaited()
    assert "PRIVATE_" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_legacy_deployment_without_store_does_not_require_session_context(execution):
    h = execution
    h.store_factory.return_value = None
    del h.session.context
    assert await execute(h, dry_run=False) == 0
    assert h.settings_seen == [h.settings]
    h.store.load.assert_not_called()
    h.capture.assert_not_awaited()
    h.store.save.assert_not_called()
    h.journal.reserve.assert_called_once()
    h.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_daily_reservation_skips_session_access_and_browser(execution):
    h = execution
    h.journal.contains.side_effect = None
    h.journal.contains.return_value = True
    assert await execute(h, dry_run=False) == 0
    h.store_factory.assert_not_called()
    h.store.load.assert_not_called()
    assert h.settings_seen == []
    h.capture.assert_not_awaited()
    h.store.save.assert_not_called()
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_refresh_dry_run_validates_revision_then_uses_new_secret(execution, monkeypatch):
    h = execution
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("REFRESH_LOGIN_STATE", "true")
    new_secret = fake_state("FAKE_NEW_MANUAL_SECRET")
    h.settings = replace(h.settings, storage_state=json.dumps(new_secret), cookie=None)
    assert await execute(h) == 0
    # Existing ciphertext is still read and verified before it can be replaced.
    h.store.load.assert_called_once_with()
    assert h.events.index("load") < h.events.index("browser")
    assert json.loads(h.settings_seen[0].storage_state) == new_secret
    assert json.loads(h.settings_seen[0].storage_state) != h.latest
    h.capture.assert_awaited_once_with(h.context)
    h.store.save.assert_called_once_with(h.refreshed)
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_cannot_ignore_encrypted_state_using_refresh_flag(execution, monkeypatch):
    h = execution
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("REFRESH_LOGIN_STATE", "true")
    assert await execute(h) == 0
    h.store.load.assert_called_once_with()
    assert json.loads(h.settings_seen[0].storage_state) == h.latest
    assert h.settings_seen[0].cookie is None
    h.store.save.assert_called_once_with(h.refreshed)
    h.journal.reserve.assert_not_called()
    h.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_refresh_rejects_real_send_before_browser_or_state_write(execution, monkeypatch):
    h = execution
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("REFRESH_LOGIN_STATE", "true")
    with pytest.raises(ConfigError):
        await execute(h, dry_run=False)
    assert h.settings_seen == []
    h.store.load.assert_not_called()
    h.capture.assert_not_awaited()
    h.store.save.assert_not_called()
    h.journal.reserve.assert_not_called()
    h.journal.finish.assert_not_called()
    h.send.assert_not_awaited()
