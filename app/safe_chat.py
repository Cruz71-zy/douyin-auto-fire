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

SAFE_REASONS = {
    "profile_missing": "没有可见且唯一的个人主页链接",
    "profile_ambiguous": "同一结果中出现多个不同的个人主页标识",
    "search_ambiguous": "搜索结果不存在或不是唯一匹配",
    "button_ambiguous": "好友操作按钮不唯一",
    "title_mismatch": "当前聊天标题不唯一或不一致",
    "uid_mismatch": "当前聊天的固定身份标识不一致",
    "composer_missing": "当前聊天输入框不可用",
    "editor_ambiguous": "聊天编辑器不唯一",
    "profile_popup_missing": "头像未打开可核验的独立个人主页",
    "profile_replaced_chat": "个人主页替换了聊天页面，无法保持身份绑定",
}


class TargetCheckError(PageOperationError):
    def __init__(self, code):
        self.code = code if code in SAFE_REASONS else "search_ambiguous"
        super().__init__(SAFE_REASONS[self.code])


def profile_uid_from_url(value):
    try:
        parsed = urlsplit(value)
        parts = parsed.path.strip("/").split("/")
        if (parsed.scheme == "https" and parsed.hostname == "www.douyin.com"
                and len(parts) == 2 and parts[0] == "user" and parts[1]):
            return parts[1]
    except ValueError:
        pass
    return None


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
    if len(ids) > 1:
        raise TargetCheckError("profile_ambiguous")
    if not ids:
        raise TargetCheckError("profile_missing")
    return ids.pop()


class SafeChat(DouyinChat):
    def __init__(self, page, expected_uid=None, report=None, **kwargs):
        super().__init__(page, **kwargs)
        self.expected_uid = expected_uid
        self.resolved_uid = None
        self.report = report or (lambda message: None)

    async def _identity(self, scope):
        try:
            return await profile_id(scope)
        except TargetCheckError as exc:
            if exc.code != "profile_missing":
                raise
            return await self._identity_from_avatar(scope)

    async def _identity_from_avatar(self, scope):
        # Some builds use window.open on an avatar rather than an <a> element.
        # Trust only the resulting canonical profile URL; never infer an ID from
        # an image URL, a nickname, or a hidden node.
        images = scope.locator("img")
        avatars = []
        for index in range(await images.count()):
            image = images.nth(index)
            if await image.is_visible():
                avatars.append(image)
        if len(avatars) != 1:
            raise TargetCheckError("profile_missing")
        context = self.page.context
        previous_pages = tuple(context.pages)
        chat_url = self.page.url
        opened = []
        try:
            self.report("个人主页检查：尝试通过唯一可见头像核验固定标识")
            await avatars[0].click(timeout=5000)
            for _ in range(12):
                opened = [page for page in context.pages if page not in previous_pages]
                if self.page.url != chat_url:
                    raise TargetCheckError("profile_replaced_chat")
                if len(opened) > 1:
                    raise TargetCheckError("profile_popup_missing")
                if len(opened) == 1:
                    uid = profile_uid_from_url(opened[0].url)
                    if uid:
                        if self.expected_uid is not None and uid != self.expected_uid:
                            raise TargetCheckError("uid_mismatch")
                        if await _any_visible(opened[0], RISK_MARKERS, 1000):
                            raise RiskControlError("个人主页需要本人安全验证")
                        self.report("个人主页检查：独立主页固定标识已核验")
                        return uid
                await self.page.wait_for_timeout(500)
            raise TargetCheckError("profile_popup_missing")
        finally:
            # Close only pages created during this avatar action, never the chat.
            for opened_page in opened:
                try:
                    await opened_page.close()
                except Exception:
                    pass

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
            raise TargetCheckError("editor_ambiguous")
        return visible[0]

    async def _search_result(self, name):
        matches = []
        rows = self.page.locator(ROWS)
        row_count = await rows.count()
        visible_count = exact_count = uid_count = 0
        for i in range(row_count):
            row = rows.nth(i)
            if not await row.is_visible():
                continue
            visible_count += 1
            if await _visible_exact_text_locator(row, NAMES, name) is None:
                continue
            exact_count += 1
            # Require a visible personal profile link, never a group/fuzzy match.
            try:
                uid = await self._identity(row)
            except TargetCheckError:
                self.report(f"搜索检查：结果节点 {row_count}，可见 {visible_count}，昵称精确匹配 {exact_count}，个人主页标识不可确认")
                raise
            if self.expected_uid is None or uid == self.expected_uid:
                uid_count += 1
                buttons = row.locator('[class*="SearchPanelitemchat_btn"]')
                if await buttons.count() != 1 or not await buttons.is_visible():
                    raise TargetCheckError("button_ambiguous")
                matches.append((buttons, uid))
        self.report(f"搜索检查：结果节点 {row_count}，可见 {visible_count}，昵称精确匹配 {exact_count}，固定标识匹配 {uid_count}")
        if len(matches) != 1:
            raise TargetCheckError("search_ambiguous")
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
            return TargetCheckError("title_mismatch")
        try:
            if await self._identity(matches[0]) != self.resolved_uid:
                return TargetCheckError("uid_mismatch")
        except PageOperationError as exc:
            return exc
        if not await self._composer_visible():
            return TargetCheckError("composer_missing")
        return None

    async def confirm_identity(self, name):
        error = await self._chat_open_error(name)
        if error:
            raise error
