"""Discrete queue forecasts with Poisson event clocks and pre-emission resets.

Rates, lot-size weights and queue rules are counterfactual, not TWSE estimates.
History windows count events. Targets always begin after the common emission.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from .first_change import TARGET,_wait
from .intraday_models import SharedIntradayModel
from .intraday_queue import QueueScenario,_draws,_queue_history,_step
from .universe import TRAINING_TICKERS,asset_class,tickers as validate_tickers


def _number(value,low,high):
    return not isinstance(value,bool) and isinstance(value,(float,int)) and np.isfinite(value) and low<=value<=high


def _weights(values):
    if (not isinstance(values,(list,tuple)) or len(values)!=4 or not all(_number(v,0,1) for v in values)
            or not np.isclose(sum(values),1.,rtol=0,atol=1e-12)):raise ValueError('Four nonnegative lot-size weights must sum to one')
    return tuple(values)


@dataclass(frozen=True)
class QueueFirstScenario:
    name:str
    queue:QueueScenario|dict|None=None
    event_rate:float=100.
    latency_seconds:float=.005
    size_weights:tuple[float,...]=(.6,.2,.1,.1)
    external_event_rates:tuple[tuple[str,float],...]=()
    external_size_weights:tuple[tuple[str,tuple[float,...]],...]=()

    def __post_init__(self):
        if not isinstance(self.name,str) or not 1<=len(self.name)<=80:raise ValueError('Declare a bounded queue-first scenario name')
        q=self.queue
        if q is None:q=QueueScenario(self.name,flow_imbalance=0.)
        elif isinstance(q,dict):q=QueueScenario(**dict({'name':self.name},**q))
        if not isinstance(q,QueueScenario):raise ValueError('Declare queue physics')
        if not _number(self.event_rate,.01,10000) or not _number(self.latency_seconds,0,5):raise ValueError('Invalid event rate or latency')
        object.__setattr__(self,'queue',q);object.__setattr__(self,'size_weights',_weights(self.size_weights))
        for field in ('external_event_rates','external_size_weights'):
            values=getattr(self,field)
            if not isinstance(values,(list,tuple)) or any(not isinstance(p,(list,tuple)) or len(p)!=2 for p in values):raise ValueError('Declare external ticker/value pairs')
            pairs=tuple((str(t),_weights(v) if field=='external_size_weights' else v) for t,v in values)
            if pairs:
                ids=validate_tickers([t for t,_ in pairs])
                if len(ids)!=len(pairs) or set(ids)&set(TRAINING_TICKERS):raise ValueError('Overrides require distinct external targets')
                if field=='external_event_rates' and any(not _number(v,.01,10000) for _,v in pairs):raise ValueError('Invalid external event rate')
            object.__setattr__(self,field,pairs)


def advance_first_change(state,draw_blocks,*,event_rate,latency_seconds,max_wait_seconds,flip_hazard,size_weights):
    """Keep processing resets before emission; label the first later move only.

    A finite iterator is an explicit compute budget. Unfinished paths remain
    unknown, never converted into verified timeouts or removed from the grid.
    """
    return _advance_queue_price(state,draw_blocks,event_rate=event_rate,latency_seconds=latency_seconds,
        max_wait_seconds=max_wait_seconds,flip_hazard=flip_hazard,size_weights=size_weights,price_kind='mid')


def _advance_queue_price(state,draw_blocks,*,event_rate,latency_seconds,max_wait_seconds,flip_hazard,size_weights,price_kind):
    _wait(max_wait_seconds);weights=_weights(size_weights)
    if price_kind not in ('mid','trade'):raise ValueError('Declare midquote or transaction event prices')
    if not _number(event_rate,.01,10000) or not _number(latency_seconds,0,5) or not _number(flip_hazard,0,1):raise ValueError('Invalid queue event-clock contract')
    queues=state.queues.copy();position=state.position.copy();pressure=state.pressure.copy();ema=state.flow_ema.copy()
    N=len(position);strength=np.broadcast_to(state.strength,(N,));response=np.broadcast_to(state.response,(N,))
    elapsed=np.zeros(N);resolved=np.zeros(N,bool);timeout=np.zeros(N,bool);anchored=np.zeros(N,bool)
    price=position.copy() if price_kind=='mid' else np.asarray(state.last_trade_price,dtype=float).copy()
    if price.shape!=(N,) or not np.isfinite(price).all():raise ValueError('Transaction paths require an observed starting last trade')
    anchor=price.copy();direction=np.full(N,np.nan);event=np.full(N,np.nan)
    pre_events=np.zeros(N,int);pre_moves=np.zeros(N,int);examined=np.zeros(N,int);applied=np.zeros(N,int)
    for block in draw_blocks:
        draw=np.asarray(block)
        if draw.ndim!=3 or draw.shape[0]!=N or draw.shape[2]!=17 or not np.isfinite(draw).all() or (draw<0).any() or (draw>=1).any():raise ValueError('Declare finite independent17-coordinate event draws')
        for j in range(draw.shape[1]):
            ids=np.flatnonzero(~resolved)
            if not len(ids):break
            values=draw[ids,j];next_at=elapsed[ids]-np.log1p(-values[:,16])/event_rate;examined[ids]+=1
            beyond=next_at>latency_seconds+max_wait_seconds
            late_ids=ids[beyond];resolved[late_ids]=True;timeout[late_ids]=True;direction[late_ids]=0.;anchor[late_ids]=price[late_ids]
            ids=ids[~beyond];values=values[~beyond];next_at=next_at[~beyond]
            if not len(ids):continue
            after=next_at>latency_seconds
            first=after&~anchored[ids];anchor[ids[first]]=price[ids[first]];anchored[ids[first]]=True
            before=position[ids].copy();before_price=price[ids].copy();local_q=queues[ids].copy();local_p=before.copy()
            press,flow=_step(local_q,local_p,pressure[ids],values[:,:16],strength[ids],flip_hazard,ema[ids],response[ids],weights)
            queues[ids]=local_q;position[ids]=local_p;pressure[ids]=press;ema[ids]=flow;elapsed[ids]=next_at;applied[ids]+=1
            if price_kind=='trade':
                buying=values[:,1]<(.5+.5*strength[ids]*press)
                price[ids]=np.where(values[:,2]<.65,before+np.where(buying,.5,-.5),before_price)
            else:price[ids]=local_p
            moved=price[ids]!=before_price;pre_events[ids[~after]]+=1;pre_moves[ids[~after&moved]]+=1
            hit=after&moved;hit_ids=ids[hit];direction[hit_ids]=np.sign(price[hit_ids]-before_price[hit]);event[hit_ids]=next_at[hit]-latency_seconds;resolved[hit_ids]=True
        if resolved.all():break
    return dict(resolved=resolved,timeout=timeout,direction=direction,event_after_emission_seconds=event,anchor_position=anchor,
        pre_emission_events=pre_events,pre_emission_price_changes=pre_moves,events_applied=applied,event_draws_examined=examined)


def _blocks(dates,ticker,seed,max_events,block_events):
    identity=int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4],'big')
    generators=[np.random.default_rng(np.random.SeedSequence([seed,identity,int(date.toordinal()),21])) for date in dates]
    for start in range(0,max_events,block_events):
        size=min(block_events,max_events-start)
        yield np.stack([rng.random((size,17)) for rng in generators])


def simulate_queue_first_change_panel(tickers,dates,scenario,*,seed=17,future_seed=29,max_wait_seconds=60,max_events=2048,block_events=32):
    return _simulate_queue_price_panel(tickers,dates,scenario,seed=seed,future_seed=future_seed,max_wait_seconds=max_wait_seconds,
        max_events=max_events,block_events=block_events,price_kind='mid')


def _simulate_queue_price_panel(tickers,dates,scenario,*,seed,future_seed,max_wait_seconds,max_events,block_events,price_kind):
    _wait(max_wait_seconds);universe=validate_tickers(tickers);dates=pd.DatetimeIndex(dates)
    if not isinstance(scenario,QueueFirstScenario) or not len(dates) or dates.hasnans or dates.has_duplicates or dates.tz is not None or not dates.equals(dates.normalize()):raise ValueError('Declare a scenario and distinct naive dates')
    if any(type(s) is not int or not 0<=s<2**32 for s in (seed,future_seed)):raise ValueError('Declare bounded integer seeds')
    if type(max_events) is not int or not 1<=max_events<=8192 or type(block_events) is not int or not 1<=block_events<=256:raise ValueError('Declare bounded event budgets and blocks')
    dates=dates.sort_values();decision=dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10)
    cutoff=decision-pd.Timedelta(seconds=scenario.latency_seconds);qspec=scenario.queue
    common=_draws(dates,'synthetic_market_clock',seed,0,1)[:,0];market_pressure=np.where(common[:,1]<.5,-1,1)
    outage=common[:,0]<qspec.input_outage_fraction;frames=[]
    for ticker in universe:
        rate=dict(scenario.external_event_rates).get(ticker,scenario.event_rate)
        sizes=dict(scenario.external_size_weights).get(ticker,scenario.size_weights)
        is_index=asset_class(ticker)=='index'
        if is_index:
            values=_draws(dates,ticker,future_seed,23,1)[:,0];wait=-np.log1p(-values[:,0])/rate;hit=wait<=max_wait_seconds
            result=dict(resolved=np.ones(len(dates),bool),timeout=~hit,direction=np.where(hit,np.where(values[:,1]<.5,1.,-1.),0.),
                event_after_emission_seconds=np.where(hit,wait,np.nan),anchor_position=np.zeros(len(dates),int),pre_emission_events=np.zeros(len(dates),int),
                pre_emission_price_changes=np.zeros(len(dates),int),events_applied=np.zeros(len(dates),int),event_draws_examined=np.zeros(len(dates),int))
            if price_kind=='trade':
                result.update(resolved=np.zeros(len(dates),bool),timeout=np.zeros(len(dates),bool),direction=np.full(len(dates),np.nan),event_after_emission_seconds=np.full(len(dates),np.nan),anchor_position=np.full(len(dates),np.nan))
            features={c:np.full(len(dates),np.nan) for c in _feature_names()};bid=ask=np.full(len(dates),np.nan)
        else:
            state=_queue_history(dates,ticker,qspec,seed,market_pressure,sizes,record_trades=price_kind=='trade')
            bid=state.queues[:,0,0].copy();ask=state.queues[:,1,0].copy();features={}
            for level in range(1,6):
                features[f'f_book_imbalance_{level}']=state.columns[f'f_book_imbalance_{level}']
                features[f'f_book_relative_depth_{level}']=np.expm1(state.columns[f'f_book_log_depth_{level}'])/np.expm1(state.columns['f_book_log_depth_5'])
            for window in (5,20,100):
                for level in (1,5):
                    for stat in ('mean','change'):features[f'f_event_imbalance_{level}_{stat}_{window}']=state.columns[f'f_book_imbalance_{level}_{stat}_{window}']
                for stat in ('change','vol'):features[f'f_event_mid_{stat}_{window}']=state.columns[f'f_book_mid_{stat}_{window}']
            result=_advance_queue_price(state,_blocks(dates,ticker,future_seed,max_events,block_events),event_rate=rate,latency_seconds=scenario.latency_seconds,
                max_wait_seconds=max_wait_seconds,flip_hazard=qspec.flip_rate,size_weights=sizes,price_kind=price_kind)
        if price_kind=='trade':features['f_trade_location']=np.full(len(dates),np.nan) if is_index else 2*(state.last_trade_price-state.position)
        ready=~outage if not is_index else np.zeros(len(dates),bool)
        duration=result['event_after_emission_seconds'];hit=np.isfinite(duration)
        ns=np.ceil(np.where(hit,np.clip(duration,1e-9,max_wait_seconds),max_wait_seconds)*1e9).astype('int64')
        event=(decision+pd.to_timedelta(ns,unit='ns')).where(hit)
        actual=result['direction']/(10000.+result['anchor_position'])
        frame=pd.DataFrame(features);frame.loc[~ready,:]=np.nan
        frame=frame.assign(ticker=ticker,date=dates,decision_at=decision,prediction_at=decision,max_input_available_at=cutoff,
            target_at=decision+pd.Timedelta(seconds=max_wait_seconds),label_available_at=decision+pd.Timedelta(seconds=max_wait_seconds,milliseconds=5),
            first_change_at=event,actual_return=actual,timeout=result['timeout'],simulation_resolved=result['resolved'],input_ready=ready,
            bid_depth=np.where(ready,bid,np.nan),ask_depth=np.where(ready,ask,np.nan),max_wait_seconds=max_wait_seconds,
            target_kind=TARGET if price_kind=='mid' else 'first_trade_change_before_deadline',
            source='synthetic_poisson_queue_first_change' if price_kind=='mid' else 'synthetic_poisson_queue_trade_first_change',
            price_basis=('abstract_index_points_no_book' if is_index else 'abstract_first_post_emission_mid_move') if price_kind=='mid' else ('index_no_transaction_target' if is_index else 'abstract_first_post_emission_trade_move'),
            diagnostic_anchor_position=result['anchor_position'],diagnostic_pre_emission_events=result['pre_emission_events'],
            diagnostic_pre_emission_price_changes=result['pre_emission_price_changes'],diagnostic_events_applied=result['events_applied'],
            diagnostic_event_draws_examined=result['event_draws_examined'],event_rate=rate,latency_seconds=scenario.latency_seconds,history_window_unit='events')
        if price_kind=='trade':frame=frame.rename(columns={'diagnostic_anchor_position':'diagnostic_anchor_trade_price'})
        frame.loc[~ready,'max_input_available_at']=pd.NaT;frames.append(frame)
    return pd.concat(frames,ignore_index=True).sort_values(['date','ticker']).reset_index(drop=True)


def _feature_names():
    names=[f'f_book_{kind}_{i}' for kind in ('imbalance','relative_depth') for i in range(1,6)]
    for window in (5,20,100):
        names.extend(f'f_event_imbalance_{i}_{stat}_{window}' for i in (1,5) for stat in ('mean','change'))
        names.extend(f'f_event_mid_{stat}_{window}' for stat in ('change','vol'))
    return names


def _event_frame(frame):
    names=_feature_names()
    if 'history_window_unit' not in frame or not frame.history_window_unit.eq('events').all():raise ValueError('History windows must be declared in events')
    if not set(names)<=set(frame):raise ValueError('Event-history model needs the declared observable event profile')
    result=frame.drop(columns=[c for c in frame if c.startswith(('f_book_','f_event_'))]).copy()
    for name in names:
        target=name.replace('f_event_','f_book_event_',1) if name.startswith('f_event_') else name
        result[target]=pd.to_numeric(frame[name],errors='coerce').round(12)
    return result


class EventHistoryModel:
    def __init__(self,frame,seconds):
        self.inner=SharedIntradayModel(_event_frame(frame),'history_logistic');self.prior=self.inner.prior
        implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.version=f'queue-first-{seconds}-{implementation[:12]}-{self.inner.version}'
        self.training_details=dict(self.inner.training_details,model='event_history_logistic',target_kind=TARGET,max_wait_seconds=seconds,
            history_window_unit='events',queue_first_implementation_sha256=implementation,hidden_state_used=False,
            labels='up/down plus timeout; emitted up/down probabilities renormalized; timeout earns no binary credit',
            timeout_training_rows=self.inner.training_details['flat_training_rows'])
        self.training_details.pop('flat_training_rows')

    def predict_proba(self,frame):return self.inner.predict_proba(_event_frame(frame))
