"""Frozen exploratory first-event experiments; no endpoint-goal substitution."""
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
from .first_change import MODELS,FirstChangeScenario,simulate_first_change_panel,fit_first_change_model,audit_first_change_predictions,first_change_feasibility,_wait
from .universe import TRAINING_TICKERS,DEFAULT_EVALUATION_TICKERS,asset_class
from .workflows import source_digest,write_json


def run_first_change_study(config,membership_path,output):
    required={'train_dates','calibration_dates','test_dates','seeds','max_wait_seconds','scenarios'}
    if not isinstance(config,dict) or set(config)!=required:raise ValueError('Declare the complete first-change configuration')
    for key,minimum in [('train_dates',30),('calibration_dates',10),('test_dates',126)]:
        if type(config[key]) is not int or not minimum<=config[key]<=10000:raise ValueError('Invalid first-change split lengths')
    seeds=config['seeds'];waits=config['max_wait_seconds'];specs=config['scenarios']
    if not isinstance(seeds,list) or not 1<=len(seeds)<=20 or any(type(s) is not int or not 0<=s<2**32 for s in seeds) or len(set(seeds))!=len(seeds):raise ValueError('Declare distinct bounded seeds')
    if not isinstance(waits,list) or not waits or len(set(waits))!=len(waits):raise ValueError('Declare distinct maximum waits')
    for wait in waits:_wait(wait)
    if not isinstance(specs,list) or not 1<=len(specs)<=20:raise ValueError('Declare bounded first-change scenarios')
    scenarios=[FirstChangeScenario(**spec) for spec in specs]
    if len({s.name for s in scenarios})!=len(scenarios) or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',s.name) for s in scenarios):raise ValueError('Declare distinct path-safe scenario names')
    membership=load_membership_snapshot(membership_path);members=membership['tickers']
    if len(members)!=50:raise ValueError('First-change research requires all50constituents')
    if any(({t for t,_ in s.external_queue_shapes}|{t for t,_ in s.external_diffusion_scales})-set(members) for s in scenarios):raise ValueError('External overrides must belong to the constituent audit')
    output=Path(output)
    if output.exists():raise ValueError('First-change study requires a fresh output directory')
    universe=tuple(dict.fromkeys((*TRAINING_TICKERS,*members,*DEFAULT_EVALUATION_TICKERS)))
    dates=pd.bdate_range('2020-01-02',periods=sum(config[k] for k in ('train_dates','calibration_dates','test_dates')))
    train_dates=dates[:config['train_dates']];cal_dates=dates[config['train_dates']:config['train_dates']+config['calibration_dates']];test_dates=dates[-config['test_dates']:]
    fit_at=cal_dates[0].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10,seconds=-1)
    selection_at=test_dates[0].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10,seconds=-1)
    protocol=dict(config=config,expanded_scenarios=[asdict(s) for s in scenarios],membership=membership,
        config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),source_sha256=source_digest(),
        training_tickers=list(TRAINING_TICKERS),evaluation_tickers=list(universe),candidates=list(MODELS),fit_at=fit_at.isoformat(),selection_at=selection_at.isoformat(),
        target_kind='first_mid_change_before_deadline',target_scope_confirmed=True,market_calibrated=False,
        selection='fixed-ten minimum calibration accuracy, then overall, then declared order',threshold=.5,
        evidence_kind='synthetic_brownian_first_passage',calendar='Synthetic weekdays from2020-01-02, not exchange history',
        assumptions=['Initial independent Gamma(k,1/k) depths sampled at fixed10am, not conditioned on future events.',
            'Independent equal-variance zero-drift Brownian queues; first depletion moves the abstract mid quote one tick.',
            'Exact first hitting times via(q/(sigma*absZ))^2; future normals independent of initial depths.',
            'Timeouts earn zero directional credit; independent common feed outages use the shared training prior.',
            'Bounded first-event labels are a separate exploratory target, never a clock-endpoint score.'],
        environment=dict(python=platform.python_version(),packages={n:version(n) for n in ('numpy','pandas','scipy','scikit-learn','pyarrow','threadpoolctl')}))
    output.mkdir(parents=True,exist_ok=False);write_json(output/'protocol.json',protocol);runs=[]
    for scenario in scenarios:
        for seed in seeds:
            for wait in waits:
                frame=simulate_first_change_panel(universe,dates,scenario,seed=seed,future_seed=seed^0x5A5A,max_wait_seconds=wait)
                primary=frame.ticker.isin(TRAINING_TICKERS)
                train=frame[primary&frame.date.isin(train_dates)];cal=frame[primary&frame.date.isin(cal_dates)];test=frame[frame.date.isin(test_dates)].copy()
                if not cal.label_available_at.lt(selection_at).all():raise ValueError('Calibration labels must mature before selection')
                models={};scores={};audits={}
                with threadpool_limits(limits=1):
                    for name in MODELS:
                        model=fit_first_change_model(train,name,fit_at=fit_at);models[name]=model
                        probability=model.predict_proba(cal)[:,1];correct=cal.actual_return.ne(0)&cal.actual_return.gt(0).eq(probability>=.5)
                        scores[name]=dict(minimum_stock_accuracy=float(correct.groupby(cal.ticker).mean().min()),overall_accuracy=float(correct.mean()),rows=len(cal),per_stock=correct.groupby(cal.ticker).mean().to_dict())
                    selected=max(MODELS,key=lambda n:(scores[n]['minimum_stock_accuracy'],scores[n]['overall_accuracy'],-MODELS.index(n)))
                    columns=['ticker','date','decision_at','prediction_at','max_input_available_at','target_at','label_available_at','first_change_at','actual_return','timeout','input_ready','target_kind','price_basis','f_book_imbalance_1']
                    p=test[columns].copy();p['max_input_available_at']=p.max_input_available_at.fillna(selection_at)
                    for name in MODELS:
                        p[f'probability_up_{name}']=models[name].predict_proba(test)[:,1]
                        rows=p.assign(probability_up=p[f'probability_up_{name}'],decision_threshold=.5,model_version=models[name].version)
                        audits[name]=audit_first_change_predictions(rows[rows.ticker.isin(members)],members,expected_decisions=test_dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10),max_wait_seconds=wait)
                p['probability_up']=p[f'probability_up_{selected}'];p['model_version']=models[selected].version;p['decision_threshold']=.5
                run_id=f'{scenario.name}_seed{seed}_wait{wait}';path=Path(run_id)/'predictions.parquet';(output/run_id).mkdir();p.to_parquet(output/path,index=False)
                bounds={t:first_change_feasibility(queue_shape=dict(scenario.external_queue_shapes).get(t,scenario.queue_shape),diffusion_scale=dict(scenario.external_diffusion_scales).get(t,scenario.diffusion_scale),max_wait_seconds=wait,availability=1-scenario.input_outage_fraction) for t in members}
                transfer={}
                for ticker in DEFAULT_EVALUATION_TICKERS:
                    q=p[p.ticker.eq(ticker)];correct=q.actual_return.ne(0)&q.actual_return.gt(0).eq(q.probability_up.ge(.5))
                    transfer[ticker]=dict(accuracy=float(correct.mean()),predictions=len(q),timeout_fraction=float(q.timeout.mean()),tradable=asset_class(ticker)!='index',used_for_training_calibration_selection=False)
                run=dict(run_id=run_id,scenario=asdict(scenario),seed=seed,max_wait_seconds=wait,selected_model=selected,
                    training_rows=len(train),calibration_rows=len(cal),candidate_calibration=scores,training_details={n:m.training_details for n,m in models.items()},
                    candidate_test_audits=audits,selected_test_audit=audits[selected],transfer=transfer,
                    sign_policy_population_bounds=bounds,analytical_bounds_apply_only_to_sign_policy=True,
                    predictions=str(path),prediction_sha256=hashlib.sha256((output/path).read_bytes()).hexdigest(),selection_uses_external_targets=False)
                write_json(output/run_id/'report.json',run);runs.append(run);write_json(output/'progress.json',dict(completed=len(runs),last_run=run_id))
    if source_digest()!=protocol['source_sha256']:raise RuntimeError('Source changed during first-change study')
    report=dict(evidence_kind='synthetic_brownian_first_passage',experiments=runs,models_trained=3*len(runs),
        target_scope_confirmed=True,market_calibrated=False,target_feasibility_established=False,empirical_target_met=False)
    write_json(output/'report.json',report)
    return report
