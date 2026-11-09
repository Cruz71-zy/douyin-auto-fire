"""Local desktop entry. Default is verification; --tick is the scheduled sender."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if (ROOT.parent / 'pw-browsers').is_dir():
    os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(ROOT.parent / 'pw-browsers'))
sys.path.insert(0, str(ROOT))
from app.local_runtime import LocalJournal, LocalSessionStore, due
from app.safe_main import load_safe_config, execute


@contextmanager
def lock(path):
    handle = path.open('a+b')
    handle.seek(0)
    handle.write(b'0')
    handle.flush()
    handle.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.close()


def write_status(directory, status, day):
    temporary = directory / 'status.tmp'
    temporary.write_text(json.dumps({'date': day, 'status': status,
        'checked_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(directory / 'status.json')


def notify(journal, day, reason):
    if not journal.alert_once(day, reason):
        return
    if os.name == 'nt':
        description = {
            'setup_required': '名单配置未完成，暂未发送。',
            'migration_required': '运行方式尚未完成迁移，暂未发送。',
            'login_required': '登录已失效，需要本人扫码登录。',
            'verification_required': '需要本人完成抖音安全验证。',
            'preflight_failed': '聊天页面或好友身份检查失败，暂未发送。',
            'delivery_uncertain_do_not_retry': '今天发送结果不明，已停止重试，请查看聊天记录。',
            'unknown': '今天发送结果不明，已停止重试，请查看聊天记录。',
        }.get(reason, '自动发送需要检查，请查看本机运行状态。')
        subprocess.Popen(['C:/Windows/System32/msg.exe', os.environ.get('USERNAME', 'HP'),
            '/time:120', '抖音续火花：' + description],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def run(directory, tick):
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    day = now.date().isoformat()
    journal = LocalJournal(directory / 'daily.sqlite3')
    with lock(directory / 'service.lock') as acquired:
        if not acquired:
            return 0
        if tick and not due(now):
            return 0
        if tick:
            try:
                activation = json.loads((directory / 'activation.json').read_text(encoding='utf-8'))
                if activation != {'mode': 'local', 'enabled': True}:
                    raise ValueError
            except Exception:
                write_status(directory, 'migration_required', day)
                notify(journal, day, 'migration_required')
                return 2
        if tick and journal.contains(day):
            status = journal.status(day)
            write_status(directory, status, day)
            if status != 'success':
                notify(journal, day, 'delivery_uncertain_do_not_retry')
            return 0
        try:
            config = directory / 'config.json'
            raw = json.loads(config.read_text(encoding='utf-8'))
            if len(raw.get('targets', [])) != 4 or raw.get('prevent_duplicates') is not True:
                raise ValueError('Four verified targets required')
            if sum(t.get('kind', 'user') == 'group' for t in raw['targets']) != 1:
                raise ValueError('Expected three friends and one group')
            # Account data stay local; inherited cloud values cannot override them.
            for name in ('DOUYIN_TARGET_UPDATES', 'DOUYIN_SESSION_KEY', 'DOUYIN_COOKIE', 'VERIFY_TARGET_UPDATES'):
                os.environ.pop(name, None)
            os.environ['DOUYIN_CONFIG'] = json.dumps(raw, ensure_ascii=False)
            store = LocalSessionStore(directory / 'storage-state.json')
            store.load()  # Validate before exposing the path to the config loader.
            os.environ['DOUYIN_STORAGE_STATE'] = str(store.path.resolve())
            settings, task, candidates, uids = load_safe_config()
            if any(not value for value in uids):
                raise ValueError('Missing fixed recipient identity')
        except Exception:
            write_status(directory, 'setup_required', day)
            notify(journal, day, 'setup_required')
            return 2
        if tick and not journal.attempt(day):
            write_status(directory, 'attempt_limit', day)
            return 2
        logging.getLogger('douyin_sender').disabled = True
        try:
            result = asyncio.run(execute(settings, task, candidates, uids,
                dry_run=not tick, journal=journal, session_store=store))
            status = journal.status(day) if tick else 'verified_no_send'
            if result or tick and status != 'success':
                status = status or 'preflight_failed'
                notify(journal, day, status)
                write_status(directory, status, day)
                return 1
        except Exception as exc:
            from app.browser import AuthenticationError, RiskControlError
            status = ('login_required' if isinstance(exc, AuthenticationError) else
                'verification_required' if isinstance(exc, RiskControlError) else 'preflight_failed')
            notify(journal, day, status)
            write_status(directory, status, day)
            return 1
        write_status(directory, status, day)
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=ROOT / 'data')
    parser.add_argument('--tick', action='store_true', help='Authorized scheduled daily sending')
    args = parser.parse_args()
    raise SystemExit(run(args.data.resolve(), args.tick))
