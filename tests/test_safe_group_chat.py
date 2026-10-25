"""Group identity and sends against locally fulfilled HTML, using invented data."""
import pytest
import pytest_asyncio
from playwright.async_api import async_playwright

from app.safe_chat import TargetCheckError
from app.safe_group_chat import SafeGroupChat
from app.safe_sender import send_once


GROUP_NAME = "虚构测试群"
GROUP_ID = "fake_group_conversation_001"
SHORT_ID = "1234567890123456789"

HTML = '''<!doctype html><meta charset="utf-8">
<input placeholder="搜索">
<section id="results"></section>
<header class="RightPanelHeader" id="group-header">
  <span class="RightPanelHeadertitle">虚构测试群</span>
</header>
<div class="messageEditor"><div contenteditable="true"></div></div>
<button class="e2e-send-msg-bt">发送</button>
<div class="messageMessageListlist"><div data-index="0"></div></div>
<script>
window.clicks = 0;
window.privateReads = 0;
window.identity = {id:'fake_group_conversation_001', shortId:'1234567890123456789', type:2};
window.attachModel = function(root) {
  const model = {};
  Object.defineProperties(model, {
    id:{get:()=>window.identity.id},
    shortId:{get:()=>window.identity.shortId},
    type:{get:()=>window.identity.type},
    messages:{get:()=>{window.privateReads++;throw new Error('Do not inspect messages');}},
    members:{get:()=>{window.privateReads++;throw new Error('Do not inspect members');}}
  });
  root.__reactFiber$test = {return:{memoizedProps:{curConversation:model}}};
};
attachModel(document.querySelector('#group-header'));
window.setRows = function(names) {
  const results = document.querySelector('#results');
  results.replaceChildren();
  for (const name of names) {
    const row = document.createElement('div');row.className='SearchPanelitembox';
    const title=document.createElement('span');title.className='SearchPanelitemname';title.textContent=name;
    const button=document.createElement('button');button.className='SearchPanelitemchat_btn';button.textContent='发消息';
    row.append(title,button);results.append(row);
  }
};
setRows(['虚构测试群']);
document.querySelector('.e2e-send-msg-bt').onclick = () => {
  window.clicks++;
  const editor=document.querySelector('[contenteditable]');
  const box=document.createElement('div');box.className='messageMessageBoxmessageBox';
  const body=document.createElement('div');body.className='messageMessageBoxcontentBox messageMessageBoxisFromMe';
  const content=document.createElement('span');content.setAttribute('data-e2e','msg-item-content');
  content.innerText=editor.innerText;body.append(content);box.append(body);
  document.querySelector('[data-index="0"]').prepend(box);editor.innerText='';
};
</script>'''


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=HTML))
            current = await context.new_page()
            await current.goto("https://www.douyin.com/chat")
            yield current
        finally:
            await browser.close()


def group_chat(page):
    return SafeGroupChat(page, expected_group_id=GROUP_ID, expected_short_id=SHORT_ID)


async def assert_no_send(page):
    assert await page.evaluate("clicks") == 0
    assert await page.evaluate("privateReads") == 0


