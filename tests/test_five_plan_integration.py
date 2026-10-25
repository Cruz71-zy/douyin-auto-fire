"""Five invented targets: four fixed personal identities and one fixed group."""
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import safe_main as runner
from app.config import ConfigError
from app.daily_plan import merge_private_targets
from app.models import Settings
from app.safe_chat import TargetCheckError


@pytest.fixture
def five_plan(monkeypatch, tmp_path):
    raw = {
        "task_id": "fake-batch-task", "timezone": "Asia/Shanghai", "prevent_duplicates": True,
        "send_interval_seconds": {"min": 0, "max": 0},
        "targets": [{"name": "PRIVATE_FAKE_USER_ALPHA", "sec_uid": "fake_uid_alpha",
                     "messages": [{"type": "text", "content": "Fake original text"}]}],
    }
    updates = {
        "version": 1, "expected_total": 5,
        "add_targets": [
            {"name": "PRIVATE_FAKE_USER_BRAVO", "sec_uid": "fake_uid_bravo"},
            {"name": "PRIVATE_FAKE_USER_CHARLIE", "sec_uid": "fake_uid_charlie"},
            {"name": "PRIVATE_FAKE_USER_DELTA", "sec_uid": "fake_uid_delta"},
            {"name": "PRIVATE_FAKE_GROUP", "kind": "group", "group_id": "fake_group_conversation_001",
             "group_short_id": "1234567890123456789"},
        ],
        "daily_messages": [f"Fake daily greeting {index:02d}" for index in range(14)],
    }
    settings = Settings(
        task_config_path=tmp_path / "unused-fake-config.json", storage_state=None,
        cookie=json.dumps([{"name": "sessionid", "value": "FAKE_COOKIE_ONLY", "domain": ".douyin.com"}]),
        headless=True, browser_path=None, artifacts_dir=tmp_path / "artifacts", trace=False,
    )
    # Bypass dotenv/default files entirely: only invented credentials can be read.
    monkeypatch.setattr(runner, "load_settings", lambda: settings)
    monkeypatch.setenv("DOUYIN_CONFIG", json.dumps(raw))
    monkeypatch.setenv("DOUYIN_TARGET_UPDATES", json.dumps(updates))
    for name in ("VERIFY_TARGET_UPDATES", "REFRESH_LOGIN_STATE", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner, "today", lambda: "2026-10-25")
    return SimpleNamespace(raw=raw, updates=updates, settings=settings)


def test_merge_and_safe_loader_keep_four_users_one_group_and_fourteen_messages(five_plan, capsys):
    p = five_plan
    before = copy.deepcopy(p.raw)
    merged = merge_private_targets(p.raw, json.dumps(p.updates))
    assert len(merged["targets"]) == 5
    assert merged["targets"][0]["sec_uid"] == "fake_uid_alpha"
    group = merged["targets"][-1]
    assert group["kind"] == "group" and "sec_uid" not in group
    assert group["group_id"] == "fake_group_conversation_001"
    assert group["group_short_id"] == "1234567890123456789"
    assert merged["daily_message_rotation"] is True
    assert p.raw == before

    _, task, candidates, ids = runner.load_safe_config()
    assert [target.kind for target in task.targets] == ["user"] * 4 + ["group"]
    assert task.targets[-1].group_short_id == group["group_short_id"]
    assert ids == ["fake_uid_alpha", "fake_uid_bravo", "fake_uid_charlie", "fake_uid_delta", group["group_id"]]
    assert all(len(options) == 14 for options in candidates)
    assert all([message.content for message in options] == p.updates["daily_messages"] for options in candidates)
    assert task.daily_message_rotation is True
    assert "PRIVATE_FAKE" not in capsys.readouterr().out


@pytest.mark.parametrize("corruption", ["fake_sec_uid", "missing_short_id"])
def test_group_schema_rejects_personal_uid_or_missing_short_id(five_plan, monkeypatch, corruption):
    updates = copy.deepcopy(five_plan.updates)
    if corruption == "fake_sec_uid":
        updates["add_targets"][-1]["sec_uid"] = "fake_personal_uid"
    else:
        del updates["add_targets"][-1]["group_short_id"]
    monkeypatch.setenv("DOUYIN_TARGET_UPDATES", json.dumps(updates))
    with pytest.raises(ConfigError):
        runner.load_safe_config()


