"""Shared intraday controls fitted only to mature fixed-ten examples."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .universe import TRAINING_TICKERS

MODELS=('prior','snapshot_logistic','history_logistic','history_hist')


class SharedIntradayModel:
    def __init__(self,frame,name):
        self.name=name
        self.columns=sorted(c for c in frame if c.startswith('f_book_'))
        if name=='snapshot_logistic':
            self.columns=[f'f_book_imbalance_{level}' for level in range(1,6)]
        labels=np.sign(frame.actual_return).astype(int)
        directional=labels.ne(0)
        self.prior=float(labels[directional].gt(0).mean()) if directional.any() else .5
        self.pipeline=None
        if name!='prior' and labels.nunique()>1:
            classifier=(HistGradientBoostingClassifier(max_iter=100,max_leaf_nodes=15,
                        min_samples_leaf=40,random_state=42) if name=='history_hist'
                        else LogisticRegression(C=1.,max_iter=1000,random_state=42))
            self.pipeline=make_pipeline(SimpleImputer(strategy='median',keep_empty_features=True,add_indicator=True),
                                         StandardScaler(),classifier)
            self.pipeline.fit(self._numeric(frame),labels)
        digest=hashlib.sha256(pd.util.hash_pandas_object(frame[['ticker','decision_at','actual_return',*self.columns]],index=False).to_numpy().tobytes()).hexdigest()
        implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.version=f'intraday-{name}-{digest[:16]}-{implementation[:12]}'
        self.training_details=dict(model=name,training_tickers=list(TRAINING_TICKERS),training_rows=len(frame),
            training_dates=int(frame.date.nunique()),flat_training_rows=int(labels.eq(0).sum()),features=self.columns,
            training_probability_up_given_direction=self.prior,training_frame_sha256=digest,
            implementation_sha256=implementation,
            labels='three classes including flat; binary emission renormalizes up/down probabilities',
            fallback='shared_training_direction_prior',external_targets_used=False)

    def _numeric(self,frame):
        return frame[self.columns].apply(pd.to_numeric,errors='coerce').replace([np.inf,-np.inf],np.nan)

    def predict_proba(self,frame):
        probability=np.full(len(frame),self.prior)
        ready=frame.input_ready.fillna(False).astype(bool).to_numpy()
        if self.pipeline is not None and ready.any():
            values=self.pipeline.predict_proba(self._numeric(frame.loc[ready]))
            classes=self.pipeline.classes_
            up=values[:,classes==1].sum(axis=1);down=values[:,classes==-1].sum(axis=1)
            probability[ready]=np.divide(up,up+down,out=np.full(len(up),self.prior),where=(up+down)>0)
        return np.column_stack([1-probability,probability])


def fit_intraday_model(frame,name,*,fit_at):
    """Reject outside training rows, mixed horizons and unmatured answers."""
    if name not in MODELS:
        raise ValueError('Unknown intraday model')
    if set(frame.ticker.astype(str))!=set(TRAINING_TICKERS):
        raise ValueError('Intraday fitting requires exactly the fixed ten training stocks')
    stamp=pd.Timestamp(fit_at)
    if stamp.tzinfo is None:
        raise ValueError('Intraday fitting requires strictly mature labels')
    if (frame.horizon_seconds.nunique()!=1 or not frame.horizon_seconds.isin([1,5,30,60]).all()
            or not np.isfinite(frame.actual_return).all() or not any(c.startswith('f_book_') for c in frame)):
        raise ValueError('Intraday training needs known labels, one fixed horizon and book features')
    if not frame.target_at.eq(frame.decision_at+pd.to_timedelta(frame.horizon_seconds,unit='s')).all():
        raise ValueError('Intraday training has a shifted target clock')
    available=pd.to_datetime(frame.label_available_at,utc=True)
    target=pd.to_datetime(frame.target_at,utc=True)
    if not (available.ge(target)&target.lt(stamp)&available.lt(stamp)).all():
        raise ValueError('Intraday fitting requires mature endpoints and consistent label receipts')
    ready=frame.input_ready.fillna(False).astype(bool)
    causal=(frame.max_input_available_at.le(frame.prediction_at)&frame.prediction_at.le(frame.decision_at))
    if not causal[ready].all():
        raise ValueError('Intraday training inputs must be causal at each emission')
    return SharedIntradayModel(frame,name)
