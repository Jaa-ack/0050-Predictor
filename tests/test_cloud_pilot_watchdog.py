from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')


def module():
    path = Path(__file__).parents[1] / 'scripts/cloud_pilot_watchdog.py'
    assert path.exists(), 'Cloud incident detection and bounded recovery are missing'
    spec = importlib.util.spec_from_file_location('cloud_watchdog', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def protocol(tmp_path):
    p = dict(dates=['2026-11-16', '2026-11-27'], collection_start='09:30:00',
             collection_end='10:15:00', prediction_time='10:00:00',
             max_emission_lateness_seconds=1)
    (tmp_path / 'protocol.json').write_text(json.dumps(p))
    return p


def healthy(stamp):
    return dict(configured=True, source_matches=True, ntp=True, free_bytes=20 * 1024**3,
                database_ok=True, fatal=False, collector_active=True,
                collector_at=stamp.isoformat(), stream_ready=True,
                last_event_ns=int(stamp.timestamp() * 10**9), forecast_active=False,
                predictions=53, targets=53, release_ns=int(stamp.replace(hour=10, minute=0, second=0).timestamp() * 10**9),
                report_current=True, backup_current=True, backup_active=False, complete=False,
                preflight_current=True,preflight_active=False)

def test_daily_report_retries_and_sends_once_for_a_failed_paper_session(tmp_path):
    m=module();p=protocol(tmp_path);day=p['dates'][0]
    stamp=datetime(2026,11,16,10,31,tzinfo=TZ)
    (tmp_path/'supervision_policy.json').write_text(json.dumps(dict(daily_results_from=day)))
    (tmp_path/('daily_acceptance_'+day+'.json')).write_text(json.dumps(dict(core_live_passed=False)))
    horizon=dict(expected=50,correct=25,missing=0,unknown=1,timeouts=5)
    result=dict(date=day,protocol_sha256=m.protocol_fingerprint(p),publication_on_time=True,
                directions=[],horizons={'60':horizon,'300':horizon})
    (tmp_path/('daily_evaluation_'+day+'.json')).write_text(json.dumps(result))
    notices=[];state={}
    def send(code, fields):
        notices.append((code,fields))
        return len(notices)>1
    for _ in range(3):
        m.daily_results(tmp_path,p,stamp,state,send)
    assert [code for code,_ in notices]==['daily_paper_result','daily_paper_result']
    assert notices[-1][1]['operational_passed'] is False
    assert notices[-1][1]['result']['horizons']['60']['correct']==25


def test_report_recovery_waits_for_backup_without_consuming_restart_attempts(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,14,tzinfo=TZ)
    view=healthy(stamp);view.update(report_current=False,backup_active=True)
    assert {'code':'report_missing','action':None} in m.assess(p,stamp,view)
    view['backup_active']=False
    assert {'code':'report_missing','action':'start_report'} in m.assess(p,stamp,view)


def test_source_delay_is_alerted_even_when_receipt_backlog_is_zero(tmp_path):
    m=module();p=protocol(tmp_path);at=datetime(2026,11,16,9,45,tzinfo=TZ);view=healthy(at)
    view.update(collector_backlog=0,source_delay_known=True,source_delay_p95_seconds=300)
    assert {'code':'source_data_delayed','action':None} in m.assess(p,at,view)


def test_all_fallback_forecasts_are_alerted_without_republishing(tmp_path):
    m=module();p=protocol(tmp_path);at=datetime(2026,11,16,10,0,10,tzinfo=TZ);view=healthy(at)
    view.update(member_predictions=50,member_fallbacks=50)
    assert {'code':'forecast_inputs_degraded','action':None} in m.assess(p,at,view)


def test_dead_maintenance_worker_is_detected_and_resumed(tmp_path):
    m=module();p=protocol(tmp_path);at=datetime(2026,11,16,14,tzinfo=TZ);view=healthy(at)
    view.update(maintenance_interrupted=True,maintenance_retryable=True,maintenance_active=False)
    assert {'code':'maintenance_failed','action':'start_replay'} in m.assess(p,at,view)


def test_snapshot_exposes_source_delay_and_member_fallbacks(tmp_path,monkeypatch):
    from test_live_pilot import protocol as live_protocol,stamp
    from twquant.live_pilot import PilotStore
    m=module();p=live_protocol();store=PilotStore(tmp_path,p);store.emit(stamp());store.seal('2026-11-16',stamp(.1));store.close()
    (tmp_path/'collector_status.json').write_text(json.dumps(dict(date='2026-11-16',source_delay_samples=512,source_delay_p95_seconds=300)))
    (tmp_path/'pipeline_state.json').write_text(json.dumps(dict(state='running',phase='verify',attempts={'verify':1})))
    monkeypatch.setattr(m,'unit_active',lambda kind:False)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='yes',returncode=0))
    view=m.snapshot(tmp_path,tmp_path,stamp(4*3600))
    assert view.get('source_delay_known') and view['source_delay_p95_seconds']==300
    assert view['member_predictions']==50 and view['member_fallbacks']==50
    assert view['maintenance_interrupted'] and view['maintenance_retryable']


