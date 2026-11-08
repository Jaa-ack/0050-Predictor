import json
import pytest

from twquant.cli import main
from twquant.workflows import ledger_for


def test_demo_exercises_replay_maturity_and_latest_prediction(tmp_path,capsys):
    config=tmp_path/"config.json"
    config.write_text(json.dumps(dict(min_train_dates=30,calibration_dates=10,test_dates=15,max_folds=1)))
    assert main(["--root",str(tmp_path),"demo","--sessions","170","--holdout-dates","20","--config",str(config)]) == 0
    result=json.loads(capsys.readouterr().out)
    assert result["forecasts"] == 30
    assert result["readiness"]["passed"] is False
    monitor=ledger_for(tmp_path).monitoring()
    assert monitor["settled"] == 30
    assert monitor["pending"] == 30
    assert ledger_for(tmp_path).monitoring(eligible_only=True)["settled"] == 0


def test_cli_error_boundary_never_echoes_configured_secret(monkeypatch,capsys):
    import twquant.cli as cli
    secret='example-cli-error-protected-secret'
    monkeypatch.setenv('FRED_API_KEY',secret)
    def fail(args):
        raise ValueError('provider unexpectedly echoed '+secret)
    monkeypatch.setattr(cli,'execute',fail)
    assert cli.main(['sources','status'])==2
    output=capsys.readouterr().err
    assert secret not in output
    assert '[REDACTED]' in output


