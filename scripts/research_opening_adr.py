"""Compare issuer-specific ADR signals with archived Taiwan opening predictions."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.special import expit, logit

from twquant import opening_gap as gap
from twquant.direction_audit import load_membership_snapshot
from twquant.source_store import SourceStore
from twquant.universe import DEFAULT_EVALUATION_TICKERS, TRAINING_TICKERS

ISSUERS = {'2330':'TSM', '3711':'ASX', '2303':'UMC', '2412':'CHT'}


def signals(sessions, bars, *, strict):
    market, market_receipt = bars['SPY']
    published = (market.index + pd.Timedelta(hours=17)).tz_localize('America/New_York')
    rows = []
    for previous, day in zip(sessions[:-1], sessions[1:], strict=True):
        anchor = previous.tz_localize(gap.LOCAL) + pd.Timedelta(hours=13, minutes=30)
        cutoff = day.tz_localize(gap.LOCAL) + gap.DECISION
        us = market.index[(published > anchor) & (published < cutoff)]
        for ticker, symbol in ISSUERS.items():
            own = relative = np.nan
            if len(us) and symbol in bars and (not strict or market_receipt < cutoff):
                series, receipt = bars[symbol]
                moves = series.reindex(us)
                if (not strict or receipt < cutoff) and moves.notna().all() and market.loc[us].notna().all():
                    gross = (1 + moves).prod()
                    own = gross - 1
                    relative = gross / (1 + market.loc[us]).prod() - 1
            rows.append(dict(date=day, ticker=ticker, own=own, relative=relative))
    return pd.DataFrame(rows).set_index(['date','ticker'])


def fit_weight(rows, feature, cutoff):
    eligible = (rows.ticker.isin(TRAINING_TICKERS) & rows.label_available_at.lt(cutoff)
                & rows.actual_return.notna() & rows.probability_up.between(0, 1) & rows[feature].notna())
    train = rows.loc[eligible]
    x = train[feature].to_numpy() * 100
    base = logit(train.probability_up.clip(1e-6, 1-1e-6).to_numpy())
    y = train.actual_return.gt(0).to_numpy()
    # One nonnegative coefficient, with a unit L2 penalty on each percentage-point effect.
    def gradient(w):
        return w + np.dot(x, expit(base + w*x) - y)
    if not len(train) or gradient(0.) >= 0:
        return 0., len(train)
    return brentq(gradient, 0., np.abs(x).sum()+1), len(train)


def directions(rows, weights=None):
    p = rows.probability_up
    base = p.ge(.5).astype(float).where(p.between(0, 1))
    variants = {'original':base}
    for feature in ('own', 'relative'):
        value = rows[feature]
        for width in (10, 20, 50):
            use = base.notna() & value.notna() & p.between((50-width)/100, (50+width)/100)
            variants[f'{feature}_{width}'] = base.mask(use, value.gt(0).astype(float))
        if weights is not None:
            odds = logit(p.clip(1e-6, 1-1e-6)) + weights[feature]*100*value
            variants[feature+'_weighted'] = base.mask(base.notna() & value.notna(), odds.ge(0).astype(float))
    return pd.DataFrame(variants, index=rows.index)


def load_bars(folder):
    store = SourceStore(folder)
    bars, sources = {}, {}
    for info in store.manifests():
        if (info['provider'], info['dataset']) != ('finmind','USStockPrice'):
            continue
        result = store.read(info['snapshot_id'])
        symbol = result.metadata['symbol']
        if symbol not in {*ISSUERS.values(), 'SPY'}:
            continue
        if symbol in bars:
            raise ValueError('Supply one pinned snapshot per US symbol')
        table = pd.DataFrame(result.records)
        if table.empty:
            continue
        if not table.stock_id.eq(symbol).all():
            raise ValueError('US quote identity mismatch')
        table.index = pd.to_datetime(table.date, format='%Y-%m-%d')
        table = table.sort_index()
        if not table.index.is_unique or table.index.hasnans:
            raise ValueError('Missing or duplicate US market dates')
        opening, close, volume = (pd.to_numeric(table[k]) for k in ('Open','Close','Volume'))
        receipt = pd.Timestamp(result.retrieved_at)
        if pd.isna(receipt) or receipt.tzinfo is None:
            raise ValueError('Missing actual US market receipt clock')
        completed = (table.index + pd.Timedelta(hours=17)).tz_localize('America/New_York') <= receipt
        valid = np.isfinite(opening) & np.isfinite(close) & opening.gt(0) & close.gt(0) & volume.gt(0)
        bars[symbol] = ((close/opening-1).where(valid).loc[completed], receipt)
        sources[symbol] = dict(snapshot=info['snapshot_id'], sha256=info['sha256'], received_at=receipt.isoformat())
    if 'SPY' not in bars:
        raise ValueError('Expected SPY market sessions')
    return bars, sources


def score(rows, policies, tickers):
    sample = rows.loc[rows.ticker.isin(tickers)]
    result = {}
    for name in policies:
        forecast = sample.assign(probability_up=policies.loc[sample.index,name])
        result[name] = dict(score=gap._metrics(forecast, tickers),
                            paired=gap._paired_hits(forecast, policies.loc[sample.index,'original']))
    return result


def compare(frame, sessions, final_dates, symbols, bars, calibration_days=126):
    core = list(TRAINING_TICKERS)
    targets = list(dict.fromkeys([*symbols, *DEFAULT_EVALUATION_TICKERS]))
    index = pd.MultiIndex.from_product([sessions[1:], targets], names=['date','ticker'])
    source = frame.set_index(['date','ticker'])
    if not source.index.is_unique or not set(core) <= set(symbols):
        raise ValueError('Invalid prediction grid or missing training constituents')
    grid = source.reindex(index)
    expected = grid.index.get_level_values('date').tz_localize(gap.LOCAL) + gap.DECISION
    if (grid.decision_ts.notna() & grid.decision_ts.ne(expected)).any():
        raise ValueError('Unexpected forecast time')
    grid['probability_up'] = grid.probability_up.where(grid.decision_ts.notna())
    cal_dates = sessions[1:][sessions[1:] < final_dates[0]][-calibration_days:]
    if len(cal_dates) != calibration_days:
        raise ValueError('Missing calibration sessions')
    scores, weights, training_counts = {}, {}, {}
    for mode, strict in [('holdout',False), ('receipt_gated',True)]:
        rows = grid.join(signals(sessions, bars, strict=strict)).reset_index()
        cal_rows = rows.date.isin(cal_dates)
        rows.loc[cal_rows & ~rows.label_available_at.lt(final_dates[0].tz_localize(gap.LOCAL)+gap.DECISION),
                 'actual_return'] = np.nan
        if not strict:
            train = rows.loc[rows.date.lt(cal_dates[0])]
            for feature in ('own','relative'):
                weights[feature], training_counts[feature] = fit_weight(
                    train, feature, cal_dates[0].tz_localize(gap.LOCAL)+gap.DECISION)
        policies = directions(rows, weights)
        if not strict:
            calibration = score(rows.loc[cal_rows], policies, core)
            selected = max(calibration, key=lambda k:gap._factor_rank(calibration[k]['score']))
        test = rows.date.isin(final_dates)
        groups = {'fixed_ten':core, 'constituents':symbols}
        groups.update({s:[s] for s in dict.fromkeys([*ISSUERS,*DEFAULT_EVALUATION_TICKERS]) if s in targets})
        scores[mode] = {name:score(rows.loc[test], policies, tickers) for name,tickers in groups.items()}
        scores[mode+'_missing_dates'] = {
            ticker:rows.loc[test & rows.ticker.eq(ticker) & rows.own.isna(),'date'].dt.strftime('%Y-%m-%d').tolist()
            for ticker in ISSUERS if ticker in targets}
    return dict(evidence_kind='historical_reconstruction', timely_issued_sessions=0, training_tickers=core,
                weights=weights, training_counts=training_counts, selected=selected, calibration=calibration, **scores)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('sources','predictions','calendar','membership'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    bars, sources = load_bars(args.sources)
    sessions = gap._sessions(pd.read_csv(args.calendar,usecols=['date']).date)
    columns = ['date','ticker','phase','actual_return','label_available_at','decision_ts','probability_up']
    frame = pd.read_parquet(args.predictions,columns=columns)
    held = frame.loc[frame.phase.eq('final'),'date']
    final = sessions[(sessions >= held.min()) & (sessions <= held.max())]
    prior = sessions[sessions < final[0]][-127:]
    symbols = load_membership_snapshot(args.membership)['tickers']
    if len(symbols) != 50 or len(prior) != 127 or len(final) != 126:
        raise ValueError('Expected the complete fixed calibration and historical holdout')
    first_us = bars['SPY'][0].index.min()
    first_session = min(prior[0], first_us)
    period = sessions[(sessions >= first_session) & (sessions <= final[-1])]
    result = compare(frame, period, final, symbols, bars)
    result.update(sources=sources, inputs={k:gap._sha(getattr(args,k)) for k in ('predictions','calendar','membership')},
                  sessions=[str(d.date()) for d in final])
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
