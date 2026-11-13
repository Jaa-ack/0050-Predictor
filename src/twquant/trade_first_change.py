"""Accepted transaction-price first changes, including the spread-bounce baseline.

This is an explicitly synthetic queue process, separate from midquote direction.
Each market event executes once at its old best quote; unfilled excess is canceled.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from .first_change import _audit_first_event_predictions,_validate_first_event_fit
from .intraday_models import SharedIntradayModel
from .queue_first_change import _advance_queue_price,_event_frame,_simulate_queue_price_panel

TARGET='first_trade_change_before_deadline'
MODELS=('prior','spread_bounce','trade_logistic','trade_hist')


def advance_trade_first_change(state,draw_blocks,*,event_rate,latency_seconds,max_wait_seconds,flip_hazard,size_weights):
    result=_advance_queue_price(state,draw_blocks,event_rate=event_rate,latency_seconds=latency_seconds,
        max_wait_seconds=max_wait_seconds,flip_hazard=flip_hazard,size_weights=size_weights,price_kind='trade')
    result['anchor_trade_price']=result.pop('anchor_position')
    return result


def simulate_trade_first_panel(tickers,dates,scenario,*,seed=17,future_seed=29,max_wait_seconds=60,max_events=2048,block_events=32):
    return _simulate_queue_price_panel(tickers,dates,scenario,seed=seed,future_seed=future_seed,max_wait_seconds=max_wait_seconds,
        max_events=max_events,block_events=block_events,price_kind='trade')


def _trade_frame(frame):
    if 'f_trade_location' not in frame:raise ValueError('Transaction models require the observed last-trade location')
    result=_event_frame(frame)
    result['f_book_trade_location']=pd.to_numeric(frame.f_trade_location,errors='coerce').round(12)
    matching=('f_trade_spread_ticks','f_trade_bid_depth_to_fill','f_trade_ask_depth_to_fill')
    present=set(matching)&set(frame)
    if present and present!=set(matching):raise ValueError('Declare the complete matching feature profile')
    for name in matching:
        if name in present:result[name.replace('f_trade_','f_book_trade_',1)]=pd.to_numeric(frame[name],errors='coerce').round(12)
    return result


class TradeFirstModel:
    def __init__(self,frame,name,seconds):
        self.name=name
        inner_name='prior' if name in ('prior','spread_bounce') else ('history_hist' if name=='trade_hist' else 'history_logistic')
        self.inner=SharedIntradayModel(_trade_frame(frame),inner_name);self.prior=self.inner.prior
        implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.version=f'trade-first-{name}-{seconds}-{implementation[:12]}-{self.inner.version}'
        self.training_details=dict(self.inner.training_details,model=name,target_kind=TARGET,max_wait_seconds=seconds,
            history_window_unit='events',trade_first_implementation_sha256=implementation,hidden_state_used=False,
            labels='up/down plus timeout; emitted up/down probabilities renormalized; timeout earns no binary credit',
            timeout_training_rows=self.inner.training_details['flat_training_rows'],spread_bounce_baseline=name=='spread_bounce')
        self.training_details.pop('flat_training_rows')
        if name=='spread_bounce':self.training_details['features']=['f_trade_location']

    def predict_proba(self,frame):
        inputs=_trade_frame(frame)
        if self.name!='spread_bounce':return self.inner.predict_proba(inputs)
        ready=frame.input_ready.fillna(False).astype(bool).to_numpy()
        location=inputs.f_book_trade_location.to_numpy()
        if not np.isfinite(location[ready]).all():raise ValueError('Ready bounce inputs require finite observed trade location')
        up=np.full(len(frame),self.prior);up[ready]=(location[ready]<0).astype(float)
        return np.column_stack([1-up,up])

    def predict_locations(self,locations,*,input_ready):
        """Use prepared causal locations; callers establish receipt/time contracts.

        This array path excludes feed reception, feature building and persistence.
        Keep the DataFrame interface's twelve-decimal location convention.
        """
        if self.name!='spread_bounce':raise ValueError('Location inference requires the spread-bounce model')
        values=np.asarray(locations);ready=np.asarray(input_ready)
        if values.ndim!=1 or values.dtype.kind not in 'fiu':raise ValueError('Declare a numeric location vector')
        if ready.shape!=values.shape or ready.dtype.kind!='b':raise ValueError('Declare a matching boolean input_ready vector')
        observed=np.round(values[ready].astype(float,copy=False),12)
        if not np.isfinite(observed).all():raise ValueError('Ready bounce locations must be finite')
        up=np.full(len(values),self.prior);up[ready]=observed<0
        return np.column_stack([1-up,up])


def fit_trade_first_model(frame,name,*,fit_at):
    if name not in MODELS:raise ValueError('Unknown transaction first-change model')
    seconds=_validate_first_event_fit(frame,fit_at=fit_at,target_kind=TARGET)
    return TradeFirstModel(frame,name,seconds)


def audit_trade_first_predictions(predictions,constituents,*,expected_decisions,max_wait_seconds,min_dates=126,evidence_kind='synthetic'):
    return _audit_first_event_predictions(predictions,constituents,expected_decisions=expected_decisions,max_wait_seconds=max_wait_seconds,
        min_dates=min_dates,evidence_kind=evidence_kind,target_kind=TARGET)
