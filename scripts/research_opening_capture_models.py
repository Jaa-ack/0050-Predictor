"""Compare fixed-ten price, positioning and overnight futures models with actual auction receipts."""
import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from twquant import opening_gap as gap
from twquant.direction_audit import load_membership_snapshot
from twquant.features import FEATURE_VERSION
from twquant.taifex_history import parse_taifex_history, taifex_night_events
from twquant.universe import TRAINING_TICKERS
from twquant.workflows import data_kind
if __package__:
    from .audit_opening_lead import RECIPE
else:
    from audit_opening_lead import RECIPE

PRICE = ('f_intraday', 'f_range', 'f_close_location')
FLOW = ('f_flow_foreign_imbalance', 'f_flow_trust_imbalance')
INPUTS = (*PRICE, *FLOW, *gap.NIGHT)
FAMILIES = ('context', 'source_fusion', 'context_tree')
CLOCKS = ('actual_receipts', 'historical_reconstruction')


def visible(rows, received, decision, mode):
    if mode not in CLOCKS:
        raise ValueError('Unsupported receipt clock')
    if received is None:
        return rows.iloc[:0]
    clock = pd.Timestamp(received)
    if clock.tzinfo is None or pd.isna(clock):
        raise ValueError('Expected an actual retrieval timestamp with timezone')
    return rows if mode == 'historical_reconstruction' or clock < decision else rows.iloc[:0]


def _table(folder, day, kind):
    path = folder / f'{day:%Y%m%d}.json'
    if not path.is_file():
        return pd.DataFrame(), None
    src = json.loads(path.read_text())
    payload = src['data']
    if payload['stat'] != 'OK' or payload['date'] != day.strftime('%Y%m%d'):
        raise ValueError('TWSE date or status mismatch')
    if kind == 'bars':
        table, = [t for t in payload['tables'] if '開盤價' in t.get('fields', []) and '證券代號' in t['fields']]
    else:
        table = payload
    frame = pd.DataFrame(table['data'], columns=table['fields']).set_index('證券代號')
    if not frame.index.is_unique:
        raise ValueError('Duplicate TWSE stock row')
    def number(name):
        return pd.to_numeric(frame[name].astype(str).str.replace(',', '', regex=False).replace('--', np.nan))
    if kind == 'bars':
        opening, high, low, close = (number(k).where(lambda x: x.gt(0)) for k in ('開盤價', '最高價', '最低價', '收盤價'))
        traded = number('成交股數').gt(0)
        values = dict(opening=opening, close=close, f_intraday=close/opening-1, f_range=(high-low)/close,
                      f_close_location=(close-low)/(high-low).replace(0, np.nan))
        frame = pd.DataFrame(values).where(traded, axis=0)
    else:
        values = {}
        for key, prefix, suffix in [(FLOW[0], '外陸資', '(不含外資自營商)'), (FLOW[1], '投信', '')]:
            buy, sell = (number(prefix+k+'股數'+suffix) for k in ('買進', '賣出'))
            gross = buy+sell
            values[key] = ((buy-sell)/gross.where(gross.gt(0))).where(gross.ne(0), 0.)
        frame = pd.DataFrame(values)
    return frame, src['received_at']


def _night(folder):
    batches = []
    for code in ('TX','TE','TF'):
        prices = folder / f'{code}.csv'
        if not prices.is_file():
            continue
        meta = json.loads((folder/f'{code}.json').read_text())
        content = prices.read_bytes()
        if hashlib.sha256(content).hexdigest() != meta['sha256']:
            raise ValueError('TAIFEX input fingerprint mismatch')
        history = parse_taifex_history(content)
        if set(history.contract) != {code}:
            raise ValueError('Unexpected futures contract')
        batches.append((taifex_night_events(history), meta['received_at']))
    return batches


def core_votes(trial, symbols):
    votes = {}
    for ticker in symbols:
        peers = [s for s in TRAINING_TICKERS if s != ticker]
        valid = [trial[s]['direct'] for s in peers if s in trial and trial[s]['direct_fallback'] is False]
        votes[ticker] = sum(valid) * 2 > len(peers) if len(valid) == len(peers) else None
    return votes


