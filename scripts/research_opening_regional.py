"""Measure the historical incremental value of the Japanese open and prior Taiwan FX close."""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from tempfile import mkdtemp

import numpy as np
import pandas as pd
import requests
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from twquant.direction_audit import load_membership_snapshot
from twquant.opening_gap import DECISION, LOCAL, _factor_rank, _metrics, _paired_hits, _sha
from twquant.universe import DEFAULT_EVALUATION_TICKERS, TRAINING_TICKERS

SOURCES = {
    'japan': ('https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_en.csv', 'japan.csv'),
    'currency': ('https://cpx.cbc.gov.tw/api/OpenData/FTDOpenData_Day', 'currency.json'),
}


def download(folder, name):
    url, filename = SOURCES[name]
    response = requests.get(url, timeout=(8, 30))
    raw = response.content
    received = datetime.now(timezone.utc).isoformat()
    response.raise_for_status()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    batch = Path(mkdtemp(dir=folder, prefix=name+'-'))
    path = batch/filename
    path.write_bytes(raw)
    path.with_suffix('.meta.json').write_text(json.dumps(
        dict(url=url, received_at=received, sha256=hashlib.sha256(raw).hexdigest())))
    return path


def regional_returns(sessions, japan, currency):
    """Japan uses its own preceding close. FX requires the preceding Taiwan session."""
    for index in (sessions, japan.index, currency.index):
        if not index.is_monotonic_increasing or not index.is_unique:
            raise ValueError('Expected distinct ordered market dates')
    japanese_gap = japan.Open / japan.Close.shift() - 1
    fx_change = currency.pct_change(fill_method=None)
    return pd.DataFrame({'japan':japanese_gap.reindex(sessions),
                         'fx':fx_change.reindex(sessions).shift()})


def compare(frame, features, final_dates, symbols, calibration_days=126):
    core = list(TRAINING_TICKERS)
    if not set(core) <= set(symbols) or len(set(symbols)) != len(symbols):
        raise ValueError('Expected distinct constituents including the fixed ten')
    dates = features.index
    start = features.japan.first_valid_index()
    if start is None or not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError('Expected ordered calendar and historical Japanese opens')
    history = dates[(dates >= start) & (dates < final_dates[0])]
    if not len(history) > calibration_days > 0 or not dates.intersection(final_dates).equals(final_dates):
        raise ValueError('Insufficient fitting or calibration sessions, or invalid holdout')
    cal = history[-calibration_days:]
    targets = list(dict.fromkeys([*symbols, *DEFAULT_EVALUATION_TICKERS]))
    index = pd.MultiIndex.from_product([history.append(final_dates), targets], names=['date','ticker'])
    source = frame.set_index(['date','ticker'])
    if not source.index.is_unique:
        raise ValueError('Duplicate historical ticker and session')
    rows = source.reindex(index).reset_index()
    rows = rows.join(features, on='date')
    decision = rows.date.dt.tz_localize(LOCAL) + DECISION
    if (rows.decision_ts.notna() & rows.decision_ts.ne(decision)).any():
        raise ValueError('Unexpected historical decision time')
    rows['raw'] = rows.control_context.where(rows.control_context.between(0, 1) & rows.decision_ts.notna())
    p = rows.raw.clip(1e-6, 1-1e-6)
    rows['base_logit'] = np.log(p/(1-p))
    # A missing market return supplies zero directional evidence, including on Japanese holidays.
    inputs = rows[['base_logit', 'japan', 'fx']].fillna(0.)
    members = rows.ticker.isin(core)
    train = (members & rows.date.lt(cal[0]) & rows.raw.notna() & rows.actual_return.notna()
             & rows.label_available_at.lt(cal[0].tz_localize(LOCAL) + DECISION))
    policies = {'calibrated':[], 'with_japan':['japan'], 'with_fx':['fx'], 'with_both':['japan','fx']}
    for name, factors in policies.items():
        columns = ['base_logit', *factors]
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1., max_iter=300))
        model.fit(inputs.loc[train, columns], rows.loc[train, 'actual_return'].gt(0))
        eligible = rows.raw.notna()
        rows.loc[eligible, name] = model.predict_proba(inputs.loc[eligible, columns])[:, 1]

    def score(part, tickers):
        results = {}
        for name in ('raw', *policies):
            pred = part.assign(probability_up=part[name])
            results[name] = dict(score=_metrics(pred, tickers), paired_to_raw=_paired_hits(pred, part.raw),
                                 paired_to_calibrated=_paired_hits(pred, part.calibrated))
        return results

    calibration = rows.loc[members & rows.date.isin(cal)].copy()
    calibration['actual_return'] = calibration.actual_return.where(
        calibration.label_available_at.lt(final_dates[0].tz_localize(LOCAL) + DECISION))
    scores = score(calibration, core)
    selected = max(scores, key=lambda k:_factor_rank(scores[k]['score']))
    final = rows.loc[rows.date.isin(final_dates)]
    groups = {'fixed_ten':core, 'constituents':symbols, **{s:[s] for s in DEFAULT_EVALUATION_TICKERS}}
    return dict(evidence_kind='historical_reconstruction', timely_issued_sessions=0, training_tickers=core,
                training_dates=rows.loc[train, 'date'].nunique(), training_rows=int(train.sum()),
                calibration_dates=calibration_days, selected=selected, calibration=scores,
                missing_source_dates={k:[d.date().isoformat() for d in final_dates[features.loc[final_dates,k].isna()]]
                                      for k in ('japan','fx')},
                holdout={name:score(final.loc[final.ticker.isin(group)], group) for name,group in groups.items()})


