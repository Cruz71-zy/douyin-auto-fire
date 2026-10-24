"""Explicit local login synchronization; never dispatch a message send.

The complete state is encrypted before GitHub storage. The bootstrap Secret
contains cookies only, so large origin storage never hits the Secret size limit.
Each explicit synchronization rotates the Fernet key together with its ciphertext.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cryptography.fernet import Fernet
from nacl.public import PublicKey, SealedBox

from app.auth_state import normalize_storage_state
from app.durable_state import GitHubJournal, NoRedirect
from scripts.login import login


DEFAULT_REPOSITORY = "Cruz71-zy/douyin-auto-fire"
SECRET_LIMIT_BYTES = 48 * 1024
STATE_BRANCH = GitHubJournal.branch
ENCRYPTED_PATH = "auth-state.enc"


class SyncError(RuntimeError):
    pass


def _valid_repository(repository):
    return isinstance(repository, str) and re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository,
    ) is not None


def _valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is not None


def github_credential(repository: str) -> str:
    """Capture GCM output in memory; never prompt, print, or persist the token."""
    if not _valid_repository(repository):
        raise SyncError("仓库名称无效")
    owner = repository.split("/", 1)[0]
    environment = dict(os.environ, GCM_INTERACTIVE="never", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input=f"protocol=https\nhost=github.com\nusername={owner}\n\n",
            text=True, capture_output=True, timeout=30, check=False, env=environment,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        token = values.get("password")
        if result.returncode != 0 or not isinstance(token, str) or not token:
            raise ValueError
        return token
    except Exception:
        raise SyncError("无法读取本机已授权的 GitHub 凭据；请先完成 Git Credential Manager 登录") from None


class GitHubAPI:
    def __init__(self, repository: str, token: str):
        if not _valid_repository(repository) or not token:
            raise SyncError("GitHub 同步配置无效")
        self.repository, self.token = repository, token
        self.root = f"https://api.github.com/repos/{repository}"

    def request(self, method, path, payload=None, *, allow_missing=False):
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
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            if allow_missing and method == "GET" and exc.code == 404:
                return None
            raise SyncError(f"GitHub 同步接口 HTTP {exc.code}；已停止，不自动重试") from None
        except Exception:
            raise SyncError("GitHub 同步接口结果不确定；已停止，不自动重试") from None


def ensure_idle(api):
    for status in ("in_progress", "queued"):
        for page in range(1, 101):
            result = api.request("GET", f"/actions/runs?status={status}&per_page=100&page={page}")
            if not isinstance(result, dict) or not isinstance(result.get("workflow_runs"), list):
                raise SyncError("无法确认工作流空闲；停止同步")
            runs = result["workflow_runs"]
            for run in runs:
                if not isinstance(run, dict):
                    raise SyncError("工作流状态格式无效；停止同步")
                path = run.get("path", "")
                if (run.get("name") == "Safe Douyin Daily"
                        or isinstance(path, str) and path.split("@", 1)[0] == ".github/workflows/send.yml"):
                    raise SyncError("Safe Douyin Daily 正在运行或排队；请待其结束后重新同步")
            if len(runs) < 100:
                break
        else:
            raise SyncError("活动工作流分页过多，无法确认空闲；停止同步")


def preflight(api):
    repo = api.request("GET", "")
    if (not isinstance(repo, dict) or not isinstance(repo.get("permissions"), dict)
            or repo["permissions"].get("push") is not True
            or not isinstance(repo.get("full_name"), str)
            or repo["full_name"].casefold() != api.repository.casefold()
            or not isinstance(repo.get("default_branch"), str) or not repo["default_branch"]):
        raise SyncError("未确认目标仓库写权限和默认分支；停止同步")
    try:
        journal = GitHubJournal(api.repository, api.token, "0")
        journal._request = api.request
        journal.load()
    except Exception:
        raise SyncError("现有发送日记不可用；停止同步，不创建或重建日记") from None
    ensure_idle(api)
    return repo


def synchronize(api, state, *, verify=False):
    repo = preflight(api)
    try:
        normalized = normalize_storage_state(state)
        bootstrap = json.dumps({"cookies": normalized["cookies"], "origins": []},
                               ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(bootstrap) >= SECRET_LIMIT_BYTES:
            raise SyncError("Cookie 引导状态也超过 Secret 容量；未写入任何配置")
        public = api.request("GET", "/actions/secrets/public-key")
        if (not isinstance(public, dict) or not isinstance(public.get("key_id"), str)
                or not public["key_id"] or not isinstance(public.get("key"), str)):
            raise ValueError
        box = SealedBox(PublicKey(base64.b64decode(public["key"], validate=True)))
        key = Fernet.generate_key()
        envelope = {"version": 1, "repository": repo["full_name"], "state": normalized}
        encrypted = Fernet(key).encrypt(json.dumps(envelope, ensure_ascii=False,
                                                 allow_nan=False).encode("utf-8"))
        sealed_key = base64.b64encode(box.encrypt(key)).decode("ascii")
        sealed_bootstrap = base64.b64encode(box.encrypt(bootstrap)).decode("ascii")
    except SyncError:
        raise
    except Exception:
        raise SyncError("登录状态或 Secret 加密准备失败；未写入任何配置") from None

    existing = api.request("GET", f"/contents/{ENCRYPTED_PATH}?ref={STATE_BRANCH}", allow_missing=True)
    sha = None
    if existing is not None:
        if not isinstance(existing, dict) or not _valid_sha(existing.get("sha")):
            raise SyncError("当前密文版本无法确认；未写入任何配置")
        sha = existing["sha"]
    # Recheck immediately before the first mutation; no remote lock is inferred.
    ensure_idle(api)
    payload = {
        "branch": STATE_BRANCH,
        "message": "Rotate encrypted browser session [skip ci]",
        "content": base64.b64encode(encrypted).decode("ascii"),
    }
    if sha is not None:
        payload["sha"] = sha
    written = api.request("PUT", f"/contents/{ENCRYPTED_PATH}", payload)
    if not isinstance(written, dict) or not isinstance(written.get("content"), dict) or not _valid_sha(
        written["content"].get("sha")
    ):
        raise SyncError("密文写入结果无法确认；停止，不自动重试；请重新执行安全同步")
    api.request("PUT", "/actions/secrets/DOUYIN_SESSION_KEY",
                {"key_id": public["key_id"], "encrypted_value": sealed_key})
    api.request("PUT", "/actions/secrets/DOUYIN_STORAGE_STATE",
                {"key_id": public["key_id"], "encrypted_value": sealed_bootstrap})
    if verify:
        api.request("POST", "/actions/workflows/send.yml/dispatches", {
            "ref": repo["default_branch"],
            "inputs": {"dry_run": True, "send_once": False, "refresh_session": False},
        })


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机同步完整加密会话与小型引导 Secret；不发信")
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY)
    parser.add_argument("--state", type=Path, default=Path("storage-state.json"))
    parser.add_argument("--login", action="store_true", help="同步前本人扫码或验证复用本机已有登录")
    parser.add_argument("--verify", action="store_true", help="同步后仅触发手动 Dry Run 验证")
    args = parser.parse_args(argv)
    try:
        api = GitHubAPI(args.repo, github_credential(args.repo))
        # Check access before asking the user to interact with the login browser.
        preflight(api)
        if args.login:
            asyncio.run(login(args.state))
        try:
            state = json.loads(args.state.read_text(encoding="utf-8"))
        except Exception:
            raise SyncError("本机登录状态不可读；请本人扫码后再同步") from None
        synchronize(api, state, verify=args.verify)
        print("安全同步完成：完整会话仅以密文保存，两个 Secret 已加密更新；未改日记或日常发送变量。")
        if args.verify:
            print("已请求 Dry Run 验证；未授权真实发送，请检查 Actions 结果。")
        return 0
    except KeyboardInterrupt:
        return 130
    except SyncError as exc:
        print(str(exc))
        print("如同步中断，请待工作流空闲后重新执行本命令；请勿删除发送日记。")
    except Exception:
        print("安全同步未完成；未输出原始异常或凭据。请待工作流空闲后重试同步。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
