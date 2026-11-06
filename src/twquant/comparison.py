"""Development-only ablations; final dates and external assets cannot select models."""
from dataclasses import replace

import numpy as np
import pandas as pd

from .features import build_features, select_feature_columns
from .research import run_research
from .universe import TRAINING_TICKERS


def reserve_development(frame, horizons, holdout_dates):
    dates = pd.DatetimeIndex(frame.loc[frame.ticker == '0050', 'date'].unique()).sort_values()
    if holdout_dates < 1 or len(dates) <= holdout_dates:
        raise ValueError('Insufficient benchmark history for reserved holdout')
    start = dates[-holdout_dates]
    primary = frame.ticker.isin((*TRAINING_TICKERS, '0050'))
    freeze = pd.to_datetime(frame.loc[primary & frame.date.eq(start), 'decision_ts'], utc=True).min()
    development = frame.loc[frame.date < start].copy()
    for h in horizons:
        known = pd.to_datetime(development[f'label_available_at_{h}'], utc=True) < freeze
        for prefix in ('target', 'execution_target', 'trade_return', 'benchmark_target',
                       'benchmark_trade_return', 'benchmark_end_adj_close'):
            development.loc[~known, f'{prefix}_{h}'] = np.nan
        development.loc[~known, f'trade_valid_{h}'] = False
    return development, start


def compare_profiles(prices, config, external=None, holdout_dates=126,
                     profiles=('portable_price', 'portable_market', 'portable_all'), mode='postclose', output=None):
    frame = build_features(prices, external=external, mode=mode)
    development, start = reserve_development(frame, config.horizons, holdout_dates)
    scores, candidates = {}, {}
    for profile in profiles:
        cfg = replace(config, feature_profile=profile)
        result = run_research(development, select_feature_columns(development, profile), cfg)
        # Fixed-ten probability quality alone determines selection. Transfer
        # results are diagnostics; final holdout is never opened here.
        metrics = result.metrics['by_horizon']
        skills = [m.get('brier_skill_vs_frequency') for m in metrics.values()]
        score = float(np.mean(skills)) if skills and all(v is not None for v in skills) else -1e9
        scores[profile] = score
        candidates[profile] = result.metrics
        if output is not None:
            from pathlib import Path
            folder = Path(output) / profile
            folder.mkdir(parents=True, exist_ok=True)
            result.predictions.to_parquet(folder / 'oos_predictions.parquet', index=False)
            result.transfer_predictions.to_parquet(folder / 'transfer_predictions.parquet', index=False)
    selected = max(scores, key=scores.get)
    return dict(selected_profile=selected, scores=scores, candidates=candidates, holdout_opened=False,
                training_tickers=list(TRAINING_TICKERS), holdout_start=str(start.date()),
                rule='Highest mean fixed-ten OOS Brier skill versus training frequency across configured horizons; ties prefer input order. Transfer results do not select the model.',
                limitation='Exploratory development comparison. Previously inspected historical dates are not an untouched test.')
