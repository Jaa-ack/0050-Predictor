"""Frozen offline queue experiments; every assumption remains counterfactual."""
from __future__ import annotations

from dataclasses import asdict,fields
import hashlib
import json
from pathlib import Path
import re
import platform
from importlib.metadata import version

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from .direction_audit import load_membership_snapshot
from .direction_intraday import audit_intraday_predictions
from .intraday_models import MODELS,fit_intraday_model
from .intraday_queue import QueueScenario,simulate_queue_panel
from .universe import DEFAULT_EVALUATION_TICKERS,TRAINING_TICKERS,asset_class
from .workflows import source_digest,write_json


def _config(config):
    required={'train_dates','calibration_dates','test_dates','seeds','horizons_seconds','scenarios'}
    if not isinstance(config,dict) or not required<=set(config)<=required|{'reference_training_scenario'}:
        raise ValueError('Queue study requires its explicit configuration fields')
    for field,minimum in [('train_dates',30),('calibration_dates',10),('test_dates',126)]:
        if type(config[field]) is not int or not minimum<=config[field]<=10000:
            raise ValueError('Queue-study split lengths are outside declared bounds')
    seeds=config['seeds'];horizons=config['horizons_seconds']
    if (not isinstance(seeds,list) or not 1<=len(seeds)<=20
            or any(type(s) is not int or not 0<=s<2**32 for s in seeds) or len(set(seeds))!=len(seeds)):
        raise ValueError('Declare distinct bounded queue-study seeds')
    if (not isinstance(horizons,list) or not horizons or any(type(h) is not int or h not in (1,5,30,60) for h in horizons)
            or len(set(horizons))!=len(horizons)):
        raise ValueError('Declare distinct fixed-second queue-study horizons')
    specs=config['scenarios']
    if not isinstance(specs,list) or not 1<=len(specs)<=20:
        raise ValueError('Declare explicit queue-study scenarios')
    scenarios=[]
    for spec in specs:
        if not isinstance(spec,dict) or not set(spec)<={f.name for f in fields(QueueScenario)} or 'name' not in spec:
            raise ValueError('Unknown queue-scenario fields')
        scenario=QueueScenario(**spec)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',scenario.name) or set(scenario.weak_tickers)&set(TRAINING_TICKERS):
            raise ValueError('Scenario names must be path-safe; weak-transfer targets must stay outside training')
        scenarios.append(scenario)
    if len({s.name for s in scenarios})!=len(scenarios):
        raise ValueError('Queue scenario names must be distinct')
    if 'reference_training_scenario' in config:
        spec=config['reference_training_scenario']
        if not isinstance(spec,dict) or 'name' not in spec or not set(spec)<={f.name for f in fields(QueueScenario)}:
            raise ValueError('Invalid reference-training scenario')
        reference=QueueScenario(**spec)
        if reference.weak_tickers or reference.external_persistent_fractions:
            raise ValueError('Reference training cannot include external-target overrides')
    return scenarios


def _calibration(frame,probability):
    correct=frame.actual_return.ne(0)&(frame.actual_return.gt(0)==(probability>=.5))
    by_stock=correct.groupby(frame.ticker).mean()
    return dict(minimum_stock_accuracy=float(by_stock.min()),overall_accuracy=float(correct.mean()),
                per_stock=by_stock.to_dict(),rows=len(frame))


