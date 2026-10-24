"""Exercise login-state persistence in Chromium with every request fulfilled locally."""
import pytest
from playwright.async_api import async_playwright

from app.auth_state import normalize_storage_state


CHAT_URL = "https://www.douyin.com/chat"
LOCAL_HTML = '<!doctype html><meta charset="utf-8"><title>Local auth fixture</title>'


@pytest.mark.asyncio
async def test_complete_login_state_survives_a_new_browser_context():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            source = await browser.new_context(service_workers="block")
            await source.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body=LOCAL_HTML,
            ))
            await source.add_cookies([{
                "name": "sessionid",
                "value": "FAKE_COOKIE_FOR_LOCAL_TEST_ONLY",
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            }])
            page = await source.new_page()
            await page.goto(CHAT_URL)
            await page.evaluate("""async () => {
                localStorage.setItem('auth-fixture', 'FAKE_LOCAL_STORAGE_VALUE');
                await new Promise((resolve, reject) => {
                    const request = indexedDB.open('local-auth-fixture', 1);
                    request.onerror = () => reject(request.error);
                    request.onupgradeneeded = () => request.result.createObjectStore('tokens');
                    request.onsuccess = () => {
                        const db = request.result;
                        const tx = db.transaction('tokens', 'readwrite');
                        tx.objectStore('tokens').put('FAKE_INDEXED_DB_TOKEN', 'current');
                        tx.oncomplete = () => { db.close(); resolve(); };
                        tx.onerror = () => { db.close(); reject(tx.error); };
                        tx.onabort = () => { db.close(); reject(tx.error); };
                    };
                });
            }""")
            snapshot = normalize_storage_state(await source.storage_state(indexed_db=True))
            await source.close()

            restored = await browser.new_context(storage_state=snapshot, service_workers="block")
            await restored.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html", body=LOCAL_HTML,
            ))
            restored_page = await restored.new_page()
            await restored_page.goto(CHAT_URL)

            cookies = await restored.cookies(CHAT_URL)
            assert any(cookie["name"] == "sessionid"
                       and cookie["value"] == "FAKE_COOKIE_FOR_LOCAL_TEST_ONLY"
                       and cookie["httpOnly"] is True for cookie in cookies)
            assert await restored_page.evaluate("localStorage.getItem('auth-fixture')") == "FAKE_LOCAL_STORAGE_VALUE"
            token = await restored_page.evaluate("""() => new Promise((resolve, reject) => {
                const request = indexedDB.open('local-auth-fixture', 1);
                request.onerror = () => reject(request.error);
                request.onupgradeneeded = () => {
                    request.transaction.abort();
                    reject(new Error('Expected database was not restored'));
                };
                request.onsuccess = () => {
                    const db = request.result;
                    try {
                        const tx = db.transaction('tokens', 'readonly');
                        const read = tx.objectStore('tokens').get('current');
                        read.onsuccess = () => resolve(read.result);
                        read.onerror = () => reject(read.error);
                        tx.oncomplete = () => db.close();
                        tx.onerror = () => { db.close(); reject(tx.error); };
                    } catch (error) {
                        db.close();
                        reject(error);
                    }
                };
            })""")
            assert token == "FAKE_INDEXED_DB_TOKEN"
            await restored.close()
        finally:
            await browser.close()
