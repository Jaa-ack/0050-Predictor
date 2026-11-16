"""Bounded PASSIVE WAL maintenance outside forecast and scoring windows."""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')


def ops():
    path=Path(__file__).with_name('watchdog.py')
    if not path.exists():path=Path(__file__).with_name('cloud_pilot_watchdog.py')
    spec=importlib.util.spec_from_file_location('checkpoint_ops',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def checkpoint(root,*,stamp=None,minimum_wal=64*1024**2):
    root=Path(root);stamp=(stamp or datetime.now(TZ)).astimezone(TZ)
    protocol=json.loads((root/'protocol.json').read_text());day=stamp.date().isoformat()
    if day not in protocol['dates']:return dict(state='outside_declared_dates')
    def at(key,default):return datetime.fromisoformat(day+'T'+protocol.get(key,default)).replace(tzinfo=TZ)
    if not at('collection_start','09:30:00')<=stamp<at('collection_end','10:15:00'):return dict(state='outside_capture')
    decision=at('prediction_time','10:00:00')
    if decision-timedelta(minutes=3)<=stamp<decision+timedelta(minutes=6):return dict(state='protected_window')
    helper=ops()
    def save(state,**values):
        result=dict(state=state,date=day,at=stamp.isoformat(),**values);helper.private_json(root/'checkpoint_status.json',result);return result
    if tuple(sqlite3.sqlite_version_info)<(3,51,3):return save('error',code='sqlite_version_unverified')
    database=root/'pilot.sqlite3';wal=root/'pilot.sqlite3-wal'
    if database.is_symlink() or wal.is_symlink():return save('error',code='unsafe_storage_path')
    try:size=wal.stat().st_size
    except FileNotFoundError:size=0
    if size<minimum_wal:return dict(state='below_threshold')
    with (root/'checkpoint.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='already_running')
        save('running',wal_bytes=size)
        try:
            with sqlite3.connect(database.resolve().as_uri()+'?mode=rw',uri=True,timeout=0) as db:
                db.execute('pragma wal_autocheckpoint=0');db.execute('pragma cache_size=-2048');db.execute('pragma synchronous=FULL')
                if db.execute('pragma journal_mode').fetchone()[0]!='wal':return save('error',code='wal_mode_required')
                busy,frames,copied=db.execute('pragma wal_checkpoint(PASSIVE)').fetchone()
            return save('complete' if not busy and frames==copied else 'partial',frames=frames,checkpointed_frames=copied,wal_bytes=size)
        except sqlite3.Error:return save('error',code='passive_checkpoint_failed')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,required=True);parser.add_argument('--interrupted',action='store_true');args=parser.parse_args();os.umask(0o077)
    try:
        if args.interrupted:
            h=ops();status=h.read_json(args.root/'checkpoint_status.json',{})
            if os.environ.get('SERVICE_RESULT','success')!='success' or status.get('state')=='running':
                h.private_json(args.root/'checkpoint_status.json',dict(state='error',date=datetime.now(TZ).date().isoformat(),at=datetime.now(TZ).isoformat(),code='checkpoint_interrupted'))
            return 0
        result=checkpoint(args.root);print(json.dumps(result));return int(result['state']=='error')
    except Exception:
        print(json.dumps(dict(state='error',code='checkpoint_operation_failed')));return 1


if __name__=='__main__':raise SystemExit(main())