def test_new_acceptance_keeps_an_observed_source_delay_failure(tmp_path):
    m=module();p=protocol(tmp_path);day='2026-11-16'
    (tmp_path/'supervision_policy.json').write_text(json.dumps(dict(source_freshness_from=day)))
    at=datetime(2026,11,16,9,35,tzinfo=TZ);view=healthy(at);state={}
    view.update(source_delay_known=True,source_delay_p95_seconds=300)
    m.daily_acceptance(tmp_path,p,at,view,state,lambda *a:True)
    view['source_delay_p95_seconds']=.1
    m.daily_acceptance(tmp_path,p,at.replace(hour=10,minute=30),view,state,lambda *a:True)
    result=json.loads((tmp_path/('daily_acceptance_'+day+'.json')).read_text())
    assert result['checks'].get('source_event_freshness') is False
    assert result['observations']['max_source_delay_p95_seconds']==300


def test_fresh_full_coverage_can_pass_the_extended_acceptance(tmp_path):
    m=module();p=protocol(tmp_path);day='2026-11-16';at=datetime(2026,11,16,10,30,tzinfo=TZ)
    (tmp_path/'supervision_policy.json').write_text(json.dumps(dict(source_freshness_from=day)))
    observed=dict(samples=90,flow_samples=86,max_backlog=0,max_lag_seconds=1,unhealthy_seen=False,
        first_at='2026-11-16T09:30:00+08:00',last_at='2026-11-16T10:16:00+08:00',max_poll_gap_seconds=30,
        source_delay_samples=86,max_source_delay_p95_seconds=.2)
    view=healthy(at);view.update(member_predictions=50,member_fallbacks=0,closeout_source_fresh=True,
        closeout_date=day,closeout_state='day_collection_finished',closeout_complete=True,closeout_remaining=0,
        receipt_complete=True,critical_windows_observed=True,gap_count=0,materialization_pending=False,active_incidents=[])
    m.daily_acceptance(tmp_path,p,at,view,dict(daily_observations={day:observed}),lambda *a:True)
    assert json.loads((tmp_path/('daily_acceptance_'+day+'.json')).read_text())['passed']


def test_late_source_data_is_not_reported_recovered_when_capture_ends(tmp_path):
    m=module();protocol(tmp_path);at=datetime(2026,11,16,10,14,tzinfo=TZ);view=healthy(at);notices=[]
    view.update(source_delay_known=True,source_delay_p95_seconds=300)
    options=dict(inspect=lambda:view,control=lambda action:False,send=lambda code,fields:notices.append((code,fields)) or True)
    m.check(tmp_path,clock=lambda:at,**options)
    m.check(tmp_path,clock=lambda:at.replace(hour=13),**options)
    assert not any(code=='recovered' and fields.get('previous')=='source_data_delayed' for code,fields in notices)


def test_telegram_health_is_checked_during_maintenance_too(tmp_path):
    m=module();p=protocol(tmp_path);at=datetime(2026,11,16,14,tzinfo=TZ);view=healthy(at)
    view.update(maintenance_active=True,shared_service_health='unhealthy')
    assert {'code':'shared_service_unhealthy','action':None} in m.assess(p,at,view)


def test_unfresh_source_tail_is_alerted_after_the_capture_window(tmp_path):
    m=module();p=protocol(tmp_path);at=datetime(2026,11,16,10,20,tzinfo=TZ);view=healthy(at)
    view.update(closeout_source_fresh=False,closeout_date='2026-11-16')
    assert {'code':'source_tail_delayed','action':None} in m.assess(p,at,view)


