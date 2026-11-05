"""Command line entry point: python -m twquant --help."""
import argparse
from dataclasses import fields
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from .workflows import forecast, ledger_for, promote, release_holdout, research, settle, write_json


TICKERS = ["2330", "2454", "2317", "2308", "3711", "1303", "2383", "2881", "2891", "3037", "0050"]


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
    f = subs.add_parser("predict", help="保存不可覆寫的1/5/20日預測")
    f.add_argument("--as-of", required=True)
    f.add_argument("--paper", action="store_true")
    f.add_argument("--events", type=Path)
    f.add_argument("--fundamentals", type=Path)
    s = subs.add_parser("settle", help="只評分已到期且實際可得的結果")
    s.add_argument("--as-of", required=True)
    subs.add_parser("status", help="模型狀態、可行性缺口與逐筆預測追蹤")
    subs.add_parser("promote", help="檢查門檻後選定正式預測模型；不涉及下單")
    subs.add_parser("holdout", help="開封已凍結的最終保留測試；結果僅計算一次供審核")
    credentials=subs.add_parser('secrets',help='建立私人金鑰欄位或執行開源前洩密檢查')
    credential_sub=credentials.add_subparsers(dest='secret_command',required=True)
    credential_sub.add_parser('init')
    audit=credential_sub.add_parser('audit')
    audit.add_argument('--repo',type=Path,default=Path('.'))
    sources=subs.add_parser('sources',help='多來源狀態、擷取、模型特徵匯出與交叉核對')
    source_sub=sources.add_subparsers(dest='source_command',required=True)
    source_sub.add_parser('status')
    sync=source_sub.add_parser('sync')
    sync.add_argument('--plan',type=Path,default=Path('configs/source_plan.json'))
    export=source_sub.add_parser('export-events')
    export.add_argument('--out',type=Path)
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
    root = args.root or Path("artifacts/demo" if args.command == "demo" else "artifacts/real")
    if args.command=='export-web':
        from .web_export import export_dashboard
        return export_dashboard(root,args.out)
    if args.command=='secrets':
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
        if args.source_command=='sync':
            return sync_sources(root,args.plan,load_dotenv())
        if args.source_command=='export-events':
            out=args.out or root/'sources'/'events.csv'
            frame=SourceStore(root/'sources').export_events(out)
            return dict(rows=len(frame),features=frame.feature.nunique(),output=str(out))
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
    if args.command == "research":
        report = research(root, config_from(args.config), args.as_of, args.events, args.mode, args.holdout_dates)
        return dict(run_id=report["run_id"], metrics=report["metrics"], readiness=report["readiness"])
    if args.command == "predict":
        result = forecast(root, args.as_of, args.paper, args.events, args.fundamentals)
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
        result = forecast(root, cutoff, args.paper, args.events, args.fundamentals)
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
        return 0
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        from .credentials import redact
        message=redact(str(exc),credentials.secret_values() if credentials else ())
        print(f"twquant: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
