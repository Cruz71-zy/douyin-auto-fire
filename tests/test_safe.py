import base64
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from app import safe_main as runner
from app.config import ConfigError
from app.durable_state import GitHubJournal, StateError
from app.douyin import PageOperationError
from app.safe_chat import TargetCheckError
from scripts.actions_entry import arguments


def test_failure_summary_preserves_fixed_stage_without_private_values():
    error=PageOperationError('PRIVATE_COOKIE_AND_CHAT_CONTENT')
    error.code='PRIVATE_CODE'
    summary=runner.exception_summary(error,'send_editor_blank')
    assert 'send_editor_blank' in summary and 'PageOperationError' in summary
    assert 'PRIVATE' not in summary
    summary=runner.exception_summary(error,'private_stage_data')
    assert 'unknown' in summary and 'private_stage_data' not in summary
    assert 'draft_present' in runner.exception_summary(TargetCheckError('draft_present'),'preflight_editor')


class MemoryJournal(GitHubJournal):
    def __init__(self):
        super().__init__("owner/repository", "secret-token", "123")
        self.data = {"version": 1, "days": {}}
        self.revision = 1
        self.fail_write = False
        self.posts = []

    def _request(self, method, path, payload=None):
        if method == "GET":
            return {"sha": f"{self.revision:040x}",
                    "content": base64.b64encode(json.dumps(self.data).encode()).decode()}
        if method == "POST":
            self.posts.append(payload)
            return {}
        if self.fail_write or (payload.get("sha") and payload["sha"] != f"{self.revision:040x}"):
            raise StateError("模拟冲突/超时")
        self.data = json.loads(base64.b64decode(payload["content"]))
        self.revision += 1
        return {}


def test_durable_reservation_and_success():
    journal = MemoryJournal()
    assert journal.reserve("2026-10-17")
    assert not journal.reserve("2026-10-17")
    journal.finish("2026-10-17", "success")
    assert journal.contains("2026-10-17")
    assert not journal.reserve("2026-10-17")
    assert journal.reserve("2026-10-18")


def test_unknown_and_cancelled_block_changed_config_and_new_run():
    journal = MemoryJournal()
    journal.reserve("2026-10-17")
    journal.run_id = "456"
    assert journal.contains("2026-10-17")
    assert not journal.reserve("2026-10-17")
    with pytest.raises(StateError):
        journal.finish("2026-10-17", "success")


@pytest.mark.parametrize("data", [[], {}, {"version": 2, "days": {}},
    {"version": 1, "days": []}, {"version": 1, "days": {"bad-date": {}}},
    {"version": 1, "days": {"2026-10-17": {"status": "deleted", "run_id": "1"}}}])
def test_corrupt_journal_stops(data):
    journal = MemoryJournal()
    journal.data = data
    with pytest.raises(StateError):
        journal.reserve("2026-10-17")


def test_cas_conflict_is_not_retried():
    journal = MemoryJournal()
    data, sha = journal.load()
    journal.reserve("2026-10-17")
    with pytest.raises(StateError):
        journal._write(data, sha)
    assert journal.contains("2026-10-17")


def test_reservation_ack_missing_fails_closed():
    journal = MemoryJournal()
    original = journal._request
    def request(method, path, payload=None):
        result = original(method, path, payload)
        if method == "PUT":
            raise StateError("模拟服务端已提交但客户端未收到响应")
        return result
    journal._request = request
    with pytest.raises(StateError):
        journal.reserve("2026-10-17")
    assert journal.contains("2026-10-17")


@pytest.fixture
def config_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in ("DOUYIN_STORAGE_STATE", "DINGTALK_WEBHOOK", "DINGTALK_SECRET",
                "WEBHOOK_URL", "WEBHOOK_HEADERS", "WEBHOOK_TEMPLATE", "TASK_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOUYIN_COOKIE", json.dumps([
        {"name": "sessionid", "value": "FAKE-COOKIE-NOT-REAL", "domain": ".douyin.com"}]))
    raw = {"friends": ["好友A", "好友B"], "messages": [{"type": "text", "value": "🔥"},
        {"type": "text", "value": "今天好"}]}
    monkeypatch.setenv("DOUYIN_CONFIG", json.dumps(raw))
    return raw


def test_compatible_multiple_friends_and_candidates(config_env):
    settings, task, candidates, uids = runner.load_safe_config()
    assert [t.name for t in task.targets] == ["好友A", "好友B"]
    assert len(candidates) == 2 and all(len(c) == 2 for c in candidates)
    assert uids == [None, None]
    assert settings.trace is False and settings.storage_state is None


@pytest.mark.parametrize("override", [
    {"friends": ["A", "A"]}, {"messages": [{"type": "image", "value": "x.png"}]},
    {"messages": []}, {"prevent_duplicates": False}, {"timezone": "UTC"},
    {"friends": []}, {"messages": [{"type": "text", "value": "x"*201}]},
    {"messages": [{"type":"text","value":"\u200b"}]}])