@pytest.mark.asyncio
async def test_group_identity_accepts_sdk_getters_from_current_header_owner(page):
    chat = group_chat(page)
    await chat._search_result(GROUP_NAME)
    await chat.confirm_identity(GROUP_NAME)
    assert len(page.context.pages) == 1
    await assert_no_send(page)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,code", [
    ("id", "fake_other_group", "group_identity_mismatch"),
    ("shortId", "9999999999999999999", "group_identity_mismatch"),
    ("type", 1, "group_type_mismatch"),
])
async def test_group_id_short_id_and_conversation_type_must_all_match(page, field, value, code):
    await page.evaluate("([field,value])=>{window.identity[field]=value}", [field, value])
    with pytest.raises(TargetCheckError) as error:
        await group_chat(page).confirm_identity(GROUP_NAME)
    assert error.value.code == code
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_missing_current_conversation_is_not_recovered_from_global_store_or_descendant(page):
    await page.evaluate("""() => {
      const root=document.querySelector('#group-header');
      delete root.__reactFiber$test.return.memoizedProps.curConversation;
      attachModel(root.querySelector('span'));
      window.unrelatedStore={curConversation:{id:identity.id,shortId:identity.shortId,type:2}};
    }""")
    with pytest.raises(TargetCheckError) as error:
        await group_chat(page).confirm_identity(GROUP_NAME)
    assert error.value.code == "group_identity_missing"
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_two_visible_top_level_chat_headers_are_rejected(page):
    await page.evaluate("""() => {
      const duplicate=document.querySelector('#group-header').cloneNode(true);
      duplicate.id='duplicate-group-header';attachModel(duplicate);document.body.append(duplicate);
    }""")
    with pytest.raises(TargetCheckError):
        await group_chat(page).confirm_identity(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_hidden_duplicate_header_does_not_make_visible_identity_ambiguous(page):
    await page.evaluate("""() => {
      const hidden=document.querySelector('#group-header').cloneNode(true);
      hidden.id='hidden-group-header';hidden.hidden=true;document.body.append(hidden);
    }""")
    await group_chat(page).confirm_identity(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_duplicate_exact_search_results_are_rejected(page):
    await page.evaluate("setRows(['虚构测试群','虚构测试群'])")
    with pytest.raises(TargetCheckError):
        await group_chat(page)._search_result(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_hidden_duplicate_search_result_is_ignored(page):
    await page.evaluate("""() => {
      setRows(['虚构测试群','虚构测试群']);
      document.querySelector('#results').lastElementChild.hidden=true;
    }""")
    await group_chat(page)._search_result(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["虚构测试群后缀", "前缀虚构测试群", "虚构测试群(成员)"])
async def test_fuzzy_search_names_do_not_match(page, title):
    await page.evaluate("title=>setRows([title])", title)
    with pytest.raises(TargetCheckError):
        await group_chat(page)._search_result(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["虚构测试群 (5)", "虚构测试群（12）", "虚构测试群(3)"])
async def test_exact_group_name_with_member_count_is_accepted(page, title):
    await page.evaluate("""title=>{
      setRows([title]);document.querySelector('#group-header span').textContent=title;
    }""", title)
    chat = group_chat(page)
    await chat._search_result(GROUP_NAME)
    await chat.confirm_identity(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_fuzzy_chat_header_is_rejected_even_if_model_ids_match(page):
    await page.locator('#group-header span').evaluate("node=>node.textContent='虚构测试群后缀'")
    with pytest.raises(TargetCheckError):
        await group_chat(page).confirm_identity(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_ambiguous_group_search_action_button_is_rejected(page):
    await page.locator('#results .SearchPanelitembox').evaluate("""row=>{
      row.append(row.querySelector('button').cloneNode(true));
    }""")
    with pytest.raises(TargetCheckError):
        await group_chat(page)._search_result(GROUP_NAME)
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_group_uses_inherited_editor_and_one_local_send_receipt(page):
    chat = group_chat(page)
    await chat.confirm_identity(GROUP_NAME)
    await send_once(chat, GROUP_NAME, "虚构的群消息，仅供本地测试", lambda: None)
    assert await page.evaluate("clicks") == 1
    assert await page.locator('[data-e2e="msg-item-content"]').inner_text() == "虚构的群消息，仅供本地测试"
    assert await page.evaluate("privateReads") == 0
    assert len(page.context.pages) == 1


@pytest.mark.asyncio
async def test_group_model_change_after_fill_stops_before_any_send_trigger(page):
    chat = group_chat(page)
    await chat.confirm_identity(GROUP_NAME)
    await page.locator('[contenteditable]').evaluate("""editor=>{
      editor.addEventListener('input',()=>{window.identity.id='fake_switched_group';},{once:true});
    }""")
    with pytest.raises(TargetCheckError) as error:
        await send_once(chat, GROUP_NAME, "虚构的未发送测试文字", lambda: None)
    assert error.value.code == "group_identity_mismatch"
    await assert_no_send(page)


@pytest.mark.asyncio
async def test_another_visible_conversation_header_blocks_even_when_title_differs(page):
    await page.evaluate("""()=>{
      const other=document.createElement('header');other.className='RightPanelHeader';
      other.textContent='另一虚构会话';document.body.append(other);
    }""")
    with pytest.raises(TargetCheckError):
        await group_chat(page).confirm_identity(GROUP_NAME)
    await assert_no_send(page)
