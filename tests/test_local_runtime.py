from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import pytest
from app.local_runtime import LocalJournal, due


def test_concurrent_reservation_has_only_one_winner(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    LocalJournal(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: LocalJournal(path).reserve('2026-11-09'), range(8)))
    assert results.count(True) == 1


def test_interruption_reservation_and_unknown_block_retries(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    j = LocalJournal(path)
    assert j.reserve('2026-11-09')
    assert LocalJournal(path).contains('2026-11-09')
    assert not LocalJournal(path).reserve('2026-11-09')
    j.finish('2026-11-09', 'unknown')
    assert not j.reserve('2026-11-09')
    with pytest.raises(ValueError):
        j.finish('2026-11-09', 'success')


def test_success_and_alerts_survive_restart(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    j = LocalJournal(path)
    j.reserve('2026-11-09')
    j.finish('2026-11-09', 'success')
    assert LocalJournal(path).status('2026-11-09') == 'success'
    assert j.alert_once('2026-11-09', 'login_required')
    assert not LocalJournal(path).alert_once('2026-11-09', 'login_required')


def test_bounded_attempts_and_new_day(tmp_path):
    j = LocalJournal(tmp_path / 'journal.sqlite3')
    assert [j.attempt('2026-11-09') for _ in range(4)] == [True, True, True, False]
    assert j.attempt('2026-11-10')


@pytest.mark.parametrize('hour,minute,expected', [(7,29,False),(7,30,True),(18,0,True),(23,29,True),(23,30,False)])
def test_schedule_window(hour, minute, expected):
    assert due(datetime(2026,11,9,hour,minute)) is expected