@pytest.mark.parametrize("corruption", ["missing_group", "duplicate_addition", "extra_target"])
def test_expected_total_five_rejects_partial_or_duplicate_recipient_plan(five_plan, monkeypatch, corruption):
    updates = copy.deepcopy(five_plan.updates)
    if corruption == "missing_group":
        updates["add_targets"].pop()
    elif corruption == "duplicate_addition":
        updates["add_targets"][-1] = copy.deepcopy(updates["add_targets"][0])
    else:
        updates["add_targets"].append({"name": "PRIVATE_FAKE_EXTRA", "sec_uid": "fake_uid_extra"})
    monkeypatch.setenv("DOUYIN_TARGET_UPDATES", json.dumps(updates))
    with pytest.raises(ConfigError):
        runner.load_safe_config()


@pytest.fixture
def execution(five_plan, monkeypatch):
    settings, task, candidates, ids = runner.load_safe_config()
    h = SimpleNamespace(settings=settings, task=task, candidates=candidates, ids=ids,
                        events=[], chats=[], opened=[], group_mismatch=False, days={}, writes=[])
    page = SimpleNamespace(goto=AsyncMock())

    @asynccontextmanager
    async def browser(selected_settings):
        h.opened.append(selected_settings)
        yield SimpleNamespace(page=page)

    monkeypatch.setattr(runner, "open_douyin", browser)
    monkeypatch.setattr(runner.EncryptedSessionStore, "from_env", MagicMock(return_value=None))
    monkeypatch.setattr(runner, "check_login", AsyncMock())
    monkeypatch.setattr(runner, "first_visible", AsyncMock())
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())

    def make_chat(kind, uid, short_id=None):
        index = ids.index(uid)
        name = task.targets[index].name
        chat = SimpleNamespace(kind=kind, resolved_uid=uid, expected_uid=uid, stage="init")

        async def open_target(target_name, retries=0):
            assert target_name == name and retries == 0
            h.events.append(("open", name, chat.stage))

        async def confirm_identity(target_name):
            assert target_name == name
            h.events.append(("identity", name, chat.stage))
            if kind == "group" and h.group_mismatch:
                raise TargetCheckError("group_identity_mismatch")

        async def message_input():
            h.events.append(("preflight", name))
            return SimpleNamespace(inner_text=AsyncMock(return_value=""))

        chat.open_target = AsyncMock(side_effect=open_target)
        chat.confirm_identity = AsyncMock(side_effect=confirm_identity)
        chat.message_input = AsyncMock(side_effect=message_input)
        if kind == "group":
            assert short_id == "1234567890123456789"
            chat.expected_short_id = short_id
        h.chats.append(chat)
        return chat

    h.user_factory = MagicMock(side_effect=lambda page, expected_uid, **kw: make_chat("user", expected_uid))
    h.group_factory = MagicMock(side_effect=lambda page, expected_group_id, expected_short_id, **kw:
                                make_chat("group", expected_group_id, expected_short_id))
    monkeypatch.setattr(runner, "SafeChat", h.user_factory)
    monkeypatch.setattr(runner, "SafeGroupChat", h.group_factory)

    async def send(chat, name, text, check_date):
        check_date()
        h.events.append(("send", name, text))

    h.send = AsyncMock(side_effect=send)
    monkeypatch.setattr(runner, "send_once", h.send)

    def reserve(day):
        h.events.append(("reserve", day))
        if day in h.days:
            return False
        h.days[day] = "reserved"
        h.writes.append((day, "reserved"))
        return True

    def finish(day, status):
        assert h.days[day] == "reserved"
        h.days[day] = status
        h.writes.append((day, status))

    h.journal = SimpleNamespace(contains=MagicMock(side_effect=lambda day: day in h.days),
                                reserve=MagicMock(side_effect=reserve), finish=MagicMock(side_effect=finish))
    return h


