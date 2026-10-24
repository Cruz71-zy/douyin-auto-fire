from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright
from app.auth_state import capture_storage_state, write_storage_state, normalize_storage_state
from app.browser import AuthenticationError, RiskControlError, verify_login
from app.config import ConfigError
from app.douyin import first_visible
from app.safe_chat import ensure_session
from app.selectors import DOUYIN_CHAT_URL, SEARCH_INPUTS


DOUYIN_URL = "https://www.douyin.com/"

async def verify_chat_session(page) -> None:
    await page.goto(DOUYIN_CHAT_URL, wait_until="domcontentloaded", timeout=45_000)
    await verify_login(page)
    await ensure_session(page)
    await first_visible(page, SEARCH_INPUTS, 15_000)


async def save_authenticated_state(context, output: Path) -> None:
    write_storage_state(output, await capture_storage_state(context))


async def login(output: Path = Path("storage-state.json"), *, check_only: bool = False) -> None:
    previous = None
    if output.is_file():
        try:
            previous = normalize_storage_state(json.loads(output.read_text(encoding="utf-8")))
        except (ConfigError, ValueError):
            if check_only:
                raise
            print("已有状态文件不可用，将重新扫码；新验证成功前保留旧文件。")
    if check_only and previous is None:
        raise ConfigError("请先本人扫码生成完整登录状态")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=check_only)
        try:
            options = {"locale": "zh-CN", "viewport": {"width": 1440, "height": 1000}}
            if previous is not None:
                options["storage_state"] = previous
            context = await browser.new_context(**options)
            page = await context.new_page()
            if previous is None:
                await page.goto(DOUYIN_URL, wait_until="domcontentloaded")
                await _open_login(page)
                print("请本人扫码登录；完成后回到终端按 Enter，将验证私信页面。")
                await asyncio.to_thread(input)
            try:
                await verify_chat_session(page)
            except AuthenticationError:
                if check_only or previous is None:
                    raise
                await page.goto(DOUYIN_URL, wait_until="domcontentloaded")
                await _open_login(page)
                print("已有会话失效，请本人重新扫码。完成后按 Enter。")
                await asyncio.to_thread(input)
                await verify_chat_session(page)
            await save_authenticated_state(context, output)
            print("私信登录检查通过，完整状态已原子保存；真实发送次数 0。")
            print("此文件含登录凭证，勿提交或粘贴到聊天。云端请运行 python scripts/sync_auth.py --verify 安全同步。")
        finally:
            await browser.close()


async def _open_login(page) -> None:
    login = page.get_by_text("登录", exact=True)
    if await login.count():
        try:
            await login.first.click(timeout=10_000)
        except Exception:
            pass

    qr_login = page.get_by_text("扫码登录", exact=True)
    if await qr_login.count():
        try:
            await qr_login.first.click(timeout=5_000)
        except Exception:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="验证并保存完整抖音登录状态；不会发信")
    parser.add_argument("--output", type=Path, default=Path("storage-state.json"))
    parser.add_argument("--check-only", action="store_true",
                        help="本机无界面验证并保存刷新后的有效会话；失效时停止，不扫码、不发信")
    args = parser.parse_args(argv)
    try:
        asyncio.run(login(args.output, check_only=args.check_only))
        return 0
    except AuthenticationError:
        print("登录未通过，旧状态未覆盖。请本人重新扫码；无法通过重试恢复失效会话。")
    except RiskControlError:
        print("平台要求本人安全验证，已停止，旧状态未覆盖。")
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("登录或私信页面验证未完成，旧状态未覆盖。未输出原始异常以保护凭证。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

