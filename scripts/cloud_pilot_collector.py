"""Time-sliced acquisition around an unchanged, fingerprinted pilot store/model."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import threading
import time

from twquant import live_market as reference


def collect(root, store, *, clock=reference.now, monotonic=time.monotonic,
            sleep=time.sleep, feed_factory=reference.FubonFeed, stop=None,
            drain_seconds=90, free_bytes=None):
    if not 0<drain_seconds<=90:raise ValueError('Invalid bounded drain budget')
    stop=stop or threading.Event()
    root=Path(root);protocol=store.protocol;local=clock();day=local.date().isoformat()
    if day not in protocol['dates']:return dict(state='outside_declared_dates')
    start=datetime.fromisoformat(day+'T'+protocol.get('collection_start','09:30:00')).replace(tzinfo=reference.TZ)
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=reference.TZ)
    if not start<=local<end:return dict(state='outside_collection_window')
    if (root/'fatal.json').exists():return dict(state='blocked_by_provider_error')
    with (root/'collector.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return dict(state='already_running')
        inbox=queue.Queue(maxsize=100000);errors=queue.Queue();overflow=threading.Event()
        accepting=True;receive_lock=threading.Lock();inflight=0
        counts=dict(received_messages=0,processed_messages=0,invalid_messages=0,dropped_messages=0)
        def receive(message):
            with receive_lock:
                received=clock()
                if not accepting or received>=end:return
                counts['received_messages']+=1
                try:inbox.put_nowait((message,received))
                except queue.Full:counts['dropped_messages']+=1;overflow.set()
        feed=None;retries=0;connected_start=None;previous=clock();last_mono=monotonic()
        last_flush=last_mono;last_state=0.
        def ingest(message,received):
            nonlocal inflight
            inflight=1
            try:store.ingest(message,received_at=received);counts['processed_messages']+=1
            except (ValueError,TypeError,KeyError):counts['invalid_messages']+=1;errors.put('invalid_message')
            inflight=0
        def status(state):
            return dict(state=state,date=day,at=clock().isoformat(),subscriptions=len(feed.subscriptions) if feed else 0,
                required_subscriptions=len(feed.expected) if feed else 0,backlog=inbox.qsize()+inflight,
                stream_ready=bool(feed and feed.ready),runtime='cloud_timesliced_v2',accepting=accepting,**counts)
        def finish(result):
            nonlocal accepting
            with receive_lock:accepting=False
            closed_at=clock();deadline=monotonic()+drain_seconds;drained=0
            available=free_bytes or (lambda:shutil.disk_usage(root).free)
            storage_ok=available()>=protocol.get('minimum_free_bytes',4*1024**3)
            reference.private_json(root/'collector_status.json',status('draining'))
            while storage_ok and not inbox.empty() and monotonic()<deadline:
                message,received=inbox.get_nowait();ingest(message,received);drained+=1
                if drained%512==0:
                    storage_ok=available()>=protocol.get('minimum_free_bytes',4*1024**3)
                    reference.private_json(root/'collector_status.json',status('draining'))
            remaining=inbox.qsize()+inflight
            if remaining:
                with inbox.mutex:oldest=inbox.queue[0][1] if inbox.queue else closed_at
                store.mark_gap(oldest,max(oldest,min(clock(),end)),'stalled')
            if counts['invalid_messages'] and not (root/'fatal.json').exists():
                reference.private_json(root/'fatal.json',dict(code='invalid_message',at=clock().isoformat()))
            store.flush()
            complete=not remaining and not counts['invalid_messages'] and not counts['dropped_messages']
            if result['state']=='day_collection_finished' and not complete:result=dict(result,state='collection_incomplete')
            receipt=dict(result,date=day,at=clock().isoformat(),capture_ended_at=closed_at.isoformat(),complete=complete,
                remaining_messages=remaining,drained_messages=drained,**counts)
            flags=os.O_WRONLY|os.O_APPEND|os.O_CREAT|getattr(os,'O_NOFOLLOW',0)
            with os.fdopen(os.open(root/'collector_closeouts.jsonl',flags,0o600),'a') as out:
                os.fchmod(out.fileno(),0o600);out.write(json.dumps(receipt)+'\n');out.flush();os.fsync(out.fileno())
            reference.private_json(root/'collector_closeout.json',receipt)
            reference.private_json(root/'collector_status.json',status(result['state']))
            return dict(result,complete=complete,remaining_messages=remaining)
        try:
            while clock()<end and not stop.is_set():
                current=clock();mono=monotonic()
                if reference.ns(current)-reference.ns(previous)>2*10**9:
                    store.mark_gap(previous,current,'stalled')
                if abs((reference.ns(current)-reference.ns(previous))/1e9-(mono-last_mono))>.25:
                    store.mark_gap(min(previous,current),max(previous,current),'clock_jump')
                previous=current;last_mono=mono
                if feed is None:
                    feed=feed_factory(store.symbols)
                    try:feed.connect(receive,errors.put)
                    except Exception:errors.put('transport_error')
                drained=0;budget=monotonic()+.25
                while drained<512 and monotonic()<budget:
                    try:message,received=inbox.get_nowait()
                    except queue.Empty:break
                    drained+=1
                    ingest(message,received)
                if overflow.is_set():store.mark_gap(current,clock(),'stalled');overflow.clear()
                while not errors.empty():
                    code=errors.get_nowait()
                    store.mark_gap(reference.iso(feed.last_received_ns) if feed and feed.last_received_ns else current,clock(),'disconnected')
                    if code in ('provider_error','invalid_message'):
                        reference.private_json(root/'fatal.json',dict(code=code,at=clock().isoformat()))
                        return finish(dict(state='blocked',code=code))
                    if feed:feed.close()
                    feed=None;connected_start=None;retries+=1
                    if retries>5:return finish(dict(state='connection_retries_exhausted'))
                    sleep(min(2**retries,30))
                    while not errors.empty():errors.get_nowait()
                current=clock();mono=monotonic()
                if feed and feed.ready and feed.last_received_ns>=reference.ns(current)-45*10**9:
                    if connected_start is None:connected_start=current;retries=0
                    if mono-last_flush>=1:
                        store.mark_coverage(connected_start,current);last_flush=mono
                elif feed and feed.last_received_ns and feed.last_received_ns<reference.ns(current)-45*10**9:
                    errors.put('transport_error')
                if mono-last_flush>=1:store.flush();last_flush=mono
                if mono-last_state>=5:
                    reference.private_json(root/'collector_status.json',status('collecting'))
                    last_state=mono
                sleep(.02)
            return finish(dict(state='stopped' if stop.is_set() and clock()<end else 'day_collection_finished',date=day))
        finally:
            if feed:feed.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True,type=Path)
    args=parser.parse_args(argv);root=args.root.resolve();store=None;stop=threading.Event()
    previous_handler=signal.signal(signal.SIGTERM,lambda *_:stop.set())
    try:
        protocol=json.loads((root/'protocol.json').read_text());store=reference.PilotStore(root,protocol)
        result=collect(root,store,stop=stop);reference.private_json(root/'last_collect.json',result)
        print(json.dumps(result));return 0
    except Exception:
        print(json.dumps(dict(state='error',code='pilot_operation_failed')));return 1
    finally:
        try:
            if store:store.close()
        finally:signal.signal(signal.SIGTERM,previous_handler)


if __name__=='__main__':raise SystemExit(main())
