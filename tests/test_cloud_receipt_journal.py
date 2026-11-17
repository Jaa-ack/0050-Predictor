from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from test_live_pilot import protocol,stamp,message
from twquant.live_pilot import PilotStore

SOURCE=Path(__file__).parents[1]/'src/twquant'


def module():
    path=SOURCE.parents[1]/'scripts/cloud_receipt_journal.py'
    assert path.exists(),'Durable receipt journal is missing'
    spec=importlib.util.spec_from_file_location('journal_ops',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_frozen_encoder_matches_reference_and_excludes_private_fields(tmp_path):
    m=module();p=protocol();encoder=m.FrozenAdapter(SOURCE,p);reference=PilotStore(tmp_path/'reference',p)
    for channel in ['books','trades']:
        item=message(channel,-2);item['data']['token']='private-canary'
        row=encoder.encode(item,stamp(-1));reference.ingest(item,received_at=stamp(-1))
        expected=reference.db.execute('select ticker,channel,event_ns,received_ns,kind,serial,payload,digest from events where channel=?',(channel,)).fetchone()
        assert tuple(row)==expected and 'private-canary' not in json.dumps(row)
    assert encoder.encode({'event':'authenticated','token':'private-canary'},stamp()) is None
    reference.close()


def test_receipts_survive_abrupt_process_exit_and_replay_is_idempotent(tmp_path):
    m=module();root=tmp_path/'pilot';root.mkdir();(root/'protocol.json').write_text(json.dumps(protocol()))
    program="""import importlib.util,json,os,sys
from datetime import datetime
s=importlib.util.spec_from_file_location('receipt',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=m.Journal(sys.argv[2],sys.argv[3]);r.event(json.loads(sys.argv[4]),datetime.fromisoformat(sys.argv[5]));r.flush();os._exit(0)
"""
    result=subprocess.run([sys.executable,'-c',program,m.__file__,str(root),str(SOURCE),json.dumps(message('trades',-2)),stamp(-1).isoformat()],capture_output=True)
    assert result.returncode==0
    consumer=m.Materializer(root,SOURCE);assert consumer.step()['events']==1;assert consumer.step()['events']==0
    with sqlite3.connect(root/'pilot.sqlite3') as db:
        assert db.execute('select count(*) from events').fetchone()[0]==1
        assert db.execute('select received_ns from events').fetchone()[0]==int(stamp(-1).timestamp())*10**9
    consumer.close()


def test_batch_replay_keeps_forecasts_labels_and_gaps_identical(tmp_path):
    m=module();p=protocol();root=tmp_path/'journal';root.mkdir();(root/'protocol.json').write_text(json.dumps(p));old=PilotStore(tmp_path/'old',p)
    journal=m.Journal(root,SOURCE)
    for item,received in [(message('books',-2),stamp(-1)),(message('trades',-2),stamp(-1)),(message('trades',20,price=101,serial=2),stamp(21))]:
        old.ingest(item,received_at=received);journal.event(item,received)
    old.mark_coverage(stamp(-60),stamp(360));journal.control('coverage',stamp(-60),stamp(360));journal.close()
    consumer=m.Materializer(root,SOURCE);consumer.step();consumer.close();new=PilotStore(root,p)
    assert new.emit(stamp())==old.emit(stamp())
    assert new.evaluate(as_of=stamp(400))==old.evaluate(as_of=stamp(400))
    old.close();new.close()


def test_failed_batch_does_not_advance_cursor_or_duplicate_gap_on_retry(tmp_path):
    m=module();root=tmp_path/'pilot';root.mkdir();(root/'protocol.json').write_text(json.dumps(protocol()))
    journal=m.Journal(root,SOURCE);journal.event(message('trades',1),stamp(2));journal.control('gap',stamp(3),stamp(4),'stalled');journal.close()
    consumer=m.Materializer(root,SOURCE)
    blocker=sqlite3.connect(root/'pilot.sqlite3');blocker.execute('begin immediate')
    with pytest.raises(sqlite3.OperationalError):consumer.step()
    blocker.rollback();blocker.close();consumer.step();consumer.step()
    assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==1
    assert consumer.store.db.execute('select count(*) from gaps').fetchone()[0]==1
    consumer.close()


def test_segment_rotation_is_private_bounded_and_rejects_corruption(tmp_path):
    m=module();root=tmp_path/'pilot';root.mkdir();(root/'protocol.json').write_text(json.dumps(protocol()))
    journal=m.Journal(root,SOURCE,segment_bytes=2048)
    for i in range(20):journal.event(message('trades',i,serial=i),stamp(i+1))
    journal.close();segments=sorted((root/'receipts').glob('*.twqj'))
    assert len(segments)>1 and all(f.stat().st_mode&0o077==0 for f in segments)
    first=segments[0];body=bytearray(first.read_bytes());body[-1]^=1;first.write_bytes(body)
    consumer=m.Materializer(root,SOURCE)
    with pytest.raises(ValueError,match='journal'):consumer.step()
    consumer.close()


def test_receipt_adapter_does_not_import_scientific_libraries(tmp_path):
    m=module();program="""import sys,importlib.util,json
s=importlib.util.spec_from_file_location('receipt',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
m.FrozenAdapter(sys.argv[2],json.loads(sys.argv[3]));m.frozen_feed(sys.argv[2],json.loads(sys.argv[3]));print(json.dumps([n for n in ['pandas','numpy','scipy','sklearn'] if n in sys.modules]))
"""
    r=subprocess.run([sys.executable,'-c',program,m.__file__,str(SOURCE),json.dumps(protocol())],capture_output=True,text=True)
    assert r.returncode==0 and json.loads(r.stdout)==[]


def test_restart_retains_valid_prefix_of_torn_receipt_and_marks_unclean_gap(tmp_path):
    m=module();root=tmp_path/'pilot';root.mkdir();(root/'protocol.json').write_text(json.dumps(protocol()))
    journal=m.Journal(root,SOURCE);journal.event(message('trades',1),stamp(2));journal.flush()
    original=journal.path;journal.handle.write(b'\x00\x00');journal.handle.close()
    recovered=m.Journal(root,SOURCE);recovered.close()
    assert original.read_bytes().endswith(b'\x00\x00')
    seal=json.loads(original.with_suffix('.seal.json').read_text())
    assert seal['recovered'] and seal['trailing_bytes']==2
    consumer=m.Materializer(root,SOURCE);consumer.step();consumer.step()
    assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==1
    assert consumer.store.db.execute('select count(*) from gaps').fetchone()[0]>=1
    consumer.close()


def test_capture_spools_a_burst_without_an_in_memory_backlog(tmp_path):
    m=module();p=protocol();p.update(collection_start='09:59:59',collection_end='10:00:01')
    (tmp_path/'protocol.json').write_text(json.dumps(p));clock=[stamp(-1)];start=clock[0]
    class Feed:
        expected={('2330','trades')};subscriptions=expected;ready=True;failed=None
        def __init__(self,symbols):self.last_received_ns=m.ns(clock[0])
        def connect(self,receive,disconnect):
            for i in range(2000):receive(message('trades',-2,serial=i))
        def close(self):pass
    class Child:
        def poll(self):return 0
        def wait(self,timeout):return 0
    result=m.capture(tmp_path,SOURCE,feed_factory=Feed,clock=lambda:clock[0],
        sleep=lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)),launch=lambda args:Child(),drain_seconds=0)
    assert result['durable_received_messages']==2000 and result['dropped_messages']==0
    assert result['receipt_complete'] and not result['complete']
    consumer=m.Materializer(tmp_path,SOURCE)
    while consumer.step()['records']:pass
    assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==2000
    consumer.close()


