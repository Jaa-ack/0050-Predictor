# 開源股票預測技術研究與首輪實驗

更新：2026-11-14，Asia/Taipei。這是可持續更新的研究紀錄，不是套件安裝清單。首輪已在本機真實歷史快照完成三項實驗：Qlib 因子消融、Darts 啟發的區間校準、Freqtrade 啟發的特徵截斷重算。程式與正式模型、資料庫、帳本及排程隔離。

**目前沒有足以替換正式預測模型的證據。** 五日因子的相對改善仍輸給簡單先驗；一日區間有初步改善，但沒有改善方向準確率。截斷稽核提供工程驗證，不提供市場優勢。這輪只評估盤後日線，不能移用到每日 10:00 的固定終點、60 秒或 300 秒首次變價目標，也未完成全 50 檔驗收。

## 1. 現有系統：依實際程式碼確認

| 項目 | 現有實作與檔案 | 對本輪研究的影響 |
|---|---|---|
| 資料契約與保存 | [`data.py`](../src/twquant/data.py)：OHLC、還原收盤、股數成交量、時區、可用時間檢查，Parquet／SHA256／DuckDB；[`ingestion.py`](../src/twquant/ingestion.py)及來源連接器 | 使用既有快照唯讀載入，不重新下載、拼接或覆寫歷史 |
| 日線目標 | [`features.py`](../src/twquant/features.py)：`log(adj_close[t+h]/adj_close[t])`，h=1/5/20 個市場交易日；方向為嚴格大於零；盤後 20:00 或下一市場日盤前 08:30 | 固定本輪為盤後、收盤至未來收盤，同一期間比較各臂 |
| 既有特徵 | 同檔案的多期動能、波動、跳空、區間、相對量、RSI、Beta、固定十檔市場及排除自身同儕；[`overseas.py`](../src/twquant/overseas.py)、`price_path.py`、`institutional_flow.py` 等為選配研究 | 不重複引入整套技術指標庫；新增九個形狀／趨勢／量價因子作獨立消融 |
| 外部資訊 | [`source_store.py`](../src/twquant/source_store.py)、[`source_workflows.py`](../src/twquant/source_workflows.py)：FRED 版本、美股 FMP、部分財務／官方事件、文件快照；`point_in_time_join` 按發布、收件、修訂及到期時間對齊 | 沿用既有事件檔與 PIT join；不將今日新聞或財報回填過去年份 |
| 模型 | [`research.py`](../src/twquant/research.py)：各 h 一個跨股票共享模型；Ridge／Logistic 與 histogram boosting 各半；訓練期中位數填補、線性標準化，獨立時間段 sigmoid／時序收縮校準，另估算術報酬 | 兩個預測臂使用同一 `_fit`、校準政策與種子，僅改輸入欄位 |
| 不確定性 | 同檔案：絕對對數殘差的 pooled 80% 區間；`q10/q90` 為相容欄位，不是真正條件分位數；中央報酬為 `exp(預測對數報酬)-1` | 區間縮放只改上下界，禁止宣稱改進分類器或算術期望 |
| 時序評估 | `temporal_splits` 依日期分組，隔離未成熟／重疊標籤及 embargo；[`holdout.py`](../src/twquant/holdout.py)、[`workflows.py`](../src/twquant/workflows.py)封存最後 126 日；[`comparison.py`](../src/twquant/comparison.py)消融，[`diagnostics.py`](../src/twquant/diagnostics.py)日期區塊抽樣 | 沿用切分與成熟條件，不隨機打散股票列，不開封保留期 |
| 方向與完整覆蓋 | [`direction.py`](../src/twquant/direction.py)、[`direction_audit.py`](../src/twquant/direction_audit.py)：時序選模、逐檔與任意五檔完整分母；[`universe.py`](../src/twquant/universe.py)固定十檔 | 漏報、未知答案及弱股都保留；外部五標的只能向同一模型查詢 |
| 策略 | `simulate_portfolio`：只形成 h1 下一開盤至當日收盤淨值；費用、股／ETF 賣出稅、滑價、現金、權重、換手、回撤及被動 0050 | 不需要替換成 VectorBT；同批選單另做雙倍滑價，不把收盤方向當可成交報酬 |
| 營運與實績 | `workflows.daily`／CLI：擷取→結算→研究及重訓→影子預測；[`ledger.py`](../src/twquant/ledger.py)不可覆寫；[`scripts/daily.sh`](../scripts/daily.sh)與 crontab 只是範本 | 沒有啟用背景任務；新研究器不呼叫 daily、promote、下單或網頁工作路由 |
| 盤中研究 | `direction_intraday.py`、`first_change.py`、`trade_first_change.py`、`matching_simulation.py`、`matching_clock_scale.py` 等；[60 秒研究](DEPTH_CLOCK_FIRST_CHANGE.md)與[300 秒研究](FIVE_MINUTE_FIRST_CHANGE.md)各自保存 | 多數已完成結果是明示人工撮合情境；本輪不把日線成果轉成盤中證據 |

