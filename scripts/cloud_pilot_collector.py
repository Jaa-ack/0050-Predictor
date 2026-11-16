"""Time-sliced acquisition around an unchanged, fingerprinted pilot store/model."""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
from pathlib import Path
import queue
import threading
import time

from twquant import live_market as reference


def collect(root, store, *, clock=reference.now, monotonic=time.monotonic,
            sleep=time.sleep, feed_factory=reference.FubonFeed):
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
        def receive(message):
            try:inbox.put_nowait((message,clock()))
            except queue.Full:overflow.set()
        feed=None;retries=0;connected_start=None;previous=clock();last_mono=monotonic()
        last_flush=last_mono;last_state=0.
        try:
            while clock()<end:
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
                    try:store.ingest(message,received_at=received)
                    except (ValueError,TypeError,KeyError):errors.put('invalid_message')
                if overflow.is_set():store.mark_gap(current,clock(),'stalled');overflow.clear()
                while not errors.empty():
                    code=errors.get_nowait()
                    store.mark_gap(reference.iso(feed.last_received_ns) if feed and feed.last_received_ns else current,clock(),'disconnected')
                    if code in ('provider_error','invalid_message'):
                        reference.private_json(root/'fatal.json',dict(code=code,at=clock().isoformat()))
                        return dict(state='blocked',code=code)
                    if feed:feed.close()
                    feed=None;connected_start=None;retries+=1
                    if retries>5:return dict(state='connection_retries_exhausted')
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
                    reference.private_json(root/'collector_status.json',dict(state='collecting',date=day,at=current.isoformat(),
                        subscriptions=len(feed.subscriptions) if feed else 0,required_subscriptions=len(feed.expected) if feed else 0,
                        backlog=inbox.qsize(),stream_ready=bool(feed and feed.ready),runtime='cloud_timesliced_v1'))
                    last_state=mono
                sleep(.02)
            store.flush();return dict(state='day_collection_finished',date=day)
        finally:
            if feed:feed.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True,type=Path)
    args=parser.parse_args(argv);root=args.root.resolve();store=None
    try:
        protocol=json.loads((root/'protocol.json').read_text());store=reference.PilotStore(root,protocol)
        result=collect(root,store);reference.private_json(root/'last_collect.json',result)
        print(json.dumps(result));return 0
    except Exception:
        print(json.dumps(dict(state='error',code='pilot_operation_failed')));return 1
    finally:
        if store:store.close()


if __name__=='__main__':raise SystemExit(main())
