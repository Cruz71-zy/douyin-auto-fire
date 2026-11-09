"""One-time setup: uniquely verify the fourth friend, then validate all targets without sending."""
import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_service import lock
from app.browser import open_douyin
from app.config import load_settings
from app.safe_main import check_login, load_safe_config, execute
from app.safe_chat import SafeChat
from app.selectors import DOUYIN_CHAT_URL
from app.local_runtime import LocalSessionStore


async def configure(data, name):
    raw = json.loads((data / 'config.json').read_text(encoding='utf-8'))
    if len(raw['targets']) != 3 or any(t['name'] == name for t in raw['targets']):
        raise ValueError('Expected three existing, different targets')
    store = LocalSessionStore(data / 'storage-state.json')
    settings = replace(load_settings(), headless=True, trace=False, cookie=None,
                       storage_state=json.dumps(store.load()))
    async with open_douyin(settings) as session:
        await session.page.goto(DOUYIN_CHAT_URL, wait_until='domcontentloaded', timeout=45000)
        await check_login(session.page)
        chat = SafeChat(session.page, expected_uid=None, timeout_ms=15000)
        await chat.open_target(name, retries=0)
        await chat.confirm_identity(name)
        uid = chat.resolved_uid
        if not uid or any(t.get('sec_uid') == uid for t in raw['targets']):
            raise ValueError('Recipient identity missing or duplicated')
        chat.expected_uid = uid
        await chat.confirm_identity(name)
    raw['targets'].insert(0, {'name':name,'sec_uid':uid,'messages':raw['targets'][0]['messages']})
    for key in ('DOUYIN_TARGET_UPDATES','DOUYIN_SESSION_KEY','DOUYIN_COOKIE','VERIFY_TARGET_UPDATES'):
        os.environ.pop(key, None)
    os.environ['DOUYIN_CONFIG'] = json.dumps(raw, ensure_ascii=False)
    os.environ['DOUYIN_STORAGE_STATE'] = json.dumps(store.load())
    settings, task, candidates, uids = load_safe_config()
    if await execute(settings, task, candidates, uids, dry_run=True, session_store=store):
        raise ValueError('Preflight failed')
    temporary = data / 'config.tmp'
    temporary.write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(data / 'config.json')
    print('Four targets verified and saved. Messages sent: 0', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    try:
        with lock(ROOT / 'data/service.lock') as acquired:
            if not acquired:
                raise RuntimeError('Service busy')
            asyncio.run(configure(ROOT / 'data', args.name.strip()))
    except Exception as exc:
        print('Setup not completed; no messages sent. Type:', type(exc).__name__)
        raise SystemExit(1)
