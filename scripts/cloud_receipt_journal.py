"""Private sequential receipts and atomic, bounded SQLite materialization.

Receipt acquisition loads only the unchanged frozen encoder/feed definitions.
Scientific dependencies and indexed database writes belong to the consumer.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime,timedelta,timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')
MAGIC=b'TWQJ1\n'
MAX_FRAME=32768
SEGMENT_BYTES=32*1024**2
EPOCH=datetime(1970,1,1,tzinfo=timezone.utc)


def now():return datetime.now(TZ)


def ns(value):
    if value.tzinfo is None:raise ValueError('Explicit timezone required')
    delta=value.astimezone(timezone.utc)-EPOCH
    return (delta.days*86400+delta.seconds)*10**9+delta.microseconds*1000+getattr(delta,'nanoseconds',0)


def private_json(path,value):
    path=Path(path);fd,name=tempfile.mkstemp(dir=path.parent,prefix=path.name+'.',suffix='.tmp')
    try:
        with os.fdopen(fd,'w') as out:
            os.fchmod(out.fileno(),0o600);json.dump(value,out,allow_nan=False);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


def read_json(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default


def error_details(error):
    name=type(error).__name__;allowed={'ValueError','TypeError','OSError','PermissionError','OperationalError','IntegrityError','AttributeError'}
    frames=[];trace=error.__traceback__
    while trace:
        if Path(trace.tb_frame.f_code.co_filename).name==Path(__file__).name:frames.append(dict(function=trace.tb_frame.f_code.co_name,line=trace.tb_lineno))
        trace=trace.tb_next
    return dict(code='receipt_operation_failed',error_class=name if name in allowed else 'other',
        sqlite_code=getattr(error,'sqlite_errorcode',None) if isinstance(error,sqlite3.Error) else None,frames=frames)


def protocol_hash(protocol):return hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()


def verify_source(source,protocol):
    source=Path(source)
    if source.is_symlink():raise ValueError('journal_source_untrusted')
    digest=hashlib.sha256()
    for path in sorted(source.glob('*.py')):
        if path.is_symlink():raise ValueError('journal_source_untrusted')
        digest.update(path.name.encode());digest.update(path.read_bytes())
    if protocol.get('source_sha256',digest.hexdigest())!=digest.hexdigest():raise ValueError('journal_source_changed')


def definitions(path,names):
    tree=ast.parse(Path(path).read_text());nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name in names]
    if {n.name for n in nodes}!=set(names):raise ValueError('journal_reference_definition_missing')
    return ast.Module(body=nodes,type_ignores=[])


class FrozenAdapter:
    """Run the exact frozen validation body against a row-capture connection."""
    def __init__(self,source,protocol):
        source=Path(source);verify_source(source,protocol)
        tree=definitions(source/'live_pilot.py',['PilotStore'])
        method=next(n for n in tree.body[0].body if isinstance(n,ast.FunctionDef) and n.name=='ingest')
        namespace=dict(json=json,math=math,hashlib=hashlib,ns=ns)
        exec(compile(ast.Module(body=[method],type_ignores=[]),str(source/'live_pilot.py'),'exec'),namespace)
        self.ingest=namespace['ingest'];self.symbols=list(dict.fromkeys(protocol['members']+['0050','1216','2002','2412','IX0001']))
        self.pending=0;self.lock=threading.RLock();self.db=self;self.row=None;self.rowcount=1
    def execute(self,sql,row):
        if not sql.startswith('INSERT OR IGNORE INTO events('):raise ValueError('journal_encoder_sql_changed')
        self.row=list(row);return self
    def flush(self):self.pending=0
    def encode(self,message,received_at):
        self.row=None;self.ingest(self,message,received_at=received_at);return self.row


def frozen_feed(source,protocol):
    verify_source(source,protocol)
    from twquant.credentials import load_credentials,prepare_sdk_log_directory,private_certificate_path
    namespace=dict(json=json,now=now,ns=ns,load_credentials=load_credentials,
        prepare_sdk_log_directory=prepare_sdk_log_directory,private_certificate_path=private_certificate_path)
    exec(compile(definitions(Path(source)/'live_market.py',['market_sdk','FubonFeed']),str(Path(source)/'live_market.py'),'exec'),namespace)
    return namespace['FubonFeed']


def frame(value):
    body=json.dumps(value,separators=(',',':'),allow_nan=False).encode()
    if len(body)>MAX_FRAME:raise ValueError('journal_frame_too_large')
    return struct.pack('!II',len(body),zlib.crc32(body))+body


def read_frame(handle,limit):
    start=handle.tell()
    if start+8>limit:return None
    header=handle.read(8)
    if len(header)!=8:return None
    length,checksum=struct.unpack('!II',header)
    if not 0<length<=MAX_FRAME:raise ValueError('journal_frame_size_invalid')
    if start+8+length>limit:return None
    body=handle.read(length)
    if len(body)!=length:return None
    if zlib.crc32(body)!=checksum:raise ValueError('journal_checksum_failed')
    try:return json.loads(body)
    except ValueError:raise ValueError('journal_frame_invalid') from None


class Journal:
    def __init__(self,root,source,*,segment_bytes=SEGMENT_BYTES,day=None,session_mode=False):
        self.root=Path(root);self.protocol=read_json(self.root/'protocol.json');self.encoder=FrozenAdapter(source,self.protocol)
        self.day=day;self.session_mode=session_mode
        self.directory=self.root/'receipts'
        if self.directory.is_symlink():raise ValueError('journal_directory_untrusted')
        self.directory.mkdir(mode=0o700,exist_ok=True);self.directory.chmod(0o700)
        self.lock=threading.RLock();self.segment_bytes=segment_bytes;self.events=0;self.records=0;self.closed=False
        files=sorted(self.directory.glob('*.twqj'));self.sequence=max([int(f.stem) for f in files],default=0)
        recovered=[];self.prior_events=0
        for path in files:
            if path.is_symlink():raise ValueError('journal_segment_untrusted')
            seal=read_json(path.with_suffix('.seal.json'))
            if seal is None:
                with path.open('rb') as handle:
                    if handle.read(len(MAGIC))!=MAGIC:raise ValueError('journal_header_invalid')
                    header=read_frame(handle,path.stat().st_size)
                    if not header or header.get('protocol')!=protocol_hash(self.protocol):raise ValueError('journal_protocol_changed')
                    offset=handle.tell();events=0;records=0;received=None
                    while True:
                        try:value=read_frame(handle,path.stat().st_size)
                        except ValueError:break
                        if value is None:break
                        offset=handle.tell();records+=1
                        if value[0]=='event':events+=1;received=value[1][3]
                digest=hashlib.sha256()
                with path.open('rb') as handle:
                    for block in iter(lambda:handle.read(1024**2),b''):digest.update(block)
                seal=dict(offset=offset,events=events,records=records,sha256=digest.hexdigest(),complete=False,recovered=True,trailing_bytes=path.stat().st_size-offset,day=header.get('day'))
                private_json(path.with_suffix('.seal.json'),seal)
                start=datetime.fromtimestamp(received/1e9,TZ) if received else datetime.fromisoformat(header['created_at'])
                day=start.date().isoformat()
                if day in self.protocol['dates']:
                    end=datetime.fromisoformat(day+'T'+self.protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
                    recovered.append((start,max(start,min(now(),end))))
            if day is None or seal.get('day')==day:self.prior_events+=seal['events']
        self._open()
        for start,end in recovered:self.control('gap',start,end,'stalled')
    def _open(self):
        self.sequence+=1;self.path=self.directory/f'{self.sequence:08d}.twqj'
        fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        self.handle=os.fdopen(fd,'wb',buffering=0);self.digest=hashlib.sha256();self.segment_events=0;self.segment_records=0
        self._write(MAGIC+frame(dict(protocol=protocol_hash(self.protocol),created_at=now().isoformat(),day=self.day,event_base=self.prior_events+self.events)))
        os.fsync(self.handle.fileno())
        directory=os.open(self.directory,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    def _write(self,body):
        view=memoryview(body)
        while view:
            count=self.handle.write(view)
            if not count:raise OSError('journal_short_write')
            view=view[count:]
        self.digest.update(body)
    def _seal(self):
        self.handle.flush();os.fsync(self.handle.fileno())
        private_json(self.path.with_suffix('.seal.json'),dict(offset=self.handle.tell(),events=self.segment_events,records=self.segment_records,sha256=self.digest.hexdigest(),complete=True,day=self.day))
        self.handle.close()
    def _append(self,value):
        body=frame(value)
        if self.handle.tell()+len(body)>self.segment_bytes and self.segment_records:
            self._seal();self._open()
        self._write(body);self.records+=1;self.segment_records+=1
        if value[0]=='event':self.events+=1;self.segment_events+=1
    def event(self,message,received_at):
        with self.lock:
            if self.closed:raise ValueError('journal_closed')
            row=self.encoder.encode(message,received_at)
            if row is None:return False
            self._append(['event',row]);return True
    def control(self,kind,start,end,reason=None):
        if kind not in ('gap','coverage') or end<start or (kind=='gap' and reason not in ('disconnected','stalled','clock_jump','invalid_message')):raise ValueError('journal_control_invalid')
        with self.lock:self._append([kind,[ns(start),ns(end),reason]])
    def flush(self):
        with self.lock:
            duplicate=os.dup(self.handle.fileno());value=dict(segment=self.path.name,offset=self.handle.tell(),events=self.events,events_total=self.prior_events+self.events,records=self.records,at=now().isoformat(),day=self.day,session_mode=self.session_mode)
        try:os.fsync(duplicate)
        finally:os.close(duplicate)
        private_json(self.root/'receipt_watermark.json',value);return value
    def close(self):
        with self.lock:
            if not self.closed:self.flush();self._seal();self.closed=True


def session_path(root,day):
    protocol=read_json(Path(root)/'protocol.json')
    if day not in protocol['dates']:raise ValueError('journal_session_date_invalid')
    path=Path(root)/'sessions'/day
    if path.is_symlink() or path.parent.is_symlink():raise ValueError('journal_session_path_untrusted')
    return path


def prepare_session(root,source,day):
    root=Path(root);protocol=read_json(root/'protocol.json');verify_source(source,protocol);path=session_path(root,day)
    path.parent.mkdir(mode=0o700,exist_ok=True);path.mkdir(mode=0o700,exist_ok=True)
    with (root/('session-'+day+'.lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        ready=read_json(path/'session_ready.json',{})
        if ready:
            if ready.get('protocol')!=protocol_hash(protocol):raise ValueError('journal_session_protocol_changed')
            return path
        private_json(path/'protocol.json',protocol)
        from twquant.live_pilot import PilotStore
        store=PilotStore(path,protocol)
        try:
            decision=ns(datetime.fromisoformat(day+'T'+protocol.get('prediction_time','10:00:00')).replace(tzinfo=TZ));received_cutoff=min(decision,ns(now()))
            source_db=root/'pilot.sqlite3';seeds=[]
            if source_db.exists():
                with sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True,timeout=30) as previous:
                    for ticker in store.symbols:
                        for channel in ['books','trades','indices']:
                            row=previous.execute('select event_ns from events indexed by event_lookup where ticker=? and channel=? and event_ns<=? and received_ns<=? order by event_ns desc,id desc limit 1',(ticker,channel,decision,received_cutoff)).fetchone()
                            if row and row[0] is not None:
                                seeds.extend(previous.execute('select id,ticker,channel,event_ns,received_ns,kind,serial,payload,digest from events indexed by event_lookup where ticker=? and channel=? and event_ns=? and received_ns<=? order by id',(ticker,channel,row[0],received_cutoff)).fetchall())
            store.db.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)',sorted(seeds));store.flush()
            private_json(path/'session_ready.json',dict(protocol=protocol_hash(protocol),date=day,seed_rows=len(seeds),at=now().isoformat()))
        finally:store.close()
    return path


class Materializer:
    def __init__(self,root,source,*,session_day=None):
        self.root=Path(root);protocol=read_json(self.root/'protocol.json');verify_source(source,protocol)
        self.session_day=session_day
        from twquant.live_pilot import PilotStore
        self.store=PilotStore(prepare_session(root,source,session_day) if session_day else self.root,protocol)
        self.store.db.execute('CREATE TABLE IF NOT EXISTS cloud_receipt_cursor(segment TEXT PRIMARY KEY,offset INTEGER NOT NULL,events INTEGER NOT NULL,records INTEGER NOT NULL)');self.store.db.commit()
        self.store.db.execute('pragma cache_size=-65536')
        self.store.db.execute('pragma busy_timeout=100')
        self.finished=set();self.last_received_ns=0
    def step(self,*,max_records=1024,max_bytes=2*1024**2,stamp=None):
        if stamp is not None and stamp.date().isoformat() in self.store.protocol['dates']:
            decision=datetime.fromisoformat(stamp.date().isoformat()+'T'+self.store.protocol.get('prediction_time','10:00:00')).replace(tzinfo=TZ)
            if decision-timedelta(seconds=2)<=stamp<decision+timedelta(seconds=1.25):return dict(state='forecast_priority',events=0,records=0)
        db=self.store.db;directory=self.root/'receipts';active=read_json(self.root/'receipt_watermark.json',{})
        if directory.is_symlink():raise ValueError('journal_directory_untrusted')
        batches=[];size=0;total=0;events=0
        for path in sorted(directory.glob('*.twqj')):
            if path.name in self.finished:continue
            if path.is_symlink():raise ValueError('journal_segment_untrusted')
            seal=read_json(path.with_suffix('.seal.json'),{})
            limit=seal.get('offset',active.get('offset',0) if active.get('segment')==path.name else 0)
            if not limit:continue
            with path.open('rb') as handle:
                if handle.read(len(MAGIC))!=MAGIC:raise ValueError('journal_header_invalid')
                header=read_frame(handle,limit)
                if not header or header.get('protocol')!=protocol_hash(self.store.protocol):raise ValueError('journal_protocol_changed')
                if self.session_day and header.get('day')!=self.session_day:self.finished.add(path.name);continue
                old=db.execute('select offset,events,records from cloud_receipt_cursor where segment=?',(path.name,)).fetchone()
                offset,previous_events,previous_records=old or (handle.tell(),0,0);handle.seek(offset);records=[];event_count=0
                while total<max_records and size<max_bytes:
                    value=read_frame(handle,limit)
                    if value is None:break
                    if not isinstance(value,list) or len(value)!=2 or value[0] not in ('event','gap','coverage'):raise ValueError('journal_record_invalid')
                    if value[0]=='event':
                        if not isinstance(value[1],list) or len(value[1])!=8:raise ValueError('journal_event_invalid')
                        event_count+=1
                    records.append(value);total+=1;size+=handle.tell()-offset;offset=handle.tell()
                if records:batches.append((path.name,offset,previous_events+event_count,previous_records+len(records),records,header.get('event_base',0)+previous_events));events+=event_count
                if seal and handle.tell()>=limit and not records:self.finished.add(path.name)
            if total>=max_records or size>=max_bytes:break
        if not batches:return dict(events=0,records=0)
        try:
            db.execute('begin immediate')
            for name,offset,count,records_count,records,event_base in batches:
                rows=[v for kind,v in records if kind=='event']
                if self.session_day:
                    slot=int(self.session_day.replace('-',''))*10**9
                    db.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)',[(slot+event_base+i+1,*row) for i,row in enumerate(rows)])
                else:db.executemany('INSERT OR IGNORE INTO events(ticker,channel,event_ns,received_ns,kind,serial,payload,digest) VALUES(?,?,?,?,?,?,?,?)',rows)
                for kind,value in records:
                    if kind=='coverage':db.execute('INSERT INTO coverage VALUES(?,?) ON CONFLICT(start_ns) DO UPDATE SET end_ns=max(end_ns,excluded.end_ns)',value[:2])
                    elif kind=='gap':db.execute('INSERT INTO gaps VALUES(?,?,?)',value)
                db.execute('INSERT INTO cloud_receipt_cursor VALUES(?,?,?,?) ON CONFLICT(segment) DO UPDATE SET offset=excluded.offset,events=excluded.events,records=excluded.records',(name,offset,count,records_count))
            db.commit()
            if rows:self.last_received_ns=rows[-1][3]
        except BaseException:db.rollback();raise
        return dict(events=events,records=total)
    def close(self):self.store.close()


def materializer_state(consumer):
    count=consumer.store.db.execute('select coalesce(sum(events),0) from cloud_receipt_cursor').fetchone()[0]
    return dict(at=now().isoformat(),state='running',events_total=count,last_received_ns=consumer.last_received_ns,date=consumer.session_day)


def merge_session(root,source,day):
    root=Path(root);path=session_path(root,day);protocol=read_json(root/'protocol.json');verify_source(source,protocol)
    from twquant.live_pilot import PilotStore
    store=PilotStore(root,protocol);db=store.db;db.execute('pragma cache_size=-65536')
    try:
        with sqlite3.connect((path/'pilot.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=30) as session:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_session_merge(day TEXT PRIMARY KEY,event_id INTEGER,gap_id INTEGER)');db.commit()
            old=db.execute('select event_id,gap_id from cloud_session_merge where day=?',(day,)).fetchone() or (0,0)
            event_id,gap_id=old
            while True:
                rows=session.execute('select id,ticker,channel,event_ns,received_ns,kind,serial,payload,digest from events where id>? order by id limit 1024',(event_id,)).fetchall()
                if not rows:break
                try:
                    db.execute('begin immediate');db.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?,?,?)',rows);event_id=rows[-1][0]
                    db.execute('INSERT INTO cloud_session_merge VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET event_id=excluded.event_id',(day,event_id,gap_id));db.commit()
                except BaseException:db.rollback();raise
            try:
                db.execute('begin immediate')
                for row in session.execute('select start_ns,end_ns from coverage'):
                    db.execute('INSERT INTO coverage VALUES(?,?) ON CONFLICT(start_ns) DO UPDATE SET end_ns=max(end_ns,excluded.end_ns)',row)
                for row in session.execute('select rowid,start_ns,end_ns,reason from gaps where rowid>? order by rowid',(gap_id,)):
                    gap_id=row[0];db.execute('INSERT INTO gaps VALUES(?,?,?)',row[1:])
                for row in session.execute('select day,ticker,row from predictions where day=?',(day,)):
                    existing=db.execute('select row from predictions where day=? and ticker=?',row[:2]).fetchone()
                    if existing and existing[0]!=row[2]:raise ValueError('journal_prediction_conflict')
                    db.execute('INSERT OR IGNORE INTO predictions VALUES(?,?,?)',row)
                seal=session.execute('select at_ns from releases where day=?',(day,)).fetchone()
                if seal:
                    existing=db.execute('select at_ns from releases where day=?',(day,)).fetchone()
                    if existing and existing!=seal:raise ValueError('journal_publication_conflict')
                    db.execute('INSERT OR IGNORE INTO releases VALUES(?,?)',(day,seal[0]))
                db.execute('INSERT INTO cloud_session_merge VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET event_id=excluded.event_id,gap_id=excluded.gap_id',(day,event_id,gap_id));db.commit()
            except BaseException:db.rollback();raise
            count=session.execute('select coalesce(sum(events),0) from cloud_receipt_cursor').fetchone()[0]
        result=dict(at=now().isoformat(),date=day,state='complete',events_total=count)
        private_json(root/'session_merge_status.json',result);return result
    finally:store.close()


def consume(root,source,*,replay=False,session_mode=False):
    root=Path(root);protocol=read_json(root/'protocol.json');stop=threading.Event()
    signal.signal(signal.SIGTERM,lambda *_:stop.set())
    stamp=now();day=stamp.date().isoformat()
    if day not in protocol['dates']:return dict(state='outside_declared_dates')
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
    if replay and not end+timedelta(minutes=3)<=stamp<end+timedelta(minutes=45):return dict(state='outside_replay_window')
    if not replay and stamp>=end+timedelta(seconds=120):return dict(state='outside_consumer_window')
    with (root/'receipt_consumer.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='consumer_already_running')
        consumer=Materializer(root,source,session_day=day if session_mode else None);last_status=0;deadline=time.monotonic()+(600 if replay else max(0,(end-now()).total_seconds())+120)
        try:
            while not stop.is_set() and time.monotonic()<deadline:
                try:result=consumer.step(stamp=now())
                except sqlite3.OperationalError as error:
                    if getattr(error,'sqlite_errorcode',None) not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
                    time.sleep(.1);continue
                if time.monotonic()-last_status>=1:
                    private_json(root/'materializer_status.json',materializer_state(consumer));last_status=time.monotonic()
                if not result['records']:
                    done=read_json(root/'receipt_capture_done.json',{})
                    if replay or done.get('date')==day:break
                    time.sleep(.05)
            state=materializer_state(consumer);state['state']='stopped';private_json(root/'materializer_status.json',state)
        finally:consumer.close()
        if session_mode and (replay or (now()>=end and read_json(root/'receipt_capture_done.json',{}).get('date')==day)):
            merge_session(root,source,day)
        return state


def forecast_session(root,source,*,session_mode=True):
    root=Path(root);protocol=read_json(root/'protocol.json');verify_source(source,protocol);stamp=now();day=stamp.date().isoformat()
    if day not in protocol['dates']:return dict(state='outside_declared_dates')
    decision=datetime.fromisoformat(day+'T'+protocol.get('prediction_time','10:00:00')).replace(tzinfo=TZ)
    if not decision-timedelta(seconds=65)<=stamp<=decision+timedelta(seconds=300):return dict(state='outside_forecast_window')
    from twquant.live_market import PilotStore,forecast
    path=prepare_session(root,source,day) if session_mode else root;store=PilotStore(path,protocol)
    try:
        result=forecast(path,store);private_json(root/'last_forecast.json',result);return result
    finally:store.close()


def close_feed(feed):
    if feed:
        worker=threading.Thread(target=feed.close,daemon=True);worker.start();worker.join(timeout=2)


def capture(root,source,*,feed_factory=None,clock=now,sleep=time.sleep,launch=subprocess.Popen,stop=None,drain_seconds=90,session_mode=False):
    root=Path(root);protocol=read_json(root/'protocol.json');stamp=clock();day=stamp.date().isoformat()
    if day not in protocol['dates']:return dict(state='outside_declared_dates')
    start=datetime.fromisoformat(day+'T'+protocol.get('collection_start','09:30:00')).replace(tzinfo=TZ)
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
    if not start<=stamp<end:return dict(state='outside_collection_window')
    if (root/'fatal.json').exists():return dict(state='blocked_by_provider_error')
    stop=stop or threading.Event();feed_factory=feed_factory or frozen_feed(source,protocol)
    with (root/'collector.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='already_running')
        journal=Journal(root,source,day=day if session_mode else None,session_mode=session_mode);(root/'receipt_capture_done.json').unlink(missing_ok=True)
        child_args=[sys.executable,str(Path(__file__).resolve()),'--root',str(root),'--source',str(source),*(['--sessions'] if session_mode else []),'--phase','consume']
        child=launch(child_args)
        errors=queue.SimpleQueue();accepting=True;gate=threading.RLock();feed=None;generation=0;retries=0
        counts=dict(received_messages=0,invalid_messages=0,dropped_messages=0)
        previous=clock();previous_mono=time.monotonic();connected=None;last_flush=0.;last_status=0.;failure=None;last_child_retry=0.;child_restarts=0
        def receive(message,identity):
            with gate:
                received=clock()
                if not accepting or identity!=generation or received>=end:return
                counts['received_messages']+=1
                try:journal.event(message,received)
                except (ValueError,TypeError,KeyError):counts['invalid_messages']+=1;errors.put('invalid_message')
                except OSError:counts['dropped_messages']+=1;errors.put('receipt_write_failed');stop.set()
        def status(state):
            imported=read_json(root/'materializer_status.json',{}).get('events_total',0)
            if session_mode and read_json(root/'materializer_status.json',{}).get('date')!=day:imported=0
            durable=read_json(root/'receipt_watermark.json',{}).get('events',0)
            return dict(at=clock().isoformat(),date=day,state=state,runtime='cloud_journal_v3',accepting=accepting,
                stream_ready=bool(feed and feed.ready),subscriptions=len(feed.subscriptions) if feed else 0,
                required_subscriptions=len(feed.expected) if feed else 0,backlog=max(0,journal.prior_events+journal.events-imported),
                processed_messages=max(0,imported-journal.prior_events),durable_received_messages=durable,
                memory_backlog=0,session_mode=session_mode,materializer_pid=getattr(child,'pid',None),materializer_active=child.poll() is None,materializer_restarts=child_restarts,**counts)
        try:
            while clock()<end and not stop.is_set():
                current=clock();mono=time.monotonic()
                if (current-previous).total_seconds()>2:journal.control('gap',previous,current,'stalled')
                if abs((current-previous).total_seconds()-(mono-previous_mono))>.25:journal.control('gap',min(previous,current),max(previous,current),'clock_jump')
                previous=current;previous_mono=mono
                if feed is None:
                    feed=feed_factory(journal.encoder.symbols);generation+=1;identity=generation
                    try:feed.connect(lambda message,identity=identity:receive(message,identity),lambda code,identity=identity:errors.put(code) if identity==generation else None)
                    except Exception:errors.put('transport_error')
                if not errors.empty():
                    code=errors.get();journal.control('gap',current,clock(),'disconnected')
                    if code in ('provider_error','invalid_message','receipt_write_failed'):
                        failure=code;stop.set();break
                    generation+=1;close_feed(feed);feed=None;connected=None;retries+=1
                    if retries>5:failure='connection_retries_exhausted';break
                    sleep(min(2**retries,30));continue
                if feed.ready and feed.last_received_ns>=ns(current)-45*10**9:
                    if connected is None:connected=current;retries=0
                    if mono-last_flush>=1:journal.control('coverage',connected,current)
                elif feed.last_received_ns and feed.last_received_ns<ns(current)-45*10**9:errors.put('transport_error')
                if mono-last_flush>=1:journal.flush();last_flush=mono
                if mono-last_status>=5:
                    private_json(root/'collector_status.json',status('collecting'));last_status=mono
                    if os.statvfs(root).f_bavail*os.statvfs(root).f_frsize<protocol.get('minimum_free_bytes',4*1024**3):failure='storage_reserve_reached';break
                if child.poll() is not None and child_restarts<3 and mono-last_child_retry>=120:
                    child=launch(child_args);last_child_retry=mono;child_restarts+=1
                sleep(.05)
        finally:
            with gate:accepting=False;generation+=1
            journal.close()
            private_json(root/'receipt_capture_done.json',dict(date=day,at=clock().isoformat(),events_total=journal.prior_events+journal.events))
            close_feed(feed)
            deadline=time.monotonic()+drain_seconds
            while child.poll() is None and time.monotonic()<deadline:sleep(.1)
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        if counts['invalid_messages'] and not failure:failure='invalid_message'
        value=status('stopped' if clock()<end else 'day_collection_finished');value.update(remaining_messages=value['backlog'],receipt_complete=counts['dropped_messages']==0 and counts['invalid_messages']==0)
        value['complete']=value['receipt_complete'] and not value['backlog'] and not failure
        if failure:value.update(state='blocked',code=failure)
        elif value['state']=='day_collection_finished' and not value['complete']:value['state']='collection_incomplete'
        if failure in ('provider_error','invalid_message'):private_json(root/'fatal.json',dict(at=clock().isoformat(),code=failure))
        if failure=='receipt_write_failed':private_json(root/'receipt_error.json',dict(at=clock().isoformat(),code=failure))
        flags=os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
        with os.fdopen(os.open(root/'collector_closeouts.jsonl',flags,0o600),'a') as out:out.write(json.dumps(value)+'\n');out.flush();os.fsync(out.fileno())
        private_json(root/'collector_closeout.json',value);private_json(root/'collector_status.json',value)
        return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True);parser.add_argument('--source',type=Path,required=True);parser.add_argument('--sessions',action='store_true');parser.add_argument('--phase',choices=['capture','consume','replay','forecast'],required=True);args=parser.parse_args();os.umask(0o077)
    stop=threading.Event();signal.signal(signal.SIGTERM,lambda *_:stop.set())
    try:
        if args.phase=='capture':result=capture(args.root,args.source,stop=stop,session_mode=args.sessions)
        elif args.phase=='forecast':result=forecast_session(args.root,args.source,session_mode=args.sessions)
        else:result=consume(args.root,args.source,replay=args.phase=='replay',session_mode=args.sessions)
        print(json.dumps(result),flush=True);code=int(result.get('state')=='blocked')
    except Exception as error:
        private_json(args.root/'receipt_error.json',dict(at=now().isoformat(),phase=args.phase,**error_details(error)))
        print(json.dumps(dict(state='error',code='receipt_operation_failed')),flush=True);code=1
    # Native SDK helper threads must not outlive the bounded, durably closed worker.
    if args.phase=='capture':os._exit(code)
    return code


if __name__=='__main__':raise SystemExit(main())
