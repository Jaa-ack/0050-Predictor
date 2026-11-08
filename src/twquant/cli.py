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
    direction = subs.add_parser('direction-search', help='巢狀時間比較方向模型，檢查固定十檔全部252種五檔組合；不開封保留期')
    direction.add_argument('--config', type=Path)
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
                           help='sector候選必需；七條含source的正規化海外產業CSV，假設美東17:00收件')
    direction.add_argument('--holdout-dates', type=int, default=126)
    direction.add_argument('--membership', type=Path, help='完整當期0050持股JSON快照；外部成分只作同模型測試')
    direction.add_argument('--candidate-set', choices=['base', 'factor', 'market', 'exposure', 'path', 'sector'], default='base',
                           help='base八組；factor十組；market十六組；exposure二十四組；path三十組；sector三十六組')
    direction.add_argument('--selection-policy', choices=['worst_stock_accuracy', 'pooled_brier', 'worst_stock_brier'],
                           default='worst_stock_accuracy', help='Brier規則固定0.5門檻；只以校準期選模')
    simulation = subs.add_parser('direction-simulate', help='模擬假設資訊量、延遲及缺漏；合成結果不是真實預測證據')
    simulation.add_argument('--sessions', type=int, default=1200)
    simulation.add_argument('--seeds', default='17,29,43', help='保留所有指定種子的結果，不挑選幸運種子')
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
    default_root = {'demo': 'artifacts/demo', 'direction-simulate': 'artifacts/direction-synthetic'}
    root = args.root or Path(default_root.get(args.command, 'artifacts/real'))
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
        from .direction import DEFAULT_CANDIDATES, FACTOR_CANDIDATES, MARKET_CANDIDATES, EXPOSURE_CANDIDATES, PATH_CANDIDATES, SECTOR_CANDIDATES, search_direction_models, simulate_information_scenarios
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
        if args.taifex_exposure and not args.taifex_history:
            raise ValueError('--taifex-exposure requires --taifex-history')
        if (args.candidate_set == 'sector') != bool(args.sector_history):
            raise ValueError('--sector-history is required only with --candidate-set sector')
        store = PriceStore(root / 'data')
        membership = load_membership_snapshot(args.membership) if args.membership else None
        config = config_from(args.config)
        if membership is not None:
            external = [t for t in membership['tickers'] if t not in TRAINING_TICKERS]
            config = replace(config, evaluation_tickers=tuple(dict.fromkeys([*config.evaluation_tickers, *external])))
            write_json(folder / 'membership_snapshot.json', membership)
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
        price_path = args.candidate_set in {'path', 'sector'}
        if price_path:
            from .price_path import add_price_path_features
            frame = add_price_path_features(frame)
        provenance = dict(snapshot_id=store.latest_snapshot_id(), evidence_kind=kind, mode=args.mode,
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
        write_json(folder / 'data_provenance.json', provenance)
        result = search_direction_models(frame, config,
            holdout_dates=args.holdout_dates, evidence_kind=kind, output=folder,
            candidates={'base': DEFAULT_CANDIDATES, 'factor': FACTOR_CANDIDATES,
                        'market': MARKET_CANDIDATES, 'exposure': EXPOSURE_CANDIDATES,
                        'path': PATH_CANDIDATES, 'sector': SECTOR_CANDIDATES}[args.candidate_set],
            selection_policy=args.selection_policy, mode=args.mode,
            constituents=membership['tickers'] if membership is not None else None,
            membership_as_of=membership['as_of'] if membership is not None else None)
        fields_to_report = ('overall_accuracy', 'minimum_stock_accuracy', 'worst_five_accuracy', 'coverage', 'empirical_target_met')
        summary = dict(run_id=run_id, report=str((folder / 'report.json').relative_to(root)),
            evidence_kind=kind, holdout_opened=False, real_world_target_verified=False,
            selection_policy=args.selection_policy, candidate_count=result.report['candidate_count'], mode=args.mode,
            overseas_window=args.overseas_window,
            taifex_history=bool(args.taifex_history),
            taifex_exposure=args.taifex_exposure,
            price_path=price_path,
            sector_history=bool(args.sector_history),
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
        if not (args.command=='secrets' and args.secret_command=='init'):
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