def panel(days, symbols, bars, flows, night, observed, mode):
    parts = []
    for i in range(3, len(days)):
        day, previous = days[i], days[i-1]
        cutoff = pd.Timestamp(day, tz=gap.LOCAL) + gap.DECISION
        raw, stamp = bars[previous]
        frame = visible(raw, stamp, cutoff, mode).reindex(index=symbols, columns=PRICE)
        flow, stamp = flows[days[i-3]]
        frame = frame.join(visible(flow, stamp, cutoff, mode).reindex(index=symbols, columns=FLOW))
        for batch, stamp in night:
            selected = batch.loc[batch.trade_date.eq(pd.Timestamp(day)) & batch.reference_date.eq(pd.Timestamp(previous))]
            for row in visible(selected, stamp, cutoff, mode).itertuples():
                frame['f_ext_'+row.feature] = row.value
        frame = frame.reindex(columns=INPUTS)
        target = bars[day][0].reindex(index=symbols, columns=['opening']).opening
        prior = raw.reindex(index=symbols, columns=['close']).close
        frame['actual_return'] = target/prior - 1
        pairs = observed.get(str(day), [])
        trial = {r['ticker']: r for r in pairs}
        if len(pairs) != len(trial) or set(trial)-set(symbols):
            raise ValueError('Ambiguous trial rows')
        for row in trial.values():
            direct, fallback = row['direct'], row['direct_fallback']
            if not ((direct is None and fallback is None) or
                    (type(direct) is bool and type(fallback) is bool and not (direct and fallback))):
                raise ValueError('Invalid trial direction or fallback')
        frame['direct_trial'] = [trial.get(s, {}).get('direct') for s in symbols]
        frame['trial_fallback'] = [trial.get(s, {}).get('direct_fallback') for s in symbols]
        votes = core_votes(trial, symbols)
        frame['peer_vote'] = [votes[s] for s in symbols]
        parts.append(frame.assign(date=str(day), ticker=symbols))
    return pd.concat(parts, ignore_index=True)


def fit(history, first_day, calibration_dates=126):
    cutoff = pd.Timestamp(first_day, tz=gap.LOCAL)+gap.DECISION
    core = history.loc[history.ticker.isin(TRAINING_TICKERS) & history.decision_ts.lt(cutoff)].copy()
    days = sorted(core.date.unique())
    if len(days) < calibration_dates + 504:
        raise ValueError('Insufficient historical training or calibration sessions')
    start = days[-calibration_dates]
    cal = core.loc[core.date.ge(start)].copy()
    cal['actual_return'] = cal.actual_return.where(cal.label_available_at.lt(cutoff))
    train = core.loc[core.date.lt(start) & core.label_available_at.lt(cal.decision_ts.min()) & core.actual_return.notna()]
    if train.date.nunique() < 504 or not cal.actual_return.notna().any():
        raise ValueError('Insufficient mature fixed-ten evidence')
    columns = ['date','decision_ts','label_available_at','actual_return',*INPUTS,*gap.EVENT_FIELDS.values()]
    # These two daily sources and the night session are the only eligible historical inputs.
    train = train.reindex(columns=columns).copy()
    train[list(gap.EVENT_FIELDS.values())] = np.nan
    candidates = gap._candidate_models(train, source_fusion=True, factor_study=True)
    models = {k:candidates[k] for k in (*FAMILIES, 'only_price', 'only_other', 'only_night',
                                          'without_price', 'without_other', 'without_night') if k in candidates}
    if not set(FAMILIES) <= set(models):
        raise ValueError('All three learned families require fitted source groups')
    scores = {k:gap._metrics(cal.assign(probability_up=m.predict(cal)), TRAINING_TICKERS) for k,m in models.items()}
    winner = max(FAMILIES, key=lambda k:gap._factor_rank(scores[k]))
    return models, winner, dict(train_dates=train.date.nunique(), train_rows=len(train),
                                latest_train_label=train.label_available_at.max().isoformat(),
                                calibration_start=str(pd.Timestamp(start).date()), calibration_dates=calibration_dates,
                                calibration_end=str(pd.Timestamp(days[-1]).date()), calibration=scores)


