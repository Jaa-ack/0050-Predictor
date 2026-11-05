"""Export an explicitly synthetic, credential-free snapshot for static hosting."""
from datetime import datetime,timezone
from pathlib import Path
import shutil

from .credentials import load_credentials,redact
from .data import PriceStore
from .web_data import DashboardData,GITHUB_URL
from .workflows import data_kind,write_json

REPORT_FIELDS={'run_id','created_at','data_kind','readiness','metrics','n_prices','n_features','date_range','scope','interval_note','training_tickers','evaluation_tickers'}
METRIC_FIELDS={'by_horizon','portfolio','feasibility','model','prediction_reference','data_quality','sealed_holdout','transfer','paired_loss_audit'}
FORECAST_FIELDS={'ticker','anchor_date','decision_ts','horizon','model_version','probability_up','expected_return',
    'q10','q50','q90','price_low','price_median','price_high','price_basis','reference_close','reference_adj_close',
    'status','signal','data_kind','features','explanation','actual_return','label_end_date','brier','covered',
    'absolute_error','issued_at','is_replay','asset_class','evaluation_role','return_basis','tradable','training_tickers','arithmetic_expected_return'}


def _report(report):
    if report is None:
        return None
    result={k:v for k,v in report.items() if k in REPORT_FIELDS}
    result['metrics']={k:v for k,v in result.get('metrics',{}).items() if k in METRIC_FIELDS}
    result['provenance']={k:v for k,v in report.get('provenance',{}).items() if k in {'holdout_dates','holdout_start','mode'}}
    return result


def export_dashboard(root,destination):
    root=Path(root).resolve()
    destination=Path(destination).resolve()
    assets=Path(__file__).parent/'static'
    if (destination==root or root in destination.parents or destination in root.parents or
        destination==assets or assets in destination.parents or destination in assets.parents):
        raise ValueError('Export destination must be separate from input data and source')
    if destination.exists() and any(destination.iterdir()) and not (destination/'.twquant-snapshot').is_file():
        raise ValueError('Export destination must be empty or a previous dashboard export')
    data=DashboardData(root)
    status=data.status()
    prices=PriceStore(root/'data').read_prices()
    if not status['report'] or status['report'].get('data_kind')!='synthetic' or data_kind(prices)!='synthetic':
        raise ValueError('Static hosting accepts synthetic demo data only; keep real financial data and credentials local')
    rows=data.forecasts()
    if any(r.get('data_kind')!='synthetic' for r in rows):
        raise ValueError('Static hosting accepts synthetic forecasts only')
    credentials=load_credentials()
    destination.mkdir(parents=True,exist_ok=True)
    public_assets=destination/'assets'
    public_assets.mkdir(exist_ok=True)
    shutil.copyfile(assets/'index.html',destination/'index.html')
    for name in ('app.css','app.js','state.js'):
        if (assets/name).exists():
            shutil.copyfile(assets/name,public_assets/name)
    generated=datetime.now(timezone.utc).isoformat()
    config=dict(mode='snapshot',github_url=GITHUB_URL,capabilities=dict(actions=False),default_workspace='demo',
                snapshot_generated_at=generated,generated_at=generated)
    write_json(destination/'app-config.json',config)
    report=_report(status['report'])
    status=dict(report=report,monitoring=status['monitoring'],promoted=False,time_zone='Asia/Taipei')
    forecasts=[]
    for row in rows:
        record={k:v for k,v in row.items() if k in FORECAST_FIELDS}
        record['valuation']=dict(status='insufficient_data',reason='線上合成快照未使用真實財報')
        forecasts.append(record)
    from .source_workflows import source_status
    from .credentials import Credentials
    sources=dict(sources=source_status(Credentials({})),snapshots=[],sync=None)
    payloads={'status':status,'workspaces':dict(workspaces=[dict(id='demo',name='合成研究快照',data_kind='synthetic',has_report=True)],default='demo'),
        'forecasts':forecasts,'equity':data.equity(),'sources':sources,'monitoring':status['monitoring'],
        'runs':[_report(r) for r in data.runs() if r.get('data_kind')=='synthetic'],'gaps':data.gaps(),'jobs':dict(jobs=[])}
    for ticker in prices.ticker.unique():
        payloads['prices-'+str(ticker)]=data.prices(str(ticker))
    for name,payload in payloads.items():
        write_json(destination/'data'/(name+'.json'),redact(payload,credentials.secret_values()))
    (destination/'.twquant-snapshot').write_text('synthetic-only\n')
    return dict(mode='snapshot',destination=str(destination),files=len(payloads)+5,forecasts=len(forecasts))
