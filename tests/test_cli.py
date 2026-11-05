import json

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
