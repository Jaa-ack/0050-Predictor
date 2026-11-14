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


def test_outside_capture_window_never_starts_provider_process(tmp_path):
    m=module();setup(tmp_path)
    def forbidden(args):raise AssertionError('Provider must not start')
    result=m.supervise(tmp_path,clock=lambda:datetime(2026,11,16,10,16,tzinfo=ZoneInfo('Asia/Taipei')),launch=forbidden)
    assert result['state']=='outside_collection_window'


def test_report_work_is_deferred_around_forecast_on_small_shared_host(tmp_path):
    m=module();p=setup(tmp_path);tz=ZoneInfo('Asia/Taipei')
    assert not m.report_ready(p,datetime(2026,11,16,9,59,tzinfo=tz))
    assert not m.report_ready(p,datetime(2026,11,16,10,5,tzinfo=tz))
    assert m.report_ready(p,datetime(2026,11,16,10,6,tzinfo=tz))
