"""Compare a one-second publication reserve with the frozen direct trial rule."""
import argparse
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3

if __package__:
    from . import opening_trial_rule as rule
else:
    import opening_trial_rule as rule

RECIPE = 'direct_trial_receipt_strictly_before_085859'


def compare_day(path, day, symbols):
    if not path.is_file():
        return [dict(ticker=s, direct=None, one_second_early=None, direct_fallback=None, early_fallback=None)
                for s in symbols]
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        direct = rule.directions(db, day, symbols)
        stop = rule.clock_us(day, rule.DECISION) - 1_000_000
        # Views live on this single read-only connection. Keep the frozen exchange-time window intact.
        for name in ('trials', 'quotes'):
            db.execute(f'CREATE TEMP VIEW {name} AS SELECT * FROM main.{name} WHERE received_us < {stop}')
        early = rule.directions(db, day, symbols)
    return [dict(ticker=a['ticker'], direct=a['up'], one_second_early=b['up'],
                 direct_fallback=a['fallback'], early_fallback=b['fallback'])
            for a, b in zip(direct, early, strict=True)]


def score(sessions, symbols, observed, prices):
    import pandas as pd
    from twquant.opening_gap import _metrics, _paired_hits
    from twquant.universe import TRAINING_TICKERS

    if len(sessions) < 2 or list(sessions) != sorted(set(sessions)):
        raise ValueError('Provide consecutive trading dates beginning with the previous session')
    samples = []
    for previous, day in zip(sessions[:-1], sessions[1:], strict=True):
        rows = observed.get(str(day), [])
        by_ticker = {r['ticker']: r for r in rows}
        if len(by_ticker) != len(rows) or set(by_ticker) - set(symbols):
            raise ValueError('Duplicate or unknown comparison ticker')
        for s in symbols:
            a = prices.get(previous, {}).get(s, (None, None))[1]
            b = prices.get(day, {}).get(s, (None, None))[0]
            row = by_ticker.get(s, {})
            values = [row.get(k) for k in ('direct', 'one_second_early', 'direct_fallback', 'early_fallback')]
            if not (all(v is None for v in values) or all(type(v) is bool for v in values)):
                raise ValueError('Invalid binary comparison row')
            samples.append(dict(ticker=s, actual_return=b/a-1 if a and b else None,
                                direct=values[0], early=values[1], direct_fallback=values[2], early_fallback=values[3]))
    frame = pd.DataFrame(samples)
    reports = {}
    for name, universe in [('fixed_ten', TRAINING_TICKERS), ('constituents', symbols)]:
        rows = frame.loc[frame.ticker.isin(universe)]
        baseline, early = rows.direct.astype(float), rows.early.astype(float)
        forecast = rows.assign(probability_up=early)
        reports[name] = dict(direct=_metrics(rows.assign(probability_up=baseline), universe),
                             early=_metrics(forecast, universe), paired=_paired_hits(forecast, baseline),
                             changes=int((baseline.notna() & early.notna() & baseline.ne(early)).sum()),
                             direct_fallbacks=int(rows.direct_fallback.eq(True).sum()),
                             early_fallbacks=int(rows.early_fallback.eq(True).sum()))
    return dict(recipe=RECIPE, evidence_kind='historical_receipt_replay', sessions=[str(d) for d in sessions[1:]],
                scopes=reports)


def main():
    if __package__:
        from .audit_opening_receipts import read_bars
    else:
        from audit_opening_receipts import read_bars
    from twquant.direction_audit import load_membership_snapshot

    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--archives', type=Path)
    group.add_argument('--pairs', type=Path)
    parser.add_argument('--bars', type=Path, required=True)
    parser.add_argument('--membership', type=Path, required=True)
    parser.add_argument('--sessions', type=date.fromisoformat, nargs='+', required=True)
    args = parser.parse_args()
    symbols = load_membership_snapshot(args.membership)['tickers']
    if len(symbols) != 50:
        parser.error('Expected fifty constituents')
    if args.pairs:
        data = json.loads(args.pairs.read_text())
        if data['recipe'] != RECIPE:
            parser.error('Unexpected comparison recipe')
        observed = data['observed']
    else:
        observed = {str(d): compare_day(args.archives / f'{d}.sqlite', d, symbols) for d in args.sessions[1:]}
    prices = {d: read_bars(args.bars / f'{d:%Y%m%d}.json', d) for d in args.sessions}
    print(json.dumps(score(args.sessions, symbols, observed, prices), allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