async def execute(h, *, dry_run=False):
    return await runner.execute(h.settings, h.task, h.candidates, h.ids,
                                dry_run=dry_run, journal=h.journal)


@pytest.mark.asyncio
async def test_five_targets_all_pass_preflight_before_one_daily_reservation_and_one_send_each(execution):
    h = execution
    assert await execute(h) == 0
    assert h.user_factory.call_count == 4 and h.group_factory.call_count == 1
    preflights = [index for index, event in enumerate(h.events) if event[0] == "preflight"]
    reserve_index = next(index for index, event in enumerate(h.events) if event[0] == "reserve")
    sends = [event for event in h.events if event[0] == "send"]
    assert len(preflights) == 5 and max(preflights) < reserve_index
    assert len(sends) == 5 and len({event[1] for event in sends}) == 5
    assert all(index > reserve_index for index, event in enumerate(h.events) if event[0] == "send")
    h.journal.reserve.assert_called_once_with("2026-10-25")
    assert h.writes == [("2026-10-25", "reserved"), ("2026-10-25", "success")]

    # A changed process/run still cannot send again for an already recorded day.
    assert await execute(h) == 0
    assert h.send.await_count == 5 and len(h.opened) == 1
    assert h.user_factory.call_count == 4 and h.group_factory.call_count == 1
    assert len(h.writes) == 2


@pytest.mark.asyncio
async def test_five_target_dry_run_checks_every_identity_without_sends_or_journal_writes(execution):
    h = execution
    assert await execute(h, dry_run=True) == 0
    assert len(h.chats) == 5
    assert all(chat.confirm_identity.await_count == 1 for chat in h.chats)
    assert all(chat.message_input.await_count == 1 for chat in h.chats)
    h.send.assert_not_awaited()
    h.journal.reserve.assert_not_called()
    h.journal.finish.assert_not_called()
    assert h.days == {} and h.writes == []


@pytest.mark.asyncio
async def test_group_identity_error_stops_entire_batch_before_reservation_or_any_send(execution):
    h = execution
    h.group_mismatch = True
    with pytest.raises(TargetCheckError) as error:
        await execute(h)
    assert error.value.code == "group_identity_mismatch"
    assert len(h.chats) == 5
    h.send.assert_not_awaited()
    h.journal.reserve.assert_not_called()
    h.journal.finish.assert_not_called()
    assert h.days == {} and h.writes == []


@pytest.mark.asyncio
async def test_next_day_keeps_one_send_per_target_and_rotates_each_message(execution, monkeypatch):
    h = execution
    assert await execute(h) == 0
    first_day = {event[1]: event[2] for event in h.events if event[0] == "send"}
    h.events.clear()
    monkeypatch.setattr(runner, "today", lambda: "2026-10-26")
    assert await execute(h) == 0
    second_day = {event[1]: event[2] for event in h.events if event[0] == "send"}
    assert len(first_day) == len(second_day) == 5
    assert all(first_day[name] != second_day[name] for name in first_day)
    assert h.send.await_count == 10


def test_verifying_target_update_draft_cannot_enable_real_send(execution, monkeypatch):
    h = execution
    monkeypatch.setenv("VERIFY_TARGET_UPDATES", "true")
    monkeypatch.setenv("ENABLE_REAL_SEND", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    run_async = MagicMock()
    monkeypatch.setattr(runner.asyncio, "run", run_async)
    assert runner.main(["--send"]) == 2
    run_async.assert_not_called()
    assert h.opened == []
    h.send.assert_not_awaited()
    h.journal.reserve.assert_not_called()
    assert h.writes == []


def test_draft_verification_requires_effective_update_payload(five_plan, monkeypatch):
    monkeypatch.setenv("VERIFY_TARGET_UPDATES", "true")
    monkeypatch.delenv("DOUYIN_TARGET_UPDATES")
    monkeypatch.setenv("DOUYIN_TARGET_UPDATES_DRAFT", json.dumps(five_plan.updates))
    # The workflow must map the selected Secret to the effective runtime variable.
    with pytest.raises(ConfigError):
        runner.load_safe_config()