def test_unsafe_config_rejected(config_env, monkeypatch, override):
    config_env.update(override)
    monkeypatch.setenv("DOUYIN_CONFIG", json.dumps(config_env))
    with pytest.raises((ConfigError, ValueError)):
        runner.load_safe_config()


def test_stable_uid_config(config_env, monkeypatch):
    monkeypatch.setenv("DOUYIN_CONFIG", json.dumps({"targets": [
        {"name": "A", "sec_uid": "MS4wLjABAAAAexample", "messages": [{"type": "text", "value": "hi"}]}]}))
    assert runner.load_safe_config()[3] == ["MS4wLjABAAAAexample"]


@pytest.mark.parametrize('event,override,allowed', [
    ('workflow_dispatch', {}, True),
    ('schedule', {}, False),
    ('push', {}, False),
    ('workflow_dispatch', {'date':'2026-10-16'}, False),
    ('workflow_dispatch', {'sec_uid':'differentUID'}, False),
    ('workflow_dispatch', {'text':'different message'}, False),
    ('workflow_dispatch', {'extra':True}, False),
])
def test_one_shot_bound_to_manual_date_identity_and_text(monkeypatch, event, override, allowed):
    authorization={'date':'2026-10-17','sec_uid':'expectedUID','text':'🔥',**override}
    monkeypatch.setenv('GITHUB_EVENT_NAME',event)
    monkeypatch.setenv('DOUYIN_CONFIG',json.dumps({'one_shot':authorization}))
    monkeypatch.setattr(runner,'today',lambda:'2026-10-17')
    task=SimpleNamespace(targets=[SimpleNamespace(name='A')])
    candidates=[(SimpleNamespace(content='🔥'),)]
    assert runner.one_shot_allowed(task,candidates,['expectedUID']) is allowed
    assert not runner.one_shot_allowed(task,candidates*2,['expectedUID']*2)
    assert not runner.one_shot_allowed(task,[(SimpleNamespace(content='🔥'),)*2],['expectedUID'])
    assert not runner.one_shot_allowed(task,candidates,[None])


def test_missing_cookie_never_starts_browser(config_env, monkeypatch, capsys):
    monkeypatch.delenv("DOUYIN_COOKIE")
    browser = MagicMock()
    monkeypatch.setattr(runner, "open_douyin", browser)
    assert runner.main([]) == 2
    browser.assert_not_called()
    assert "FAKE-COOKIE" not in capsys.readouterr().out


@pytest.mark.parametrize("args,enabled,actions", [(["--send"], "false", "true"),
    (["--send"], "true", "false")])
def test_explicit_send_gates(config_env, monkeypatch, args, enabled, actions):
    monkeypatch.setenv("ENABLE_REAL_SEND", enabled)
    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    browser = MagicMock()
    monkeypatch.setattr(runner, "open_douyin", browser)
    assert runner.main(args) == 2
    browser.assert_not_called()


