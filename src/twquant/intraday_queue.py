"""Counterfactual queue-depletion process, not calibrated Taiwan market data.

One event per second and a one-tick-spread reset are deliberate simplifications.
Pressure is hidden from predictors. Future draws never construct known features.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd

from .universe import TRAINING_TICKERS,asset_class,tickers as validate_tickers


@dataclass(frozen=True)
class QueueScenario:
    name: str
    flow_imbalance: float = .8
    flip_rate: float = .002
    input_outage_fraction: float = 0.
    weak_tickers: tuple[str, ...] = ()
    weak_flow_imbalance: float = .1
    persistent_fraction: float = 1.
    background_flow_imbalance: float = .1
    external_persistent_fractions: tuple[tuple[str,float], ...] = ()
    liquidity_response: float = 0.
    liquidity_regimes: tuple[float, ...] = ()
    liquidity_weights: tuple[float, ...] = ()
    external_liquidity_weights: tuple[tuple[str,tuple[float, ...]], ...] = ()

    def __post_init__(self):
        if not isinstance(self.name,str) or not self.name or len(self.name)>80:
            raise ValueError('Declare a short queue-scenario name')
        for value in (self.flow_imbalance,self.flip_rate,self.input_outage_fraction,self.weak_flow_imbalance,
                      self.persistent_fraction,self.background_flow_imbalance):
            if not isinstance(value,(int,float)) or isinstance(value,bool) or not np.isfinite(value) or not 0<=value<=1:
                raise ValueError('Queue parameters must be finite fractions in [0,1]')
        if self.weak_tickers:
            object.__setattr__(self,'weak_tickers',tuple(validate_tickers(self.weak_tickers)))
        if (isinstance(self.liquidity_response,bool) or not isinstance(self.liquidity_response,(int,float))
                or not np.isfinite(self.liquidity_response) or not 0<=self.liquidity_response<=2):
            raise ValueError('Liquidity response must be finite in [0,2]')
        if (not isinstance(self.external_persistent_fractions,(list,tuple))
                or any(not isinstance(pair,(list,tuple)) or len(pair)!=2 for pair in self.external_persistent_fractions)):
            raise ValueError('External regime frequencies require ticker/fraction pairs')
        pairs=tuple((str(ticker),fraction) for ticker,fraction in self.external_persistent_fractions)
        if pairs:
            ids=validate_tickers([ticker for ticker,_ in pairs])
            if len(ids)!=len(pairs) or set(ids)&set(TRAINING_TICKERS):
                raise ValueError('Regime overrides require distinct external targets')
            if any(isinstance(value,bool) or not isinstance(value,(int,float)) or not np.isfinite(value) or not 0<=value<=1 for _,value in pairs):
                raise ValueError('External regime frequencies must be fractions')
        object.__setattr__(self,'external_persistent_fractions',pairs)
        if not isinstance(self.liquidity_regimes,(list,tuple)) or not isinstance(self.liquidity_weights,(list,tuple)):
            raise ValueError('Liquidity regimes and weights require explicit vectors')
        regimes=tuple(self.liquidity_regimes);weights=tuple(self.liquidity_weights)
        if bool(regimes)!=bool(weights) or (regimes and (len(regimes)!=len(weights) or self.liquidity_response!=0)):
            raise ValueError('Declare either scalar liquidity response or matched regime/weight vectors')
        if (len(set(regimes))!=len(regimes) or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not np.isfinite(x) or not 0<=x<=2 for x in regimes)):
            raise ValueError('Liquidity regimes must be distinct finite responses in [0,2]')
        def valid_weights(values):
            return (len(values)==len(regimes) and all(not isinstance(x,bool) and isinstance(x,(int,float)) and np.isfinite(x) and x>=0 for x in values)
                    and np.isclose(sum(values),1.,rtol=0,atol=1e-12))
        if regimes and not valid_weights(weights):raise ValueError('Liquidity weights must sum to one')
        if (not isinstance(self.external_liquidity_weights,(list,tuple)) or
                any(not isinstance(pair,(list,tuple)) or len(pair)!=2 or not isinstance(pair[1],(list,tuple)) for pair in self.external_liquidity_weights)):
            raise ValueError('External liquidity weights require ticker/vector pairs')
        overrides=tuple((str(ticker),tuple(values)) for ticker,values in self.external_liquidity_weights)
        if overrides:
            ids=validate_tickers([t for t,_ in overrides])
            if not regimes or len(ids)!=len(overrides) or set(ids)&set(TRAINING_TICKERS) or any(not valid_weights(w) for _,w in overrides):
                raise ValueError('Liquidity overrides require distinct external targets and valid weights')
        object.__setattr__(self,'liquidity_regimes',regimes)
        object.__setattr__(self,'liquidity_weights',weights)
        object.__setattr__(self,'external_liquidity_weights',overrides)


def _draws(dates,ticker,seed,phase,steps):
    identity=int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4],'big')
    return np.stack([np.random.default_rng(np.random.SeedSequence(
        [seed,identity,int(date.toordinal()),phase])).random((steps,16)) for date in dates])


def _queues(draw,flow_ema=None,response=0.):
    queues=(5+np.floor(6*draw).astype(int)).reshape(-1,2,5)+4*np.arange(5)
    if np.any(response):
        skew=response*flow_ema
        scale=np.stack([np.exp(-skew),np.exp(skew)],axis=1)
        queues=np.maximum(1,np.ceil(queues*scale[:,:,None]).astype(int))
    return queues


def _step(queues,position,pressure,draw,strength,flip_rate,flow_ema,response):
    pressure=np.where(draw[:,0]<-np.expm1(-flip_rate),-pressure,pressure)
    buying=draw[:,1]<(.5+.5*strength*pressure)
    # Markets consume the best quote; cancellations and insertions also touch depth.
    market=draw[:,2]<.65
    flow_ema=np.where(market,.95*flow_ema+.05*np.where(buying,1.,-1.),flow_ema)
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
    local_response=response[depleted] if np.ndim(response) else response
    queues[depleted]=_queues(draw[depleted,6:16],flow_ema[depleted],local_response)
    return pressure,flow_ema


@dataclass
class _QueueHistory:
    queues: np.ndarray
    position: np.ndarray
    pressure: np.ndarray
    strength: np.ndarray
    response: np.ndarray | float
    flow_ema: np.ndarray
    persistent: np.ndarray
    columns: dict


def _queue_history(dates,ticker,scenario,seed,market_pressure):
    """Observed history plus private Markov state; never an inference feature frame."""
    past=_draws(dates,ticker,seed,1,101)
    queues=_queues(past[:,0,:10])
    pressure=np.where(past[:,0,11]<.35,market_pressure,np.where(past[:,0,10]<.5,-1,1))
    position=np.zeros(len(dates),dtype=int)
    frequency=dict(scenario.external_persistent_fractions).get(ticker,scenario.persistent_fraction)
    persistent=past[:,0,12]<frequency
    peak=scenario.weak_flow_imbalance if ticker in scenario.weak_tickers else scenario.flow_imbalance
    strength=np.where(persistent,peak,scenario.background_flow_imbalance)
    response=scenario.liquidity_response
    if scenario.liquidity_regimes:
        weights=dict(scenario.external_liquidity_weights).get(ticker,scenario.liquidity_weights)
        indices=np.searchsorted(np.cumsum(weights),past[:,0,13],side='right')
        response=np.array(scenario.liquidity_regimes)[np.minimum(indices,len(weights)-1)]
    flow_ema=np.zeros(len(dates))
    history=np.zeros((len(dates),100))
    imbalance=np.zeros((len(dates),100,5))
    for step in range(100):
        pressure,flow_ema=_step(queues,position,pressure,past[:,step+1],strength,scenario.flip_rate,flow_ema,response)
        history[:,step]=position
        bid=np.cumsum(queues[:,0],axis=1);ask=np.cumsum(queues[:,1],axis=1)
        imbalance[:,step]=(bid-ask)/np.maximum(bid+ask,1)
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
    return _QueueHistory(queues,position,pressure,strength,response,flow_ema,persistent,columns)


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
        state=_queue_history(dates,ticker,scenario,seed,market_pressure)
        queues=state.queues;position=state.position;pressure=state.pressure
        strength=state.strength;response=state.response;flow_ema=state.flow_ema
        persistent=state.persistent;columns=state.columns
        future=_draws(dates,ticker,future_seed,2,60)
        initial=position.copy()
        for step in range(horizon_seconds):
            pressure,flow_ema=_step(queues,position,pressure,future[:,step],strength,scenario.flip_rate,flow_ema,response)
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
            diagnostic_persistent_regime=persistent,
            diagnostic_liquidity_response=response,
            source='synthetic_queue_process',price_basis='abstract_index_points_no_book' if is_index else 'abstract_one_tick_mid_quote')
        frame.loc[~ready,'max_input_available_at']=pd.NaT
        frames.append(frame)
    return pd.concat(frames,ignore_index=True).sort_values(['date','ticker']).reset_index(drop=True)
