"""Keep the fixed forecast causal while deferring the historical event store."""
import json
import subprocess
import sys
import random
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from test_cloud_receipt_journal import module, SOURCE
from test_live_pilot import protocol, stamp, message
from twquant.live_pilot import PilotStore


DAY = '2026-11-16'


def setup_root(root):
    root.mkdir()
    (root/'protocol.json').write_text(json.dumps(protocol()))
    (root/'capture_profile.json').write_text(json.dumps({'projection_from': DAY}))
    return root


def drain(consumer, **kwargs):
    while consumer.step(**kwargs)['records']:
        pass


def test_projection_retains_the_full_receipts_with_a_small_identical_forecast(tmp_path):
    m = module(); root = setup_root(tmp_path/'pilot')
    reference = PilotStore(tmp_path/'reference', protocol())
    journal = m.Journal(root, SOURCE, day=DAY, session_mode=True)
    for i in range(2000):
        item = message('trades' if i % 2 else 'books', -50+i/100,
                       serial=i, price=100+i % 2)
        received = stamp(-49+i/100)
        reference.ingest(item, received_at=received); journal.event(item, received)
    journal.close(); original = {f.name: f.read_bytes() for f in (root/'receipts').glob('*.twqj')}
    consumer = m.Materializer(root, SOURCE, session_day=DAY)
    try:
        drain(consumer, max_records=117)
        assert consumer.store.db.execute('select count(*) from events').fetchone()[0] == 2
        assert consumer.store.db.execute('select sum(events) from cloud_receipt_cursor').fetchone()[0] == 2000
        actual = PilotStore(consumer.store.root, protocol())
        try:
            assert actual.emit(stamp()) == reference.emit(stamp())
        finally:
            actual.close()
        assert {f.name: f.read_bytes() for f in (root/'receipts').glob('*.twqj')} == original
    finally:
        consumer.close(); reference.close()


def test_projection_preserves_first_digest_ties_late_arrivals_and_future_cutoffs(tmp_path):
    m = module(); root = setup_root(tmp_path/'pilot'); reference = PilotStore(tmp_path/'reference', protocol())
    journal = m.Journal(root, SOURCE, day=DAY, session_mode=True)
    values = [
        (message('books', -2), stamp(-1)),
        (message('trades', -2, serial=1), stamp(-1)),
        (message('trades', -2, serial=2, price=101), stamp(-.9)),
        (message('trades', -2, serial=1), stamp(-.8)),
        (message('trades', -3, serial=3, price=90), stamp(-.7)),
        (message('trades', 30, serial=4, price=200), stamp(-.6)),
        (message('trades', -.1, serial=5, price=300), stamp(.1)),
        # Clock reversal must not turn a duplicate's later receipt into its first.
        (message('trades', -.1, serial=5, price=300), stamp(-.1)),
    ]
    for item, received in values:
        reference.ingest(item, received_at=received); journal.event(item, received)
    journal.close()
    consumer = m.Materializer(root, SOURCE, session_day=DAY)
    # Every restart must continue from the durable cursor and retain tie identities.
    for _ in values:
        consumer.step(max_records=1); consumer.close()
        consumer = m.Materializer(root, SOURCE, session_day=DAY)
    try:
        actual = PilotStore(consumer.store.root, protocol())
        try:
            assert actual.emit(stamp()) == reference.emit(stamp())
            assert actual.db.execute('select count(*) from events').fetchone()[0] == 3
        finally:
            actual.close()
    finally:
        consumer.close(); reference.close()


