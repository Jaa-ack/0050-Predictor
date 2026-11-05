import asyncio
import httpx

from twquant.api import create_app


def test_empty_dashboard_is_readonly_and_explains_missing_data(tmp_path):
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),base_url="http://localhost") as client:
            assert (await client.get("/health")).status_code == 200
            result=(await client.get("/api/status")).json()
            assert result["report"] is None
            assert result["monitoring"]["total"] == 0
            page=await client.get("/")
            assert page.status_code == 200
            assert "0050" in page.text
            assert (await client.post("/api/predict")).status_code == 404
            assert (await client.get("/api/forecasts?limit=100000")).status_code == 422
    asyncio.run(check())


def test_source_status_exposes_presence_only_and_rejects_foreign_host(tmp_path,monkeypatch):
    monkeypatch.setenv('FRED_API_KEY','example-protected-server-key')
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(tmp_path)),base_url='http://localhost') as client:
            response=await client.get('/api/sources')
            assert response.status_code==200
            assert 'example-protected-server-key' not in response.text
            assert 'configured_unverified' in response.text
            page=await client.get('/')
            assert 'Content-Security-Policy' in page.headers
            assert page.headers['Cache-Control']=='no-store'
            assert 'example-protected-server-key' not in page.text
            assert (await client.get('/health',headers={'host':'attacker.example'})).status_code==400
    asyncio.run(check())
