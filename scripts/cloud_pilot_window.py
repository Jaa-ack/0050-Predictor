"""Lightweight maintenance guard; no SQLite, provider or model imports."""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

TZ=ZoneInfo('Asia/Taipei')


def protected(protocol,stamp):
    stamp=stamp.astimezone(TZ);day=stamp.date().isoformat()
    if day not in protocol['dates']:return False
    start=datetime.fromisoformat(day+'T'+protocol.get('collection_start','09:30:00')).replace(tzinfo=TZ)
    end=datetime.fromisoformat(day+'T'+protocol.get('collection_end','10:15:00')).replace(tzinfo=TZ)
    preflight=datetime.fromisoformat(day+'T08:38:00').replace(tzinfo=TZ)
    return min(preflight,start-timedelta(minutes=2))<=stamp<end+timedelta(seconds=150)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--phase',choices=['condition','quiet-condition'],required=True)
    args=parser.parse_args();value=protected(json.loads((args.root/'protocol.json').read_text()),datetime.now(TZ))
    return int(value if args.phase=='condition' else not value)


if __name__=='__main__':raise SystemExit(main())
