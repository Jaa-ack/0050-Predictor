from datetime import datetime,timedelta
import importlib.util
import json
import threading
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


def run_closing_backlog(tmp_path,*,drain_seconds=90,stop_after=None):
    m=module();start=datetime(2026,11,16,9,30,tzinfo=ZoneInfo('Asia/Taipei'))
    clock=[start];seen=[];stop=threading.Event();closed=[];gaps=[]
    class Store:
        protocol={'dates':['2026-11-16'],'collection_start':'09:30:00','collection_end':'09:30:01'}
        symbols=['2330']
        def ingest(self,message,received_at):
            seen.append((message['sequence'],received_at));clock[0]+=timedelta(seconds=.03)
            if stop_after and len(seen)==stop_after:stop.set()
            return True
        def mark_gap(self,a,b,reason):gaps.append((a,b,reason))
        def mark_coverage(self,a,b):pass
        def flush(self):pass
    class Feed:
        expected={('2330','trades')};subscriptions=expected;ready=True
        def __init__(self,symbols):self.last_received_ns=m.reference.ns(start)
        def connect(self,receive,disconnect):
            for i in range(100):receive({'sequence':i})
        def close(self):closed.append(True)
    result=m.collect(tmp_path,Store(),clock=lambda:clock[0],monotonic=lambda:(clock[0]-start).total_seconds(),
        sleep=lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)),feed_factory=Feed,
        stop=stop,drain_seconds=drain_seconds)
    return result,seen,closed,gaps,start


def test_window_end_drains_accepted_messages_with_their_original_receipt_times(tmp_path):
    result,seen,closed,gaps,start=run_closing_backlog(tmp_path)
    assert seen==[(i,start) for i in range(100)] and closed==[True]
    assert result['state']=='day_collection_finished'
    receipt=json.loads((tmp_path/'collector_closeout.json').read_text())
    assert receipt['complete'] and receipt['remaining_messages']==0
    assert receipt['received_messages']==receipt['processed_messages']==100
    assert json.loads((tmp_path/'collector_status.json').read_text())['backlog']==0


def test_exhausted_drain_budget_retains_an_incomplete_receipt_and_gap(tmp_path):
    result,seen,closed,gaps,start=run_closing_backlog(tmp_path,drain_seconds=.1)
    receipt=json.loads((tmp_path/'collector_closeout.json').read_text())
    assert result['state']=='collection_incomplete' and not receipt['complete']
    assert receipt['remaining_messages']>0 and gaps and closed==[True]
    assert receipt['received_messages']==len(seen)+receipt['remaining_messages']==100


def test_stop_request_drains_data_but_never_claims_the_capture_window_finished(tmp_path):
    result,seen,closed,gaps,start=run_closing_backlog(tmp_path,stop_after=3)
    assert len(seen)==100 and result['state']=='stopped'
    lines=[json.loads(x) for x in (tmp_path/'collector_closeouts.jsonl').read_text().splitlines()]
    assert len(lines)==1 and lines[0]['complete'] and lines[0]['state']=='stopped'