def test_interrupted_backup_cleanup_preserves_published_data_and_respects_the_lock(tmp_path):
    import fcntl
    m=module();directory=tmp_path/'backups';directory.mkdir()
    (directory/'latest.sqlite3').write_bytes(b'published')
    orphan=directory/'snapshot.deadbeef.sqlite3';orphan.write_bytes(b'partial')
    (directory/'other.sqlite3').write_bytes(b'keep')
    with (tmp_path/'backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert m.clean_incomplete_backups(tmp_path)['state']=='busy'
        assert orphan.exists()
    result=m.clean_incomplete_backups(tmp_path)
    assert result['removed_bytes']==7 and not orphan.exists()
    assert (directory/'latest.sqlite3').read_bytes()==b'published'
    assert (directory/'other.sqlite3').exists()


def test_daily_backup_uses_complete_day_store_instead_of_growing_global_history(tmp_path):
    m=module();p=protocol(tmp_path);day=p['dates'][0];path=tmp_path/'sessions'/day;path.mkdir(parents=True)
    for folder,value in [(tmp_path,'historical'),(path,'today')]:
        with sqlite3.connect(folder/'pilot.sqlite3') as db:
            db.execute('create table marker(value text)');db.execute('insert into marker values(?)',(value,))
    (tmp_path/'capture_profile.json').write_text(json.dumps(dict(pipeline_from=day)))
    (tmp_path/'day_history_status.json').write_text(json.dumps(dict(date=day,state='complete',events_total=3)))
    (tmp_path/'receipt_watermark.json').write_text(json.dumps(dict(day=day,session_mode=True,events_total=3)))
    result=m.backup(tmp_path,stamp=datetime(2026,11,16,10,20,tzinfo=TZ),free_bytes=lambda:10**10)
    assert result['state']=='complete' and result['scope']=='daily_session'
    with sqlite3.connect(tmp_path/'backups/latest.sqlite3') as db:
        assert db.execute('select value from marker').fetchone()[0]=='today'


def test_running_pipeline_does_not_spend_recovery_attempts_on_queued_heavy_work(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,14,tzinfo=TZ);view=healthy(stamp)
    view.update(maintenance_active=True,backup_current=False,report_current=False,archive_expected=True,archive_current=False,archive_active=False)
    issues=m.assess(p,stamp,view)
    assert all(i['action'] is None for i in issues if i['code'] in ('backup_missing','report_missing','archive_failed'))


def test_failed_pipeline_has_a_fixed_notice_and_resumes_only_outside_capture(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,14,tzinfo=TZ);view=healthy(stamp)
    view.update(maintenance_error=True,maintenance_retryable=True,maintenance_active=False)
    assert {'code':'maintenance_failed','action':'start_replay'} in m.assess(p,stamp,view)
    assert 'maintenance_failed' in m.MESSAGES
    assert {'code':'maintenance_failed','action':None} in m.assess(p,stamp.replace(hour=9,minute=59),view)


def test_report_fingerprint_tracks_protocol_values_not_json_whitespace(tmp_path):
    m=module();p=protocol(tmp_path);before=m.report_fingerprint(tmp_path)
    (tmp_path/'protocol.json').write_text(json.dumps(p,indent=2))
    assert m.report_fingerprint(tmp_path)==before


def test_live_verification_is_separate_from_pending_maintenance_without_relaxing_total_pass(tmp_path):
    m=module();p=protocol(tmp_path);day='2026-11-16';at=datetime(2026,11,16,10,30,tzinfo=TZ)
    observed=dict(samples=90,flow_samples=86,max_backlog=0,max_lag_seconds=1,unhealthy_seen=False,
        first_at='2026-11-16T09:30:00+08:00',last_at='2026-11-16T10:16:00+08:00',max_poll_gap_seconds=30)
    state=dict(daily_observations={day:observed});view=healthy(at)
    view.update(receipt_complete=True,critical_windows_observed=True,gap_count=0,closeout_date=day,
        closeout_state='day_collection_finished',closeout_complete=True,closeout_remaining=0,
        materialization_pending=True,backup_current=False,report_current=False,active_incidents=['backup_missing'])
    notices=[];m.daily_acceptance(tmp_path,p,at,view,state,lambda code,fields:notices.append(code) or True)
    path=tmp_path/('daily_acceptance_'+day+'.json');value=json.loads(path.read_text());original=path.read_bytes()
    assert value['core_live_passed'] and value['maintenance_pending'] and not value['passed']
    assert notices==['maintenance_delayed']
    view.update(materialization_pending=False,backup_current=True,report_current=True,active_incidents=[])
    m.daily_acceptance(tmp_path,p,at+timedelta(hours=1),view,state,lambda *a:True)
    assert path.read_bytes()==original


def test_checkpoint_does_not_expire_a_report_of_the_same_committed_generation(tmp_path):
    import hashlib
    m=module();p=protocol(tmp_path);day='2026-11-16';at=datetime(2026,11,16,10,18,tzinfo=TZ)
    (tmp_path/'capture_profile.json').write_text(json.dumps(dict(pipeline_from=day)))
    (tmp_path/'session_merge_status.json').write_text(json.dumps(dict(date=day,state='complete',events_total=1,protocol=hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest())))
    db=sqlite3.connect(tmp_path/'pilot.sqlite3');db.execute('pragma journal_mode=WAL');db.execute('pragma wal_autocheckpoint=0');db.execute('create table evidence(value integer)');db.execute('insert into evidence values(1)');db.commit()
    m.begin_report(tmp_path,p,at);(tmp_path/'evaluation.json').write_text(json.dumps(dict(as_of=at.isoformat(),final=False)))
    assert m.record_report(tmp_path,p,at)['cached']
    db.execute('pragma wal_checkpoint(TRUNCATE)');db.close()
    assert m.report_current(tmp_path,p,at)


def test_watchdog_restarts_stale_collector_but_retains_provider_fatal_and_low_disk(tmp_path):
    m = module(); p = protocol(tmp_path)
    stamp = datetime(2026, 11, 16, 9, 35, tzinfo=TZ)
    snapshot = healthy(stamp)
    snapshot['collector_at'] = (stamp - timedelta(seconds=90)).isoformat()
    issues = m.assess(p, stamp, snapshot)
    assert {'code': 'collector_stalled', 'action': 'restart_collect'} in issues
    snapshot['fatal'] = True
    issues = m.assess(p, stamp, snapshot)
    assert any(i['code'] == 'provider_fatal' for i in issues)
    assert not any(i.get('action') in ('start_collect', 'restart_collect') for i in issues)
    snapshot['fatal'] = False; snapshot['free_bytes'] = 1
    assert not any(i.get('action') in ('start_collect', 'restart_collect') for i in m.assess(p, stamp, snapshot))


def test_missing_forecasts_are_alerted_without_backdating_or_restarting_after_deadline(tmp_path):
    m = module(); p = protocol(tmp_path)
    stamp = datetime(2026, 11, 16, 10, 0, 10, tzinfo=TZ)
    snapshot = healthy(stamp); snapshot.update(predictions=0, release_ns=None)
    assert {'code': 'forecast_missing', 'action': 'start_forecast'} in m.assess(p, stamp, snapshot)
    after = stamp.replace(minute=6)
    issues = m.assess(p, after, snapshot)
    assert any(i['code'] == 'forecast_missing' for i in issues)
    assert not any(i.get('action') == 'start_forecast' for i in issues)
    snapshot.update(predictions=53, release_ns=int(stamp.timestamp() * 10**9))
    assert any(i['code'] == 'forecast_late' for i in m.assess(p, stamp, snapshot))


def test_monitor_never_starts_market_workers_on_weekends_or_outside_frozen_dates(tmp_path):
    m = module(); p = protocol(tmp_path)
    stamp = datetime(2026, 11, 15, 10, 0, tzinfo=TZ)
    snapshot = healthy(stamp); snapshot.update(collector_active=False, predictions=0, release_ns=None)
    assert not any(i.get('action') for i in m.assess(p, stamp, snapshot))
    snapshot['source_matches'] = False
    assert any(i['code'] == 'source_integrity_failed' for i in m.assess(p, stamp, snapshot))


def test_recovery_is_rate_limited_and_notifications_retry_without_duplicate_success(tmp_path):
    m = module(); protocol(tmp_path)
    clock = [datetime(2026, 11, 16, 9, 35, tzinfo=TZ)]
    snapshot = healthy(clock[0]); snapshot['collector_active'] = False
    actions = []; notices = []; delivery = [False]
    def control(action): actions.append(action); return True
    def send(code, fields): notices.append(code); return delivery[0]
    kwargs = dict(clock=lambda: clock[0], inspect=lambda: snapshot, control=control, send=send)
    m.check(tmp_path, **kwargs)
    assert actions == ['start_collect'] and notices == ['collector_inactive']
    delivery[0] = True
    m.check(tmp_path, **kwargs)
    assert actions == ['start_collect'] and notices == ['collector_inactive'] * 2
    m.check(tmp_path, **kwargs)
    assert len(notices) == 2
    for _ in range(5):
        clock[0] += timedelta(seconds=121)
        m.check(tmp_path, **kwargs)
    assert actions == ['start_collect'] * 3
    snapshot.update(collector_active=True, collector_at=clock[0].isoformat(), last_event_ns=int(clock[0].timestamp()*10**9))
    m.check(tmp_path, **kwargs)
    assert notices[-1] == 'recovered'


def test_rolling_backup_is_consistent_private_and_preserves_original_on_low_space(tmp_path):
    m = module(); protocol(tmp_path)
    db = sqlite3.connect(tmp_path / 'pilot.sqlite3')
    db.execute('create table evidence(value text)')
    db.execute("insert into evidence values('real-evidence-placeholder')"); db.commit(); db.close()
    stamp = datetime(2026, 11, 16, 10, 20, tzinfo=TZ)
    result = m.backup(tmp_path, stamp=stamp, free_bytes=lambda: 100 * 1024**3)
    assert result['state'] == 'complete'
    dest = tmp_path / 'backups/latest.sqlite3'
    with sqlite3.connect(dest) as copy:
        assert copy.execute('select value from evidence').fetchone()[0] == 'real-evidence-placeholder'
    assert dest.stat().st_mode & 0o777 == 0o600
    previous = dest.read_bytes(); original = (tmp_path / 'pilot.sqlite3').read_bytes()
    result = m.backup(tmp_path, stamp=stamp, free_bytes=lambda: 1)
    assert result['state'] == 'insufficient_space'
    assert dest.read_bytes() == previous and (tmp_path / 'pilot.sqlite3').read_bytes() == original


def test_final_notification_is_retried_before_monitor_retirement(tmp_path):
    m = module(); protocol(tmp_path)
    stamp = datetime(2026, 11, 27, 14, tzinfo=TZ)
    snapshot = healthy(stamp); snapshot['complete'] = True
    actions = []; allowed = [False]
    kwargs = dict(clock=lambda: stamp, inspect=lambda: snapshot,
                  control=lambda action: actions.append(action) or True,
                  send=lambda code, fields: allowed[0])
    m.check(tmp_path, **kwargs)
    assert actions == []
    allowed[0] = True
    m.check(tmp_path, **kwargs)
    assert actions == ['retire_monitor']


def test_sender_does_not_leak_telegram_credentials_on_transport_failure(tmp_path, capsys):
    m = module(); secret = tmp_path / '.secrets'; secret.mkdir(mode=0o700)
    config = secret / 'telegram.json'
    config.write_text(json.dumps({'token': 'fake-token-must-not-leak', 'chat_id': 123}))
    config.chmod(0o600)
    def fail(*args, **kwargs): raise RuntimeError('fake-token-must-not-leak')
    assert m.send_notice(tmp_path, 'collector_inactive', {}, opener=fail) is False
    assert 'fake-token-must-not-leak' not in capsys.readouterr().out


def test_resolved_outage_is_reported_even_if_original_notification_failed(tmp_path):
    m = module(); protocol(tmp_path)
    stamp = datetime(2026, 11, 16, 9, 35, tzinfo=TZ)
    view = healthy(stamp); view['collector_active'] = False
    notices = []
    kwargs = dict(clock=lambda: stamp, inspect=lambda: view, control=lambda action: True)
    m.check(tmp_path, send=lambda code, fields: False, **kwargs)
    view['collector_active'] = True
    m.check(tmp_path, send=lambda code, fields: notices.append((code, fields)) or True, **kwargs)
    assert ('recovered', {'previous': 'collector_inactive'}) in notices


def test_premarket_probe_failure_is_retried_before_collection_without_market_relabeling(tmp_path):
    m=module();p=protocol(tmp_path)
    stamp=datetime(2026,11,16,8,42,tzinfo=TZ)
    view=healthy(stamp);view.update(preflight_current=False,preflight_active=False)
    assert {'code':'preflight_failed','action':'start_preflight'} in m.assess(p,stamp,view)
    later=stamp.replace(hour=10,minute=6)
    assert not any(i.get('action')=='start_preflight' for i in m.assess(p,later,view))


def test_monitor_exception_sends_fixed_diagnostic_without_raw_error(tmp_path,monkeypatch,capsys):
    m=module();notices=[]
    monkeypatch.setattr(m,'send_notice',lambda prefix,code,fields:notices.append(code) or True)
    assert m.main(['--root',str(tmp_path),'--prefix',str(tmp_path)])==1
    assert notices==['watchdog_failed']
    assert json.loads(capsys.readouterr().out)=={'state':'error','code':'watchdog_operation_failed'}


def test_backlog_and_recorded_gaps_alert_without_restarting_a_working_collector(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,9,45,tzinfo=TZ);view=healthy(stamp)
    view.update(collector_backlog=2000,gap_count=1,wal_bytes=600*1024**2)
    issues=m.assess(p,stamp,view)
    for code in ['collector_backlog','market_data_gaps','wal_large']:
        assert {'code':code,'action':None} in issues
    assert not any(x.get('action') in ('start_collect','restart_collect') for x in issues)


def test_missing_or_partial_closeout_is_not_mistaken_for_a_successful_capture(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,10,18,tzinfo=TZ);view=healthy(stamp)
    assert {'code':'collector_closeout_incomplete','action':None} in m.assess(p,stamp,view)
    view.update(closeout_date='2026-11-16',closeout_state='day_collection_finished',closeout_complete=True,closeout_remaining=0)
    assert not any(x['code']=='collector_closeout_incomplete' for x in m.assess(p,stamp,view))
    view['closeout_remaining']=3
    assert any(x['code']=='collector_closeout_incomplete' for x in m.assess(p,stamp,view))


def test_busy_database_cannot_resolve_a_known_late_forecast(tmp_path):
    m=module();protocol(tmp_path);stamp=datetime(2026,11,16,10,1,tzinfo=TZ);view=healthy(stamp)
    view['release_ns']+=2*10**9;notices=[]
    kwargs=dict(clock=lambda:stamp,inspect=lambda:view,control=lambda action:True,
        send=lambda code,fields:notices.append((code,fields)) or True)
    m.check(tmp_path,**kwargs);assert any(c=='forecast_late' for c,f in notices)
    view.update(database_ok=False,database_recovering=True)
    m.check(tmp_path,**kwargs)
    assert not any(c=='recovered' and f.get('previous')=='forecast_late' for c,f in notices)
    state=json.loads((tmp_path/'watchdog_state.json').read_text())
    assert 'forecast_late' in state['incidents'] and 'database_recovering' in state['incidents']


def test_snapshot_exposes_real_backlog_closeout_and_only_todays_gaps(tmp_path,monkeypatch):
    m=module();protocol(tmp_path);stamp=datetime(2026,11,16,9,45,tzinfo=TZ)
    (tmp_path/'collector_status.json').write_text('{"backlog":7}')
    (tmp_path/'collector_closeout.json').write_text('{"date":"2026-11-16","state":"day_collection_finished","complete":true,"remaining_messages":0}')
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as db:
        db.executescript('CREATE TABLE events(id INTEGER,received_ns INTEGER); CREATE TABLE predictions(day TEXT); CREATE TABLE releases(day TEXT,at_ns INTEGER); CREATE TABLE gaps(start_ns INTEGER,end_ns INTEGER);')
        ns=int(stamp.timestamp())*10**9
        db.executemany('INSERT INTO gaps VALUES(?,?)',[(ns,ns+1),(ns-86400*10**9,ns-86400*10**9+1)])
    monkeypatch.setattr(m,'unit_active',lambda kind:False)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='yes',returncode=0))
    view=m.snapshot(tmp_path,tmp_path,stamp)
    assert view['database_ok'] and view['gap_count']==1 and view['collector_backlog']==7
    assert view['closeout_complete'] and view['closeout_remaining']==0 and view['wal_bytes']==0
    error=sqlite3.OperationalError('synthetic recovery');error.sqlite_errorcode=261
    def recovering(*a,**k):raise error
    monkeypatch.setattr(m.sqlite3,'connect',recovering)
    view=m.snapshot(tmp_path,tmp_path,stamp)
    assert not view['database_ok'] and view['database_recovering']


