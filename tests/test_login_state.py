import json
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import safe_main
from app.browser import AuthenticationError
from app.config import ConfigError
from scripts import login as helper


@pytest.fixture
def state():
    return {"cookies": [{"name": "sessionid", "value": "PRIVATE_COOKIE",
                         "domain": ".douyin.com", "path": "/"}],
            "origins": [{"origin": "https://www.douyin.com",
                         "localStorage": [{"name": "auth", "value": "PRIVATE_LOCAL"}],
                         "indexedDB": []}]}


@pytest.fixture
def fake_browser(monkeypatch, state):
    page = SimpleNamespace(goto=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page),
                              storage_state=AsyncMock(return_value=state))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    launch = AsyncMock(return_value=browser)
    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setattr(helper, "async_playwright", playwright)
    monkeypatch.setattr(helper, "verify_chat_session", AsyncMock())
    monkeypatch.setattr(helper, "_open_login", AsyncMock())
    monkeypatch.setattr(helper.asyncio, "to_thread", AsyncMock())
    return browser, context, page, launch


@pytest.mark.asyncio
async def test_export_filters_third_party_and_keeps_complete_storage(tmp_path, state):
    state["cookies"].append({"name": "external", "value": "PRIVATE_EXTERNAL", "domain": "example.com"})
    state["origins"].append({"origin": "https://example.com", "localStorage": []})
    context = SimpleNamespace(storage_state=AsyncMock(return_value=state))
    output = tmp_path / "storage-state.json"
    await helper.save_authenticated_state(context, output)
    context.storage_state.assert_awaited_once_with(indexed_db=True)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert len(saved["cookies"]) == len(saved["origins"]) == 1
    assert saved["origins"][0] == state["origins"][0]
    assert "PRIVATE_EXTERNAL" not in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_failed_authentication_never_overwrites_previous_state(tmp_path, state, fake_browser):
    browser, context, _, _ = fake_browser
    output = tmp_path / "storage-state.json"
    original = json.dumps(state).encode()
    output.write_bytes(original)
    helper.verify_chat_session.side_effect = AuthenticationError("PRIVATE_EXCEPTION")
    with pytest.raises(AuthenticationError):
        await helper.login(output, check_only=True)
    assert output.read_bytes() == original
    context.storage_state.assert_not_awaited()
    browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_only_reuses_and_saves_server_updated_state(tmp_path, state, fake_browser, capsys):
    browser, context, _, launch = fake_browser
    output = tmp_path / "storage-state.json"
    output.write_text(json.dumps(state), encoding="utf-8")
    fresh = json.loads(json.dumps(state))
    fresh["cookies"][0]["value"] = "PRIVATE_REFRESHED"
    context.storage_state.return_value = fresh
    await helper.login(output, check_only=True)
    launch.assert_awaited_once_with(headless=True)
    assert browser.new_context.call_args.kwargs["storage_state"]["cookies"][0]["value"] == "PRIVATE_COOKIE"
    assert json.loads(output.read_text(encoding="utf-8"))["cookies"][0]["value"] == "PRIVATE_REFRESHED"
    helper.asyncio.to_thread.assert_not_awaited()
    assert "PRIVATE_" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_interactive_login_can_repair_corrupted_state(tmp_path, fake_browser):
    output = tmp_path / "storage-state.json"
    output.write_text("broken previous state", encoding="utf-8")
    await helper.login(output)
    helper.asyncio.to_thread.assert_awaited_once()
    helper.verify_chat_session.assert_awaited_once()
    assert json.loads(output.read_text(encoding="utf-8"))["cookies"]


@pytest.mark.asyncio
async def test_check_only_missing_or_corrupt_state_cannot_open_browser(tmp_path, fake_browser):
    output = tmp_path / "storage-state.json"
    with pytest.raises(ConfigError):
        await helper.login(output, check_only=True)
    output.write_text("PRIVATE_BROKEN_STATE", encoding="utf-8")
    with pytest.raises(ValueError):
        await helper.login(output, check_only=True)
    fake_browser[3].assert_not_awaited()
    assert output.read_text(encoding="utf-8") == "PRIVATE_BROKEN_STATE"


@pytest.mark.asyncio
async def test_interactive_failed_authentication_keeps_corrupt_old_file(tmp_path, fake_browser):
    output = tmp_path / "storage-state.json"
    output.write_bytes(b"old-corrupt-but-preserved")
    helper.verify_chat_session.side_effect = AuthenticationError("PRIVATE_EXCEPTION")
    with pytest.raises(AuthenticationError):
        await helper.login(output)
    assert output.read_bytes() == b"old-corrupt-but-preserved"
    fake_browser[1].storage_state.assert_not_awaited()


def test_login_failure_summary_contains_only_fixed_maintenance_steps(monkeypatch, tmp_path, capsys):
    logger = logging.getLogger("douyin_sender")
    monkeypatch.setattr(logger, "disabled", logger.disabled)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(safe_main, "load_safe_config", lambda: (None, None, None, None))
    monkeypatch.setattr(safe_main, "execute", AsyncMock(side_effect=AuthenticationError("PRIVATE_EXCEPTION")))
    send = AsyncMock()
    monkeypatch.setattr(safe_main, "send_once", send)
    assert safe_main.main([]) == 2
    text = capsys.readouterr().out + summary.read_text(encoding="utf-8")
    assert "auth_required" in text and "DOUYIN_STORAGE_STATE" in text
    assert "PRIVATE_" not in text
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_validation_requires_authenticated_chat_and_search(monkeypatch):
    checks = []
    page = SimpleNamespace(goto=AsyncMock())
    async def mark_login(_):
        checks.append("login")
    async def mark_session(_):
        checks.append("session")
    async def mark_search(*_):
        checks.append("search")
    monkeypatch.setattr(helper, "verify_login", mark_login)
    monkeypatch.setattr(helper, "ensure_session", mark_session)
    monkeypatch.setattr(helper, "first_visible", mark_search)
    await helper.verify_chat_session(page)
    page.goto.assert_awaited_once_with(helper.DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
    assert checks == ["login", "session", "search"]
