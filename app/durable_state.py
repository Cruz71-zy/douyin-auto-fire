"""Fail-closed daily batch journal backed by GitHub Contents CAS updates."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
from datetime import date
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class StateError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the repository token to another endpoint.


class GitHubJournal:
    branch = "douyin-send-state"
    path = "daily-state.json"

    def __init__(self, repository: str, token: str, run_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
            raise StateError("缺少 GitHub 状态存储配置")
        if not re.fullmatch(r"[0-9]+", run_id):
            raise StateError("无效运行标识")
        self.root = f"https://api.github.com/repos/{repository}"
        self.token, self.run_id = token, run_id

    @classmethod
    def from_env(cls):
        return cls(os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GH_STATE_TOKEN", ""),
                   os.getenv("GITHUB_RUN_ID", ""))

    def _request(self, method, path, payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.root + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2026-03-10",
            "Cache-Control": "no-cache",
        })
        try:
            with build_opener(NoRedirect).open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            # Discard response bodies and request objects: they may contain secrets.
            raise StateError(f"状态接口 HTTP {exc.code}，停止发送") from None
        except Exception:
            raise StateError("状态接口结果不确定，停止发送") from None

    def load(self):
        response = self._request("GET", f"/contents/{self.path}?ref={self.branch}")
        try:
            data = json.loads(base64.b64decode(response["content"], validate=False))
            sha = response["sha"]
            if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40,64}", sha):
                raise ValueError
            if (not isinstance(data, dict) or set(data) != {"version", "days"}
                    or type(data["version"]) is not int or data["version"] != 1
                    or not isinstance(data["days"], dict)):
                raise ValueError
            for day, entry in data["days"].items():
                if (date.fromisoformat(day).isoformat() != day
                        or not isinstance(entry, dict) or set(entry) != {"status", "run_id"}
                        or entry["status"] not in {"reserved", "success", "unknown"}
                        or not isinstance(entry["run_id"], str) or not entry["run_id"].isdigit()):
                    raise ValueError
            return data, sha
        except Exception:
            raise StateError("状态记录损坏，停止发送；禁止自动重建") from None

    def _write(self, data, sha=None):
        payload = {"branch": self.branch, "message": "Update daily safety journal [skip ci]",
                   "content": base64.b64encode(json.dumps(data, sort_keys=True).encode()).decode()}
        if sha:
            payload["sha"] = sha
        self._request("PUT", f"/contents/{self.path}", payload)

    def initialize(self, source_sha):
        if not re.fullmatch(r"[a-f0-9]{40}", source_sha):
            raise StateError("无效初始提交")
        # POST fails if the branch already exists. No reset or overwrite path exists.
        self._request("POST", "/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": source_sha})
        self._write({"version": 1, "days": {}})
        self.load()

    def contains(self, day):
        data, _ = self.load()
        return day in data["days"]

    def reserve(self, day):
        data, sha = self.load()
        if day in data["days"]:
            return False
        updated = copy.deepcopy(data)
        updated["days"][day] = {"status": "reserved", "run_id": self.run_id}
        self._write(updated, sha)  # CAS conflict or timeout => no send, no retry.
        verified, _ = self.load()
        if verified != updated:
            raise StateError("预占状态无法确认，停止发送")
        return True

    def finish(self, day, status):
        if status not in {"success", "unknown"}:
            raise StateError("无效完成状态")
        data, sha = self.load()
        if data["days"].get(day) != {"status": "reserved", "run_id": self.run_id}:
            raise StateError("预占状态已变化，停止发送")
        data["days"][day]["status"] = status
        self._write(data, sha)
