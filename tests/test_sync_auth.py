import base64
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest
from cryptography.fernet import Fernet
from nacl.public import PrivateKey, SealedBox

from scripts import sync_auth


@pytest.fixture
def large_state():
    return {
        "cookies": [{"name": "sessionid", "value": "PRIVATE_FAKE_COOKIE", "domain": ".douyin.com"}],
        "origins": [{
            "origin": "https://www.douyin.com",
            "localStorage": [{"name": "auth", "value": "PRIVATE_FAKE_LOCAL_" + "x" * 70_000}],
            "indexedDB": [{"name": "auth", "version": 1, "stores": [
                {"name": "tokens", "records": [{"key": "current", "value": "PRIVATE_FAKE_IDB"}]}]}],
        }],
    }


class MemorySyncAPI:
    repository = "owner/repository"
    token = "FAKE_LOCAL_GCM_CREDENTIAL"

    def __init__(self):
        self.private = PrivateKey.generate()
        self.calls = []
        self.ciphertext = None
        self.secrets = {}
        self.revision = 1
        self.push = True
        self.journal = {"version": 1, "days": {"2026-10-24": {"status": "success", "run_id": "99"}}}
        self.active = {"in_progress": [], "queued": []}
        self.fail_cas = False
        self.fail_secret = False
        self.bad_ack = False
        self.run_reads = 0
        self.start_during_preparation = False

    def request(self, method, path, payload=None, *, allow_missing=False):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == "":
            return {"permissions": {"push": self.push}, "full_name": self.repository, "default_branch": "main"}
        if method == "GET" and path.startswith("/contents/daily-state.json?"):
            if self.journal is None:
                raise sync_auth.SyncError("simulated unavailable journal")
            return {"sha": "a" * 40, "content": base64.b64encode(json.dumps(self.journal).encode()).decode()}
        if method == "GET" and path.startswith("/actions/runs?"):
            self.run_reads += 1
            if self.start_during_preparation and self.run_reads == 4:
                return {"workflow_runs": [{"name": "Safe Douyin Daily"}]}
            status = "in_progress" if "status=in_progress" in path else "queued"
            return {"workflow_runs": self.active[status]}
        if method == "GET" and path == "/actions/secrets/public-key":
            return {"key_id": "test-public-key", "key": base64.b64encode(bytes(self.private.public_key)).decode()}
        if method == "GET" and path.startswith("/contents/auth-state.enc?"):
            if self.ciphertext is None:
                assert allow_missing is True
                return None
            return {"sha": f"{self.revision:040x}"}
        if method == "PUT" and path == "/contents/auth-state.enc":
            expected = None if self.ciphertext is None else f"{self.revision:040x}"
            if self.fail_cas or payload.get("sha") != expected:
                raise sync_auth.SyncError("simulated CAS conflict")
            assert payload["branch"] == "douyin-send-state"
            self.ciphertext = base64.b64decode(payload["content"], validate=True)
            self.revision += 1
            return {} if self.bad_ack else {"content": {"sha": f"{self.revision:040x}"}}
        if method == "PUT" and path.startswith("/actions/secrets/"):
            if self.fail_secret:
                raise sync_auth.SyncError("simulated Secret write failure")
            assert payload["key_id"] == "test-public-key"
            self.secrets[path.rsplit("/", 1)[1]] = payload["encrypted_value"]
            return None
        if method == "POST" and path == "/actions/workflows/send.yml/dispatches":
            return None
        raise AssertionError("Unexpected mocked API operation")

    def decrypt_secret(self, name):
        return SealedBox(self.private).decrypt(base64.b64decode(self.secrets[name]))


def mutations(api):
    return [call for call in api.calls if call[0] != "GET"]


def test_large_complete_state_goes_only_into_ciphertext_and_small_bootstrap_secret(large_state, capsys):
    api = MemorySyncAPI()
    old_journal = copy.deepcopy(api.journal)
    sync_auth.synchronize(api, large_state)
    assert len(json.dumps(large_state).encode()) > sync_auth.SECRET_LIMIT_BYTES
    key = api.decrypt_secret("DOUYIN_SESSION_KEY")
    bootstrap = api.decrypt_secret("DOUYIN_STORAGE_STATE")
    assert len(bootstrap) < sync_auth.SECRET_LIMIT_BYTES
    assert json.loads(bootstrap)["origins"] == []
    assert json.loads(bootstrap)["cookies"][0]["value"] == "PRIVATE_FAKE_COOKIE"
    envelope = json.loads(Fernet(key).decrypt(api.ciphertext))
    assert envelope["version"] == 1 and envelope["repository"] == api.repository
    assert envelope["state"]["origins"] == large_state["origins"]
    requests = json.dumps(api.calls)
    assert "PRIVATE_" not in requests
    assert key.decode() not in requests
    assert b"PRIVATE_" not in api.ciphertext
    assert api.journal == old_journal
    assert all("daily-state.json" not in path and "/variables/" not in path
               for _, path, _ in mutations(api))
    assert [method for method, _, _ in mutations(api)] == ["PUT", "PUT", "PUT"]
    assert capsys.readouterr().out == ""


