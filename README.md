# 0050 多因子預測與估值研究系統

這是一套能在本機執行的資料 → 特徵 → 歷史驗證 → 預測 → 到期評分／校準系統。第一版以日線為核心，提供十檔股票的 1、5、20 交易日方向機率、中央報酬估計、80% 殘差區間及獨立基本面估值。

**目前已完成工程流程與多來源連接器，尚未證明真實市場預測優勢。** `demo` 使用合成資料，不能用示範績效判定投資可行性。可選 FinMind、Fugle 或富邦提供還原行情，並接入 FRED、FMP、News API、Twinkle。已實測部分帳號與方案限制；富邦需在相容的官方SDK環境使用私人憑證。另有無需Key的TWSE、TAIFEX、TDCC及GDELT接入計畫，保留實際取得時間及資料邊界。

金鑰僅存本機私人 `.env`（0600）或環境變數；可提交的 `.env.example` 保持空白。請先讀 [網頁操作指南](docs/WEB_GUIDE.md)、[來源與Key設定](docs/DATA_SOURCES.md)、[完整專案剩餘項目](docs/PROJECT_STATUS.md)、[Key保護與開源檢查](docs/SECRETS.md)。

已接資料後，完整歷史與每天的實際操作請讀[真實歷史檢查與每日影子預測](docs/DAILY_RESEARCH.md)：長期計畫為`configs/market_context_plan.json`，全歷史多折設定為`configs/research_history.json`。本機成果與線上合成預覽分開，沒有自動啟用排程。

[架構、工程完成比例與正式驗收邊界](docs/SYSTEM_READINESS.md)將第一階段工程、資料完整性及模型可行性分開列示；可執行流程不等於已通過正式上線。

[優化順序與驗收方法](docs/OPTIMIZATION_PLAN.md)列出基準／機率校準、可成交目標、因子消融、歷史版本、執行環境及每日營運的下一步；建議項目尚未全部實作。

富邦`.p12`／`.pfx`可透過`secrets import-cert --from /absolute/path/exported.p12`安全複製至本機私人目錄；API費用、免費替代與人工設定見[憑證及資源指南](docs/CERTIFICATES_AND_COSTS.md)。免費資料可直接執行：

```bash
.venv/bin/python -m twquant --root artifacts/public-live sources sync --plan configs/public_source_plan.json
.venv/bin/python -m twquant --root artifacts/public-live sources export-events
.venv/bin/python -m twquant --root artifacts/public-live sources export-documents
.venv/bin/python -m twquant --root artifacts/public-live sources export-calendar --year 2026
```

年度日曆匯出是待核實候選，不能直接啟用。GDELT僅取得最新15分鐘批次的新聞中繼資料，不是全天新聞或完整文章。沒有自動安裝背景收集排程。

```bash
.venv/bin/python -m twquant sources status
.venv/bin/python -m twquant sources verify --full
.venv/bin/python -m twquant secrets audit
# Key填入本機.env之後，依設定計畫抓取；缺Key的來源會明確列出。
.venv/bin/python -m twquant --root artifacts/real sources sync --plan configs/source_plan.json
.venv/bin/python -m twquant --root artifacts/real sources export-events
```

固定訓練十檔、0050／1216／2002／2412／TAIEX同模型測試與優化操作，見[固定十檔研究協定](docs/FIXED_TEN_PROTOCOL.md)。不將測試標的加入訓練；只有真實未來紀錄可作逐日驗證。

[任意五檔方向準確率研究](docs/DIRECTION_RESEARCH.md)提供八組模型的時序選擇、全部252種五檔組合驗收，以及假設額外資訊的多種子模擬。真實歷史研究尚未達75%；強訊號合成情境的通過不代表已發現真實市場方法。

完整當期0050名單可透過`direction-search --membership <本機完整持股快照.json>`作同模型延伸驗收，涵蓋50檔的2,118,760種五檔組合；訓練與選模仍只限固定十檔，當期名單不作歷史成分證明。

方向研究可選`--candidate-set market`，在原十組外加入六組「價格＋美股市場資訊」候選，並以原時序流程比較完整事件集合；此研究不會自動替換正式模型。範例及證據限制見[研究協定](docs/DIRECTION_RESEARCH.md)。

