import json

from twquant.cli import main


def test_feasibility_cli_runs_without_data_keys_or_models_and_never_claims_empirical_success(tmp_path, capsys):
    from test_direction_feasibility import planning_config
    config = tmp_path/'assumptions.json'; config.write_text(json.dumps(planning_config()))
    output = tmp_path/'design'
    args = ['--root', str(tmp_path/'no_provider_data'), 'direction-feasibility',
            '--config', str(config), '--out', str(output)]
    assert main(args) == 0
    response = json.loads(capsys.readouterr().out)
    report = json.loads((output/'report.json').read_text())
    protocol = json.loads((output/'protocol.json').read_text())
    assert response['evidence_kind'] == 'synthetic_assumptions'
    assert response['real_world_target_verified'] is False
    assert report['empirical_target_met'] is False
    assert report['unconditional_75_expectation_supported'] is False
    assert protocol['assumptions'] == planning_config()
    assert len(protocol['source_sha256']) == len(protocol['assumptions_sha256']) == 64
    assert not list(tmp_path.rglob('*.joblib'))
    assert not (tmp_path/'no_provider_data').exists()
    assert main(args) == 2
    assert 'fresh' in capsys.readouterr().err


def test_feasibility_does_not_read_server_credentials(tmp_path, capsys, monkeypatch):
    from test_direction_feasibility import planning_config
    import twquant.cli as cli
    def forbidden_credentials():
        raise AssertionError('Offline feasibility must not read credentials')
    monkeypatch.setattr(cli, 'load_dotenv', forbidden_credentials)
    config = tmp_path/'assumptions.json'; config.write_text(json.dumps(planning_config()))
    assert main(['direction-feasibility', '--config', str(config), '--out', str(tmp_path/'design')]) == 0
    assert json.loads(capsys.readouterr().out)['real_world_target_verified'] is False
