"""Command line entry point: python -m twquant --help."""
import argparse
from dataclasses import fields, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .workflows import forecast, ledger_for, promote, release_holdout, research, settle, write_json


from .universe import TRAINING_TICKERS

TICKERS = [*TRAINING_TICKERS, "0050"]


def load_dotenv(path=Path(".env")):
    """Compatibility name; validate/read server-only secrets without exporting to process-wide state."""
    from .credentials import load_credentials
    return load_credentials(path)


def now_taipei():
    return datetime.now(ZoneInfo("Asia/Taipei")).isoformat()


def config_from(path):
    from .research import ResearchConfig
    if not path:
        return ResearchConfig()
    values = json.loads(Path(path).read_text())
    unknown = set(values) - {f.name for f in fields(ResearchConfig)}
    if unknown:
        raise ValueError("Unknown research configuration: " + ", ".join(sorted(unknown)))
    if "horizons" in values:
        values["horizons"] = tuple(values["horizons"])
    if "evaluation_tickers" in values:
        values["evaluation_tickers"] = tuple(values["evaluation_tickers"])
    return ResearchConfig(**values)


def import_data(root, args):
    from .data import PriceStore
    from .ingestion import ingest_csv, ingest_finmind
    store = PriceStore(root / "data")
    if args.csv:
        path = Path(args.csv)
        prices = ingest_csv(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        archived = root / "data" / "raw" / (digest + ".csv")
        archived.parent.mkdir(parents=True, exist_ok=True)
        if not archived.exists():
            with archived.open("xb") as out:
                out.write(path.read_bytes())
        metadata = dict(provider="csv", raw_sha256=digest)
    else:
        if not args.start:
            raise ValueError("--start YYYY-MM-DD is required for FinMind ingestion")
        end = args.end or now_taipei()[:10]
        tickers = args.tickers.split(",") if args.tickers else TICKERS
        if "0050" not in tickers:
            tickers.append("0050")
        credentials=load_dotenv()
        if args.provider=='finmind':
            token=credentials.get('FINMIND_TOKEN').get_secret_value() if credentials.status()['FINMIND_TOKEN'] else None
            prices = ingest_finmind(tickers, args.start, end,token=token, raw_dir=root / "data" / "raw")
        else:
            from .sources import SourceTransport,fetch_fugle_prices,fetch_fubon_prices
            t=SourceTransport(credentials,raw_dir=root/'data'/'raw')
            prices=(fetch_fugle_prices if args.provider=='fugle' else fetch_fubon_prices)(t,tickers,args.start,end)
        metadata = dict(provider=args.provider, start=args.start, end=end)
    sid = store.write_prices(prices, metadata=metadata)
    return dict(snapshot_id=sid, rows=len(prices), date_min=str(prices.date.min().date()),
                date_max=str(prices.date.max().date()))


def parser():
    p = argparse.ArgumentParser(description="0050 多因子研究、歷史驗證與逐日預測追蹤")
    p.add_argument("--root", type=Path, help="隔離資料與模型工作目錄（預設 artifacts/real；demo 使用 artifacts/demo）")
    subs = p.add_subparsers(dest="command", required=True)
    d = subs.add_parser("demo", help="合成資料端到端驗證；不得用來證明投資可行性")
    d.add_argument("--sessions", type=int, default=800)
    d.add_argument("--seed", type=int, default=42)
    d.add_argument("--config", type=Path)
    d.add_argument("--holdout-dates", type=int, default=126)
    for name in ("ingest", "daily"):
        cmd = subs.add_parser(name, help="匯入歷史行情" if name == "ingest" else "匯入、結算、重訓校準並產生今日預測")
        cmd.add_argument("--csv", type=Path)
        cmd.add_argument("--start", help="FinMind 下載起日；完整快照必須包含足夠訓練歷史")
        cmd.add_argument("--end")
        cmd.add_argument("--tickers", help="逗號分隔代號；自動納入0050")
        cmd.add_argument('--provider',choices=['finmind','fugle','fubon'],default='finmind')
        if name == "daily":
            cmd.add_argument("--skip-ingest", action="store_true", help="使用現有完整快照")
            cmd.add_argument("--as-of", default=None, help="含時區的決策時間，預設現在 Asia/Taipei")
            cmd.add_argument("--paper", action="store_true", help="可行性未通過時只做影子預測")
            cmd.add_argument("--config", type=Path)
            cmd.add_argument("--mode", choices=["postclose", "preopen"], default="postclose")
            cmd.add_argument("--events", type=Path)
            cmd.add_argument("--fundamentals", type=Path)
            cmd.add_argument("--holdout-dates", type=int, default=126)
    r = subs.add_parser("research", help="逐期樣本外回測、基準比較、模型訓練與校準")
    r.add_argument("--config", type=Path)
    r.add_argument("--as-of")
    r.add_argument("--events", type=Path)
    r.add_argument("--mode", choices=["postclose", "preopen"], default="postclose")
    r.add_argument("--holdout-dates", type=int, default=126)
    compare = subs.add_parser("compare", help="僅以開發期固定十檔指標選擇特徵組；不開封保留期")
    compare.add_argument("--config", type=Path)
    compare.add_argument("--events", type=Path)
    compare.add_argument("--holdout-dates", type=int, default=126)
    screen = subs.add_parser('screen', help='離線篩選既有特徵JSON/CSV，保留通過、失敗與缺失原因；不訓練或下單')
    screen.add_argument('metrics', type=Path)
    screen.add_argument('--rules', type=Path, required=True)
    screen.add_argument('--out', type=Path, help='完整結果JSON或CSV；不只匯出通過者')
    study = subs.add_parser('skill-study', help='既有快照的A/B/C/D與純日線規則比較；不開封保留期、不更新正式模型')
    study.add_argument('--config', type=Path)
    study.add_argument('--rules', type=Path)
    study.add_argument('--risk-limits', type=Path)
    study.add_argument('--events', type=Path)
    study.add_argument('--snapshot', help='既有PriceStore快照ID；預設最新，不取得新資料')
    study.add_argument('--mode', choices=['postclose', 'preopen'], default='postclose')
    study.add_argument('--holdout-dates', type=int, default=126)
    study.add_argument('--out', type=Path, help='必須是尚未存在的獨立實驗目錄')
    direction = subs.add_parser('direction-search', help='巢狀時間比較方向模型，檢查固定十檔全部252種五檔組合；不開封保留期')
    direction.add_argument('--config', type=Path)
    direction.add_argument('--reference-run', type=Path,
                           help='可信本機研究目錄；驗證完整輸入與配方後重用已固定模型及校準分數')
    direction.add_argument('--pin-reference', action='store_true',
                           help='只重播核對並固定reference-run的選中模型；不搜尋、不覆寫原結果')
    direction.add_argument('--events', type=Path)
    direction.add_argument('--mode', choices=['postclose', 'preopen'], default='postclose',
                           help='盤後20:00或下一實際市場日盤前08:30；兩者維持同一收盤價錨點')
    direction.add_argument('--overseas-window', action='store_true',
                           help='另加錨點20:00至決策截止的海外累積變動與新收件指標；需全球FMP收盤事件')
    direction.add_argument('--taifex-history', type=Path,
                           help='官方歷史CSV或單一CSV的ZIP；夜盤收件假設06:00、有效至08:30，不是原始PIT證據')
    direction.add_argument('--taifex-exposure', action='store_true',
                           help='需歷史檔；加入個股過去60日對日盤期貨的反應及夜盤交互作用，日盤假設20:00可得')
    direction.add_argument('--sector-history', type=Path,
                           help='sector／flow／margin及開盤研究必需；七條含source的海外產業CSV，假設美東17:00收件')
    direction.add_argument('--flow-history', type=Path,
                           help='flow／margin及開盤研究必需；含source的個股法人分類日CSV，不改寫原收件')
    direction.add_argument('--flow-lag-sessions', type=int, choices=[0, 2], default=2,
                           help='法人資料20:00假設收件延遲，預設兩個主要市場日；零延遲僅供獨立敏感度研究')
    direction.add_argument('--margin-history', type=Path,
                           help='margin候選必需；含source的融資融券日CSV，21:00假設收件，不代表原始PIT')
    direction.add_argument('--margin-lag-sessions', type=int, choices=[0,2], default=2,
                           help='融資融券21:00收件假設的主要市場日延遲，預設2；零延遲僅供獨立敏感度')
    direction.add_argument('--opening-noise-bps', type=float, default=None,
                           help='僅opening-proxy／opening-residual：獨立對數雜訊，預設0基點；一律合成可得性假設')
    direction.add_argument('--opening-missing-fraction', type=float, default=None,
                           help='僅opening-proxy／opening-residual：獨立缺報率，預設0；缺報仍給出預測')
    direction.add_argument('--opening-seed', type=int, default=None,
                           help='僅opening-proxy／opening-residual：依股票及日期固定的種子，預設17')
    direction.add_argument('--holdout-dates', type=int, default=126)
    direction.add_argument('--membership', type=Path, help='完整當期0050持股JSON快照；外部成分只作同模型測試')
    direction.add_argument('--candidate-set', choices=['base', 'factor', 'market', 'fusion', 'exposure', 'path', 'sector', 'flow', 'margin', 'response', 'similarity', 'opening-proxy', 'opening-residual'], default='base',
                           help='base八組；factor十組；market十六組；fusion五組價格／美股／夜盤融合；exposure二十四組；path三十組；sector三十六組；flow四十二組；margin四十八組；response五十四組；similarity七組共享近鄰比較；opening-proxy五組；opening-residual九組合成參考假設')
    direction.add_argument('--selection-policy', choices=['worst_stock_accuracy', 'pooled_brier', 'worst_stock_brier'],
                           default='worst_stock_accuracy', help='Brier規則固定0.5門檻；只以校準期選模')
    simulation = subs.add_parser('direction-simulate', help='模擬假設資訊量、延遲及缺漏；合成結果不是真實預測證據')
    simulation.add_argument('--sessions', type=int, default=1200)
    simulation.add_argument('--seeds', default='17,29,43', help='保留所有指定種子的結果，不挑選幸運種子')
    feasibility = subs.add_parser('direction-feasibility', help='離線估算完整50檔的條件式資訊需求；假設通過不代表實際百分之75')
    feasibility.add_argument('--config', type=Path, required=True, help='明確的訊號強度、可得率、缺答案與全部模擬種子')
    feasibility.add_argument('--out', type=Path, help='必須尚未存在的獨立設計評估目錄')
    queue = subs.add_parser('direction-queue-study', help='離線共享委託簿過程比較；全部資料皆為未校準合成情境')
    queue.add_argument('--config', type=Path, default=Path('configs/intraday_queue.json'))
    queue.add_argument('--membership', type=Path, required=True)
    queue.add_argument('--out', type=Path, required=True)
    ceiling=subs.add_parser('direction-queue-ceiling',help='離線估計完整隱藏模擬狀態的方向上限；不產生可部署預測')
    ceiling.add_argument('--config',type=Path,required=True)
    ceiling.add_argument('--membership',type=Path,required=True)
    ceiling.add_argument('--out',type=Path,required=True)
    first=subs.add_parser('direction-first-change-study',help='獨立探索有期限的首次價格變動方向；不替代固定端點目標')
    first.add_argument('--config',type=Path,required=True)
    first.add_argument('--membership',type=Path,required=True)
    first.add_argument('--out',type=Path,required=True)
    queue_first=subs.add_parser('direction-queue-first-study',help='離散隊列、Poisson事件時鐘與發報後首次變價；離線合成研究')
    queue_first.add_argument('--config',type=Path,required=True)
    queue_first.add_argument('--membership',type=Path,required=True)
    queue_first.add_argument('--out',type=Path,required=True)
    trade_first=subs.add_parser('direction-trade-first-study',help='成交價首次變動及價差反彈基準；離線合成研究')
    trade_first.add_argument('--config',type=Path,required=True)
    trade_first.add_argument('--membership',type=Path,required=True)
    trade_first.add_argument('--out',type=Path,required=True)
    matching=subs.add_parser('direction-matching-study',help='逐價部分成交、動態價差與首次成交價變動；離線合成研究')
    matching.add_argument('--config',type=Path,required=True)
    matching.add_argument('--membership',type=Path,required=True)
    matching.add_argument('--out',type=Path,required=True)
    f = subs.add_parser("predict", help="保存不可覆寫的1/5/20日預測")
    f.add_argument("--as-of", required=True)
    f.add_argument("--paper", action="store_true")
    f.add_argument("--events", type=Path)
    f.add_argument("--fundamentals", type=Path)
    f.add_argument("--tickers", help="逗號分隔；預設固定十檔，其他標的限 --paper")
    s = subs.add_parser("settle", help="只評分已到期且實際可得的結果")
    s.add_argument("--as-of", required=True)
    subs.add_parser("status", help="模型狀態、可行性缺口與逐筆預測追蹤")
    subs.add_parser("promote", help="檢查門檻後選定正式預測模型；不涉及下單")
    subs.add_parser("holdout", help="開封已凍結的最終保留測試；結果僅計算一次供審核")
    credentials=subs.add_parser('secrets',help='建立私人金鑰欄位或執行開源前洩密檢查')
    credential_sub=credentials.add_subparsers(dest='secret_command',required=True)
    credential_sub.add_parser('init')
    cert=credential_sub.add_parser('import-cert',help='安全複製本機.p12/.pfx至私人目錄；不登入、不覆寫')
    cert.add_argument('--from',dest='certificate_source',type=Path,required=True)
    audit=credential_sub.add_parser('audit')
    audit.add_argument('--repo',type=Path,default=Path('.'))
    sources=subs.add_parser('sources',help='多來源狀態、擷取、模型特徵匯出與交叉核對')
    source_sub=sources.add_subparsers(dest='source_command',required=True)
    source_sub.add_parser('status')
    verify=source_sub.add_parser('verify',help='小量唯讀測試Key、端點權限、格式與快照；不覆寫主行情')
    verify.add_argument('--full',action='store_true',help='逐系列與歷史權限擴充檢查')
    verify.add_argument('--providers',help='以逗號分隔指定來源；預設全部')
    sync=source_sub.add_parser('sync')
    sync.add_argument('--plan',type=Path,default=Path('configs/source_plan.json'))
    export=source_sub.add_parser('export-events')
    export.add_argument('--out',type=Path)
    documents=source_sub.add_parser('export-documents',help='去重匯出新聞／公告與內容修正版本')
    documents.add_argument('--out',type=Path)
    calendar_export=source_sub.add_parser('export-calendar',help='匯出待核實年度日曆；不自動啟用')
    calendar_export.add_argument('--year',type=int,default=int(now_taipei()[:4]))
    calendar_export.add_argument('--out',type=Path)
    compare=source_sub.add_parser('reconcile')
    compare.add_argument('--left',choices=['finmind','fugle','fubon'],required=True)
    compare.add_argument('--right',choices=['finmind','fugle','fubon'],required=True)
    c = subs.add_parser("calendar", help="匯入已核實的交易所開休市日曆供盤前預測使用")
    c.add_argument("--csv", type=Path, required=True)
    web = subs.add_parser("serve", help="啟動本機研究工作台與固定任務操作")
    web.add_argument("--port", type=int, default=8000)
    export_web=subs.add_parser('export-web',help='匯出僅含合成研究資料的安全靜態網頁')
    export_web.add_argument('--out',type=Path,required=True)
    return p


def execute(args):
    from .data import PriceStore
    default_root = {'demo': 'artifacts/demo', 'direction-simulate': 'artifacts/direction-synthetic',
                    'direction-feasibility': 'artifacts/direction-feasibility'}
    root = args.root or Path(default_root.get(args.command, 'artifacts/real'))
    if args.command=='direction-matching-study':
        from .queue_first_study import run_matching_study
        report=run_matching_study(json.loads(args.config.read_text()),args.membership,args.out)
        return dict(report=str(args.out/'report.json'),evidence_kind=report['evidence_kind'],experiments=len(report['experiments']),
                    models_trained=report['models_trained'],target_scope_confirmed=True,target_feasibility_established=False)
    if args.command=='direction-trade-first-study':
        from .queue_first_study import run_trade_first_study
        report=run_trade_first_study(json.loads(args.config.read_text()),args.membership,args.out)
        return dict(report=str(args.out/'report.json'),evidence_kind=report['evidence_kind'],experiments=len(report['experiments']),
                    models_trained=report['models_trained'],target_scope_confirmed=True,target_feasibility_established=False)
    if args.command=='direction-queue-first-study':
        from .queue_first_study import run_queue_first_study
        report=run_queue_first_study(json.loads(args.config.read_text()),args.membership,args.out)
        return dict(report=str(args.out/'report.json'),evidence_kind=report['evidence_kind'],experiments=len(report['experiments']),
                    models_trained=report['models_trained'],target_scope_confirmed=True,target_feasibility_established=False)
    if args.command=='direction-first-change-study':
        from .first_change_study import run_first_change_study
        report=run_first_change_study(json.loads(args.config.read_text()),args.membership,args.out)
        return dict(report=str(args.out/'report.json'),evidence_kind=report['evidence_kind'],
                    experiments=len(report['experiments']),models_trained=report['models_trained'],target_scope_confirmed=True,target_feasibility_established=False)
    if args.command=='direction-queue-ceiling':
        from .queue_ceiling import run_queue_ceiling
        report=run_queue_ceiling(json.loads(args.config.read_text()),args.membership,args.out)
        return dict(report=str(args.out/'report.json'),evidence_kind=report['evidence_kind'],
                    experiments=len(report['experiments']),models_trained=0,forecast_produced=False,
                    target_feasibility_established=False)
    if args.command == 'direction-queue-study':
        from .intraday_queue_study import run_queue_study
        report=run_queue_study(json.loads(args.config.read_text()),args.membership,args.out)
        return dict(report=str(args.out/'report.json'),evidence_kind=report['evidence_kind'],
                    experiments=len(report['experiments']),models_trained=report['models_trained'],
                    real_world_target_verified=False,unconditional_75_expectation_supported=False)
    if args.command == 'direction-feasibility':
        from .direction_feasibility import assess_feasibility
        from .workflows import source_digest
        from uuid import uuid4
        output = args.out or root / uuid4().hex
        if output.exists():
            raise ValueError('Feasibility requires a fresh output directory')
        body = args.config.read_bytes()
        assumptions = json.loads(body)
        report = assess_feasibility(assumptions)
        protocol = dict(assumptions=assumptions, assumptions_sha256=hashlib.sha256(body).hexdigest(),
                        source_sha256=source_digest(), evidence_kind='synthetic_assumptions',
                        real_world_target_verified=False)
        write_json(output / 'protocol.json', protocol)
        write_json(output / 'report.json', report)
        return dict(report=str(output / 'report.json'), evidence_kind=report['evidence_kind'],
                    real_world_target_verified=False, unconditional_75_expectation_supported=False,
                    scenarios=[dict(name=r['name'], expected=r['expected']['worst_five_accuracy'],
                                    conditional_target_met=r['expected_target_met_under_assumptions'])
                               for r in report['scenarios']])
    if args.command == 'screen':
        from .skill_screener import load_metrics, screen
        results = screen(load_metrics(args.metrics), json.loads(args.rules.read_text(encoding='utf-8')))
        if args.out:
            if args.out.suffix.lower() == '.json':
                write_json(args.out, results)
            elif args.out.suffix.lower() == '.csv':
                import pandas as pd
                args.out.parent.mkdir(parents=True, exist_ok=True)
                rows = [dict(r, checks=json.dumps(r['checks'], ensure_ascii=False),
                             rank_parts=json.dumps(r['rank_parts']), rank_gaps=json.dumps(r['rank_gaps'])) for r in results]
                pd.DataFrame(rows, columns=['ticker', 'passed', 'checks', 'score', 'rank_parts', 'rank_gaps']).to_csv(args.out, index=False)
            else:
                raise ValueError('Screen output must use .json or .csv')
        return dict(total=len(results), passed=sum(r['passed'] for r in results), results=results)
    if args.command == 'skill-study':
        from .skill_integration import compare_skill_modules
        from .skill_risk import RiskLimits
        from .workflows import load_events
        from uuid import uuid4
        rules = json.loads(args.rules.read_text(encoding='utf-8')) if args.rules else None
        risk = None
        if args.risk_limits:
            values = json.loads(args.risk_limits.read_text(encoding='utf-8'))
            if not isinstance(values, dict) or set(values) - {f.name for f in fields(RiskLimits)}:
                raise ValueError('Unknown risk limit configuration')
            risk = RiskLimits(**values)
        output = args.out or root / 'skill-studies' / uuid4().hex
        if output.exists():
            raise ValueError('Skill study requires a fresh output directory')
        report = compare_skill_modules(PriceStore(root / 'data').read_prices(args.snapshot), config_from(args.config),
            external=load_events(args.events), rule_spec=rules, risk_limits=risk,
            mode=args.mode, holdout_dates=args.holdout_dates, output=output)
        return dict(report=str(output / 'report.json'), data_kind=report['data_kind'],
                    holdout_opened=False, variants=list(report['variants']),
                    paired_brier_B_minus_A=report['paired_brier_B_minus_A'])
    if args.command=='export-web':
        from .web_export import export_dashboard
        return export_dashboard(root,args.out)
    if args.command=='secrets':
        if args.secret_command=='import-cert':
            from .credentials import import_certificate
            import_certificate(args.certificate_source)
            return dict(imported=True,directory='.secrets/fubon',permissions='directory0700/file0600',
                env_field='FUBON_CERT_PATH',login_attempted=False,credential_values_exposed=False)
        if args.secret_command=='init':
            from .credentials import initialize_credentials
            return dict(created=initialize_credentials(),file='.env',required_permissions='0600',secret_values_exposed=False)
        from .secret_audit import audit_repository
        return audit_repository(args.repo,load_dotenv())
    if args.command=='sources':
        from .source_workflows import source_status,sync_sources,reconcile_prices
        from .source_store import SourceStore
        if args.source_command=='status':
            return dict(sources=source_status(load_dotenv()),snapshots=SourceStore(root/'sources').manifests())
        if args.source_command=='verify':
            from .source_verification import verify_sources
            return verify_sources(root,load_dotenv(),full=args.full,
                providers=args.providers.split(',') if args.providers else None)
        if args.source_command=='sync':
            return sync_sources(root,args.plan,load_dotenv())
        if args.source_command=='export-events':
            out=args.out or root/'sources'/'events.csv'
            frame=SourceStore(root/'sources').export_events(out)
            return dict(rows=len(frame),features=frame.feature.nunique(),output=str(out))
        if args.source_command=='export-documents':
            out=args.out or root/'sources/documents.jsonl'
            rows=SourceStore(root/'sources').export_documents(out)
            return dict(documents=len(rows),output=str(out),raw_news_not_sentiment=True)
        if args.source_command=='export-calendar':
            out=args.out or root/'sources/calendar_candidates.csv'
            rows=SourceStore(root/'sources').export_calendar(out,args.year)
            return dict(days=len(rows),output=str(out),verified=False,automatic_activation=False)
        if args.source_command=='reconcile':
            return reconcile_prices(PriceStore(root/'source_prices'/args.left).read_prices(),PriceStore(root/'source_prices'/args.right).read_prices())
    if args.command == "ingest":
        return import_data(root, args)
    if args.command == "demo":
        from .demo import make_demo_prices
        prices = make_demo_prices(args.sessions, args.seed)
        store = PriceStore(root / "data")
        store.write_prices(prices, metadata=dict(provider="synthetic_demo", seed=args.seed))
        cutoff = prices.available_at.max().isoformat()
        sessions = sorted(prices.date.unique())
        if len(sessions) <= 21:
            raise ValueError("Demo needs enough history plus 20 sessions to exercise maturity")
        replay_anchor = sessions[-21]
        replay_cutoff = prices.loc[prices.date == replay_anchor, "available_at"].max().isoformat()
        research(root, config_from(args.config), as_of=replay_cutoff, holdout_dates=args.holdout_dates)
        forecast(root, replay_cutoff, paper=True)
        matured = settle(root, cutoff)
        result = research(root, config_from(args.config), as_of=cutoff, holdout_dates=args.holdout_dates)
        predictions = forecast(root, cutoff, paper=True)
        return dict(root=str(root.resolve()), run_id=result["run_id"],
                    readiness=result["readiness"], forecasts=predictions["total"],
                    replay_settled=matured["newly_settled"],
                    report=str((root / "runs" / result["run_id"] / "report.json").resolve()))
    if args.command == "compare":
        from .comparison import compare_profiles
        from .workflows import load_events
        from .data import PriceStore
        result = compare_profiles(PriceStore(root/"data").read_prices(), config_from(args.config),
            load_events(args.events), args.holdout_dates, output=root/"comparison")
        write_json(root/"comparison"/"report.json", result)
        return dict(selected_profile=result["selected_profile"], scores=result["scores"], holdout_opened=False)
    if args.command in {'direction-search', 'direction-simulate'}:
        import uuid
        from .direction import DEFAULT_CANDIDATES, FACTOR_CANDIDATES, MARKET_CANDIDATES, FUSION_CANDIDATES, EXPOSURE_CANDIDATES, PATH_CANDIDATES, SECTOR_CANDIDATES, FLOW_CANDIDATES, MARGIN_CANDIDATES, RESPONSE_CANDIDATES, SIMILARITY_CANDIDATES, OPENING_CANDIDATES, OPENING_RESIDUAL_CANDIDATES, search_direction_models, simulate_information_scenarios
        from .direction_audit import load_membership_snapshot
        from .features import build_features
        from .workflows import data_kind, load_events
        run_id = datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]
        folder = root / 'direction' / run_id
        if args.command == 'direction-simulate':
            report = simulate_information_scenarios(sessions=args.sessions,
                seeds=tuple(int(s.strip()) for s in args.seeds.split(',')), output=folder)
            return dict(run_id=run_id, report=str((folder / 'report.json').relative_to(root)),
                evidence_kind='synthetic', scenarios=report['scenarios'], real_world_target_verified=False)
        if args.pin_reference and args.reference_run is None:
            raise ValueError('--pin-reference requires --reference-run')
        if args.taifex_exposure and not args.taifex_history:
            raise ValueError('--taifex-exposure requires --taifex-history')
        opening_proxy = args.candidate_set in {'opening-proxy', 'opening-residual'}
        if args.candidate_set in {'margin','response','similarity'} and args.mode != 'preopen':
            raise ValueError('margin/response/similarity requires --mode preopen for its21:00 receipt contract')
        if opening_proxy and args.mode != 'preopen':
            raise ValueError('Opening proxy studies require --mode preopen and are always synthetic')
        if not opening_proxy and any(v is not None for v in (args.opening_noise_bps, args.opening_missing_fraction, args.opening_seed)):
            raise ValueError('Opening simulation options require --candidate-set opening-proxy or opening-residual')
        if (args.candidate_set in {'sector', 'flow', 'margin', 'response', 'similarity', 'opening-proxy', 'opening-residual'}) != bool(args.sector_history):
            raise ValueError('--sector-history is required only with sector, flow, margin, response, similarity or an opening study')
        if (args.candidate_set in {'flow', 'margin', 'response', 'similarity', 'opening-proxy', 'opening-residual'}) != bool(args.flow_history):
            raise ValueError('--flow-history is required only with flow, margin, response, similarity or an opening study')
        if (args.candidate_set in {'margin','response','similarity'}) != bool(args.margin_history):
            raise ValueError('--margin-history is required only with --candidate-set margin, response or similarity')
        store = PriceStore(root / 'data')
        membership = load_membership_snapshot(args.membership) if args.membership else None
        config = config_from(args.config)
        if membership is not None:
            external = [t for t in membership['tickers'] if t not in TRAINING_TICKERS]
            config = replace(config, evaluation_tickers=tuple(dict.fromkeys([*config.evaluation_tickers, *external])))
            if not args.pin_reference:write_json(folder / 'membership_snapshot.json', membership)
        prices, events = store.read_prices(), load_events(args.events)
        taifex_sha = None
        if args.taifex_history:
            import pandas as pd
            from .taifex_history import parse_taifex_history, taifex_night_events
            history = parse_taifex_history(args.taifex_history.read_bytes())
            night_events = taifex_night_events(history)
            if night_events.empty:
                raise ValueError('No paired TAIFEX night prices in the supplied history')
            taifex_sha = history.attrs['input_sha256']
            events = pd.concat([events, night_events], ignore_index=True) if events is not None else night_events
        sector_sha, sector_sources = None, []
        if args.sector_history:
            from io import BytesIO
            import pandas as pd
            from .us_sectors import sector_price_events
            sector_bytes = args.sector_history.read_bytes()
            sector_events = sector_price_events(pd.read_csv(BytesIO(sector_bytes)))
            sector_sha = hashlib.sha256(sector_bytes).hexdigest()
            sector_sources = sorted(sector_events.source.unique().tolist())
            events = pd.concat([events, sector_events], ignore_index=True) if events is not None else sector_events
        kind = data_kind(prices)
        if events is not None and data_kind(events) == 'synthetic':
            kind = 'synthetic'
        frame = build_features(prices, external=events, mode=args.mode)
        if args.overseas_window:
            from .overseas import add_overseas_window_features
            frame = add_overseas_window_features(frame, events)
        if args.sector_history:
            from .overseas import add_overseas_window_features
            frame = add_overseas_window_features(frame, events, event_prefix='us_sector')
        if args.taifex_exposure:
            from .taifex_exposure import add_taifex_exposure_features
            frame = add_taifex_exposure_features(frame, history)
        price_path = args.candidate_set in {'path', 'sector', 'flow', 'margin', 'response', 'similarity', 'opening-proxy', 'opening-residual'}
        if price_path:
            from .price_path import add_price_path_features
            frame = add_price_path_features(frame)
        flow_sha, flow_sources = None, []
        if args.flow_history:
            from io import BytesIO
            import pandas as pd
            from .institutional_flow import add_institutional_flow_features
            flow_bytes = args.flow_history.read_bytes()
            flow_history = pd.read_csv(BytesIO(flow_bytes), dtype={'stock_id': str})
            frame = add_institutional_flow_features(frame, flow_history, lag_sessions=args.flow_lag_sessions)
            flow_sha = hashlib.sha256(flow_bytes).hexdigest()
            flow_sources = sorted(flow_history.source.unique().tolist())
            if data_kind(flow_history) == 'synthetic':
                kind = 'synthetic'
        margin_sha, margin_sources = None, []
        if args.margin_history:
            from io import BytesIO
            import pandas as pd
            from .margin_flow import add_margin_features
            margin_bytes = args.margin_history.read_bytes()
            margin_history = pd.read_csv(BytesIO(margin_bytes), dtype={'stock_id':str})
            frame = add_margin_features(frame, margin_history, lag_sessions=args.margin_lag_sessions)
            margin_sha = hashlib.sha256(margin_bytes).hexdigest()
            margin_sources = sorted(margin_history.source.unique().tolist())
            if data_kind(margin_history) == 'synthetic': kind = 'synthetic'
        if args.candidate_set in {'response','similarity'}:
            from .sector_response import add_sector_response_features
            frame=add_sector_response_features(frame)
        if opening_proxy:
            from .opening_proxy import add_opening_reference_proxy
            frame = add_opening_reference_proxy(frame,
                noise_bps=args.opening_noise_bps if args.opening_noise_bps is not None else 0.,
                missing_fraction=args.opening_missing_fraction if args.opening_missing_fraction is not None else 0.,
                seed=args.opening_seed if args.opening_seed is not None else 17)
            kind = 'synthetic'
        provenance = dict(snapshot_id=store.latest_snapshot_id(), evidence_kind=kind, mode=args.mode,
            sector_response_contract=frame.attrs.get('sector_response_contract'),
            margin_history_sha256=margin_sha, margin_sources=margin_sources,
            margin_receipt_contract=frame.attrs.get('margin_receipt_contract'),
            opening_proxy_contract=frame.attrs.get('opening_proxy_contract'),
            flow_history_sha256=flow_sha, flow_sources=flow_sources,
            flow_receipt_contract=frame.attrs.get('flow_receipt_contract'),
            sector_history_sha256=sector_sha, sector_sources=sector_sources,
            sector_receipt_assumption=('US17:00 local; original receipt unverified' if args.sector_history else None),
            price_path=price_path,
            overseas_window=args.overseas_window, taifex_history_sha256=taifex_sha,
            taifex_exposure=args.taifex_exposure,
            taifex_regular_receipt_assumption=('anchor date 20:00; original receipt unverified' if args.taifex_exposure else None),
            availability_basis=sorted(prices.availability_basis.unique().tolist()),
            external_availability_basis=(sorted(events.availability_basis.dropna().astype(str).unique().tolist())
                if events is not None and 'availability_basis' in events else []),
            external_expiring_features=(sorted(events.loc[events.expires_at.notna(), 'feature'].unique().tolist())
                if events is not None and 'expires_at' in events else []),
            limitation='Historical receipt timestamps and latest adjusted prices do not establish original point-in-time data.')
        if args.pin_reference:
            from .direction_reference import pin_direction_reference
            pinned=pin_direction_reference(frame,config,args.reference_run)
            return dict(reference_candidate_count=pinned['reference_candidate_count'],
                selected_models_replayed_exactly=pinned['selected_models_replayed_exactly'],
                pinned_models=len(pinned['models']),real_world_target_verified=False)
        write_json(folder / 'data_provenance.json', provenance)
        result = search_direction_models(frame, config,
            holdout_dates=args.holdout_dates, evidence_kind=kind, output=folder,
            candidates={'base': DEFAULT_CANDIDATES, 'factor': FACTOR_CANDIDATES,
                        'market': MARKET_CANDIDATES, 'fusion': FUSION_CANDIDATES, 'exposure': EXPOSURE_CANDIDATES,
                        'path': PATH_CANDIDATES, 'sector': SECTOR_CANDIDATES,
                        'flow': FLOW_CANDIDATES, 'margin': MARGIN_CANDIDATES, 'response': RESPONSE_CANDIDATES, 'similarity': SIMILARITY_CANDIDATES, 'opening-proxy': OPENING_CANDIDATES,
                        'opening-residual': OPENING_RESIDUAL_CANDIDATES}[args.candidate_set],
            selection_policy=args.selection_policy, mode=args.mode,
            constituents=membership['tickers'] if membership is not None else None,
            membership_as_of=membership['as_of'] if membership is not None else None,reference_run=args.reference_run)
        fields_to_report = ('overall_accuracy', 'minimum_stock_accuracy', 'worst_five_accuracy', 'coverage', 'empirical_target_met')
        summary = dict(run_id=run_id, report=str((folder / 'report.json').relative_to(root)),
            evidence_kind=kind, holdout_opened=False, real_world_target_verified=False,
            selection_policy=args.selection_policy, candidate_count=result.report['candidate_count'], mode=args.mode,
            overseas_window=args.overseas_window,
            taifex_history=bool(args.taifex_history),
            taifex_exposure=args.taifex_exposure,
            price_path=price_path,
            sector_history=bool(args.sector_history),
            flow_history=bool(args.flow_history),
            margin_history=bool(args.margin_history),
            opening_proxy=opening_proxy,
            by_horizon={h: {k: m[k] for k in fields_to_report} for h, m in result.report['by_horizon'].items()})
        if membership is not None:
            audits = result.report['constituents']['by_horizon']
            summary['constituents'] = dict(constituent_count=membership['constituent_count'],
                membership_as_of=membership['as_of'], combination_count=next(iter(audits.values()))['combination_count'],
                real_world_target_verified=False,
                by_horizon={h: {k: m[k] for k in fields_to_report} for h, m in audits.items()})
        return summary
    if args.command == "research":
        report = research(root, config_from(args.config), args.as_of, args.events, args.mode, args.holdout_dates)
        return dict(run_id=report["run_id"], metrics=report["metrics"], readiness=report["readiness"])
    if args.command == "predict":
        result = forecast(root, args.as_of, args.paper, args.events, args.fundamentals, tickers=args.tickers.split(",") if args.tickers else None)
        return dict(inserted=result["inserted"], forecasts=result["total"])
    if args.command == "settle":
        return settle(root, args.as_of)
    if args.command == "promote":
        return promote(root)
    if args.command == "holdout":
        return release_holdout(root)
    if args.command == "calendar":
        import pandas as pd
        cal = pd.read_csv(args.csv, dtype={"date":str})
        if not {"date","is_open","source"} <= set(cal):
            raise ValueError("Calendar requires date,is_open,source")
        if 'verified' in cal and not cal.verified.astype(str).str.lower().isin(['true','1']).all():
            raise ValueError('Candidate calendar is not verified; reconcile official schedule and emergency closures first')
        if cal.date.duplicated().any() or cal.source.isna().any():
            raise ValueError("Calendar must have unique dates and nonempty sources")
        pd.to_datetime(cal.date, errors="raise")
        root.mkdir(parents=True, exist_ok=True)
        cal.to_csv(root / "session_calendar.csv", index=False)
        return dict(calendar_rows=len(cal))
    if args.command == "daily":
        if not args.skip_ingest:
            import_data(root, args)
        cutoff = args.as_of or now_taipei()
        settlements = settle(root, cutoff)
        report = research(root, config_from(args.config), cutoff, args.events, args.mode, args.holdout_dates)
        if not args.paper:
            promote(root)
        result = forecast(root, cutoff, args.paper, args.events, args.fundamentals, tickers=args.tickers.split(",") if args.tickers else None)
        return dict(newly_settled=settlements["newly_settled"], run_id=report["run_id"],
                    forecasts=result["total"], readiness=report["readiness"])
    if args.command == "status":
        from .workflows import current_report
        try:
            report = current_report(root)
        except ValueError:
            report = None
        return dict(root=str(root.resolve()), report=report, monitoring=ledger_for(root).monitoring())
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        workspaces={'demo':Path('artifacts/demo'),'real':Path('artifacts/real')}
        if root.resolve() not in {p.resolve() for p in workspaces.values()}:
            workspaces['current']=root
        uvicorn.run(create_app(root,workspaces=workspaces,enable_actions=True), host="127.0.0.1", port=args.port)
        return None


def main(argv=None):
    args = parser().parse_args(argv)
    credentials=None
    try:
        if args.command not in {'direction-feasibility','direction-queue-study','direction-queue-ceiling','direction-first-change-study','direction-queue-first-study','direction-trade-first-study','direction-matching-study'} and not (args.command=='secrets' and args.secret_command=='init'):
            credentials=load_dotenv()
        result = execute(args)
        if result is not None:
            from .ledger import json_safe
            from .credentials import redact
            result=redact(result,credentials.secret_values() if credentials else ())
            print(json.dumps(json_safe(result), ensure_ascii=False, indent=2, allow_nan=False))
        if args.command=='secrets' and args.secret_command=='audit' and not result['passed']:
            return 2
        if args.command=='sources' and args.source_command=='sync' and any(r['status']=='error' for r in result['jobs']):
            return 2
        if args.command=='sources' and args.source_command=='verify' and not result['passed']:
            return 2
        return 0
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        from .credentials import redact
        message=redact(str(exc),credentials.secret_values() if credentials else ())
        print(f"twquant: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
