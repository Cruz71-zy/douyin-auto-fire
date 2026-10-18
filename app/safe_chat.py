"""Conservative adapter for the upstream Douyin DOM and text sender."""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from app.browser import AuthenticationError, RiskControlError, _any_visible
from app.douyin import DouyinChat, PageOperationError, _visible_exact_text_locator, first_visible
from app.selectors import LOGIN_REQUIRED_MARKERS, RISK_MARKERS, SEARCH_INPUTS

ROWS = '[class*="SearchPanelitembox"], [class*="SearchPanelitem-box"], [class*="SearchPanelitem_box"]'
NAMES = tuple(f'[class*="SearchPanelitem{x}"]' for x in
              ("title", "Title", "_title", "-title", "name", "Name", "_name", "-name"))
HEADERS = '[class*="RightPanelHeader" i], [class*="chatHeader" i]'
TITLES = tuple(f'[class*="{x}"]' for x in (
    "RightPanelHeadertitle", "RightPanelHeaderTitle", "RightPanelHeader_title",
    "RightPanelHeader-title", "chatHeadertitle", "ChatHeaderTitle",
    "chatHeader_title", "ChatHeader-title", "RightPanelHeadername", "RightPanelHeaderName",
    "RightPanelHeader_name", "RightPanelHeader-name", "chatHeaderName", "ChatHeaderName",
    "name", "Name", "nickname", "Nickname"))

SAFE_REASONS = {
    "profile_missing": "没有可见且唯一的个人主页链接",
    "profile_ambiguous": "同一结果中出现多个不同的个人主页标识",
    "search_ambiguous": "搜索结果不存在或不是唯一匹配",
    "button_ambiguous": "好友操作按钮不唯一",
    "title_mismatch": "当前聊天标题不唯一或不一致",
    "uid_mismatch": "当前聊天的固定身份标识不一致",
    "composer_missing": "当前聊天输入框不可用",
    "editor_ambiguous": "聊天编辑器不唯一",
    "draft_present": "当前聊天存在真实未发送草稿，未修改或发送",
    "profile_popup_missing": "头像未打开可核验的独立个人主页",
    "profile_replaced_chat": "个人主页替换了聊天页面，无法保持身份绑定",
    "avatar_ambiguous": "网页没有唯一可操作的头像入口",
    "metadata_ambiguous": "同一聊天条目的客户端身份字段相互冲突",
}


