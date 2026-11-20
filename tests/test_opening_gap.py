import pandas as pd

from twquant import opening_gap
from twquant.universe import TRAINING_TICKERS


def history(sessions, symbols):
    rows = []
    for i, date in enumerate(sessions):
        previous = date.tz_localize('Asia/Taipei')
        upcoming = (sessions[i + 1].tz_localize('Asia/Taipei')
                    if i < len(sessions) - 1 else pd.NaT)
        for ticker in symbols:
            rows.append(dict(date=date, ticker=ticker, open=101 if i % 2 else 100,
                             close=100, volume=500, available_at=previous + pd.Timedelta(hours=20),
                             decision_ts=upcoming + pd.Timedelta(hours=8, minutes=30),
                             f_return_1=.01 if i % 2 == 0 else -.01, f_rvol_20=1,
                             f_ext_taifex_tx_night_return=.01 if i % 2 == 0 else -.01,
                             f_ext_taifex_tx_night_return_age_days=.1, f_openref_return=.99))
    return pd.DataFrame(rows)


def test_fixed_cutoff_never_uses_trial_updates_at_cutoff_or_imagined_open():
    days = pd.bdate_range('2024-01-08', periods=4)
    frame = history(days, ['2330'])
    frame.loc[frame.date.eq(days[1]), 'open'] = 120
    events = pd.DataFrame([
        dict(session_date=days[1], ticker='2330', feature='auction_indicative_price',
             value=101, published_at='2024-01-09T08:58:45+08:00',
             available_at='2024-01-09T08:58:50+08:00'),
        dict(session_date=days[1], ticker='2330', feature='auction_indicative_price',
             value=125, published_at='2024-01-09T08:58:56+08:00',
             available_at='2024-01-09T08:59:00+08:00'),
    ])
    panel = opening_gap.build_opening_gap_panel(frame, days, ['2330'], events=events)
    assert panel.date.tolist() == days[1:].tolist()
    assert abs(panel.iloc[0].f_open_auction_gap - .01) < 1e-12
    assert abs(panel.iloc[0].actual_return - .20) < 1e-12
    assert pd.isna(panel.iloc[1].f_open_auction_gap)
    assert 'f_openref_return' not in panel


def test_missing_session_and_missing_prior_close_stay_in_score_grid():
    days = pd.bdate_range('2024-01-08', periods=5)
    frame = history(days, ['2330'])
    frame = frame.loc[frame.date.ne(days[1])]
    frame.loc[frame.date.eq(days[3]), 'volume'] = 0
    panel = opening_gap.build_opening_gap_panel(frame, days, ['2330', '1216'])
    core = panel.loc[panel.ticker.eq('2330')].reset_index(drop=True)
    assert len(panel) == 8
    assert core.actual_return.isna().tolist() == [True, True, True, True]
    assert pd.isna(core.iloc[1].f_return_1)


def test_external_truth_cannot_change_selection_and_missing_labels_cost_points():
    days = pd.bdate_range('2023-01-02', periods=114)
    symbols = (*TRAINING_TICKERS, '1216', '2002', '2412', '0050', 'IX0001')
    frame = history(days, symbols)
    missing = frame.ticker.eq('2002') & frame.date.eq(days[-1])
    frame.loc[missing, 'open'] = float('nan')
    arguments = dict(membership_as_of='2023-06-01', evidence_kind='synthetic', fold_dates=20, calibration_dates=20,
                     final_dates=20, min_train_dates=30)
    panel = opening_gap.build_opening_gap_panel(frame, days, symbols)
    result = opening_gap.validate_opening_gap(panel, (*TRAINING_TICKERS, '1216', '2002'), **arguments)
    frame.loc[~frame.ticker.isin(TRAINING_TICKERS) & ~missing, 'open'] = 80
    alternate = opening_gap.validate_opening_gap(
        opening_gap.build_opening_gap_panel(frame, days, symbols),
        (*TRAINING_TICKERS, '1216', '2002'), **arguments)
    left = result.predictions
    right = alternate.predictions
    pd.testing.assert_series_equal(left.probability_up, right.probability_up)
    assert result.report['final']['constituents']['expected_predictions'] == 240
    assert result.report['final']['constituents']['unresolved_labels'] == 1
    assert result.report['real_world_target_verified'] is False
    assert result.report['evidence_kind'] == 'synthetic'
    assert result.report['final']['constituents']['evidence_kind'] == 'synthetic'
    assert len(result.report['final']['transfers']) == 5
    for fold in result.report['folds']:
        assert fold['train_label_available_max'] < fold['calibration_decision_start']
        assert fold['calibration_label_available_max'] < fold['test_decision_start']


def test_unavailable_control_never_earns_credit_for_a_flat_open():
    predictions = pd.DataFrame(dict(ticker=['2330', '2330'], actual_return=[0., 0.],
                                    probability_up=[float('nan'), .25]))
    assert opening_gap._metrics(predictions, ['2330'])['accuracy'] == .5


def test_probability_contributions_reconstruct_the_issued_score():
    model = opening_gap.OpeningGapModel('overnight_sign', .60, (
        'f_ext_taifex_tx_night_return', 'f_ext_fmp_spy_window_return'))
    rows = pd.DataFrame({'f_ext_taifex_tx_night_return': [.10, float('nan'), float('nan')],
                         'f_ext_fmp_spy_window_return': [-.01, -.01, float('nan')]})
    impacts = model.explain(rows)
    pd.testing.assert_series_equal(impacts.factor_base + impacts.filter(like='impact_').sum(axis=1),
                                   pd.Series(model.predict(rows)), check_names=False)
    assert impacts.impact_night.tolist() == [.50, 0., 0.]
    assert impacts.impact_us.tolist() == [-.35, -.35, 0.]
