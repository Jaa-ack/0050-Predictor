"""Fixed-ten first-change study with explicitly timed discrete queues."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from .direction_audit import load_membership_snapshot
from .first_change import MODELS,EXTRA_MODELS,_wait,fit_first_change_model,audit_first_change_predictions
from .queue_first_change import QueueFirstScenario,simulate_queue_first_change_panel
from .universe import TRAINING_TICKERS,DEFAULT_EVALUATION_TICKERS,asset_class
from .workflows import source_digest,write_json


def run_queue_first_study(config,membership_path,output):
    return _run_queue_event_study(config,membership_path,output,event_kind='mid')


def run_trade_first_study(config,membership_path,output):
    return _run_queue_event_study(config,membership_path,output,event_kind='trade')


def _run_queue_event_study(config,membership_path,output,*,event_kind):
    if event_kind=='trade':
        from .trade_first_change import MODELS as allowed,fit_trade_first_model as fit,audit_trade_first_predictions as audit,simulate_trade_first_panel as simulate,TARGET as target
    else:
        from .first_change import TARGET as target
        allowed=(*MODELS,*EXTRA_MODELS);fit=fit_first_change_model;audit=audit_first_change_predictions;simulate=simulate_queue_first_change_panel
    evidence='synthetic_poisson_queue_trade_first_change' if event_kind=='trade' else 'synthetic_poisson_queue_first_change'
    required={'train_dates','calibration_dates','test_dates','seeds','max_wait_seconds','scenarios','candidates'}
    if not isinstance(config,dict) or not required<=set(config)<=required|{'max_events'}:raise ValueError('Declare queue-first study fields')
    for key,minimum in [('train_dates',30),('calibration_dates',10),('test_dates',126)]:
        if type(config[key]) is not int or not minimum<=config[key]<=10000:raise ValueError('Invalid queue-first split lengths')
    seeds=config['seeds'];candidates=config['candidates'];specs=config['scenarios'];wait=config['max_wait_seconds'];_wait(wait)
    if not isinstance(seeds,list) or not 1<=len(seeds)<=20 or any(type(s) is not int or not 0<=s<2**32 for s in seeds) or len(set(seeds))!=len(seeds):raise ValueError('Declare distinct bounded seeds')
    if not isinstance(candidates,list) or not candidates or len(set(candidates))!=len(candidates) or not set(candidates)<=set(allowed):raise ValueError('Declare distinct supported first-change candidates')
    if not isinstance(specs,list) or not 1<=len(specs)<=24:raise ValueError('Declare bounded queue-first scenarios')
    scenarios=[QueueFirstScenario(**spec) for spec in specs]
    if len({s.name for s in scenarios})!=len(scenarios) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',s.name) for s in scenarios):raise ValueError('Declare distinct path-safe scenario names')
    max_events=config.get('max_events',2048)
    if type(max_events) is not int or not 1<=max_events<=8192:raise ValueError('Declare a bounded simulation event budget')
    membership=load_membership_snapshot(membership_path);members=membership['tickers']
    if len(members)!=50:raise ValueError('Queue-first research requires all50constituents')
    for s in scenarios:
        overrides={t for t,_ in s.external_event_rates}|{t for t,_ in s.external_size_weights}|set(s.queue.weak_tickers)|{t for t,_ in s.queue.external_persistent_fractions}|{t for t,_ in s.queue.external_liquidity_weights}
        if overrides-set(members) or set(s.queue.weak_tickers)&set(TRAINING_TICKERS):raise ValueError('Transfer interventions must stay within external constituents')
    output=Path(output)
    if output.exists():raise ValueError('Queue-first research requires a fresh output directory')
    universe=tuple(dict.fromkeys((*TRAINING_TICKERS,*members,*DEFAULT_EVALUATION_TICKERS)))
    dates=pd.bdate_range('2020-01-02',periods=sum(config[k] for k in ('train_dates','calibration_dates','test_dates')))
    train_dates=dates[:config['train_dates']];cal_dates=dates[config['train_dates']:config['train_dates']+config['calibration_dates']];test_dates=dates[-config['test_dates']:]
    fit_at=cal_dates[0].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10,seconds=-1);selection_at=test_dates[0].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10,seconds=-1)
    protocol=dict(config=config,expanded_scenarios=[asdict(s) for s in scenarios],membership=membership,
        source_sha256=source_digest(),config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
        training_tickers=list(TRAINING_TICKERS),evaluation_tickers=list(universe),candidates=candidates,fit_at=fit_at.isoformat(),selection_at=selection_at.isoformat(),threshold=.5,
        evidence_kind=evidence,target_kind=target,history_window_unit='events',prediction_target_starts_at='emission',target_scope_confirmed=True,
        market_calibrated=False,synthetic_calendar='Weekdays from2020-01-02; not exchange history',max_future_events=max_events,
        assumptions=['Five integer queues,100past events,65pct market removal,15pct cancellation,20pct insertion; one-tick reset on depletion.',
            'Independent exponential waits; named event rates are per second; pressure flip hazard and all history windows are per event.',
            'Separate17th future coordinate controls time, independent of all16queue-transition coordinates.',
            'All pre-emission events and resets are processed; labels start at emission, never at the stale input quote.',
            'Size weights, rates, liquidity response and delays are counterfactual; no TWSE calibration or actual emission measurement.',
            'Budget-exhausted paths remain unknown; they cannot become timeouts or be silently removed.'],
        environment=dict(python=platform.python_version(),packages={n:version(n) for n in ('numpy','pandas','scipy','scikit-learn','pyarrow','threadpoolctl')}))
    if event_kind=='trade':protocol['assumptions']+=['A market event executes once at its pre-reset best quote; any quantity beyond that queue is canceled, not swept across levels.',
        'Warmup starts with a declared synthetic bid/ask transaction. Last trade updates on each market event; same-price prints are not changes.',
        'The last trade at actual emission anchors the target, including all unobserved pre-emission prints.',
        'Indices have no transaction prints: keep the shared prior query and unknown answer without fabricating an index trade.']
    output.mkdir(parents=True,exist_ok=False);write_json(output/'protocol.json',protocol);runs=[]
    for scenario in scenarios:
        for seed in seeds:
            frame=simulate(universe,dates,scenario,seed=seed,future_seed=seed^0x5A5A,max_wait_seconds=wait,max_events=max_events)
            primary=frame.ticker.isin(TRAINING_TICKERS);train=frame[primary&frame.date.isin(train_dates)];cal=frame[primary&frame.date.isin(cal_dates)];test=frame[frame.date.isin(test_dates)].copy()
            if not train.simulation_resolved.all() or not cal.simulation_resolved.all():raise RuntimeError('Unresolved primary labels: retain the frozen protocol and increase a separately declared compute budget')
            if not cal.label_available_at.lt(selection_at).all():raise ValueError('Calibration labels must mature before selection')
            models={};scores={};audits={}
            with threadpool_limits(limits=1):
                for name in candidates:
                    model=fit(train,name,fit_at=fit_at);models[name]=model
                    probability=model.predict_proba(cal)[:,1];correct=cal.actual_return.ne(0)&cal.actual_return.gt(0).eq(probability>=.5)
                    scores[name]=dict(minimum_stock_accuracy=float(correct.groupby(cal.ticker).mean().min()),overall_accuracy=float(correct.mean()),rows=len(cal),per_stock=correct.groupby(cal.ticker).mean().to_dict())
                chosen=max(candidates,key=lambda n:(scores[n]['minimum_stock_accuracy'],scores[n]['overall_accuracy'],-candidates.index(n)))
                p=test.copy();p['max_input_available_at']=p.max_input_available_at.fillna(selection_at)
                for name in candidates:
                    p[f'probability_up_{name}']=models[name].predict_proba(test)[:,1]
                    rows=p.assign(probability_up=p[f'probability_up_{name}'],decision_threshold=.5,model_version=models[name].version)
                    audits[name]=audit(rows[rows.ticker.isin(members)],members,expected_decisions=test_dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10),max_wait_seconds=wait)
            p['probability_up']=p[f'probability_up_{chosen}'];p['model_version']=models[chosen].version;p['decision_threshold']=.5
            run_id=f'{scenario.name}_seed{seed}';path=Path(run_id)/'predictions.parquet';(output/run_id).mkdir();p.to_parquet(output/path,index=False)
            transfer={}
            for ticker in DEFAULT_EVALUATION_TICKERS:
                q=p[p.ticker.eq(ticker)];correct=q.actual_return.notna()&q.actual_return.ne(0)&q.actual_return.gt(0).eq(q.probability_up.ge(.5))
                transfer[ticker]=dict(accuracy=float(correct.mean()),predictions=len(q),timeout_fraction=float(q.timeout.mean()),tradable=asset_class(ticker)!='index',used_for_training_calibration_selection=False)
                if event_kind=='trade':transfer[ticker]['unresolved_answers']=int(q.actual_return.isna().sum())
            r=dict(run_id=run_id,scenario=asdict(scenario),seed=seed,selected_model=chosen,training_rows=len(train),calibration_rows=len(cal),
                candidate_calibration=scores,training_details={n:m.training_details for n,m in models.items()},candidate_test_audits=audits,selected_test_audit=audits[chosen],
                predictions=str(path),prediction_sha256=hashlib.sha256((output/path).read_bytes()).hexdigest(),transfer=transfer,selection_uses_external_targets=False,
                unresolved_test_rows=int((~p.simulation_resolved).sum()),pre_emission_price_change_fraction=float(p.diagnostic_pre_emission_price_changes.gt(0).mean()))
            write_json(output/run_id/'report.json',r);runs.append(r);write_json(output/'progress.json',dict(completed=len(runs),last_run=run_id))
    if source_digest()!=protocol['source_sha256']:raise RuntimeError('Source changed during queue-first research')
    report=dict(evidence_kind=evidence,experiments=runs,models_trained=len(candidates)*len(runs),
        target_scope_confirmed=True,market_calibrated=False,target_feasibility_established=False,empirical_target_met=False)
    write_json(output/'report.json',report)
    return report
