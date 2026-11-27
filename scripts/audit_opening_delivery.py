"""Score due, frozen 08:59 sessions from private inbox receipts and official raw daily prices."""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import time

import pandas as pd

if __package__:
    from . import opening_delivery as delivery
    from .audit_opening_receipts import read_bars
else:
    import opening_delivery as delivery
    from audit_opening_receipts import read_bars

from twquant.opening_gap import _metrics, _paired_hits


def _validate(batch, sha, day, due, symbols):
    if (batch['protocol'] != sha or batch['session'] != str(day) or batch['cutoff_ns'] != due
            or batch['evidence_kind'] != 'private_local_delivery'
            or not due - 60_000_000_000 <= batch['armed_ns'] < due <= batch['sent_ns'] <= batch['observed_ns']):
        raise ValueError('Invalid issuance identity or clocks')
    rows = batch['rows']
    if ([r['ticker'] for r in rows] != list(symbols) or any(type(r['up']) is not bool
            or type(r['fallback']) is not bool or (r['fallback'] and r['up']) for r in rows)):
        raise ValueError('Invalid frozen prediction grid')
    for row in rows:
        if not row['fallback']:
            ref, trial, received = (row[k] for k in ('reference_received_us', 'trial_exchange_us', 'trial_received_us'))
            if (not all(type(x) is int for x in (ref, trial, received))
                    or not (0 < ref < due // 1000 and due // 1000 - 30_000_000 <= trial <= received < due // 1000)):
                raise ValueError('Prediction includes an ineligible market observation')


def assess(protocol, sha, prior, prices, batches, as_of_ns):
    symbols = protocol['tickers']
    dates = [date.fromisoformat(s) for s in protocol['sessions']]
    if prior >= dates[0]:
        raise ValueError('Expected the trading session preceding the frozen calendar')
    samples = []
    late, missing, future, delays = [], [], [], {}
    timely_sessions = 0
    for day in dates:
        iso = str(day)
        due = delivery.rule.clock_us(day, delivery.rule.DECISION) * 1000
        if as_of_ns < due:
            future.append(iso)
            continue
        batch = batches.get(iso)
        timely = False
        if batch is not None:
            _validate(batch, sha, day, due, symbols)
        if batch is None or batch['observed_ns'] > as_of_ns:
            missing.append(iso)
        else:
            delay = batch['observed_ns'] - due
            delays[iso] = delay
            timely = delay <= 0
            if timely:
                timely_sessions += 1
            else:
                late.append(iso)
        for i, ticker in enumerate(symbols):
            opening = prices.get(day, {}).get(ticker, (None, None))[0]
            prev_close = prices.get(prior, {}).get(ticker, (None, None))[1]
            actual = opening / prev_close - 1 if opening and prev_close else None
            probability = float(batch['rows'][i]['up']) if timely else float('nan')
            samples.append(dict(ticker=ticker, actual_return=actual, probability_up=probability))
        prior = day
    score = paired = None
    if samples:
        frame = pd.DataFrame(samples)
        score = _metrics(frame, symbols)
        paired = _paired_hits(frame, pd.Series(0., index=frame.index))
    return dict(task=protocol['task'], protocol=sha, evidence_kind='private_local_delivery',
                as_of=datetime.fromtimestamp(as_of_ns / 1e9, timezone.utc).isoformat(), scheduled_sessions=len(dates),
                due_sessions=len(dates)-len(future), timely_sessions=timely_sessions, missing_sessions=missing,
                late_sessions=late, pending_sessions=future, receipt_delay_ns=delays, score=score,
                paired_to_not_up=paired, accepted=bool(score and len(dates) >= 126 and timely_sessions == len(dates)
                and score['known'] == score['expected'] and score['minimum_stock_accuracy'] >= .75))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('bundle', 'ledger', 'bars'):
        parser.add_argument('--'+key, required=True, type=Path)
    parser.add_argument('--protocol-sha256', required=True)
    parser.add_argument('--previous-session', type=date.fromisoformat, required=True)
    parser.add_argument('--workbench', type=Path, help='Private opening capture directory for the latest score')
    args = parser.parse_args()
    protocol = delivery.rule.load(args.bundle)
    sha = delivery.rule.digest((args.bundle / 'protocol.json').read_bytes())
    if sha != args.protocol_sha256:
        parser.error('Wrong pinned opening protocol')
    now = time.time_ns()
    due = [date.fromisoformat(d) for d in protocol['sessions']
           if delivery.rule.clock_us(date.fromisoformat(d), delivery.rule.DECISION)*1000 <= now]
    prices = {d: read_bars(args.bars / f'{d:%Y%m%d}.json', d) for d in [args.previous_session, *due]}
    batches = {str(d): delivery.read_day(args.ledger, sha, str(d)) for d in due}
    report = assess(protocol, sha, args.previous_session, prices, batches, now)
    if args.workbench:
        from twquant.opening_compare import _save
        _save(args.workbench / 'delivery-audit.json', report)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
