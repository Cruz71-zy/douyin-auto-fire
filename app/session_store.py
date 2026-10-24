"""Encrypted browser sessions on the existing safety-state branch.

Only ciphertext enters GitHub Contents. The encryption key stays in a Secret,
and the API uses the run's short-lived GH_STATE_TOKEN. No initialization or
retry path can create, reset, or bypass the daily safety journal.
"""
from __future__ import annotations

import base64
import json
import os
import re
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from app.auth_state import normalize_storage_state
from app.durable_state import GitHubJournal, NoRedirect, StateError


class _NotFound(StateError):
    """An explicit HTTP 404, distinct from an uncertain API failure."""


def _valid_sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is not None


class EncryptedSessionStore:
    branch = GitHubJournal.branch
    path = "auth-state.enc"

    def __init__(self, repository: str, token: str, run_id: str, key: str):
        config = GitHubJournal(repository, token, run_id)
        self.root, self.token, self.run_id = config.root, config.token, config.run_id
        self.repository = repository
        self._revision_read = False
        self._revision = None
        try:
            from cryptography.fernet import Fernet

            if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}=", key) is None:
                raise ValueError
            self._fernet = Fernet(key.encode("ascii"))
        except Exception:
            raise StateError("会话加密密钥无效或加密依赖不可用，停止任务") from None

    @classmethod
    def from_env(cls):
        key = os.getenv("DOUYIN_SESSION_KEY")
        if key is None:
            return None
        return cls(os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GH_STATE_TOKEN", ""),
                   os.getenv("GITHUB_RUN_ID", ""), key)

    def _request(self, method, path, payload=None):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.root + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
            "Cache-Control": "no-cache",
        })
        try:
            with build_opener(NoRedirect).open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 404:
                raise _NotFound("会话接口 HTTP 404") from None
            raise StateError(f"会话接口 HTTP {exc.code}，停止任务") from None
        except Exception:
            raise StateError("会话接口结果不确定，停止任务") from None

    def _existing_journal_required(self):
        # A content 404 can also mean an inaccessible repository or missing
        # branch. Confirm the existing, valid journal before allowing bootstrap.
        journal = GitHubJournal(self.repository, self.token, self.run_id)
        journal._request = self._request
        journal.load()

    def _read_record(self):
        try:
            response = self._request("GET", f"/contents/{self.path}?ref={self.branch}")
        except _NotFound:
            self._existing_journal_required()
            return None, None
        try:
            if not isinstance(response, dict) or response.get("encoding") != "base64":
                raise ValueError
            sha = response["sha"]
            content = response["content"]
            if not _valid_sha(sha) or not isinstance(content, str):
                raise ValueError
            encrypted = base64.b64decode(content.replace("\n", "").replace("\r", ""), validate=True)
            envelope = json.loads(self._fernet.decrypt(encrypted))
            if (not isinstance(envelope, dict)
                    or set(envelope) != {"version", "repository", "state"}
                    or type(envelope["version"]) is not int or envelope["version"] != 1
                    or envelope["repository"] != self.repository):
                raise ValueError
            return normalize_storage_state(envelope["state"]), sha
        except Exception:
            raise StateError("加密会话损坏、密钥不匹配或格式无效，停止任务；禁止自动覆盖") from None

    def load(self) -> dict | None:
        state, sha = self._read_record()
        self._revision_read, self._revision = True, sha
        return state

    def save(self, state: dict) -> None:
        try:
            normalized = normalize_storage_state(state)
            envelope = {"version": 1, "repository": self.repository, "state": normalized}
            encrypted = self._fernet.encrypt(json.dumps(envelope, ensure_ascii=False,
                                                       allow_nan=False).encode("utf-8"))
        except Exception:
            raise StateError("待保存的会话状态无效，停止任务") from None
        _, sha = self._read_record()
        if self._revision_read and sha != self._revision:
            raise StateError("会话版本已变化，停止任务；禁止自动重试覆盖")
        payload = {
            "branch": self.branch,
            "message": "Update encrypted browser session [skip ci]",
            "content": base64.b64encode(encrypted).decode("ascii"),
        }
        if sha is not None:
            payload["sha"] = sha
        response = self._request("PUT", f"/contents/{self.path}", payload)
        try:
            updated_sha = response["content"]["sha"]
            if not _valid_sha(updated_sha):
                raise ValueError
        except Exception:
            raise StateError("会话写入结果无法确认，停止任务；禁止自动重试") from None
        self._revision_read, self._revision = True, updated_sha