優點是資料／標籤時間契約、不可覆寫產物、同模型轉移測試及完整分母已存在。主要限制仍是歷史當時收件與公司行動版本未獲充分證實、複雜模型未穩定勝過先驗、長期間區間覆蓋不足、可成交模擬缺容量與撮合，以及真正逐日未來影子樣本不足。新聞 Agent 或大型模型不能補上這些證據。

## 2. 開源專案查核表

以下均查閱公開 GitHub metadata、預設分支 HEAD、README、實際程式碼及授權；完整 40 碼 commit、已讀檔案、SHA256、永久連結與授權辨識差異保存在 [`open_source_sources.json`](research/open_source_sources.json)。表中的日期是被查 HEAD 的 commit 日期（UTC），不是對活躍程度或維護品質的保證。查核時 15 個專案均未標為 archived。

分類：**直**＝直接套用；**改**＝修改後套用；**重寫**＝參考後重新實作；**不採**＝目前不建議完整採用。成本是針對本專案的定性判斷；P1 為本輪，P2 為後續研究，P3 為暫緩。上游績效敘述沒有當作本系統的驗證結果。

| 專案／來源與固定版本 | 授權／維護觀察 | 讀到的實作與可抽取技術 | 採用方式、成本、主要風險／優先度 |
|---|---|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib/tree/54355232463878d2eebb91fe0ee5fa7fa1f5976c) `543552324638` | MIT；2026-10-08 | `contrib/data/loader.py` 的 Alpha158 蠟燭／Slope／Rsquare／Resi／Corr／SUMP／WVMA；`dataset/processor.py` 的訓練期 median/MAD 與截面 rank；handler 將學習與推論處理器分開 | 因子**重寫**，本輪零新依賴／低成本／P1。完整 Qlib **不採**：需轉資料格式、Cython／服務依賴；中國股票池／標籤不能沿用。未來截面正規化只能以固定十檔為參考，外部標的不能改變尺度 |
| [Kronos](https://github.com/shiyu-coder/Kronos/tree/67b630e67f6a18c9e9be918d9b4337c960db1e9a) `67b630e67f6a` | MIT 程式碼；2026-04-13，較久未更新 | `model/kronos.py`：OHLCV tokenizer、分層離散碼、autoregressive predictor、取樣與反正規化；README 微調分 tokenizer／predictor | 預訓練模型可**改**，高成本／P2；依賴 Torch、HF、權重及可核對訓練截止日，需把路徑轉成同錨點方向機率。缺權重與 runtime，本輪未執行；取樣誤差、價格還原與預訓練污染待查 |
| [Amazon Chronos](https://github.com/amazon-science/chronos-forecasting/tree/1224fafa91e1ecd6674f5204066b159e40b5e4a1) `1224fafa91e1` | Apache-2.0 程式碼；2026-10-05 | `chronos.py` 的 mean-scale uniform bins／mask／sample quantiles；`chronos2/pipeline.py` 已有新一代 fit、covariates／group IDs，不能只依舊版介紹 | API 可**直**、目標轉換需**改**；中高成本／P2。Torch>=2.2,<3、Transformers>=4.41,<6 目前未裝；權重版本、基礎模型與訓練資料授權／污染需另審。禁止把五個測試標的放入 joint group 的訓練或上下文 |
| [NeuralForecast](https://github.com/Nixtla/neuralforecast/tree/5bcb26d86aa6523cec76d12b78a656a1754e7490) `5bcb26d86aa6` | Apache-2.0；2026-11-12 | `core.py` 的 refit／no-refit cross-validation、val/test windows；`losses/pytorch.py` 的 quantile／分布損失；可選 NHITS／NBEATS 等全域模型 | 模型及 quantile loss 可**改**，中高成本／P2；當前依賴 Torch>=2.9.1、Lightning>=2.6.6。資料 `unique_id/ds/y`／scaler 可用，但不能以預設等距日曆跳過台股休市、PIT 或重疊標籤隔離 |
| [Darts](https://github.com/unit8co/darts/tree/e679f7bea284734b15961c9ee3c4e4fc3fcd7fd2) `e679f7bea284` | Apache-2.0；2026-11-13 | `conformal_models.py`：凍結預測器、獨立歷史殘差、對稱／非對稱校準與 `_conformal_quantile` 精確有限樣本排序 | 校準 wrapper **重寫**／低成本／P1。波動率縮放是本輪自行提出的改法，不是已驗證的 Darts 功能成效；完整套件及深度 extras 暫不加。時間相依下無無條件涵蓋保證 |
| [Microsoft RD-Agent](https://github.com/microsoft/RD-Agent/tree/484776c211e4fbbeef03e0ec00d6bbee7362a4f4) `484776c211e4` | MIT；2026-09-23 | `scenarios/qlib/experiment/factor_experiment.py` 的 FactorTask／experiment workspace／base-feature code 分離，假設→開發→回饋迴圈 | 有界研究登錄可**重寫**／低中成本／P2；本輪採用固定假設與先存 protocol 的概念，沒有啟動 Agent。完整 LLM／容器執行、反覆試驗成本與多重選擇偏誤高，不允許自動改正式模型 |
| [FinGPT](https://github.com/AI4Finance-Foundation/FinGPT/tree/ac5c5443fbeeafad801e7a3c188e4f24cfaad4d8) `ac5c5443fbee` | MIT 程式碼；2026-10-25 | Forecaster `data.py` 將週報酬分箱、新聞與財務轉提示；`train_lora.py` 是 PEFT/LoRA 監督微調及生成評估，並非現成台股日線分類器 | 事件特徵可**改**／高成本／P3；需繁中歷史文本、首次發布／修訂、去重與實體映射。setup 限 Python<3.13，與本機3.14不合；Llama 基礎模型、LoRA、新聞各有獨立條款。現成美股週目標不公平 |
| [TradingAgents](https://github.com/TauricResearch/TradingAgents/tree/9fa1196fffbcfd9de5146d2dd2869df84ab99838) `9fa1196fffbc` | Apache-2.0；2026-11-04 | `graph/trading_graph.py` 的分析員圖、checkpoints、記憶／settlement、執行 signature；不是簡單「多 Agent 投票」 | 執行紀錄設計可**重寫**／中成本／P3；完整圖**不採**。LangChain／LangGraph、外部 LLM／行情費用，當前新聞與模型記憶可能污染歷史回測，文字結論不等於校準機率 |
| [VectorBT](https://github.com/polakowo/vectorbt/tree/9ac1b697166b00911def494cf95937274218aa54) `9ac1b697166b` | **Apache-2.0 + Commons Clause**，不是無限制 Apache；2026-11-11 | `portfolio/base.py`／`nb.py` 的 broadcast、逐時間列模擬、`from_signals`、fees／slippage、共用資金／執行順序 | 向量化成本網格與凍結訊號概念**重寫**；完整依賴**不採**／中成本／P3。LICENSE 限制 Sell 範圍；Numba>=0.66、NumPy>=2.4.6、Pandas>=3.0.3 會擴大依賴；現有 simulator 已有主要能力 |
| [QuantConnect LEAN](https://github.com/QuantConnect/Lean/tree/82d26047e95a89b53c9cbe02b67efaa3c4455ed9) `82d26047e95a` | Apache-2.0 引擎；2026-11-13 | `AlgorithmManager.cs` 按同步 timeSlice 推進、交易 handler 分離；Engine csproj 目標 net10.0、Python.NET／NodaTime | 時鐘／事件與填單模型可**重寫**／中高成本／P2；全引擎**不採**。台灣資料／費率／撮合需自建，雲端／資料服務權利不由引擎 LICENSE 提供；本輪不需要 broker layer |
| [FinRL-X / FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading/tree/4409abe925c904e570be78ebfb5e77ac3491dff8) `4409abe925c9` | Apache-2.0；2026-09-18；README 品牌為 FinRL-X，repo 仍為 FinRL-Trading | `adaptive_rotation/walk_forward.py` 的決策期物件；`backtest_engine.py` 以權重為介面、bt 成本模型／成交量與波動配對 | 權重契約及成本模型可**重寫**／中成本／P2。資料前填不能代表停牌可成交；RL reward 是策略績效，不是方向改善。Alpaca、bt、RL 選配及美股欄位需轉換；完整交易平台**不採** |
| [Freqtrade / FreqAI](https://github.com/freqtrade/freqtrade/tree/5197bedab82559fac6be6c1539c64b94368fb91c) `5197bedab825` | GPL-3.0（以 LICENSE 為準）；2026-11-12 | `optimize/analysis/lookahead.py` 的 full／cut indicators 差異；`freqai/data_kitchen.py` 的時間窗、特徵／標籤／模型資料分工 | 截斷稽核**重寫**／低成本／P1；不複製 GPL 程式、不啟動其交易或刪模型目錄邏輯。24/7 加密市場窗口與台股不同；完整套件／重訓管理需多個交易所及 ML 依賴，暫不導入 |
| [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis/tree/ac9c33dfcf4cb84974d7bfee9c8661d88b8cfbff) `ac9c33dfcf4c` | MIT；2026-11-14 | `src/analyzer.py`、provider manager 的備援路由；本版已有 `tw_institutional_fetcher.py` 的 TWSE/TPEx 法人、TTL、節流、熔斷，不能再概括成只支援 A 股 | 節流／熔斷可**重寫**／中成本／P2。備援只能在還原基準、單位與日期契約一致時使用；fail-open／空結果不能變成零因子。整套 LLM 報告與推播**不採**；新抓的官方資料仍無當年收件證明 |
| [OpenBB / Open Data Platform](https://github.com/openbq-org/OpenBB/tree/e43353a82815ff9ba56ad1b7ad47c32729dcf314) `e43353a82815` | 根 LICENSE 為 **Apache-2.0**，API 的 NOASSERTION 不作結論；2026-11-13；README 名稱 ODP | `provider/abstract/fetcher.py` 的 query→extract→typed transform／AnnotatedResult、同步／非同步與 streaming seam | Provider 可**直**接於未來獨立 adapter，或契約**重寫**／中成本／P2。目前來源層已有相近職責，完整導入邊際收益低；每個供應商授權／延遲／歷史修訂仍需驗證 |
| [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot/tree/63e1884a42ea7b729551a5424b4bd6a65af636e0) `63e1884a42ea` | Apache-2.0；2026-10-24；已拆 AutoGen／desktop／equity | `finrobot_autogen/finrobot/agents/workflow.py` 的 assistant、RAG、group／leader；不能把所有子產品當一個穩定 API | 可追溯分析步驟可**重寫**／高成本／P3；完整整合**不採**。根套件 Python>=3.10,<3.12，AutoGen requirements 另釘 pandas2.0.3／numpy1.26.4，與目前環境衝突；財務報告／生成文字不是日線預測證據 |

本輪只借用方法與自行撰寫數學實作，**沒有拷入上游程式碼、安裝套件、下載權重或資料集**。上游完整程式碼、基礎模型、權重、資料及新聞的權利需要分別查核；根目錄 LICENSE 不能替代後四者。後續若實際移植程式，需在移植前保留該固定版本的授權與必要 notices。本 repo 尚未選定自己的正式 LICENSE，本研究沒有替它作授權決定。

## 3. 優先順序與實驗假設

| 候選技術 | 可能改善的項目與機制 | 相容性／資料／運算與風險 | 決定及如何驗證 |
|---|---|---|---|
| Qlib 九因子 | 預測：蠟燭上下影、線性趨勢形狀、量價關係可能補充單純動能 | 直接用已有還原 OHLCV；同 fitter／fold；CPU 小；與既有因子重複、量的分割調整與過擬合風險 | P1／實驗 A；只增加固定九欄，檢查 Brier／log loss／MAE／弱股與配對日期區塊差異 |
| Darts 啟發的殘差 wrapper + 波動率縮放 | 評估／機率區間品質：平靜與動盪時給不同寬度 | 已有 vol20、獨立校準段；不加模型；CPU 很小；相依資料及波動落後可能失準 | P1／實驗 B；同凍結中央與機率，比 coverage／width／proper interval score，另設精確排序 pooled 控制 |
| Freqtrade 啟發的 prefix audit | 工程可靠性：刪掉未來後，過去因子應完全一致 | 可重跑本地 builder；無新資料／套件；只檢查抽樣截點，不能證明全部 PIT | P1／實驗 C；比對列集合、NaN 與數值，測試中注入 `shift(-1)` 作必須被抓到的負控制 |
| Kronos／Chronos 零樣本 | 預測：預訓練路徑分布可能補充樹模型 | 未裝 Torch／Transformers，沒有權重；需還原、calendar、訓練截止與權重授權查核 | P2；無本輪分數。未來固定權重／種子／樣本數，轉同目標後與 current／frequency 比；使用權重訓練期之後的新資料 |
| RD-Agent 有界假設迴圈 | 研究效率：自動提案與失敗留存 | 有可靠 runner 才有用；Agent 本身不產生獨立證據，多重檢驗與 API 成本高 | P2；先保留 protocol、固定 trial budget、所有失敗及唯一未來驗證，禁止自動合併／升級 |
| 新回測引擎 | 策略與效率：大規模成本／參數網格 | 現有 simulator 已有成本／現金／基準；完整移植收益小、依賴與授權成本高 | 暫不替換；先用現有模擬器凍結選單作兩級滑價壓力，與預測指標分開 |

## 4. 可執行原型與共同比較協定

新增檔案：[`open_source_factors.py`](../src/twquant/open_source_factors.py)、[`open_source_intervals.py`](../src/twquant/open_source_intervals.py)、[`open_source_study.py`](../src/twquant/open_source_study.py)、[`research_open_source.json`](../configs/research_open_source.json)，以及三個對應測試檔。既有正式 fitter／特徵 builder 沒有改變。

實際執行指令（`--out` 必須不存在）：

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant.open_source_study \
  --root artifacts/fixed-ten \
  --snapshot 20261105T151537109342Z_b157f58774 \
  --config configs/research_open_source.json \
  --events artifacts/fixed-ten/sources/events.csv \
  --out artifacts/open-source-study-20261114-v1

LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m pytest -q \
  tests/test_open_source_factors.py tests/test_open_source_intervals.py tests/test_open_source_study.py
```

沒有該私人快照的其他使用者，先依 [README](../README.md) 匯入自己有權使用的原始／還原日線，改用其 root／snapshot；外部事件可不指定，但那是另一組資料實驗，不能聲稱重現本表數字。可用 `twquant.demo.make_demo_prices` 與 `run_study` 重現合成工程流程，合成結果必須標示 synthetic。

協定在擬合前寫入 `protocol.json`、`splits.json` 與本輪 `source/`。每次只建立新目錄，逐 fold 寫固定欄位 `execution.jsonl`；保存特徵、模型版本、逐筆核心／轉移預測、fold 表、校準參數、成本決策、淨值、report 及所有檔案 SHA256。未寫模型 binary／current pointer、官方 run／holdout seal／帳本。`source/` 僅封存本地 Python 原始碼，沒有複製憑證或資料商原始檔；所有產物留在被忽略的 `artifacts/`。

本輪固定條件：

- Fugle 唯讀快照 45,907 列、2014-01-02～2026-11-05。原始／還原價格來自同一快照；`reconstructed_eod` 是歷史 20:00 可得假設，並非當年實際交付證明，單一現今還原版本也不能證明歷史修訂可得性。外部事件使用既有 PIT join，不補造新聞；事件檔 SHA 與快照 SHA 見結果摘要。
- 保留 126 個市場日，自 **2026-05-05** 起排除；跨入保留期、或在界線前尚未收件的答案一併遮蔽。這段不開封；較早歷史已被其他研究看過，所以 OOS 仍是探索用途。
- 固定十檔訓練／校準／同儕；0050、1216、2002、2412、IX0001 僅凍結模型推論。指數為點數，絕不納入交易。
- 每 horizon 最近最多八折，每折 63 日測試、63 日校準、至少 252 日訓練；訓練限最近 756 日，embargo 1 日，標籤終點與收件均需成熟；種子 42，沿用 chronological shrinkage，不根據外層測試調參。
- 核心 53 個既有 `portable_all` 欄位，A 臂加九欄。使用相同 `_fit`、訓練列、校準列、測試日曆及答案；既有簡單線性、訓練期上漲頻率／平均簡單報酬、p=.5／零報酬都比較。
- 本研究器對既有 splitter 傳入固定十檔＋0050 的完整觀測日曆，並移除「0050 未來有價才能評分」的相依。這是重跑現有模型演算法的公平基準，不是直接挪用舊 report 的分數；ETF 缺價不會縮小十檔測試分母。全市場資料同時缺失時，觀測日曆仍無法證明那天是否開市。
- h1／h5／h20 各臂期間相同，但不同 h 的切分位置不同，不能將其 Accuracy 互相比作同一任務。h1 測試 2024-05-07～2026-04-30 共 481 日；h5 為 2024-05-17～2026-04-24 共 469 日；h20 為 2024-03-28～2026-04-01 共 487 日。
- 預測損失只在已知答案計算，完整方向稽核另保留全部預期格。三期間各有一筆漏報、兩個未知格（含漏報格），分母分別 4810／4690／4870；核心觀測數 4808／4688／4868。沒有刪除低信心或弱股以達標，覆蓋未達 100% 本身就不能通過正式驗收。

### 實驗 A：Alpha158 子集

新增九欄 `f_oss_*`：body/range、upper/range、lower/range、20 日斜率／當前價、線性趨勢 R²、末點殘差／當前價、價格與 log(volume+1) 相關、正價差總量／絕對價差總量、abs(return)×volume 的 std/mean。只看過去與當日已完成資料；按市場日曆 reindex，缺價不壓縮窗口，不填成零。零區間、零活動與不完整窗口保持未知，由既有訓練期 imputer 處理。

這不是完整 Alpha158，也不等於重現 Qlib 論文。量仍為供應商股數，跨分割的成交量尺度需更嚴格資料查核；固定少量比例因子是控制工程成本，不是證明已解決公司行動問題。

| h | 現有 Accuracy → 加因子 | Brier 現有 → 加因子（低較好） | frequency Brier | MAE 現有 → 加因子 | frequency／零報酬 MAE |
|---|---:|---:|---:|---:|---:|
| 1 | 50.92% → 51.60% | .250447 → .250172 | .250362 | .020601 → .020525 | .019466／.019468 |
| 5 | 55.91% → 56.74% | .258416 → .250122 | .246993 | .055775 → .055904 | .044862／.045384 |
| 20 | 60.39% → 59.68% | .242358 → .243059 | .238137 | .134269 → .133479 | .095709／.099300 |

Accuracy 表為已知答案的分類指標；保守完整分母準確率及所有 252 種五檔組合保存在 report。最弱五檔平均：h1 49.56%→49.69%、h5 53.39%→53.39%、h20 54.50%→57.00%，均未達75%。線性基準 Brier 分別 .276961／.351570／.465578，比它好並不足以證明額外價值；h1 也仍輸給 p=.5 的 .25，報酬 MAE 三期間均輸給簡單基準。

以市場日期為單位的配對 Brier 差（alpha−current），20 日 circular blocks、500 次、seed42：h1 −.000275，95% 描述區間 [−.000781,.000164]；h5 −.008291，[−.017099,−.001610]；h20 +.000703，[−.003066,.004015]。日期平均與觀測平均有微小權重差異。這是多臂、多期間、已看歷史的探索區間，未校正研究選擇，也不是獨立顯著性宣告。h5 只有3/8折改善、4折持平，增益並非穩定逐折出現；三 h 改善折數為4／3／2。

### 實驗 B：固定預測器的波動率縮放區間

`current` 保留原殘差區間；`pooled_exact` 為精確第 `min(n,ceil((n+1)*.8))` 個殘差的控制臂，避免把原程式 `quantile(..., higher)` 的排序差異誤認成波動率效果。`vol_scaled` 用已知 `vol20 × sqrt(h)` 正規化絕對對數殘差，再用同一排名回復每筆寬度。缺波動以實際訓練窗口中位數回退，floor=max(1e-6,0.1×該中位數)，都不能由外部股票或測試資料估計；訓練全未知則使用固定 .01。每個 h 的 sqrt(h) 在校準／回復時相消，不是對跨期波動擴散的實證假設。

| h／臂 | coverage80 | 平均簡單報酬寬度 | interval score80（低較好） |
|---|---:|---:|---:|
| 1 current | 76.85% | .05718 | .10606 |
| 1 pooled_exact | 76.81% | .05694 | .10610 |
| 1 vol_scaled | **78.08%** | .06866 | **.10238** |
| 5 current | 73.17% | .14103 | .26921 |
| 5 pooled_exact | 72.89% | .14026 | .26951 |
| 5 vol_scaled | 73.04% | .16992 | .27044 |
| 20 current | 66.80% | .29650 | .63927 |
| 20 pooled_exact | 66.54% | .29557 | .64014 |
| 20 vol_scaled | 69.99% | .37167 | .63630 |

interval score=`(U−L)+10 max(L−y,0)+10 max(y−U,0)`，同時懲罰過寬與漏覆蓋。h1 平均 score 約改善3.46%，5/8折改善，但寬度增加約20.1%；h5 更寬且分數變差；h20 雖彙總微幅改善，但只有2/8折改善、覆蓋仍不足70%，不能說已解決長期區間。尚未提供區間分數改善的獨立未來顯著性證據。三臂的方向機率、中央報酬與方向分母完全相同，**沒有新增分類或交易優勢**。

### 實驗 C：特徵截斷重算

只對開發期價格重算，不使用保留期答案。2020-02-25、2023-03-23、2025-02-05 三個截點，比對完整歷史與僅保留截點以前資料得到的所有過去列、NaN mask、36 個價格／新增因子欄位；共 **3,374,280** 個值比較，沒有差異，列集合亦一致。測試中另注入 `groupby(ticker).adj_close.shift(-1)`，稽核確實拒絕該因子。

通過的是這三個截點與 builder 的特徵因果一致性。沒有測到的全截點、外部事件發布版本、整個模型訓練過程、真實歷史供料、分割修訂及跨市場延遲不能由此推論安全。相關時間／隔離另由既有 PIT join 測試、模型輸出不受測試答案影響的測試，以及本輪保留期／外部資產干預測試驗證。

### 策略、成本與轉移：與預測分開

使用原 h1 選單、預設雙邊手續費0.1425%、單邊滑價0.1%、股票賣出稅0.3%、ETF0.1%作研究假設，沒有重新聲稱這是即時法定／帳戶費率。次級壓力測試固定所有原選單與資金權重，只將滑價加倍到0.2%，不重選股票。每筆下一開盤進、當日收盤出，5／20日沒有交易淨值。

| h1 模型 | 基本成本後總報酬 | 雙倍滑價 | 基本成本最大回撤 | 換手總量 | 雙邊成交次數 |
|---|---:|---:|---:|---:|---:|
| current | −3.69% | −5.03% | −4.39% | 13.98 | 70 |
| alpha | −3.99% | −5.52% | −10.59% | 15.99 | 80 |

相同起訖可核對端點的被動0050基本成本總報酬約 **+144.70%**；期間有缺估值，現有模擬器把其年化／Sharpe／回撤列為未知，只保留端點總報酬，不能補成完整每日風險序列。策略平均曝險僅1.45%／1.66%，與被動基準風險不同；即便如此，這輪沒有交易增益證據。日線成交假設仍缺市場衝擊、最小單位、撮合與容量。

同一凍結模型的 h1 transfer Brier（current→alpha）：0050 .254515→.254493、1216 .248032→.247728、2002 .249364→.248889、2412 .245865→.245954、IX0001 .254013→.253474。它們沒有參與訓練、校準或選模；小幅變化不作採用條件，IX0001 不交易。其他期間及區間結果逐標的留在本機 report。

## 5. 執行成本、驗證與可重現性

首次執行約 **70.82 秒**，同環境重跑約 **95.70 秒**（同時執行完整測試），紀錄另存 `artifacts/open-source-study-20261114-repeat`。四臂共八份核心／轉移預測 Parquet **逐 byte 相同**；獨立核對通過兩次輸出的170個產物雜湊、76個已讀上游檔案雜湊、標籤成熟界線、完整格分母與原模擬器基本成本重播。每次共48次模型擬合（2臂×3期間×8折），區間控制／縮放不新增模型擬合；CPU模型執行緒依既有 threadpool 限制為1。沒有新增 API 費用、套件、GPU 或付費資料。執行時間是本機牆鐘時間，非性能保證；首輪未量測峰值記憶體。

環境與版本：Python3.14.3、macOS arm64、NumPy2.5.4、Pandas3.1.0、SciPy1.18.1、scikit-learn1.9.1、PyArrow24.0.0。輸入／設定／程式與結果的安全彙總另存 [`open_source_initial_results.json`](research/open_source_initial_results.json)，不包含原始價格、事件內容、憑證、模型或逐筆輸出。

新增10項自動測試涵蓋手算斜率／影線／R²／殘差、缺交易日不壓縮、標籤不入因子、前綴負控制、精確殘差排序／凍結尺度、與既有 fitter 的相同輸出、相同各臂答案、同模型轉移、保留期及外部價格／答案干預、全股票缺日仍在分母，以及滑價壓力確實保留邊際選單與原權重。測試先觀察缺功能失敗，再完成實作。全Python測試628項及前端狀態測試10項通過；工作樹secrets audit無發現，這不是完整Git歷史掃描。獨立Parquet核對時，受限sandbox的PyArrow原生CPU快取探測印出sysctl權限診斷，指令仍exit0、數值與byte核對皆通過；未修改系統權限或套件以消除環境診斷。

遇到的限制明確保留：Torch／Transformers／Kronos／Chronos／LightGBM 均未安裝，權重沒有下載；Kronos／Chronos 零樣本與微調、FinGPT 文本、Agent 因子搜尋、非線性衝擊／多日策略均**未做績效實驗**，沒有編造分數。GitHub API 的授權自動標示不完整（VectorBT／OpenBB）已改以實際 LICENSE 文字判讀。外部下載僅供靜態檢查，沒有執行上游 setup／範例。

## 6. 結論與後續清單

| 證據狀態 | 技術／判斷 | 後續採用門檻 |
|---|---|---|
| 已實驗、有局部正面證據 | h1 波動縮放的區間分數／涵蓋率；截斷稽核的可偵錯能力 | 保留獨立研究模組及 prefix guard；區間先做逐股／高低波動／危機期檢查與新的未來樣本，再考慮正式接入 |
| 已實驗、沒有足夠預測增益 | Alpha 九因子：h5相對現有模型有改善，但仍輸給頻率；h1證據弱，h20變差；策略沒有增益 | 不替換正式模型；一次固定少量因子群消融，只在內層時間區段選擇，外層／新未來驗證，記錄全部試驗數 |
| 已實驗、沒有穩健區間改善 | h5縮放；h20大多數折變差，整體覆蓋仍不足 | 不靠單一彙總 score 推廣至所有 h；研究非對稱／分位數與依日期校準，但不能重複挑既有測試期 |
| 目前無法驗證 | Kronos／Chronos／NeuralForecast 的實際增益 | 隔離環境、固定權重／tokenizer版本及授權、確認預訓練截止，先零樣本低成本抽樣，再以相同全部決策格比較 |
| 值得繼續研究 | Qlib 訓練期 robust scaler／固定十檔截面；RD-Agent有界實驗；provider熔斷與 typed contracts；容量成本 | 每項一個可否證假設、固定 trial budget、保留失敗。先驗證真實歷史資料品質；工程吞吐與模型損失分開量測 |
| 不適合目前完整導入 | 另建 LEAN／Freqtrade／FinRL 全交易平台、大型 Agent 報告系統、VectorBT 完整依賴 | 現有能力已足夠首輪公平研究；只有新增需求及明確效益超過依賴、資料、運算、授權與維護成本才重新評估 |

正式採用任何預測變更前，仍須達到既有[固定十檔協定](FIXED_TEN_PROTOCOL.md)及[方法門檻](METHODOLOGY.md)：可核實公司行動／PIT、完整每日覆蓋、簡單基準優勢、穩定逐股／跨期表現、不可重複調參的新未來測試及真實影子紀錄。任意五檔75%與全50檔要求都沒有因本輪原型而降低；方向、區間、策略及工程成果各自驗收。