def test_report_cache_skips_unchanged_data_but_expires_on_writes_and_new_matured_days(tmp_path):
    m=module();p=protocol(tmp_path);p['dates']=['2026-11-16','2026-11-17','2026-11-27']
    (tmp_path/'protocol.json').write_text(json.dumps(p))
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as db:db.execute('create table evidence(value integer)')
    start=datetime(2026,11,16,10,18,tzinfo=TZ);m.begin_report(tmp_path,p,start)
    (tmp_path/'evaluation.json').write_text(json.dumps(dict(as_of=start.isoformat(),final=False)))
    assert m.record_report(tmp_path,p,start+timedelta(seconds=1))['cached']
    assert m.report_current(tmp_path,p,start.replace(hour=23))
    assert not m.report_wanted(tmp_path,p,start.replace(hour=23))
    assert m.report_wanted(tmp_path,p,start+timedelta(days=1))
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as db:db.execute('insert into evidence values(1)')
    assert m.report_wanted(tmp_path,p,start)


def test_report_cache_rejects_data_changed_during_report_and_requires_due_final_report(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,27,13,45,tzinfo=TZ)
    (tmp_path/'protocol.json').write_text(json.dumps(p))
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as db:db.execute('create table evidence(value integer)')
    m.begin_report(tmp_path,p,stamp)
    with sqlite3.connect(tmp_path/'pilot.sqlite3') as db:db.execute('insert into evidence values(1)')
    (tmp_path/'evaluation.json').write_text(json.dumps(dict(as_of=stamp.isoformat(),final=True)))
    assert not m.record_report(tmp_path,p,stamp+timedelta(seconds=1))['cached']
    m.begin_report(tmp_path,p,stamp)
    (tmp_path/'evaluation.json').write_text(json.dumps(dict(as_of=stamp.isoformat(),final=False)))
    assert not m.record_report(tmp_path,p,stamp+timedelta(seconds=1))['cached']
    (tmp_path/'evaluation.json').write_text(json.dumps(dict(as_of=stamp.isoformat(),final=True)))
    assert m.record_report(tmp_path,p,stamp+timedelta(seconds=1))['cached']
    assert m.report_current(tmp_path,p,stamp+timedelta(minutes=10))