def test_repeated_explicit_sync_rotates_key_and_uses_ciphertext_cas(large_state):
    api = MemorySyncAPI()
    sync_auth.synchronize(api, large_state)
    previous_key = api.decrypt_secret("DOUYIN_SESSION_KEY")
    previous_sha = f"{api.revision:040x}"
    sync_auth.synchronize(api, large_state)
    assert api.decrypt_secret("DOUYIN_SESSION_KEY") != previous_key
    puts = [payload for method, path, payload in api.calls
            if method == "PUT" and path == "/contents/auth-state.enc"]
    assert puts[-1]["sha"] == previous_sha
    assert not any(method == "GET" and path == "/actions/secrets/DOUYIN_SESSION_KEY"
                   for method, path, _ in api.calls)


def test_verify_dispatch_is_explicitly_a_nonrefreshing_dry_run(large_state):
    api = MemorySyncAPI()
    sync_auth.synchronize(api, large_state, verify=True)
    assert api.calls[-1] == ("POST", "/actions/workflows/send.yml/dispatches", {
        "ref": "main", "inputs": {"dry_run": True, "send_once": False, "refresh_session": False},
    })
    assert len(api.secrets) == 2


@pytest.mark.parametrize("status", ["in_progress", "queued"])
def test_active_daily_workflow_blocks_every_write(large_state, status):
    api = MemorySyncAPI()
    api.active[status] = [{"name": "Safe Douyin Daily", "path": ".github/workflows/send.yml"}]
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state, verify=True)
    assert mutations(api) == []


def test_workflow_renamed_but_same_path_still_blocks_sync(large_state):
    api = MemorySyncAPI()
    api.active["queued"] = [{"name": "Renamed", "path": ".github/workflows/send.yml@refs/heads/main"}]
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state)
    assert mutations(api) == []


def test_workflow_starting_during_preparation_blocks_first_write(large_state):
    api = MemorySyncAPI()
    api.start_during_preparation = True
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state)
    assert mutations(api) == []


@pytest.mark.parametrize("journal", [None, {}, {"version": 2, "days": {}}])
def test_missing_or_corrupt_journal_is_never_rebuilt_or_modified(large_state, journal):
    api = MemorySyncAPI()
    api.journal = journal
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state)
    assert mutations(api) == []


def test_missing_push_permission_blocks_every_write(large_state):
    api = MemorySyncAPI()
    api.push = False
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state)
    assert len(api.calls) == 1 and mutations(api) == []


def test_invalid_state_or_oversized_cookie_bootstrap_cannot_write(large_state):
    for state in ({"cookies": []}, {**large_state, "cookies": [
        {"name": "sessionid", "value": "x" * 50_000, "domain": ".douyin.com"}]}):
        api = MemorySyncAPI()
        with pytest.raises(sync_auth.SyncError):
            sync_auth.synchronize(api, state)
        assert mutations(api) == []


def test_cas_conflict_stops_before_secret_updates_and_is_not_retried(large_state):
    api = MemorySyncAPI()
    api.fail_cas = True
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state, verify=True)
    assert len(mutations(api)) == 1
    assert api.secrets == {}


def test_unconfirmed_ciphertext_write_does_not_update_secrets_or_dispatch(large_state):
    api = MemorySyncAPI()
    api.bad_ack = True
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state, verify=True)
    assert len(mutations(api)) == 1
    assert api.secrets == {}


def test_partial_secret_failure_stops_without_dispatch_or_automatic_retry(large_state):
    api = MemorySyncAPI()
    api.fail_secret = True
    with pytest.raises(sync_auth.SyncError):
        sync_auth.synchronize(api, large_state, verify=True)
    assert [method for method, _, _ in mutations(api)] == ["PUT", "PUT"]


