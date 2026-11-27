"""Replay private opening captures against official unadjusted TWSE daily prices."""
import argparse
from datetime import date
import json
import math
from pathlib import Path

import pandas as pd

from twquant.direction_audit import load_membership_snapshot
from twquant.opening_capture import CUTOFF, WINDOW_START, session_us
from twquant.opening_compare import EVIDENCE, TASK
from twquant.opening_gap import _metrics, _paired_hits


def read_bars(path, day):
    if not path.is_file():
        return {}
    return official_bars(json.loads(path.read_text())['data'], day)


def official_bars(payload, day):
    if payload['stat'] != 'OK' or payload.get('date') != day.strftime('%Y%m%d'):
        raise ValueError('Unexpected official market date or status')
    columns = ('證券代號', '開盤價', '收盤價')
    table, = [t for t in payload['tables'] if set(columns) <= set(t.get('fields', []))]
    ticker, opening, close = [table['fields'].index(k) for k in columns]

    def price(value):
        if value in ('--', '---', '', None):
            return None
        number = float(str(value).replace(',', ''))
        return number if math.isfinite(number) and number > 0 else None

    bars = {}
    for row in table['data']:
        symbol = row[ticker]
        if symbol in bars:
            raise ValueError('Duplicate symbol in daily market response')
        bars[symbol] = (price(row[opening]), price(row[close]))
    return bars


def by_ticker(rows):
    keyed = {row['ticker']: row for row in rows}
    if len(keyed) != len(rows):
        raise ValueError('Duplicate capture or prediction for a symbol')
    return keyed


def score(days, symbols, observations, bars, comparisons):
    if len(days) < 2 or list(days) != sorted(set(days)) or not symbols or len(symbols) != len(set(symbols)):
        raise ValueError('Provide the preceding trading date and ordered scoring sessions with distinct symbols')
    samples = []
    missing_days = []
    for previous, current in zip(days[:-1], days[1:], strict=True):
        day = current.isoformat()
        observed = by_ticker(observations.get(day, []))
        if not observed:
            missing_days.append(day)
        comp = comparisons.get(day, {})
        if comp and (comp.get('validation_date') != day or comp.get('evidence_kind') != EVIDENCE
                     or comp.get('task') != TASK):
            raise ValueError('Unexpected comparison evidence or session')
        original = by_ticker(comp.get('original', {}).get('validation_rows', []))
        repair = by_ticker(comp.get('repair', {}).get('rows', []))
        cutoff = session_us(current, CUTOFF)
        start = session_us(current, WINDOW_START)
        for ticker in symbols:
            row = observed.get(ticker, {})
            previous_close = bars.get(previous, {}).get(ticker, (None, None))[1]
            opening = bars.get(current, {}).get(ticker, (None, None))[0]
            actual = opening / previous_close - 1 if opening and previous_close else None
            exchange, received = row.get('trial_exchange_us'), row.get('trial_received_us')
            gap = row.get('trial_gap')
            valid = (type(exchange) is int and type(received) is int and start <= exchange <= received < cutoff
                     and gap is not None and math.isfinite(gap))
            # The direct rule uses the reference actually received, even if later official bars disagree.
            trial = bool(valid and gap > 0) if row else None
            samples.append(dict(ticker=ticker, date=day, actual_return=actual, timed_input=valid,
                                provider_mismatch=actual is not None and row.get('actual_up') is not None
                                and (actual > 0) != row['actual_up'], direct_trial=trial,
                                original=original.get(ticker, {}).get('predicted_up'),
                                repair=repair.get(ticker, {}).get('predicted_up')))
    frame = pd.DataFrame(samples)
    models = {}
    for name in ('direct_trial', 'original', 'repair'):
        if any(v is not None and type(v) is not bool for v in (row[name] for row in samples)):
            raise ValueError('Binary replay directions required')
        policy = frame.assign(probability_up=frame[name].astype(float))
        models[name] = dict(_metrics(policy, symbols), paired_to_trial=_paired_hits(policy, frame.direct_trial.astype(float)))
    return dict(task=TASK, evidence='auction_receipt_replay_with_official_labels',
                sessions=[d.isoformat() for d in days[1:]], missing_capture_dates=missing_days,
                timed_trial_inputs=int(frame.timed_input.sum()), provider_direction_mismatches=int(frame.provider_mismatch.sum()),
                models=models, timely_issued_sessions=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=date.fromisoformat, nargs='+', required=True,
                        help='Consecutive exchange sessions, starting with the prior-close date')
    for name in ('membership', 'observations', 'bars', 'comparisons'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    args = parser.parse_args()
    symbols = load_membership_snapshot(args.membership)['tickers']
    if len(symbols) != 50:
        parser.error('The opening protocol requires exactly 50 constituents')
    comparisons = {}
    for day in args.sessions[1:]:
        path = args.comparisons / f'comparison-{day}.json'
        if path.is_file():
            comparisons[str(day)] = json.loads(path.read_text())
    bars = {day: read_bars(args.bars / f'{day:%Y%m%d}.json', day) for day in args.sessions}
    result = score(args.sessions, symbols, json.loads(args.observations.read_text()), bars, comparisons)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
