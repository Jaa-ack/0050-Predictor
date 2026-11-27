"""Replay ex-rights auction bases against the established raw-close direction forecast."""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
from tempfile import mkdtemp

import numpy as np
import pandas as pd
import requests

from twquant import opening_gap as gap
from twquant.direction_audit import load_membership_snapshot
from twquant.universe import DEFAULT_EVALUATION_TICKERS, TRAINING_TICKERS

RESULT_URL = 'https://www.twse.com.tw/rwd/zh/exRight/TWT49U'


def fetch(folder, start, end):
    if end < start:
        raise ValueError('Reversed TWSE request range')
    params = dict(startDate=start.strftime('%Y%m%d'), endDate=end.strftime('%Y%m%d'), response='json')
    response = requests.get(RESULT_URL, params=params, timeout=(8, 40))
    raw = response.content
    received = datetime.now(timezone.utc).isoformat()
    response.raise_for_status()
    payload = json.loads(raw)
    if payload.get('stat') == 'OK' and (payload.get('strDate'), payload.get('endDate')) != (
            params['startDate'], params['endDate']):
        raise ValueError('Unexpected TWSE response range')
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = Path(mkdtemp(dir=folder, prefix='ex-rights-')) / 'results.json'
    path.write_bytes(raw)
    path.with_suffix('.meta.json').write_text(json.dumps(
        dict(url=RESULT_URL, params=params, received_at=received, sha256=gap._sha(path))))
    return path


def parse(payload):
    if payload['stat'] != 'OK':
        raise ValueError('Unexpected TWSE result status')
    columns = {'資料日期':'date', '股票代號':'ticker', '除權息前收盤價':'exchange_prior',
               '開盤競價基準':'auction_basis', '漲停價格':'ceiling'}
    rows = pd.DataFrame(payload['data'], columns=payload['fields'])[list(columns)].rename(columns=columns)
    dates = rows.date.str.extract(r'^(\d+)年(\d+)月(\d+)日$')
    rows['date'] = pd.to_datetime(dict(year=dates[0].astype(int)+1911, month=dates[1], day=dates[2]))
    interval = [pd.to_datetime(payload[k], format='%Y%m%d') for k in ('strDate','endDate')]
    if interval[1] < interval[0] or not rows.date.between(*interval).all():
        raise ValueError('Event date outside TWSE response range')
    for field in ('exchange_prior','auction_basis','ceiling'):
        text = rows[field].astype('string').str.replace(',', '', regex=False).str.strip()
        rows[field] = pd.to_numeric(text.replace(['', '--', '---', '不設限'], pd.NA)).astype(float)
        if not (rows[field].isna() | (rows[field].gt(0) & np.isfinite(rows[field]))).all():
            raise ValueError('Expected a positive TWSE reference price')
    if rows.duplicated(['date','ticker']).any():
        raise ValueError('Ambiguous TWSE ticker and effective date')
    return rows


def directions(rows, strict):
    base = rows.probability_up.ge(.5).astype(float).where(rows.probability_up.between(0, 1))
    match = np.isclose(rows.reference_close, rows.exchange_prior, atol=1e-6, rtol=0)
    eligible = base.notna() & match
    if strict:
        eligible &= rows.received_at.lt(rows.decision_ts)
    variants = {'original':base, 'ceiling_at_prior':base.mask(eligible & rows.ceiling.le(rows.reference_close), 0.)}
    for threshold in (1, 2, 5):
        lower = rows.auction_basis * 100 <= rows.reference_close * (100 - threshold) + 1e-8
        variants[f'decline_{threshold}pct'] = base.mask(eligible & lower, 0.)
    return pd.DataFrame(variants, index=rows.index)


