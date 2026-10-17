from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.browser import (AuthenticationError, RiskControlError, SearchBoxNotReadyError,
                         _normalize_cookies, open_douyin, verify_login)
from app.config import ConfigError, load_settings, load_task, parse_auth_json
from app.douyin import PageOperationError
from app.durable_state import GitHubJournal, StateError
from app.safe_chat import SafeChat, TargetCheckError, SAFE_REASONS, ensure_session
from app.selectors import DOUYIN_CHAT_URL, SEARCH_INPUTS
from app.douyin import first_visible
from app.safe_sender import send_once


class IdentityConfigError(ConfigError):
    pass


def event(message):
    print(f"{datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')} {message}", flush=True)


def today():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def load_safe_config():
    settings = load_settings()
    # This entry point never captures page data or invokes upstream notifications.
    settings = replace(settings, trace=False, headless=True, dingtalk_webhook=None,
                       dingtalk_secret=None, webhook_url=None)
    if not settings.cookie:
        raise ConfigError("缺少 DOUYIN_COOKIE")
    cookies = parse_auth_json(settings.cookie, "DOUYIN_COOKIE")
    if not isinstance(cookies, list):
        raise ConfigError("DOUYIN_COOKIE 必须为 Cookie 数组")
    cookies = _normalize_cookies(cookies)
    if any(c["domain"].lstrip(".") != "douyin.com" and
           not c["domain"].lstrip(".").endswith(".douyin.com") for c in cookies):
        raise ConfigError("Cookie 仅允许抖音域名")
    settings = replace(settings, cookie=json.dumps(cookies), storage_state=None)
    raw_text = os.getenv("DOUYIN_CONFIG")
    if raw_text is None:
        raw_text = settings.task_config_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    if not isinstance(raw, dict):
        raise ConfigError("配置必须为对象")
    if raw.get("timezone", "Asia/Shanghai") != "Asia/Shanghai" or raw.get("prevent_duplicates", True) is not True:
        raise ConfigError("安全入口固定上海时区且不允许关闭防重复")
    with tempfile.TemporaryDirectory(prefix="douyin-config-") as directory:
        path = Path(directory) / "config.json"
        path.write_text(raw_text, encoding="utf-8")
        task = load_task(replace(settings, task_config_path=path))
    names = [t.name for t in task.targets]
    if len(set(names)) != len(names) or len(names) > 10:
        raise ConfigError("好友重复或超过十人")
    def texts(message):
        if message.type == "random":
            return [m for choice in message.choices for m in texts(choice)]
        if message.type != "text" or not message.content or len(message.content) > 200:
            raise ConfigError("最小版本仅支持 1 至 200 字文字消息")
        return [message]
    candidates = [tuple(m for message in t.messages for m in texts(message)) for t in task.targets]
    meta = raw.get("targets", [{} for _ in names])
    uids = [m.get("sec_uid") for m in meta]
    if any(uid is not None and (not isinstance(uid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,200}", uid)) for uid in uids):
        raise ConfigError("sec_uid 格式无效")
    return settings, task, candidates, uids


async def execute(settings, task, candidates, uids, *, dry_run=True, journal=None):
    day = today()
    if not dry_run:
        if len(uids) != len(task.targets) or any(not uid for uid in uids):
            raise IdentityConfigError("正式发送需要本人核对的 sec_uid")
        if journal is None:
            raise StateError("正式发送必须使用持久状态")
        if journal.contains(day):
            event("最终状态：duplicate；当天已处理或状态未知，跳过全部好友")
            return 0
    reserved = False
    attempted = False
    try:
        async with open_douyin(settings) as session:
            page = session.page
            await page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45000)
            await verify_login(page)
            await ensure_session(page)
            await first_visible(page, SEARCH_INPUTS, 15000)
            event("登录检查成功")
            chats = []
            resolved = set()
            # Resolve every target before any reservation or message.
            for index, target in enumerate(task.targets):
                chat = SafeChat(page, expected_uid=uids[index],
                                report=lambda message, number=index + 1: event(f"好友#{number}：{message}"),
                                timeout_ms=int(task.target_open_timeout_seconds * 1000))
                await chat.open_target(target.name, retries=0)
                await chat.confirm_identity(target.name)
                if chat.resolved_uid in resolved:
                    raise PageOperationError("多个配置指向同一好友")
                resolved.add(chat.resolved_uid)
                # Pin the identity across search/preflight and actual sending.
                chat.expected_uid = chat.resolved_uid
                chats.append(chat)
                event(f"好友#{index + 1}：唯一身份和聊天检查成功")
            if dry_run:
                event("最终状态：dry_run_success；真实发送次数 0")
                return 0
            if today() != day:
                raise StateError("运行跨越日期，停止发送")
            if not journal.reserve(day):
                event("最终状态：duplicate；当天已预占，停止发送")
                return 0
            reserved = True
            for index, (target, chat) in enumerate(zip(task.targets, chats)):
                if today() != day:
                    raise StateError("运行跨越日期，停止发送")
                await chat.open_target(target.name, retries=0)
                await chat.confirm_identity(target.name)
                message = random.choice(candidates[index])
                # Upstream sender validates a new outgoing bubble and failure/pending markers.
                attempted = True
                def check_date():
                    if today() != day:
                        raise StateError("运行跨越日期，停止发送")
                await send_once(chat, target.name, message.content, check_date)
                await chat.confirm_identity(target.name)
                event(f"好友#{index + 1}：发送成功（新消息及页面发送状态已确认）")
                if index + 1 < len(chats):
                    await asyncio.sleep(random.uniform(task.interval_min, task.interval_max))
            journal.finish(day, "success")
            event("最终状态：success")
            return 0
    except Exception:
        if reserved:
            try:
                journal.finish(day, "unknown")
            except Exception:
                pass  # Durable reservation already blocks all subsequent runs today.
        if attempted:
            event("最终状态：unknown；状态未知，当天禁止自动重试")
            return 1
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全续火花：默认 Dry Run")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--send", action="store_true", help="需同时设置 ENABLE_REAL_SEND=true")
    parser.add_argument("--initialize-state", action="store_true", help="仅首次显式初始化，无私信操作")
    args = parser.parse_args(argv)
    # Do not print arbitrary exception text, locators, URLs, cookies or page diagnostics.
    logging.getLogger("douyin_sender").disabled = True
    try:
        if args.initialize_state:
            if args.send:
                raise ConfigError("初始化禁止发送")
            GitHubJournal.from_env().initialize(os.getenv("GITHUB_SHA", ""))
            event("状态分支初始化成功；真实发送次数 0")
            return 0
        settings, task, candidates, uids = load_safe_config()
        dry_run = not args.send or args.dry_run
        if not dry_run and os.getenv("ENABLE_REAL_SEND") != "true":
            raise ConfigError("正式发送尚未明确启用")
        if not dry_run and os.getenv("GITHUB_ACTIONS") != "true":
            raise ConfigError("正式发送仅允许在 GitHub Actions 中执行")
        journal = None if dry_run else GitHubJournal.from_env()
        return asyncio.run(execute(settings, task, candidates, uids, dry_run=dry_run, journal=journal))
    except IdentityConfigError:
        event("最终状态：failed；正式发送要求每位好友在 targets 中配置本人核对的 sec_uid；未执行发送")
    except (ConfigError, ValueError, OSError):
        event("最终状态：failed；配置错误，请检查 DOUYIN_COOKIE、DOUYIN_CONFIG 和正式发送开关；未执行发送")
    except AuthenticationError:
        event("最终状态：failed；登录失效或无法确认，请本人重新登录并更新凭证")
    except RiskControlError:
        event("最终状态：failed；需要本人完成平台安全验证，停止任务")
    except TargetCheckError as exc:
        reason = SAFE_REASONS.get(exc.code, "好友身份无法确认")
        event(f"最终状态：failed；好友检查未通过：{reason}；停止任务")
    except (PageOperationError, SearchBoxNotReadyError):
        event("最终状态：failed；好友匹配、个人主页标识或页面控件无法确认，停止任务")
    except StateError:
        event("最终状态：failed；持久状态缺失、损坏、冲突或接口不可用，停止任务；勿删除状态重试")
    except KeyboardInterrupt:
        event("最终状态：cancelled；如已预占，当天不会自动重试")
        return 130
    except Exception:
        event("最终状态：failed；浏览器或页面操作异常；未输出原始异常以保护隐私")
    return 2
