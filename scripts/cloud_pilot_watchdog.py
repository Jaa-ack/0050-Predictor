"""Private, bounded pilot supervision; never changes frozen forecasts or models."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')
RESERVE = 4 * 1024**3
ACTIONS = {
    'start_collect': ['start', 'twquant-pilot-collect.service'],
    'restart_collect': ['restart', 'twquant-pilot-collect.service'],
    'start_forecast': ['start', 'twquant-pilot-forecast.service'],
    'start_report': ['start', 'twquant-pilot-report.service'],
    'start_backup': ['start', 'twquant-pilot-backup.service'],
    'start_preflight': ['start', 'twquant-pilot-preflight.service'],
    'start_archive': ['start', 'twquant-pilot-archive.service'],
    'retire_monitor': ['disable', '--now', 'twquant-pilot-watchdog.timer'],
}
MESSAGES = {
    'ready': '雲端上線驗證完成：富邦105/105訂閱確認，研究排程與監控已啟用。這是非交易日連線驗證，尚非盤中完整性驗收。',
    'configuration_missing': '研究私人設定或行情存取驗證缺失，需要檢查設定；不會自動替換憑證。',
    'preflight_failed': '當日盤前富邦連線驗證尚未成功。收集開始前會有限重試；不會更換憑證。',
    'source_integrity_failed': '凍結來源校驗失敗，停止自動復原，不會更換模型或改寫協定。',
    'clock_unsynchronized': '主機時鐘未確認同步，請檢查對時；晚發報仍按原規則計分。',
    'disk_low': '磁碟低於保留量，收集將受限；不刪除資料、不購買容量。',
    'database_unreadable': '研究資料庫無法讀取，需人工檢查；不會刪除或重建原資料庫。',
    'database_recovering': '研究資料庫正在恢復WAL或忙碌，暫不能確認資料；不會把既有異常宣告恢復。',
    'provider_fatal': '行情程式記錄供應商或訊息錯誤，保留原始失敗狀態，需人工檢查。',
    'collector_inactive': '收集窗口內採集服務未運行。',
    'collector_stalled': '採集程序心跳逾時。',
    'feed_not_ready': '行情訂閱持續未就緒。',
    'market_data_stale': '超過120秒未寫入新行情。缺口與受影響答案會保留。',
    'collector_backlog': '採集佇列或落盤延遲偏高，可能影響輸入新鮮度；先告警，不以重啟丟棄待存資料。',
    'market_data_gaps': '當日已記錄觀測缺口，受影響答案與失敗分母保留；恢復連線不能補回缺失行情。',
    'collector_closeout_incomplete': '採集窗口已結束，但收尾完成回執缺失、積壓未清空或有明確遺失；不宣稱全部資料已保存。',
    'wal_large': '研究WAL已超過512MiB，可能增加I/O與恢復成本；不在發報路徑強制整理或刪除WAL。',
    'checkpoint_failed': '有界WAL整理未完成或被中斷，下一個允許時段會再試；不刪資料、不改發布門檻。',
    'receipt_pipeline_failed': '耐久收件或批次匯入發生錯誤；私人收件日誌保留，未提交資料不宣稱已匯入。',
    'archive_failed': '本機壓縮歸檔尚未完成或驗證失敗；保留來源與既有備份，不刪除唯一副本。',
    'shared_service_unhealthy': '原Telegram容器的健康狀態異常或無法確認；研究監控不會擅自重啟原服務。',
    'daily_validation_failed': '當日盤中營運驗收未通過，私人逐項結果已保存；晚發、缺口及失敗分母保留。',
    'supervision_ready': '下一個宣告交易日的研究排程與Telegram通知通道已完成設定驗證；這不是盤中驗收通過或準確率達標通知。',
    'forecast_missing': '10:00完整預測或發布封印缺失。補發不會倒填時間，也不會取得準時信用。',
    'forecast_late': '預測發布逾時；原始發布時間及扣分保留。',
    'backup_missing': '當日一致性備份未完成，會在空間足夠時有限重試。',
    'report_missing': '進度或期末報告未如期更新。',
    'recovery_exhausted': '當日自動復原次數已達上限，需要人工介入。',
    'watchdog_failed': '研究監控本身發生錯誤，systemd會重試；需要檢查監控狀態。',
    'recovered': '先前的研究運作異常已通過後續狀態檢查；既有缺口與失敗紀錄保留。',
    'complete': '十交易日期末評估已完成，私人報告已保存。研究監控即將結束，Telegram原有提醒不受影響。',
    'monitor_window_ended': '研究監控期限已到，期末報告仍未確認完成；資料與失敗紀錄保留，需人工檢查。',
}


def now():
    return datetime.now(TZ)


def private_json(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(value, out, allow_nan=False)
            out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def age(value, stamp):
    try:
        return (stamp - datetime.fromisoformat(value)).total_seconds()
    except (TypeError, ValueError):
        return float('inf')


def report_key(protocol,stamp):
    def decision(day):return datetime.fromisoformat(day+'T'+protocol.get('prediction_time','10:00:00')).replace(tzinfo=TZ)
    days=[d for d in protocol['dates'] if decision(d)+timedelta(seconds=301)<=stamp]
    if not days or stamp>decision(protocol['dates'][-1]).replace(hour=14,minute=0,second=0)+timedelta(days=1):return None
    return dict(day=days[-1],final=stamp>=decision(protocol['dates'][-1])+timedelta(hours=3,minutes=40))


def report_fingerprint(root):
    files={}
    for name in ['pilot.sqlite3','pilot.sqlite3-wal']:
        try:stat=(root/name).stat()
        except FileNotFoundError:continue
        if name.endswith('-wal') and stat.st_size==0:continue
        files[name]=[stat.st_size,stat.st_mtime_ns]
    return dict(files=files,protocol=hashlib.sha256((root/'protocol.json').read_bytes()).hexdigest())


def report_current(root,protocol,stamp):
    cache=read_json(root/'report_cache.json',{});key=report_key(protocol,stamp)
    report=read_json(root/'evaluation.json',{})
    try:
        return bool(key and cache.get('key')==key and (not key['final'] or report.get('final') is True)
            and cache.get('fingerprint')==report_fingerprint(root)
            and cache.get('report_sha256')==hashlib.sha256((root/'evaluation.json').read_bytes()).hexdigest())
    except OSError:return False


def report_wanted(root,protocol,stamp):
    return report_key(protocol,stamp) is not None and not report_current(root,protocol,stamp)


def receipts_pending(root):
    watermark=read_json(root/'receipt_watermark.json',{});durable=watermark.get('events_total',0)
    imported=read_json(root/'materializer_status.json',{})
    count=imported.get('events_total',0) if not watermark.get('session_mode') or watermark.get('day')==imported.get('date') else 0
    if durable>count:return True
    if watermark.get('session_mode'):
        merged=read_json(root/'session_merge_status.json',{})
        return merged.get('date')!=watermark.get('day') or merged.get('state')!='complete' or merged.get('events_total',0)<durable
    return False


def begin_report(root,protocol,stamp):
    private_json(root/'report_input.json',dict(at=stamp.isoformat(),fingerprint=report_fingerprint(root)))


def record_report(root,protocol,stamp):
    source=read_json(root/'report_input.json',{});report=read_json(root/'evaluation.json',{})
    key=report_key(protocol,stamp);fingerprint=report_fingerprint(root)
    valid=bool(key and source.get('fingerprint')==fingerprint and report.get('as_of','')>=source.get('at','~')
        and (not key['final'] or report.get('final') is True))
    if valid:private_json(root/'report_cache.json',dict(at=stamp.isoformat(),key=key,fingerprint=fingerprint,
        report_sha256=hashlib.sha256((root/'evaluation.json').read_bytes()).hexdigest()))
    return dict(cached=valid)


def daily_acceptance(root,protocol,stamp,current,state,send):
    day=stamp.date().isoformat();policy=read_json(root/'supervision_policy.json',{})
    if day not in protocol['dates'] or day<policy.get('daily_checks_from',protocol['dates'][0]):return
    start=datetime.fromisoformat(day+'T'+protocol.get('collection_start','09:30:00')).replace(tzinfo=TZ)
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
    observed=state.setdefault('daily_observations',{}).setdefault(day,dict(samples=0,max_backlog=0,max_lag_seconds=0,unhealthy_seen=False))
    observed.setdefault('flow_samples',0)
    if start<=stamp<end+timedelta(seconds=150):
        if observed.get('last_at'):observed['max_poll_gap_seconds']=max(observed.get('max_poll_gap_seconds',0),age(observed['last_at'],stamp))
        observed.setdefault('first_at',stamp.isoformat());observed['last_at']=stamp.isoformat()
        backlog=current.get('collector_backlog') or 0;observed['samples']+=1
        if stamp>=start+timedelta(seconds=120):
            observed['flow_samples']+=1;observed['max_backlog']=max(observed['max_backlog'],backlog)
            observed['ram_backlog_high_seen']=observed.get('ram_backlog_high_seen',False) or (not current.get('durable_receipts') and backlog>=1024)
            if backlog:observed['max_lag_seconds']=max(observed['max_lag_seconds'],stamp.timestamp()-current.get('last_event_ns',0)/1e9)
        if current.get('shared_service_health')!='healthy':observed['unhealthy_seen']=True
    if stamp.strftime('%H:%M:%S')<'10:30:00':return
    path=root/('daily_acceptance_'+day+'.json');result=read_json(path)
    if result is None:
        decision=datetime.fromisoformat(day+'T'+protocol.get('prediction_time','10:00:00')).replace(tzinfo=TZ)
        delay=current.get('release_ns',0)/1e9-decision.timestamp() if current.get('release_ns') else None
        checks=dict(environment=all(current.get(k) for k in ['configured','source_matches','database_ok','ntp']),
            no_active_incidents=not current.get('active_incidents'),
            materialization_current=not current.get('materialization_pending'),
            receipts_complete=current.get('receipt_complete') is not False,
            preflight=current.get('preflight_current') is True,
            full_grid=current.get('predictions')==current.get('targets',53),publication_on_time=delay is not None and 0<=delay<=protocol.get('max_emission_lateness_seconds',1),
            both_windows_observed=current.get('critical_windows_observed') is True,no_recorded_gaps=current.get('gap_count')==0,
            capture_closeout=current.get('closeout_date')==day and current.get('closeout_state')=='day_collection_finished' and current.get('closeout_complete') is True and current.get('closeout_remaining')==0,
            backup=current.get('backup_current') is True,report=current.get('report_current') is True,
            data_flow_stable=observed['flow_samples']>0 and not observed.get('ram_backlog_high_seen',observed['max_backlog']>=1024 and not current.get('durable_receipts')) and observed['max_lag_seconds']<=5,
            shared_service_stable=observed['samples']>0 and not observed['unhealthy_seen'],provider_ok=not current.get('fatal'))
        checks['monitoring_coverage']=(observed['samples']>=30 and bool(observed.get('first_at')) and bool(observed.get('last_at')) and age(observed.get('first_at'),start+timedelta(seconds=120))>=0
            and age(observed.get('last_at'),end)<=0 and observed.get('max_poll_gap_seconds',float('inf'))<=90)
        result=dict(at=stamp.isoformat(),date=day,scope='daily_operations_not_accuracy_claim',checks=checks,passed=all(checks.values()),publication_delay_seconds=delay,observations=observed.copy())
        private_json(path,result)
    notified=state.setdefault('daily_notifications',{})
    if not result['passed'] and not notified.get(day):notified[day]=send('daily_validation_failed',{})


def assess(protocol, stamp, snapshot):
    """Return fixed incident codes and allowlisted recovery suggestions only."""
    local = stamp.astimezone(TZ); day = local.date().isoformat()
    if snapshot.get('complete'):
        return []
    issues = []
    def add(code, action=None):
        issues.append(dict(code=code, action=action))
    for key, code in [('configured', 'configuration_missing'), ('source_matches', 'source_integrity_failed'),
                      ('ntp', 'clock_unsynchronized'), ('database_ok', 'database_unreadable')]:
        if not snapshot.get(key):add('database_recovering' if key=='database_ok' and snapshot.get('database_recovering') else code)
    low_disk = snapshot.get('free_bytes', 0) < protocol.get('minimum_free_bytes', RESERVE)
    if low_disk: add('disk_low')
    if snapshot.get('fatal'): add('provider_fatal')
    unsafe = any(not snapshot.get(k) for k in ('configured', 'source_matches', 'database_ok'))
    if day not in protocol['dates'] or unsafe:
        # Reports can still account for missing days after the final session.
        end = datetime.fromisoformat(protocol['dates'][-1] + 'T13:50:00').replace(tzinfo=TZ)
        if local >= end and not snapshot.get('report_final') and not unsafe:
            add('report_missing', 'start_report')
        return issues
    start = datetime.fromisoformat(day + 'T' + protocol.get('collection_start', '09:30:00')).replace(tzinfo=TZ)
    end = datetime.fromisoformat(day + 'T' + protocol.get('collection_end', '10:15:00')).replace(tzinfo=TZ)
    decision = datetime.fromisoformat(day + 'T' + protocol.get('prediction_time', '10:00:00')).replace(tzinfo=TZ)
    if snapshot.get('gap_count',0)>0:add('market_data_gaps')
    if snapshot.get('wal_bytes',0)>=512*1024**2:add('wal_large')
    if snapshot.get('checkpoint_error'):add('checkpoint_failed')
    if snapshot.get('receipt_error'):add('receipt_pipeline_failed')
    if snapshot.get('archive_expected') and not snapshot.get('archive_current'):
        add('archive_failed','start_archive' if not low_disk and not snapshot.get('archive_active') and not snapshot.get('materialization_pending') else None)
    if start<=local<end+timedelta(seconds=150) and snapshot.get('shared_service_health') in ('unhealthy','starting','unavailable'):
        add('shared_service_unhealthy')
    if local.strftime('%H:%M:%S') >= '08:42:00' and not snapshot.get('preflight_current'):
        retry = local < start and not snapshot.get('preflight_active')
        add('preflight_failed', 'start_preflight' if retry else None)
    if start + timedelta(seconds=30) <= local < end and not low_disk and not snapshot.get('fatal'):
        backlog=snapshot.get('collector_backlog',0) or 0
        lag=local.timestamp()-snapshot.get('last_event_ns',0)/1e9
        if local>=start+timedelta(seconds=120) and ((backlog>=1024 and not snapshot.get('durable_receipts')) or (backlog>0 and lag>5)):
            add('collector_backlog')
        if not snapshot.get('collector_active'):
            add('collector_inactive', 'start_collect')
        elif age(snapshot.get('collector_at'), local) > 30:
            if snapshot.get('receipt_progressing'):add('collector_backlog')
            else:add('collector_stalled', 'restart_collect')
        elif local >= start + timedelta(seconds=120):
            if not snapshot.get('stream_ready'):
                add('feed_not_ready', 'restart_collect')
            elif (local.timestamp() - snapshot.get('last_event_ns', 0) / 1e9) > 120:
                add('market_data_stale', None if snapshot.get('receipt_progressing') else 'restart_collect')
    if local>=end+timedelta(seconds=150) and (snapshot.get('closeout_date')!=day or
            snapshot.get('closeout_state')!='day_collection_finished' or not snapshot.get('closeout_complete') or
            snapshot.get('closeout_remaining')!=0):add('collector_closeout_incomplete')
    if local >= decision - timedelta(seconds=45):
        missing = snapshot.get('predictions', 0) != snapshot.get('targets', 53) or not snapshot.get('release_ns')
        can_start = local < decision + timedelta(seconds=300) and not snapshot.get('forecast_active')
        if missing and (local >= decision + timedelta(seconds=5) or can_start):
            add('forecast_missing', 'start_forecast' if can_start else None)
        elif not missing and snapshot['release_ns'] / 1e9 > decision.timestamp() + protocol.get('max_emission_lateness_seconds', 1):
            add('forecast_late')
    if local.strftime('%H:%M:%S') >= '10:20:00' and not snapshot.get('backup_current') and not snapshot.get('backup_active'):
        pending=snapshot.get('materialization_pending')
        if not pending or local.strftime('%H:%M:%S')>='10:30:00':
            add('backup_missing', None if low_disk or pending else 'start_backup')
    if local.strftime('%H:%M:%S') >= '13:50:00' and not snapshot.get('report_current'):
        add('report_missing', None if snapshot.get('materialization_pending') or snapshot.get('backup_active') else 'start_report')
    return issues


def control(action):
    args = ACTIONS.get(action)
    if args is None:
        return False
    try:
        return subprocess.run(['/bin/systemctl', '--no-block', *args], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def send_notice(prefix, code, fields, *, opener=urlopen):
    """No exception, authenticated URL, account object or provider payload escapes."""
    if code not in MESSAGES:
        return False
    try:
        path = Path(prefix) / '.secrets/telegram.json'
        if path.is_symlink() or path.stat().st_mode & 0o077:
            return False
        config = json.loads(path.read_text())
        if type(config['chat_id']) is not int or config['chat_id'] <= 0:
            return False
        text = '[0050研究] ' + MESSAGES[code]
        if fields.get('action') in ACTIONS:
            text += '\n已嘗試有限復原：' + fields['action']
        if fields.get('previous') in MESSAGES:
            text += '\n異常代碼：' + fields['previous']
        text += '\n臺北時間：' + now().isoformat(timespec='seconds')
        body = json.dumps(dict(chat_id=config['chat_id'], text=text)).encode()
        request = Request('https://api.telegram.org/bot' + config['token'] + '/sendMessage',
                          data=body, headers={'Content-Type': 'application/json'}, method='POST')
        with opener(request, timeout=12) as response:
            result = json.loads(response.read())
        return result.get('ok') is True and result.get('result', {}).get('chat', {}).get('id') == config['chat_id']
    except Exception:
        return False


def unit_active(kind):
    try:
        r = subprocess.run(['/bin/systemctl', 'is-active', 'twquant-pilot-' + kind + '.service'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() in ('active', 'activating')
    except (OSError, subprocess.TimeoutExpired):
        return False


def snapshot(root, prefix, stamp):
    protocol = read_json(root / 'protocol.json', {})
    digest = hashlib.sha256()
    for path in sorted((prefix / 'app/src/twquant').glob('*.py')):
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    probe = read_json(root / 'preflight_status.json', {})
    collector = read_json(root / 'collector_status.json', {})
    report = read_json(root / 'evaluation.json', {})
    backup_state = read_json(root / 'backup_status.json', {})
    closeout=read_json(root/'collector_closeout.json',{})
    checkpoint=read_json(root/'checkpoint_status.json',{})
    receipt_error=read_json(root/'receipt_error.json',{});archive_state=read_json(root/'archive_status.json',{})
    profile=read_json(root/'capture_profile.json',{});receipt_progressing=False
    database_root=root
    session=root/'sessions'/stamp.date().isoformat()
    if (profile.get('session_mode') or collector.get('session_mode')) and (session/'pilot.sqlite3').is_file() and not session.is_symlink():database_root=session
    projected=bool(profile.get('projection_from') and stamp.date().isoformat()>=profile['projection_from'])
    projection=root/'projections'/stamp.date().isoformat();merged=read_json(root/'session_merge_status.json',{})
    history_ready=merged.get('date')==stamp.date().isoformat() and merged.get('state')=='complete'
    if projected and not history_ready and (projection/'pilot.sqlite3').is_file() and not projection.is_symlink():database_root=projection
    for segment in (root/'receipts').glob('????????.twqj'):
        if segment.stem.isdigit() and not segment.is_symlink():
            modified=segment.stat().st_mtime
            receipt_progressing=receipt_progressing or 0<=now().timestamp()-modified<=30
    shared_health=None
    local=stamp.astimezone(TZ);day=local.date().isoformat()
    if day in protocol.get('dates',[]):
        start=datetime.fromisoformat(day+'T'+protocol.get('collection_start','09:30:00')).replace(tzinfo=TZ)
        end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
        if start<=local<end+timedelta(seconds=150) or 'shared_service_unhealthy' in read_json(root/'watchdog_state.json',{}).get('incidents',{}):
            try:
                health_probe=subprocess.run(['docker','--config',str(prefix/'docker-observer'),'inspect','--format={{.State.Health.Status}}','telegram-reminder-bot-1'],capture_output=True,text=True,timeout=3)
                shared_health=health_probe.stdout.strip() if health_probe.returncode==0 and health_probe.stdout.strip() in ('healthy','unhealthy','starting') else 'unavailable'
            except (OSError,subprocess.TimeoutExpired):shared_health='unavailable'
    try:wal_bytes=(database_root/'pilot.sqlite3-wal').stat().st_size
    except FileNotFoundError:wal_bytes=0
    try:
        synced = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                                capture_output=True, text=True, timeout=10).stdout.strip() == 'yes'
    except (OSError, subprocess.TimeoutExpired):
        synced = False
    result = dict(configured=(prefix / '.env').is_file() and any((prefix / '.secrets/fubon').glob('*')),
                  source_matches=digest.hexdigest() == protocol.get('source_sha256'), ntp=synced,
                  free_bytes=shutil.disk_usage(root).free, fatal=(root / 'fatal.json').exists(),
                  collector_active=unit_active('collect'), collector_at=collector.get('at'),
                  stream_ready=collector.get('stream_ready', False), forecast_active=unit_active('forecast'),
                  collector_backlog=collector.get('backlog'),closeout_date=closeout.get('date'),
                  durable_receipts=collector.get('runtime') in ('cloud_journal_v3','cloud_projection_v4'),
                  closeout_state=closeout.get('state'),closeout_complete=closeout.get('complete'),
                  closeout_remaining=closeout.get('remaining_messages'),
                  wal_bytes=wal_bytes,
                  shared_service_health=shared_health,checkpoint_error=checkpoint.get('date')==day and checkpoint.get('state')=='error',
                  receipt_progressing=receipt_progressing,materialization_pending=receipts_pending(root),
                  receipt_complete=closeout.get('receipt_complete'),receipt_error=receipt_error.get('at','').startswith(day),
                  archive_expected=day>=profile.get('archive_from','9999') and local.strftime('%H:%M:%S')>='11:15:00',
                  archive_current=archive_state.get('at','').startswith(day) and archive_state.get('state')=='complete',archive_active=unit_active('archive'),
                  backup_active=unit_active('backup'), targets=len(set(protocol.get('members', []) + ['0050', '1216', '2002', '2412', 'IX0001'])),
                  preflight_active=unit_active('preflight'),
                  preflight_current=probe.get('date') == stamp.date().isoformat() and probe.get('state') == 'verified',
                  report_current=report_current(root,protocol,stamp),
                  report_final=report.get('final') is True,
                  backup_current=backup_state.get('date') == stamp.date().isoformat() and backup_state.get('state') == 'complete',
                  complete=(root / 'cloud_complete').exists() and report.get('final') is True)
    try:
        with sqlite3.connect((database_root / 'pilot.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            row = db.execute('SELECT received_ns FROM events ORDER BY id DESC LIMIT 1').fetchone()
            result['last_event_ns'] = row[0] if row else 0
            progress=read_json(root/'materializer_status.json',{})
            if projected and progress.get('projection_only') and progress.get('date')==stamp.date().isoformat():
                result['last_event_ns']=progress.get('last_received_ns',0)
            day = stamp.date().isoformat()
            result['predictions'] = db.execute('SELECT count(*) FROM predictions WHERE day=?', (day,)).fetchone()[0]
            row = db.execute('SELECT at_ns FROM releases WHERE day=?', (day,)).fetchone()
            result['release_ns'] = row[0] if row else None
            windows=[]
            if row:
                for horizon in (60,300):
                    stop=row[0]+horizon*10**9
                    windows.append(bool(db.execute('SELECT 1 FROM coverage WHERE start_ns<=? AND end_ns>=?',(row[0],stop)).fetchone()) and not db.execute('SELECT 1 FROM gaps WHERE start_ns<=? AND end_ns>=?',(stop,row[0])).fetchone())
            result['critical_windows_observed']=len(windows)==2 and all(windows)
            begin=datetime.fromisoformat(day+'T00:00:00').replace(tzinfo=TZ);low=int(begin.timestamp())*10**9
            result['gap_count']=db.execute('SELECT count(*) FROM gaps WHERE start_ns<? AND end_ns>=?',
                (low+86400*10**9,low)).fetchone()[0]
            result['database_ok'] = True
    except sqlite3.Error as error:
        result['database_ok'] = False
        result['database_recovering']=getattr(error,'sqlite_errorcode',0) in (sqlite3.SQLITE_BUSY,getattr(sqlite3,'SQLITE_BUSY_RECOVERY',261))
    return result


def check(root, *, prefix=Path('/opt/twquant-pilot'), clock=now, inspect=None, control=control, send=None):
    root = Path(root).resolve(); prefix = Path(prefix)
    stamp = clock().astimezone(TZ); protocol = read_json(root / 'protocol.json')
    if not protocol:
        raise ValueError('pilot_protocol_missing')
    send = send or (lambda code, fields: send_notice(prefix, code, fields))
    current = inspect() if inspect else snapshot(root, prefix, stamp)
    state = read_json(root / 'watchdog_state.json', {})
    state.setdefault('incidents', {}); state.setdefault('attempts', {})
    last = datetime.fromisoformat(protocol['dates'][-1] + 'T14:00:00').replace(tzinfo=TZ) + timedelta(days=1)
    if current.get('complete') or stamp >= last:
        code = 'complete' if current.get('complete') else 'monitor_window_ended'
        if not state.get('final_notified'):
            state['final_notified'] = send(code, {})
        if state.get('final_notified'):
            control('retire_monitor')
        state['checked_at'] = stamp.isoformat()
        private_json(root / 'watchdog_state.json', state)
        return dict(state='complete' if current.get('complete') else 'expired', notified=state.get('final_notified', False))
    issues = assess(protocol, stamp, current)
    active = {i['code'] for i in issues}
    for issue in issues:
        code = issue['code']; action = issue.get('action'); attempted = False
        if action:
            # All collector recovery paths share one daily budget.
            kind = 'collect' if action.endswith('collect') else action
            key = stamp.date().isoformat() + ':' + kind
            attempt = state['attempts'].get(key, dict(count=0, at=None))
            if attempt['count'] < 3 and age(attempt['at'], stamp) >= 120:
                attempted = control(action)
                state['attempts'][key] = dict(count=attempt['count'] + 1, at=stamp.isoformat())
            elif attempt['count'] >= 3:
                active.add('recovery_exhausted')
                previous = state['incidents'].get('recovery_exhausted', {})
                if not previous.get('notified'):
                    previous['notified'] = send('recovery_exhausted', {})
                state['incidents']['recovery_exhausted'] = previous
        previous = state['incidents'].get(code, {})
        if not previous.get('notified'):
            previous['notified'] = send(code, {'action': action if attempted else None})
        state['incidents'][code] = previous
    trusted=all(current.get(k) for k in ('configured','source_matches','database_ok','ntp'))
    for code in list(state['incidents']):
        if code not in active and trusted and (code!='shared_service_unhealthy' or current.get('shared_service_health')=='healthy'):
            if send('recovered', {'previous': code}):
                del state['incidents'][code]
    current['active_incidents']=sorted(active|set(state['incidents']))
    daily_acceptance(root,protocol,stamp,current,state,send)
    state['checked_at'] = stamp.isoformat()
    private_json(root / 'watchdog_state.json', state)
    pending=active|set(state['incidents'])
    return dict(state='healthy' if not pending else 'incidents', codes=sorted(pending), at=stamp.isoformat())


def backup(root, *, stamp=None, free_bytes=None):
    root = Path(root).resolve(); stamp = (stamp or now()).astimezone(TZ)
    protocol = read_json(root / 'protocol.json'); day = stamp.date().isoformat()
    def finish(state, **extra):
        result = dict(state=state, date=day, at=stamp.isoformat(), **extra)
        private_json(root / 'backup_status.json', result)
        return result
    if day not in protocol['dates'] or stamp.strftime('%H:%M:%S') < '10:16:00':
        return dict(state='outside_backup_window')
    if receipts_pending(root):return finish('receipts_pending')
    with (root / 'backup.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return dict(state='already_running')
        database = root / 'pilot.sqlite3'
        size = sum(p.stat().st_size for p in [database, root / 'pilot.sqlite3-wal'] if p.exists())
        available = free_bytes or (lambda: shutil.disk_usage(root).free)
        if available() < size + protocol.get('minimum_free_bytes', RESERVE):
            return finish('insufficient_space')
        directory = root / 'backups'
        if directory.is_symlink(): return finish('unsafe_backup_path')
        directory.mkdir(mode=0o700, exist_ok=True); directory.chmod(0o700)
        fd, name = tempfile.mkstemp(dir=directory, prefix='snapshot.', suffix='.sqlite3')
        os.close(fd); temporary = Path(name); deadline = time.monotonic() + 120
        try:
            def progress(*_):
                if time.monotonic() > deadline or available() < protocol.get('minimum_free_bytes', RESERVE):
                    raise RuntimeError('backup_budget_reached')
            with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3) as source:
                with sqlite3.connect(temporary) as destination:
                    source.backup(destination, pages=256, progress=progress, sleep=.01)
                    if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('backup_integrity_failed')
            with temporary.open('rb') as f: os.fsync(f.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, directory / 'latest.sqlite3')
            return finish('complete', size_bytes=(directory / 'latest.sqlite3').stat().st_size)
        except Exception:
            return finish('backup_failed')
        finally:
            temporary.unlink(missing_ok=True)


def preflight(root, prefix):
    stamp=now();protocol=read_json(root/'protocol.json')
    if stamp.date().isoformat() not in protocol['dates']:
        return dict(state='outside_declared_dates')
    verified=False;acknowledged=0
    try:
        result=subprocess.run([str(prefix/'venv/bin/python'),'-m','twquant.live_market',
            '--root',str(root),'--mode','probe'],cwd=prefix,capture_output=True,timeout=70,
            env={**os.environ,'PYTHONPATH':str(prefix/'app/src'),'TZ':'Asia/Taipei','PYTHONDONTWRITEBYTECODE':'1'})
        access=read_json(root/'access_check.json',{})
        acknowledged=access.get('acknowledged_subscriptions',0)
        verified=(result.returncode==0 and access.get('access_verified') is True
            and acknowledged==access.get('required_subscriptions')==105)
    except Exception:
        pass
    status=dict(state='verified' if verified else 'failed',at=stamp.isoformat(),
        date=stamp.date().isoformat(),acknowledged_subscriptions=acknowledged)
    private_json(root/'preflight_status.json',status)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--prefix', type=Path, default=Path('/opt/twquant-pilot'))
    parser.add_argument('--phase', choices=('check', 'backup', 'preflight', 'notify-ready'), default='check')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.phase == 'backup': result = backup(args.root)
        elif args.phase == 'preflight': result = preflight(args.root,args.prefix)
        elif args.phase == 'notify-ready': result = dict(notified=send_notice(args.prefix, 'ready', {}))
        else:
            with (args.root / 'watchdog.lock').open('a') as lock:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: return 0
                result = check(args.root, prefix=args.prefix)
        print(json.dumps(result))
        return 0 if result.get('state') not in ('backup_failed','failed') and result.get('notified', True) else 1
    except Exception:
        send_notice(args.prefix,'watchdog_failed',{})
        print(json.dumps(dict(state='error', code='watchdog_operation_failed')))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
