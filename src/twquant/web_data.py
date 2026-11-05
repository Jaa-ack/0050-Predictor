"""Read models for the dashboard; workspace paths are server-side allowlisted."""
from pathlib import Path
import json

import pandas as pd

from .data import PriceStore
from .workflows import current_report,ledger_for,data_kind

GITHUB_URL='https://github.com/Jaa-ack/0050-Predictor'
GAPS=[
    dict(id='data-access',priority='P0',category='資料',title='驗證私人來源與長期還原行情',status='pending',description='完成七個來源的Key、端點權限、配額及十年基準歷史核對。',acceptance='逐來源記錄實際起訖、缺漏率、權限與還原規則。'),
    dict(id='pit',priority='P0',category='資料',title='補齊歷史公告與修正版本',status='pending',description='今天下載的財報與分析師預期不能回填過去，需原發布及可用時間。',acceptance='通過延遲公告、同日更正及跨市場時區案例。'),
    dict(id='membership',priority='P0',category='資料',title='歷史成分股與交易日曆',status='partial',description='已有持股下載，仍需確認歷史涵蓋、公告與生效日期及臨時休市。',acceptance='保存當時股票池，區分固定十檔與歷史前十大策略。'),
    dict(id='fundamentals',priority='P0',category='估值',title='財務單位與估值自動供料',status='partial',description='DCF與金融P/B已實作，尚需TTM、金額、股數及產業科目映射。',acceptance='核對真實公告、每股估值、幣別與情境假設。'),
    dict(id='overnight',priority='P0',category='因子',title='夜盤合約與跨市場對齊',status='pending',description='驗證歷史分K、日夜盤、換月與08:30／20:00資料截止。',acceptance='保留實際合約，避免跨決策時間的未來資訊。'),
    dict(id='events',priority='P1',category='因子',title='新聞及政府資料事件模型',status='partial',description='已能保存去重，實體、事件、情緒及意外程度仍待驗證。',acceptance='提供歷史標註與消融實驗。'),
    dict(id='validation',priority='P1',category='驗證',title='真實樣本外與封存測試',status='pending',description='合成資料已驗證流程，真實投資優勢尚待證明。',acceptance='凍結程序，完成保留期、成本壓力及時間區塊抽樣。'),
    dict(id='paper',priority='P1',category='驗證',title='累積真實每日影子紀錄',status='pending',description='歷史重播不計入真實逐日證據。',acceptance='至少60個交易日與500筆成熟一日預測。'),
    dict(id='operations',priority='P1',category='營運',title='正式排程、告警與復原',status='pending',description='本機操作已具備，正式背景排程及備份演練仍待設定。',acceptance='驗證中斷、復原與回滾，不自動下單。'),
    dict(id='advanced',priority='P2',category='模型',title='以增益決定進階模型',status='pending',description='目前為線性基準與提升樹；市場alpha、條件分位數及SHAP尚待加入。',acceptance='相同成本與切分下，取得穩定樣本外增益。')
]


class DashboardData:
    def __init__(self,root,workspaces=None):
        self.root=Path(root).resolve()
        self.workspaces={k:Path(v).resolve() for k,v in (workspaces or {'current':self.root}).items()}
        self.default=next((k for k,v in self.workspaces.items() if v==self.root),next(iter(self.workspaces)))

    def resolve(self,workspace=None):
        key=workspace or self.default
        if key not in self.workspaces:
            raise ValueError('Unknown workspace')
        return self.workspaces[key]

    def workspace_list(self):
        rows=[]
        for key,root in self.workspaces.items():
            try:
                report=current_report(root)
            except (ValueError,FileNotFoundError):
                report=None
            rows.append(dict(id=key,name={'demo':'合成研究示範','real':'實際資料研究','current':'目前研究環境'}.get(key,key),data_kind=report['data_kind'] if report else 'synthetic' if key=='demo' else 'historical',has_report=bool(report)))
        return dict(workspaces=rows,default=self.default)

    def status(self,workspace=None):
        root=self.resolve(workspace)
        try:
            report=current_report(root)
        except (ValueError,FileNotFoundError):
            report=None
        return dict(report=report,monitoring=ledger_for(root).monitoring(),promoted=(root/'champion.json').exists(),time_zone='Asia/Taipei')

    def forecasts(self,workspace=None,limit=3000):
        rows=ledger_for(self.resolve(workspace)).forecasts()
        rows.sort(key=lambda r:(r['decision_ts'],r.get('issued_at','')),reverse=True)
        return rows[:limit]

    def equity(self,workspace=None):
        root=self.resolve(workspace)
        report=self.status(workspace)['report']
        if report is None:
            return []
        path=root/'runs'/report['run_id']/'equity.csv'
        return pd.read_csv(path).to_dict('records') if path.exists() else []

    def runs(self,workspace=None):
        rows=[]
        for path in self.resolve(workspace).glob('runs/*/report.json'):
            report=json.loads(path.read_text())
            rows.append({k:report.get(k) for k in ['run_id','created_at','data_kind','readiness','metrics','n_features','n_prices','date_range']})
        return sorted(rows,key=lambda r:r.get('created_at') or '',reverse=True)[:30]

    def prices(self,ticker,workspace=None,limit=250):
        try:
            frame=PriceStore(self.resolve(workspace)/'data').read_prices()
        except FileNotFoundError:
            return dict(ticker=ticker,source=None,data_kind='unknown',rows=[],available_at_max=None)
        prices=frame[frame.ticker==ticker].sort_values('date').tail(limit)
        rows=[dict(date=str(r.date.date()),close=r.close,adj_close=r.adj_close,volume=r.volume) for r in prices.itertuples()]
        return dict(ticker=ticker,source=sorted(prices.source.unique().tolist()),data_kind=data_kind(frame),rows=rows,available_at_max=prices.available_at.max().isoformat() if len(prices) else None)

    def sources(self,workspace=None):
        from .credentials import load_credentials
        from .source_store import SourceStore
        from .source_workflows import source_status
        root=self.resolve(workspace)
        sync=root/'sources'/'sync_report.json'
        return dict(sources=source_status(load_credentials()),snapshots=SourceStore(root/'sources').manifests(),sync=json.loads(sync.read_text()) if sync.exists() else None)

    @staticmethod
    def gaps():
        return dict(items=GAPS,docs=[dict(label=label,url=GITHUB_URL+'/blob/main/'+path) for label,path in [('完整缺口清單','docs/PROJECT_STATUS.md'),('資料來源設定','docs/DATA_SOURCES.md'),('金鑰保護','docs/SECRETS.md'),('研究方法','docs/METHODOLOGY.md')]])
