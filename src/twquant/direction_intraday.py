"""Full-calendar, fixed-clock intraday direction accounting.

This module audits supplied forecasts; it cannot establish quote provenance or
training isolation. Flat endpoints earn no binary up/down credit. Event-time
labels and smoothed three-class benchmarks cannot enter this audit.
"""
from __future__ import annotations

from math import comb

import numpy as np
import pandas as pd

from .direction_audit import constituent_universe
from .universe import TRAINING_TICKERS


def _aware(values, name):
    parsed=[]
    for value in values:
        stamp=pd.Timestamp(value)
        if pd.notna(stamp) and stamp.tzinfo is None:
            raise ValueError(f'{name} requires timezone-aware timestamps')
        parsed.append(stamp)
    return pd.DatetimeIndex(pd.to_datetime(parsed,utc=True))


def audit_intraday_predictions(predictions, constituents, *, expected_decisions,
                               horizon_seconds, evidence_kind='unknown', min_dates=126):
    """Audit one predeclared fixed-clock decision per trading day for all 50.

    All rows, missing days and flat/unknown endpoints keep equal denominators.
    A valid emission requires its latest input receipt <= emission <= decision.
    A sample gate is accounting only, never evidence that a model transfers.
    """
    universe=constituent_universe(constituents)
    if len(universe)!=50:
        raise ValueError('Intraday acceptance requires all 50 constituents')
    if type(horizon_seconds) is not int or horizon_seconds not in (1,5,30,60):
        raise ValueError('Declare a fixed 1/5/30/60-second horizon')
    if evidence_kind not in {'unknown','synthetic','historical'} or type(min_dates) is not int or min_dates<1:
        raise ValueError('Invalid intraday evidence settings')
    decisions=_aware(expected_decisions,'Decision calendar')
    local=decisions.tz_convert('Asia/Taipei')
    if (not len(decisions) or decisions.hasnans or decisions.has_duplicates
            or local.normalize().has_duplicates or len(set(local.time))!=1):
        raise ValueError('Declare exactly one common fixed-clock decision per trading date')
    seconds=(local-local.normalize()).total_seconds()
    if (seconds<9*3600).any() or (seconds+horizon_seconds>=13*3600+25*60).any():
        raise ValueError('Decision and target must stay inside the regular continuous session')
    decisions=decisions.sort_values()
    required={'ticker','decision_at','target_at','prediction_at','max_input_available_at',
              'actual_return','probability_up','decision_threshold','model_version'}
    if not required<=set(predictions):
        raise ValueError('Intraday predictions lack required columns')
    p=predictions.copy()
    if 'target_kind' in p and not p.target_kind.eq('fixed_endpoint').all():
        raise ValueError('Clock-endpoint audit rejects other target definitions')
    p['ticker']=p.ticker.astype(str)
    if not set(p.ticker)<=set(universe):
        raise ValueError('Predictions are outside the declared constituents')
    for column in ('decision_at','target_at','prediction_at','max_input_available_at'):
        p[column]=_aware(p[column],column)
    if (p.decision_at.isna().any() or not set(p.decision_at)<=set(decisions)
            or p.duplicated(['decision_at','ticker']).any()):
        raise ValueError('Duplicate or undeclared intraday decisions')
    if not p.target_at.eq(p.decision_at+pd.Timedelta(seconds=horizon_seconds)).all():
        raise ValueError('Targets must equal the decision plus the declared fixed-clock horizon')
    if p.groupby('decision_at')[['model_version','decision_threshold']].nunique(dropna=False).gt(1).any().any():
        raise ValueError('Every stock requires the same shared model and threshold at each decision')
    grid=pd.MultiIndex.from_product([decisions,universe],names=['decision_at','ticker'])
    present=pd.MultiIndex.from_frame(p[['decision_at','ticker']])
    p=p.set_index(['decision_at','ticker']).reindex(grid)
    probability=pd.to_numeric(p.probability_up,errors='coerce')
    threshold=pd.to_numeric(p.decision_threshold,errors='coerce')
    actual=pd.to_numeric(p.actual_return,errors='coerce')
    decision=pd.Series(grid.get_level_values('decision_at'),index=grid)
    valid=(probability.between(0,1)&threshold.between(0,1)
           &p.model_version.notna()&p.model_version.astype(str).str.strip().ne('')
           &p.prediction_at.le(decision)&p.max_input_available_at.le(p.prediction_at))
    if 'abstain' in p:
        valid &= ~p.abstain.fillna(True).astype(bool)
    known=pd.Series(np.isfinite(actual),index=grid)
    flat=known&actual.eq(0)
    up=actual.gt(0)
    predicted_up=probability.ge(threshold)
    correct=known&~flat&valid&predicted_up.eq(up)
    stocks={}
    for ticker in universe:
        mask=grid.get_level_values('ticker')==ticker
        positive=mask&known&up
        negative=mask&known&actual.lt(0)
        recall_up=float((valid&predicted_up)[positive].mean()) if positive.any() else None
        recall_down=float((valid&~predicted_up)[negative].mean()) if negative.any() else None
        stocks[ticker]=dict(accuracy=float(correct[mask].mean()),
            prediction_coverage=float(valid[mask].mean()),answer_coverage=float(known[mask].mean()),
            flat_fraction=float(flat[mask].mean()),recall_up=recall_up,recall_down=recall_down,
            balanced_accuracy=(recall_up+recall_down)/2 if recall_up is not None and recall_down is not None else None,
            always_up_accuracy=float((known&up)[mask].mean()),
            always_down_accuracy=float((known&actual.lt(0))[mask].mean()))
    ranking=sorted(universe,key=lambda t:(stocks[t]['accuracy'],t))
    minimum=stocks[ranking[0]]['accuracy']
    edge=all(s['balanced_accuracy'] is not None and s['balanced_accuracy']>.5
             and s['accuracy']>max(s['always_up_accuracy'],s['always_down_accuracy']) for s in stocks.values())
    gate=bool(valid.all() and known.all() and len(decisions)>=min_dates and minimum>=.75 and edge)
    return dict(evidence_kind=evidence_kind,horizon_seconds=horizon_seconds,
        target_definition='sign of raw endpoint mid-quote minus decision mid-quote; flat earns no up/down credit',
        target_accuracy=.75,test_dates=len(decisions),expected_predictions=len(grid),
        missing_predictions=int((~grid.isin(present)).sum()),invalid_predictions=int((~valid&grid.isin(present)).sum()),
        unresolved_answers=int((~known).sum()),prediction_coverage=float(valid.mean()),
        answer_coverage=float(known.mean()),flat_fraction=float(flat.mean()),
        direction_accuracy_ceiling_given_answers=float((known&~flat).mean()),
        overall_accuracy=float(correct.mean()),minimum_stock_accuracy=minimum,
        worst_five_accuracy=float(np.mean([stocks[t]['accuracy'] for t in ranking[:5]])),
        worst_five_tickers=ranking[:5],per_stock=stocks,five_stock_combinations=comb(50,5),
        up_vs_nonup_accuracy_diagnostic=float((known&valid&predicted_up.eq(up)).mean()),
        directional_edge_demonstrated=edge,full_coverage_sample_gate_met=gate,
        synthetic_sample_gate_met=gate and evidence_kind=='synthetic',
        planned_training_tickers=list(TRAINING_TICKERS),same_shared_model_checked=True,
        training_isolation_verified=False,quote_provenance_verified=False,
        empirical_target_met=False,real_world_target_verified=False,
        limitations=['Timestamp declarations do not verify raw quotes, feature causality or training isolation.',
                     'Expected decisions must come from an independent primary trading calendar, never available quote rows.',
                     'Sample accounting cannot establish future accuracy, transferability or an unconditional75expectation.',
                     'Mid-quotes are not executable prices; no profitability or trading claim follows.'])
