"""Conservative adapter for the upstream Douyin DOM and text sender."""
from __future__ import annotations

from urllib.parse import urlsplit
from app.browser import AuthenticationError, RiskControlError, _any_visible
from app.douyin import DouyinChat, PageOperationError, _visible_exact_text_locator, first_visible
from app.selectors import LOGIN_REQUIRED_MARKERS, RISK_MARKERS, SEARCH_INPUTS

ROWS = '[class*="SearchPanelitembox"], [class*="SearchPanelitem-box"], [class*="SearchPanelitem_box"]'
NAMES = tuple(f'[class*="SearchPanelitem{x}"]' for x in
              ("title", "Title", "_title", "-title", "name", "Name", "_name", "-name"))
HEADERS = '[class*="RightPanelHeader"], [class*="chatHeader"], [class*="ChatHeader"]'
TITLES = tuple(f'[class*="{x}"]' for x in (
    "RightPanelHeadertitle", "RightPanelHeaderTitle", "RightPanelHeader_title",
    "RightPanelHeader-title", "chatHeadertitle", "ChatHeaderTitle",
    "chatHeader_title", "ChatHeader-title"))


async def ensure_session(page):
    if await _any_visible(page, RISK_MARKERS + (
        'iframe[src*="captcha"]', '[id*="captcha"]', 'text=访问过于频繁', 'text=操作频繁'), 1000):
        raise RiskControlError("需要本人安全验证")
    if await _any_visible(page, LOGIN_REQUIRED_MARKERS, 1000):
        raise AuthenticationError("登录失效，需要本人登录")
    url = urlsplit(page.url)
    if url.scheme != "https" or url.hostname != "www.douyin.com" or url.path.rstrip("/") != "/chat":
        raise AuthenticationError("未处于私信页面")


async def profile_id(scope):
    links = scope.locator('a[href*="/user/"]')
    ids = set()
    for i in range(await links.count()):
        link = links.nth(i)
        if not await link.is_visible():
            continue
        href = await link.get_attribute("href") or ""
        parsed = urlsplit(href)
        if parsed.scheme not in {"", "https"}:
            continue
        if parsed.netloc and (parsed.scheme != "https" or parsed.hostname != "www.douyin.com"):
            continue
        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "user" and parts[1]:
            ids.add(parts[1])
    if len(ids) != 1:
        raise PageOperationError("无法确认唯一个人主页标识，停止发送")
    return ids.pop()


class SafeChat(DouyinChat):
    def __init__(self, page, expected_uid=None, **kwargs):
        super().__init__(page, **kwargs)
        self.expected_uid = expected_uid
        self.resolved_uid = None

    async def _open_target_once(self, name):
        await ensure_session(self.page)
        search = await first_visible(self.page, SEARCH_INPUTS, self.timeout_ms)
        await search.fill(name)
        await self.page.wait_for_timeout(1500)
        result = await self._search_result(name)
        await result.click()  # Respect visibility, overlays and platform restrictions.
        await self._confirm_opened(name)

    async def message_input(self):
        editors = self.page.locator(
            '.DraftEditor-root [contenteditable="true"], '
            '[class*="messageEditor"] [contenteditable="true"], '
            '.messageEditorinputArea[contenteditable="true"]')
        visible = []
        for i in range(await editors.count()):
            editor = editors.nth(i)
            if await editor.is_visible():
                visible.append(editor)
        if len(visible) != 1:
            raise PageOperationError("聊天编辑器不唯一")
        return visible[0]

    async def _search_result(self, name):
        matches = []
        rows = self.page.locator(ROWS)
        for i in range(await rows.count()):
            row = rows.nth(i)
            if not await row.is_visible():
                continue
            if await _visible_exact_text_locator(row, NAMES, name) is None:
                continue
            # Require a visible personal profile link, never a group/fuzzy match.
            uid = await profile_id(row)
            if self.expected_uid is None or uid == self.expected_uid:
                buttons = row.locator('[class*="SearchPanelitemchat_btn"]')
                if await buttons.count() != 1 or not await buttons.is_visible():
                    raise PageOperationError("好友操作按钮不唯一")
                matches.append((buttons, uid))
        if len(matches) != 1:
            raise PageOperationError("好友不存在或匹配不唯一，停止发送")
        button, self.resolved_uid = matches[0]
        return button

    async def _chat_open_error(self, name):
        await ensure_session(self.page)
        matches = []
        headers = self.page.locator(HEADERS)
        for i in range(await headers.count()):
            header = headers.nth(i)
            if await header.is_visible() and await _visible_exact_text_locator(header, TITLES, name) is not None:
                matches.append(header)
        if len(matches) != 1 or self.resolved_uid is None:
            return PageOperationError("当前聊天标题不唯一或不一致")
        try:
            if await profile_id(matches[0]) != self.resolved_uid:
                return PageOperationError("当前聊天个人标识不一致")
        except PageOperationError as exc:
            return exc
        if not await self._composer_visible():
            return PageOperationError("当前聊天输入框不可用")
        return None

    async def confirm_identity(self, name):
        error = await self._chat_open_error(name)
        if error:
            raise error
