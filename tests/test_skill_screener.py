import json

import numpy as np
import pandas as pd
import pytest


def screen(rows, spec):
    from twquant.skill_screener import screen as evaluate
    return evaluate(pd.DataFrame(rows), spec)


def test_explained_rules_and_weighted_midrank_preserve_every_candidate():
    rows = [{'ticker': '2330', 'growth': 10., 'pe': 20.},
            {'ticker': '2454', 'growth': 20., 'pe': 10.},
            {'ticker': '2881', 'growth': 20., 'pe': None},
            {'ticker': '0050', 'growth': -1., 'pe': 2.}]
    spec = dict(rules=[dict(metric='growth', op='>=', value=0)],
                rank=[dict(metric='growth', order='desc', weight=2),
                      dict(metric='pe', order='asc', weight=1)])
    result = screen(rows, spec)
    by = {r['ticker']: r for r in result}
    assert len(result) == 4 and by['0050']['passed'] is False
    assert by['2454']['score'] == pytest.approx(5 / 6)
    assert by['2881']['score'] == pytest.approx(.75)
    assert by['2881']['rank_gaps'] == ['pe']
    assert by['2330']['checks'][0] == dict(metric='growth', op='>=', value=10.,
                                             threshold=0, status='PASS')


@pytest.mark.parametrize('op,value,expected', [
    ('>', 2, False), ('>=', 2, True), ('<', 2, False), ('<=', 2, True),
    ('==', 2, True), ('!=', 2, False), ('between', [1, 2], True),
    ('in', [1, 2], True), ('not in', [1, 2], False),
])
def test_all_supported_numeric_comparisons(op, value, expected):
    assert screen([dict(ticker='0050', x=2)], dict(rules=[dict(metric='x', op=op, value=value)]))[0]['passed'] is expected


@pytest.mark.parametrize('value', [None, np.nan, np.inf, -np.inf, True, 'bad'])
@pytest.mark.parametrize('missing', ['fail', 'skip'])
def test_unknown_and_invalid_numbers_are_never_passed_silently(value, missing):
    r = screen([dict(ticker='2330', x=value)],
               dict(rules=[dict(metric='x', op='!=', value=0)], missing=missing))[0]
    assert r['passed'] is bool(missing == 'skip' and (value is None or isinstance(value, float) and np.isnan(value)))
    assert r['checks'][0]['status'] in {'MISSING', 'INVALID'}


@pytest.mark.parametrize('spec', [
    dict(rules=[dict(metric='x', op='?', value=1)]),
    dict(rules=[dict(metric='x', op='between', value=[2, 1])]),
    dict(rules=[dict(metric='x', op='>=', value=np.nan)]),
    dict(rules=[dict(metric='x', op='>=', value='1')]),
    dict(rank=[dict(metric='x', weight=0)]),
    dict(rank=[dict(metric='x', weight=-1)]),
    dict(rank=[dict(metric='x', weight=np.inf)]),
    dict(rank=[dict(metric='x'), dict(metric='x')]),
    dict(missing='maybe'), dict(rules='bad'), dict(extra=True),
    dict(rules=[dict(metric='x', op=[], value=1)]), dict(missing=[]),
    dict(rank=[dict(metric='x', order=[])]),
])
def test_malformed_rules_rejected_at_public_interface(spec):
    with pytest.raises(ValueError):
        screen([], spec)


def test_strings_leading_zero_empty_input_and_singleton_ranking(tmp_path):
    from twquant.skill_screener import load_metrics
    path = tmp_path / 'metrics.csv'
    path.write_text('ticker,x\n0050,1\n2330,2\n')
    frame = load_metrics(path)
    assert frame.ticker.tolist() == ['0050', '2330']
    spec = dict(rules=[dict(metric='ticker', op='in', value=['0050'])],
                rank=[dict(metric='x', order='asc')])
    r = screen(frame.to_dict('records'), spec)
    assert r[0]['ticker'] == '0050' and r[0]['score'] == .5
    assert screen([], spec) == []
    path = tmp_path / 'metrics.json'
    path.write_text(json.dumps(dict(rows=frame.to_dict('records'))))
    pd.testing.assert_frame_equal(load_metrics(path), frame, check_dtype=False)
    with pytest.raises(ValueError, match='duplicate'):
        screen([dict(ticker='0050'), dict(ticker='0050')], {})


def test_extreme_finite_ranks_do_not_overflow_and_ties_are_deterministic():
    spec = dict(rank=[dict(metric='x', weight=1e308), dict(metric='y', weight=1e308)])
    rows = [dict(ticker=t, x=1e308, y=-1e308) for t in ['2454', '2330']]
    a = screen(rows, spec)
    assert [r['ticker'] for r in a] == ['2330', '2454']
    assert [r['score'] for r in a] == [.5, .5]
    assert a == screen(list(reversed(rows)), spec)


def test_weight_normalization_uses_only_observed_rank_parts():
    spec = dict(rank=[dict(metric='missing', weight=1e308), dict(metric='x', weight=1e-308)])
    result = screen([dict(ticker='2330', x=1)], spec)
    assert result[0]['score'] == .5
