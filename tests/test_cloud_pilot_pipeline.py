import fcntl
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from test_cloud_receipt_journal import module as receipts,SOURCE
from test_live_pilot import protocol,stamp,message
from twquant.live_pilot import PilotStore


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_pipeline.py'
    assert path.exists(),'Ordered, resumable maintenance is missing'
    spec=importlib.util.spec_from_file_location('pipeline',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def prepare(root,*,last_day=False):
    root.mkdir();p=protocol()
    if last_day:p['dates']=['2026-11-16']
    (root/'protocol.json').write_text(json.dumps(p))
    (root/'capture_profile.json').write_text(json.dumps(dict(pipeline_from='2026-11-16',projection_from='2026-11-16')))
    m=receipts();journal=m.Journal(root,SOURCE,day='2026-11-16',session_mode=True)
    for item,received in [(message('books',-2),stamp(-1)),(message('trades',-2),stamp(-1)),(message('trades',20,price=101,serial=2),stamp(21))]:journal.event(item,received)
    journal.control('coverage',stamp(-60),stamp(360));journal.close()
    consumer=m.Materializer(root,SOURCE,session_day='2026-11-16')
    while consumer.step()['records']:pass
    live=PilotStore(consumer.store.root,p);live.emit(stamp());live.seal('2026-11-16',stamp(.1));live.close();consumer.close()
    (root/'receipt_capture_done.json').write_text(json.dumps(dict(date='2026-11-16',events_total=3)))
    return p


def report(root,day,at):
    p=json.loads((root/'protocol.json').read_text());store=PilotStore(root,p)
    try:return store.write_report(as_of=at)
    finally:store.close()


def test_ordered_pipeline_preserves_scoring_and_releases_only_rebuildable_workspace(tmp_path):
    m=module();root=tmp_path/'pilot';prepare(root)
    result=m.run(root,SOURCE,clock=lambda:stamp(18*60),report=report)
    assert result['state']=='complete'
    assert result['completed']==list(m.STAGES)
    assert not (root/'sessions/2026-11-16/pilot.sqlite3').exists()
    with sqlite3.connect(root/'backups/latest.sqlite3') as db:
        assert db.execute('select count(*) from events').fetchone()[0]==3
        assert db.execute('select count(*) from predictions').fetchone()[0]==len(receipts().FrozenAdapter(SOURCE,protocol()).symbols)
    with sqlite3.connect(root/'pilot.sqlite3') as db:
        assert db.execute('select count(*) from events').fetchone()[0]==3
        assert db.execute('select at_ns from releases').fetchone()[0]==receipts().ns(stamp(.1))
    value=json.loads((root/'evaluation.json').read_text())
    assert value['horizons']['60']['expected_predictions']==50
    assert value['horizons']['300']['missing_predictions']==0
    assert m.load_helper('watchdog').report_current(root,protocol(),stamp(20*60))
    checkpoint=(root/'pipeline_state.json').stat().st_mtime_ns
    assert m.run(root,SOURCE,clock=lambda:stamp(20*60),report=lambda *a:pytest.fail('Completed work repeated'))['state']=='complete'
    assert (root/'pipeline_state.json').stat().st_mtime_ns==checkpoint
    assert m.load_helper('watchdog').backup(root,stamp=stamp(20*60))['state']=='complete'


def test_pipeline_does_not_overlap_capture_or_an_existing_maintenance_worker(tmp_path):
    m=module();root=tmp_path/'pilot';prepare(root)
    assert m.run(root,SOURCE,clock=lambda:stamp(),report=report)['state']=='protected_window'
    with (root/'maintenance.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert m.run(root,SOURCE,clock=lambda:stamp(18*60),report=report)['state']=='busy'
    assert (root/'receipts/00000001.twqj').exists()


def test_failed_pipeline_keeps_data_and_resumes_at_the_failed_stage(tmp_path):
    m=module();root=tmp_path/'pilot';prepare(root)
    def fail(*args):raise RuntimeError('private-error-must-not-leak')
    result=m.run(root,SOURCE,clock=lambda:stamp(18*60),report=fail)
    assert result['state']=='failed' and result['phase']=='report'
    assert 'private-error' not in json.dumps(result)
    assert (root/'sessions/2026-11-16/pilot.sqlite3').exists()
    assert m.run(root,SOURCE,clock=lambda:stamp(18*60+30),report=report)['state']=='retry_wait'
    result=m.run(root,SOURCE,clock=lambda:stamp(21*60),report=report)
    assert result['state']=='complete' and result['attempts']['report']==2


def test_incomplete_capture_never_starts_archive_or_disposes_of_data(tmp_path):
    m=module();root=tmp_path/'pilot';prepare(root)
    (root/'receipt_capture_done.json').write_text(json.dumps(dict(date='2026-11-16',events_total=4)))
    result=m.run(root,SOURCE,clock=lambda:stamp(18*60),report=report)
    assert result['state']=='capture_incomplete'
    assert not (root/'archives').exists()
    assert (root/'receipts/00000001.twqj').exists()


def test_previous_day_maintenance_yields_to_next_premarket_probe(tmp_path):
    from datetime import timedelta
    m=module();root=tmp_path/'pilot';prepare(root)
    at=(stamp()+timedelta(days=1)).replace(hour=8,minute=40)
    assert m.run(root,SOURCE,clock=lambda:at,report=report)['state']=='protected_window'
    assert not (root/'archives').exists()


def test_each_running_stage_renews_its_own_service_deadline(tmp_path,monkeypatch):
    m=module();root=tmp_path/'pilot';prepare(root);notifications=[]
    monkeypatch.setattr(m,'notify_service',lambda value:notifications.append(value),raising=False)
    result=m.run(root,SOURCE,clock=lambda:stamp(18*60),report=report)
    assert result['state']=='complete'
    assert len(notifications)==len(m.STAGES),'Every stage needs its own finite timeout budget'
    assert all('EXTEND_TIMEOUT_USEC=' in value for value in notifications)


def test_reports_use_committed_history_before_the_explicit_checkpoint(tmp_path):
    m=module();root=tmp_path/'pilot';p=prepare(root);base=PilotStore(root,p);base.close()
    def observed(root,day,at):
        wal=root/'pilot.sqlite3-wal'
        assert wal.exists() and wal.stat().st_size>0
        assert json.loads((root/'archive_status.json').read_text())['state']=='complete'
        return report(root,day,at)
    result=m.run(root,SOURCE,clock=lambda:stamp(18*60),report=observed)
    assert result['state']=='complete'
    assert result['completed'].index('report')<result['completed'].index('checkpoint')
    assert not (root/'pilot.sqlite3-wal').exists() or (root/'pilot.sqlite3-wal').stat().st_size==0


def test_finalization_waits_for_checkpoint_and_workspace_release(tmp_path,monkeypatch):
    m=module();root=tmp_path/'pilot';p=prepare(root,last_day=True)
    def final(root,day,at):
        store=PilotStore(root,p)
        try:return store.write_report(as_of=at,final=True)
        finally:store.close()
    def interrupted(db):raise RuntimeError('checkpoint_interrupted')
    checkpoint=m.checkpoint_history;monkeypatch.setattr(m,'checkpoint_history',interrupted)
    result=m.run(root,SOURCE,clock=lambda:stamp(4*3600),report=final)
    assert result['state']=='failed' and result['phase']=='checkpoint'
    assert not (root/'cloud_complete').exists()
    assert (root/'sessions/2026-11-16/pilot.sqlite3').exists()
    monkeypatch.setattr(m,'checkpoint_history',checkpoint)
    result=m.run(root,SOURCE,clock=lambda:stamp(4*3600+180),report=final)
    assert result['state']=='complete' and (root/'cloud_complete').exists()
    assert not (root/'sessions/2026-11-16/pilot.sqlite3').exists()
    (root/'cloud_complete').unlink()
    assert m.run(root,SOURCE,clock=lambda:stamp(4*3600+181),report=final)['state']=='complete'
    assert (root/'cloud_complete').exists()


def test_legacy_report_still_retires_after_its_final_evaluation(tmp_path,monkeypatch):
    m=module();root=tmp_path/'pilot';p=prepare(root,last_day=True)
    (root/'capture_profile.json').unlink();store=PilotStore(root,p);store.close()
    at=stamp(4*3600);monkeypatch.setattr(m,'now',lambda:at)
    def final(root,day,stamp):
        store=PilotStore(root,p)
        try:return store.write_report(as_of=stamp,final=True)
        finally:store.close()
    monkeypatch.setattr(m,'write_report',final)
    monkeypatch.setattr(m.sys,'argv',['pipeline','--root',str(root),'--source',str(SOURCE),'--phase','report'])
    assert m.main()==0 and (root/'cloud_complete').exists()


def test_merge_verification_compares_nullable_fields_and_earlier_digest_identity(tmp_path):
    m=module();root=tmp_path/'pilot';p=prepare(root);day='2026-11-16'
    daily=root/'sessions'/day;daily.mkdir(parents=True)
    source=PilotStore(daily,p);source.ingest(message('trades',-2),received_at=stamp(-1));source.flush()
    source.db.execute('update events set serial=NULL');source.db.commit();source.close()
    canonical=PilotStore(root,p)
    canonical.db.execute('attach database ? as daily',(str(daily/'pilot.sqlite3'),))
    canonical.db.execute('insert into events select * from daily.events');canonical.db.commit()
    assert m.verify_merge(root,day)['state']=='complete'
    canonical.db.execute('update events set id=id+100');canonical.db.commit()
    assert m.verify_merge(root,day)['state']=='complete'
    canonical.db.execute("update events set payload='{}'");canonical.db.commit();canonical.close()
    with pytest.raises(ValueError,match='merge_mismatch'):m.verify_merge(root,day)