加上`--overseas-window`可研究錨點20:00到決策截止之間新增的海外累積變動；需全球FMP收盤事件。它保留既有輸入與未知資料，不會把缺少起始報價填成零。

方向研究另可指定`--mode preopen`，使用下一個實際市場日08:30前已可得的資訊，維持同樣的前收盤錨點與未來收盤標籤；預設仍為`postclose`。模式、決策時間與資料來源均列入實驗紀錄。

## 啟動

本工作區已建立 `.venv` 並安裝依賴。直接執行：

```bash
.venv/bin/python -m twquant demo
.venv/bin/python -m twquant --root artifacts/demo holdout
.venv/bin/python -m twquant --root artifacts/demo serve --port 8000
```

開啟 <http://127.0.0.1:8000> 使用繁體中文研究工作台，提供六個頁面：研究總覽、個股預測、歷史回測、資料來源、工作流程、待完成項目。可切換示範／真實工作區、1／5／20 日、搜尋股票、查看歷史價格與區間、線性基準因子及估值，並匯出 JSON。示範行情最後一天固定為 2025-12-31，畫面不是即時行情。

本機「工作流程」可建立示範、同步來源、研究、開封保留測試、產生影子預測與結算。每次只執行一項工作，結果與錯誤顯示在執行紀錄；任務不會透過網頁接受任意命令或金鑰。`/docs` 提供 API 文件，寫入操作只開放固定任務路由。

線上版本採**合成資料靜態快照**，展示相同圖表並停用執行功能；真正的資料擷取與模型運算需啟動本機 Python。可自行產生安全預覽：