def test_git_credential_fill_is_noninteractive_and_stdout_stays_in_memory(monkeypatch, capsys):
    run = MagicMock(return_value=SimpleNamespace(
        returncode=0, stdout="protocol=https\nusername=owner\npassword=PRIVATE_FAKE_GCM_TOKEN\n",
        stderr="PRIVATE_HELPER_DIAGNOSTIC",
    ))
    monkeypatch.setattr(sync_auth.subprocess, "run", run)
    assert sync_auth.github_credential("owner/repository") == "PRIVATE_FAKE_GCM_TOKEN"
    args, kwargs = run.call_args
    assert args == (["git", "credential", "fill"],)
    assert "username=owner\n" in kwargs["input"]
    assert kwargs["capture_output"] is True and kwargs["check"] is False
    assert kwargs["env"]["GCM_INTERACTIVE"] == "never"
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "PRIVATE_" not in capsys.readouterr().out


def test_git_failure_does_not_expose_helper_output(monkeypatch):
    monkeypatch.setattr(sync_auth.subprocess, "run", MagicMock(return_value=SimpleNamespace(
        returncode=1, stdout="password=PRIVATE_FAKE_TOKEN", stderr="PRIVATE_DIAGNOSTIC",
    )))
    with pytest.raises(sync_auth.SyncError) as error:
        sync_auth.github_credential("owner/repository")
    assert "PRIVATE_" not in str(error.value)


@pytest.mark.parametrize("code,allow_missing,method,is_missing", [
    (404, True, "GET", True), (404, False, "GET", False),
    (404, True, "PUT", False), (403, True, "GET", False), (409, False, "PUT", False),
])
def test_api_only_treats_explicit_allowed_get_404_as_absence(monkeypatch, code, allow_missing, method, is_missing):
    attempts = []

    def fail_open(request, timeout):
        attempts.append(request)
        assert request.full_url.startswith("https://api.github.com/repos/owner/repository")
        assert timeout == 30
        raise HTTPError(request.full_url, code, "PRIVATE_API_BODY", None, None)

    opener = MagicMock(return_value=SimpleNamespace(open=fail_open))
    monkeypatch.setattr(sync_auth, "build_opener", opener)
    api = sync_auth.GitHubAPI("owner/repository", "PRIVATE_FAKE_TOKEN")
    if is_missing:
        assert api.request(method, "/contents/auth-state.enc", allow_missing=allow_missing) is None
    else:
        with pytest.raises(sync_auth.SyncError) as error:
            api.request(method, "/contents/auth-state.enc", allow_missing=allow_missing)
        assert "PRIVATE_" not in str(error.value)
    opener.assert_called_once_with(sync_auth.NoRedirect)
    assert len(attempts) == 1


def test_api_handles_empty_success_and_network_error_without_output(monkeypatch):
    monkeypatch.setattr(sync_auth, "build_opener", lambda _: SimpleNamespace(open=lambda *a, **k: io.BytesIO(b"")))
    api = sync_auth.GitHubAPI("owner/repository", "PRIVATE_FAKE_TOKEN")
    assert api.request("PUT", "/actions/secrets/DOUYIN_SESSION_KEY", {}) is None

    def offline(*a, **k):
        raise URLError("PRIVATE_NETWORK_DETAIL")

    monkeypatch.setattr(sync_auth, "build_opener", lambda _: SimpleNamespace(open=offline))
    with pytest.raises(sync_auth.SyncError) as error:
        api.request("GET", "")
    assert "PRIVATE_" not in str(error.value)


def test_cli_login_verify_uses_local_login_then_safe_sync(monkeypatch, tmp_path, large_state, capsys):
    fake_file = tmp_path / "fake-storage-state.json"
    fake_file.write_text(json.dumps(large_state), encoding="utf-8")
    api = MemorySyncAPI()
    monkeypatch.setattr(sync_auth, "github_credential", lambda repo: "FAKE_LOCAL_CREDENTIAL")
    monkeypatch.setattr(sync_auth, "GitHubAPI", lambda repo, token: api)
    marker = object()
    fake_login = MagicMock(return_value=marker)
    fake_run = MagicMock()
    monkeypatch.setattr(sync_auth, "login", fake_login)
    monkeypatch.setattr(sync_auth.asyncio, "run", fake_run)
    assert sync_auth.main(["--repo", api.repository, "--state", str(fake_file), "--login", "--verify"]) == 0
    fake_login.assert_called_once_with(fake_file)
    fake_run.assert_called_once_with(marker)
    assert api.calls[-1][0] == "POST"
    assert "PRIVATE_" not in capsys.readouterr().out