def compare(rows, calibration_dates, holdout_dates, symbols):
    def scores(part, tickers, strict):
        variants = directions(part, strict)
        result = {}
        for name, predictions in variants.items():
            policy = part.assign(probability_up=predictions)
            result[name] = dict(score=gap._metrics(policy, tickers), paired=gap._paired_hits(policy, variants.original))
        return result

    core = list(TRAINING_TICKERS)
    calibration = rows.loc[rows.date.isin(calibration_dates) & rows.ticker.isin(core)].copy()
    cutoff = holdout_dates[0].tz_localize(gap.LOCAL) + gap.DECISION
    calibration['actual_return'] = calibration.actual_return.where(calibration.label_available_at.lt(cutoff))
    cal = scores(calibration, core, False)
    selected = max(cal, key=lambda name:gap._factor_rank(cal[name]['score']))
    final = rows.loc[rows.date.isin(holdout_dates)]
    groups = {'fixed_ten':core, 'constituents':symbols}
    groups.update({s:[s] for s in DEFAULT_EVALUATION_TICKERS if s in set(final.ticker)})
    report = dict(evidence_kind='historical_reconstruction', timely_issued_sessions=0, selected=selected,
                  training_tickers=core, calibration=cal)
    for key, strict in [('holdout', False), ('recorded_receipts', True)]:
        report[key] = {group:scores(final.loc[final.ticker.isin(tickers)], tickers, strict)
                       for group,tickers in groups.items()}
    for label, part in [('calibration_events',calibration), ('holdout_events',final.loc[final.ticker.isin(symbols)])]:
        events = part.loc[part.received_at.notna()]
        match = np.isclose(events.reference_close, events.exchange_prior, atol=1e-6, rtol=0)
        policies = directions(events, False)
        changes = events.loc[policies.original.notna() & policies[selected].ne(policies.original)]
        report[label] = dict(rows=len(events), dates=events.date.nunique(), matches=int(match.sum()),
                             predecision_receipts=int(events.received_at.lt(events.decision_ts).sum()),
                             effective_dates=sorted({d.date().isoformat() for d in events.date}),
                             changed_stocks=changes.ticker.nunique(), changed_dates=changes.date.nunique())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    download = commands.add_parser('fetch')
    for name in ('start', 'end'):
        download.add_argument('--'+name, type=date.fromisoformat, required=True)
    download.add_argument('--out', type=Path, required=True)
    replay = commands.add_parser('score')
    for name in ('calendar','bars','predictions','membership'):
        replay.add_argument('--'+name, type=Path, required=True)
    replay.add_argument('--tables', type=Path, nargs='+', required=True)
    args = parser.parse_args()
    if args.command == 'fetch':
        print(fetch(args.out, args.start, args.end))
        return
    symbols = load_membership_snapshot(args.membership)['tickers']
    if len(symbols) != 50 or not set(TRAINING_TICKERS) <= set(symbols):
        raise ValueError('Expected all 50 constituents and the fixed training universe')
    targets = list(dict.fromkeys([*symbols, *DEFAULT_EVALUATION_TICKERS]))
    cols = ['date','ticker','phase','probability_up','actual_return','decision_ts']
    predictions = pd.read_parquet(args.predictions, columns=cols)
    calendar = gap._sessions(pd.read_csv(args.calendar, usecols=['date']).date)
    final = predictions.loc[predictions.phase.eq('final'), 'date']
    holdout = calendar[(calendar >= final.min()) & (calendar <= final.max())]
    previous = calendar[calendar < holdout[0]][-127:]
    if len(holdout) != 126 or len(previous) != 127:
        raise ValueError('Expected the established 126-day calibration and holdout windows')
    dates = previous.append(holdout)
    bars = pd.read_parquet(args.bars, columns=gap.REQUIRED_COLUMNS)
    price = gap.build_opening_gap_panel(bars, dates, targets)
    rows = price[['date','ticker','decision_ts','reference_close','actual_return','label_available_at']].merge(
        predictions.drop(columns='phase'), on=['date','ticker','decision_ts'], how='left', suffixes=('','_archive'),
        validate='one_to_one', indicator=True)
    present = rows._merge.eq('both')
    if not np.allclose(rows.loc[present,'actual_return'], rows.loc[present,'actual_return_archive'],
                       rtol=0, atol=1e-10, equal_nan=True):
        raise ValueError('Archived labels disagree with the raw opening and prior close')
    observed, covered, sources = [], set(), []
    for path in args.tables:
        meta = json.loads(path.with_suffix('.meta.json').read_text())
        stamp = pd.Timestamp(meta['received_at'])
        if stamp.tzinfo is None or pd.isna(stamp) or gap._sha(path) != meta['sha256']:
            raise ValueError('Invalid receipt time or stored table digest')
        payload = json.loads(path.read_bytes())
        if payload.get('stat') != 'OK':
            sources.append(dict(sha256=meta['sha256'], received_at=stamp.isoformat(), status='unconfirmed'))
            continue
        table = parse(payload)
        start, end = (pd.to_datetime(payload[k], format='%Y%m%d') for k in ('strDate','endDate'))
        period = set(dates[(dates >= start) & (dates <= end)])
        if covered.intersection(period):
            raise ValueError('Overlapping TWSE results')
        covered.update(period)
        observed.append(table.assign(received_at=stamp))
        sources.append(dict(sha256=meta['sha256'], received_at=stamp.isoformat(), start=str(start.date()), end=str(end.date())))
    if observed:
        rows = rows.merge(pd.concat(observed, ignore_index=True), on=['date','ticker'], how='left', validate='one_to_one')
    else:
        for field in ('exchange_prior','auction_basis','ceiling'):
            rows[field] = np.nan
        rows['received_at'] = pd.Series(pd.NaT, index=rows.index, dtype='datetime64[ns, UTC]')
    result = compare(rows, previous[1:], holdout, symbols)
    result.update(source_tables=sources, missing_result_dates=[str(d.date()) for d in dates[1:] if d not in covered],
                  inputs={k:gap._sha(getattr(args,k)) for k in ('calendar','bars','predictions','membership')})
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
