import json
import os
from scripts import desktop_service as service


def configuration(directory, count=4):
    targets = [{'name':f'fake-{i}','kind':'group' if i==0 else 'user'} for i in range(count)]
    (directory/'config.json').write_text(json.dumps({'targets':targets,'prevent_duplicates':True}),encoding='utf-8')
    (directory/'activation.json').write_text(json.dumps({'mode':'local','enabled':True}),encoding='utf-8')


def test_large_login_state_is_passed_as_path_not_environment_value(tmp_path,monkeypatch):
    configuration(tmp_path)
    monkeypatch.setattr(service.LocalSessionStore,'load',lambda self:{'large':'x'*100000})
    def load():
        assert os.environ['DOUYIN_STORAGE_STATE']==str((tmp_path/'storage-state.json').resolve())
        return None,None,None,('fakeAAAA','fakeBBBB','fakeCCCC','fakeDDDD')
    monkeypatch.setattr(service,'load_safe_config',load)
    async def execute(*args,**kwargs):
        assert kwargs['dry_run'] is True
        return 0
    monkeypatch.setattr(service,'execute',execute)
    assert service.run(tmp_path,False)==0
    assert json.loads((tmp_path/'status.json').read_text())['status']=='verified_no_send'


def test_incomplete_list_cannot_reach_sender(tmp_path,monkeypatch):
    configuration(tmp_path,3)
    monkeypatch.setattr(service,'due',lambda now:True)
    monkeypatch.setattr(service,'notify',lambda *args:None)
    monkeypatch.setattr(service,'load_safe_config',lambda: (_ for _ in ()).throw(AssertionError('sender must not load')))
    assert service.run(tmp_path,True)==2
    assert json.loads((tmp_path/'status.json').read_text())['status']=='setup_required'


def test_successful_day_skips_all_browser_and_config_work(tmp_path,monkeypatch):
    journal=service.LocalJournal(tmp_path/'daily.sqlite3')
    day=service.datetime.now(service.ZoneInfo('Asia/Shanghai')).date().isoformat()
    journal.reserve(day)
    journal.finish(day,'success')
    (tmp_path/'activation.json').write_text(json.dumps({'mode':'local','enabled':True}),encoding='utf-8')
    monkeypatch.setattr(service,'due',lambda now:True)
    monkeypatch.setattr(service,'load_safe_config',lambda: (_ for _ in ()).throw(AssertionError('must skip')))
    assert service.run(tmp_path,True)==0
