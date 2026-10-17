"""Real Chromium against locally fulfilled HTML; no request reaches Douyin."""
import pytest
import pytest_asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from playwright.async_api import async_playwright

from app.safe_chat import SafeChat, TargetCheckError
from app.safe_sender import send_once
from app.douyin import PageOperationError
from app import safe_main as runner


HTML = '''<!doctype html><meta charset="utf-8">
<input placeholder="搜索">
<section id="results"></section>
<header class="RightPanelHeader">
  <span class="RightPanelHeadertitle">好友A</span><a id="profile" href="/user/uidAAAA">个人主页</a>
</header>
<div class="messageEditor"><div contenteditable="true"></div></div>
<button class="e2e-send-msg-bt">发送</button>
<div class="messageMessageListlist"><div data-index="0"></div></div>
<script>
window.clicks=0;
window.setRows = function(names, ids) {
  document.querySelector('#results').innerHTML = names.map((n,i) =>
    `<div class="SearchPanelitembox"><span class="SearchPanelitemname">${n}</span>
     <a href="/user/${ids[i]}">个人主页</a><button class="SearchPanelitemchat_btn">发消息</button></div>`).join('');
};
setRows(['好友A'], ['uidAAAA']);
document.querySelector('.e2e-send-msg-bt').onclick = () => {
  window.clicks++;
  const editor=document.querySelector('[contenteditable]');
  const box=document.createElement('div'); box.className='messageMessageBoxmessageBox';
  const body=document.createElement('div'); body.className='messageMessageBoxcontentBox messageMessageBoxisFromMe';
  const content=document.createElement('span');content.setAttribute('data-e2e','msg-item-content');content.innerText=editor.innerText;
  body.append(content);box.append(body);
  if(window.failSend) box.insertAdjacentHTML('beforeend','<b title="重试">!</b>');
  document.querySelector('[data-index="0"]').prepend(box);editor.innerText='';
};
</script>'''


@pytest_asyncio.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        # Fulfill every network request locally, including the fake chat URL.
        await context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=HTML))
        page = await context.new_page()
        await page.goto('https://www.douyin.com/chat')
        yield page
        await browser.close()


@pytest.mark.asyncio
async def test_unique_identity_and_local_send_confirmation(page):
    chat = SafeChat(page)
    await chat._search_result('好友A')
    await chat.confirm_identity('好友A')
    await send_once(chat, '好友A', '🔥', lambda: None)
    assert await page.evaluate('clicks') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('names,ids', [(['好友A','好友A'], ['uidAAAA','uidBBBB']),
    (['好友A(7)'], ['uidAAAA']), (['好友A后缀'], ['uidAAAA'])])
async def test_duplicate_fuzzy_and_group_names_stop(page, names, ids):
    await page.evaluate('([names,ids])=>setRows(names,ids)', [names,ids])
    with pytest.raises(PageOperationError):
        await SafeChat(page)._search_result('好友A')
    assert await page.evaluate('clicks') == 0


@pytest.mark.asyncio
async def test_sec_uid_disambiguates_same_nickname(page):
    await page.evaluate("setRows(['好友A','好友A'],['uidAAAA','uidBBBB'])")
    chat = SafeChat(page, expected_uid='uidBBBB')
    await chat._search_result('好友A')
    assert chat.resolved_uid == 'uidBBBB'
    with pytest.raises(PageOperationError):
        await chat.confirm_identity('好友A') # Header still points to uidAAAA.


@pytest.mark.asyncio
async def test_missing_profile_stops(page):
    await page.locator('#results a').evaluate('(e)=>e.remove()')
    with pytest.raises(PageOperationError):
        await SafeChat(page)._search_result('好友A')


@pytest.mark.asyncio
async def test_existing_draft_never_triggers_send(page):
    chat = SafeChat(page)
    await chat._search_result('好友A')
    await page.locator('[contenteditable]').fill('原有私人草稿')
    with pytest.raises(PageOperationError):
        await send_once(chat, '好友A', '🔥', lambda: None)
    assert await page.evaluate('clicks') == 0


