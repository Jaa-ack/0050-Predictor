"""Exploratory paired-loss uncertainty; resample market dates, never stock rows."""
import numpy as np
import pandas as pd


def paired_block_audit(predictions, block_dates=20, repetitions=1000, random_state=42):
    if block_dates < 1 or repetitions < 1:
        raise ValueError('Positive block length and repetitions required')
    output = {}
    for h, group in predictions.groupby('horizon'):
        p = group.loc[group.actual_return.notna()].copy()
        if p.empty:
            continue
        labels = p.actual_return.gt(0).astype(float)
        prior = p.get('training_probability', pd.Series(.5, index=p.index))
        p['loss_difference'] = (p.probability_up-labels)**2 - (prior-labels)**2
        daily = p.groupby('date', sort=True).loss_difference.mean().to_numpy()
        n = len(daily)
        # Circular moving blocks retain cross-sectional dependence and local
        # serial dependence. Results are descriptive, not a selection guarantee.
        length = min(block_dates, n)
        rng = np.random.default_rng(random_state)
        draws = []
        for _ in range(repetitions):
            starts = rng.integers(0, n, size=int(np.ceil(n/length)))
            indices = ((starts[:, None] + np.arange(length)) % n).ravel()[:n]
            draws.append(float(daily[indices].mean()))
        output[str(h)] = dict(dates=n, observations=len(p), block_dates=length,
            loss_difference=float(daily.mean()), ci95=np.quantile(draws, [.025, .975]).tolist())
    return dict(by_horizon=output, unit='market_date', repetitions=repetitions, exploratory=True,
                baseline='training_frequency', lower_is_better=True,
                limitation='Equal weight per market date; circular blocks may cross separated OOS folds. Block length and historical design selection affect uncertainty; not proof of an untouched test or trading edge.')