[開啟線上研究工作台](https://tw0050-research-lab.jaaack1203.chatgpt.site)（私人Site，需擁有者帳號登入）。

```bash
.venv/bin/python -m twquant --root artifacts/demo export-web --out artifacts/web-preview
```

`demo` 會先重播較早時點的30筆預測，結算其1／5／20日答案，再以較新資料重新訓練並產生30筆最新預測，示範完整校正循環。所有重播都明確標記，不計入真實每日影子驗證門檻。

在新環境安裝：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m pytest -q
node --test tests/web_state.test.mjs
```

Python >=3.11；本機驗證環境為 Python 3.14.3／macOS arm64。`requirements.lock` 保存本次實際驗證版本；其他作業系統優先用 `pyproject.toml` 安裝。`LOKY_MAX_CPU_COUNT=1` 避免受限環境探測 CPU 的警告；模型數值計算已限制執行緒。

## 真實歷史研究

金鑰放在本機 `.env` 或環境變數，勿提交版本庫。`.env.example` 提供格式。

```bash
# 下載原始與還原行情，逐筆對齊；資料不足或權限失敗會中止。
.venv/bin/python -m twquant --root artifacts/real ingest --start 2014-01-01 --end 2026-11-04

# 也可匯入自己的完整歷史快照。
.venv/bin/python -m twquant --root artifacts/real ingest --csv /absolute/path/prices.csv

# 最後126個交易日保留給最終測試，開發回測不使用其答案。
.venv/bin/python -m twquant --root artifacts/real research --config configs/research.json

# 固定程式、設定、資料快照後才開封；重跑只讀取原結果。
.venv/bin/python -m twquant --root artifacts/real holdout
```

資料至少包含 `ticker,date,open,high,low,close,adj_close,volume,available_at`。代號必須保存前導零（`0050`），成交量單位為股，時間須含時區。`source,adjustment_source,availability_basis` 用來記錄品質與來源；未提供時標示「使用者提供／未驗證」。[CSV 範例](examples/prices.template.csv) 是格式示意，不能作真實資料使用。

`ingest` 匯入的是**完整快照**，不會把缺漏的新檔偷偷拼接到舊版還原價格。每日下載仍抓完整設定區間，以免混合不同還原基準；日後可在公司行動版本一致性檢查後加入增量更新。每次保存不可覆寫原始回應／CSV、Parquet、SHA256 與來源資訊。

可用 `--events examples/events.csv` 示範具時間戳的外部因子。來源快照已支援匯出FRED、美股及部分財務數值並按可用時間對齊；夜盤連續合約、新聞事件及政府資料的特徵映射仍待驗證。範例均為合成資料。

## 每日追蹤與校正

在可行性通過前，用 `--paper` 進行影子預測：

```bash
# 假設這個時間前，已匯入當天完整行情。
.venv/bin/python -m twquant --root artifacts/real predict --as-of 2026-11-04T20:00:00+08:00 --paper

# 新行情到達後，只評分已成熟且實際可取得的答案。
.venv/bin/python -m twquant --root artifacts/real settle --as-of 2026-11-05T20:00:00+08:00

# 匯入、結算、重新訓練與時間序列校準、保存今日預測。
.venv/bin/python -m twquant --root artifacts/real daily --start 2014-01-01 --paper

# 狀態、資料缺口、回測與到期表現。
.venv/bin/python -m twquant --root artifacts/real status
```

上述日期是指令格式範例，實際執行時請使用當時截止時間。盤後模型限定最新完成交易日 20:00 之後；資料過期、當日尚未收齊或必需欄位缺漏時停止出報。可加入 `--fundamentals /path/fundamentals.csv` 計算當時可得的估值；未提供財務資料會顯示資訊不足。

校正採**新版本模型**：以已成熟答案重新訓練，另取較近且獨立的時段校準機率與殘差區間。舊預測保持不變。系統保留實際發出時間，事後歷史重播不會累積為正式影子實績。

盤前研究使用獨立根目錄及模型：

```bash
.venv/bin/python -m twquant --root artifacts/preopen ingest --csv /path/prices.csv
.venv/bin/python -m twquant --root artifacts/preopen research --mode preopen
.venv/bin/python -m twquant --root artifacts/preopen calendar --csv /path/exchange-calendar.csv
.venv/bin/python -m twquant --root artifacts/preopen predict --as-of 2026-11-05T08:30:00+08:00 --paper
```

日曆格式為 `date,is_open,source`，須使用交易所核實日期，不能直接把週一到週五當作開市。盤前訊號參考上一交易日收盤價，外部資料按實際 08:30 可得時間選取。歷史盤前模式不等於已接上真實夜盤或美股資訊。

正式預測前執行 `promote`；門檻包括實際來源／還原與 PIT 品質、十年基準歷史、個股歷史覆蓋、樣本外優勢、最終保留測試，以及至少 60 個真實按時發出的市場決策日／500 筆已成熟一日預測。相同固定研究程序的有效保留測試證據可沿用 90 天；變更程式、特徵或設定會建立新程序版本。這是預測啟用門檻，專案沒有券商下單功能。

`scripts/daily.sh` 與 `configs/schedule.crontab.example` 提供排程範本，**未安裝到系統排程**。取得真實資料並完成影子驗證前，保留 paper 模式。請確保機器開機、時區及可核實的開休市日曆。

## 產物與驗證

- `artifacts/<環境>/data/raw/`：來源原始回應／CSV。
- `data/snapshots/<快照>/`：Parquet、manifest 與 SHA256。
- `runs/<版本>/`：設定、封存資訊、特徵快照、逐筆樣本外預測、交易曲線、模型與報告。
- `forecasts.sqlite`：不可覆寫預測、實際發出時間及到期結果。
- `latest_forecast.json`、`monitoring.json`：最新預測與監控輸出。

目前未直接採用 Qlib、LightGBM、FinGPT、vectorbt 或 MLflow；核心使用 pandas／DuckDB／scikit-learn，保留替換介面。這能先驗證資料與樣本外流程，再依增益引入更複雜的模型。

GitHub 主儲存庫為 [Jaa-ack/0050-Predictor](https://github.com/Jaa-ack/0050-Predictor)。CI 執行 Python 3.11／3.14、前端狀態測試、金鑰／歷史掃描，並產生合成網頁預覽 artifact；Dependabot 每週提出依賴更新 PR，待審核合併。後續已授權變更須更新文件、驗證及 commit，規則見 [AGENTS.md](AGENTS.md) 與 [CONTRIBUTING.md](CONTRIBUTING.md)。尚未選定正式程式碼 LICENSE。

完整說明見 [方法與限制](docs/METHODOLOGY.md)、[最新缺口與驗收順序](docs/PROJECT_STATUS.md)、[操作與故障排查](docs/RUNBOOK.md)、[架構](docs/ARCHITECTURE.md)。

來源實測、修正及手動設定見 [API驗收](docs/API_VERIFICATION.md)；未接入但值得採用的官方資料／套件見 [OPEN_RESOURCES.md](docs/OPEN_RESOURCES.md)。