def test_daily_acceptance_retains_a_bad_live_sample_and_retries_failure_delivery(tmp_path):
    m=module();protocol(tmp_path);clock=[datetime(2026,11,16,9,45,tzinfo=TZ)];view=healthy(clock[0]);view.update(collector_backlog=2000,shared_service_health='unhealthy')
    notices=[];delivered=[False]
    def send(code,fields):notices.append(code);return delivered[0] if code=='daily_validation_failed' else True
    args=dict(clock=lambda:clock[0],inspect=lambda:view,control=lambda action:True,send=send)
    m.check(tmp_path,**args)
    clock[0]=clock[0].replace(hour=10,minute=31);view.update(healthy(clock[0]));view.update(collector_backlog=0,shared_service_health='healthy',closeout_date='2026-11-16',closeout_state='day_collection_finished',closeout_complete=True,closeout_remaining=0,critical_windows_observed=True,gap_count=0)
    m.check(tmp_path,**args);path=tmp_path/'daily_acceptance_2026-11-16.json';original=path.read_bytes();result=json.loads(original)
    assert not result['passed'] and not result['checks']['shared_service_stable'] and not result['checks']['data_flow_stable']
    delivered[0]=True;m.check(tmp_path,**args);m.check(tmp_path,**args)
    assert path.read_bytes()==original and notices.count('daily_validation_failed')==2


