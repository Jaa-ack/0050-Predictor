import importlib.util
from pathlib import Path
import sqlite3
import subprocess
import sys


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_sqlite.py'
    assert path.is_file(),'Cloud WAL checkpoint tuning is missing'
    spec=importlib.util.spec_from_file_location('cloud_sqlite',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def test_tuning_retains_full_commit_and_visibility_without_commit_time_checkpoint(tmp_path):
    m=module();path=tmp_path/'evidence.sqlite3'
    con=m.connect(path);con.execute('pragma journal_mode=WAL');con.execute('pragma synchronous=FULL')
    con.execute('create table evidence(value text)');con.execute("insert into evidence values('synthetic-canary')");con.commit()
    assert con.execute('pragma synchronous').fetchone()[0]==2
    assert con.execute('pragma wal_autocheckpoint').fetchone()[0]==0
    assert con.execute('pragma cache_size').fetchone()[0]==-8192
    with sqlite3.connect(path) as reader:
        assert reader.execute('select value from evidence').fetchone()[0]=='synthetic-canary'
    con.close()


def test_committed_wal_survives_abrupt_worker_exit(tmp_path):
    m=module();path=tmp_path/'evidence.sqlite3'
    program='''import importlib.util,os,sys
spec=importlib.util.spec_from_file_location('ops',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
c=m.connect(sys.argv[2]);c.execute('pragma journal_mode=WAL');c.execute('pragma synchronous=FULL')
c.execute('create table evidence(value text)');c.execute("insert into evidence values('durable-canary')");c.commit();os._exit(0)
'''
    r=subprocess.run([sys.executable,'-c',program,m.__file__,str(path)],capture_output=True)
    assert r.returncode==0 and (tmp_path/'evidence.sqlite3-wal').exists()
    with sqlite3.connect(path) as reader:
        assert reader.execute('select value from evidence').fetchone()[0]=='durable-canary'
        assert reader.execute('pragma integrity_check').fetchone()[0]=='ok'


def test_startup_install_is_idempotent_and_limits_changes_to_sqlite_connections(monkeypatch):
    m=module();original=sqlite3.connect
    monkeypatch.setattr(sqlite3,'connect',original)
    monkeypatch.setattr(sqlite3.dbapi2,'connect',sqlite3.dbapi2.connect)
    m.install();installed=sqlite3.connect;m.install()
    assert sqlite3.connect is installed
    with sqlite3.connect(':memory:') as con:
        assert con.execute('pragma wal_autocheckpoint').fetchone()[0]==0


def test_checkpoint_gate_rejects_engines_before_the_wal_reset_fix():
    m=module()
    assert not m.checkpoint_supported((3,40,1)) and not m.checkpoint_supported((3,51,2))
    assert m.checkpoint_supported((3,51,3)) and m.checkpoint_supported((3,54,1))
