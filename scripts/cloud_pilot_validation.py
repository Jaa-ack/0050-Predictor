"""Bounded, isolated real-stream rehearsal. Never writes formal pilot scores."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime,timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')
PREFIX=Path('/opt/twquant-pilot')
KINDS=('collect','forecast','report','preflight','backup','watchdog','quiet','checkpoint','replay','archive','fault','report-stress','final','safety')


def make_plan(start):
    start=start.astimezone(TZ).replace(second=0,microsecond=0)
    if start.hour<9 or start+timedelta(minutes=60)>=start.replace(hour=13,minute=30):
        raise ValueError('Validation must finish during the market session')
    session='twquant-validation-'+start.strftime('%Y%m%d-%H%M')
    result=dict(session=session,root=str(PREFIX/'runtime'/session),control=str(PREFIX/'validation-control'/session),
                day=start.date().isoformat(),prefix=str(PREFIX),diagnostic_only=True)
    for key,minutes in [('preflight',-4),('start',0),('fault',12),('decision',30),('quiet',-2),
                        ('end',45),('backup',50),('final',55),('safety',60)]:
        result[key]=(start+timedelta(minutes=minutes)).isoformat()
    return result


def validate_plan(plan):
    if not re.fullmatch(r'twquant-validation-20\d{6}-\d{4}',plan['session']):raise ValueError('Invalid isolated session')
    if plan['prefix']!=str(PREFIX) or plan['root']!=str(PREFIX/'runtime'/plan['session']):raise ValueError('Untrusted validation path')
    if plan['control']!=str(PREFIX/'validation-control'/plan['session']):raise ValueError('Untrusted control path')
    if plan.get('helper_prefix',str(PREFIX)) not in (str(PREFIX),str(PREFIX/'projection-candidate')):raise ValueError('Untrusted candidate helper path')
    if plan.get('diagnostic_only') is not True:raise ValueError('Diagnostics must remain separate')


def diagnostic_protocol(original,plan):
    validate_plan(plan);result=copy.deepcopy(original)
    result.update(dates=[plan['day']],prediction_time=datetime.fromisoformat(plan['decision']).strftime('%H:%M:%S'),
        collection_start=datetime.fromisoformat(plan['start']).strftime('%H:%M:%S'),
        collection_end=datetime.fromisoformat(plan['end']).strftime('%H:%M:%S'),diagnostic_only=True,
        parent_protocol_sha256=hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest())
    return result


def health_clock(snapshot,offset):
    result=copy.deepcopy(snapshot)
    if result.get('collector_at'):result['collector_at']=(datetime.fromisoformat(result['collector_at'])-offset).isoformat()
    for key in ['last_event_ns','release_ns']:
        if result.get(key):result[key]-=int(offset.total_seconds())*10**9
    return result


def quiet_evidence(quiet,report,*,now,monotonic_now):
    """Use the original unit times; receipt retrieval never backdates evidence."""
    started=int(report.get('ExecMainStartTimestampMonotonic','0'))/1e6
    stopped=int(report.get('InactiveEnterTimestampMonotonic','0'))/1e6
    command=int(quiet.get('ExecMainStartTimestampMonotonic','0'))/1e6
    inactive=report.get('ActiveState') not in ('active','activating','deactivating')
    return dict(at=now.isoformat(),evidence_kind='original_systemd_monotonic_times',
        report_was_running=bool(0<started<=command<=stopped and quiet.get('ExecMainStatus')=='0'),
        report_stopped=inactive,
        stopped_at=(now-timedelta(seconds=monotonic_now-stopped)).isoformat() if stopped else None,
        original_receipt_unit_result=quiet.get('Result'))


def action_args(plan,action):
    validate_plan(plan)
    actions={'start_collect':['start','collect.service'],'restart_collect':['restart','collect.service'],
        'start_forecast':['start','forecast.service'],'start_report':['start','report.service'],
        'start_backup':['start','backup.service'],'start_preflight':['start','preflight.service'],
        'retire_monitor':['disable','--now','watchdog.timer']}
    if action not in actions:raise ValueError('Unsupported diagnostic recovery')
    result=actions[action].copy();result[-1]=plan['session']+'-'+result[-1];return result


def load_module(path,name):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def render_units(plan):
    validate_plan(plan);prepare=load_module(Path(__file__).with_name('prepare_cloud_pilot.py'),'validation_prepare')
    original=prepare.units(str(PREFIX),'twquant-pilot',[plan['day']],memory_max='640M')
    driver=plan['control']+'/driver.py';meta=plan['control']+'/plan.json';result={}
    def command(phase):return '/usr/bin/python3 '+driver+' --plan '+meta+' --phase '+phase
    for name,text in original.items():
        if name.endswith('.slice'):continue
        name=name.replace('twquant-pilot-',plan['session']+'-')
        text=text.replace('twquant-pilot-',plan['session']+'-').replace(str(PREFIX/'runtime'),plan['root'])
        if plan.get('helper_prefix'):
            for helper in ['resource_guard.py','receipt_journal.py','archive.py','checkpoint.py','pipeline.py','watchdog.py','window.py']:
                text=text.replace(str(PREFIX/helper),str(Path(plan['helper_prefix'])/helper))
        kind=name.removeprefix(plan['session']+'-').split('.')[0]
        if name.endswith('.timer'):
            if kind=='watchdog':
                first=datetime.fromisoformat(plan['preflight'])+timedelta(minutes=1)
                timing='OnCalendar='+first.strftime('%Y-%m-%d %H:%M:%S')+' Asia/Taipei\nOnUnitInactiveSec=30s'
            else:
                key={'collect':'start','checkpoint':'start','forecast':'decision','report':'start','preflight':'preflight','backup':'backup','quiet':'quiet','replay':'end','archive':'end'}[kind]
                value=datetime.fromisoformat(plan[key])
                if kind in ('replay','archive'):value+=timedelta(minutes=3 if kind=='replay' else 45)
                if kind=='forecast':value-=timedelta(minutes=3 if plan.get('helper_prefix') else 1)
                if kind=='report':value+=timedelta(minutes=5)
                timing='OnCalendar='+value.strftime('%Y-%m-%d %H:%M:%S')+' Asia/Taipei'
                if kind in ('collect','checkpoint','report'):timing+='\nOnUnitInactiveSec='+('300s' if kind=='report' else '60s')
            text='[Unit]\nDescription=Isolated validation '+kind+'\n[Timer]\n'+timing+'\nAccuracySec=1us\nRandomizedDelaySec=0\nPersistent=true\nUnit='+plan['session']+'-'+kind+'.service\n[Install]\nWantedBy=timers.target\n'
        elif kind in ('watchdog','preflight','backup'):
            phase='watchdog' if kind=='watchdog' else kind
            text=re.sub(r'^ExecStart=.*$', 'ExecStart='+command(phase),text,flags=re.M)
        elif kind=='quiet':
            text=re.sub(r'^ExecCondition=.*$','ExecCondition='+command('quiet-condition'),text,flags=re.M)
            text+='\nReadWritePaths='+plan['root']+'\nExecStartPost='+command('quiet-receipt')+'\n'
        elif kind=='report':
            text=re.sub(r'^ExecCondition=.*$','ExecCondition='+command('report-condition'),text,flags=re.M)
            text+='\nExecStartPost='+command('report-hold')+'\n'
        elif kind=='forecast':
            text+='\nExecStopPost='+command('forecast-receipt')+'\n'
        elif kind=='finalize':
            text+='ExecStart='+command('restore')+'\n'
        result[name]=text
    for kind,phase,key,user in [('fault','inject','fault','root'),('final','final','final','twquant-pilot'),('safety','cleanup','safety','root')]:
        result[plan['session']+'-'+kind+'.service']='[Unit]\nDescription=Bounded validation '+kind+'\n'+('OnSuccess='+plan['session']+'-finalize.service\n' if kind=='final' else '')+'[Service]\nType=oneshot\nUser='+user+'\nWorkingDirectory='+str(PREFIX)+'\nUMask=0077\nEnvironment=TZ=Asia/Taipei\nEnvironment=PYTHONPATH='+str(PREFIX/'app/src')+'\nExecStart='+('/opt/twquant-pilot/venv/bin/python '+driver+' --plan '+meta+' --phase final' if kind=='final' else command(phase))+'\nTimeoutStartSec=300\nNoNewPrivileges=true\nPrivateTmp=true\nProtectSystem=strict\nProtectHome=true\nReadWritePaths='+plan['root']+' '+str(PREFIX/'log')+'\n'
        if kind=='final':
            targets=plan['session']+'-report.service '+plan['session']+'-report.timer'
            result[plan['session']+'-'+kind+'.service']=result[plan['session']+'-'+kind+'.service'].replace('[Unit]\n','[Unit]\nConflicts='+targets+'\nAfter='+targets+'\n',1)
            result[plan['session']+'-'+kind+'.service']=result[plan['session']+'-'+kind+'.service'].replace('[Service]\n','[Service]\nSlice=twquant-pilot.slice\nNice=10\n',1)
        stamp=datetime.fromisoformat(plan[key]).strftime('%Y-%m-%d %H:%M:%S')
        result[plan['session']+'-'+kind+'.timer']='[Unit]\nDescription=Single validation '+kind+'\n[Timer]\nOnCalendar='+stamp+' Asia/Taipei\nAccuracySec=1us\nRandomizedDelaySec=0\nPersistent=true\nUnit='+plan['session']+'-'+kind+'.service\n[Install]\nWantedBy=timers.target\n'
    held_at=(datetime.fromisoformat(plan['quiet'])-timedelta(seconds=45)).strftime('%Y-%m-%d %H:%M:%S')
    result[plan['session']+'-report-stress.service']='[Unit]\nDescription=Start only the diagnostic report for quiet-window testing\n[Service]\nType=oneshot\nExecStart=/bin/systemctl --no-block start '+plan['session']+'-report.service\n'
    result[plan['session']+'-report-stress.timer']='[Unit]\nDescription=One bounded diagnostic quiet-window challenge\n[Timer]\nOnCalendar='+held_at+' Asia/Taipei\nAccuracySec=1us\nRandomizedDelaySec=0\nUnit='+plan['session']+'-report-stress.service\n[Install]\nWantedBy=timers.target\n'
    return result


def execute(args,timeout=30):
    return subprocess.run(args,capture_output=True,text=True,timeout=timeout)


def stamp():return datetime.now(TZ)


def receipt(root,name,value):
    ops=load_module(PREFIX/'watchdog.py','validation_receipt');ops.private_json(root/name,value)
    if os.geteuid()==0:os.chown(root/name,root.stat().st_uid,root.stat().st_gid)


def audit(root,name,value):
    flags=os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)
    fd=os.open(root/name,flags,0o600)
    with os.fdopen(fd,'a') as out:
        if os.geteuid()==0:os.fchown(out.fileno(),root.stat().st_uid,root.stat().st_gid)
        out.write(json.dumps(value)+'\n')


def read_lines(path):
    if not path.exists():return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def projection_flow_checks(plan,samples):
    start=datetime.fromisoformat(plan['start']);end=datetime.fromisoformat(plan['end']);fault=datetime.fromisoformat(plan['fault'])
    observed=[s for s in samples if start<=datetime.fromisoformat(s['at'])<=end+timedelta(seconds=150)]
    steady=[s for s in observed if start+timedelta(seconds=120)<=datetime.fromisoformat(s['at'])<end
            and not fault-timedelta(seconds=2)<=datetime.fromisoformat(s['at'])<=fault+timedelta(seconds=120)]
    lags=[max(0,datetime.fromisoformat(s['at']).timestamp()-s.get('last_event_ns',0)/1e9) for s in steady]
    health=[s for s in observed if datetime.fromisoformat(s['at'])>=start+timedelta(seconds=120)]
    polls=[(datetime.fromisoformat(b['at'])-datetime.fromisoformat(a['at'])).total_seconds() for a,b in zip(observed,observed[1:])]
    max_lag=max(lags,default=float('inf'));max_wal=max((s.get('wal_bytes',0) for s in observed),default=0)
    checks=dict(steady_receipt_flow=bool(steady) and all(s.get('collector_active') and s.get('ready') for s in steady) and max_lag<=5,
        shared_service_stable=bool(health) and all(s.get('shared_service_health')=='healthy' for s in health),
        bounded_projection_wal=bool(observed) and max_wal<512*1024**2,
        sampled_monitoring_coverage=len(observed)>=30 and datetime.fromisoformat(observed[0]['at'])<=start+timedelta(seconds=120)
            and datetime.fromisoformat(observed[-1]['at'])>=end and max(polls,default=float('inf'))<=90)
    return dict(checks=checks,max_lag_seconds=max_lag if lags else None,max_wal_bytes=max_wal,samples=len(observed),declared_fault_window_retained=True)


def backup_integrity(path):
    if path.is_symlink():return False
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5) as db:
            return db.execute('pragma quick_check').fetchone()[0]=='ok'
    except (sqlite3.Error,OSError):return False


def forecast_receipt(root,day):
    daily=root/'sessions'/day/'pilot.sqlite3'
    profile=json.loads((root/'capture_profile.json').read_text()) if (root/'capture_profile.json').exists() else {}
    projection=root/'projections'/day/'pilot.sqlite3'
    merged=json.loads((root/'session_merge_status.json').read_text()) if (root/'session_merge_status.json').exists() else {}
    history_ready=merged.get('date')==day and merged.get('state')=='complete'
    database=root/'pilot.sqlite3' if history_ready else projection if profile.get('projection_from') and day>=profile['projection_from'] and projection.exists() else daily if daily.exists() else root/'pilot.sqlite3'
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=5) as db:
        rows=[x[0] for x in db.execute('select row from predictions where day=? order by ticker',(day,))]
        released=db.execute('select at_ns from releases where day=?',(day,)).fetchone()
    result=dict(at=stamp().isoformat(),count=len(rows),rows_sha256=hashlib.sha256('\n'.join(rows).encode()).hexdigest(),
        release_ns=released[0] if released else None)
    receipt(root,'forecast_observed.json',result);return result


def run_phase(plan,phase):
    validate_plan(plan);root=Path(plan['root']);helpers=Path(plan.get('helper_prefix',str(PREFIX)));ops=load_module(helpers/'watchdog.py','validation_ops')
    session=plan['session'];decision=datetime.fromisoformat(plan['decision'])
    shift=decision-decision.replace(hour=10,minute=0,second=0,microsecond=0)
    ops.MESSAGES={k:'【獨立流程演練 '+decision.strftime('%H:%M')+'，不計正式成績】'+v.replace('10:00',decision.strftime('%H:%M')) for k,v in ops.MESSAGES.items()}
    ops.MESSAGES['validation_started']='獨立完整流程測試已排程：'+plan['start'][11:16]+'～'+plan['end'][11:16]+'採集，'+decision.strftime('%H:%M')+'測試發報；正式十日紀錄不改寫。'
    ops.MESSAGES['complete']='獨立完整流程演練已完成，結果另存測試報告；不是正式十日試驗完成，也不改寫今日晚發。'
    ops.MESSAGES['validation_failed']='【獨立流程演練，不計正式成績】驗收未通過，已保留晚發、缺口及失敗檢查；不代表系統完全正常。安全收尾仍會恢復正式排程。'
    def send(code,fields):
        ok=ops.send_notice(PREFIX,code,fields)
        audit(root,'notification_attempts.jsonl',dict(at=stamp().isoformat(),code=code,ok=ok))
        return ok
    if phase=='notice':
        if plan.get('notify_start') is False:return dict(state='not_requested',notified=False)
        return dict(notified=send('validation_started',{}))
    if phase in ('quiet-condition','report-condition'):
        guard=load_module(helpers/'resource_guard.py','validation_guard')
        ready=guard.report_ready({'dates':[plan['day']]},stamp()-shift)
        return dict(condition_passed=ready if phase=='report-condition' else not ready)
    if phase=='preflight':return ops.preflight(root,PREFIX)
    if phase=='backup':return ops.backup(root)
    if phase=='forecast-receipt':return forecast_receipt(root,plan['day'])
    if phase=='report-hold':
        quiet=datetime.fromisoformat(plan['quiet'])
        if quiet-timedelta(seconds=60)<=stamp()<quiet:
            receipt(root,'report_hold.json',dict(at=stamp().isoformat(),purpose='planned_quiet_window_challenge'))
            time.sleep(90)
        return dict(state='report_poststep_finished')
    if phase=='quiet-receipt':
        def properties(kind):
            output=execute(['systemctl','show',session+'-'+kind+'.service',
                '--property=ExecMainStartTimestampMonotonic,InactiveEnterTimestampMonotonic,ExecMainStatus,ActiveState,Result']).stdout
            return dict(line.split('=',1) for line in output.splitlines() if '=' in line)
        quiet=properties('quiet');report=properties('report')
        value=quiet_evidence(quiet,report,now=stamp(),monotonic_now=time.monotonic())
        receipt(root,'quiet_observed.json',value);return value
    if phase in ('restore','cleanup'):
        kinds=['collect','forecast','report','preflight','backup','quiet','checkpoint','replay','archive','fault','report-stress','final']
        if phase=='cleanup':kinds+=['watchdog','safety']
        execute(['systemctl','disable','--now',*[session+'-'+k+'.timer' for k in kinds]])
        execute(['systemctl','stop',*[session+'-'+k+'.service' for k in ['collect','forecast','report','checkpoint','replay','archive']]])
        if plan.get('formal_report_was_active'):execute(['systemctl','start','twquant-pilot-report.timer'])
        result=dict(state='restored',at=stamp().isoformat(),formal_report_timer_restored=bool(plan.get('formal_report_was_active')))
        receipt(root,'restoration.json',result);return result
    if phase=='inject':
        with sqlite3.connect((root/'pilot.sqlite3').as_uri()+'?mode=ro',uri=True) as db:before=db.execute('select max(id) from events').fetchone()[0]
        result=execute(['systemctl','stop',session+'-collect.service'],timeout=45)
        record=dict(at=stamp().isoformat(),kind='planned_collector_stop',persisted_id_before=before,command_ok=result.returncode==0)
        receipt(root,'fault_injection.json',record);return record
    if phase=='watchdog':
        if (root/'cloud_complete').exists() and plan.get('notify_start') is False:
            execute(['systemctl',*action_args(plan,'retire_monitor')])
            return dict(state='complete',notification='not_requested')
        terminal=root/'validation_result.json'
        if terminal.exists() and json.loads(terminal.read_text()).get('passed') is False:
            notice=root/'validation_failure_notification.json'
            notified=notice.exists() and json.loads(notice.read_text()).get('notified') is True
            if not notified:notified=send('validation_failed',{})
            receipt(root,notice.name,dict(at=stamp().isoformat(),notified=notified))
            if notified:execute(['systemctl',*action_args(plan,'retire_monitor')])
            return dict(state='validation_failed',notified=notified)
        active=lambda kind:execute(['systemctl','is-active',session+'-'+kind+'.service']).stdout.strip() in ('active','activating')
        ops.unit_active=active;original_assess=ops.assess
        def assess(protocol,when,snapshot):
            p=dict(protocol,prediction_time='10:00:00',collection_start='09:30:00',collection_end='10:15:00')
            return original_assess(p,when-shift,health_clock(snapshot,shift))
        ops.assess=assess
        def control(action):
            args=action_args(plan,action);result=execute(['systemctl','--no-block',*args])
            audit(root,'recovery_actions.jsonl',dict(at=stamp().isoformat(),action=action,ok=result.returncode==0))
            return result.returncode==0
        sample=ops.snapshot(root,PREFIX,stamp())
        audit(root,'health_samples.jsonl',dict(at=stamp().isoformat(),collector_active=sample['collector_active'],collector_at=sample.get('collector_at'),
            ready=sample['stream_ready'],last_event_ns=sample.get('last_event_ns',0),predictions=sample.get('predictions',0),release_ns=sample.get('release_ns'),
            shared_service_health=sample.get('shared_service_health'),wal_bytes=sample.get('wal_bytes',0),backlog=sample.get('collector_backlog')))
        result=ops.check(root,prefix=PREFIX,inspect=lambda:sample,control=control,send=send)
        if result.get('state')=='complete':receipt(root,'completion_notification.json',dict(notified=result.get('notified'),at=stamp().isoformat()))
        return result
    if phase=='final':
        from twquant.live_pilot import PilotStore,ns
        protocol=json.loads((root/'protocol.json').read_text());store=PilotStore(root,protocol)
        try:
            report=store.write_report(as_of=stamp(),final=True)
            before=json.loads((root/'forecast_observed.json').read_text());after=forecast_receipt(root,plan['day'])
            release=after['release_ns'];deadline=release+300*10**9 if release else 0
            coverage=bool(store.db.execute('select 1 from coverage where start_ns<=? and end_ns>=?',(release or 0,deadline)).fetchone())
            gap=bool(store.db.execute('select 1 from gaps where start_ns<=? and end_ns>=?',(deadline,release or 0)).fetchone())
            backup=json.loads((root/'backup_status.json').read_text()) if (root/'backup_status.json').exists() else {}
            checks=dict(full_grid=after['count']==53,publication_on_time=bool(release and 0<=release-ns(decision)<=10**9),
                immutable_predictions=before['rows_sha256']==after['rows_sha256'] and before['release_ns']==release,
                both_horizons_complete=all(report['horizons'][str(h)]['expected_predictions']==50 and report['horizons'][str(h)]['missing_predictions']==0 for h in [60,300]),
                scoring_window_observed=coverage and not gap,backup_complete=backup.get('state')=='complete',
                database_integrity=store.db.execute('pragma quick_check').fetchone()[0]=='ok')
            notifications=read_lines(root/'notification_attempts.jsonl')
            checks['start_notification']=plan.get('notify_start') is False or any(x['code']=='validation_started' and x['ok'] for x in notifications)
            checks['fault_and_recovery_notifications']=any(x['code'] in ('collector_inactive','collector_stalled') and x['ok'] for x in notifications) and any(x['code']=='recovered' and x['ok'] for x in notifications)
            fault=json.loads((root/'fault_injection.json').read_text());fault_at=datetime.fromisoformat(fault['at'])
            samples=read_lines(root/'health_samples.jsonl')
            flow=projection_flow_checks(plan,samples) if plan.get('helper_prefix') else None
            if flow:
                checks.update(flow['checks']);closeout=ops.read_json(root/'collector_closeout.json',{});done=ops.read_json(root/'receipt_capture_done.json',{});merged=ops.read_json(root/'session_merge_status.json',{})
                checks['durable_capture_complete']=closeout.get('receipt_complete') is True and closeout.get('complete') is True and closeout.get('remaining_messages')==0 and closeout.get('state')=='day_collection_finished'
                checks['full_history_materialized']=merged.get('date')==plan['day'] and merged.get('state')=='complete' and merged.get('events_total')==done.get('events_total')
            recovered=[x for x in samples if x['collector_active'] and x['ready'] and x.get('collector_at') and datetime.fromisoformat(x['collector_at'])>=fault_at]
            checks['bounded_fault_recovery']=bool(recovered and (datetime.fromisoformat(recovered[0]['at'])-fault_at).total_seconds()<=120)
            quiet=json.loads((root/'quiet_observed.json').read_text())
            checks['active_report_stopped_at_quiet']=quiet['report_stopped'] and quiet['report_was_running'] and abs((datetime.fromisoformat(quiet['stopped_at'])-datetime.fromisoformat(plan['quiet'])).total_seconds())<=15
            checks['backup_integrity']=backup_integrity(root/'backups/latest.sqlite3')
            result=dict(scope='isolated_full_session_validation_not_formal_scores',at=stamp().isoformat(),checks=checks,passed=all(checks.values()),
                report='evaluation.json',publication_delay_seconds=(release-ns(decision))/1e9 if release else None,flow=flow)
            receipt(root,'validation_result.json',result)
            if not result['passed']:raise RuntimeError('validation_gate_failed')
        finally:store.close()
        finish=execute([str(PREFIX/'venv/bin/python'),str(PREFIX/'finish.py'),str(root)])
        if finish.returncode:raise RuntimeError('validation_finalization_failed')
        return result
    raise ValueError('Unsupported validation phase')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',required=True,type=Path);parser.add_argument('--phase',required=True)
    args=parser.parse_args();path=args.plan
    if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o022:raise ValueError('Validation plan must be root-owned and read-only')
    plan=json.loads(path.read_text());validate_plan(plan);os.umask(0o077)
    try:
        result=run_phase(plan,args.phase);print(json.dumps(result))
        if 'condition_passed' in result:return 0 if result['condition_passed'] else 1
        return 0
    except Exception:
        print(json.dumps(dict(state='error',code='validation_phase_failed',phase=args.phase)));return 1


if __name__=='__main__':raise SystemExit(main())
