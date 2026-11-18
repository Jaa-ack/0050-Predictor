from datetime import datetime,timedelta
import importlib.util
from pathlib import Path
from zoneinfo import ZoneInfo
import copy
import json
import sqlite3
from types import SimpleNamespace
import pytest


def module():
    path=Path(__file__).parents[1]/'scripts/cloud_pilot_validation.py'
    assert path.is_file(),'Isolated full-session validation is missing'
    spec=importlib.util.spec_from_file_location('validation',path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def plan():
    return module().make_plan(datetime(2026,11,16,10,55,tzinfo=ZoneInfo('Asia/Taipei')))


def test_full_validation_keeps_real_duration_and_relative_decision_windows():
    p=plan();stamp=lambda k:datetime.fromisoformat(p[k])
    assert stamp('end')-stamp('start')==timedelta(minutes=45)
    assert stamp('decision')-stamp('start')==timedelta(minutes=30)
    assert stamp('quiet')==stamp('start')-timedelta(minutes=2)
    assert stamp('start')<stamp('fault')<stamp('decision')-timedelta(minutes=2)
    assert stamp('backup')>stamp('end') and stamp('final')>stamp('backup')
    assert stamp('safety')<stamp('start').replace(hour=13,minute=30)


def test_diagnostic_protocol_preserves_model_and_never_mutates_formal_protocol():
    m=module();original=dict(dates=['2026-11-16','2026-11-17'],members=['2330'],training_tickers=['2330'],
        model_version='fixed-reference',source_sha256='a'*64,horizons=[60,300],prediction_time='10:00:00',
        collection_start='09:30:00',collection_end='10:15:00')
    before=copy.deepcopy(original);p=m.diagnostic_protocol(original,plan())
    assert original==before and p['diagnostic_only'] is True
    assert p['dates']==['2026-11-16'] and p['prediction_time']=='11:25:00'
    for key in ['members','training_tickers','model_version','source_sha256','horizons']:assert p[key]==original[key]


def test_virtual_health_clock_changes_only_comparison_copy_not_saved_observations():
    m=module();offset=timedelta(minutes=85)
    actual=datetime(2026,11,16,11,25,tzinfo=ZoneInfo('Asia/Taipei'))
    x=dict(collector_at=actual.isoformat(),last_event_ns=int(actual.timestamp()*1e9),release_ns=int(actual.timestamp()*1e9),backlog=7)
    before=copy.deepcopy(x);y=m.health_clock(x,offset)
    assert x==before and datetime.fromisoformat(y['collector_at']).hour==10
    assert y['release_ns']==x['release_ns']-85*60*10**9 and y['backlog']==7


def test_diagnostic_recovery_cannot_target_formal_services():
    m=module();p=plan()
    assert m.action_args(p,'restart_collect')==['restart',p['session']+'-collect.service']
    assert m.action_args(p,'retire_monitor')==['disable','--now',p['session']+'-watchdog.timer']
    with pytest.raises(ValueError):m.action_args(p,'stop_all_services')
    with pytest.raises(ValueError):m.action_args(dict(p,session='twquant-pilot'),'restart_collect')


def test_rendered_validation_jobs_are_single_session_and_have_deadline_cleanup():
    m=module();p=plan();units=m.render_units(p)
    assert p['session']+'-safety.timer' in units
    assert '11:55:00 Asia/Taipei' in units[p['session']+'-safety.timer']
    assert '11:24:00 Asia/Taipei' in units[p['session']+'-forecast.timer']
    assert '10:53:00 Asia/Taipei' in units[p['session']+'-quiet.timer']
    for text in units.values():
        assert 'stop twquant-pilot-' not in text and 'disable --now twquant-pilot-' not in text
    assert '--root '+p['root'] in units[p['session']+'-collect.service']


def test_candidate_validation_uses_staged_helpers_without_replacing_formal_code():
    m=module();p=dict(plan(),helper_prefix=str(m.PREFIX/'projection-candidate'))
    units=m.render_units(p)
    assert '11:22:00 Asia/Taipei' in units[p['session']+'-forecast.timer']
    for kind,name in [('collect','resource_guard.py'),('forecast','receipt_journal.py'),('replay','pipeline.py'),('archive','pipeline.py')]:
        assert p['helper_prefix']+'/'+name in units[p['session']+'-'+kind+'.service']
    with pytest.raises(ValueError):m.validate_plan(dict(p,helper_prefix='/tmp/untrusted'))


def test_validation_can_omit_unrequested_start_messages(tmp_path,monkeypatch):
    m=module();p=plan();p['notify_start']=False;calls=[]
    ops=SimpleNamespace(MESSAGES={},send_notice=lambda *args:calls.append(args))
    monkeypatch.setattr(m,'load_module',lambda *args:ops)
    assert m.run_phase(p,'notice')['state']=='not_requested'
    assert calls==[]


def test_projection_validation_retains_unplanned_lag_and_unhealthy_samples():
    m=module();p=plan();start=datetime.fromisoformat(p['start']);fault=datetime.fromisoformat(p['fault']);samples=[]
    for seconds in range(0,45*60+121,30):
        at=start+timedelta(seconds=seconds)
        samples.append(dict(at=at.isoformat(),collector_active=True,ready=True,last_event_ns=int((at.timestamp()-1)*1e9),shared_service_health='healthy',wal_bytes=1000))
    # The declared fault has its own recovery gate and stays in the source audit.
    samples[24].update(collector_active=False,ready=False,last_event_ns=0)
    assert all(m.projection_flow_checks(p,samples)['checks'].values())
    samples[60]['last_event_ns']-=10*10**9
    result=m.projection_flow_checks(p,samples)
    assert not result['checks']['steady_receipt_flow'] and result['max_lag_seconds']>=11
    samples[61]['shared_service_health']='unavailable'
    assert not m.projection_flow_checks(p,samples)['checks']['shared_service_stable']


def test_final_validation_uses_the_same_research_memory_group():
    p=plan();units=module().render_units(p)
    assert 'Slice=twquant-pilot.slice' in units[p['session']+'-final.service']


def test_publication_receipt_reads_the_small_projection_until_history_is_merged(tmp_path,monkeypatch):
    m=module();day='2026-11-18';path=tmp_path/'projections'/day;path.mkdir(parents=True)
    (tmp_path/'capture_profile.json').write_text(json.dumps(dict(projection_from=day)))
    with sqlite3.connect(path/'pilot.sqlite3') as db:
        db.executescript('CREATE TABLE predictions(day TEXT,ticker TEXT,row TEXT); CREATE TABLE releases(day TEXT,at_ns INTEGER)')
        db.execute('INSERT INTO predictions VALUES(?,?,?)',(day,'2330','{}'));db.execute('INSERT INTO releases VALUES(?,?)',(day,42))
    monkeypatch.setattr(m,'receipt',lambda root,name,value:None)
    result=m.forecast_receipt(tmp_path,day)
    assert result['count']==1 and result['release_ns']==42
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as db:
        db.executescript('CREATE TABLE predictions(day TEXT,ticker TEXT,row TEXT); CREATE TABLE releases(day TEXT,at_ns INTEGER)')
        db.execute('INSERT INTO predictions VALUES(?,?,?)',(day,'2330','{"preserved":true}'));db.execute('INSERT INTO releases VALUES(?,?)',(day,43))
    (tmp_path/'session_merge_status.json').write_text(json.dumps(dict(date=day,state='complete')))
    assert m.forecast_receipt(tmp_path,day)['release_ns']==43


def test_validation_stops_reports_before_capture_and_excludes_them_from_finalization():
    p=plan();units=module().render_units(p);unit=units[p['session']+'-final.service']
    assert datetime.fromisoformat(p['quiet'])<datetime.fromisoformat(p['start'])
    assert datetime.fromisoformat(p['preflight'])<datetime.fromisoformat(p['quiet'])-timedelta(seconds=45)
    targets=p['session']+'-report.service '+p['session']+'-report.timer'
    assert 'Conflicts='+targets in unit and 'After='+targets in unit


def test_quiet_receipt_can_write_only_the_isolated_runtime_under_the_sandbox():
    p=plan();unit=module().render_units(p)[p['session']+'-quiet.service']
    assert 'ProtectSystem=strict' in unit
    assert 'ReadWritePaths='+p['root']+'\n' in unit


def test_quiet_evidence_requires_a_report_running_across_the_original_stop_command():
    m=module();now=datetime(2026,11,16,11,24,tzinfo=ZoneInfo('Asia/Taipei'))
    quiet={'ExecMainStartTimestampMonotonic':'60000000','ExecMainStatus':'0'}
    report={'ExecMainStartTimestampMonotonic':'15000000','InactiveEnterTimestampMonotonic':'61000000','ActiveState':'failed'}
    result=m.quiet_evidence(quiet,report,now=now,monotonic_now=120)
    assert result['report_was_running'] and result['report_stopped']
    assert datetime.fromisoformat(result['stopped_at'])==now-timedelta(seconds=59)
    assert result['at']==now.isoformat()
    report['InactiveEnterTimestampMonotonic']='59000000'
    assert not m.quiet_evidence(quiet,report,now=now,monotonic_now=120)['report_was_running']


def test_failed_validation_notifies_retries_then_retires_only_its_own_monitor(tmp_path,monkeypatch):
    m=module();p=plan();monkeypatch.setattr(m,'PREFIX',tmp_path)
    p.update(prefix=str(tmp_path),root=str(tmp_path/'runtime'/p['session']),control=str(tmp_path/'validation-control'/p['session']))
    root=Path(p['root']);root.mkdir(parents=True)
    (root/'validation_result.json').write_text('{"passed":false}')
    notices=[];actions=[];delivered=[False]
    def save(path,value):path.write_text(json.dumps(value))
    ops=SimpleNamespace(MESSAGES={},private_json=save,send_notice=lambda prefix,code,fields:notices.append(code) or delivered[0])
    ops.assess=lambda *args:[]
    ops.snapshot=lambda *args:dict(collector_active=True,stream_ready=True)
    ops.check=lambda *args,**kwargs:dict(state='healthy',notified=True)
    monkeypatch.setattr(m,'load_module',lambda *args:ops)
    monkeypatch.setattr(m,'execute',lambda args,**kwargs:actions.append(args) or SimpleNamespace(returncode=0))
    assert m.run_phase(p,'watchdog')['notified'] is False
    assert not actions
    delivered[0]=True
    assert m.run_phase(p,'watchdog')['notified'] is True
    assert actions==[['systemctl','disable','--now',p['session']+'-watchdog.timer']]
    m.run_phase(p,'watchdog')
    assert notices==['validation_failed','validation_failed']


def test_missing_or_corrupt_backup_fails_the_gate_without_creating_a_database(tmp_path):
    m=module();path=tmp_path/'backup.sqlite3'
    assert m.backup_integrity(path) is False and not path.exists()
    path.write_bytes(b'synthetic corrupt backup')
    assert m.backup_integrity(path) is False
    path.unlink()
    with sqlite3.connect(path) as db:db.execute('create table evidence(value integer)')
    assert m.backup_integrity(path) is True
