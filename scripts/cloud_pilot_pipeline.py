"""Single-flight, restartable maintenance after durable market acquisition.

Only derived daily workspaces are released, after a verified daily backup,
archive and canonical merge. Frozen forecasting and scoring remain unchanged.
"""
from __future__ import annotations
import argparse
from contextlib import closing,ExitStack
from datetime import datetime,timedelta
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import time
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')
STAGES=('cleanup','receipt_archive','compact','materialize','backup','daily_result','archive','merge','verify','report','checkpoint','release')
STAGE_SECONDS=dict(cleanup=120,receipt_archive=900,compact=900,materialize=1800,
    daily_result=300,backup=1800,archive=1200,merge=3600,verify=3600,report=1800,checkpoint=3600,release=120)


def notify_service(value):
    address=os.environ.get('NOTIFY_SOCKET')
    if not address:return False
    if address.startswith('@'):address='\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
            sock.settimeout(.1);sock.connect(address);sock.sendall(value.encode())
        return True
    except OSError:return False


def load_helper(name):
    local=Path(__file__).with_name(name+'.py')
    if not local.exists():local=Path(__file__).with_name('cloud_'+('pilot_'+name if name in ('watchdog','archive') else name)+'.py')
    spec=importlib.util.spec_from_file_location('maintenance_'+name,local);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def now():return datetime.now(TZ)


def protected(protocol,stamp):
    return load_helper('watchdog').maintenance_protected(protocol,stamp)


def write_report(root,day,stamp):
    from twquant.live_pilot import PilotStore
    protocol=json.loads((root/'protocol.json').read_text());store=PilotStore(root,protocol)
    last=datetime.fromisoformat(protocol['dates'][-1]+'T'+protocol.get('prediction_time','10:00:00')).replace(tzinfo=TZ)
    try:return store.write_report(as_of=now(),final=day==protocol['dates'][-1] and now()>=last+timedelta(hours=3,minutes=40))
    finally:store.close()

def write_daily_result(root, day, stamp):
    from twquant.live_pilot import PilotStore, private_json
    protocol = json.loads((root/'protocol.json').read_text())
    session = root/'sessions'/day
    if not (session/'pilot.sqlite3').is_file():
        raise ValueError('daily_history_missing')
    with closing(PilotStore(session, protocol)) as store:
        marker = store.db.execute("select value from metadata where key='cloud_history_complete'").fetchone()
        if marker != (day,):
            raise ValueError('daily_history_not_sealed')
        decision = store.decisions[day]
        store.decisions = {day: decision}
        report = store.evaluate(as_of=stamp)
        if not all(day in h['per_day'] for h in report['horizons'].values()):
            raise ValueError('daily_answers_pending')
        release = store.db.execute('select at_ns from releases where day=?', (day,)).fetchone()
        member_set = set(protocol['members'])
        directions = []
        for ticker, raw in store.db.execute('select ticker,row from predictions where day=? order by ticker', (day,)):
            if ticker in member_set:
                row = json.loads(raw)
                directions.append([ticker, row['direction'], row['fallback']])
        published = release and len(directions) == 50
        result = dict(date=day, as_of=stamp.isoformat(), protocol_sha256=store.digest,
            publication_on_time=bool(published and 0<=release[0]-decision<=protocol['max_emission_lateness_seconds']*10**9),
            directions=directions if published else [],
            horizons={key: dict(value['per_day'][day], timeouts=value['timeout_predictions'])
                for key, value in report['horizons'].items()})
    private_json(root/('daily_evaluation_'+day+'.json'), result)
    return dict(state='complete')


