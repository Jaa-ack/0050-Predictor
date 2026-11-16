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
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout='yes'))
    view=m.snapshot(tmp_path,tmp_path,stamp)
    assert view['database_ok'] and view['gap_count']==1 and view['collector_backlog']==7
    assert view['closeout_complete'] and view['closeout_remaining']==0 and view['wal_bytes']==0
    error=sqlite3.OperationalError('synthetic recovery');error.sqlite_errorcode=261
    def recovering(*a,**k):raise error
    monkeypatch.setattr(m.sqlite3,'connect',recovering)
    view=m.snapshot(tmp_path,tmp_path,stamp)
    assert not view['database_ok'] and view['database_recovering']