def test_daily_flow_acceptance_respects_the_existing_two_minute_startup_grace(tmp_path):
    m=module();p=protocol(tmp_path);state={};stamp=datetime(2026,11,16,9,30,tzinfo=TZ);view=healthy(stamp)
    view.update(collector_backlog=2000,last_event_ns=0,shared_service_health='healthy')
    m.daily_acceptance(tmp_path,p,stamp,view,state,lambda *a:True)
    stamp=stamp.replace(minute=35);view.update(healthy(stamp));view.update(collector_backlog=0,shared_service_health='healthy')
    m.daily_acceptance(tmp_path,p,stamp,view,state,lambda *a:True)
    observed=state['daily_observations']['2026-11-16']
    assert observed['flow_samples']==1 and observed['max_backlog']==0 and observed['max_lag_seconds']==0


def test_a_single_late_monitor_sample_cannot_pass_daily_monitoring_coverage(tmp_path):
    m=module();p=protocol(tmp_path);state={};stamp=datetime(2026,11,16,10,16,tzinfo=TZ);view=healthy(stamp)
    view.update(collector_backlog=0,shared_service_health='healthy')
    m.daily_acceptance(tmp_path,p,stamp,view,state,lambda *a:True)
    m.daily_acceptance(tmp_path,p,stamp.replace(minute=31),view,state,lambda *a:True)
    result=json.loads((tmp_path/'daily_acceptance_2026-11-16.json').read_text())
    assert not result['checks']['monitoring_coverage']