def verify_merge(root,day):
    daily=root/'sessions'/day/'pilot.sqlite3'
    with closing(sqlite3.connect((root/'pilot.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.execute('pragma cache_size=-262144');db.execute('attach database ? as daily',(daily.as_uri()+'?mode=ro',))
        fields=['digest','payload','event_ns','received_ns','ticker','channel','kind','serial']
        same=' and '.join('c.'+key+' is s.'+key for key in fields)
        # Stable IDs permit sequential table reads. Only a digest already kept
        # under an earlier ID needs the random digest-index fallback.
        alias=' and '.join('c.'+key+' is s.'+key for key in fields if key!='received_ns')
        mismatch=db.execute('select 1 from daily.events s left join main.events c on c.id=s.id where (c.id is not null and not ('+same+')) or (c.id is null and not exists(select 1 from main.events c where '+alias+')) limit 1').fetchone()
        if mismatch:raise ValueError('pipeline_merge_mismatch')
        for table,columns in [('predictions','day,ticker,row'),('releases','day,at_ns')]:
            a=db.execute('select '+columns+' from daily.'+table+' where day=? order by 1,2',(day,)).fetchall()
            b=db.execute('select '+columns+' from main.'+table+' where day=? order by 1,2',(day,)).fetchall()
            if a!=b:raise ValueError('pipeline_publication_changed')
    return dict(state='complete',publication_preserved=True)


def checkpoint_history(db):
    busy,frames,written=db.execute('pragma wal_checkpoint(TRUNCATE)').fetchone()
    if busy or frames!=written:raise RuntimeError('pipeline_checkpoint_busy')
    return dict(state='complete')


def mark_complete(root,protocol,day):
    if day==protocol['dates'][-1] and load_helper('receipt_journal').read_json(root/'evaluation.json',{}).get('final'):
        fd=os.open(root/'cloud_complete',os.O_WRONLY|os.O_CREAT,0o600);os.fsync(fd);os.close(fd)


def run(root,source,*,clock=None,report=write_report):
    root=Path(root).resolve();source=Path(source);clock=clock or now;stamp=clock()
    receipt=load_helper('receipt_journal');ops=load_helper('watchdog');archives=load_helper('archive')
    protocol=receipt.read_json(root/'protocol.json');receipt.verify_source(source,protocol)
    done=receipt.read_json(root/'receipt_capture_done.json',{});day=done.get('date')
    if day not in protocol['dates']:return dict(state='capture_not_closed')
    if not ops.pipeline_enabled(root,day):return dict(state='legacy_profile')
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
    if protected(protocol,stamp) or stamp<end+timedelta(seconds=150):return dict(state='protected_window')
    watermark=receipt.read_json(root/'receipt_watermark.json',{})
    seals=[(p.name,receipt.read_json(p,{})) for p in sorted((root/'receipts').glob('*.seal.json'))]
    selected=[(name,v) for name,v in seals if v.get('day')==day]
    expected=done.get('events_total',0)
    if watermark.get('day')!=day or watermark.get('events_total')!=expected or sum(v.get('events',0) for _,v in selected)!=expected:return dict(state='capture_incomplete')
    identity=hashlib.sha256(json.dumps([receipt.protocol_hash(protocol),day,expected,[(n,v.get('sha256'),v.get('offset')) for n,v in selected]],sort_keys=True).encode()).hexdigest()
    with (root/'maintenance.lock').open('a') as lock,ExitStack() as connections:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='busy')
        path=root/'pipeline_state.json';state=receipt.read_json(path,{})
        if state.get('identity')!=identity:state=dict(identity=identity,date=day,completed=[],attempts={})
        stages=tuple(p for p in STAGES if p!='daily_result' or 'release' not in state['completed'])
        key=ops.report_key(protocol,stamp)
        if 'report' in state['completed'] and (state.get('report_key')!=key or not ops.report_current(root,protocol,stamp)):state['completed'].remove('report')
        if state.get('state')=='complete' and all(p in state['completed'] for p in stages):
            mark_complete(root,protocol,day);return state
        if state.get('state')=='failed' and state.get('failed_at') and (stamp-datetime.fromisoformat(state['failed_at'])).total_seconds()<120:return dict(state='retry_wait',phase=state.get('phase'))
        def save():
            state['at']=clock().isoformat();receipt.private_json(path,state)
        keeper=None
        for phase in stages:
            if phase in state['completed']:continue
            if protected(protocol,clock()):return dict(state='protected_window')
            if phase!='cleanup' and shutil.disk_usage(root).free<protocol.get('minimum_free_bytes',4*1024**3):
                state.update(state='deferred',phase=phase,retryable=True);save();return state
            if state['attempts'].get(phase,0)>=3:
                state.update(state='failed',phase=phase,retryable=False);save();return state
            state['attempts'][phase]=state['attempts'].get(phase,0)+1
            state.update(state='running',phase=phase);save();started=time.monotonic()
            notify_service('EXTEND_TIMEOUT_USEC='+str(STAGE_SECONDS[phase]*1000000)+'\nSTATUS=maintenance_'+phase)
            try:
                if phase in ('merge','verify','report','checkpoint') and keeper is None and (root/'pilot.sqlite3').is_file():
                    # Keep committed WAL available to readers without an implicit
                    # last-connection checkpoint delaying the report.
                    keeper=connections.enter_context(closing(sqlite3.connect(root/'pilot.sqlite3',timeout=30)))
                    keeper.execute('pragma wal_autocheckpoint=0');keeper.execute('pragma schema_version').fetchone()
                if phase=='cleanup':result=ops.clean_incomplete_backups(root)
                elif phase=='receipt_archive':result=archives.archive(root,receipts_only=True)
                elif phase=='compact':result=archives.compact_archived_receipts(root)
                elif phase=='materialize':
                    with (root/'receipt_consumer.lock').open('a') as consumer_lock:
                        fcntl.flock(consumer_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        consumer=receipt.Materializer(root,source,session_day=day,full_history=True)
                        try:
                            while consumer.step(max_records=8192,max_bytes=8*1024**2)['records']:
                                if time.monotonic()-started>900:raise TimeoutError('pipeline_materialization_budget')
                            count=receipt.materializer_state(consumer)['events_total']
                            if count!=expected:raise ValueError('pipeline_receipt_count_changed')
                            consumer.finish_history()
                            if receipt.projection_enabled(root,day):receipt.copy_publication(root,source,day,consumer.store)
                        finally:consumer.close()
                    result=dict(state='complete',date=day,events_total=count,at=clock().isoformat())
                    receipt.private_json(root/'day_history_status.json',result)
                    receipt.private_json(root/'materializer_status.json',dict(result,projection_only=False))
                elif phase=='daily_result':result=write_daily_result(root,day,clock())
                elif phase=='backup':result=ops.backup(root,stamp=clock(),session_day=day)
                elif phase=='merge':
                    result=receipt.merge_session(root,source,day)
                elif phase=='verify':result=verify_merge(root,day)
                elif phase=='archive':result=archives.archive(root)
                elif phase=='report':
                    ops.begin_report(root,protocol,clock());result=report(root,day,clock());state['report_key']=ops.report_key(protocol,clock())
                    if not ops.record_report(root,protocol,clock())['cached']:raise ValueError('pipeline_report_not_current')
                    result=dict(state='complete')
                elif phase=='checkpoint':
                    if keeper is None:
                        keeper=connections.enter_context(closing(sqlite3.connect(root/'pilot.sqlite3',timeout=30)))
                    result=checkpoint_history(keeper)
                elif phase=='release':
                    backup=receipt.read_json(root/'backup_status.json',{});merged=receipt.read_json(root/'session_merge_status.json',{})
                    if backup.get('date')!=day or backup.get('state')!='complete' or backup.get('scope')!='daily_session' or merged.get('state')!='complete' or merged.get('date')!=day:raise ValueError('pipeline_release_not_verified')
                    item=receipt.read_json(root/'archives/index.json',{}).get('backups/latest.sqlite3',{})
                    name=item.get('directory','')
                    if not name.startswith('latest.sqlite3-') or '/' in name:raise ValueError('pipeline_backup_archive_missing')
                    manifest=archives.check_parts(root/'archives'/name)
                    if manifest['source_size_bytes']!=backup['size_bytes']:raise ValueError('pipeline_backup_archive_changed')
                    directory=receipt.session_path(root,day);released=0
                    for name in ['pilot.sqlite3','pilot.sqlite3-wal','pilot.sqlite3-shm']:
                        file=directory/name
                        if file.is_symlink():raise ValueError('pipeline_workspace_untrusted')
                        if file.exists():released+=file.stat().st_size;file.unlink()
                    fd=os.open(directory,os.O_RDONLY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                    result=dict(state='complete',released_bytes=released)
                if result.get('state')!='complete':raise RuntimeError('pipeline_stage_deferred')
                state['completed'].append(phase);state.setdefault('durations',{})[phase]=round(time.monotonic()-started,3)
                state.pop('error',None);state.pop('failed_at',None);save()
            except Exception as error:
                state.update(state='failed',phase=phase,retryable=state['attempts'][phase]<3,failed_at=clock().isoformat(),error=receipt.error_details(error));save();return state
        state.update(state='complete',phase='complete',retryable=False);save()
        mark_complete(root,protocol,day)
        return state


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--phase',choices=['replay','backup','archive','report','condition','quiet-condition'],default='replay');args=parser.parse_args();os.umask(0o077)
    receipt=load_helper('receipt_journal');ops=load_helper('watchdog');protocol=receipt.read_json(args.root/'protocol.json');stamp=now()
    if args.phase=='condition':return int(protected(protocol,stamp))
    if args.phase=='quiet-condition':return int(not protected(protocol,stamp))
    notify_service('READY=1')
    if not ops.pipeline_enabled(args.root,stamp.date().isoformat()):
        if args.phase=='replay':result=receipt.consume(args.root,args.source,replay=True,session_mode=True)
        elif args.phase=='backup':result=ops.backup(args.root)
        elif args.phase=='archive':result=load_helper('archive').archive(args.root)
        elif ops.report_wanted(args.root,protocol,stamp):
            ops.begin_report(args.root,protocol,now());result=write_report(args.root,stamp.date().isoformat(),stamp);ops.record_report(args.root,protocol,now())
            if result.get('final'):mark_complete(args.root,protocol,stamp.date().isoformat())
        else:result=dict(state='already_reported')
    else:result=run(args.root,args.source)
    print(json.dumps({k:result[k] for k in ['state','phase','retryable'] if k in result}));return 0


if __name__=='__main__':raise SystemExit(main())
