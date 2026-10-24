import copy
import json
from pathlib import Path

import pytest

from app import auth_state
from app import safe_main
from app.config import ConfigError


@pytest.fixture
def complete_state():
    return {
        "cookies": [{
            "name": "sessionid",
            "value": "PRIVATE_COOKIE_VALUE",
            "domain": ".douyin.com",
            "path": "/",
            "expirationDate": 2_000_000_000,
            "httpOnly": True,
            "secure": True,
            "sameSite": "no_restriction",
        }],
        "origins": [{
            "origin": "https://www.douyin.com",
            "localStorage": [{"name": "login", "value": "PRIVATE_LOCAL_VALUE"}],
            "indexedDB": [{
                "name": "auth-db",
                "version": 1,
                "stores": [{
                    "name": "tokens",
                    "autoIncrement": False,
                    "records": [{"key": "current", "value": "PRIVATE_IDB_VALUE"}],
                }],
            }],
        }],
    }


def test_complete_state_preserves_browser_authentication_data(complete_state):
    original = copy.deepcopy(complete_state)
    state = auth_state.normalize_storage_state(complete_state)
    assert state["cookies"][0]["value"] == "PRIVATE_COOKIE_VALUE"
    assert state["cookies"][0]["expires"] == 2_000_000_000
    assert state["cookies"][0]["sameSite"] == "None"
    assert state["origins"] == original["origins"]
    assert complete_state == original


@pytest.mark.parametrize("domain", [
    "example.com", ".douyin.com.example.com", "evildouyin.com", "douyin.com/",
])
def test_cookie_domain_must_belong_to_douyin(complete_state, domain):
    complete_state["cookies"][0]["domain"] = domain
    with pytest.raises(ConfigError) as error:
        auth_state.normalize_storage_state(complete_state)
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("origin", [
    "http://www.douyin.com",
    "https://douyin.com.example.com",
    "https://evildouyin.com",
    "https://www.douyin.com/",
    "https://www.douyin.com/chat",
    "https://www.douyin.com?PRIVATE_QUERY_VALUE",
    "https://www.douyin.com#PRIVATE_FRAGMENT_VALUE",
    "https://PRIVATE_USERNAME:PRIVATE_PASSWORD@www.douyin.com",
    "https://www.douyin.com:443",
])
def test_origin_must_be_an_exact_secure_douyin_origin(complete_state, origin):
    complete_state["origins"][0]["origin"] = origin
    with pytest.raises(ConfigError) as error:
        auth_state.normalize_storage_state(complete_state)
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("raw", [
    None, [], {}, {"cookies": [], "origins": []},
    {"cookies": "PRIVATE_INVALID_COOKIE", "origins": []},
])
def test_state_requires_a_nonempty_cookie_array(raw):
    with pytest.raises(ConfigError) as error:
        auth_state.normalize_storage_state(raw)
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("origins", "PRIVATE_INVALID_ORIGINS"),
    ("localStorage", {}),
    ("localStorage", [{"name": "PRIVATE_KEY", "value": 123}]),
    ("localStorage", [{"name": 123, "value": "PRIVATE_VALUE"}]),
    ("indexedDB", {}),
])
def test_invalid_browser_storage_is_rejected(complete_state, field, value):
    if field == "origins":
        complete_state[field] = value
    else:
        complete_state["origins"][0][field] = value
    with pytest.raises(ConfigError) as error:
        auth_state.normalize_storage_state(complete_state)
    assert "PRIVATE_" not in str(error.value)


def test_storage_write_replaces_the_complete_file_without_temporary_residue(tmp_path, complete_state):
    destination = tmp_path / "storage-state.json"
    destination.write_text("old login state", encoding="utf-8")
    state = auth_state.normalize_storage_state(complete_state)
    auth_state.write_storage_state(destination, state)
    assert json.loads(destination.read_text(encoding="utf-8")) == state
    assert set(tmp_path.iterdir()) == {destination}


def test_failed_atomic_replace_preserves_previous_login_and_cleans_temp(
    monkeypatch, tmp_path, complete_state,
):
    destination = tmp_path / "storage-state.json"
    old_content = b"previous-auth-state-must-survive"
    destination.write_bytes(old_content)
    attempts = []

    def fail_replace(source, target):
        source, target = Path(source), Path(target)
        assert source.parent == destination.parent
        assert source != destination
        assert json.loads(source.read_text(encoding="utf-8"))["cookies"]
        assert target == destination
        attempts.append(source)
        raise PermissionError("simulated replacement failure")

    monkeypatch.setattr(auth_state.os, "replace", fail_replace)
    with pytest.raises(OSError):
        auth_state.write_storage_state(destination, complete_state)
    assert len(attempts) == 1
    assert destination.read_bytes() == old_content
    assert set(tmp_path.iterdir()) == {destination}


@pytest.fixture
def safe_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key in (
        "DOUYIN_STORAGE_STATE", "DOUYIN_COOKIE", "DINGTALK_WEBHOOK", "DINGTALK_SECRET",
        "WEBHOOK_URL", "WEBHOOK_HEADERS", "WEBHOOK_TEMPLATE", "TASK_CONFIG",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DOUYIN_CONFIG", json.dumps({
        "friends": ["Test friend"],
        "messages": [{"type": "text", "value": "hello"}],
    }))
    return monkeypatch


def test_safe_runner_accepts_complete_storage_without_cookie(safe_environment, complete_state):
    safe_environment.setenv("DOUYIN_STORAGE_STATE", json.dumps(complete_state))
    settings, task, candidates, uids = safe_main.load_safe_config()
    assert json.loads(settings.storage_state) == auth_state.normalize_storage_state(complete_state)
    assert len(task.targets) == len(candidates) == len(uids) == 1
    assert settings.trace is False


def test_complete_storage_takes_precedence_over_old_cookie(safe_environment, complete_state):
    safe_environment.setenv("DOUYIN_STORAGE_STATE", json.dumps(complete_state))
    safe_environment.setenv("DOUYIN_COOKIE", "PRIVATE_INVALID_LEGACY_COOKIE")
    settings, *_ = safe_main.load_safe_config()
    assert json.loads(settings.storage_state) == auth_state.normalize_storage_state(complete_state)
    assert settings.cookie is None


@pytest.mark.parametrize("state", ["PRIVATE_INVALID_JSON", '{"cookies": [], "origins": []}'])
def test_bad_explicit_storage_never_falls_back_to_legacy_cookie(safe_environment, state):
    safe_environment.setenv("DOUYIN_STORAGE_STATE", state)
    safe_environment.setenv("DOUYIN_COOKIE", json.dumps([{
        "name": "sessionid", "value": "PRIVATE_LEGACY_COOKIE", "domain": ".douyin.com",
    }]))
    with pytest.raises(ConfigError) as error:
        safe_main.load_safe_config()
    assert "PRIVATE_" not in str(error.value)
