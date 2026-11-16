from datetime import datetime,timedelta
import importlib.util
import json
from pathlib import Path
from zoneinfo import ZoneInfo


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_collector.py'
    assert path.is_file(),'Time-sliced cloud collector is missing'
    spec=importlib.util.spec_from_file_location('cloud_collector',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def test_busy_backlog_yields_to_heartbeat_and_flush_without_dropping_observations(tmp_path):
    m=module();clock=[datetime(2026,11,16,9,30,tzinfo=ZoneInfo('Asia/Taipei'))]
    start=clock[0];seen=[];gaps=[];flushes=[]
    class Store:
        protocol={'dates':['2026-11-16'],'collection_start':'09:30:00','collection_end':'09:30:06'}
        symbols=['2330'];decisions={'2026-11-16':0}
        def ingest(self,message,received_at):
            seen.append(message['sequence']);clock[0]+=timedelta(seconds=.01)
        def mark_gap(self,a,b,reason):gaps.append(reason)
        def mark_coverage(self,a,b):flushes.append(clock[0])
        def flush(self):flushes.append(clock[0])
    class Feed:
        expected={('2330','trades')};subscriptions=expected;ready=True;failed=None
        def __init__(self,symbols):self.last_received_ns=m.reference.ns(clock[0])
        def connect(self,on_message,on_disconnect):
            for i in range(300):on_message({'sequence':i})
        def close(self):pass
    result=m.collect(tmp_path,Store(),clock=lambda:clock[0],monotonic=lambda:(clock[0]-start).total_seconds(),
        sleep=lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)),feed_factory=Feed)
    assert result['state']=='day_collection_finished'
    assert seen==list(range(300)) and not gaps
    status=json.loads((tmp_path/'collector_status.json').read_text())
    assert status['stream_ready'] is True and status['backlog']==0
    assert len(flushes)>=4


def test_provider_fatal_remains_blocking_without_erasing_evidence(tmp_path):
    m=module();existing=tmp_path/'fatal.json';existing.write_text('{"code":"provider_error"}')
    class Store:
        protocol={'dates':['2026-11-16'],'collection_start':'09:30:00','collection_end':'10:15:00'}
    result=m.collect(tmp_path,Store(),clock=lambda:datetime(2026,11,16,10,tzinfo=ZoneInfo('Asia/Taipei')))
    assert result['state']=='blocked_by_provider_error'
    assert json.loads(existing.read_text())['code']=='provider_error'