def test_projection_consumer_does_not_load_scientific_dependencies(tmp_path):
    m = module(); root = setup_root(tmp_path/'pilot')
    code = '''import importlib.util,sys,json
s=importlib.util.spec_from_file_location('receipt',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
c=m.Materializer(sys.argv[2],sys.argv[3],session_day=sys.argv[4]);c.close()
print(json.dumps([n for n in ['pandas','numpy','scipy','sklearn'] if n in sys.modules]))
'''
    result = subprocess.run([sys.executable, '-c', code, m.__file__, str(root), str(SOURCE), DAY],
                            capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == []


def test_deferred_replay_restores_every_event_and_copies_actual_publication(tmp_path, monkeypatch):
    m = module(); root = setup_root(tmp_path/'pilot'); reference = PilotStore(tmp_path/'reference', protocol())
    journal = m.Journal(root, SOURCE, day=DAY, session_mode=True)
    for item, received in [(message('books', -2), stamp(-1)),
                           (message('trades', -2), stamp(-1)),
                           (message('trades', 20, price=101, serial=2), stamp(21))]:
        reference.ingest(item, received_at=received); journal.event(item, received)
    journal.control('coverage', stamp(-60), stamp(360)); journal.close()
    reference.mark_coverage(stamp(-60), stamp(360))
    consumer = m.Materializer(root, SOURCE, session_day=DAY); drain(consumer)
    publication = PilotStore(consumer.store.root, protocol())
    expected = reference.emit(stamp()); reference.seal(DAY, stamp(.1))
    assert publication.emit(stamp()) == expected
    publication.seal(DAY, stamp(.1)); publication.close(); consumer.close()
    (root/'receipt_capture_done.json').write_text(json.dumps({'date': DAY, 'events_total': 3}))
    monkeypatch.setattr(m, 'now', lambda: stamp(18*60))
    m.consume(root, SOURCE, replay=True, session_mode=True)
    rebuilt = PilotStore(root, protocol())
    try:
        assert rebuilt.db.execute('select count(*) from events').fetchone()[0] == 3
        assert rebuilt.emit(stamp(1)) == expected
        assert rebuilt.db.execute('select at_ns from releases where day=?', (DAY,)).fetchone()[0] == m.ns(stamp(.1))
        assert rebuilt.evaluate(as_of=stamp(400)) == reference.evaluate(as_of=stamp(400))
    finally:
        rebuilt.close(); reference.close()


def test_projection_matches_frozen_inputs_at_every_chaotic_receipt_prefix(tmp_path):
    m=module();root=setup_root(tmp_path/'pilot');reference=PilotStore(tmp_path/'reference',protocol())
    journal=m.Journal(root,SOURCE,day=DAY,session_mode=True);consumer=m.Materializer(root,SOURCE,session_day=DAY)
    rng=random.Random(37);messages=[]
    try:
        for i in range(500):
            if messages and i%4==0:item=rng.choice(messages)
            else:
                item=message(rng.choice(['trades','books']),rng.choice([-3,-2,-1,1]),serial=i,price=rng.choice([100,101]))
                messages.append(item)
            received=stamp(rng.choice([-2,-1,.1]));reference.ingest(item,received_at=received);journal.event(item,received);journal.flush();drain(consumer,max_records=19)
            for channel in ['books','trades']:
                expected=reference._observed('2330',channel,m.ns(stamp()))
                got=consumer.store.db.execute('select event_ns,received_ns,payload from events where ticker=? and channel=? and received_ns<=? and event_ns<=? order by event_ns desc,id desc limit 1',('2330',channel,m.ns(stamp()),m.ns(stamp()))).fetchone()
                assert got==expected,(i,channel)
    finally:
        journal.close();consumer.close();reference.close()


def test_projection_keeps_seeded_equal_timestamp_identities(tmp_path):
    m=module();root=setup_root(tmp_path/'pilot');old=PilotStore(root,protocol())
    items=[message('books',-2),message('trades',-2,serial=1),message('trades',-2,serial=2,price=101)]
    for item in items:old.ingest(item,received_at=stamp(-1))
    old.flush();journal=m.Journal(root,SOURCE,day=DAY,session_mode=True);journal.event(items[1],stamp(-.1));journal.close()
    consumer=m.Materializer(root,SOURCE,session_day=DAY);drain(consumer)
    actual=PilotStore(consumer.store.root,protocol())
    try:assert actual.emit(stamp())==old.emit(stamp())
    finally:actual.close();consumer.close();old.close()


def test_projection_rollback_does_not_advance_cursor_or_lose_previous_input(tmp_path):
    import sqlite3
    m=module();root=setup_root(tmp_path/'pilot');journal=m.Journal(root,SOURCE,day=DAY,session_mode=True)
    journal.event(message('trades',-3),stamp(-2));journal.flush();consumer=m.Materializer(root,SOURCE,session_day=DAY);drain(consumer)
    journal.event(message('trades',-2,serial=2,price=101),stamp(-1));journal.close()
    before=consumer.store.db.execute('select * from cloud_receipt_cursor').fetchall()
    blocker=sqlite3.connect(consumer.store.root/'pilot.sqlite3');blocker.execute('begin immediate')
    try:
        with pytest.raises(sqlite3.OperationalError):consumer.step()
        assert consumer.store.db.execute('select * from cloud_receipt_cursor').fetchall()==before
    finally:blocker.rollback();blocker.close()
    drain(consumer);assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==1
    consumer.close()


def test_copying_a_projection_rejects_rewriting_an_existing_release(tmp_path):
    m=module();root=setup_root(tmp_path/'pilot');consumer=m.Materializer(root,SOURCE,session_day=DAY)
    live=PilotStore(consumer.store.root,protocol());live.emit(stamp());live.seal(DAY,stamp(.1));live.close()
    full=PilotStore(root/'sessions'/DAY,protocol());full.emit(stamp());full.seal(DAY,stamp(.2))
    try:
        with pytest.raises(ValueError,match='publication_conflict'):m.copy_publication(root,SOURCE,DAY,full)
        assert full.db.execute('select at_ns from releases').fetchone()[0]==m.ns(stamp(.2))
    finally:full.close();consumer.close()


@pytest.mark.parametrize('projected', [False, True])
def test_historical_replay_builds_secondary_indexes_only_after_full_ingestion(tmp_path,projected):
    m=module();root=setup_root(tmp_path/'pilot');journal=m.Journal(root,SOURCE,day=DAY,session_mode=True)
    if not projected:(root/'capture_profile.json').write_text('{}')
    for i in range(100):journal.event(message('trades',-20+i/10,serial=i),stamp(-1))
    journal.close();consumer=m.Materializer(root,SOURCE,session_day=DAY,full_history=True)
    try:
        names={r[1] for r in consumer.store.db.execute('pragma index_list(events)')}
        assert 'event_lookup' not in names and 'receipt_lookup' not in names
        drain(consumer);assert consumer.store.db.execute('select count(*) from events').fetchone()[0]==100
        consumer.finish_history()
        names={r[1] for r in consumer.store.db.execute('pragma index_list(events)')}
        assert {'event_lookup','receipt_lookup'}<=names
    finally:consumer.close()


def test_receipt_sync_continues_during_a_slow_provider_handshake(tmp_path):
    m=module();now=datetime.now(ZoneInfo('Asia/Taipei'));p=protocol()
    p.update(dates=[now.date().isoformat()],collection_start=(now-timedelta(seconds=1)).strftime('%H:%M:%S'),collection_end=(now+timedelta(seconds=8)).strftime('%H:%M:%S'))
    (tmp_path/'protocol.json').write_text(json.dumps(p));stop=threading.Event();observed=[]
    class Feed:
        ready=False;expected={('2330','trades')};subscriptions=set();last_received_ns=0
        def __init__(self,symbols):pass
        def connect(self,receive,disconnect):
            receive(message('trades',-2));time.sleep(1.4)
            observed.append((tmp_path/'receipt_watermark.json').exists());stop.set()
        def close(self):pass
    class Child:
        def poll(self):return 0
        def wait(self,timeout):return 0
    m.capture(tmp_path,SOURCE,feed_factory=Feed,stop=stop,launch=lambda args:Child(),drain_seconds=0)
    assert observed==[True], 'Provider handshake must not block durable receipt watermarks'


def test_small_projection_bounds_wal_growth_with_research_automatic_checkpoints_off(tmp_path,monkeypatch):
    m=module();root=setup_root(tmp_path/'pilot');journal=m.Journal(root,SOURCE,day=DAY,session_mode=True)
    for i in range(12000):journal.event(message('trades',-50+i/1000,serial=i),stamp(-1))
    journal.close();original=m.sqlite3.connect
    def tuned(*args,**kwargs):
        connection=original(*args,**kwargs);connection.execute('pragma wal_autocheckpoint=0');return connection
    monkeypatch.setattr(m.sqlite3,'connect',tuned)
    consumer=m.Materializer(root,SOURCE,session_day=DAY)
    try:
        drain(consumer,max_records=12)
        assert (consumer.store.root/'pilot.sqlite3-wal').stat().st_size<2*1024**2
        assert consumer.store.db.execute('select sum(events) from cloud_receipt_cursor').fetchone()[0]==12000
    finally:consumer.close()


def test_forecast_preloads_before_waiting_for_the_frozen_entry_window(tmp_path,monkeypatch):
    import twquant.live_market as market
    m=module();root=setup_root(tmp_path/'pilot');clock=[stamp(-170)];events=[]
    def create(path,p):
        events.append(('loaded',clock[0]));return PilotStore(path,p)
    def forecast(path,store):
        events.append(('forecast',clock[0]));return dict(state='forecast_recorded',targets=len(store.symbols))
    monkeypatch.setattr(market,'PilotStore',create);monkeypatch.setattr(market,'forecast',forecast)
    monkeypatch.setattr(m,'now',lambda:clock[0]);monkeypatch.setattr(m.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+timedelta(seconds=seconds)))
    result=m.forecast_session(root,SOURCE)
    assert result['state']=='forecast_recorded'
    assert events==[('loaded',stamp(-170)),('forecast',stamp(-60))]
