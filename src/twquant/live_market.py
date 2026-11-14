"""Bounded local Fubon market-data collection; paper forecasts only."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import queue
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

from .credentials import load_credentials,prepare_sdk_log_directory,private_certificate_path
from .live_pilot import PilotStore,ns,iso,private_json

TZ=ZoneInfo('Asia/Taipei')
LABEL='com.twquant.0050pilot'


def now():return datetime.now(TZ)


def market_sdk():
    """Keep all SDK authentication details inside the read-only adapter."""
    c=load_credentials();prepare_sdk_log_directory()
    try:
        from fubon_neo.sdk import FubonSDK
        key='FUBON_API_KEY' if c.status()['FUBON_API_KEY'] else 'FUBON_PASSWORD'
        values=[c.get(k).get_secret_value() for k in ('FUBON_PERSONAL_ID',key,'FUBON_CERT_PATH')]
        values[2]=str(private_certificate_path(values[2]))
        if c.status()['FUBON_CERT_PASSWORD']:values.append(c.get('FUBON_CERT_PASSWORD').get_secret_value())
        sdk=FubonSDK()
        login=(sdk.apikey_login if key=='FUBON_API_KEY' else sdk.login)(*values)
        if not getattr(login,'is_success',False):raise RuntimeError('rejected')
        sdk.init_realtime()
        return sdk
    except Exception:raise RuntimeError('market_authentication_failed') from None
    finally:prepare_sdk_log_directory()


class FubonFeed:
    def __init__(self,symbols,*,sdk=None):
        self.symbols=list(symbols);self.sdk=sdk;self.stock=None
        self.subscriptions=set();self.failed=None;self.last_received_ns=0
        self.expected={(t,c) for t in symbols if not t.startswith('IX') for c in ('trades','books')}
        self.expected|={(t,'indices') for t in symbols if t.startswith('IX')}

    @property
    def ready(self):return self.subscriptions==self.expected and not self.failed

    def connect(self,on_message,on_disconnect):
        self.sdk=self.sdk or market_sdk();self.stock=self.sdk.marketdata.websocket_client.stock
        def disconnected(*_):
            self.failed='disconnected';on_disconnect('disconnected')
        def transport_error(*_):
            self.failed='transport_error';on_disconnect('transport_error')
        def receive(raw):
            self.last_received_ns=ns(now())
            try:
                message=json.loads(raw);event=message.get('event')
                if event=='error':
                    self.failed='provider_error';on_disconnect('provider_error');return
                if event=='subscribed':
                    data=message.get('data',[]);data=data if isinstance(data,list) else [data]
                    self.subscriptions.update((str(r.get('symbol')),r.get('channel')) for r in data)
                if event in ('data','snapshot'):on_message(message)
            except Exception:
                self.failed='invalid_message';on_disconnect('invalid_message')
        self.stock.on('message',receive);self.stock.on('disconnect',disconnected);self.stock.on('error',transport_error)
        self.stock.connect()
        tradable=[t for t in self.symbols if not t.startswith('IX')]
        for channel in ('trades','books'):self.stock.subscribe(dict(channel=channel,symbols=tradable))
        for ticker in self.symbols:
            if ticker.startswith('IX'):self.stock.subscribe(dict(channel='indices',symbol=ticker))

    def close(self):
        if self.stock:
            try:self.stock.disconnect()
            except Exception:pass


def launch_agent(workspace,python,root,mode):
    workspace=Path(workspace).resolve();python=Path(python).absolute();root=Path(root).resolve()
    if mode not in ('collect','report','forecast') or not python.is_relative_to(workspace) or not root.is_relative_to(workspace):
        raise ValueError('Launch jobs require fixed trusted workspace paths')
    arguments=[str(python),'-m','twquant.live_market','--root',str(root),'--mode',mode]
    if mode=='collect':arguments=['/usr/bin/caffeinate','-i','-s',*arguments]
    value=dict(Label=LABEL+'.'+mode,ProgramArguments=arguments,WorkingDirectory=str(workspace),
        RunAtLoad=True,StartInterval=60 if mode=='collect' else 300,ProcessType='Background',
        EnvironmentVariables={'TZ':'Asia/Taipei','PYTHONPATH':str(root/'source_snapshot/src'),'LOKY_MAX_CPU_COUNT':'1','TWQUANT_PILOT_MANAGED':'1'},
        StandardOutPath=str(root/(mode+'.log')),StandardErrorPath=str(root/(mode+'.log')),
        ThrottleInterval=60,Umask=0o077)
    if mode=='forecast':
        value.pop('StartInterval')
        dates=json.loads((root/'protocol.json').read_text())['dates'];schedule=[]
        for day in dates:
            local=datetime.fromtimestamp(ns(day+'T09:59:00+08:00')/1e9).astimezone()
            schedule.append(dict(Month=local.month,Day=local.day,Hour=local.hour,Minute=local.minute))
        value['StartCalendarInterval']=schedule
    return value


def probe(root,store,seconds=25):
    """Subscription acknowledgements verify access, not tick completeness."""
    feed=FubonFeed(store.symbols);errors=[];count=[0]
    try:
        feed.connect(lambda m:count.__setitem__(0,count[0]+int(m.get('event') in ('snapshot','data'))),errors.append)
        end=time.monotonic()+seconds
        while time.monotonic()<end and not feed.ready and not errors:time.sleep(.1)
        status=dict(source='fubon',required_subscriptions=len(feed.expected),acknowledged_subscriptions=len(feed.subscriptions),
            access_verified=feed.ready,market_messages=count[0],code=feed.failed)
    except Exception:status=dict(source='fubon',access_verified=False,code='market_probe_failed')
    finally:feed.close()
    private_json(Path(root)/'access_check.json',status);return status


def collect(root,store):
    today=now().date().isoformat()
    if today not in store.decisions:return dict(state='outside_declared_dates')
    clock=now();start=clock.replace(hour=8,minute=45,second=0,microsecond=0);end=clock.replace(hour=13,minute=40,second=0,microsecond=0)
    if clock<start or clock>end:return dict(state='outside_collection_window')
    if (root/'fatal.json').exists():return dict(state='blocked_by_provider_error')
    lock=(root/'collector.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:lock.close();return dict(state='already_running')
    feed=None;inbox=queue.Queue(maxsize=100000);errors=queue.Queue();overflow=threading.Event()
    def receive(message):
        try:inbox.put_nowait((message,now()))
        except queue.Full:overflow.set()
    retries=0;connected_start=None;previous=now();last_flush=time.monotonic();last_state=0.;last_mono=time.monotonic()
    try:
        while now()<end:
            current=now();mono=time.monotonic()
            if ns(current)-ns(previous)>2*10**9:
                store.mark_gap(previous,current,'stalled')
            if abs((ns(current)-ns(previous))/1e9-(mono-last_mono))>.25:
                store.mark_gap(min(previous,current),max(previous,current),'clock_jump')
            previous=current;last_mono=mono
            if feed is None:
                feed=FubonFeed(store.symbols)
                try:feed.connect(receive,errors.put)
                except Exception:errors.put('transport_error')
            drained=0
            while not inbox.empty() and drained<20000:
                message,received=inbox.get_nowait();drained+=1
                try:store.ingest(message,received_at=received)
                except (ValueError,TypeError,KeyError):errors.put('invalid_message')
            if overflow.is_set():
                store.mark_gap(current,now(),'stalled');overflow.clear()
            while not errors.empty():
                code=errors.get_nowait();store.mark_gap(iso(feed.last_received_ns) if feed and feed.last_received_ns else current,now(),'disconnected')
                if code in ('provider_error','invalid_message'):
                    private_json(root/'fatal.json',dict(code=code,at=now().isoformat()))
                    return dict(state='blocked',code=code)
                if feed:feed.close()
                feed=None;connected_start=None;retries+=1
                if retries>5:return dict(state='connection_retries_exhausted')
                time.sleep(min(2**retries,30))
                # close() callbacks belong to the old connection, not a new fault.
                while not errors.empty():errors.get_nowait()
            if feed and feed.ready and feed.last_received_ns>=ns(now())-45*10**9:
                if connected_start is None:connected_start=now();retries=0
                if mono-last_flush>=1:
                    store.mark_coverage(connected_start,now());last_flush=mono
            elif feed and feed.last_received_ns and feed.last_received_ns<ns(now())-45*10**9:
                errors.put('transport_error')
            if mono-last_flush>=1:store.flush();last_flush=mono
            if mono-last_state>=5:
                private_json(root/'collector_status.json',dict(state='collecting',date=today,at=now().isoformat(),
                    subscriptions=len(feed.subscriptions) if feed else 0,required_subscriptions=len(feed.expected) if feed else 0,
                    backlog=inbox.qsize(),stream_ready=bool(feed and feed.ready)))
                last_state=mono
            time.sleep(.02)
        store.flush();return dict(state='day_collection_finished',date=today)
    finally:
        if feed:feed.close()
        lock.close()


def forecast(root,store):
    day=now().date().isoformat()
    if day not in store.decisions:return dict(state='outside_declared_dates')
    decision=store.decisions[day]
    if ns(now())<decision-65*10**9 or ns(now())>decision+300*10**9:return dict(state='outside_forecast_window')
    with (root/'forecast.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='forecast_already_running')
        while ns(now())<decision:time.sleep(.005)
        rows=store.emit(now());store.seal(day,now(),allow_existing=True)
        return dict(state='forecast_recorded',date=day,targets=len(rows),model_version=store.protocol['model_version'])


def retire_jobs(root):
    """Retire only the three fixed jobs whose --root identifies this pilot."""
    for mode in ('collect','forecast','report'):
        label=LABEL+'.'+mode;path=Path.home()/'Library/LaunchAgents'/(label+'.plist')
        if not path.exists():continue
        value=plistlib.loads(path.read_bytes());arguments=value.get('ProgramArguments',[])
        if value.get('Label')!=label or '--root' not in arguments:continue
        i=arguments.index('--root')
        if i+1>=len(arguments) or arguments[i+1]!=str(Path(root).resolve()):continue
        path.unlink()
        subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{label}'],capture_output=True,check=False)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Ten-session local read-only market pilot')
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--mode',required=True,choices=('collect','forecast','report','probe','status'))
    args=parser.parse_args(argv);root=args.root.resolve();store=None
    try:
        protocol=json.loads((root/'protocol.json').read_text());store=PilotStore(root,protocol)
        if args.mode=='probe':result=probe(root,store)
        elif args.mode=='collect':result=collect(root,store)
        elif args.mode=='forecast':result=forecast(root,store)
        elif args.mode=='report':
            final=ns(now())>=max(store.decisions.values())+(13*3600+40*60-10*3600)*10**9
            report=store.write_report(as_of=now(),final=final)
            result=dict(state='final_report_ready' if final else 'progress_report_ready',report=str(root/'evaluation.md'))
        else:
            result=dict(state='configured',dates=protocol['dates'],source=protocol['source'],
                tradable_symbols=len(store.tradable),total_targets=len(store.symbols),horizons=protocol['horizons'],
                event_count=store.db.execute('SELECT count(*) FROM events').fetchone()[0])
        private_json(root/('last_'+args.mode+'.json'),result)
        print(json.dumps(result),flush=True)
        store.close();store=None
        if args.mode=='report' and result['state']=='final_report_ready' and os.environ.get('TWQUANT_PILOT_MANAGED')=='1':
            retire_jobs(root)
        return 0
    except Exception:
        if root.exists():private_json(root/('last_'+args.mode+'.json'),dict(state='error',code='pilot_operation_failed',at=now().isoformat()))
        print(json.dumps(dict(state='error',code='pilot_operation_failed')));return 1
    finally:
        if store:store.close()


if __name__=='__main__':raise SystemExit(main())
