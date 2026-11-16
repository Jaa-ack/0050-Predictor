from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_checkpoint.py'
    assert path.exists(),'Bounded checkpoint runtime is missing'
    spec=importlib.util.spec_from_file_location('checkpoint',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def setup(tmp_path):
    (tmp_path/'protocol.json').write_text(json.dumps(dict(dates=['2026-11-17'],collection_start='09:30:00',collection_end='10:15:00',prediction_time='10:00:00')))
    db=sqlite3.connect(tmp_path/'pilot.sqlite3');db.execute('pragma journal_mode=wal');db.execute('pragma wal_autocheckpoint=0')
    db.execute('create table evidence(value text)');db.executemany('insert into evidence values(?)',[('synthetic-'+str(i),) for i in range(1000)]);db.commit();return db


def test_checkpoint_is_excluded_from_prewarm_and_both_scoring_windows(tmp_path,monkeypatch):
    m=module();db=setup(tmp_path)
    monkeypatch.setattr(m.sqlite3,'connect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Protected window must not open SQLite')))
    for hour,minute in [(9,57),(9,59),(10,0),(10,5)]:
        r=m.checkpoint(tmp_path,stamp=datetime(2026,11,17,hour,minute,tzinfo=ZoneInfo('Asia/Taipei')),minimum_wal=1)
        assert r['state']=='protected_window'
    db.close()


def test_passive_checkpoint_preserves_data_and_allows_subsequent_writes(tmp_path,monkeypatch):
    m=module();db=setup(tmp_path);monkeypatch.setattr(m.sqlite3,'sqlite_version_info',(3,54,1))
    r=m.checkpoint(tmp_path,stamp=datetime(2026,11,17,9,45,tzinfo=ZoneInfo('Asia/Taipei')),minimum_wal=1)
    assert r['state']=='complete'
    db.execute("insert into evidence values('after-checkpoint')");db.commit()
    assert db.execute('select count(*) from evidence').fetchone()[0]==1001
    assert db.execute('pragma quick_check').fetchone()[0]=='ok';db.close()


def test_passive_checkpoint_keeps_a_reader_snapshot_and_reports_partial_work(tmp_path,monkeypatch):
    m=module();db=setup(tmp_path);monkeypatch.setattr(m.sqlite3,'sqlite_version_info',(3,54,1))
    reader=sqlite3.connect(tmp_path/'pilot.sqlite3');reader.execute('begin');assert reader.execute('select count(*) from evidence').fetchone()[0]==1000
    db.execute("insert into evidence values('new')");db.commit()
    r=m.checkpoint(tmp_path,stamp=datetime(2026,11,17,9,45,tzinfo=ZoneInfo('Asia/Taipei')),minimum_wal=1)
    assert r['state']=='partial' and reader.execute('select count(*) from evidence').fetchone()[0]==1000
    reader.close();db.close()
