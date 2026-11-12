"""Offline causal examples from explicitly timestamped local book snapshots.

This is neither a market-data receiver nor proof of quote provenance. Planned
research cutoffs do not constitute observed forecast emissions. Endpoint prices
are separate from feature inputs; no absent checkpoint is forward-filled.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .direction_intraday import _aware
from .universe import asset_class,tickers as validate_tickers


def _phase(row):
    trial=row['is_trial'];continuous=row['is_continuous']
    return (isinstance(trial,(bool,np.bool_)) and not trial
            and isinstance(continuous,(bool,np.bool_)) and continuous)


def _book(row):
    if not _phase(row):return None
    try:
        bid,ask,bs,as_=[np.asarray(row[name],dtype=float) for name in
            ['bid_prices','ask_prices','bid_sizes','ask_sizes']]
    except (ValueError,TypeError):return None
    if any(values.shape!=(5,) for values in [bid,ask,bs,as_]):return None
    if not np.isfinite(bs).all() or not np.isfinite(as_).all() or (bs<0).any() or (as_<0).any():return None
    active_bid=bid[bs>0];active_ask=ask[as_>0]
    if (bs[0]<=0 or as_[0]<=0 or not np.isfinite(active_bid).all() or not np.isfinite(active_ask).all()
            or (active_bid<=0).any() or (active_ask<=0).any() or not bid[0]<ask[0]
            or (np.diff(active_bid)>=0).any() or (np.diff(active_ask)<=0).any()):return None
    return bs,as_,(bid[0]+ask[0])/2,ask[0]-bid[0]


def _features(books):
    bid=np.array([b[0] for b in books]);ask=np.array([b[1] for b in books])
    cb=np.cumsum(bid,axis=1);ca=np.cumsum(ask,axis=1)
    imbalance=(cb-ca)/(cb+ca)
    depth=(cb[-1]+ca[-1])/(cb[-1,-1]+ca[-1,-1])
    mid=np.array([b[2] for b in books]);mid=(mid-mid[-1])/books[-1][3]
    result={f'f_book_imbalance_{i+1}':float(imbalance[-1,i]) for i in range(5)}
    result.update({f'f_book_relative_depth_{i+1}':float(depth[i]) for i in range(5)})
    for window in (5,20,100):
        for level in (0,4):
            values=imbalance[-window:,level]
            result[f'f_book_imbalance_{level+1}_mean_{window}']=float(values.mean())
            result[f'f_book_imbalance_{level+1}_change_{window}']=float(values[-1]-values[0])
        result[f'f_book_mid_change_{window}']=float(mid[-1]-mid[-window])
        result[f'f_book_mid_vol_{window}']=float(np.diff(mid[-window:]).std())
    return result


def _checkpoint(row,ticker):
    if row is None or not _phase(row) or row.available_at<row.price_at:return np.nan
    if asset_class(ticker)=='index':
        value=pd.to_numeric(row.index_value,errors='coerce')
        return float(value) if np.isfinite(value) and value>0 else np.nan
    bid=pd.to_numeric(row.bid,errors='coerce');ask=pd.to_numeric(row.ask,errors='coerce')
    return float((bid+ask)/2) if np.isfinite(bid) and np.isfinite(ask) and 0<bid<ask else np.nan


def build_intraday_book_frame(quotes,checkpoints,tickers,*,expected_decisions,horizon_seconds=30):
    """Build every requested ticker/day, with a fixed one-second compute buffer.

    Features use a100point one-second as-of grid ending at decision-minus1s;
    each selected full snapshot must be at most2s old and received by cutoff.
    Checkpoints must explicitly cover the exact decision and endpoint instants.
    All returned source records remain synthetic or unverified research inputs.
    """
    universe=validate_tickers(tickers)
    decisions=_aware(expected_decisions,'Decision calendar').sort_values()
    local=decisions.tz_convert('Asia/Taipei')
    if (not len(decisions) or decisions.hasnans or decisions.has_duplicates
            or local.normalize().has_duplicates or len(set(local.time))!=1):
        raise ValueError('Require one common fixed-clock decision per declared daily date')
    if type(horizon_seconds) is not int or horizon_seconds not in (1,5,30,60):
        raise ValueError('Require a fixed1/5/30/60-second endpoint')
    seconds=(local-local.normalize()).total_seconds()
    if (seconds<9*3600).any() or (seconds+horizon_seconds>=13*3600+25*60).any():
        raise ValueError('Book examples must remain inside continuous trading hours')
    qr={'ticker','state_at','received_at','bid_prices','ask_prices','bid_sizes','ask_sizes','is_trial','is_continuous','source'}
    cr={'ticker','price_at','available_at','bid','ask','index_value','is_trial','is_continuous','source'}
    if not qr<=set(quotes) or not cr<=set(checkpoints):
        raise ValueError('Local book inputs lack the canonical columns')
    q=quotes.copy();c=checkpoints.copy()
    q['ticker']=q.ticker.astype(str);c['ticker']=c.ticker.astype(str)
    for column in ['state_at','received_at']:q[column]=_aware(q[column],column)
    for column in ['price_at','available_at']:c[column]=_aware(c[column],column)
    if (q[['state_at','received_at']].isna().any().any() or c[['price_at','available_at']].isna().any().any()
            or q.duplicated(['ticker','state_at','received_at']).any() or c.duplicated(['ticker','price_at']).any()):
        raise ValueError('Ambiguous local snapshot or checkpoint identity')
    # Template names are independent of observed labels and other instruments.
    template=_features([(np.ones(5),np.ones(5),100.,1.) for _ in range(100)])
    rows=[]
    for decision in decisions:
        cutoff=decision-pd.Timedelta(seconds=1);target=decision+pd.Timedelta(seconds=horizon_seconds)
        grid=pd.date_range(cutoff-pd.Timedelta(seconds=99),cutoff,freq='s')
        for ticker in universe:
            row={name:np.nan for name in template}
            history=q[q.ticker.eq(ticker)&q.received_at.le(cutoff)&q.state_at.le(cutoff)]
            history=history[history.state_at.dt.tz_convert('Asia/Taipei').dt.normalize().eq(decision.tz_convert('Asia/Taipei').normalize())]
            history=history.sort_values(['state_at','received_at']).drop_duplicates('state_at',keep='last')
            ready=False;reason='missing_history';used=history.iloc[:0]
            if asset_class(ticker)=='index':reason='index_without_book'
            elif len(history):
                positions=history.state_at.searchsorted(grid,side='right')-1
                if (positions>=0).all():
                    used=history.iloc[positions]
                    stale=(grid-pd.DatetimeIndex(used.state_at)).total_seconds()>2
                    books=[_book(snapshot) for _,snapshot in used.iterrows()]
                    if stale.any():reason='stale_history'
                    elif not used.received_at.ge(used.state_at).all() or any(book is None for book in books):reason='invalid_book'
                    else:row.update(_features(books));ready=True;reason='ready'
            selected=c[c.ticker.eq(ticker)&c.price_at.isin([decision,target])]
            anchor=selected[selected.price_at.eq(decision)];end=selected[selected.price_at.eq(target)]
            anchor=anchor.iloc[0] if len(anchor) else None;end=end.iloc[0] if len(end) else None
            start_price=_checkpoint(anchor,ticker);end_price=_checkpoint(end,ticker)
            known=np.isfinite(start_price) and np.isfinite(end_price)
            available=max(anchor.available_at,end.available_at) if known else pd.NaT
            sources=[*used.source.astype(str),*selected.source.astype(str)]
            kind='synthetic' if any(value.startswith('synthetic') for value in sources) else 'unverified'
            row.update(ticker=ticker,date=decision.tz_convert('Asia/Taipei').tz_localize(None).normalize(),
                decision_at=decision,prediction_at=decision,feature_cutoff_at=cutoff,
                max_input_available_at=used.received_at.max() if len(used) else pd.NaT,
                target_at=target,label_available_at=available,
                actual_return=end_price/start_price-1 if known else np.nan,input_ready=ready,
                input_reason=reason,horizon_seconds=horizon_seconds,source=f'{kind}_local_book_frame',
                evidence_kind='synthetic' if kind=='synthetic' else 'unknown',
                price_basis='index_points' if asset_class(ticker)=='index' else 'raw_mid_quote',
                feature_profile='portable_book_v1',emission_clock_basis='planned_research_cutoff',
                quote_provenance_verified=False)
            rows.append(row)
    return pd.DataFrame(rows).sort_values(['date','ticker']).reset_index(drop=True)
