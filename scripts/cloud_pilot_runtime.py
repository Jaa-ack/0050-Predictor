"""Small-host supervision outside the immutable prediction implementation."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')


def now():return datetime.now(TZ)


def report_ready(protocol,stamp):
    local=stamp.astimezone(TZ)
    if local.date().isoformat() not in protocol['dates']:return True
    return not ('09:58:00'<=local.strftime('%H:%M:%S')<'10:06:00')


def write_status(root,result):
    fd,name=tempfile.mkstemp(dir=root,prefix='cloud_collect_status.',suffix='.tmp')
    with os.fdopen(fd,'w') as out:
        json.dump(result,out,allow_nan=False);out.flush();os.fsync(out.fileno())
    os.replace(name,root/'cloud_collect_status.json')


def supervise(root,*,clock=now,sleep=time.sleep,launch=subprocess.Popen,free_bytes=None):
    root=Path(root).resolve();protocol=json.loads((root/'protocol.json').read_text())
    local=clock().astimezone(TZ);day=local.date().isoformat()
    start=datetime.fromisoformat(day+'T'+protocol.get('collection_start','08:45:00')).replace(tzinfo=TZ)
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','13:40:00')).replace(tzinfo=TZ)
    def finish(state,**fields):
        result=dict(state=state,at=clock().astimezone(TZ).isoformat(),**fields);write_status(root,result);return result
    if day not in protocol['dates'] or not start<=local<end:return finish('outside_collection_window')
    reserve=protocol.get('minimum_free_bytes',4*1024**3)
    available=free_bytes or (lambda:shutil.disk_usage(root).free)
    if available()<reserve:return finish('storage_reserve_reached')
    with (root/'cloud_collect.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return finish('collector_already_running')
        child=launch([sys.executable,'-m','twquant.live_market','--root',str(root),'--mode','collect'])
        reason='worker_exited'
        try:
            while child.poll() is None:
                if clock().astimezone(TZ)>=end:reason='planned_window_end';break
                if available()<reserve:reason='storage_reserve_reached';break
                sleep(1)
        finally:
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        return finish(reason,worker_returncode=child.poll())


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--phase',choices=('collect','report-ready'),required=True)
    args=parser.parse_args(argv)
    if args.phase=='report-ready':
        protocol=json.loads((args.root/'protocol.json').read_text())
        return 0 if report_ready(protocol,now()) else 1
    result=supervise(args.root);print(json.dumps(result));return 0


if __name__=='__main__':raise SystemExit(main())
