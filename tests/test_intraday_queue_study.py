import json

from twquant.cli import main
from twquant.universe import TRAINING_TICKERS


def test_queue_study_freezes_before_fitting_preserves_all_controls_and_does_not_load_credentials(tmp_path,capsys,monkeypatch):
    import twquant.cli as cli
    def forbidden():raise AssertionError('Offline queue study cannot load credentials')
    monkeypatch.setattr(cli,'load_dotenv',forbidden)
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',
        source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',
        retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=tmp_path/'queue.json'
    config.write_text(json.dumps(dict(train_dates=30,calibration_dates=10,test_dates=126,seeds=[17],
        horizons_seconds=[30],scenarios=[dict(name='fixture',flow_imbalance=.8,flip_rate=.002)])))
    output=tmp_path/'study'
    args=['direction-queue-study','--config',str(config),'--membership',str(membership),'--out',str(output)]
    assert main(args)==0
    response=json.loads(capsys.readouterr().out)
    report=json.loads((output/'report.json').read_text())
    protocol=json.loads((output/'protocol.json').read_text())
    assert response['evidence_kind']=='synthetic_queue_process'
    assert not report['unconditional_75_expectation_supported']
    assert not report['real_world_target_verified']
    assert protocol['training_tickers']==list(TRAINING_TICKERS)
    assert len(protocol['source_sha256'])==64
    run=report['experiments'][0]
    assert run['selection_uses_external_targets'] is False
    assert run['training_rows']==300 and run['calibration_rows']==100
    assert len(run['candidate_calibration'])==len(run['candidate_test_audits'])==4
    assert run['selected_test_audit']['expected_predictions']==126*50
    assert run['selected_test_audit']['five_stock_combinations']==2118760
    assert not run['selected_test_audit']['empirical_target_met']
    assert (output/run['predictions']).exists()
    assert main(args)==2
    assert 'fresh' in capsys.readouterr().err


def test_mixture_study_keeps_a_pure_training_reference_outside_candidate_selection(tmp_path):
    from twquant.intraday_queue_study import run_queue_study
    members=list(TRAINING_TICKERS)+[str(4000+i) for i in range(40)]
    membership=tmp_path/'membership.json'
    membership.write_text(json.dumps(dict(tickers=members,count=50,as_of='2026-11-06',
        source_url='https://www.yuantaetfs.com/product/detail/0050/ratio',
        retrieved_at='2026-11-07T00:00:00+00:00',html_sha256='a'*64)))
    config=dict(train_dates=30,calibration_dates=10,test_dates=126,seeds=[17],horizons_seconds=[30],
        scenarios=[dict(name='mixed',persistent_fraction=.5)],
        reference_training_scenario=dict(name='pure_reference',persistent_fraction=1.))
    output=tmp_path/'mixture'
    report=run_queue_study(config,membership,output)
    run=report['experiments'][0]
    assert report['models_trained']==5
    assert run['frozen_reference']['model']=='history_logistic'
    assert run['frozen_reference']['training_details']['training_rows']==300
    assert set(run['frozen_reference']['training_details']['training_tickers'])==set(TRAINING_TICKERS)
    assert len(run['candidate_calibration'])==4
    assert run['frozen_reference']['used_for_selection'] is False
    import pandas as pd
    p=pd.read_parquet(output/run['predictions'])
    assert 'diagnostic_persistent_regime' in p and 'probability_up_frozen_reference' in p
    assert p.probability_up_frozen_reference.between(0,1).all()
    protocol=json.loads((output/'protocol.json').read_text())
    assert protocol['reference_training_scenario']['persistent_fraction']==1.
    assert 'scikit-learn' in protocol['environment']['packages']
