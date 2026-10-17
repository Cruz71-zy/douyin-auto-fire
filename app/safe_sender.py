"""One trigger, no retry; reuse upstream new-bubble and terminal-state checks."""
from app.douyin import PageOperationError
from app.sender import (_mark_latest_outgoing_message, _trigger_send,
                        _confirm_outgoing_message, LATEST_OUTGOING_MESSAGE)


async def send_once(chat, name, content, check_date):
    page = chat.page
    await chat.confirm_identity(name)
    editor = await chat.message_input()
    if (await editor.inner_text()).strip():
        raise PageOperationError("存在未发送草稿，停止操作")
    await editor.fill(content)
    if (await editor.inner_text()).strip() != content.strip():
        raise PageOperationError("输入内容不一致")
    before = await _mark_latest_outgoing_message(page)
    await chat.confirm_identity(name)
    check_date()
    await _trigger_send(page)
    await _confirm_outgoing_message(page, before, "文字", expected_text=content)
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    bubble = latest.locator('[data-e2e="msg-item-content"]').first
    if not await latest.is_visible() or not await bubble.count():
        raise PageOperationError("新消息记录不可见，状态未知")
    normalize = lambda value: " ".join(value.split())
    if normalize(await bubble.inner_text()) != normalize(content):
        raise PageOperationError("新消息内容不完全一致，状态未知")
