"""Counterfactual ordered-level market, with no simultaneous quote resets.

Mechanics preserve partial fills and resting/IOC remainders. Event proportions,
size distributions, finite initial depth and clocks are not TWSE calibrations.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib

import numpy as np
import pandas as pd

from .first_change import _wait
from .intraday_queue import _draws
from .matching_book import BookBatch,TimedOrders,first_trade_change
from .queue_first_change import QueueFirstScenario,_number,_feature_names
from .universe import TRAINING_TICKERS,asset_class,tickers as validate_tickers


def _depth_range(values):
    if (not isinstance(values,(list,tuple)) or len(values)!=2
            or any(type(v) is not int for v in values) or not 1<=values[0]<=values[1]<=1_000_000):
        raise ValueError('Depth range requires two ordered positive bounded integers')
    return tuple(values)


@dataclass(frozen=True)
class MatchingScenario(QueueFirstScenario):
    latency_seconds:float=.0001
    market_fraction:float=.65
    cancel_fraction:float=.15
    inside_fraction:float=1.
    sweep_fraction:float=1.
    large_order_fraction:float=0.
    grid_ticks:int=128
    external_large_order_fractions:tuple[tuple[str,float],...]=()
    initial_depth_range:tuple[int,int]=(5,10)
    external_depth_ranges:tuple[tuple[str,tuple[int,int]],...]=()

    def __post_init__(self):
        super().__post_init__()
        values=(self.market_fraction,self.cancel_fraction,self.inside_fraction,self.sweep_fraction,self.large_order_fraction)
        if not all(_number(v,0,1) for v in values) or self.market_fraction+self.cancel_fraction>1:raise ValueError('Matching fractions must be valid probabilities')
        if type(self.grid_ticks) is not int or self.grid_ticks%2 or not 32<=self.grid_ticks<=1024:raise ValueError('Declare an even bounded finite price grid')
        if self.queue.liquidity_response or self.queue.liquidity_regimes:raise ValueError('Reset-based liquidity response is unsupported by actual level matching')
        pairs=self.external_large_order_fractions
        if not isinstance(pairs,(list,tuple)) or any(not isinstance(p,(list,tuple)) or len(p)!=2 for p in pairs):raise ValueError('Declare external large-order fractions')
        pairs=tuple((str(t),v) for t,v in pairs)
        if pairs:
            ids=validate_tickers([t for t,_ in pairs])
            if len(ids)!=len(pairs) or set(ids)&set(TRAINING_TICKERS) or any(not _number(v,0,1) for _,v in pairs):raise ValueError('Declare distinct external large-order fractions')
        object.__setattr__(self,'external_large_order_fractions',pairs)
        object.__setattr__(self,'initial_depth_range',_depth_range(self.initial_depth_range))
        depth_pairs=self.external_depth_ranges
        if not isinstance(depth_pairs,(list,tuple)) or any(not isinstance(p,(list,tuple)) or len(p)!=2 for p in depth_pairs):
            raise ValueError('Declare external depth ranges')
        depth_pairs=tuple((str(t),_depth_range(v)) for t,v in depth_pairs)
        if depth_pairs:
            ids=validate_tickers([t for t,_ in depth_pairs])
            if len(ids)!=len(depth_pairs) or set(ids)&set(TRAINING_TICKERS) or any(asset_class(t)=='index' for t in ids):
                raise ValueError('External depth ranges require distinct nonprimary tradable targets')
        object.__setattr__(self,'external_depth_ranges',depth_pairs)


def _generators(dates,ticker,seed,phase):
    identity=int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4],'big')
    return [np.random.default_rng(np.random.SeedSequence([seed,identity,int(date.toordinal()),phase])) for date in dates]


def _order_fields(book,draw,scenario,pressure,strength,ticker):
    pressure=np.where(draw[:,0]<-np.expm1(-scenario.queue.flip_rate),-pressure,pressure)
    buy=draw[:,1]<(.5+.5*strength*pressure)
    kind=np.where(draw[:,2]<scenario.market_fraction,0,np.where(draw[:,2]<scenario.market_fraction+scenario.cancel_fraction,1,2))
    weights=dict(scenario.external_size_weights).get(ticker,scenario.size_weights)
    size=1+np.minimum(np.searchsorted(np.cumsum(weights),draw[:,5],side='right'),3)
    large=dict(scenario.external_large_order_fractions).get(ticker,scenario.large_order_fraction)
    size=np.where((kind==0)&(draw[:,8]<large),np.where(draw[:,9]<.5,16,32),size)
    bid=book.bid_price;ask=book.ask_price;level=np.floor(5*draw[:,4])
    price=np.where(buy,bid-level,ask+level)
    spread=ask-bid
    improve=(kind==2)&(spread>1)&(draw[:,6]<scenario.inside_fraction)
    price=np.where(improve,bid+1+np.floor(draw[:,7]*np.maximum(spread-1,0)),price)
    capped=(kind==0)&(draw[:,3]>=scenario.sweep_fraction)
    price=np.where(capped,np.where(buy,ask,bid),price);kind=np.where(capped,2,kind)
    priced=kind!=0
    inside=np.isfinite(price)&(price>=book.origin)&(price<book.origin+book.quantities.shape[1])
    book.complete&=~priced|inside
    return kind,buy,size,np.where(priced,price,np.nan),capped,pressure


def _history(dates,ticker,scenario,seed,market_pressure):
    W=scenario.grid_ticks;N=len(dates);origin=10000-W//2
    initial=np.stack([rng.random(1024) for rng in _generators(dates,ticker,seed,30)])[:,512-W//2:512+W//2]
    depth_low,depth_high=dict(scenario.external_depth_ranges).get(ticker,scenario.initial_depth_range)
    quantities=(depth_low+np.floor((depth_high-depth_low+1)*initial)).astype('int64')*np.where(np.arange(W)<W//2,1,-1)
    book=BookBatch(quantities,origin=origin);past=_draws(dates,ticker,seed,31,101);first=past[:,0];spec=scenario.queue
    pressure=np.where(first[:,11]<.35,market_pressure,np.where(first[:,10]<.5,-1,1))
    frequency=dict(spec.external_persistent_fractions).get(ticker,spec.persistent_fraction)
    peak=spec.weak_flow_imbalance if ticker in spec.weak_tickers else spec.flow_imbalance
    strength=np.where(first[:,12]<frequency,peak,spec.background_flow_imbalance)
    last=np.where(first[:,14]<.5,9999.,10000.);volume=np.zeros(N);count=np.zeros(N)
    mid_history=[];imb_history=[]
    for step in range(1,101):
        kind,buy,size,price,ioc,pressure=_order_fields(book,past[:,step],scenario,pressure,strength,ticker)
        result=book.apply(kind=kind,buy=buy,quantity=size,price=price,ioc=ioc)
        for fill in result.fills:
            hit=fill.quantity>0;last[hit]=fill.price[hit];volume+=fill.quantity;count+=hit
        depths=book.five_depths();bid=np.cumsum(depths[:,0],axis=1);ask=np.cumsum(depths[:,1],axis=1)
        imb_history.append((bid-ask)/np.maximum(bid+ask,1));mid_history.append((book.bid_price+book.ask_price)/2)
    imbalance=np.stack(imb_history,axis=1);mid=np.stack(mid_history,axis=1);total=np.cumsum(depths.sum(axis=1),axis=1)
    features={}
    for level in range(1,6):
        features[f'f_book_imbalance_{level}']=imbalance[:,-1,level-1]
        features[f'f_book_relative_depth_{level}']=total[:,level-1]/np.maximum(total[:,-1],1)
    for window in (5,20,100):
        for level in (1,5):
            values=imbalance[:,-window:,level-1]
            for stat,value in (('mean',values.mean(axis=1)),('change',values[:,-1]-values[:,0])):features[f'f_event_imbalance_{level}_{stat}_{window}']=value
        features[f'f_event_mid_change_{window}']=mid[:,-1]-mid[:,-window]
        features[f'f_event_mid_vol_{window}']=np.diff(mid[:,-window:],axis=1).std(axis=1)
    spread=book.ask_price-book.bid_price;mean_fill=np.maximum(volume/np.maximum(count,1),1.)
    features.update(f_trade_location=2*(last-mid[:,-1])/spread,f_trade_spread_ticks=spread,
        f_trade_bid_depth_to_fill=depths[:,0,0]/mean_fill,f_trade_ask_depth_to_fill=depths[:,1,0]/mean_fill)
    return book,last,pressure,strength,features,depths


def _future_source(dates,ticker,scenario,seed,max_events,block_events,pressure,strength):
    generators=_generators(dates,ticker,seed,32);rate=dict(scenario.external_event_rates).get(ticker,scenario.event_rate)
    def source(book):
        elapsed=np.zeros(len(dates));hidden=pressure.copy()
        for start in range(0,max_events,block_events):
            size=min(block_events,max_events-start);draw=np.stack([rng.random((size,17)) for rng in generators])
            for step in range(size):
                u=draw[:,step];elapsed+=-np.log1p(-u[:,16])/rate
                kind,buy,quantity,price,ioc,hidden=_order_fields(book,u,scenario,hidden,strength,ticker)
                yield TimedOrders(elapsed.copy(),kind,buy,quantity,price,ioc)
    return source


def simulate_matching_panel(tickers,dates,scenario,*,seed=17,future_seed=29,max_wait_seconds=60,max_events=2048,block_events=32):
    _wait(max_wait_seconds,target_kind='first_trade_change_before_deadline');universe=validate_tickers(tickers);dates=pd.DatetimeIndex(dates)
    if not isinstance(scenario,MatchingScenario) or not len(dates) or dates.hasnans or dates.has_duplicates or dates.tz is not None or not dates.equals(dates.normalize()):raise ValueError('Declare a matching scenario and distinct naive dates')
    if any(type(s) is not int or not 0<=s<2**32 for s in (seed,future_seed)):raise ValueError('Declare bounded integer seeds')
    if type(max_events) is not int or not 1<=max_events<=8192 or type(block_events) is not int or not 1<=block_events<=256:raise ValueError('Declare bounded matching event budgets')
    dates=dates.sort_values();N=len(dates);decision=dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10)
    common=_draws(dates,'synthetic_matching_clock',seed,0,1)[:,0]
    market_pressure=np.where(common[:,1]<.5,-1,1);outage=common[:,0]<scenario.queue.input_outage_fraction;frames=[]
    names=(*_feature_names(),'f_trade_location','f_trade_spread_ticks','f_trade_bid_depth_to_fill','f_trade_ask_depth_to_fill')
    for ticker in universe:
        is_index=asset_class(ticker)=='index'
        if is_index:
            features={c:np.full(N,np.nan) for c in names};bid=ask=last=np.full(N,np.nan);depths=np.full((N,2,5),np.nan);ready=np.zeros(N,bool)
            result=dict(resolved=np.zeros(N,bool),timeout=np.zeros(N,bool),actual_return=np.full(N,np.nan),anchor_trade_price=np.full(N,np.nan),first_change_price=np.full(N,np.nan),
                event_after_emission_seconds=np.full(N,np.nan),first_change_fill_rank=np.full(N,np.nan),pre_emission_events=np.zeros(N,int),pre_emission_price_changes=np.zeros(N,int),events_applied=np.zeros(N,int),book_incomplete=np.zeros(N,bool))
        else:
            book,last,pressure,strength,features,depths=_history(dates,ticker,scenario,seed,market_pressure)
            bid=book.bid_price;ask=book.ask_price;ready=book.complete&~outage
            result=first_trade_change(book,last,_future_source(dates,ticker,scenario,future_seed,max_events,block_events,pressure,strength),latency_seconds=scenario.latency_seconds,max_wait_seconds=max_wait_seconds)
        duration=result['event_after_emission_seconds'];hit=np.isfinite(duration)
        ns=np.ceil(np.where(hit,np.clip(duration,1e-9,max_wait_seconds),max_wait_seconds)*1e9).astype('int64')
        first=(decision+pd.to_timedelta(ns,unit='ns')).where(hit)
        frame=pd.DataFrame(features);frame.loc[~ready,:]=np.nan
        frame=frame.assign(ticker=ticker,date=dates,decision_at=decision,prediction_at=decision,
            max_input_available_at=decision-pd.Timedelta(seconds=scenario.latency_seconds),target_at=decision+pd.Timedelta(seconds=max_wait_seconds),
            label_available_at=decision+pd.Timedelta(seconds=max_wait_seconds,milliseconds=5),first_change_at=first,first_change_price=result['first_change_price'],
            actual_return=result['actual_return'],timeout=result['timeout'],simulation_resolved=result['resolved'],input_ready=ready,
            observed_bid=np.where(ready,bid,np.nan),observed_ask=np.where(ready,ask,np.nan),observed_last_trade=np.where(ready,last,np.nan),
            bid_depth=np.where(ready,depths[:,0,0],np.nan),ask_depth=np.where(ready,depths[:,1,0],np.nan),
            diagnostic_anchor_trade_price=result['anchor_trade_price'],first_change_fill_rank=result['first_change_fill_rank'],
            diagnostic_pre_emission_events=result['pre_emission_events'],diagnostic_pre_emission_price_changes=result['pre_emission_price_changes'],
            diagnostic_events_applied=result['events_applied'],diagnostic_book_incomplete=result['book_incomplete'],
            max_wait_seconds=max_wait_seconds,target_kind='first_trade_change_before_deadline',source='synthetic_multilevel_matching',
            price_basis='index_no_transaction_target' if is_index else 'synthetic_ordered_level_trade_ticks',history_window_unit='events',
            event_rate=dict(scenario.external_event_rates).get(ticker,scenario.event_rate),latency_seconds=scenario.latency_seconds)
        frame.loc[~ready,'max_input_available_at']=pd.NaT;frames.append(frame)
    return pd.concat(frames,ignore_index=True).sort_values(['date','ticker']).reset_index(drop=True)
