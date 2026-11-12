"""Paper-only target-portfolio checks inspired by both trader-risk versions.

Independent implementation: no broker tickets, quote fetching or order state.
Weights are fractions of NAV committed INCLUDING entry fees. The full-loss
budget is committed principal, not a stop-loss guarantee or a future VaR.
"""
from dataclasses import dataclass, asdict
import math

from .skill_screener import finite_number
from .universe import asset_class, tickers, TRAINING_TICKERS, DEFAULT_EVALUATION_TICKERS


TRADABLE_RESEARCH_ASSETS = frozenset(t for t in (*TRAINING_TICKERS, *DEFAULT_EVALUATION_TICKERS)
                                    if asset_class(t) != 'index')


@dataclass(frozen=True)
class RiskLimits:
    max_weight: float = .2
    max_exposure: float = .8
    min_cash: float = .1
    max_single_volatility: float = .04
    max_portfolio_volatility: float = .02
    max_capital_at_risk: float = .8

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not finite_number(value) or not 0 <= value <= 1 or value == 0 and name != 'min_cash':
                raise ValueError(f'Invalid risk limit: {name}')


def check_target_weights(weights, volatility, limits=None):
    """Atomic PASS/BLOCK; unknown risk blocks an invested portfolio.

    sum(weight * trailing daily sigma) assumes perfect positive correlation;
    it is a historical volatility bound, not a maximum future loss. Zero-weight
    assets need no sigma. Cash and concentration use the full initial NAV.
    Only the project's declared equities/ETF are tradable; unknown identifiers
    and index aliases cannot gain permission through a ticker-shaped string.
    """
    limits = limits or RiskLimits()
    reasons = []
    valid = isinstance(weights, dict) and isinstance(volatility, dict)
    if valid:
        try:
            for ticker, weight in weights.items():
                tickers([ticker])
                if not isinstance(ticker, str) or ticker not in TRADABLE_RESEARCH_ASSETS:
                    reasons.append('nontradable_or_unknown_asset')
                if not finite_number(weight) or not 0 <= weight <= 1:
                    valid = False
                elif weight > 0 and (not finite_number(volatility.get(ticker)) or volatility[ticker] < 0):
                    valid = False
        except (ValueError, TypeError):
            valid = False
    if not valid:
        return dict(verdict='BLOCK', reasons=['invalid_or_missing_input'],
                    cash_fraction=None, committed_fraction=None, volatility_bound=None)
    total = math.fsum(weights.values())
    try:
        bound = math.fsum(w * volatility[t] for t, w in weights.items() if w > 0)
    except OverflowError:
        bound = math.inf
    eps = 1e-12
    if any(w > limits.max_weight + eps for w in weights.values()):
        reasons.append('single_weight')
    if total > limits.max_exposure + eps:
        reasons.append('gross_exposure')
    if 1 - total < limits.min_cash - eps:
        reasons.append('cash_buffer')
    if total > limits.max_capital_at_risk + eps:
        reasons.append('capital_at_risk')
    if any(w > 0 and volatility[t] > limits.max_single_volatility + eps for t, w in weights.items()):
        reasons.append('single_volatility')
    if bound > limits.max_portfolio_volatility + eps:
        reasons.append('portfolio_volatility')
    return dict(verdict='BLOCK' if reasons else 'PASS', reasons=sorted(set(reasons)),
                cash_fraction=1 - total, committed_fraction=total,
                volatility_bound=bound if math.isfinite(bound) else None)
