"""One portable nonlinear classifier using observable state interactions only."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd

from .intraday_models import SharedIntradayModel


def _state_frame(frame):
    base=[f'f_book_imbalance_{i}' for i in range(1,6)]
    for window in (5,20,100):
        base.extend(f'f_book_imbalance_{i}_{stat}_{window}' for i in (1,5) for stat in ('mean','change'))
        base.extend([f'f_book_mid_change_{window}',f'f_book_mid_vol_{window}'])
    relative=[f'f_book_relative_depth_{i}' for i in range(1,6)]
    logs=[f'f_book_log_depth_{i}' for i in range(1,6)]
    if not set(base)<=set(frame) or not (set(relative)<=set(frame) or set(logs)<=set(frame)):
        raise ValueError('Observable-state model requires the declared book history profile')
    out=frame.drop(columns=[c for c in frame if c.startswith('f_book_')]).copy()
    for col in base:out[col]=pd.to_numeric(frame[col],errors='coerce')
    if set(relative)<=set(frame):
        for col in relative:out[col]=pd.to_numeric(frame[col],errors='coerce')
    else:
        with np.errstate(over='ignore',invalid='ignore',divide='ignore'):
            totals=np.expm1(pd.to_numeric(frame[logs[-1]],errors='coerce'))
            for target,source in zip(relative,logs,strict=True):
                values=np.expm1(pd.to_numeric(frame[source],errors='coerce'))
                out[target]=values/np.where(totals>0,totals,np.nan)
    for level in (1,5):
        for window in (20,100):
            out[f'f_book_alignment_{level}_{window}']=out[f'f_book_imbalance_{level}']*out[f'f_book_mid_change_{window}']
    columns=[c for c in out if c.startswith('f_book_')]
    out[columns]=out[columns].replace([np.inf,-np.inf],np.nan).round(12)
    return out


class ObservableStateModel:
    def __init__(self,frame):
        self.inner=SharedIntradayModel(_state_frame(frame),'history_hist')
        self.prior=self.inner.prior
        implementation=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.version=f'state-{implementation[:12]}-{self.inner.version}'
        self.training_details=dict(self.inner.training_details,model='history_state_hist',
            observable_state_sha256=implementation,feature_profile='portable_state_v1',
            hidden_regime_labels_used=False,feature_rounding_decimals=12,
            description='Relative depth and observed trend/imbalance products; no regime router or online refitting.')

    def predict_proba(self,frame):
        return self.inner.predict_proba(_state_frame(frame))