def run_queue_study(config,membership_path,output):
    """Fit fixed-ten controls, select on fixed-ten calibration, audit every target.

    The protocol is saved before simulation and fitting. No external observation
    influences transforms, model weights, calibration scores or selection.
    """
    scenarios=_config(config)
    membership=load_membership_snapshot(membership_path)
    if len(membership['tickers'])!=50:
        raise ValueError('Queue study requires exactly fifty declared constituents')
    if any((set(s.weak_tickers)|{t for t,_ in s.external_persistent_fractions})-set(membership['tickers']) for s in scenarios):
        raise ValueError('Weak-transfer targets must belong to the declared full constituent audit')
    output=Path(output)
    if output.exists():
        raise ValueError('Queue study requires a fresh output directory')
    universe=tuple(dict.fromkeys((*TRAINING_TICKERS,*membership['tickers'],*DEFAULT_EVALUATION_TICKERS)))
    dates=pd.bdate_range('2020-01-02',periods=sum(config[k] for k in ('train_dates','calibration_dates','test_dates')))
    train_dates=dates[:config['train_dates']]
    calibration_dates=dates[config['train_dates']:config['train_dates']+config['calibration_dates']]
    test_dates=dates[-config['test_dates']:]
    fit_at=calibration_dates[0].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10,seconds=-1)
    selection_at=test_dates[0].tz_localize('Asia/Taipei')+pd.Timedelta(hours=10,seconds=-1)
    reference_scenario=QueueScenario(**config['reference_training_scenario']) if 'reference_training_scenario' in config else None
    protocol=dict(config=config,expanded_scenarios=[asdict(s) for s in scenarios],
        config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),source_sha256=source_digest(),
        evidence_kind='synthetic_queue_process',market_calibrated=False,membership=membership,
        training_tickers=list(TRAINING_TICKERS),evaluation_tickers=list(universe),candidates=list(MODELS),
        selection='maximum fixed-ten minimum calibration accuracy, then mean, then declared candidate order',
        threshold=.5,fit_at=fit_at.isoformat(),selection_at=selection_at.isoformat(),
        synthetic_calendar='Weekdays from2020-01-02; not an exchange calendar or market history',
        scope='All50 declared constituents every synthetic day; secondary fixed-second horizons remain separate',
        simulation_assumptions=['One event each second; five queues per side; one-tick symmetric-spread reset on depletion.',
            '65pct market removal,15pct cancellation,20pct insertion; integer sizes1to4; reset depth5to10plus4perlevel.',
            'Hidden two-state pressure with declared flip rate;35pct shared initial pressure; date-common input outages.',
            'Fixed clock and ideal synthetic emission; no measured latency, auctions, halts, fees or real quote provenance.'],
        real_holdout_opened=False,unconditional_75_expectation_supported=False)
    protocol['environment']=dict(python=platform.python_version(),packages={name:version(name) for name in
        ['numpy','pandas','scipy','scikit-learn','pyarrow','threadpoolctl']})
    protocol['reference_training_scenario']=asdict(reference_scenario) if reference_scenario else None
    protocol['mixture_and_liquidity_assumptions']=[
        'Hidden persistent/background regime chosen before observed history; diagnostic flag never enters features or selection.',
        'Optional external frequency overrides never alter primary training/calibration.',
        'Liquidity response uses only an EMA of already observed simulated market-order signs; no hidden pressure oracle.',
        'At resets, bid depth scales by exp(-response*EMA), ask by exp(response*EMA), rounded upward.',
        'Pure-training history_logistic reference is a declared comparison only, never an extra selection candidate.']
    output.mkdir(parents=True,exist_ok=False)
    write_json(output/'protocol.json',protocol)
    experiments=[]
    for scenario in scenarios:
        for seed in config['seeds']:
            for horizon in config['horizons_seconds']:
                run_id=f'{scenario.name}_seed{seed}_h{horizon}'
                frame=simulate_queue_panel(universe,dates,scenario,seed=seed,future_seed=seed^0x5A5A,horizon_seconds=horizon)
                primary=frame.ticker.isin(TRAINING_TICKERS)
                train=frame.loc[primary&frame.date.isin(train_dates)]
                calibration=frame.loc[primary&frame.date.isin(calibration_dates)]
                if not calibration.label_available_at.lt(selection_at).all():
                    raise ValueError('Calibration answers must mature before model selection')
                test=frame.loc[frame.date.isin(test_dates)].copy()
                scores={};models={};audits={}
                with threadpool_limits(limits=1):
                    for name in MODELS:
                        model=fit_intraday_model(train,name,fit_at=fit_at)
                        scores[name]=_calibration(calibration,model.predict_proba(calibration)[:,1])
                        models[name]=model
                    selected=max(MODELS,key=lambda name:(scores[name]['minimum_stock_accuracy'],scores[name]['overall_accuracy'],-MODELS.index(name)))
                    predictions=test[['ticker','date','decision_at','prediction_at','max_input_available_at','target_at','label_available_at','actual_return','input_ready','price_basis','diagnostic_persistent_regime']].copy()
                    predictions['max_input_available_at']=predictions.max_input_available_at.fillna(selection_at)
                    for name in MODELS:
                        predictions[f'probability_up_{name}']=models[name].predict_proba(test)[:,1]
                        audit_input=predictions.assign(probability_up=predictions[f'probability_up_{name}'],
                            decision_threshold=.5,model_version=models[name].version)
                        audits[name]=audit_intraday_predictions(audit_input[audit_input.ticker.isin(membership['tickers'])],
                            membership['tickers'],expected_decisions=test_dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10),
                            horizon_seconds=horizon,evidence_kind='synthetic')
                    reference=None
                    if reference_scenario is not None:
                        pure=simulate_queue_panel(TRAINING_TICKERS,train_dates,reference_scenario,
                            seed=seed,future_seed=seed^0x5A5A,horizon_seconds=horizon)
                        fixed=fit_intraday_model(pure,'history_logistic',fit_at=fit_at)
                        predictions['probability_up_frozen_reference']=fixed.predict_proba(test)[:,1]
                        reference_input=predictions.assign(probability_up=predictions.probability_up_frozen_reference,
                            decision_threshold=.5,model_version=fixed.version)
                        reference=dict(model='history_logistic',used_for_selection=False,
                            model_version=fixed.version,training_details=fixed.training_details,
                            calibration=_calibration(calibration,fixed.predict_proba(calibration)[:,1]),
                            test_audit=audit_intraday_predictions(reference_input[reference_input.ticker.isin(membership['tickers'])],
                                membership['tickers'],expected_decisions=test_dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10),
                                horizon_seconds=horizon,evidence_kind='synthetic'))
                predictions['probability_up']=predictions[f'probability_up_{selected}']
                predictions['decision_threshold']=.5;predictions['model_version']=models[selected].version
                path=Path(run_id)/'predictions.parquet'
                (output/run_id).mkdir()
                predictions.to_parquet(output/path,index=False)
                transfer={}
                for ticker in DEFAULT_EVALUATION_TICKERS:
                    p=predictions[predictions.ticker.eq(ticker)]
                    correct=p.actual_return.ne(0)&p.actual_return.gt(0).eq(p.probability_up.ge(.5))
                    transfer[ticker]=dict(accuracy=float(correct.mean()),predictions=len(p),
                        flat_fraction=float(p.actual_return.eq(0).mean()),tradable=asset_class(ticker)!='index',
                        used_for_training_calibration_selection=False)
                result=dict(run_id=run_id,scenario=asdict(scenario),seed=seed,horizon_seconds=horizon,
                    training_rows=len(train),calibration_rows=len(calibration),selected_model=selected,
                    selection_uses_external_targets=False,candidate_calibration=scores,
                    training_details={name:model.training_details for name,model in models.items()},
                    candidate_test_audits=audits,selected_test_audit=audits[selected],transfer=transfer,
                    predictions=str(path),prediction_sha256=hashlib.sha256((output/path).read_bytes()).hexdigest())
                if reference is not None:result['frozen_reference']=reference
                write_json(output/run_id/'report.json',result)
                experiments.append(result)
                write_json(output/'progress.json',dict(completed=len(experiments),last_run=run_id))
    if source_digest()!=protocol['source_sha256']:
        raise RuntimeError('Source changed during queue study; retain results but reject the frozen-run claim')
    report=dict(evidence_kind='synthetic_queue_process',experiments=experiments,models_trained=(4+int(reference_scenario is not None))*len(experiments),
        all_declared_experiments_completed=True,market_calibrated=False,empirical_target_met=False,
        real_world_target_verified=False,unconditional_75_expectation_supported=False,
        limitation='Passing a known artificial process is conditional simulation evidence, not a75pct market expectation.')
    write_json(output/'report.json',report)
    return report
