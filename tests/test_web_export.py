import json

import pytest

from twquant.data import PriceStore
from twquant.demo import make_demo_prices
from twquant.web_export import export_dashboard


def report_fixture(root):
    run='example-run'
    folder=root/'runs'/run
    folder.mkdir(parents=True)
    (root/'current.json').write_text(json.dumps({'run_id':run}))
    (folder/'report.json').write_text(json.dumps({'run_id':run,'data_kind':'synthetic','metrics':{},
        'readiness':{'passed':False,'blockers':['synthetic_data']},'n_prices':330,'n_features':3,
        'private_key':'example-snapshot-private-key','machine_path':'/private/user/location',
        'provenance':{'holdout_dates':126,'holdout_start':'2025-07-09','mode':'postclose','machine_path':'/private/user/location'}}))


def test_export_contains_only_synthetic_data_and_never_live_configuration(tmp_path,monkeypatch):
    monkeypatch.setenv('FRED_API_KEY','example-snapshot-private-key')
    root=tmp_path/'demo'
    PriceStore(root/'data').write_prices(make_demo_prices(30))
    report_fixture(root)
    output=tmp_path/'site'/'dist'
    result=export_dashboard(root,output)
    assert result['mode']=='snapshot'
    config=json.loads((output/'app-config.json').read_text())
    assert config['capabilities']['actions'] is False
    status=json.loads((output/'data'/'status.json').read_text())
    assert status['report']['data_kind']=='synthetic'
    assert status['report']['provenance']=={'holdout_dates':126,'holdout_start':'2025-07-09','mode':'postclose'}
    text='\n'.join(p.read_text() for p in output.rglob('*') if p.is_file())
    assert 'example-snapshot-private-key' not in text
    assert '/private/user/location' not in text
    assert not list(output.rglob('*.joblib'))
    assert (output/'data'/'prices-2330.json').is_file()
    assert json.loads((output/'data'/'jobs.json').read_text())['jobs']==[]


def test_export_rejects_real_prices_even_if_report_is_mislabeled(tmp_path):
    root=tmp_path/'real'
    PriceStore(root/'data').write_prices(make_demo_prices(30).assign(source='real_vendor'))
    report_fixture(root)
    with pytest.raises(ValueError,match='synthetic'):
        export_dashboard(root,tmp_path/'dist')


def test_export_destination_cannot_overwrite_source_or_input_data(tmp_path):
    root=tmp_path/'demo'
    PriceStore(root/'data').write_prices(make_demo_prices(30))
    report_fixture(root)
    with pytest.raises(ValueError,match='destination'):
        export_dashboard(root,root)


def test_synthetic_export_retains_transfer_roles_and_grouped_evaluation(tmp_path):
    from twquant.web_export import _report, FORECAST_FIELDS
    report={'data_kind':'synthetic','training_tickers':['2330'],'evaluation_tickers':['IX0001'],
            'metrics':{'transfer':{'IX0001':{'tradable':False}}}}
    result=_report(report)
    assert result['metrics']['transfer']['IX0001']['tradable'] is False
    assert result['training_tickers']==['2330']
    assert {'asset_class','evaluation_role','return_basis','tradable'} <= FORECAST_FIELDS