# Read only identity data attached to this row/header and its immediate rendering
# owner, not application stores, network responses or unrelated result collections.
IDENTITY_METADATA_JS = r"""(root, expectedName) => {
    const ids=new Set(), seen=new WeakSet(); let records=0, attributes=0;
    const secKeys=['sec_uid','secUid','secUID','sec_user_id','secUserId','secUserID','sec_id','secId'];
    const nameKeys=['nickname','nick_name','nickName','display_name','displayName','name'];
    const subjects=['user','userInfo','user_info','item','data','friend','contact','profile','info',
        'peer','peerInfo','targetUser','userProfile','conversation','conversationInfo'];
    const valid=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{6,200}$/.test(value);
    const own=(object,key)=>{
        try { const d=Object.getOwnPropertyDescriptor(object,key);return d&&'value' in d?d.value:undefined; }
        catch { return undefined; }
    };
    const record=(object,depth=0)=>{
        if(!object||typeof object!=='object'||Array.isArray(object)||depth>3||seen.has(object))return;
        seen.add(object);
        const names=nameKeys.map(key=>own(object,key)).filter(value=>typeof value==='string');
        const values=secKeys.map(key=>own(object,key)).filter(valid);
        if(values.length){records++;if(names.some(value=>value.trim()===expectedName))values.forEach(value=>ids.add(value));}
        for(const key of subjects)record(own(object,key),depth+1);
    };
    const nodes=[root,...root.querySelectorAll('*')].slice(0,65);
    for(const node of nodes){
        for(const key of ['data-sec-uid','data-secuid','data-user-sec-uid']){
            const value=node.getAttribute(key);if(valid(value)){attributes++;ids.add(value);}
        }
        for(const key of Object.keys(node)){
            if(key.startsWith('__reactProps$'))record(own(node,key));
            if(key.startsWith('__reactFiber$')){
                const fiber=own(node,key);if(!fiber)continue;
                record(own(fiber,'memoizedProps'));
                // Only the root's immediate rendering owner is inspected. Never
                // walk up to a results collection or traverse array/children data.
                if(node===root){const owner=own(fiber,'return');if(owner)record(own(owner,'memoizedProps'));}
            }
        }
    }
    return {ids:[...ids],records,attributes};
}"""


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
        self.stage = 'init'
        self.report = report or (lambda message: None)

    async def _identity(self, scope, name):
        try:
            return await profile_id(scope)
        except TargetCheckError as exc:
            if exc.code != "profile_missing":
                raise
            uid = await self._identity_from_metadata(scope, name)
            if uid is not None:
                return uid
            return await self._identity_from_avatar(scope)

    async def _identity_from_metadata(self, scope, name):
        try:
            evidence = await scope.evaluate(IDENTITY_METADATA_JS, name)
        except Exception:
            self.report("客户端身份检查：当前条目未提供可读的身份字段")
            return None
        if not isinstance(evidence, dict) or not isinstance(evidence.get("ids"), list):
            return None
        ids = evidence["ids"]
        if any(not isinstance(uid, str) or not uid for uid in ids):
            raise TargetCheckError("metadata_ambiguous")
        ids = set(ids)
        records = evidence.get("records", 0)
        attributes = evidence.get("attributes", 0)
        records = min(1000, max(0, records)) if type(records) is int else 0
        attributes = min(1000, max(0, attributes)) if type(attributes) is int else 0
        self.report(f"客户端身份检查：关联身份记录 {records}，身份属性 {attributes}，同名固定标识 {len(ids)}")
        if len(ids) > 1:
            raise TargetCheckError("metadata_ambiguous")
        if not ids:
            return None
        uid = ids.pop()
        if self.expected_uid is not None and uid != self.expected_uid:
            raise TargetCheckError("uid_mismatch")
        self.report("客户端身份检查：当前条目的固定标识已精确核验")
        return uid

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
        image_count = len(avatars)
        # Require an explicit avatar component. Arbitrary images in a chat header
        # could be call/action icons, so never click one merely because it is the
        # only image. Background avatars and nested avatar images are supported.
        selector = ('[class*="avatar" i], [class*="avator" i], [class*="avtar" i], '
                    'img[alt="头像"], img[alt="avatar" i]')
        components = scope.locator(selector)
        avatars = []
        for index in range(await components.count()):
            component = components.nth(index)
            if not await component.is_visible():
                continue
            nested = await component.evaluate(
                "(element, selector) => !!element.parentElement?.closest(selector)", selector)
            if not nested:
                avatars.append(component)
        self.report(f"头像入口检查：可见图片 {image_count}，独立头像容器 {len(avatars)}")
        if len(avatars) != 1:
            await self._report_shape(scope)
            raise TargetCheckError("avatar_ambiguous")
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

    async def _report_shape(self, scope):
        # Boolean structural facts only: no text, class names, attribute values,
        # image URLs, profile IDs, page HTML or React state can enter logs.
        try:
            shape = await scope.evaluate("""root => [...root.querySelectorAll('*')].slice(0, 32).map(el => {
                const style=getComputedStyle(el);
                return {tag:el.tagName.toLowerCase(),
                    avatar:/avatar|avator|avtar/i.test(typeof el.className==='string'?el.className:''),
                    visible:!!el.getClientRects().length&&style.visibility!=='hidden'&&style.display!=='none',
                    background:style.backgroundImage!=='none',
                    link:el.tagName==='A',
                    button:el.tagName==='BUTTON'||el.getAttribute('role')==='button',
                    children:el.childElementCount};
            })""")
            safe = []
            for node in shape[:32]:
                if not isinstance(node, dict):
                    continue
                tag = node.get("tag")
                tag = tag if tag in {"div", "span", "img", "svg", "path", "a", "button", "p", "i"} else "other"
                safe.append({"tag": tag, **{key: node.get(key) is True for key in
                    ("avatar", "visible", "background", "link", "button")},
                    "children": min(100, max(0, node.get("children", 0)))
                    if type(node.get("children")) is int else 0})
            self.report("头像结构检查：" + json.dumps(safe, ensure_ascii=True, separators=(",", ":")))
        except Exception:
            self.report("头像结构检查：无法读取结构计数")

    async def _open_target_once(self, name):
        await ensure_session(self.page)
        search = await first_visible(self.page, SEARCH_INPUTS, self.timeout_ms)
        await search.click()
        await search.fill('')
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
        count = await editors.count()
        for i in range(count):
            editor = editors.nth(i)
            if await editor.is_visible():
                visible.append(editor)
        self.report(f'编辑框检查：候选 {count}，可见 {len(visible)}')
        for index, editor in enumerate(visible[:4]):
            value = await editor.inner_text()
            empty = not value.strip()
            scaffold_empty = not value.replace('\u200b', '').replace('\ufeff', '').strip()
            nested = await editor.evaluate('(node)=>!!node.parentElement?.closest("[contenteditable=true]")')
            self.report(f'编辑框结构#{index + 1}：纯空白={empty}；移除零宽占位后空白={scaffold_empty}；嵌套可编辑={nested is True}')
        if len(visible) != 1:
            raise TargetCheckError("editor_ambiguous")
        return visible[0]

    async def _search_result(self, name):
        candidates = []
        rows = self.page.locator(ROWS)
        for index in range(await rows.count()):
            row = rows.nth(index)
            if (await row.is_visible() and
                    await _visible_exact_text_locator(row, NAMES, name) is not None):
                candidates.append(row)
        # Opening a conversation is navigation, not permission to send. The real
        # search row has no profile identifier; defer identity proof to its header.
        # Never use this path to choose between duplicate names.
        if len(candidates) == 1:
            row = candidates[0]
            try:
                uid = await profile_id(row)
            except TargetCheckError as exc:
                if exc.code != 'profile_missing':
                    raise
                uid = await self._identity_from_metadata(row, name)
            if uid is not None and self.expected_uid is not None and uid != self.expected_uid:
                raise TargetCheckError('uid_mismatch')
            buttons = row.locator('[class*="SearchPanelitemchat_btn"]')
            if await buttons.count() != 1 or not await buttons.is_visible():
                raise TargetCheckError('button_ambiguous')
            if uid is not None:
                if self.resolved_uid is not None and self.resolved_uid != uid:
                    raise TargetCheckError('uid_mismatch')
                self.resolved_uid = uid
            self.report('搜索检查：唯一昵称用于打开聊天；发送前仍须核验聊天头固定标识')
            return buttons
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
                uid = await self._identity(row, name)
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
        button, uid = matches[0]
        if self.resolved_uid is not None and uid != self.resolved_uid:
            raise TargetCheckError('uid_mismatch')
        self.resolved_uid = uid
        return button

    async def _chat_open_error(self, name):
        await ensure_session(self.page)
        matches = []
        headers = self.page.locator(HEADERS)
        count = await headers.count()
        visible_count = 0
        for i in range(count):
            header = headers.nth(i)
            if not await header.is_visible():
                continue
            # A CSS-module title may share its parent's header prefix. Inspect
            # only the top-level header so that nested title wrappers are not
            # mistaken for a second conversation header.
            if await header.evaluate('(node, selector)=>!!node.parentElement?.closest(selector)', HEADERS):
                continue
            visible_count += 1
            if await _visible_exact_text_locator(header, TITLES, name) is not None:
                matches.append(header)
        shape = (count, visible_count, len(matches))
        if getattr(self, '_last_header_shape', None) != shape:
            self.report(f'聊天头检查：节点 {count}，可见独立容器 {visible_count}，昵称精确匹配 {len(matches)}')
            self._last_header_shape = shape
        if len(matches) != 1:
            return TargetCheckError("title_mismatch")
        try:
            uid = await self._identity(matches[0], name)
            if ((self.expected_uid is not None and uid != self.expected_uid) or
                    (self.resolved_uid is not None and uid != self.resolved_uid)):
                return TargetCheckError("uid_mismatch")
        except PageOperationError as exc:
            return exc
        if not await self._composer_visible():
            return TargetCheckError("composer_missing")
        self.resolved_uid = uid
        return None

    async def confirm_identity(self, name):
        error = await self._chat_open_error(name)
        if error:
            raise error
