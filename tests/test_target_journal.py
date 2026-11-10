import pytest
import asyncio
from app.models import TaskConfig,Target,Message
from app.local_runtime import receipt_identity
from scripts import desktop_service as service
from app.local_runtime import LocalJournal,TargetJournal


def prepared(tmp_path):
    j=LocalJournal(tmp_path/'daily.sqlite3')
    j.prepare_receipts('2026-11-10',['a','b','c','d'])
    return j,TargetJournal(j,'a')


def test_pretrigger_failure_can_retry_but_trigger_unknown_cannot(tmp_path):
    j,t=prepared(tmp_path)
    assert t.reserve('2026-11-10')
    t.finish('2026-11-10','unknown')
    assert not t.contains('2026-11-10')
    assert t.reserve('2026-11-10')
    t.before_trigger('2026-11-10')
    t.finish('2026-11-10','unknown')
    assert t.contains('2026-11-10')
    assert not t.reserve('2026-11-10')


def test_success_never_retried_and_other_targets_remain_available(tmp_path):
    j,t=prepared(tmp_path)
    t.reserve('2026-11-10');t.before_trigger('2026-11-10');t.finish('2026-11-10','success')
    assert t.contains('2026-11-10')
    assert not t.reserve('2026-11-10')
    assert not TargetJournal(j,'b').contains('2026-11-10')


def test_restart_after_trigger_boundary_never_resends(tmp_path):
    j,t=prepared(tmp_path)
    t.reserve('2026-11-10');t.before_trigger('2026-11-10')
    restarted=TargetJournal(LocalJournal(j.path),'a')
    assert restarted.contains('2026-11-10')


def test_legacy_unknown_and_changed_plan_cannot_be_reset(tmp_path):
    j=LocalJournal(tmp_path/'daily.sqlite3')
    j.reserve('2026-11-10');j.finish('2026-11-10','unknown')
    with pytest.raises(ValueError):j.prepare_receipts('2026-11-10',['a','b','c','d'])
    assert j.status('2026-11-10')=='unknown'


def test_all_four_success_required_for_aggregate_success(tmp_path):
    j,t=prepared(tmp_path)
    for identity in ['a','b','c']:
        target=TargetJournal(j,identity)
        target.reserve('2026-11-10');target.before_trigger('2026-11-10');target.finish('2026-11-10','success')
    assert j.summarize_receipts('2026-11-10')=='unknown'
    target=TargetJournal(j,'d')
    target.reserve('2026-11-10');target.before_trigger('2026-11-10');target.finish('2026-11-10','success')
    assert j.summarize_receipts('2026-11-10')=='success'


def plan():
    message=Message(type='text',content='fake message')
    targets=tuple(Target(name=f'fake-{i}',messages=(message,)) for i in range(4))
    task=TaskConfig(task_id='test',timezone='Asia/Shanghai',targets=targets,stickers={},
                    interval_min=0,interval_max=0,continue_on_error=False,prevent_duplicates=True)
    uids=tuple(f'fakeUID{i}' for i in range(4))
    return task,tuple((message,) for _ in targets),uids


def test_uncertain_target_does_not_block_other_unsent_targets(tmp_path,monkeypatch):
    task,candidates,uids=plan()
    journal=LocalJournal(tmp_path/'daily.sqlite3')
    ids=[receipt_identity(t.kind,u) for t,u in zip(task.targets,uids)]
    journal.prepare_receipts('2026-11-10',ids)
    first=TargetJournal(journal,ids[0]);first.reserve('2026-11-10');first.before_trigger('2026-11-10');first.finish('2026-11-10','unknown')
    called=[]
    async def execute(settings,subtask,candidates,uids,**kw):
        called.append(subtask.targets[0].name)
        adapter=kw['journal'];adapter.reserve('2026-11-10');adapter.before_trigger('2026-11-10');adapter.finish('2026-11-10','success')
        return 0
    monkeypatch.setattr(service,'execute',execute)
    assert asyncio.run(service.deliver_targets(None,task,candidates,uids,journal,None,'2026-11-10'))==1
    assert called==['fake-1','fake-2','fake-3']
    assert list(journal.receipt_statuses('2026-11-10').values()).count('success')==3


def test_proven_pretrigger_failure_is_repaired_without_repeating_success(tmp_path,monkeypatch):
    task,candidates,uids=plan()
    journal=LocalJournal(tmp_path/'daily.sqlite3')
    calls=[]
    async def execute(settings,subtask,candidates,uids,**kw):
        if kw['dry_run']:return 0
        adapter=kw['journal'];name=subtask.targets[0].name;calls.append(name)
        assert adapter.reserve('2026-11-10')
        if name=='fake-0' and calls.count(name)==1:
            adapter.finish('2026-11-10','unknown')
            return 1
        adapter.before_trigger('2026-11-10');adapter.finish('2026-11-10','success')
        return 0
    async def no_sleep(*args):pass
    monkeypatch.setattr(service,'execute',execute)
    monkeypatch.setattr(service.asyncio,'sleep',no_sleep)
    assert asyncio.run(service.deliver_targets(None,task,candidates,uids,journal,None,'2026-11-10'))==0
    assert calls==['fake-0','fake-0','fake-1','fake-2','fake-3']
    assert journal.status('2026-11-10')=='success'
