"""One trigger, no retry; reuse upstream new-bubble and terminal-state checks."""
from app.douyin import PageOperationError
from app.sender import (_mark_latest_outgoing_message, _trigger_send,
                        _confirm_outgoing_message, LATEST_OUTGOING_MESSAGE)


async def send_once(chat, name, content, check_date):
    # ``chat.stage`` records a fixed step label so a failure names the exact
    # step; it never carries page text, nicknames, URLs or credentials.
    page = chat.page
    chat.stage = "send_identity_before_edit"
    await chat.confirm_identity(name)
    chat.report('发送阶段：身份已核验，检查空白编辑框')
    chat.stage = "send_editor_blank"
    editor = await chat.message_input()
    if (await editor.inner_text()).strip():
        raise PageOperationError("存在未发送草稿，停止操作")
    chat.stage = "send_fill"
    await editor.fill(content)
    chat.report('发送阶段：已输入本次授权文字，检查完整内容')
    chat.stage = "send_verify_text"
    if (await editor.inner_text()).strip() != content.strip():
        raise PageOperationError("输入内容不一致")
    chat.stage = "send_mark_baseline"
    before = await _mark_latest_outgoing_message(page)
    chat.stage = "send_identity_before_trigger"
    await chat.confirm_identity(name)
    check_date()
    chat.report('发送阶段：即将触发一次发送')
    chat.stage = "send_trigger"
    await _trigger_send(page)
    chat.report('发送阶段：已触发一次发送，等待聊天记录确认')
    chat.stage = "send_confirm_outgoing"
    await _confirm_outgoing_message(page, before, "文字", expected_text=content)
    chat.stage = "send_verify_bubble"
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    bubble = latest.locator('[data-e2e="msg-item-content"]').first
    if not await latest.is_visible() or not await bubble.count():
        raise PageOperationError("新消息记录不可见，状态未知")
    normalize = lambda value: " ".join(value.split())
    if normalize(await bubble.inner_text()) != normalize(content):
        raise PageOperationError("新消息内容不完全一致，状态未知")
