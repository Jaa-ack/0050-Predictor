"""Offline conditional information budgets, never measured market forecasts."""
from __future__ import annotations

from math import asin, pi, sin, sqrt
from numbers import Real

from scipy.stats import betabinom, binom


def _fraction(value, name, *, lower=0.):
    if isinstance(value, bool) or not isinstance(value, Real) or not lower <= value <= 1:
        raise ValueError(f'{name} requires a finite fraction in [{lower},1]')
    return float(value)


def gaussian_sign_accuracy(explained_variance):
    """Sign agreement for a zero-mean Gaussian conditional mean plus noise.

    R2 is assumed available information, not an R2 estimated from our markets.
    The two-dimensional isotropic-normal angle gives agreement 1-acos(rho)/pi.
    """
    r2 = _fraction(explained_variance, 'Explained variance')
    return .5 + asin(sqrt(r2)) / pi


def required_gaussian_r2(accuracy):
    accuracy = _fraction(accuracy, 'Accuracy', lower=.5)
    return sin(pi * (accuracy - .5)) ** 2


def majority_vote_accuracy(accuracy, voters, error_correlation):
    """Exchangeable Bernoulli-error sensitivity, not a fitted ensemble claim."""
    p = _fraction(accuracy, 'Accuracy')
    rho = _fraction(error_correlation, 'Error correlation')
    if isinstance(voters, bool) or not isinstance(voters, int) or voters < 1 or voters % 2 != 1:
        raise ValueError('Voters must be a positive odd integer')
    if voters == 1 or rho == 1 or p in (0., 1.):
        return p
    if rho == 0:
        return float(binom.sf(voters // 2, voters, p))
    concentration = 1 / rho - 1
    return float(betabinom.sf(voters // 2, voters, p * concentration, (1-p) * concentration))


def _vector(value, name, count):
    import numpy as np
    if isinstance(value, (list, tuple, np.ndarray)):
        if len(value) != count:
            raise ValueError(f'{name} must describe all {count} constituents')
        return np.array([_fraction(x, name) for x in value])
    return np.full(count, _fraction(value, name))


def _summary(accuracy):
    import numpy as np
    return dict(overall_accuracy=float(np.mean(accuracy)), minimum_stock_accuracy=float(np.min(accuracy)),
                worst_five_accuracy=float(np.mean(np.sort(accuracy)[:5])),
                per_constituent_accuracy=accuracy.tolist())


def _simulate(base, signal, availability, unknown, correlation, sessions, seed):
    """Generate known scores first, then independent future innovations.

    This is an optimistic information budget with known conditional means,
    not a trained predictor or proof that a proposed network can learn them.
    Daily draws are IID, stocks share latent shocks and feed outages.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    count = len(base)
    def latent():
        return (sqrt(correlation) * rng.normal(size=(sessions, 1))
                + sqrt(1-correlation) * rng.normal(size=(sessions, count)))
    base_score = np.sqrt(base) * latent()
    rich_mean = base_score + np.sqrt(signal-base) * latent()
    available = rng.random((sessions, 1)) < availability
    predicted_up = np.where(available, rich_mean, base_score) > 0
    # Future returns are never read to construct the known scores or availability.
    future_return = rich_mean + np.sqrt(1-signal) * latent()
    answer_known = rng.random((sessions, count)) >= unknown
    correct = (predicted_up == (future_return > 0)) & answer_known
    return dict(seed=seed, predictions=sessions*count, prediction_coverage=1.,
                unresolved_answers=int((~answer_known).sum()), **_summary(correct.mean(axis=0)))


def assess_feasibility(config):
    """Return conditional requirements and all declared simulations for 50 assets.

    No provider data, fitted models, holdout labels or external-target training
    are used. Passing a hypothetical scenario cannot enable empirical flags.
    """
    import numpy as np
    from math import comb
    from .universe import TRAINING_TICKERS
    required = {'constituents', 'horizon', 'target_accuracy', 'sessions', 'seeds', 'scenarios'}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError('Feasibility requires only its explicit configuration fields')
    count = config['constituents']
    if type(count) is not int or count != 50:
        raise ValueError('Feasibility must preserve all 50 constituents')
    if type(config['horizon']) is not int or config['horizon'] not in (1, 5, 20):
        raise ValueError('Feasibility requires a declared 1/5/20-session horizon')
    target = _fraction(config['target_accuracy'], 'Target accuracy', lower=.5)
    sessions, seeds = config['sessions'], config['seeds']
    if type(sessions) is not int or not 126 <= sessions <= 100000:
        raise ValueError('Simulation sessions must be an integer in [126,100000]')
    if (not isinstance(seeds, list) or not 1 <= len(seeds) <= 20
            or any(type(s) is not int or not 0 <= s < 2**32 for s in seeds)
            or len(set(seeds)) != len(seeds)):
        raise ValueError('Declare distinct integer seeds; every seed is retained')
    scenarios = config['scenarios']
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError('Declare at least one information scenario')
    fields = {'name', 'baseline_r2', 'signal_r2', 'signal_availability',
              'unknown_answer_fraction', 'cross_stock_correlation'}
    results, names = [], set()
    for spec in scenarios:
        if not isinstance(spec, dict) or set(spec) != fields:
            raise ValueError('Unknown or missing scenario configuration fields')
        name = spec['name']
        if not isinstance(name, str) or not name or len(name) > 80 or name in names:
            raise ValueError('Scenario names must be nonempty, short and distinct')
        names.add(name)
        base = _vector(spec['baseline_r2'], 'Baseline R2', count)
        signal = _vector(spec['signal_r2'], 'Signal R2', count)
        if (signal < base).any():
            raise ValueError('Rich signal R2 cannot be below baseline in this nested information model')
        q = _vector(spec['signal_availability'], 'Signal availability', count)
        unknown = _vector(spec['unknown_answer_fraction'], 'Unknown answer fraction', count)
        correlation = _fraction(spec['cross_stock_correlation'], 'Cross-stock correlation')
        fallback = np.array([gaussian_sign_accuracy(x) for x in base])
        rich = np.array([gaussian_sign_accuracy(x) for x in signal])
        expected = (1-unknown) * (q*rich + (1-q)*fallback)
        required_accuracy, required_r2, minimum_availability, status = [], [], [], []
        for p, availability, u in zip(fallback, q, unknown, strict=True):
            needed = (target/(1-u) - (1-availability)*p)/availability if availability > 0 and u < 1 else None
            required_accuracy.append(float(needed) if needed is not None else None)
            required_r2.append(required_gaussian_r2(float(needed)) if needed is not None and .5 <= needed <= 1 else (0. if needed is not None and needed < .5 else None))
            minimum_availability.append(float(max(0., (target/(1-u)-p)/(1-p))) if u < 1 and p < 1 else (0. if (1-u)*p >= target else None))
            status.append('fallback_already_sufficient' if (1-u)*p >= target else
                          'unattainable_given_availability_and_answers' if needed is None or needed > 1 else
                          'needs_unverified_signal_quality')
        statistics = _summary(expected)
        results.append(dict(name=name, assumptions=spec, prediction_coverage=1., expected=statistics,
            expected_target_met_under_assumptions=bool(statistics['minimum_stock_accuracy'] >= target
                                                       and statistics['worst_five_accuracy'] >= target),
            five_stock_combinations_covered=comb(count, 5),
            required_signal_accuracy=required_accuracy, required_signal_r2=required_r2,
            requirement_status=status,
            minimum_signal_availability_if_perfect=minimum_availability,
            perfect_signal_accuracy_ceiling=((1-unknown)*(q+(1-q)*fallback)).tolist(),
            simulations=[_simulate(base, signal, q, unknown, correlation, sessions, seed) for seed in seeds]))
    return dict(schema_version=1, evidence_kind='synthetic_assumptions',
        scope='50 abstract constituents; no claim of historical or current membership',
        horizon=config['horizon'], target_accuracy=target, prediction_coverage=1., models_trained=0,
        planned_training_tickers=list(TRAINING_TICKERS), external_targets_used=False,
        holdout_opened=False, empirical_target_met=False, real_world_target_verified=False,
        unconditional_75_expectation_supported=False,
        limitations=['Information strength, transferability and availability are assumptions, not measured forecasts.',
                     'Known Gaussian conditional means are an optimistic information benchmark, not a learned model.',
                     'Zero-mean Gaussian innovations, IID days and outcome-independent missingness are assumptions.',
                     'Monte Carlo variation does not quantify uncertainty about future market performance.',
                     'No empirical or unconditional75claim follows from a conditional scenario passing.'],
        scenarios=results,
        ensemble_sensitivity=[dict(single_accuracy=.6, voters=15, assumed_error_correlation=rho,
            expected_majority_accuracy=majority_vote_accuracy(.6, 15, rho)) for rho in (0., .02, .05, .15, .5, 1.)])
