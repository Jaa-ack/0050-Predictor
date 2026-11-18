from datetime import datetime,timedelta
import importlib.util
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_runtime.py'
    assert path.exists(), 'Bounded collector supervision is missing'
    spec=importlib.util.spec_from_file_location('cloud_runtime',path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


def setup(tmp_path):
    p=dict(dates=['2026-11-16'],collection_start='09:30:00',collection_end='10:15:00',minimum_free_bytes=1024)
    (tmp_path/'protocol.json').write_text(json.dumps(p));return p


class Child:
    def __init__(self):self.stopped=False
    def poll(self):return 0 if self.stopped else None
    def terminate(self):self.stopped=True
    def wait(self,timeout):return 0
    def kill(self):self.stopped=True


def test_capture_stops_after_all_labels_mature_without_changing_worker_rules(tmp_path):
    m=module();setup(tmp_path);clock=[datetime(2026,11,16,10,14,59,tzinfo=ZoneInfo('Asia/Taipei'))];child=Child();calls=[]
    def launch(args):calls.append(args);return child
    result=m.supervise(tmp_path,clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+timedelta(seconds=s)),launch=launch,free_bytes=lambda:10000)
    assert result['state']=='planned_window_end' and child.stopped
    assert calls[0][-2:]==['--mode','collect']
    assert calls[0][1:3]==['-m','twquant.live_market']
    assert json.loads((tmp_path/'cloud_collect_status.json').read_text())['state']=='planned_window_end'


def test_storage_reserve_stops_only_collector_and_never_deletes_data(tmp_path):
    m=module();setup(tmp_path);existing=tmp_path/'pilot.sqlite3';existing.write_bytes(b'preserve-all-records')
    clock=datetime(2026,11,16,10,tzinfo=ZoneInfo('Asia/Taipei'));child=Child();free=iter([10000,1])
    result=m.supervise(tmp_path,clock=lambda:clock,sleep=lambda s:None,launch=lambda args:child,free_bytes=lambda:next(free))
    assert result['state']=='storage_reserve_reached' and child.stopped
    assert existing.read_bytes()==b'preserve-all-records'


def test_cloud_window_end_waits_for_accepted_data_to_drain_before_sending_a_signal(tmp_path,monkeypatch):
    m=module();setup(tmp_path);clock=[datetime(2026,11,16,10,14,59,tzinfo=ZoneInfo('Asia/Taipei'))]
    entry=Path(m.__file__).with_name('collector_runtime.py');monkeypatch.setattr(m,'__file__',str(tmp_path/'resource_guard.py'))
    (tmp_path/'collector_runtime.py').write_text('# trusted test collector')
    class DrainingChild(Child):
        def wait(self,timeout):
            assert timeout>=90 and not self.stopped
            self.stopped=True;return 0
        def terminate(self):raise AssertionError('A natural window end must allow draining first')
    result=m.supervise(tmp_path,clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+timedelta(seconds=s)),
        launch=lambda args:DrainingChild(),free_bytes=lambda:10000,cloud_runtime=True)
    assert result['state']=='planned_window_end' and not result['forced_termination']


def test_outside_capture_window_never_starts_provider_process(tmp_path):
    m=module();setup(tmp_path)
    def forbidden(args):raise AssertionError('Provider must not start')
    result=m.supervise(tmp_path,clock=lambda:datetime(2026,11,16,10,16,tzinfo=ZoneInfo('Asia/Taipei')),launch=forbidden)
    assert result['state']=='outside_collection_window'


def test_report_work_is_deferred_around_forecast_on_small_shared_host(tmp_path):
    m=module();p=setup(tmp_path);tz=ZoneInfo('Asia/Taipei')
    assert not m.report_ready(p,datetime(2026,11,16,9,59,tzinfo=tz))
    assert not m.report_ready(p,datetime(2026,11,16,10,5,tzinfo=tz))
    assert m.report_ready(p,datetime(2026,11,16,10,18,tzinfo=tz))


def test_reports_yield_for_the_entire_capture_and_bounded_drain_window(tmp_path):
    m=module();p=setup(tmp_path);tz=ZoneInfo('Asia/Taipei')
    for hour,minute,second in [(9,28,0),(9,35,0),(10,10,0),(10,17,29)]:
        assert not m.report_ready(p,datetime(2026,11,16,hour,minute,second,tzinfo=tz))
    assert m.report_ready(p,datetime(2026,11,16,10,17,30,tzinfo=tz))
    assert m.report_ready(p,datetime(2026,11,15,9,35,tzinfo=tz))


def test_report_start_waits_until_the_backup_releases_its_lock(tmp_path,monkeypatch):
    import fcntl
    m=module();setup(tmp_path)
    monkeypatch.setattr(m,'now',lambda:datetime(2026,11,16,10,18,tzinfo=ZoneInfo('Asia/Taipei')))
    with (tmp_path/'backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert m.main(['--root',str(tmp_path),'--phase','report-ready'])==1
        assert not (tmp_path/'report_input.json').exists()
    assert m.main(['--root',str(tmp_path),'--phase','report-ready'])==0
    assert (tmp_path/'report_input.json').exists()


def test_active_report_quiet_condition_is_bounded_to_declared_forecast_window(tmp_path,monkeypatch):
    m=module();setup(tmp_path);tz=ZoneInfo('Asia/Taipei')
    monkeypatch.setattr(m,'now',lambda:datetime(2026,11,16,9,58,tzinfo=tz))
    assert m.main(['--root',str(tmp_path),'--phase','report-quiet'])==0
    monkeypatch.setattr(m,'now',lambda:datetime(2026,11,16,10,18,tzinfo=tz))
    assert m.main(['--root',str(tmp_path),'--phase','report-quiet'])==1
    monkeypatch.setattr(m,'now',lambda:datetime(2026,11,15,9,58,tzinfo=tz))
    assert m.main(['--root',str(tmp_path),'--phase','report-quiet'])==1


def test_journal_profile_launches_only_the_lightweight_receiver(tmp_path,monkeypatch):
    m=module();setup(tmp_path);monkeypatch.setattr(m,'__file__',str(tmp_path/'resource_guard.py'))
    (tmp_path/'receipt_journal.py').write_text('# trusted helper');clock=[datetime(2026,11,16,10,14,59,tzinfo=ZoneInfo('Asia/Taipei'))];calls=[];child=Child()
    result=m.supervise(tmp_path,cloud_runtime=True,receipt_journal=True,clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+timedelta(seconds=s)),launch=lambda args:calls.append(args) or child,free_bytes=lambda:10000)
    assert calls[0][1]==str(tmp_path/'receipt_journal.py') and calls[0][-2:]==['--phase','capture']


def test_failed_worker_propagates_a_nonzero_exit_status(tmp_path,monkeypatch):
    m=module();setup(tmp_path)
    monkeypatch.setattr(m,'supervise',lambda *a,**k:dict(state='worker_exited',worker_returncode=1))
    assert m.main(['--root',str(tmp_path),'--phase','collect'])==1
