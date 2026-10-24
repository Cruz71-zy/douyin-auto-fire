import base64
import copy
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest
from cryptography.fernet import Fernet

from app import session_store
from app.auth_state import normalize_storage_state
from app.durable_state import StateError
from app.session_store import EncryptedSessionStore


@pytest.fixture
def key():
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def state():
    return {
        "cookies": [{"name": "sessionid", "value": "PRIVATE_FAKE_COOKIE",
                     "domain": ".douyin.com", "secure": True, "httpOnly": True}],
        "origins": [{
            "origin": "https://www.douyin.com",
            "localStorage": [{"name": "auth", "value": "PRIVATE_FAKE_LOCAL_STORAGE"}],
            "indexedDB": [{"name": "auth", "version": 1,
                           "stores": [{"name": "tokens", "records": [
                               {"key": "current", "value": "PRIVATE_FAKE_IDB_TOKEN"}]}]}],
        }],
    }


class MemoryAPI:
    def __init__(self):
        self.encrypted = None
        self.revision = 1
        self.calls = []
        self.fail_put = False
        self.journal_exists = True
        self.journal = {"version": 1, "days": {}}

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path.startswith("/contents/daily-state.json?"):
            if not self.journal_exists:
                raise session_store._NotFound("simulated 404")
            return {"sha": "1" * 40, "encoding": "base64", "content":
                    base64.b64encode(json.dumps(self.journal).encode()).decode()}
        if method == "GET" and path.startswith("/contents/auth-state.enc?"):
            if self.encrypted is None:
                raise session_store._NotFound("simulated 404")
            # GitHub Contents wraps base64 across lines.
            return {"sha": f"{self.revision:040x}", "encoding": "base64", "content":
                    base64.encodebytes(self.encrypted).decode("ascii")}
        if method == "PUT" and path == "/contents/auth-state.enc":
            expected = None if self.encrypted is None else f"{self.revision:040x}"
            if self.fail_put or payload.get("sha") != expected:
                raise StateError("simulated CAS conflict")
            assert payload["branch"] == "douyin-send-state"
            self.encrypted = base64.b64decode(payload["content"], validate=True)
            self.revision += 1
            return {"content": {"sha": f"{self.revision:040x}"}}
        raise AssertionError("Unexpected mocked API operation")


@pytest.fixture
def store(key, monkeypatch):
    result = EncryptedSessionStore("owner/repository", "FAKE_GITHUB_RUN_TOKEN", "123", key)
    api = MemoryAPI()
    monkeypatch.setattr(result, "_request", api.request)
    return result, api


def test_only_ciphertext_is_stored_and_full_auth_state_round_trips(store, key, state, capsys):
    current, api = store
    original = copy.deepcopy(state)
    current.save(state)
    assert b"PRIVATE_" not in api.encrypted
    assert b"sessionid" not in api.encrypted
    serialized_requests = json.dumps(api.calls)
    assert "PRIVATE_" not in serialized_requests
    envelope = json.loads(Fernet(key.encode()).decrypt(api.encrypted))
    assert envelope["state"] == normalize_storage_state(state)
    assert current.load() == normalize_storage_state(state)
    assert state == original
    assert capsys.readouterr().out == ""
    assert all(method != "POST" for method, _, _ in api.calls)
    assert all(path != "/contents/daily-state.json" for _, path, _ in api.calls)


def test_explicit_missing_ciphertext_allows_bootstrap_only_with_existing_journal(store):
    current, api = store
    assert current.load() is None
    assert [method for method, _, _ in api.calls] == ["GET", "GET"]
    assert "daily-state.json" in api.calls[-1][1]


@pytest.mark.parametrize("journal", [None, {}, {"version": 2, "days": {}}])
def test_missing_or_corrupt_journal_never_allows_bootstrap(store, journal, state):
    current, api = store
    api.journal_exists = journal is not None
    api.journal = journal
    with pytest.raises(StateError):
        current.load()
    with pytest.raises(StateError):
        current.save(state)
    assert all(method == "GET" for method, _, _ in api.calls)


def test_existing_ciphertext_is_updated_with_current_blob_sha(store, state):
    current, api = store
    current.save(state)
    prior_sha = f"{api.revision:040x}"
    state["cookies"][0]["value"] = "PRIVATE_REFRESHED_COOKIE"
    current.save(state)
    assert api.calls[-1][2]["sha"] == prior_sha
    assert current.load()["cookies"][0]["value"] == "PRIVATE_REFRESHED_COOKIE"


def test_cas_conflict_is_not_retried(store, state):
    current, api = store
    current.save(state)
    previous_ciphertext = api.encrypted
    api.calls.clear()
    api.fail_put = True
    with pytest.raises(StateError):
        current.save(state)
    assert sum(method == "PUT" for method, _, _ in api.calls) == 1
    assert api.encrypted == previous_ciphertext