@pytest.mark.asyncio
async def test_failure_marker_no_retry(page):
    chat = SafeChat(page)
    await chat._search_result('好友A')
    await page.evaluate('window.failSend=true')
    with pytest.raises(PageOperationError):
        await send_once(chat, '好友A', '🔥', lambda: None)
    assert await page.evaluate('clicks') == 1


@pytest.mark.asyncio
async def test_identity_changes_before_send_zero_triggers(page):
    chat = SafeChat(page)
    await chat._search_result('好友A')
    await page.locator('#profile').evaluate("e=>e.href='/user/uidBBBB'")
    with pytest.raises(PageOperationError):
        await send_once(chat, '好友A', '🔥', lambda: None)
    assert await page.evaluate('clicks') == 0


@pytest.mark.asyncio
async def test_real_browser_complete_dry_run_never_inputs_or_sends(page, monkeypatch):
    @asynccontextmanager
    async def browser(settings):
        yield SimpleNamespace(page=page)
    monkeypatch.setattr(runner, 'open_douyin', browser)
    task = SimpleNamespace(targets=[SimpleNamespace(name='好友A')], target_open_timeout_seconds=1)
    assert await runner.execute(None, task, [(SimpleNamespace(content='🔥'),)], [None]) == 0
    assert await page.evaluate('clicks') == 0
    assert await page.locator('[contenteditable]').inner_text() == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('profile_uid,allowed', [('uidAAAA', True), ('uidBBBB', False)])
@pytest.mark.parametrize('avatar_kind', ['image', 'background'])
async def test_avatar_popup_requires_exact_configured_uid(page, profile_uid, allowed, avatar_kind):
    await page.locator('#results a').evaluate('(e)=>e.remove()')
    await page.locator('#results .SearchPanelitembox').evaluate('''(row,kind) => {
        const avatar=document.createElement(kind==='image'?'img':'div');
        avatar.className='test-avatar';
        const resource='data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
        if(kind==='image') avatar.src=resource;
        else avatar.style.backgroundImage='url('+resource+')';
        avatar.style.width='24px';avatar.style.height='24px';row.prepend(avatar);
    }''', avatar_kind)
    await page.locator('#results .test-avatar').evaluate(
        "(image,uid)=>{image.onclick=()=>window.open('/user/'+uid,'_blank');}", profile_uid)
    chat = SafeChat(page, expected_uid='uidAAAA')
    if allowed:
        await chat._search_result('好友A')
        assert chat.resolved_uid == 'uidAAAA'
    else:
        with pytest.raises(PageOperationError):
            await chat._search_result('好友A')
    assert len(page.context.pages) == 1
    assert await page.evaluate('clicks') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('source,uid,allowed', [
    ('props','uidAAAA',True), ('owner','uidAAAA',True), ('attribute','uidAAAA',True),
    ('props','uidBBBB',False), ('array','uidAAAA',False), ('conflict','uidAAAA',False),
])
async def test_scoped_client_identity_requires_exact_subject(page, source, uid, allowed):
    await page.locator('#results a').evaluate('(e)=>e.remove()')
    await page.locator('#results .SearchPanelitembox').evaluate('''(row,config)=>{
        const user={nickname:'好友A',sec_uid:config.uid};
        if(config.source==='props')row.__reactProps$test={user};
        if(config.source==='owner')row.__reactFiber$test={memoizedProps:{},return:{memoizedProps:{user}}};
        if(config.source==='attribute')row.setAttribute('data-sec-uid',config.uid);
        if(config.source==='array')row.__reactProps$test={data:[user]};
        if(config.source==='conflict')row.__reactProps$test={user,item:{nickname:'好友A',sec_uid:'uidBBBB'}};
    }''', {'source':source,'uid':uid})
    chat=SafeChat(page,expected_uid='uidAAAA')
    if allowed:
        await chat._search_result('好友A')
        assert chat.resolved_uid=='uidAAAA'
    else:
        with pytest.raises(TargetCheckError):
            await chat._search_result('好友A')
    assert len(page.context.pages)==1
    assert await page.evaluate('clicks')==0