@pytest.mark.parametrize('candidate_set,policy,mode,overseas_window,taifex,taifex_exposure', [
    ('base', 'worst_stock_accuracy', 'postclose', False, False, False),
    ('factor', 'pooled_brier', 'preopen', False, False, False),
    ('market', 'worst_stock_brier', 'preopen', False, False, False),
    ('market', 'pooled_brier', 'preopen', True, False, False),
    ('exposure', 'worst_stock_brier', 'preopen', True, False, False),
    ('exposure', 'pooled_brier', 'preopen', True, True, False),
    ('exposure', 'pooled_brier', 'preopen', True, True, True),
    ('path', 'pooled_brier', 'preopen', True, True, True),
    ('sector', 'pooled_brier', 'preopen', True, True, True),
    ('flow', 'pooled_brier', 'preopen', True, True, True),
    ('opening-proxy', 'pooled_brier', 'preopen', True, True, True),
    ('opening-residual', 'pooled_brier', 'preopen', True, True, True),
    ('margin', 'pooled_brier', 'preopen', True, True, True),
])
def test_direction_search_cli_writes_a_separate_research_run(tmp_path, capsys, candidate_set, policy, mode, overseas_window, taifex, taifex_exposure):
    import pandas as pd
    from twquant.data import PriceStore
    from twquant.demo import make_demo_prices
    prices = make_demo_prices(170)
    if candidate_set in {'flow','opening-proxy','opening-residual','margin'}:
        prices['source']='historical_fixture'  # Verify that synthetic flow alone controls evidence kind.
    PriceStore(tmp_path / 'data').write_prices(prices)
    config = tmp_path / 'direction.json'
    config.write_text(json.dumps(dict(horizons=[1], min_train_dates=30, calibration_dates=10,
                                     test_dates=15, max_folds=1)))
    extra = []
    if overseas_window:
        dates = pd.DatetimeIndex(sorted(prices.date.unique()))
        timestamps = (dates.tz_localize('Asia/Taipei') + pd.Timedelta(hours=6)).tz_convert('UTC')
        events = pd.DataFrame(dict(ticker='*', feature='fmp_spy_close', value=range(100, 100+len(dates)),
                                  published_at=timestamps, available_at=timestamps, source='synthetic'))
        if candidate_set in {'flow','opening-proxy','opening-residual','margin'}: events['source']='historical_fixture'
        path = tmp_path / 'events.csv'
        events.to_csv(path, index=False)
        extra = ['--overseas-window', '--events', str(path)]
    if taifex:
        from test_taifex_history import csv_bytes, record
        rows = []
        for date in dates:
            text = date.strftime('%Y/%m/%d')
            expiry = date.strftime('%Y%m')
            rows.extend([record(date=text, expiry=expiry), record(date=text, expiry=expiry, session='盤後', close=101)])
        history = tmp_path / 'taifex.csv'
        history.write_bytes(csv_bytes(rows))
        extra.extend(['--taifex-history', str(history)])
    if taifex_exposure:
        extra.append('--taifex-exposure')
    if candidate_set in {'sector','flow','opening-proxy','opening-residual','margin'}:
        from test_us_sectors import SYMBOLS
        sector_dates = pd.DatetimeIndex(sorted(prices.date.unique()))
        sector = pd.concat([pd.DataFrame(dict(date=sector_dates,symbol=s,
            adj_close=range(100,100+len(sector_dates)),source='yahoo_historical_chart' if candidate_set in {'flow','opening-proxy','opening-residual','margin'} else 'synthetic')) for s in SYMBOLS],ignore_index=True)
        sector_path = tmp_path/'sectors.csv'
        sector.to_csv(sector_path,index=False)
        extra.extend(['--sector-history',str(sector_path)])
    if candidate_set in {'flow','opening-proxy','opening-residual','margin'}:
        rows=[]
        for ticker in prices.ticker.unique():
            if not ticker.isdigit(): continue
            for date in sector_dates:
                rows.extend([dict(date=date,stock_id=ticker,name='Foreign_Investor',buy=300,sell=100,source='synthetic'),
                    dict(date=date,stock_id=ticker,name='Investment_Trust',buy=100,sell=300,source='synthetic')])
        flow_path=tmp_path/'flow.csv';pd.DataFrame(rows).to_csv(flow_path,index=False)
        if candidate_set in {'opening-proxy','opening-residual','margin'}:
            data=pd.read_csv(flow_path,dtype={'stock_id':str});data['source']='finmind_historical_institutional';data.to_csv(flow_path,index=False)
        if candidate_set in {'opening-proxy','opening-residual'}:
            extra.extend(['--opening-noise-bps','50','--opening-missing-fraction','.1','--opening-seed','29'])
        extra.extend(['--flow-history',str(flow_path)])
    if candidate_set=='margin':
        rows=[]
        for ticker in prices.ticker.unique():
            if not ticker.isdigit():continue
            for date in sector_dates:
                row=dict(date=date,stock_id=ticker,source='synthetic',OffsetLoanAndShort=0)
                for prefix in ('MarginPurchase','ShortSale'):
                    row.update({prefix+'Buy':30,prefix+'Sell':10,prefix+'CashRepayment':10,
                                prefix+'Limit':1000,prefix+'TodayBalance':300,prefix+'YesterdayBalance':200})
                rows.append(row)
        margin_path=tmp_path/'margin.csv';pd.DataFrame(rows).to_csv(margin_path,index=False)
        extra.extend(['--margin-history',str(margin_path)])
    assert main(['--root', str(tmp_path), 'direction-search', '--config', str(config),
                 '--holdout-dates', '20', '--candidate-set', candidate_set, '--selection-policy', policy, '--mode', mode, *extra]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['evidence_kind'] == 'synthetic'
    assert result['real_world_target_verified'] is False
    assert result['holdout_opened'] is False
    assert result['selection_policy'] == policy
    assert result['mode'] == mode
    assert result['overseas_window'] == overseas_window
    assert result['taifex_history'] == taifex
    assert result['taifex_exposure'] == taifex_exposure
    assert result['price_path'] == (candidate_set in {'path','sector','flow','opening-proxy','opening-residual','margin'})
    assert result['sector_history'] == (candidate_set in {'sector','flow','opening-proxy','opening-residual','margin'})
    assert result['flow_history'] == (candidate_set in {'flow','opening-proxy','opening-residual','margin'})
    assert result['candidate_count'] == {'base': 8, 'factor': 10, 'market': 16, 'exposure': 24, 'path': 30, 'sector': 36, 'flow': 42, 'opening-proxy':5, 'opening-residual':9, 'margin':48}[candidate_set]
    assert (tmp_path / result['report']).exists()
    report = json.loads((tmp_path / result['report']).read_text())
    assert report['protocol']['selection_policy'] == policy
    assert report['protocol']['mode'] == mode
    expected_windows = ['f_ext_fmp_spy_window_return', 'f_ext_fmp_spy_window_updated'] if overseas_window else []
    assert report['protocol']['overseas_window_features'] == expected_windows
    provenance = json.loads((tmp_path / result['report']).with_name('data_provenance.json').read_text())
    assert provenance['price_path'] == (candidate_set in {'path','sector','flow','opening-proxy','opening-residual','margin'})
    if candidate_set in {'flow','margin'}:
        assert provenance['flow_history_sha256'] and provenance['flow_sources']==(['synthetic'] if candidate_set=='flow' else ['finmind_historical_institutional'])
        assert len(report['protocol']['institutional_flow_features'])==8
        assert report['protocol']['flow_receipt_contract']['lag_sessions']==2
        assert provenance['flow_receipt_contract']==report['protocol']['flow_receipt_contract']
        added=[c for c in report['protocol']['candidates'] if c['feature_profile']=='portable_flow']
        assert len(added)==6 and all(c['window_dates']==756 for c in added)
        scored={s['candidate'] for s in report['selections'][0]['candidate_scores']}
        assert all(f"{c['model']}:portable_flow:756" in scored for c in added)
    elif candidate_set in {'opening-proxy','opening-residual'}:
        contract=report['protocol']['opening_proxy_contract']
        assert contract['noise_bps']==50 and contract['missing_fraction']==.1 and contract['seed']==29
        assert provenance['opening_proxy_contract']==contract
        assert provenance['evidence_kind']=='synthetic'
        assert len(report['protocol']['opening_proxy_features'])==3
    else:
        assert provenance['flow_history_sha256'] is None and provenance['flow_sources']==[]
        assert report['protocol']['institutional_flow_features']==[]
        assert report['protocol']['flow_receipt_contract'] is None
    if candidate_set=='margin':
        assert result['margin_history'] is True
        assert provenance['margin_history_sha256'] and provenance['margin_sources']==['synthetic']
        assert len(report['protocol']['margin_features'])==18
        assert report['protocol']['margin_receipt_contract']['lag_sessions']==2
        assert report['protocol']['margin_receipt_contract']==provenance['margin_receipt_contract']
        added=[c for c in report['protocol']['candidates'] if c['feature_profile']=='portable_margin']
        assert len(added)==6 and all(c['window_dates']==756 for c in added)
    if candidate_set in {'sector','flow','opening-proxy','opening-residual','margin'}:
        assert provenance['sector_history_sha256']
        assert provenance['sector_sources'] == (['yahoo_historical_chart'] if candidate_set in {'flow','opening-proxy','opening-residual','margin'} else ['synthetic'])
        assert provenance['sector_receipt_assumption'] == 'US17:00 local; original receipt unverified'
        assert len(report['protocol']['us_sector_features']) == 42
        added = [c for c in report['protocol']['candidates'] if c['feature_profile']=='portable_sector']
        assert len(added) == (0 if candidate_set in {'opening-proxy','opening-residual'} else 6) and all(c['window_dates']==756 for c in added)
        scored = {s['candidate'] for s in report['selections'][0]['candidate_scores']}
        assert all(f"{c['model']}:portable_sector:756" in scored for c in added)
    else:
        assert provenance['sector_history_sha256'] is None
        assert provenance['sector_sources'] == []
        assert report['protocol']['us_sector_features'] == []
    path_features = report['protocol']['price_path_features']
    if candidate_set in {'path','sector','flow','opening-proxy','opening-residual','margin'}:
        assert len(path_features) == 13
        assert 'f_path_flat_1_60' in path_features
        assert 'f_path_up_20_252' in path_features
        assert 'f_path_same_sign_1_60' in path_features
        added = [c for c in report['protocol']['candidates'] if c['feature_profile'] == 'portable_path']
        assert {c['model'] for c in added} == (set() if candidate_set in {'opening-proxy','opening-residual'} else {'logistic','hist','extra_trees','random_forest','factor_beta_ridge','factor_beta_vol_ridge'})
        assert all(c['window_dates'] == 756 for c in added)
        scored = {s['candidate'] for s in report['selections'][0]['candidate_scores']}
        assert all(f"{c['model']}:portable_path:756" in scored for c in added)
    else:
        assert path_features == []
    assert provenance['taifex_exposure'] == taifex_exposure
    expected_exposures = sorted('f_ext_taifex_' + code + suffix for code in ('tx', 'te', 'tf')
                                for suffix in ('_beta60', '_beta_night_return')) if taifex_exposure else []
    assert report['protocol']['taifex_exposure_features'] == expected_exposures
    assert provenance['taifex_regular_receipt_assumption'] == ('anchor date 20:00; original receipt unverified' if taifex_exposure else None)
    if taifex:
        assert 'f_ext_taifex_tx_night_return' in report['protocol']['taifex_night_features']
        assert provenance['taifex_history_sha256']
        assert 'reconstructed_taifex_trade_date_0600_no_original_receipt' in provenance['external_availability_basis']
        assert 'taifex_tx_night_return' in provenance['external_expiring_features']
    else:
        assert provenance['taifex_history_sha256'] is None
        assert report['protocol']['taifex_night_features'] == []
    if candidate_set == 'market':
        market = [c for c in report['protocol']['candidates'] if c['feature_profile'] == 'portable_market']
        assert {c['model'] for c in market} == {
            'logistic', 'hist', 'extra_trees', 'random_forest', 'factor_ridge', 'factor_hist'}
        assert all(c['window_dates'] == 756 for c in market)
        scored = {s['candidate'] for s in report['selections'][0]['candidate_scores']}
        assert all(f"{c['model']}:portable_market:756" in scored for c in market)
    if candidate_set == 'exposure':
        added = [c for c in report['protocol']['candidates'] if c['model'].startswith('factor_beta_')]
        assert len(added) == 8
        assert {c['feature_profile'] for c in added} == {'portable_all', 'portable_market'}
        scored = {s['candidate'] for s in report['selections'][0]['candidate_scores']}
        assert all(f"{c['model']}:{c['feature_profile']}:756" in scored for c in added)
    assert not (tmp_path / 'current.json').exists()


def test_sector_search_requires_its_explicit_history_argument(tmp_path,capsys):
    assert main(['--root',str(tmp_path),'direction-search','--candidate-set','sector']) == 2
    assert '--sector-history' in capsys.readouterr().err


def test_flow_search_requires_its_explicit_history_argument(tmp_path,capsys):
    assert main(['--root',str(tmp_path),'direction-search','--candidate-set','flow',
                 '--sector-history',str(tmp_path/'unused.csv')])==2
    assert '--flow-history' in capsys.readouterr().err


def test_direction_simulation_cli_labels_every_result_synthetic(tmp_path, capsys):
    assert main(['--root', str(tmp_path), 'direction-simulate', '--sessions', '380', '--seeds', '7']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['evidence_kind'] == 'synthetic'
    assert result['real_world_target_verified'] is False
    assert len(result['scenarios']) == 5
    report = json.loads((tmp_path / result['report']).read_text())
    assert len(report['experiments']) == 5
    assert report['provider_data_obtained'] is False


def test_direction_search_cli_adds_full_membership_as_transfer_tests_only(tmp_path, capsys):
    import pandas as pd
    from test_constituents import membership_document
    from twquant.data import PriceStore
    from twquant.demo import make_demo_prices
    from twquant.universe import TRAINING_TICKERS
    prices = make_demo_prices(170)
    external = tuple(str(6001 + i) for i in range(40))
    copies = [prices]
    for ticker in external:
        copies.append(prices.loc[prices.ticker.eq('2330')].assign(ticker=ticker))
    PriceStore(tmp_path / 'data').write_prices(pd.concat(copies, ignore_index=True))
    config = tmp_path / 'direction.json'
    config.write_text(json.dumps(dict(horizons=[1], min_train_dates=30, calibration_dates=10,
                                     test_dates=15, max_folds=1, evaluation_tickers=['0050'])))
    document = membership_document()
    document['tickers'] = [*TRAINING_TICKERS, *external]
    document['training_tickers'] = ['6001']  # A document cannot rotate training roles.
    membership = tmp_path / 'membership.json'
    membership.write_text(json.dumps(document), encoding='utf-8')
    assert main(['--root', str(tmp_path), 'direction-search', '--config', str(config),
                 '--holdout-dates', '20', '--membership', str(membership)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['constituents']['constituent_count'] == 50
    assert result['mode'] == 'postclose'
    assert result['constituents']['combination_count'] == 2_118_760
    report_path = tmp_path / result['report']
    report = json.loads(report_path.read_text())
    assert report['protocol']['training_tickers'] == list(TRAINING_TICKERS)
    assert set(report['protocol']['config']['evaluation_tickers']) == {'0050', *external}
    assert (report_path.parent / 'membership_snapshot.json').exists()
    saved = pd.read_parquet(report_path.parent / 'constituent_predictions.parquet')
    assert set(saved.ticker) == set(document['tickers'])


@pytest.mark.parametrize('options,message', [
    (['--candidate-set','margin','--mode','preopen','--sector-history','unused.csv','--flow-history','unused.csv'],'--margin-history'),
    (['--candidate-set','margin'],'--mode preopen'),
    (['--margin-history','unused.csv'],'--margin-history'),
])
def test_margin_study_cli_rejects_missing_history_wrong_clock_or_unrelated_history(options,message,capsys):
    assert main(['direction-search',*options])==2
    assert message in capsys.readouterr().err
