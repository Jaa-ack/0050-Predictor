"""Durable, causal paper forecasts and conservative observed-stream scoring.

No orders or credentials are accepted here. Market messages are an allowlisted
local archive; predictions are immutable. Provider-stream labels do not assert
that every individual exchange fill was disseminated by the provider.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import tempfile
import statistics

import pandas as pd
from scipy.stats import beta

from .universe import TRAINING_TICKERS, DEFAULT_EVALUATION_TICKERS, asset_class


def ns(value):
    stamp=pd.Timestamp(value)
    if stamp.tzinfo is None:raise ValueError('Explicit timezone required')
    return stamp.value


def iso(value):
    return pd.Timestamp(value,tz='UTC').tz_convert('Asia/Taipei').isoformat()


def private_json(path, value):
    path=Path(path);fd,name=tempfile.mkstemp(dir=path.parent,prefix=path.name+'.',suffix='.tmp');temporary=Path(name)
    with os.fdopen(fd,'w') as out:
        json.dump(value,out,ensure_ascii=False,indent=2,allow_nan=False);out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)


class PilotStore:
    """One immutable protocol, persisted observations, forecasts and reports."""
    def __init__(self,root,protocol):
        if 'source_sha256' in protocol:
            digest=hashlib.sha256()
            for path in sorted(Path(__file__).parent.glob('*.py')):
                digest.update(path.name.encode());digest.update(path.read_bytes())
            if digest.hexdigest()!=protocol['source_sha256']:raise ValueError('Pilot source implementation differs')
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if self.root.is_symlink():raise ValueError('Private pilot root cannot be a symlink')
        self.root.chmod(0o700);self.protocol=protocol;self.lock=threading.RLock()
        members=protocol['members'];dates=protocol['dates']
        if (len(members)!=50 or len(set(members))!=50 or not set(TRAINING_TICKERS)<=set(members)
                or protocol['training_tickers']!=list(TRAINING_TICKERS)
                or protocol['horizons']!=[60,300] or not 1<=len(dates)<=10 or dates!=sorted(set(dates))
                or not 0<=protocol['fallback_probability_up']<=1):raise ValueError('Invalid full-coverage pilot protocol')
        self.symbols=list(dict.fromkeys([*members,*DEFAULT_EVALUATION_TICKERS]))
        self.tradable=[t for t in self.symbols if asset_class(t)!='index']
        self.decisions={d:ns(pd.Timestamp(d,tz='Asia/Taipei')+pd.Timedelta(protocol.get('prediction_time','10:00:00'))) for d in dates}
        self.digest=hashlib.sha256(json.dumps(protocol,sort_keys=True).encode()).hexdigest()
        database=self.root/'pilot.sqlite3'
        if database.is_symlink():raise ValueError('Private database cannot be a symlink')
        fd=os.open(database,os.O_WRONLY|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600);os.close(fd)
        self.db=sqlite3.connect(database,check_same_thread=False,timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ticker TEXT,channel TEXT,event_ns INTEGER,received_ns INTEGER,kind TEXT,serial TEXT,payload TEXT,digest TEXT UNIQUE);
          CREATE INDEX IF NOT EXISTS event_lookup ON events(ticker,channel,event_ns);
          CREATE INDEX IF NOT EXISTS receipt_lookup ON events(ticker,channel,received_ns);
          CREATE TABLE IF NOT EXISTS predictions(day TEXT,ticker TEXT,row TEXT,PRIMARY KEY(day,ticker));
          CREATE TABLE IF NOT EXISTS coverage(start_ns INTEGER,end_ns INTEGER,PRIMARY KEY(start_ns));
          CREATE TABLE IF NOT EXISTS gaps(start_ns INTEGER,end_ns INTEGER,reason TEXT);
          CREATE TABLE IF NOT EXISTS releases(day TEXT PRIMARY KEY,at_ns INTEGER);
        ''')
        previous=self.db.execute("SELECT value FROM metadata WHERE key='protocol'").fetchone()
        if previous and previous[0]!=self.digest:
            self.db.close();raise ValueError('Existing pilot protocol differs')
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES('protocol',?)",(self.digest,));self.db.commit()
        private_json(self.root/'protocol.json',protocol)
        (self.root/'pilot.sqlite3').chmod(0o600);self.pending=0

    def ingest(self,message,*,received_at):
        """Persist only public numeric market fields, never authentication data."""
        if message.get('event') not in ('snapshot','data') or message.get('channel') not in ('trades','books','indices'):return False
        data=message.get('data',{});ticker=str(data.get('symbol',''))
        if ticker not in self.symbols:return False
        timestamp=data.get('time')
        if type(timestamp) is not int or timestamp<=0:raise ValueError('Invalid market timestamp')
        allowed={'symbol','time','serial','price','size','volume','bid','ask','index','bids','asks','isContinuous','isTrial','isOpen','isClose','isDelayedOpen','isDelayedClose','isLimitDownHalt','isLimitUpHalt','type'}
        payload={k:v for k,v in data.items() if k in allowed}
        for side in ('bids','asks'):
            if side in payload:
                payload[side]=[{k:x[k] for k in ('price','size') if k in x} for x in payload[side][:5]]
        # Arbitrary nested values cannot smuggle private strings into the archive.
        for key,value in payload.items():
            if key in ('bids','asks'):
                values=[v for level in value for v in level.values()]
            elif key in ('symbol','type'):
                if not isinstance(value,str) or len(value)>40:raise ValueError('Invalid market instrument')
                continue
            else:values=[value]
            if any(not isinstance(v,(int,float,bool)) or not math.isfinite(v) for v in values):raise ValueError('Market fields must be finite numbers')
        body=json.dumps(payload,sort_keys=True,allow_nan=False)
        digest=hashlib.sha256((message['channel']+message['event']+body).encode()).hexdigest()
        with self.lock:
            result=self.db.execute('INSERT OR IGNORE INTO events(ticker,channel,event_ns,received_ns,kind,serial,payload,digest) VALUES(?,?,?,?,?,?,?,?)',
                (ticker,message['channel'],timestamp*1000,ns(received_at),message['event'],str(data.get('serial','')),body,digest))
            self.pending+=1
            if self.pending>=200:self.flush()
            return result.rowcount==1

    def flush(self):
        with self.lock:self.db.commit();self.pending=0

    def mark_coverage(self,start,end):
        with self.lock:
            self.db.execute('INSERT INTO coverage VALUES(?,?) ON CONFLICT(start_ns) DO UPDATE SET end_ns=max(end_ns,excluded.end_ns)',(ns(start),ns(end)));self.flush()

    def mark_gap(self,start,end,reason):
        if reason not in ('disconnected','stalled','clock_jump','invalid_message'):raise ValueError('Use a fixed gap code')
        with self.lock:
            self.db.execute('INSERT INTO gaps VALUES(?,?,?)',(ns(start),ns(end),reason));self.flush()

    def _observed(self,ticker,channel,cutoff):
        row=self.db.execute('SELECT event_ns,received_ns,payload FROM events WHERE ticker=? AND channel=? AND received_ns<=? AND event_ns<=? ORDER BY event_ns DESC,id DESC LIMIT 1',
            (ticker,channel,cutoff,cutoff)).fetchone()
        return row

    def emit(self,emitted_at):
        """Emit every declared target once; missing inputs use the frozen prior."""
        day=pd.Timestamp(emitted_at).tz_convert('Asia/Taipei').date().isoformat()
        if day not in self.decisions:raise ValueError('Undeclared pilot date')
        decision=self.decisions[day];emitted=ns(emitted_at)
        if emitted<decision:raise ValueError('Forecast cannot precede its fixed clock')
        with self.lock:
            old=self.db.execute('SELECT row FROM predictions WHERE day=? ORDER BY ticker',(day,)).fetchall()
            if old:return [json.loads(r[0]) for r in old]
            rows=[]
            for ticker in sorted(self.symbols):
                trade=self._observed(ticker,'trades',decision);book=self._observed(ticker,'books',decision)
                ready=False;last_received=None;location=None
                if trade and book and asset_class(ticker)!='index':
                    td=json.loads(trade[2]);bd=json.loads(book[2]);last_received=max(trade[1],book[1])
                    bids=bd.get('bids',[]);asks=bd.get('asks',[])
                    if bids and asks and not td.get('isTrial') and not bd.get('isTrial') and td.get('isContinuous') and bd.get('isContinuous'):
                        last=td.get('price',0);bid=bids[0].get('price',0);ask=asks[0].get('price',0)
                        ready=bool(0<bid<ask and last>0 and min(trade[0],book[0])>=decision-self.protocol['max_input_age_seconds']*10**9)
                        if ready:location=round(2*(last-(bid+ask)/2)/(ask-bid),12)
                score=float(location<0) if ready else self.protocol['fallback_probability_up']
                row=dict(day=day,ticker=ticker,model_version=self.protocol['model_version'],probability_up=score,direction=1 if score>=.5 else -1,
                    decision_at=iso(decision),emitted_at=iso(emitted),input_ready=ready,fallback=not ready,
                    max_input_received_at=iso(last_received) if last_received is not None else None,
                    observed_location=location,on_time=emitted-decision<=self.protocol['max_emission_lateness_seconds']*10**9)
                self.db.execute('INSERT INTO predictions VALUES(?,?,?)',(day,ticker,json.dumps(row)));rows.append(row)
            self.flush();private_json(self.root/f'predictions_{day}.json',rows)
            return rows

    def _label(self,ticker,start,horizon,as_of):
        deadline=start+horizon*10**9
        if deadline>as_of:return 'pending',None
        covered=self.db.execute('SELECT 1 FROM coverage WHERE start_ns<=? AND end_ns>=?',(start,deadline)).fetchone()
        gap=self.db.execute('SELECT 1 FROM gaps WHERE start_ns<=? AND end_ns>=?',(deadline,start)).fetchone()
        if not covered or gap:return 'unknown',None
        anchor=self.db.execute("SELECT payload FROM events WHERE ticker=? AND channel='trades' AND event_ns<=? ORDER BY event_ns DESC,id DESC LIMIT 1",(ticker,start)).fetchone()
        if not anchor:return 'unknown',None
        anchor=json.loads(anchor[0]);price=anchor.get('price')
        if not price or anchor.get('isTrial') or not anchor.get('isContinuous'):return 'unknown',None
        for event_ns,payload in self.db.execute("SELECT event_ns,payload FROM events WHERE ticker=? AND channel='trades' AND kind='data' AND event_ns>? AND event_ns<=? ORDER BY event_ns,CAST(serial AS INTEGER),id",(ticker,start,deadline)):
            row=json.loads(payload)
            if row.get('isTrial') or not row.get('isContinuous'):continue
            if row.get('price',price)!=price:return 'event',1 if row['price']>price else -1
        return 'timeout',None

    def seal(self,day,released_at,*,allow_existing=False):
        """Seal already-durable predictions; scoring starts after publication."""
        with self.lock:
            value=ns(released_at)
            previous=self.db.execute('SELECT at_ns FROM releases WHERE day=?',(day,)).fetchone()
            if previous:
                if not allow_existing and value!=previous[0]:raise ValueError('Release seal is immutable')
                return
            if day not in self.decisions or value<self.decisions[day]:raise ValueError('Invalid release time')
            if self.db.execute('SELECT count(*) FROM predictions WHERE day=?',(day,)).fetchone()[0]!=len(self.symbols):
                raise ValueError('Seal requires the entire durable forecast grid')
            self.db.execute('INSERT INTO releases VALUES(?,?)',(day,value));self.flush()
            private_json(self.root/f'release_{day}.json',dict(day=day,released_at=iso(value),protocol_sha256=self.digest))

    def evaluate(self,*,as_of):
        """Score matured full daily grids, keeping absent predictions wrong."""
        with self.lock:
            self.flush();result=dict(protocol_sha256=self.digest,as_of=iso(ns(as_of)),evidence_kind='prospective_observed_provider_stream',
                label_scope=self.protocol['label_scope'],exchange_fill_completeness_verified=False,
                empirical_target_met=False,statistical_target_established=False,horizons={})
            delays=[(received-event)/1e9 for received,event in self.db.execute('SELECT received_ns,event_ns FROM events ORDER BY id DESC LIMIT 10000')]
            result['data_quality']=dict(received_events=self.db.execute('SELECT count(*) FROM events').fetchone()[0],
                gap_count=self.db.execute('SELECT count(*) FROM gaps').fetchone()[0],latency_sample='last_10000_market_messages',
                median_event_to_receipt_seconds=statistics.median(delays) if delays else None,
                negative_timestamp_delays=sum(d<0 for d in delays))
            for horizon in self.protocol['horizons']:
                days=[d for d,t in self.decisions.items() if t+(horizon+self.protocol['max_emission_lateness_seconds'])*10**9<=ns(as_of)]
                stocks={t:dict(correct=0,unknown=0,timeout=0,missing=0,late=0,fallback=0,always_up_correct=0,always_down_correct=0) for t in self.protocol['members']}
                per_day={d:dict(expected=50,correct=0,unknown=0,missing=0) for d in days}
                for day in days:
                    predictions={r['ticker']:r for (raw,) in self.db.execute('SELECT row FROM predictions WHERE day=?',(day,)) for r in [json.loads(raw)]}
                    release=self.db.execute('SELECT at_ns FROM releases WHERE day=?',(day,)).fetchone()
                    for ticker,counts in stocks.items():
                        pred=predictions.get(ticker)
                        if not pred:
                            counts['missing']+=1;counts['unknown']+=1;per_day[day]['missing']+=1;per_day[day]['unknown']+=1;continue
                        start=max(ns(pred['emitted_at']),release[0]) if release else ns(pred['emitted_at'])
                        on_time=pred['on_time'] and start<=self.decisions[day]+self.protocol['max_emission_lateness_seconds']*10**9
                        counts['fallback']+=int(pred['fallback']);counts['late']+=int(not on_time)
                        status,direction=self._label(ticker,start,horizon,ns(as_of))
                        if self.protocol.get('require_release_seal') and not release:status,direction='unknown',None
                        counts['unknown']+=int(status in ('unknown','pending'));counts['timeout']+=int(status=='timeout')
                        success=int(status=='event' and direction==pred['direction'] and on_time)
                        counts['correct']+=success;per_day[day]['correct']+=success;per_day[day]['unknown']+=int(status in ('unknown','pending'))
                        counts['always_up_correct']+=int(status=='event' and direction==1)
                        counts['always_down_correct']+=int(status=='event' and direction==-1)
                n=len(days);lower=[]
                for counts in stocks.values():
                    k=counts['correct'];counts['accuracy']=k/n if n else None
                    counts['joint_lower']=float(beta.ppf(.05/(50*len(self.protocol['horizons'])),k,n-k+1)) if k else 0.
                    lower.append(counts['joint_lower'])
                ranking=sorted(stocks,key=lambda t:(stocks[t]['correct'],t))
                result['horizons'][str(horizon)]=dict(test_dates=n,expected_predictions=50*n,
                    missing_predictions=sum(s['missing'] for s in stocks.values()),late_predictions=sum(s['late'] for s in stocks.values()),
                    unknown_answers=sum(s['unknown'] for s in stocks.values()),timeout_predictions=sum(s['timeout'] for s in stocks.values()),
                    fallback_predictions=sum(s['fallback'] for s in stocks.values()),
                    worst_five_tickers=ranking[:5],worst_five_accuracy=sum(stocks[t]['correct'] for t in ranking[:5])/(5*n) if n else None,
                    worst_five_joint_lower=sum(sorted(lower)[:5])/5,per_stock=stocks,per_day=per_day)
                transfers={}
                for ticker in DEFAULT_EVALUATION_TICKERS:
                    tradable=asset_class(ticker)!='index';correct=0;unknown=0
                    for day in days:
                        found=self.db.execute('SELECT row FROM predictions WHERE day=? AND ticker=?',(day,ticker)).fetchone()
                        release=self.db.execute('SELECT at_ns FROM releases WHERE day=?',(day,)).fetchone()
                        if not tradable or not found:unknown+=1;continue
                        pred=json.loads(found[0]);start=max(ns(pred['emitted_at']),release[0]) if release else ns(pred['emitted_at'])
                        status,direction=self._label(ticker,start,horizon,ns(as_of))
                        if self.protocol.get('require_release_seal') and not release:status='unknown'
                        unknown+=int(status in ('unknown','pending'))
                        correct+=int(status=='event' and direction==pred['direction'] and pred['on_time']
                            and start<=self.decisions[day]+self.protocol['max_emission_lateness_seconds']*10**9)
                    transfers[ticker]=dict(accuracy=correct/n if n and tradable else None,unknown=unknown,
                        predictions=n,tradable=tradable,used_for_training=False)
                result['horizons'][str(horizon)]['transfers']=transfers
            return result

    def write_report(self,*,as_of,final=False):
        if final and ns(as_of)<max(self.decisions.values())+301*10**9:raise ValueError('Pilot is not finished')
        report=self.evaluate(as_of=as_of);report['final']=final
        private_json(self.root/'evaluation.json',report)
        lines=['# 10交易日真實行情試驗評估','',f"狀態：{'期末報告' if final else '進度報告'}；來源：{self.protocol['source']}。",'',
            '評分範圍為供應商實際揭示的成交訊息；未證明每個交易所成交片段均完整傳送。10日期間不足以確立長期75%。', '',
            '|期限|成熟交易日|預期回答|缺報|未知|逾時|最弱五檔準確率|共同下界|','|---|---:|---:|---:|---:|---:|---:|---:|']
        for horizon,r in report['horizons'].items():
            accuracy='尚無成熟答案' if r['worst_five_accuracy'] is None else f"{r['worst_five_accuracy']:.2%}"
            lines.append(f"|{horizon}秒|{r['test_dates']}|{r['expected_predictions']}|{r['missing_predictions']}|{r['unknown_answers']}|{r['timeout_predictions']}|{accuracy}|{r['worst_five_joint_lower']:.2%}|")
        lines+=['','逐股明細、缺報、晚發報及回退次數見 evaluation.json。全部50檔完整分母保留，未刪除失敗日期。',
            '60秒與300秒分開驗收；指數不建立成交價答案。統計界限未涵蓋市場分布變化或資料遺失的不確定性。',
            '本輪為凍結共享價差反彈基準的真實轉移試驗，不在測試期重新訓練或挑選股票。',
            '逐股上漲／下跌常數基準及逐日正確數見JSON。行情延遲摘要取最後10000則訊息，並非全期延遲分布。']
        quality=report['data_quality']
        lines+=['','## 資料與營運品質','',f"已保存{quality['received_events']}則行情，記錄{quality['gap_count']}個資料／執行缺口。最近至多10000則訊息的事件至收件中位秒數為{quality['median_event_to_receipt_seconds']}，負延遲樣本{quality['negative_timestamp_delays']}筆。",
            '負延遲需要核對兩端時鐘；秒數同時包含來源時間定義及傳輸差異，不直接當成交易所延遲。',
            '供應商訊息完整性尚未等同交易所逐成交片段完整性，嚴格市場75%驗收維持未成立。']
        for horizon,r in report['horizons'].items():
            lines+=['',f'## {horizon}秒逐日與逐股明細','',
                '|日期|應答|正確|缺報|未知|','|---|---:|---:|---:|---:|']
            for day,c in r['per_day'].items():lines.append(f"|{day}|{c['expected']}|{c['correct']}|{c['missing']}|{c['unknown']}|")
            lines+=['','|股票|正確|準確率|共同下界|未知|逾時|缺報|晚報|回退|常數漲正確|常數跌正確|',
                '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
            for ticker,c in sorted(r['per_stock'].items()):
                accuracy='待成熟' if c['accuracy'] is None else f"{c['accuracy']:.2%}"
                lines.append(f"|{ticker}|{c['correct']}|{accuracy}|{c['joint_lower']:.2%}|{c['unknown']}|{c['timeout']}|{c['missing']}|{c['late']}|{c['fallback']}|{c['always_up_correct']}|{c['always_down_correct']}|")
            lines+=['','轉移查詢（不參與擬合或選模）：']
            for ticker,c in r['transfers'].items():
                accuracy='無成交目標／尚無成熟答案' if c['accuracy'] is None else f"{c['accuracy']:.2%}"
                lines.append(f"- {ticker}：{accuracy}；未知{c['unknown']}；可交易標的={c['tradable']}。")
        lines+=['','## 評估限制與判讀','',
            '10個交易日每檔只有10次回答。即使10次全對，在本次50檔×2期限的100比較家族下，單檔共同單側95%下界也只有約46.76%；樣本75%不可當成長期75%證明。',
            '所有模型／規則及股票池在試驗前凍結。未用這10日結果重新選參數；本輪沒有真實成交下單或獲利驗證。',
            '若資料缺口、時鐘、成交片段完整性或主機停機影響結果，先修正資料與營運，再以新的未見日期重新驗證；原始失敗紀錄不刪除。']
        path=self.root/'evaluation.md';path.write_text('\n'.join(lines)+'\n');path.chmod(0o600)
        return report

    def close(self):
        with self.lock:self.flush();self.db.close()
