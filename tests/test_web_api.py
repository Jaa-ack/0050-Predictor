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