def snapshot(path):
    meta = json.loads(path.with_suffix('.meta.json').read_text())
    stamp = pd.Timestamp(meta['received_at'])
    raw = path.read_bytes()
    if stamp.tzinfo is None or pd.isna(stamp) or hashlib.sha256(raw).hexdigest() != meta['sha256']:
        raise ValueError('Invalid private source fingerprint or receipt clock')
    return raw, dict(sha256=meta['sha256'], received_at=stamp.isoformat())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('fetch', help='Take private snapshots of both official datasets')
    fetch.add_argument('--out', type=Path, required=True)
    replay = commands.add_parser('score', help='Run the historical matched-grid study')
    for name in ('predictions','calendar','japan','currency','membership'):
        replay.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'fetch':
        for name in SOURCES:
            print(download(args.out, name))
        return
    raw_japan, japan_meta = snapshot(args.japan)
    japan = pd.read_csv(io.BytesIO(raw_japan), encoding='cp932', usecols=['Date of Data','Open','Close'])
    # The official English CSV ends with a single copyright notice.
    notice = japan['Date of Data'].str.startswith('This material is a copyrightable work of Nikkei.')
    japan = japan.loc[~notice].set_index('Date of Data')
    japan.index = pd.to_datetime(japan.index, format='%Y/%m/%d')
    raw_fx, fx_meta = snapshot(args.currency)
    currency = pd.DataFrame(json.loads(raw_fx)).set_index('日期').NTD_USD.astype(float)
    currency.index = pd.to_datetime(currency.index, format='%Y%m%d')
    for series in (japan.Open, japan.Close, currency):
        if not (np.isfinite(series) & series.gt(0)).all():
            raise ValueError('Invalid official market prices')
    sessions = pd.DatetimeIndex(pd.read_csv(args.calendar, usecols=['date'], parse_dates=['date']).date)
    features = regional_returns(sessions, japan, currency)
    columns = ['date','ticker','phase','actual_return','label_available_at','decision_ts','control_context']
    predictions = pd.read_parquet(args.predictions, columns=columns)
    final = predictions.loc[predictions.phase.eq('final'), 'date']
    final_dates = sessions[(sessions >= final.min()) & (sessions <= final.max())]
    if len(final_dates) != 126:
        raise ValueError('Expected the established 126-session historical holdout')
    members = load_membership_snapshot(args.membership)['tickers']
    if len(members) != 50:
        raise ValueError('Expected all 50 constituents')
    report = compare(predictions, features, final_dates, members)
    report.update(inputs=dict(japan=japan_meta, currency=fx_meta, predictions=_sha(args.predictions),
                              calendar=_sha(args.calendar), membership=_sha(args.membership)),
                  holdout_dates=[d.date().isoformat() for d in final_dates])
    print(json.dumps(report, allow_nan=False, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