def test_materializer_yields_the_writer_during_fixed_forecast_publication(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text(json.dumps(protocol()));journal=m.Journal(tmp_path,SOURCE)
    journal.event(message('trades',-2),stamp(-1));journal.close();consumer=m.Materializer(tmp_path,SOURCE)
    assert consumer.step(stamp=stamp(-1))['state']=='forecast_priority'
    assert consumer.step(stamp=stamp())['state']=='forecast_priority'
    assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==0
    assert consumer.step(stamp=stamp(2))['events']==1
    consumer.close()


def test_consumer_initialization_allows_a_short_schema_lock(tmp_path,monkeypatch):
    import threading
    import twquant.live_pilot as reference
    m=module();p=protocol();(tmp_path/'protocol.json').write_text(json.dumps(p));store=PilotStore(tmp_path,p)
    blocker=sqlite3.connect(tmp_path/'pilot.sqlite3',check_same_thread=False);blocker.execute('begin immediate')
    monkeypatch.setattr(reference,'PilotStore',lambda *a,**k:store)
    release=threading.Timer(.3,blocker.rollback);release.start()
    try:consumer=m.Materializer(tmp_path,SOURCE)
    finally:release.join();blocker.close()
    consumer.close()


def test_adapter_rejects_changed_frozen_source_before_accepting_receipts(tmp_path):
    m=module();p=protocol();p['source_sha256']='0'*64;(tmp_path/'protocol.json').write_text(json.dumps(p))
    with pytest.raises(ValueError,match='source_changed'):m.Journal(tmp_path,SOURCE)
    assert not (tmp_path/'receipts').exists()


def test_error_receipt_has_diagnostic_location_without_exception_payload():
    m=module();result=m.error_details(ValueError('private-secret-canary'))
    assert result['code']=='receipt_operation_failed' and result['error_class']=='ValueError'
    assert 'private-secret-canary' not in json.dumps(result)


def test_indexed_materializer_has_a_bounded_cache_without_weakening_durability(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text(json.dumps(protocol()));consumer=m.Materializer(tmp_path,SOURCE)
    assert consumer.store.db.execute('pragma cache_size').fetchone()[0]==-65536
    assert consumer.store.db.execute('pragma synchronous').fetchone()[0]==2
    assert consumer.store.db.execute('pragma journal_mode').fetchone()[0]=='wal'
    consumer.close()


def test_daily_store_has_identical_causal_inputs_and_merges_actual_seals(tmp_path):
    m=module();p=protocol();(tmp_path/'protocol.json').write_text(json.dumps(p));old=PilotStore(tmp_path,p)
    # Retain equal-timestamp revisions so a repeated older digest cannot win a tie.
    for item in [message('books',-2),message('trades',-2,serial=1),message('trades',-2,serial=2,price=101)]:old.ingest(item,received_at=stamp(-1))
    old.flush();session=m.prepare_session(tmp_path,SOURCE,'2026-11-16')
    journal=m.Journal(tmp_path,SOURCE,day='2026-11-16')
    item=message('trades',-2,serial=1);journal.event(item,stamp(-.1));journal.control('coverage',stamp(-60),stamp(360));journal.close()
    daily=m.Materializer(tmp_path,SOURCE,session_day='2026-11-16')
    while daily.step()['records']:pass
    before=old.emit(stamp());old.seal('2026-11-16',stamp(.1))
    assert daily.store.emit(stamp())==before
    daily.store.seal('2026-11-16',stamp(.1));daily.close();old.close()
    m.merge_session(tmp_path,SOURCE,'2026-11-16');m.merge_session(tmp_path,SOURCE,'2026-11-16')
    result=PilotStore(tmp_path,p)
    assert result.emit(stamp(1))==before
    assert result.db.execute('select at_ns from releases').fetchone()[0]==int(stamp(.1).timestamp()*10**9)
    assert result.db.execute('select count(*) from events').fetchone()[0]==3
    assert result.db.execute('select count(*) from coverage').fetchone()[0]==1
    result.close()


def test_merge_never_replaces_a_previous_prediction_or_publication(tmp_path):
    m=module();p=protocol();(tmp_path/'protocol.json').write_text(json.dumps(p));canonical=PilotStore(tmp_path,p)
    original=canonical.emit(stamp());canonical.seal('2026-11-16',stamp(.1));canonical.close()
    journal=m.Journal(tmp_path,SOURCE,day='2026-11-16');journal.event(message('books',-2),stamp(-1));journal.event(message('trades',-2,price=101),stamp(-1));journal.close()
    daily=m.Materializer(tmp_path,SOURCE,session_day='2026-11-16');daily.step();daily.store.emit(stamp());daily.store.seal('2026-11-16',stamp(.1));daily.close()
    with pytest.raises(ValueError,match='prediction_conflict'):m.merge_session(tmp_path,SOURCE,'2026-11-16')
    canonical=PilotStore(tmp_path,p);assert canonical.emit(stamp(2))==original;canonical.close()


def test_unclean_daily_restart_preserves_event_order_and_does_not_reuse_ids(tmp_path):
    m=module();(tmp_path/'protocol.json').write_text(json.dumps(protocol()))
    first=m.Journal(tmp_path,SOURCE,day='2026-11-16',session_mode=True)
    for i in range(10):first.event(message('trades',i,serial=i),stamp(i+1))
    first.flush();consumer=m.Materializer(tmp_path,SOURCE,session_day='2026-11-16');consumer.step();consumer.close();first.handle.close()
    second=m.Journal(tmp_path,SOURCE,day='2026-11-16',session_mode=True)
    for i in range(10,20):second.event(message('trades',i,serial=i),stamp(i+1))
    second.close();consumer=m.Materializer(tmp_path,SOURCE,session_day='2026-11-16')
    while consumer.step()['records']:pass
    assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==20
    assert [r[0] for r in consumer.store.db.execute('select serial from events order by id')]==[str(i) for i in range(20)]
    consumer.close()


def test_daily_context_does_not_scan_the_entire_historical_ticker(tmp_path,monkeypatch):
    m=module();p=protocol();(tmp_path/'protocol.json').write_text(json.dumps(p));store=PilotStore(tmp_path,p)
    encoder=m.FrozenAdapter(SOURCE,p)
    rows=[encoder.encode(message('trades',-30000+i,serial=i),stamp(-1)) for i in range(20000)]
    store.db.executemany('insert into events(ticker,channel,event_ns,received_ns,kind,serial,payload,digest) values(?,?,?,?,?,?,?,?)',rows);store.flush();store.close()
    original=m.sqlite3.connect;budget=[0]
    def connect(*args,**kwargs):
        db=original(*args,**kwargs)
        if '?mode=ro' in str(args[0]):
            def progress():budget[0]+=1;return int(budget[0]>20)
            db.set_progress_handler(progress,1000)
        return db
    monkeypatch.setattr(m.sqlite3,'connect',connect)
    m.prepare_session(tmp_path,SOURCE,'2026-11-16')
