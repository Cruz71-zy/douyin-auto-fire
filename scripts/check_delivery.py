"""Read-only delivery checks; notify the repository owner once per incident."""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
from zoneinfo import ZoneInfo


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class API:
    def __init__(self):
        self.repo = os.environ['GITHUB_REPOSITORY']
        self.root = 'https://api.github.com/repos/' + self.repo
        self.token = os.environ['GH_MONITOR_TOKEN']

    def request(self, method, path, data=None, raw=False):
        for attempt in range(3 if method == 'GET' else 1):
            try:
                req = Request(self.root + path, method=method,
                    data=None if data is None else json.dumps(data).encode(), headers={
                    'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                    'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2026-03-10'})
                try:
                    with build_opener(NoRedirect).open(req, timeout=30) as response:
                        body = response.read()
                except HTTPError as error:
                    if not raw or error.code not in (301, 302, 303, 307):
                        raise
                    location = error.headers['Location']
                    if urlparse(location).scheme != 'https':
                        raise RuntimeError('Invalid log redirect') from None
                    # Download using a fresh request WITHOUT the repository token.
                    with urlopen(Request(location), timeout=30) as response:
                        body = response.read()
                return body.decode('utf-8', errors='replace') if raw else json.loads(body)
            except Exception:
                if attempt == (2 if method == 'GET' else 0):
                    raise RuntimeError('GitHub check request failed') from None
                time.sleep(1)


def run_day(run):
    return datetime.fromisoformat(run['created_at'].replace('Z', '+00:00')).astimezone(
        ZoneInfo('Asia/Shanghai')).date().isoformat()


def log_problem(log):
    if '私密好友配置检查通过：共 4 位' not in log:
        return '四目标配置未确认'
    if '好友 3 位；群聊 1 个；每日消息池 14 条' not in log:
        return '好友、群或消息池数量不符合配置'
    sent = re.findall(r'目标#(\d+)：发送成功（新消息及页面发送状态已确认）', log)
    if sorted(sent) != ['1', '2', '3', '4']:
        return '未确认全部四个目标各发送成功一次'
    if '最终状态：success' not in log:
        return '发送任务未报告完整成功'
    if '最新登录状态已加密保存' not in log:
        return '最新登录态未确认保存'
    return None


def check_run(api, run):
    if run.get('status') != 'completed' or run.get('conclusion') != 'success':
        return '发送任务未成功结束（失败、取消、跳过或仍在运行）'
    jobs = api.request('GET', f"/actions/runs/{run['id']}/jobs?per_page=100")['jobs']
    jobs = [job for job in jobs if job['name'] == 'send']
    if len(jobs) != 1 or jobs[0]['conclusion'] != 'success':
        return '发送作业缺失或未成功'
    problem = log_problem(api.request('GET', f"/actions/jobs/{jobs[0]['id']}/logs", raw=True))
    if problem:
        return problem
    record = api.request('GET', '/contents/daily-state.json?ref=douyin-send-state')
    journal = json.loads(base64.b64decode(record['content']))
    day = journal['days'].get(run_day(run), {})
    if day != {'status': 'success', 'run_id': str(run['id'])}:
        return '当天持久成功记录与发送任务不一致'
    return None


def notify(api, key, reason, run=None, test=False):
    marker = '<!-- douyin-delivery-monitor:' + key + ' -->'
    # Include closed issues: acknowledging/closing an alert must not resend it.
    page = 1
    while True:
        issues = api.request('GET', f'/issues?state=all&per_page=100&page={page}')
        for issue in issues:
            if marker in (issue.get('body') or ''):
                return issue['html_url']
        if len(issues) < 100:
            break
        page += 1
    url = run['html_url'] if run else f'https://github.com/{api.repo}/actions/workflows/send.yml'
    body = (marker + '\n\n' + ('这是一条通知通道测试，不代表发送失败。' if test else reason)
        + '\n\n[查看任务](' + url + ')\n\n请检查任务日志；如出现登录或验证码提示，请本人完成登录维护。'
        + '\n\n提醒不会自动重发消息，也不会重置当天发送记录。')
    issue = api.request('POST', '/issues', {
        'title': ('[通知测试] ' if test else '[续火花异常] ') + key,
        'body': body, 'assignees': [api.repo.split('/')[0]]})
    if api.repo.split('/')[0] not in [x['login'] for x in issue.get('assignees', [])]:
        raise RuntimeError('Alert assignment was not confirmed')
    if test:
        api.request('PATCH', f"/issues/{issue['number']}", {'state': 'closed'})
    return issue['html_url']


def main():
    api = API()
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if os.getenv('TEST_NOTIFICATION') == 'true':
        print('Notification test:', notify(api, 'test-' + today, '', test=True))
    if os.environ['GITHUB_EVENT_NAME'] == 'workflow_run':
        run = event['workflow_run']
        if (run['event'] != 'schedule' or run['head_branch'] != 'main'
                or run['repository']['full_name'] != api.repo):
            print('Non-daily run: skipped')
            return
        runs = [run]
    else:
        try:
            runs = api.request('GET', '/actions/workflows/send.yml/runs?event=schedule&branch=main&per_page=100')['workflow_runs']
        except Exception:
            print('Alert:', notify(api, today + '-check-unavailable', '检查接口不可用，无法确认今天的发送结果。'))
            return
        runs = [run for run in runs if run_day(run) == today]
        if not runs:
            print('Alert:', notify(api, today + '-missing', '截至检查时，今天的每日发送任务尚未启动。'))
            return
    for run in runs:
        key = run_day(run) + '-run-' + str(run['id'])
        try:
            problem = check_run(api, run)
        except Exception:
            problem = '检查接口或日志不可用，无法确认发送成功；请人工检查'
        if problem:
            print('Alert:', notify(api, key, problem, run))
        else:
            print('All four deliveries and persisted state verified:', run['id'])


if __name__ == '__main__':
    main()
