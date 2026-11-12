"""Exploratory bounded first-change target, separate from clock endpoints.

Future labels are exact independent Brownian first-passage races, never a
specified probability of correctness. Depth distributions and rates remain
uncalibrated assumptions. The user accepted this separate target on2026-11-12.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from math import comb
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.integrate import quad
from scipy.special import betainc,erf,erfcx,gammaincinv,ndtri
from sklearn.linear_model import LogisticRegression

from .direction_audit import constituent_universe
from .direction_intraday import _aware
from .intraday_queue import _draws
from .universe import TRAINING_TICKERS,asset_class,tickers as validate_tickers

TARGET='first_mid_change_before_deadline'
MODELS=('prior','logistic','symmetric_logistic')


def _number(value,low,high):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and np.isfinite(value) and low<=value<=high


@dataclass(frozen=True)
class FirstChangeScenario:
    name:str
    queue_shape:float=1.
    diffusion_scale:float=1.
    quantity_units:float=1.
    input_outage_fraction:float=0.
    external_queue_shapes:tuple[tuple[str,float],...]=()
    external_diffusion_scales:tuple[tuple[str,float],...]=()

    def __post_init__(self):
        if not isinstance(self.name,str) or not 1<=len(self.name)<=80:raise ValueError('Declare a bounded first-change scenario name')
        for value,low,high in [(self.queue_shape,.25,8),(self.diffusion_scale,.05,10),(self.quantity_units,.001,1e6),(self.input_outage_fraction,0,1)]:
            if not _number(value,low,high):raise ValueError('Invalid first-change physical assumptions')
        for field,low,high in [('external_queue_shapes',.25,8),('external_diffusion_scales',.05,10)]:
            values=getattr(self,field)
            if not isinstance(values,(list,tuple)) or any(not isinstance(p,(list,tuple)) or len(p)!=2 for p in values):raise ValueError('Declare external ticker/value pairs')
            pairs=tuple((str(t),v) for t,v in values)
            if pairs:
                ids=validate_tickers([t for t,_ in pairs])
                if len(ids)!=len(pairs) or set(ids)&set(TRAINING_TICKERS) or any(not _number(v,low,high) for _,v in pairs):raise ValueError('Overrides require distinct external targets and bounded values')
            object.__setattr__(self,field,pairs)


def _wait(seconds):
    if type(seconds) is not int or seconds not in (1,5,30,60):raise ValueError('Declare a1/5/30/60second maximum wait')


def first_change_feasibility(*,queue_shape,diffusion_scale,max_wait_seconds,availability=1.):
    """Analytical bounds for the fixed sign-imbalance policy under stated laws."""
    _wait(max_wait_seconds)
    if not _number(queue_shape,.25,8) or not _number(diffusion_scale,.05,10) or not _number(availability,0,1):raise ValueError('Invalid first-change feasibility assumptions')
    k=queue_shape
    integral=quad(lambda u:(2*betainc(k,k,(1+u)/2)-1)/(1+u*u),0,1,epsabs=1e-11)[0]
    unbounded=float(1-2*integral/np.pi)
    if k==1:survival=float(erfcx(diffusion_scale*np.sqrt(max_wait_seconds/2)))
    else:survival=quad(lambda u:erf(gammaincinv(k,u)/(k*diffusion_scale*np.sqrt(2*max_wait_seconds))),0,1,epsabs=1e-10)[0]
    timeout=float(survival**2);fallback=.5*(1-timeout)
    lower=availability*max(0.,unbounded-timeout)+(1-availability)*fallback
    upper=availability*min(unbounded,1-timeout)+(1-availability)*fallback
    return dict(queue_shape=k,diffusion_scale=diffusion_scale,max_wait_seconds=max_wait_seconds,availability=availability,
        unbounded_direction_accuracy=unbounded,timeout_probability=timeout,missing_input_baseline_accuracy=fallback,
        complete_coverage_accuracy_lower=float(lower),complete_coverage_accuracy_upper=float(upper),
        policy='bid_depth>=ask_depth predicts up; no confidence filtering',
        assumptions='Independent equal-variance zero-drift Brownian queues; iid Gamma(k,1/k) initial depths at fixed decision clock; independent missing inputs.',
        market_calibrated=False,target_scope_confirmed=True,target_feasibility_established=False)


def simulate_first_change_panel(tickers,dates,scenario,*,seed=17,future_seed=29,max_wait_seconds=60):
    _wait(max_wait_seconds);universe=validate_tickers(tickers);dates=pd.DatetimeIndex(dates)
    if (not isinstance(scenario,FirstChangeScenario) or not len(dates) or dates.tz is not None or dates.hasnans
            or dates.has_duplicates or not dates.equals(dates.normalize())):raise ValueError('Declare a scenario and distinct naive daily state dates')
    if any(type(s) is not int or not 0<=s<2**32 for s in (seed,future_seed)):raise ValueError('Declare bounded integer seeds')
    dates=dates.sort_values();decision=dates.tz_localize('Asia/Taipei')+pd.Timedelta(hours=10)
    outage=_draws(dates,'first_change_market_clock',seed,10,1)[:,0,0]<scenario.input_outage_fraction
    frames=[]
    for ticker in universe:
        future=_draws(dates,ticker,future_seed,12,1)[:,0]
        is_index=asset_class(ticker)=='index'
        if is_index:
            bid=ask=imbalance=np.full(len(dates),np.nan);duration=np.ones(len(dates));up=future[:,2]<.5
        else:
            shape=dict(scenario.external_queue_shapes).get(ticker,scenario.queue_shape)
            activity=dict(scenario.external_diffusion_scales).get(ticker,scenario.diffusion_scale)
            past=_draws(dates,ticker,seed,11,1)[:,0]
            q=np.maximum(gammaincinv(shape,past[:,:2])/shape,np.finfo(float).tiny)
            bid=q[:,0]*scenario.quantity_units;ask=q[:,1]*scenario.quantity_units
            imbalance=np.round((q[:,0]-q[:,1])/(q[:,0]+q[:,1]),12)
            absolute_normals=ndtri((future[:,:2]+1)/2)
            with np.errstate(divide='ignore',over='ignore'):
                hitting=(q/(activity*absolute_normals))**2
            duration=hitting.min(axis=1);up=hitting[:,1]<hitting[:,0]
        hit=duration<=max_wait_seconds
        delay=np.clip(np.ceil(np.minimum(duration,max_wait_seconds)*1e9),1,max_wait_seconds*1e9).astype('int64')
        event=(decision+pd.to_timedelta(delay,unit='ns')).where(hit)
        ready=~outage if not is_index else np.zeros(len(dates),dtype=bool)
        frame=pd.DataFrame(dict(ticker=ticker,date=dates,decision_at=decision,prediction_at=decision,
            max_input_available_at=decision-pd.Timedelta(milliseconds=5),target_at=decision+pd.Timedelta(seconds=max_wait_seconds),
            label_available_at=decision+pd.Timedelta(seconds=max_wait_seconds,milliseconds=5),first_change_at=event,
            actual_return=np.where(hit,np.where(up,.0001,-.0001),0.),timeout=~hit,input_ready=ready,
            bid_depth=np.where(ready,bid,np.nan),ask_depth=np.where(ready,ask,np.nan),
            f_book_imbalance_1=np.where(ready,imbalance,np.nan),max_wait_seconds=max_wait_seconds,target_kind=TARGET,
            source='synthetic_brownian_first_passage',price_basis='abstract_index_points_no_book' if is_index else 'abstract_first_one_tick_mid_move'))
        frame.loc[~ready,'max_input_available_at']=pd.NaT;frames.append(frame)
    return pd.concat(frames,ignore_index=True).sort_values(['date','ticker']).reset_index(drop=True)


def _labels(frame,seconds):
    required={'ticker','decision_at','target_at','first_change_at','timeout','actual_return','target_kind'}
    if not required<=set(frame) or not frame.target_kind.eq(TARGET).all():raise ValueError('First-change labels require their own target definition')
    p=frame.copy()
    for col in ['decision_at','target_at','first_change_at']:p[col]=_aware(p[col],col)
    if not p.target_at.eq(p.decision_at+pd.Timedelta(seconds=seconds)).all():raise ValueError('First-change target deadline is shifted')
    actual=pd.to_numeric(p.actual_return,errors='coerce');known=np.isfinite(actual);event=known&actual.ne(0);timeout=known&actual.eq(0)
    valid_event=p.first_change_at.gt(p.decision_at)&p.first_change_at.le(p.target_at)&p.timeout.eq(False)
    valid_timeout=p.first_change_at.isna()&p.timeout.eq(True)
    if (event&~valid_event).any() or (timeout&~valid_timeout).any():raise ValueError('Inconsistent first-change event or timeout labels')
    return p


class FirstChangeModel:
    def __init__(self,frame,name,seconds):
        self.name=name;self.seconds=seconds;labels=np.sign(frame.actual_return).astype(int)
        directional=labels.ne(0);self.prior=float(labels[directional].gt(0).mean()) if directional.any() else .5
        self.classifier=None
        if name!='prior' and labels.nunique()>1:
            self.classifier=LogisticRegression(C=1.,max_iter=1000,fit_intercept=name!='symmetric_logistic',random_state=42)
            self.classifier.fit(self._numeric(frame),labels)
        digest=hashlib.sha256(pd.util.hash_pandas_object(frame[['ticker','decision_at','target_at','first_change_at','actual_return','f_book_imbalance_1']],index=False).to_numpy().tobytes()).hexdigest()
        implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.version=f'first-{name}-{seconds}-{digest[:16]}-{implementation[:12]}'
        self.training_details=dict(model=name,training_tickers=list(TRAINING_TICKERS),training_rows=len(frame),training_dates=int(frame.date.nunique()),
            training_frame_sha256=digest,implementation_sha256=implementation,features=['f_book_imbalance_1'],target_kind=TARGET,
            max_wait_seconds=seconds,training_probability_up_given_event=self.prior,external_targets_used=False,
            timeout_training_rows=int(labels.eq(0).sum()),missing_input_training_rows=int((~frame.input_ready.fillna(False)).sum()),
            labels='up/down plus timeout; emitted up/down probabilities renormalized; timeout earns no binary credit',
            fallback='shared_training_direction_prior')
        slope=intercept=None
        if self.classifier is not None and {-1,1}<=set(self.classifier.classes_):
            if len(self.classifier.classes_)==2:
                slope=float(self.classifier.coef_[0,0]);intercept=float(self.classifier.intercept_[0])
            else:
                up=list(self.classifier.classes_).index(1);down=list(self.classifier.classes_).index(-1)
                slope=float(self.classifier.coef_[up,0]-self.classifier.coef_[down,0])
                intercept=float(self.classifier.intercept_[up]-self.classifier.intercept_[down])
        self.training_details.update(up_down_log_odds_slope=slope,up_down_log_odds_intercept=intercept)

    def _numeric(self,frame):
        values=pd.to_numeric(frame.f_book_imbalance_1,errors='coerce')
        ready=frame.input_ready.fillna(False).astype(bool)
        if not values[ready].between(-1,1).all():raise ValueError('Ready first-change inputs require finite imbalance')
        return np.where(ready,values,0.).reshape(-1,1)

    def predict_proba(self,frame):
        ready=frame.input_ready.fillna(False).astype(bool).to_numpy();probability=np.full(len(frame),self.prior)
        if self.classifier is not None and ready.any():
            values=self.classifier.predict_proba(self._numeric(frame.loc[ready]));classes=self.classifier.classes_
            up=values[:,classes==1].sum(axis=1);down=values[:,classes==-1].sum(axis=1)
            probability[ready]=np.divide(up,up+down,out=np.full(len(up),self.prior),where=(up+down)>0)
        return np.column_stack([1-probability,probability])


def fit_first_change_model(frame,name,*,fit_at):
    if name not in MODELS:raise ValueError('Unknown first-change model')
    if set(frame.ticker.astype(str))!=set(TRAINING_TICKERS):raise ValueError('First-change fitting requires exactly the fixed ten')
    if frame.max_wait_seconds.nunique()!=1:raise ValueError('Declare one first-change deadline')
    seconds=int(frame.max_wait_seconds.iloc[0]);_wait(seconds);p=_labels(frame,seconds)
    stamp=pd.Timestamp(fit_at)
    if stamp.tzinfo is None or not np.isfinite(p.actual_return).all():raise ValueError('First-change fitting requires mature known labels')
    available=_aware(p.label_available_at,'label_available_at')
    if not (available>=pd.DatetimeIndex(p.target_at)).all() or not (available<stamp).all() or not p.target_at.lt(stamp).all():raise ValueError('First-change fitting requires strictly mature deadline labels')
    ready=p.input_ready.fillna(False).astype(bool)
    for col in ('prediction_at','max_input_available_at'):p[col]=_aware(p[col],col)
    if not (p.max_input_available_at.le(p.prediction_at)&p.prediction_at.le(p.decision_at))[ready].all():raise ValueError('First-change training inputs must be causal')
    return FirstChangeModel(frame,name,seconds)


def audit_first_change_predictions(predictions,constituents,*,expected_decisions,max_wait_seconds,min_dates=126,evidence_kind='synthetic'):
    _wait(max_wait_seconds);universe=constituent_universe(constituents);decisions=_aware(expected_decisions,'Decision calendar').sort_values()
    local=decisions.tz_convert('Asia/Taipei');seconds=(local-local.normalize()).total_seconds()
    if (len(universe)!=50 or not len(decisions) or decisions.hasnans or decisions.has_duplicates or local.normalize().has_duplicates
            or len(set(local.time))!=1 or (seconds<9*3600).any() or (seconds+max_wait_seconds>=13*3600+25*60).any()):raise ValueError('Declare a complete fixed-clock continuous-session calendar and50constituents')
    if evidence_kind not in ('synthetic','historical','unknown') or type(min_dates) is not int or min_dates<1:raise ValueError('Invalid first-change audit settings')
    p=_labels(predictions,max_wait_seconds);required={'probability_up','model_version','decision_threshold','prediction_at','max_input_available_at'}
    if not required<=set(p):raise ValueError('Missing first-change prediction fields')
    for col in ('prediction_at','max_input_available_at'):p[col]=_aware(p[col],col)
    if (not set(p.ticker)<=set(universe) or not set(p.decision_at)<=set(decisions) or p.duplicated(['decision_at','ticker']).any()):raise ValueError('Duplicate or undeclared first-change predictions')
    if p.groupby('decision_at')[['model_version','decision_threshold']].nunique(dropna=False).gt(1).any().any():raise ValueError('Use one shared model and threshold for all stocks')
    grid=pd.MultiIndex.from_product([decisions,universe],names=['decision_at','ticker']);present=pd.MultiIndex.from_frame(p[['decision_at','ticker']])
    p=p.set_index(['decision_at','ticker']).reindex(grid);actual=pd.to_numeric(p.actual_return,errors='coerce')
    probability=pd.to_numeric(p.probability_up,errors='coerce');threshold=pd.to_numeric(p.decision_threshold,errors='coerce')
    valid=(probability.between(0,1)&threshold.between(0,1)&p.model_version.notna()&p.model_version.astype(str).str.strip().ne('')
        &p.max_input_available_at.le(p.prediction_at)&p.prediction_at.le(pd.Series(grid.get_level_values('decision_at'),index=grid)))
    if 'abstain' in p:valid&=~p.abstain.fillna(True).astype(bool)
    known=np.isfinite(actual);event=known&actual.ne(0);up=actual.gt(0);predicted=probability.ge(threshold)
    correct=valid&event&up.eq(predicted);stocks={}
    for ticker in universe:
        mask=grid.get_level_values('ticker')==ticker;positive=mask&event&up;negative=mask&event&actual.lt(0)
        recall_up=float((valid&predicted)[positive].mean()) if positive.any() else None
        recall_down=float((valid&~predicted)[negative].mean()) if negative.any() else None
        stocks[ticker]=dict(accuracy=float(correct[mask].mean()),prediction_coverage=float(valid[mask].mean()),answer_coverage=float(known[mask].mean()),
            timeout_fraction=float((known&actual.eq(0))[mask].mean()),always_up_accuracy=float((event&up)[mask].mean()),always_down_accuracy=float((event&actual.lt(0))[mask].mean()),
            balanced_accuracy=(recall_up+recall_down)/2 if recall_up is not None and recall_down is not None else None)
    ranking=sorted(universe,key=lambda t:(stocks[t]['accuracy'],t));minimum=stocks[ranking[0]]['accuracy']
    edge=all(s['balanced_accuracy'] is not None and s['balanced_accuracy']>.5 and s['accuracy']>max(s['always_up_accuracy'],s['always_down_accuracy']) for s in stocks.values())
    gate=bool(valid.all() and known.all() and len(decisions)>=min_dates and minimum>=.75 and edge)
    return dict(evidence_kind=evidence_kind,target_kind=TARGET,max_wait_seconds=max_wait_seconds,target_scope_confirmed=True,
        expected_predictions=len(grid),test_dates=len(decisions),missing_predictions=int((~grid.isin(present)).sum()),invalid_predictions=int((~valid&grid.isin(present)).sum()),
        unresolved_answers=int((~known).sum()),timeout_predictions=int((known&actual.eq(0)).sum()),event_coverage=float(event.mean()),prediction_coverage=float(valid.mean()),answer_coverage=float(known.mean()),
        overall_accuracy=float(correct.mean()),minimum_stock_accuracy=minimum,worst_five_accuracy=float(np.mean([stocks[t]['accuracy'] for t in ranking[:5]])),
        worst_five_tickers=ranking[:5],per_stock=stocks,five_stock_combinations=comb(50,5),directional_edge_demonstrated=edge,full_coverage_sample_gate_met=gate,
        empirical_target_met=False,quote_provenance_verified=False,training_isolation_verified=False,target_feasibility_established=False,
        limitation='Separate exploratory first-event target; timeout/unknown/missing rows retained. Declarations cannot prove the event was the first or that observations were complete.')
