"""Privileged-state bounds for an artificial process; no deployable forecasts.

Knowing the complete simulator Markov state can only improve the best decision.
Finite-branch maxima are upward biased by Jensen, so their population mean plus
a date-level Hoeffding radius is an upper bound, not an achieved accuracy.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
import platform
import re

import numpy as np
import pandas as pd

from .intraday_queue import QueueScenario,_draws,_queue_history,_step
from .universe import asset_class,tickers as validate_tickers
from .direction_audit import load_membership_snapshot
from .workflows import source_digest,write_json


def _calendar(dates):
    dates=pd.DatetimeIndex(dates)
    if (not len(dates) or dates.hasnans or dates.has_duplicates or dates.tz is not None
            or not dates.equals(dates.normalize())):
        raise ValueError('Declare distinct timezone-naive daily state dates')
    return dates.sort_values()


def _horizons(values):
    if (not isinstance(values,(list,tuple)) or not values or any(type(h) is not int or h not in (1,5,30,60) for h in values)
            or len(set(values))!=len(values)):
        raise ValueError('Declare distinct fixed 1/5/30/60-second horizons')
    return sorted(values)


def queue_ceiling_panel(tickers,dates,scenario,*,seed=17,branch_seed=73,branches=64,
                        horizons_seconds=(1,5,30,60),chunk_dates=64):
    """Fork complete hidden current states into independent phase3 futures.

    Phase2 (the original held-out endpoint) is never drawn or read. Hidden
    pressure, strength, response, EMA and all queues are diagnostic privileges.
    Rows contain counts only and cannot be submitted as trained predictions.
    """
    universe=validate_tickers(tickers);dates=_calendar(dates);horizons=_horizons(horizons_seconds)
    if any(asset_class(t)=='index' for t in universe):raise ValueError('No artificial index book in queue ceiling diagnostics')
    if not isinstance(scenario,QueueScenario):raise ValueError('Declare an explicit queue scenario')
    if any(type(s) is not int or not 0<=s<2**32 for s in (seed,branch_seed)):raise ValueError('Declare bounded integer random seeds')
    if type(branches) is not int or not 4<=branches<=1024 or branches%2:raise ValueError('Declare an even branch count in [4,1024]')
    if type(chunk_dates) is not int or not 1<=chunk_dates<=256:raise ValueError('Declare bounded date chunks')
    common=_draws(dates,'synthetic_market_clock',seed,0,1)[:,0]
    market_pressure=np.where(common[:,1]<.5,-1,1)
    split=branches//2;frames=[]
    for ticker in universe:
        state=_queue_history(dates,ticker,scenario,seed,market_pressure)
        response=np.broadcast_to(state.response,(len(dates),))
        fingerprints=[]
        for i in range(len(dates)):
            raw=np.concatenate([state.queues[i].ravel(),[state.position[i],state.pressure[i],state.strength[i],response[i],state.flow_ema[i]]]).astype('<f8').tobytes()
            fingerprints.append(hashlib.sha256(raw).hexdigest())
        identity=int.from_bytes(hashlib.sha256(ticker.encode()).digest()[:4],'big')
        for start in range(0,len(dates),chunk_dates):
            end=min(start+chunk_dates,len(dates));size=end-start
            # Branch-first arrays preserve existing paths when branches expand.
            draws=np.stack([np.random.default_rng(np.random.SeedSequence([branch_seed,identity,int(date.toordinal()),3])).random((branches,60,16)) for date in dates[start:end]])
            draws=draws.transpose(2,0,1,3).reshape(60,size*branches,16).copy()
            queues=np.repeat(state.queues[start:end],branches,axis=0)
            position=np.zeros(size*branches,dtype=int)
            pressure=np.repeat(state.pressure[start:end],branches)
            strength=np.repeat(state.strength[start:end],branches)
            local_response=np.repeat(response[start:end],branches)
            ema=np.repeat(state.flow_ema[start:end],branches)
            for step in range(max(horizons)):
                pressure,ema=_step(queues,position,pressure,draws[step],strength,scenario.flip_rate,ema,local_response)
                horizon=step+1
                if horizon not in horizons:continue
                outcome=position.reshape(size,branches)
                rows=dict(ticker=ticker,date=dates[start:end],horizon_seconds=horizon,branches=branches,
                    state_sha256=fingerprints[start:end],input_ready=common[start:end,0]>=scenario.input_outage_fraction)
                for prefix,values in [('pilot',outcome[:,:split]),('eval',outcome[:,split:])]:
                    for label,mask in [('up',values>0),('down',values<0),('flat',values==0)]:rows[f'{prefix}_{label}']=mask.sum(axis=1)
                frames.append(pd.DataFrame(rows))
    return pd.concat(frames,ignore_index=True).sort_values(['date','ticker','horizon_seconds']).reset_index(drop=True)


def summarize_queue_ceiling(counts,tickers,dates,*,family_size,alpha=.05):
    """Simultaneous date-level population bounds for both scoring definitions.

    Two targets times two sides use delta=alpha/(4*family_size). Dependence
    between stocks/horizons is allowed by a union bound; dates must be generated
    independently. No bound here describes unknown market dynamics.
    """
    universe=validate_tickers(tickers);dates=_calendar(dates)
    needed={'ticker','date','horizon_seconds','branches',*[f'{p}_{c}' for p in ('pilot','eval') for c in ('up','down','flat')]}
    if not needed<=set(counts):raise ValueError('Missing conditional outcome counts')
    frame=counts.copy();horizons=_horizons(sorted(frame.horizon_seconds.unique().tolist()))
    if type(family_size) is not int or family_size<len(universe)*len(horizons):raise ValueError('Declare the entire comparison family')
    if isinstance(alpha,bool) or not isinstance(alpha,(float,int)) or not 0<alpha<1:raise ValueError('Declare an error probability in (0,1)')
    expected=pd.MultiIndex.from_product([dates,universe,horizons],names=['date','ticker','horizon_seconds'])
    if (frame.duplicated(['date','ticker','horizon_seconds']).any() or len(frame)!=len(expected)
            or set(pd.MultiIndex.from_frame(frame[['date','ticker','horizon_seconds']]))!=set(expected)):
        raise ValueError('Conditional counts require the complete declared grid')
    numeric=frame[[c for c in needed if c not in {'ticker','date','horizon_seconds'}]].apply(pd.to_numeric,errors='coerce')
    frame[numeric.columns]=numeric
    if (not np.isfinite(numeric).all().all() or (numeric<0).any().any() or not numeric.eq(np.floor(numeric)).all().all()
            or not frame.branches.between(4,1024).all() or not frame.branches.mod(2).eq(0).all()
            or any(not frame[[f'{prefix}_{c}' for c in ('up','down','flat')]].sum(axis=1).eq(frame.branches/2).all() for prefix in ('pilot','eval'))):
        raise ValueError('Invalid independent pilot/evaluation counts')
    radius=float(np.sqrt(np.log(4*family_size/alpha)/(2*len(dates))))
    result={}
    for horizon in horizons:
        panel=frame[frame.horizon_seconds.eq(horizon)];stocks={}
        for ticker in universe:
            p=panel[panel.ticker.eq(ticker)];stock={}
            for target in ('strict','up_nonup'):
                pilot_other=p.pilot_down+(p.pilot_flat if target=='up_nonup' else 0)
                eval_other=p.eval_down+(p.eval_flat if target=='up_nonup' else 0)
                total_up=p.pilot_up+p.eval_up;total_other=pilot_other+eval_other
                plugin=float((np.maximum(total_up,total_other)/p.branches).mean())
                policy=float((np.where(p.pilot_up>=pilot_other,p.eval_up,eval_other)/(p.branches/2)).mean())
                stock[target]=dict(plugin_upper_point=plugin,split_policy_point=policy,
                    lower=max(0.,policy-radius),upper=min(1.,plugin+radius))
            stocks[ticker]=stock
        metrics={}
        for target in ('strict','up_nonup'):
            upper=sorted(s[target]['upper'] for s in stocks.values())
            metrics[target]=dict(minimum_stock_upper=upper[0],worst_five_upper=float(np.mean(upper[:5])),
                stocks_ruled_out_at75=sum(s[target]['upper']<.75 for s in stocks.values()),
                any_five_mean75_ruled_out=bool(np.mean(upper[:5])<.75))
        result[str(horizon)]=dict(states=len(panel),state_dates=len(dates),hoeffding_radius=radius,per_stock=stocks,metrics=metrics)
    return dict(evidence_kind='synthetic_privileged_state_diagnostic',privileged_state_used=True,
        models_trained=0,forecast_produced=False,target_feasibility_established=False,
        empirical_target_met=False,market_calibrated=False,family_size=family_size,alpha=alpha,horizons=result,
        limitations=['Complete simulator state is unavailable to deployable predictors.',
            'Finite-branch maximum is upward biased; it is not achieved forecast accuracy.',
            'Bounds assume independent generated dates and the declared artificial law.',
            'An upper bound above75 does not establish existence of a usable75pct method.'])


def run_queue_ceiling(config,membership_path,output):
    required={'state_dates','calendar_offset','branches','seeds','branch_seed','horizons_seconds','scenarios','chunk_dates','alpha'}
    if not isinstance(config,dict) or set(config)!=required:raise ValueError('Declare the complete queue-ceiling configuration')
    if type(config['state_dates']) is not int or not 126<=config['state_dates']<=10000:raise ValueError('Declare126to10000independent state dates')
    if type(config['calendar_offset']) is not int or not 0<=config['calendar_offset']<=10000:raise ValueError('Invalid synthetic calendar offset')
    seeds=config['seeds'];specs=config['scenarios'];horizons=_horizons(config['horizons_seconds'])
    if not isinstance(seeds,list) or not 1<=len(seeds)<=20 or any(type(s) is not int or not 0<=s<2**32 for s in seeds) or len(set(seeds))!=len(seeds):raise ValueError('Declare distinct bounded seeds')
    if not isinstance(specs,list) or not 1<=len(specs)<=20:raise ValueError('Declare bounded queue scenarios')
    scenarios=[QueueScenario(**spec) for spec in specs]
    if len({s.name for s in scenarios})!=len(scenarios) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',s.name) for s in scenarios):raise ValueError('Declare distinct path-safe scenario names')
    members=load_membership_snapshot(membership_path)
    if len(members['tickers'])!=50:raise ValueError('Queue ceiling requires all50declared constituents')
    overrides=lambda s:set(s.weak_tickers)|{t for t,_ in s.external_persistent_fractions}|{t for t,_ in s.external_liquidity_weights}
    if any(overrides(s)-set(members['tickers']) for s in scenarios):raise ValueError('Overrides must belong to the declared constituents')
    output=Path(output)
    if output.exists():raise ValueError('Queue ceiling requires a fresh output directory')
    dates=pd.bdate_range('2020-01-02',periods=config['calendar_offset']+config['state_dates'])[-config['state_dates']:]
    family=50*len(scenarios)*len(seeds)*len(horizons)
    protocol=dict(config=config,expanded_scenarios=[asdict(s) for s in scenarios],membership=members,
        config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),source_sha256=source_digest(),
        privileged_state_used=True,models_trained=0,forecast_produced=False,target_feasibility_established=False,
        family_size=family,random_stream='Independent phase3; phase2 actual endpoints never drawn or read.',
        state_information='All queues,current pressure,strength,response,flow EMA; complete known artificial transition law.',
        synthetic_calendar='Weekdays from2020-01-02 with explicit offset; not exchange history.',
        bound='Jensen upward maximum plus date-level Hoeffding; independent split-policy lower; two targets times two sides.',
        environment=dict(python=platform.python_version(),packages={n:version(n) for n in ('numpy','pandas','pyarrow')}))
    output.mkdir(parents=True,exist_ok=False);write_json(output/'protocol.json',protocol)
    runs=[]
    for scenario in scenarios:
        for seed in seeds:
            run_id=f'{scenario.name}_seed{seed}'
            counts=queue_ceiling_panel(members['tickers'],dates,scenario,seed=seed,branch_seed=config['branch_seed'],
                branches=config['branches'],horizons_seconds=horizons,chunk_dates=config['chunk_dates'])
            summary=summarize_queue_ceiling(counts,members['tickers'],dates,family_size=family,alpha=config['alpha'])
            path=Path(run_id)/'counts.parquet';(output/run_id).mkdir();counts.to_parquet(output/path,index=False)
            run=dict(run_id=run_id,scenario=asdict(scenario),seed=seed,summary=summary,
                counts=str(path),counts_sha256=hashlib.sha256((output/path).read_bytes()).hexdigest())
            write_json(output/run_id/'report.json',run);runs.append(run)
            write_json(output/'progress.json',dict(completed=len(runs),last_run=run_id))
    if source_digest()!=protocol['source_sha256']:raise RuntimeError('Source changed during ceiling diagnostic')
    report=dict(evidence_kind='synthetic_privileged_state_diagnostic',experiments=runs,models_trained=0,
        forecast_produced=False,target_feasibility_established=False,empirical_target_met=False,market_calibrated=False)
    write_json(output/'report.json',report)
    return report
