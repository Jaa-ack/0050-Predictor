"""Run one phase of the pinned prospective opening study on its private host."""
import argparse
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time

import requests

from twquant import opening_compare
from twquant.universe import TRAINING_TICKERS

if __package__:
    from . import opening_trial_rule as rule
    from .audit_opening_delivery import audit_cached
    from .audit_opening_receipts import official_bars
else:
    import opening_trial_rule as rule
    from audit_opening_delivery import audit_cached
    from audit_opening_receipts import official_bars

TWSE_BARS = 'https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX'


def acquire(mode, protocol, root, now):
    days = tuple(date.fromisoformat(s) for s in protocol['sessions'])
    return opening_compare.acquire_future(mode, root, days, protocol['tickers'], now)


def fetch_bars(folder, day):
    path = folder / f'{day:%Y%m%d}.json'
    if path.is_file():
        return True
    try:
        response = requests.get(TWSE_BARS, params=dict(date=day.strftime('%Y%m%d'), type='ALLBUT0999',
                                                        response='json'), timeout=(8, 20))
        received = datetime.now(timezone.utc).isoformat()
        response.raise_for_status()
        data = response.json()
        bars = official_bars(data, day)
        if not any(op or close for op, close in bars.values()):
            return False
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return False
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=folder, mode='w', encoding='utf-8', delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(dict(received_at=received, url=response.url, data=data), out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # A concurrent first receipt already won.
        finally:
            temporary.unlink(missing_ok=True)
    return True


def settle(protocol, sha, prior, root, clock_ns=time.time_ns):
    dates = [date.fromisoformat(s) for s in protocol['sessions']]
    if prior >= dates[0]:
        raise ValueError('Invalid prior session')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with opening_compare.capture._lock(root / 'opening-settle.lock') as locked:
        if not locked:
            raise RuntimeError('Settlement already running')
        now = datetime.fromtimestamp(clock_ns() // 10**9, rule.LOCAL)
        folder = root / 'twse-raw-check'
        unavailable = []
        for day in [prior, *dates]:
            if day > now.date() or (day == now.date() and now.time() < opening_compare.capture.EVALUATE_AT):
                continue
            if not fetch_bars(folder, day):
                unavailable.append(str(day))
        report = audit_cached(protocol, sha, prior, folder, root/'deliveries.sqlite', clock_ns())
        report['unavailable_bar_dates'] = unavailable
        opening_compare._save(root/'delivery-audit.json', report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('collect','recover','settle','status'))
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--protocol-sha256', required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--previous-session', type=date.fromisoformat)
    args = parser.parse_args(argv)
    if args.mode == 'settle' and args.previous_session is None:
        parser.error('Supply the trading session immediately preceding the frozen calendar')
    os.umask(0o077)
    try:
        bundle, root = args.bundle.resolve(), args.root.resolve()
        protocol = rule.load(bundle)
        sha = rule.digest((bundle/'protocol.json').read_bytes())
        if sha != args.protocol_sha256 or not set(TRAINING_TICKERS) <= set(protocol['tickers']):
            raise ValueError('Invalid opening protocol')
        now = opening_compare.capture._now()
        if args.mode == 'status':
            upcoming = [s for s in protocol['sessions'] if rule.clock_us(date.fromisoformat(s), rule.DECISION)
                        > int(now.timestamp()*1e6)]
            print(json.dumps(dict(protocol=sha, symbols=len(protocol['tickers']), sessions=len(protocol['sessions']),
                                  next_decision=upcoming[0] if upcoming else None)))
        elif args.mode == 'settle':
            report = settle(protocol, sha, args.previous_session, root)
            print(json.dumps({k:report[k] for k in ('due_sessions','timely_sessions','unavailable_bar_dates')}))
            return int(bool(report['unavailable_bar_dates']))
        else:
            return acquire(args.mode, protocol, root, now)
        return 0
    except Exception as exc:
        print(f'opening_forward_{args.mode}_failed:{type(exc).__name__}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