def test_session_changed_since_load_is_never_overwritten(store, state):
    current, api = store
    current.save(state)
    current.load()
    api.revision += 1
    api.calls.clear()
    with pytest.raises(StateError):
        current.save(state)
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("ciphertext", [b"PRIVATE_PLAINTEXT_NOT_ENCRYPTED", b"", b"gAAAA-invalid"])
def test_invalid_ciphertext_fails_closed_without_bootstrap_or_overwrite(store, state, ciphertext):
    current, api = store
    api.encrypted = ciphertext
    with pytest.raises(StateError) as error:
        current.load()
    assert "PRIVATE_" not in str(error.value)
    with pytest.raises(StateError):
        current.save(state)
    assert all(method == "GET" and "auth-state.enc" in path for method, path, _ in api.calls)


def test_wrong_key_fails_closed(store, state, monkeypatch):
    current, api = store
    current.save(state)
    wrong = EncryptedSessionStore("owner/repository", "FAKE_GITHUB_RUN_TOKEN", "456",
                                  Fernet.generate_key().decode())
    monkeypatch.setattr(wrong, "_request", api.request)
    api.calls.clear()
    with pytest.raises(StateError):
        wrong.load()
    with pytest.raises(StateError):
        wrong.save(state)
    assert all(method == "GET" for method, _, _ in api.calls)


@pytest.mark.parametrize("envelope", [
    {"version": 2, "repository": "owner/repository", "state": {}},
    {"version": 1, "repository": "another/repository", "state": {}},
    {"version": 1, "repository": "owner/repository", "state": {"cookies": []}},
])
def test_authenticated_but_invalid_envelope_is_rejected(store, key, envelope):
    current, api = store
    api.encrypted = Fernet(key.encode()).encrypt(json.dumps(envelope).encode())
    with pytest.raises(StateError):
        current.load()
    assert len(api.calls) == 1


@pytest.mark.parametrize("bad_key", ["", "PRIVATE_INVALID_KEY", "a" * 44, "é" * 44])
def test_invalid_key_is_rejected_without_private_error_values(bad_key):
    with pytest.raises(StateError) as error:
        EncryptedSessionStore("owner/repository", "FAKE_GITHUB_RUN_TOKEN", "123", bad_key)
    assert "PRIVATE_" not in str(error.value)


def test_key_is_optional_only_when_environment_variable_is_absent(monkeypatch, key):
    monkeypatch.delenv("DOUYIN_SESSION_KEY", raising=False)
    assert EncryptedSessionStore.from_env() is None
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    monkeypatch.setenv("GH_STATE_TOKEN", "FAKE_GITHUB_RUN_TOKEN")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("DOUYIN_SESSION_KEY", key)
    assert isinstance(EncryptedSessionStore.from_env(), EncryptedSessionStore)
    monkeypatch.setenv("DOUYIN_SESSION_KEY", "")
    with pytest.raises(StateError):
        EncryptedSessionStore.from_env()


@pytest.mark.parametrize("code", [401, 403, 409, 429, 500])
def test_http_failures_are_fixed_errors_and_never_retried(monkeypatch, key, code):
    attempts = []

    def fail_open(request, timeout):
        attempts.append(request)
        assert request.full_url.startswith("https://api.github.com/repos/owner/repository/")
        assert timeout == 30
        raise HTTPError(request.full_url, code, "PRIVATE_ERROR_BODY", None, None)

    handlers = []

    def opener_factory(handler):
        handlers.append(handler)
        return SimpleNamespace(open=fail_open)

    monkeypatch.setattr(session_store, "build_opener", opener_factory)
    current = EncryptedSessionStore("owner/repository", "FAKE_GITHUB_RUN_TOKEN", "123", key)
    with pytest.raises(StateError) as error:
        current.load()
    assert "PRIVATE_" not in str(error.value)
    assert str(code) in str(error.value)
    assert len(attempts) == 1
    assert handlers == [session_store.NoRedirect]


def test_only_http_404_triggers_existing_journal_confirmation(monkeypatch, key):
    calls = []

    def not_found(request, timeout):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, 404, "PRIVATE_NOT_FOUND", None, None)

    monkeypatch.setattr(session_store, "build_opener", lambda _: SimpleNamespace(open=not_found))
    current = EncryptedSessionStore("owner/repository", "FAKE_GITHUB_RUN_TOKEN", "123", key)
    with pytest.raises(StateError) as error:
        current.load()
    assert "PRIVATE_" not in str(error.value)
    assert len(calls) == 2
    assert "auth-state.enc" in calls[0] and "daily-state.json" in calls[1]


def test_network_uncertainty_does_not_trigger_bootstrap(monkeypatch, key):
    attempts = []

    def offline(request, timeout):
        attempts.append(request)
        raise URLError("PRIVATE_NETWORK_DETAIL")

    monkeypatch.setattr(session_store, "build_opener", lambda _: SimpleNamespace(open=offline))
    current = EncryptedSessionStore("owner/repository", "FAKE_GITHUB_RUN_TOKEN", "123", key)
    with pytest.raises(StateError) as error:
        current.load()
    assert "PRIVATE_" not in str(error.value)
    assert len(attempts) == 1


def test_unconfirmed_write_ack_is_not_retried(store, state, monkeypatch):
    current, api = store
    original = api.request

    def missing_ack(method, path, payload=None):
        response = original(method, path, payload)
        return {} if method == "PUT" else response

    monkeypatch.setattr(current, "_request", missing_ack)
    with pytest.raises(StateError):
        current.save(state)
    assert sum(method == "PUT" for method, _, _ in api.calls) == 1
