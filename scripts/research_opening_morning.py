"""Compare public TX tick replays and existing models on the same opening dates."""
import argparse
import json
from pathlib import Path

import pandas as pd

from twquant.direction_audit import load_membership_snapshot
from twquant.ledger import json_safe
from twquant.opening_gap import _metrics
from twquant.taifex_history import opening_tx_median, parse_taifex_history
from twquant.universe import TRAINING_TICKERS


def compare(predictions, sessions, history, constituents, archives, start, end):
    selected = predictions.loc[predictions.phase.eq('final') & predictions.date.between(start, end)]
    days = sessions[sessions.between(start, end)]
    if not len(days) or set(days) != set(selected.date):
        raise ValueError('The diagnostic interval must be covered by the final opening-gap frame')
    prior = dict(zip(sessions, sessions.shift(), strict=True))
    regular = history.loc[history.contract.eq('TX') & history.session.eq('regular')]
    closes = regular.set_index(['trade_date', 'expiry']).close
    prob = {}
    for day in days:
        path = archives / f'Daily_{day.strftime("%Y_%m_%d")}.zip'
        observation = opening_tx_median(path.read_bytes(), day) if path.is_file() else None
        if observation is None:
            continue
        expiry, price, _ = observation
        previous_close = closes.get((prior[day], expiry), 0)
        if previous_close > 0:
            prob[day] = .75 if price > previous_close else .25
    scores = {}
    policies = dict(selected_model=selected.probability_up,
                    overnight_rule=selected.control_overnight_sign, morning_tx_rule=selected.date.map(prob))
    for name, values in policies.items():
        panel = selected.assign(probability_up=values)
        scores[name] = {scope: _metrics(panel.loc[panel.ticker.isin(symbols)], symbols)
                        for scope, symbols in [('fixed_ten', TRAINING_TICKERS), ('constituents', constituents)]}
    return dict(evidence_kind='retrospective_exchange_time_replay', received_in_real_time_verified=False,
                start=days.iloc[0].date().isoformat(), end=days.iloc[-1].date().isoformat(),
                market_sessions=len(days), morning_input_sessions=len(prob), policies=scores)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('predictions', 'calendar', 'taifex-history', 'membership', 'archives'):
        parser.add_argument(f'--{name}', type=Path, required=True)
    for name in ('start', 'end'):
        parser.add_argument(f'--{name}', type=pd.Timestamp, required=True)
    args = parser.parse_args()
    result = compare(pd.read_parquet(args.predictions),
                     pd.read_csv(args.calendar, usecols=['date'], parse_dates=['date']).date,
                     parse_taifex_history(args.taifex_history.read_bytes()),
                     load_membership_snapshot(args.membership)['tickers'], args.archives, args.start, args.end)
    print(json.dumps(json_safe(result), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
