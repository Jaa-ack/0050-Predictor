"""Counterfactual queue-depletion process, not calibrated Taiwan market data.

One event per second and a one-tick-spread reset are deliberate simplifications.
Pressure is hidden from predictors. Future draws never construct known features.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd

from .universe import asset_class, tickers as validate_tickers


@dataclass(frozen=True)
class QueueScenario:
    name: str
    flow_imbalance: float = .8
    flip_rate: float = .002
    input_outage_fraction: float = 0.
    weak_tickers: tuple[str, ...] = ()
    weak_flow_imbalance: float = .1

    def __post_init__(self):
        if not isinstance(self.name,str) or not self.name or len(self.name)>80:
            raise ValueError('Declare a short queue-scenario name')
        for value in (self.flow_imbalance,self.flip_rate,self.input_outage_fraction,self.weak_flow_imbalance):
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not np.isfinite(value) or not 0<=value<=1:
                raise ValueError('Queue parameters must be finite fractions in [0,1]')
        if self.weak_tickers:
            object.__setattr__(self,'weak_tickers',tuple(validate_tickers(self.weak_tickers)))


def _draws(dates,ticker,seed,phase,steps):
    identity=int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4],'big')
    return np.stack([np.random.default_rng(np.random.SeedSequence(
        [seed,identity,int(date.toordinal()),phase])).random((steps,16)) for date in dates])


def _queues(draw):
    return (5+np.floor(6*draw).astype(int)).reshape(-1,2,5)+4*np.arange(5)


def _step(queues,position,pressure,draw,strength,flip_rate):
    pressure=np.where(draw[:,0]<-np.expm1(-flip_rate),-pressure,pressure)
    buying=draw[:,1]<(.5+.5*strength*pressure)
    # Markets consume the best quote; cancellations and insertions also touch depth.
    market=draw[:,2]<.65
    cancel=(draw[:,2]>=.65)&(draw[:,2]<.8)
    side=np.where(market,buying,np.where(cancel,draw[:,3]<.5,~buying)).astype(int)
    level=np.where(market,0,np.floor(5*draw[:,4]).astype(int))
    rows=np.arange(len(queues))
    size=1+np.floor(4*draw[:,5]).astype(int)
    queues[rows,side,level]+=np.where(market|cancel,-size,size)
    # Deeper empty levels are retained as empty; only a best-queue depletion moves price.
    depleted=queues[rows,side,0]<=0
    position+=np.where(depleted,np.where(side==1,1,-1),0)
    queues[:]=np.maximum(queues,0)
    queues[depleted]=_queues(draw[depleted,6:16])
    return pressure


def simulate_queue_panel(tickers,dates,scenario,*,seed=17,future_seed=29,horizon_seconds=30):
    """Generate complete daily examples and finite-horizon raw endpoint labels.

    Random streams are keyed by ticker, date and phase: adding external stocks,
    extending the calendar or changing future draws cannot change known history.
    Synthetic dates are supplied explicitly; no exchange calendar is asserted.
    """
    universe=validate_tickers(tickers)
    dates=pd.DatetimeIndex(dates)
    if (not len(dates) or dates.hasnans or dates.has_duplicates or dates.tz is not None
            or not dates.equals(dates.normalize())):
        raise ValueError('Declare distinct timezone-naive daily simulation dates')
    dates=dates.sort_values()
    if not isinstance(scenario,QueueScenario):
        raise ValueError('Declare an explicit QueueScenario')
    if type(horizon_seconds) is not int or horizon_seconds not in (1,5,30,60):
        raise ValueError('Queue horizons must be 1/5/30/60 fixed seconds')
    if any(type(s) is not int or not 0<=s<2**32 for s in (seed,future_seed)):
        raise ValueError('Declare bounded integer simulation seeds')
    decision=dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10)
    common=_draws(dates,'synthetic_market_clock',seed,0,1)[:,0]
    outage=common[:,0]<scenario.input_outage_fraction
    market_pressure=np.where(common[:,1]<.5,-1,1)
    frames=[]
    for ticker in universe:
        past=_draws(dates,ticker,seed,1,101)
        future=_draws(dates,ticker,future_seed,2,60)
        queues=_queues(past[:,0,:10])
        pressure=np.where(past[:,0,11]<.35,market_pressure,np.where(past[:,0,10]<.5,-1,1))
        position=np.zeros(len(dates),dtype=int)
        strength=scenario.weak_flow_imbalance if ticker in scenario.weak_tickers else scenario.flow_imbalance
        history=np.zeros((len(dates),100))
        imbalance=np.zeros((len(dates),100,5))
        for step in range(100):
            pressure=_step(queues,position,pressure,past[:,step+1],strength,scenario.flip_rate)
            history[:,step]=position
            bid=np.cumsum(queues[:,0],axis=1);ask=np.cumsum(queues[:,1],axis=1)
            imbalance[:,step]=(bid-ask)/np.maximum(bid+ask,1)
        initial=position.copy()
        columns={f'f_book_imbalance_{level+1}':imbalance[:,-1,level] for level in range(5)}
        depth=np.cumsum(queues.sum(axis=1),axis=1)
        columns.update({f'f_book_log_depth_{level+1}':np.log1p(depth[:,level]) for level in range(5)})
        for window in (5,20,100):
            for level in (0,4):
                values=imbalance[:,-window:,level]
                columns[f'f_book_imbalance_{level+1}_mean_{window}']=values.mean(axis=1)
                columns[f'f_book_imbalance_{level+1}_change_{window}']=values[:,-1]-values[:,0]
            columns[f'f_book_mid_change_{window}']=history[:,-1]-history[:,-window]
            columns[f'f_book_mid_vol_{window}']=np.diff(history[:,-window:],axis=1).std(axis=1)
        for step in range(horizon_seconds):
            pressure=_step(queues,position,pressure,future[:,step],strength,scenario.flip_rate)
        actual=(position-initial)/(10000.+initial)
        is_index=asset_class(ticker)=='index'
        ready=~outage if not is_index else np.zeros(len(dates),dtype=bool)
        if is_index:
            # Independent abstract index-point walk; never fabricate an index book.
            actual=np.where(future[:,:horizon_seconds,0]<.5,-1,1).sum(axis=1)/10000.
        frame=pd.DataFrame(columns)
        frame.loc[~ready,list(columns)]=np.nan
        frame=frame.assign(ticker=ticker,date=dates,decision_at=decision,prediction_at=decision,
            max_input_available_at=decision-pd.Timedelta(milliseconds=5),
            target_at=decision+pd.Timedelta(seconds=horizon_seconds),
            label_available_at=decision+pd.Timedelta(seconds=horizon_seconds,milliseconds=5),
            actual_return=actual,input_ready=ready,horizon_seconds=horizon_seconds,
            source='synthetic_queue_process',price_basis='abstract_index_points_no_book' if is_index else 'abstract_one_tick_mid_quote')
        frame.loc[~ready,'max_input_available_at']=pd.NaT
        frames.append(frame)
    return pd.concat(frames,ignore_index=True).sort_values(['date','ticker']).reset_index(drop=True)