def compare(rows, probabilities, winner, symbols):
    samples = dict(probabilities)
    baseline = rows.direct_trial.astype(float)
    samples['direct_trial'] = baseline
    samples['trial_with_backup'] = baseline.mask(rows.trial_fallback.eq(True), probabilities[winner])
    samples['trial_with_core_vote'] = baseline.mask(rows.trial_fallback.eq(True), rows.peer_vote.eq(True).astype(float))
    result = {}
    for name, p in samples.items():
        policy = rows.assign(probability_up=p)
        result[name] = dict(score=gap._metrics(policy, symbols), paired_to_trial=gap._paired_hits(policy, baseline))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for k in ('history','calendar','membership','bars','flows','night','pairs','out'):
        parser.add_argument('--'+k, type=Path, required=True)
    parser.add_argument('--sessions', nargs='+', type=date.fromisoformat, required=True,
                        help='Three preceding exchange sessions followed by scoring dates')
    args = parser.parse_args()
    days = args.sessions
    if len(days) < 4 or days != sorted(set(days)):
        parser.error('Expected three warm-up dates and consecutive scoring sessions')
    symbols = load_membership_snapshot(args.membership)['tickers']
    if len(symbols) != 50:
        parser.error('Expected 50 constituents')
    inputs = [args.history, args.calendar, args.membership, args.pairs,
              *[args.bars/f'{d:%Y%m%d}.json' for d in days[2:]], *[args.flows/f'{d:%Y%m%d}.json' for d in days[:-3]],
              *[args.night/(c+ext) for c in ('TX','TE','TF') for ext in ('.json','.csv')]]
    def hashes():
        return {str(p):gap._sha(p) if p.is_file() else None for p in inputs}
    fingerprints = hashes()
    fields = [*gap.REQUIRED_COLUMNS, 'feature_version','source', *INPUTS, *[k+'_age_days' for k in gap.NIGHT]]
    frame = pd.read_parquet(args.history, columns=fields, filters=[('ticker','in',list(TRAINING_TICKERS))])
    if set(frame.feature_version) != {FEATURE_VERSION} or data_kind(frame) != 'historical' or frame.source.isna().any():
        raise ValueError('Expected recognized historical inputs')
    history = gap.build_opening_gap_panel(frame, pd.read_csv(args.calendar,usecols=['date']).date, TRAINING_TICKERS)
    pairs = json.loads(args.pairs.read_text())
    if pairs['recipe'] != RECIPE:
        raise ValueError('Unexpected trial receipt recipe')
    bars = {d:_table(args.bars,d,'bars') for d in days[2:]}
    flows = {d:_table(args.flows,d,'flow') for d in days[:-3]}
    night = _night(args.night)
    with threadpool_limits(limits=1):
        models, winner, training = fit(history, days[3])
        report = dict(evidence_kind='historical_replay', timely_issued_sessions=0, training_tickers=TRAINING_TICKERS,
                      inputs=fingerprints, runner_sha256=gap._sha(__file__), selected_backup=winner,
                      training=training, sessions=list(map(str,days[3:])), rounds={})
        grids = []
        for mode in CLOCKS:
            rows = panel(days, symbols, bars, flows, night, pairs['observed'], mode)
            for name, model in models.items():
                rows[name] = model.predict(rows)
            summary = {}
            for name, group in [('fixed_ten',TRAINING_TICKERS), ('constituents',symbols)]:
                part = rows.loc[rows.ticker.isin(group)]
                summary[name] = compare(part, {k:part[k] for k in models}, winner, group)
            summary['observed_rows'] = {k:int(rows[list(cols)].notna().any(axis=1).sum())
                                        for k,cols in [('price',PRICE),('flow',FLOW),('night',gap.NIGHT)]}
            core = rows.loc[rows.ticker.isin(TRAINING_TICKERS)]
            simulated = core.peer_vote.eq(True).astype(float).where(core.direct_trial.notna())
            masked = core.assign(probability_up=simulated)
            summary['fixed_ten_simulated_own_trial_outage'] = dict(
                score=gap._metrics(masked, TRAINING_TICKERS),
                paired_to_not_up=gap._paired_hits(masked, pd.Series(0.,index=core.index)))
            report['rounds'][mode] = summary
            grids.append(rows.assign(clock_mode=mode))
    if fingerprints != hashes():
        raise ValueError('Study inputs changed during replay')
    os.umask(0o077)
    args.out.mkdir(parents=True,exist_ok=False)
    pd.concat(grids,ignore_index=True).to_parquet(args.out/'predictions.parquet',index=False)
    joblib.dump(models,args.out/'models.joblib')
    report['artifacts'] = {k:gap._sha(args.out/k) for k in ('models.joblib','predictions.parquet')}
    gap._write_json(args.out/'report.json', report)
    print(json.dumps(dict(report=str(args.out/'report.json'),selected_backup=winner,sessions=report['sessions'])))


if __name__ == '__main__':
    main()
