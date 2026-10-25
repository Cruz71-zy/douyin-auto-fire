"""Group navigation bound to the visible header's current conversation model."""
import re

from app.safe_chat import SafeChat, TargetCheckError, HEADERS, ROWS, ensure_session


GROUP_IDENTITY_JS = r"""root => {
 const own=(o,k)=>{try{const d=Object.getOwnPropertyDescriptor(o,k);return d&&'value'in d?d.value:undefined;}catch{return undefined;}};
 const result=[];
 for(const key of Object.keys(root)){
   if(!key.startsWith('__reactFiber$'))continue;
   const owner=own(own(root,key),'return');
   const props=own(owner,'memoizedProps');
   const current=own(props,'curConversation');
   if(!current||typeof current!=='object'||Array.isArray(current))continue;
   try{
     const id=Reflect.get(current,'id'), shortId=Reflect.get(current,'shortId'), type=Reflect.get(current,'type');
     const scalar=value=>typeof value==='string'||(typeof value==='number'&&Number.isSafeInteger(value));
     if(scalar(id)&&scalar(shortId))result.push({id:String(id),short_id:String(shortId),type});
   }catch{}
 }
 return result;
}"""


def valid_group_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,200}", value) is not None


def group_title_pattern(name):
    return re.compile(r"^" + re.escape(name) + r"(?:\s*[（(]\d+[)）])?$")


async def _title_visible(scope, name):
    labels = scope.get_by_text(group_title_pattern(name))
    return any([await labels.nth(i).is_visible() for i in range(await labels.count())])


class SafeGroupChat(SafeChat):
    def __init__(self, page, expected_group_id=None, expected_short_id=None, **kwargs):
        super().__init__(page, expected_uid=expected_group_id, **kwargs)
        self.expected_short_id = expected_short_id
        self.resolved_short_id = None

    async def _search_result(self, name):
        rows, matches = self.page.locator(ROWS), []
        for index in range(await rows.count()):
            row = rows.nth(index)
            if await row.is_visible() and await _title_visible(row, name):
                buttons = row.locator('[class*="chat_btn"]')
                if await buttons.count() != 1 or not await buttons.is_visible():
                    raise TargetCheckError("button_ambiguous")
                matches.append(buttons)
        if len(matches) != 1:
            raise TargetCheckError("search_ambiguous")
        return matches[0]

    async def _chat_open_error(self, name):
        await ensure_session(self.page)
        headers, visible = self.page.locator(HEADERS), []
        for index in range(await headers.count()):
            header = headers.nth(index)
            if (not await header.is_visible() or await header.evaluate(
                    '(node,s)=>!!node.parentElement?.closest(s)', HEADERS)):
                continue
            visible.append(header)
        if len(visible) != 1 or not await _title_visible(visible[0], name):
            return TargetCheckError("title_mismatch")
        records = await visible[0].evaluate(GROUP_IDENTITY_JS)
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            return TargetCheckError("group_identity_missing")
        record = records[0]
        if type(record.get("type")) not in (int, str) or record["type"] not in (2, "2"):
            return TargetCheckError("group_type_mismatch")
        identity, short_id = record.get("id"), record.get("short_id")
        if not valid_group_id(identity) or not valid_group_id(short_id):
            return TargetCheckError("group_identity_missing")
        if ((self.expected_uid is not None and identity != self.expected_uid)
                or (self.expected_short_id is not None and short_id != self.expected_short_id)
                or (self.resolved_uid is not None and identity != self.resolved_uid)
                or (self.resolved_short_id is not None and short_id != self.resolved_short_id)):
            return TargetCheckError("group_identity_mismatch")
        if not await self._composer_visible():
            return TargetCheckError("composer_missing")
        self.resolved_uid, self.resolved_short_id = identity, short_id
        self.report("群聊固定会话身份核验成功")
        return None