def test_shared_service_does_not_recover_merely_because_observation_window_ended(tmp_path):
    m=module();protocol(tmp_path);stamp=[datetime(2026,11,16,9,45,tzinfo=TZ)];view=healthy(stamp[0]);view['shared_service_health']='unhealthy';notices=[]
    args=dict(clock=lambda:stamp[0],inspect=lambda:view,control=lambda action:True,send=lambda code,fields:notices.append((code,fields)) or True)
    m.check(tmp_path,**args);stamp[0]=stamp[0].replace(hour=10,minute=31);view['shared_service_health']=None
    result=m.check(tmp_path,**args)
    assert not any(c=='recovered' and f.get('previous')=='shared_service_unhealthy' for c,f in notices)
    assert result['state']=='incidents' and 'shared_service_unhealthy' in result['codes']
    view['shared_service_health']='healthy';m.check(tmp_path,**args)
    assert any(c=='recovered' and f.get('previous')=='shared_service_unhealthy' for c,f in notices)


def test_daily_acceptance_cannot_pass_with_a_current_incident(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,10,31,tzinfo=TZ);view=healthy(stamp)
    view.update(closeout_date='2026-11-16',closeout_state='day_collection_finished',closeout_complete=True,closeout_remaining=0,critical_windows_observed=True,gap_count=0,active_incidents=['checkpoint_failed'])
    observed=dict(samples=90,flow_samples=80,max_backlog=0,max_lag_seconds=0,unhealthy_seen=False,first_at=stamp.replace(hour=9,minute=30).isoformat(),last_at=stamp.replace(minute=16).isoformat(),max_poll_gap_seconds=31)
    m.daily_acceptance(tmp_path,p,stamp,view,{'daily_observations':{'2026-11-16':observed}},lambda *a:True)
    assert not json.loads((tmp_path/'daily_acceptance_2026-11-16.json').read_text())['passed']


def test_durable_receipt_progress_prevents_destructive_restart_on_stale_metadata(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,9,45,tzinfo=TZ);view=healthy(stamp)
    view.update(collector_at=stamp.replace(minute=40).isoformat(),receipt_progressing=True)
    issues=m.assess(p,stamp,view)
    assert not any(i.get('action')=='restart_collect' for i in issues)
    assert any(i['code']=='collector_backlog' for i in issues)


def test_pending_receipts_defer_reports_and_backup_without_losing_data(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,10,25,tzinfo=TZ);view=healthy(stamp)
    view.update(materialization_pending=True,report_current=False,backup_current=False)
    assert not any(i.get('action') in ('start_report','start_backup') for i in m.assess(p,stamp,view))
    (tmp_path/'receipt_watermark.json').write_text('{"events_total":12}');(tmp_path/'materializer_status.json').write_text('{"events_total":1}')
    assert m.backup(tmp_path,stamp=stamp)['state']=='receipts_pending'


