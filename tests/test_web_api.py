import asyncio

import httpx

from twquant.api import create_app
from twquant.credentials import Credentials
from twquant.web_jobs import JobManager


def test_workspace_selection_prices_runs_and_gaps_are_safe(tmp_path):
    from twquant.data import PriceStore
    from twquant.demo import make_demo_prices
    demo=tmp_path/'demo'
    real=tmp_path/'real'
    PriceStore(demo/'data').write_prices(make_demo_prices(30))
    async def check():
        app=create_app(demo,workspaces={'demo':demo,'real':real})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
            config=(await c.get('/app-config.json')).json()
            assert config['mode']=='local'
            workspaces=(await c.get('/api/workspaces')).json()
            assert {w['id'] for w in workspaces['workspaces']}=={'demo','real'}
            prices=(await c.get('/api/prices?ticker=2330&workspace=demo')).json()
            assert len(prices['rows'])==30
            assert prices['data_kind']=='synthetic'
            assert (await c.get('/api/status?workspace=../../secrets')).status_code==404
            assert (await c.get('/api/runs?workspace=real')).json()==[]
            assert (await c.get('/api/gaps')).json()['items']
            assert (await c.get('/assets/../credentials.py')).status_code==404
    asyncio.run(check())


def test_local_jobs_require_nonce_and_same_origin_and_never_expose_provider_keys(tmp_path,monkeypatch):
    secret='example-server-job-test-key'
    monkeypatch.setenv('FRED_API_KEY',secret)
    demo=tmp_path/'demo'
    manager=JobManager({'demo':demo},tmp_path,Credentials({'FRED_API_KEY':secret}),lambda *a:(0,'done'))
    async def check():
        app=create_app(demo,workspaces={'demo':demo},enable_actions=True,job_manager=manager)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as c:
            payload={'action':'demo','workspace':'demo'}
            assert (await c.post('/api/jobs',json=payload)).status_code==403
            token=(await c.get('/api/session')).json()['action_token']
            assert token!=secret
            headers={'X-TWQuant-Token':token,'Origin':'https://attacker.example'}
            assert (await c.post('/api/jobs',json=payload,headers=headers)).status_code==403
            headers['Origin']='http://localhost'
            response=await c.post('/api/jobs',json=payload,headers=headers)
            assert response.status_code==202
            assert secret not in response.text
            bad=await c.post('/api/jobs',json={'action':'shell','workspace':'demo'},headers=headers)
            assert bad.status_code==422
    asyncio.run(check())


def test_opening_routes_use_only_the_configured_archives(tmp_path):
    import json
    import pandas as pd
    study = tmp_path / 'opening'
    study.mkdir()
    (study / 'report.json').write_text(json.dumps({
        'task': '0859_open_above_previous_session_close', 'evidence_kind': 'historical',
        'final': {'constituents': {'evaluation_tickers': ['2330'], 'overall_accuracy': 1, 'expected_predictions': 1}},
        'folds': [{'model': 'context_tree', 'model_version': 'f1',
                   'candidate_features': {'context_tree': ['f_return_1']}}],
    }))
    prediction = dict(date=pd.Timestamp('2026-11-05'), ticker='2330', phase='final',
                       probability_up=.8, actual_return=.01, factor_base=.5, impact_price=.3,
                       f_return_1=.02, model_version='f1', private_payload='private-data-marker')
    pd.DataFrame([prediction, {**prediction, 'ticker': '0050'}]).to_parquet(
        study / 'predictions.parquet', index=False)
    async def check():
        app = create_app(tmp_path, opening_study=study)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as c:
            summary = (await c.get('/api/opening')).json()
            assert summary['history']['days'][0]['date'] == '2026-11-05'
            assert summary['history']['days'][0]['expected'] == 1
            result = await c.get('/api/opening/predictions?view=history&date=2026-11-05')
            assert result.status_code == 200
            row = result.json()['rows'][0]
            assert row['groups'][0]['features'][0] == {'key': 'f_return_1', 'value': .02}
            assert row['groups'][0]['impact'] == .3
            transfer = result.json()['rows'][1]
            assert transfer['ticker'] == '0050' and transfer['transfer'] and not transfer['constituent']
            assert 'private-data-marker' not in result.text
            assert (await c.get('/api/opening/predictions?view=../')).status_code == 422
            assert (await c.get('/legacy.html')).status_code == 404
            assert summary['history']['diagnostics']['price']['features'][0]['key'] == 'f_return_1'
            assert summary['history']['diagnostics']['price']['impact_share'] == 1
    asyncio.run(check())


def test_opening_diagnostics_keeps_correlation_separate_from_impact():
    import pandas as pd
    from pytest import approx
    from twquant.opening_web import _diagnostics

    frame = pd.DataFrame(dict(actual_up=[0., 0., 1., 1., None], rising=[0., 0., 1., 1., 5.],
                              falling=[1., 1., 0., 0., 5.], impact_price=[.3] * 5,
                              impact_night=[-.1] * 5))
    result = _diagnostics(frame, {'price':['rising'], 'night':['falling']})
    assert result['price']['features'][0]['correlation'] == approx(1)
    assert result['night']['features'][0]['correlation'] == approx(-1)
    assert result['price']['features'][0]['samples'] == 4
    assert result['price']['impact_share'] == approx(.75)
