import base64
import json
from scripts.check_delivery import check_run, log_problem, notify, run_day


def good_log():
    return ('私密好友配置检查通过：共 4 位\n好友 3 位；群聊 1 个；每日消息池 14 条\n'
        '最新登录状态已加密保存\n' + '\n'.join(
        f'目标#{i}：发送成功（新消息及页面发送状态已确认）' for i in range(1, 5))
        + '\n最终状态：success')


def test_requires_all_four_once_and_persistence():
    log = good_log()
    assert log_problem(log) is None
    for part in ['目标#4：发送成功（新消息及页面发送状态已确认）', '最新登录状态已加密保存',
                 '最终状态：success', '好友 3 位；群聊 1 个；每日消息池 14 条']:
        assert log_problem(log.replace(part, ''))
    assert log_problem(log + '\n目标#1：发送成功（新消息及页面发送状态已确认）')
    assert log_problem(log.replace('最终状态：success', '最终状态：dry_run_success'))


def test_china_day():
    assert run_day({'created_at': '2026-10-28T23:50:00Z'}) == '2026-10-29'


def test_failed_or_incomplete_run_never_passes():
    for status, conclusion in [('completed', 'failure'), ('completed', 'cancelled'), ('in_progress', None)]:
        assert check_run(None, {'status': status, 'conclusion': conclusion})


def test_journal_must_match_run():
    class Fake:
        def request(self, method, path, raw=False):
            if path.endswith('jobs?per_page=100'):
                return {'jobs': [{'name': 'send', 'conclusion': 'success', 'id': 2}]}
            if raw:
                return good_log()
            return {'content': base64.b64encode(json.dumps({'days': {
                '2026-10-29': {'status': 'success', 'run_id': '999'}}}).encode()).decode()}
    assert check_run(Fake(), {'id': 1, 'status': 'completed', 'conclusion': 'success',
                              'created_at': '2026-10-29T02:00:00Z'})


def test_closed_alert_is_not_duplicated():
    class Fake:
        repo = 'owner/repo'
        def request(self, method, path, data=None):
            assert method == 'GET'
            return [{'body': '<!-- douyin-delivery-monitor:day-run-1 -->',
                     'html_url': 'existing', 'state': 'closed'}]
    assert notify(Fake(), 'day-run-1', 'failure') == 'existing'


def test_alert_assigned_without_private_logs():
    class Fake:
        repo = 'owner/repo'
        def request(self, method, path, data=None):
            if method == 'GET':
                return []
            assert data['assignees'] == ['owner']
            assert '提醒不会自动重发消息' in data['body']
            return {'html_url': 'new', 'assignees': [{'login': 'owner'}]}
    assert notify(Fake(), 'day-run-1', '未确认全部四个目标各发送成功一次') == 'new'