def test_durable_disk_backlog_is_judged_by_lag_instead_of_ram_capacity(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,9,45,tzinfo=TZ);view=healthy(stamp)
    view.update(durable_receipts=True,collector_backlog=5000,last_event_ns=int((stamp.timestamp()-3)*1e9))
    assert not any(i['code']=='collector_backlog' for i in m.assess(p,stamp,view))
    view['last_event_ns']=int((stamp.timestamp()-6)*1e9)
    assert any(i['code']=='collector_backlog' for i in m.assess(p,stamp,view))


def test_daily_flow_keeps_the_same_lag_gate_for_persisted_receipts(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,9,45,tzinfo=TZ);view=healthy(stamp)
    view.update(durable_receipts=True,collector_backlog=5000,last_event_ns=int((stamp.timestamp()-3)*1e9),shared_service_health='healthy')
    state={};m.daily_acceptance(tmp_path,p,stamp,view,state,lambda *a:True)
    m.daily_acceptance(tmp_path,p,stamp.replace(hour=10,minute=31),view,state,lambda *a:True)
    result=json.loads((tmp_path/'daily_acceptance_2026-11-16.json').read_text())
    assert result['checks']['data_flow_stable']


def test_report_and_backup_wait_for_daily_data_merge(tmp_path):
    m=module();(tmp_path/'receipt_watermark.json').write_text('{"day":"2026-11-18","session_mode":true,"events_total":50}')
    (tmp_path/'materializer_status.json').write_text('{"date":"2026-11-18","events_total":50}')
    assert m.receipts_pending(tmp_path)
    (tmp_path/'session_merge_status.json').write_text('{"date":"2026-11-18","state":"complete","events_total":50}')
    assert not m.receipts_pending(tmp_path)


def test_finished_merge_triggers_backup_before_the_daily_acceptance_deadline(tmp_path):
    m=module();p=protocol(tmp_path);stamp=datetime(2026,11,16,10,23,tzinfo=TZ);view=healthy(stamp)
    view.update(backup_current=False,backup_active=False,materialization_pending=False)
    assert any(i.get('action')=='start_backup' for i in m.assess(p,stamp,view))
    view['materialization_pending']=True
    assert not any(i['code']=='backup_missing' for i in m.assess(p,stamp,view))


def test_projection_monitor_tracks_receipt_progress_after_the_fixed_input_cutoff(tmp_path,monkeypatch):
    m=module();protocol(tmp_path);stamp=datetime(2026,11,16,10,4,tzinfo=TZ);day=stamp.date().isoformat()
    path=tmp_path/'projections'/day;path.mkdir(parents=True)
    (tmp_path/'capture_profile.json').write_text(json.dumps(dict(projection_from=day,session_mode=True)))
    (tmp_path/'collector_status.json').write_text(json.dumps(dict(runtime='cloud_projection_v4',session_mode=True,backlog=30)))
    (tmp_path/'materializer_status.json').write_text(json.dumps(dict(date=day,projection_only=True,events_total=100,last_received_ns=int(stamp.timestamp()*10**9))))
    with sqlite3.connect(path/'pilot.sqlite3') as db:
        db.executescript('CREATE TABLE events(id INTEGER,received_ns INTEGER); CREATE TABLE predictions(day TEXT); CREATE TABLE releases(day TEXT,at_ns INTEGER); CREATE TABLE gaps(start_ns INTEGER,end_ns INTEGER);')
        db.execute('insert into events values(1,?)',(int((stamp-timedelta(minutes=4)).timestamp()*10**9),))
    monkeypatch.setattr(m,'unit_active',lambda kind:False)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='yes',returncode=0))
    view=m.snapshot(tmp_path,tmp_path,stamp)
    assert view['database_ok'] and view['durable_receipts']
    assert view['last_event_ns']==int(stamp.timestamp()*10**9)
    assert m.receipts_pending(tmp_path) is False  # No watermark exists in this minimal probe.


def test_projection_cursor_cannot_substitute_for_completed_historical_replay(tmp_path):
    m=module();day='2026-11-18'
    (tmp_path/'receipt_watermark.json').write_text(json.dumps(dict(day=day,session_mode=True,events_total=50)))
    (tmp_path/'materializer_status.json').write_text(json.dumps(dict(date=day,projection_only=True,events_total=50)))
    assert m.receipts_pending(tmp_path)


def test_receipt_progress_uses_a_fresh_clock_after_inspection_work(tmp_path,monkeypatch):
    import os
    m=module();protocol(tmp_path);stamp=datetime(2026,11,16,9,45,tzinfo=TZ)
    directory=tmp_path/'receipts';directory.mkdir();segment=directory/'00000001.twqj';segment.write_bytes(b'receipt')
    os.utime(segment,(stamp.timestamp()+1,stamp.timestamp()+1))
    monkeypatch.setattr(m,'now',lambda:stamp+timedelta(seconds=2))
    monkeypatch.setattr(m,'unit_active',lambda kind:False)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='yes',returncode=0))
    assert m.snapshot(tmp_path,tmp_path,stamp)['receipt_progressing']