def setup_execution(monkeypatch, count=2):
    page = SimpleNamespace(goto=AsyncMock())
    @asynccontextmanager
    async def browser(settings):
        yield SimpleNamespace(page=page)
    monkeypatch.setattr(runner, "open_douyin", browser)
    for method in ("verify_login", "ensure_session", "first_visible"):
        monkeypatch.setattr(runner, method, AsyncMock())
    counter = iter(range(count))
    chats = []
    def chat(*a, **kw):
        obj = SimpleNamespace(resolved_uid=str(next(counter)), open_target=AsyncMock(),
                              confirm_identity=AsyncMock(),
                              message_input=AsyncMock(return_value=SimpleNamespace(inner_text=AsyncMock(return_value=''))),
                              expected_uid=None)
        chats.append(obj)
        return obj
    monkeypatch.setattr(runner, "SafeChat", chat)
    monkeypatch.setattr(runner, "send_once", AsyncMock())
    monkeypatch.setattr(runner.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(runner, "today", lambda: "2026-10-17")
    task = SimpleNamespace(targets=[SimpleNamespace(name=f"Friend{i}") for i in range(count)],
                           target_open_timeout_seconds=1, interval_min=0, interval_max=0)
    candidates = [(SimpleNamespace(content="🔥"),)] * count
    return task, candidates, chats


@pytest.mark.asyncio
async def test_dry_run_checks_login_all_targets_and_never_sends_or_writes(monkeypatch):
    task, candidates, chats = setup_execution(monkeypatch)
    journal = MemoryJournal()
    assert await runner.execute(None, task, candidates, [None]*2, journal=journal) == 0
    assert len(chats) == 2
    assert all(c.open_target.await_count == 1 for c in chats)
    assert all(c.message_input.await_count == 1 for c in chats)
    runner.verify_login.assert_awaited_once()
    runner.send_once.assert_not_awaited()
    assert journal.data["days"] == {}


@pytest.mark.asyncio
async def test_one_send_per_friend_and_second_run_skips_before_browser(monkeypatch):
    task, candidates, chats = setup_execution(monkeypatch)
    journal = MemoryJournal()
    assert await runner.execute(None, task, candidates, ["0","1"], dry_run=False, journal=journal) == 0
    assert runner.send_once.await_count == 2
    assert journal.data["days"]["2026-10-17"]["status"] == "success"
    assert await runner.execute(None, task, candidates, ["0","1"], dry_run=False, journal=journal) == 0
    assert runner.send_once.await_count == 2


@pytest.mark.asyncio
async def test_unknown_blocks_rest_and_next_run(monkeypatch, capsys):
    task, candidates, _ = setup_execution(monkeypatch)
    runner.send_once.side_effect = RuntimeError("COOKIE-DO-NOT-LOG; private message")
    journal = MemoryJournal()
    assert await runner.execute(None, task, candidates, ["0","1"], dry_run=False, journal=journal) == 1
    assert runner.send_once.await_count == 1
    assert journal.data["days"]["2026-10-17"]["status"] == "unknown"
    assert await runner.execute(None, task, candidates, ["0","1"], dry_run=False, journal=journal) == 0
    assert "COOKIE-DO-NOT-LOG" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_state_write_failure_prevents_send(monkeypatch):
    task, candidates, _ = setup_execution(monkeypatch)
    journal = MemoryJournal()
    journal.fail_write = True
    with pytest.raises(StateError):
        await runner.execute(None, task, candidates, ["0","1"], dry_run=False, journal=journal)
    runner.send_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_midnight_stops_before_reservation(monkeypatch):
    task, candidates, _ = setup_execution(monkeypatch)
    days = iter(["2026-10-17", "2026-10-18"])
    monkeypatch.setattr(runner, "today", lambda: next(days))
    journal = MemoryJournal()
    with pytest.raises(StateError):
        await runner.execute(None, task, candidates, ["0","1"], dry_run=False, journal=journal)
    runner.send_once.assert_not_awaited()
    assert journal.data["days"] == {}


@pytest.mark.asyncio
async def test_formal_send_requires_pinned_configuration_identity(monkeypatch):
    task, candidates, _ = setup_execution(monkeypatch)
    journal = MemoryJournal()
    with pytest.raises(runner.IdentityConfigError):
        await runner.execute(None, task, candidates, [None]*2, dry_run=False, journal=journal)
    runner.send_once.assert_not_awaited()
    assert journal.data['days'] == {}


def test_workflow_safety_contract():
    root = Path(__file__).resolve().parents[1]
    data = yaml.load((root/'.github/workflows/send.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert data['on']['schedule'] == [{'cron': '30 7 * * *', 'timezone': 'Asia/Shanghai'}]
    assert data['on']['workflow_dispatch']['inputs']['dry_run']['default'] == 'true'
    assert data['on']['workflow_dispatch']['inputs']['send_once']['default'] == 'false'
    assert data['on']['workflow_dispatch']['inputs']['send_once']['type'] == 'boolean'
    assert data['on']['workflow_dispatch']['inputs']['refresh_session']['default'] == 'false'
    assert data['on']['workflow_dispatch']['inputs']['verify_target_updates'] == {
        'description': '只验证私密名单草稿与每日消息，不发信、不改变正式名单',
        'type': 'boolean', 'default': 'false'}
    run_step = next(step for step in data['jobs']['send']['steps'] if step.get('name') == 'Run safely')
    assert run_step['env']['ENABLE_REAL_SEND'] == "${{ github.event_name == 'workflow_dispatch' && inputs.send_once && 'true' || vars.ENABLE_REAL_SEND }}"
    assert '(inputs.dry_run || inputs.verify_target_updates)' in run_step['env']['REQUEST_DRY_RUN']
    assert data['concurrency']['cancel-in-progress'] == 'false'
    source = (root/'.github/workflows/send.yml').read_text(encoding='utf-8')
    assert 'upload-artifact@' not in source
    assert 'persist-credentials: false' in source
    assert 'secrets.' not in '\n'.join(s.get('run','') for s in data['jobs']['send']['steps'])


@pytest.mark.parametrize("dry,init,expected", [("true", "false", ["--dry-run"]),
    ("false", "false", ["--send"]), ("true", "true", ["--initialize-state"])])
def test_actions_dispatch(monkeypatch, dry, init, expected):
    monkeypatch.setenv("REQUEST_DRY_RUN", dry)
    monkeypatch.setenv("INITIALIZE_STATE", init)
    assert arguments() == expected


def test_init_and_send_rejected(monkeypatch):
    monkeypatch.setenv("REQUEST_DRY_RUN", "false")
    monkeypatch.setenv("INITIALIZE_STATE", "true")
    with pytest.raises(ValueError):
        arguments()

