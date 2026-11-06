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


@pytest.mark.parametrize('candidate_set,policy,mode', [('base', 'worst_stock_accuracy', 'postclose'), ('factor', 'pooled_brier', 'preopen')])
def test_direction_search_cli_writes_a_separate_research_run(tmp_path, capsys, candidate_set, policy, mode):
    from twquant.data import PriceStore
    from twquant.demo import make_demo_prices
    PriceStore(tmp_path / 'data').write_prices(make_demo_prices(170))
    config = tmp_path / 'direction.json'
    config.write_text(json.dumps(dict(horizons=[1], min_train_dates=30, calibration_dates=10,
                                     test_dates=15, max_folds=1)))
    assert main(['--root', str(tmp_path), 'direction-search', '--config', str(config),
                 '--holdout-dates', '20', '--candidate-set', candidate_set, '--selection-policy', policy, '--mode', mode]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['evidence_kind'] == 'synthetic'
    assert result['real_world_target_verified'] is False
    assert result['holdout_opened'] is False
    assert result['selection_policy'] == policy
    assert result['mode'] == mode
    assert result['candidate_count'] == (8 if candidate_set == 'base' else 10)
    assert (tmp_path / result['report']).exists()
    report = json.loads((tmp_path / result['report']).read_text())
    assert report['protocol']['selection_policy'] == policy
    assert report['protocol']['mode'] == mode
    assert not (tmp_path / 'current.json').exists()


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
