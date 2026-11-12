import json

import pandas as pd
import pytest

from twquant.cli import main


def test_screen_cli_preserves_all_rows_and_exports_csv_json(tmp_path, capsys):
    metrics = tmp_path / 'metrics.csv'
    metrics.write_text('ticker,f_rvol_20\n0050,2\n2330,0\n2454,\n')
    rules = tmp_path / 'rules.json'
    rules.write_text(json.dumps(dict(rules=[dict(metric='f_rvol_20', op='>=', value=1)])))
    for ext in ['json', 'csv']:
        output = tmp_path / f'result.{ext}'
        assert main(['screen', str(metrics), '--rules', str(rules), '--out', str(output)]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['total'] == 3 and result['passed'] == 1
        if ext == 'json':
            assert len(json.loads(output.read_text())) == 3
        else:
            rows = pd.read_csv(output, dtype={'ticker': str})
            assert rows.ticker.tolist() == ['0050', '2330', '2454']


def test_skill_study_cli_uses_only_existing_snapshot_and_writes_no_model_pointer(tmp_path, capsys):
    from twquant.data import PriceStore
    from twquant.demo import make_demo_prices
    prices = make_demo_prices(145)
    PriceStore(tmp_path / 'data').write_prices(prices)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(dict(horizons=[1], min_train_dates=30, calibration_dates=10,
                                     test_dates=10, max_folds=1)))
    output = tmp_path / 'experiment'
    options = ['--root', str(tmp_path), 'skill-study', '--config', str(config),
               '--holdout-dates', '15', '--out', str(output)]
    assert main(options) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['data_kind'] == 'synthetic' and result['holdout_opened'] is False
    assert (output / 'report.json').is_file()
    assert not (tmp_path / 'current.json').exists()
    assert not list(tmp_path.rglob('*.joblib'))
    assert main(options) == 2
    assert 'fresh' in capsys.readouterr().err
