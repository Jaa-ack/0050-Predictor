"""Local dashboard reads and opt-in workflow actions; provider secrets stay server-side."""
from pathlib import Path
from typing import Literal
import base64
import hashlib
import re
import secrets

from fastapi import FastAPI,HTTPException,Query,Request
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .credentials import load_credentials,redact
from .ledger import json_safe
from .web_data import DashboardData,GITHUB_URL
from .web_jobs import JobManager


class JobRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:Literal['demo','sync','research','holdout','predict','settle']
    workspace:str


def create_app(root:Path,workspaces=None,enable_actions=False,job_manager=None):
    data=DashboardData(root,workspaces)
    credentials=load_credentials()
    manager=job_manager or JobManager(data.workspaces,Path.cwd(),credentials)
    nonce=secrets.token_urlsafe(32)
    assets=Path(__file__).parent/'static'
    app=FastAPI(title='0050 Predictor Research Workspace',version='0.2.0')
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','[::1]'])
    app.mount('/assets',StaticFiles(directory=assets),name='assets')

    def response(value,status_code=200):
        return JSONResponse(json_safe(redact(value,credentials.secret_values())),status_code=status_code)

    def selected(workspace):
        try:
            data.resolve(workspace)
        except ValueError:
            raise HTTPException(404,'Unknown workspace') from None
        return workspace

    @app.middleware('http')
    async def private_local_headers(request,call_next):
        result=await call_next(request)
        result.headers['Cache-Control']='no-store'
        result.headers['X-Content-Type-Options']='nosniff'
        result.headers['X-Frame-Options']='DENY'
        result.headers['Referrer-Policy']='no-referrer'
        if request.url.path=='/':
            inline=re.findall(r'<script>([\s\S]*?)</script>',(assets/'index.html').read_text())
            hashes=' '.join("'sha256-"+base64.b64encode(hashlib.sha256(s.encode()).digest()).decode()+"'" for s in inline)
            result.headers['Content-Security-Policy']=(f"default-src 'self'; script-src 'self' {hashes}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return result

    @app.get('/health')
    def health():
        return {'status':'ok'}

    @app.get('/')
    def index():
        return FileResponse(assets/'index.html')

    @app.get('/app-config.json')
    def config():
        return dict(mode='local',github_url=GITHUB_URL,capabilities=dict(actions=enable_actions),default_workspace=data.default)

    @app.get('/api/workspaces')
    def workspace_list():
        return response(data.workspace_list())

    @app.get('/api/status')
    def status(workspace:str|None=None):
        return response(data.status(selected(workspace)))

    @app.get('/api/forecasts')
    def forecasts(workspace:str|None=None,limit:int=Query(3000,ge=1,le=3000)):
        return response(data.forecasts(selected(workspace),limit))

    @app.get('/api/monitoring')
    def monitoring(workspace:str|None=None):
        return response(data.status(selected(workspace))['monitoring'])

    @app.get('/api/sources')
    def sources(workspace:str|None=None):
        return response(data.sources(selected(workspace)))

    @app.get('/api/equity')
    def equity(workspace:str|None=None):
        return response(data.equity(selected(workspace)))

    @app.get('/api/runs')
    def runs(workspace:str|None=None):
        return response(data.runs(selected(workspace)))

    @app.get('/api/prices')
    def prices(ticker:str=Query('2330',pattern=r'^[A-Za-z0-9^_.-]{1,20}$'),workspace:str|None=None,limit:int=Query(250,ge=1,le=1000)):
        return response(data.prices(ticker,selected(workspace),limit))

    @app.get('/api/gaps')
    def gaps():
        return response(data.gaps())

    @app.get('/api/jobs')
    def jobs():
        return response(dict(jobs=manager.list()))

    @app.get('/api/session')
    def session():
        if not enable_actions:
            raise HTTPException(403,'Workflow actions are disabled')
        return response(dict(action_token=nonce))

    @app.post('/api/jobs',status_code=202)
    async def start_job(body:JobRequest,request:Request):
        if not enable_actions or not secrets.compare_digest(request.headers.get('X-TWQuant-Token',''),nonce):
            raise HTTPException(403,'Local workflow token required')
        origin=request.headers.get('Origin')
        if origin and origin!=str(request.base_url).rstrip('/'):
            raise HTTPException(403,'Same-origin workflow request required')
        selected(body.workspace)
        try:
            return response(manager.submit(body.action,body.workspace),202)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409,str(exc)) from None

    return app
