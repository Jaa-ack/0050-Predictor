# Trader Skills 整合評估

評估日期：2026-11-12。第一批整合、來源檢查及歷史對照已執行；以下計畫在比較結果產生前固定。結論：可解釋篩選與可替換紙上風控有工程價值，但目前沒有穩健的模型／策略增益證據，不啟用至正式預測。

## 第一階段計畫

1. 沿用 `PriceStore`、`build_features`、`reserve_development`、`run_research` 的資料、目標與成熟標籤切分。既有資料取得、正式模型、帳本及網站不變。
2. 修改移植 `trader-screener/screen.py` 的比較、理由與加權百分位排名；加上有限數值、型別、規則與權重驗證。保留全部通過、未通過及缺失紀錄。
3. 修改移植 `trader-scanner/scan.py::find_setups` 的五種日線條件；使用還原收盤、主要市場日曆及既有 0–1 RSI，不採用美股代號、資料下載或當沖時段。
4. 參考兩版 Risk 重寫紙上目標權重檢查：單股、總曝險、現金、單股／組合波動度與全額本金風險。缺失／非有限值拒絕通過；不模擬停損保證成交。
5. 獨立 CLI 執行 A 原模型、B 加入五種訊號、C 原模型加篩選、D 在 C 上加固定等權配置及風控，另比較純規則策略 R。C/D 不刪除任何方向預測。預設規則為 20 日均線乖離大於 0、相對量至少 1；不根據比較結果調參。
6. 保留最後 126 日；各模型沿用相同原設定與 1/5/20 日標籤。成本／h1 開盤至收盤投資組合沿用原模擬器；5/20 日只比較預測品質，不複利重疊標籤。
7. 只用本機既有歷史快照及合成測試。結果是已檢視歷史的探索比較；不開封最終保留期、不選新正式模型、不宣稱 75% 已達成。

## 固定來源

| Repository | Commit | 授權 |
|---|---|---|
| [trader-skill](https://github.com/jakeschincariol/trader-skill/tree/5189e79b9a480755a3733ac9713f5353981dbe18) | `5189e79b9a480755a3733ac9713f5353981dbe18` | MIT，Copyright (c) 2026 Jake Schincariol |
| [day-trader-skill](https://github.com/jakeschincariol/day-trader-skill/tree/3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd) | `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` | MIT，Copyright (c) 2026 Jake Schincariol |

來源在 `/private/tmp/0050-trader-skill` 與 `/private/tmp/0050-day-trader-skill` 隔離檢查，沒有安裝 Skills 或修改全域設定。

## 1. 現有系統分析

本次從實際程式、測試及設定確認下列介面，沒有新增資料取得流程。

| 環節 | 實作與保留理由 |
|---|---|
| 資料契約 | `data.validate_prices` 驗證 `ticker,date,open,high,low,close,adj_close,volume,available_at`，保存字串代號與時區、排除重複價格；指數可保留未知成交量。`PriceStore` 保存不可覆寫 Parquet／manifest／SHA256，經 DuckDB 讀取與再次驗證。 |
| 因子 | `features.build_features` 依主要市場日曆計算還原報酬、波動、乖離、0–1 RSI、先前成交量均值的相對量、beta、固定十檔市場／同儕代理。`point_in_time_join` 使用公布／收件／修訂／到期時間，模型只能讀 `f_` 欄位。已有功能不重寫。 |
| 預測目標 | 1／5／20 個市場日的收盤報酬，另有開盤至未來收盤標籤。下一開盤、答案與可成交性是評估欄位，不能成為篩選條件。 |
| 模型 | `research._fit` 共用 Ridge／直方圖提升樹回歸及邏輯／提升樹分類，固定混合，獨立時序機率校準、殘差區間與算術報酬估計。研究特徵 profile、訓練窗口與校準政策沿用設定。 |
| 切分 | `temporal_splits` 以市場日期分組，按標籤成熟與實際可得時間 purge／embargo；`reserve_development` 排除末尾保留期及跨界答案。`holdout` 另行開封，本次沒有呼叫。 |
| 投資組合 | `simulate_portfolio` 只做 h1 次日開盤買進、當日收盤賣出，長倉、含雙邊佣金／滑價與賣出稅，最多五檔、單股上限、資金含買入費用；先選股再核對未來是否成交，失敗資金留現金，不以事後資料遞補。已有等權配置與被動／等權基準。 |
| 日常系統 | `workflows` 管理版本、成熟重訓與正式門檻；`ledger` 保存不可覆寫簽發／到期紀錄。CLI／FastAPI 與固定 allowlist 的單工作介面分離；排程是未安裝範例。網站託管內容僅為合成快照。 |
| 既有方向研究 | `direction`／`direction_models` 已有巢狀時序選模、固定十檔共享模型與外部 transfer；`direction_audit` 以完整日期／股票分母計算缺報與全部 252 種五檔組合。本次沿用 audit，不擴大既有候選搜尋。 |

訓練／校準／研究選擇固定為 `2330,2454,2317,2308,3711,1303,2383,2881,2891,3037`。`0050,1216,2002,2412,IX0001` 僅對同一模型推論；本次特徵、排名與風控不以外部標的建立同儕上下文，指數沒有可交易部位。

## 2. 外部功能盤點與移植決策

以下 T 指前述固定 SHA 的 trader-skill，D 指固定 SHA 的 day-trader-skill。每個候選的 `SKILL.md`、計算程式、CLI、設定及對應測試／fixture 都已檢查。選入程式使用標準庫，現有專案僅沿用既有 pandas／NumPy／scikit-learn；沒有新增執行期依賴。

| 來源／原始檔案 | 實際功能 | 專案現況 | 潛在價值 | 難度 | 主要風險 | 建議與本次結果 | 驗證方式 |
|---|---|---|---|---|---|---|---|
| T `trader-screener/screen.py`、`rules.example.json`、`tests/test_screener.py` | 九種條件比較、每條通過／失敗原因、缺失 fail／skip、加權 midrank、JSON／CSV | 有特徵，沒有通用規則與解釋介面 | 獨立檢查既有因子、可讀失敗原因 | 低：不需資料接入 | NaN／布林／錯誤型別、負／非有限權重、跨日期或外部標的排名污染 | **修改移植**比較、解釋與排名；強化契約，無隱含幣別／百分比轉換 | 正常／缺失／極值、全部 operator、單例／同分、日期分組與代號前導零；C 對照 |
| D `trader-scanner/scan.py::find_setups`、`rsi`、`tests/test_scanner.py` | 20 日收盤新高、bull flag、重站 MA20、超賣、超賣反彈；另有 5 分鐘 ORB | 已有 RSI／均線／相對量／動能；缺少上述五個顯式旗標 | 候選特徵、獨立規則與後處理可分開比較 | 中：須處理還原、收件、缺失交易日 | 上游刪掉 null 後壓縮日數；美股時段／流動性門檻不適用；RSI 初始化不同 | **修改移植五個日線公式**，使用原 RSI；不移植網路／美股 universe／ORB | 未來擾動、分割、晚收件、停牌、歷史不足、零量／平盤；B／R 分別驗證 |
| T／D `trader-risk/check.py`、兩版 `tests/test_risk.py` | 共用逐筆模擬現金、單筆／單股限制、重複與數量限制；D 加 stop、R/R、日風險、進場時間與出場豁免 | 已有 h1 資金與單股 cap，沒有獨立可解釋組合 gate | 可檢查額外曝險、現金與波動限制 | 中：broker 訂單／狀態不能直接沿用 | 兩版接受 NaN 金額；D 缺少既有最大損失只警告不計入日風險；stop 不能保證成交；美元／美股驗證耦合 | **參考重寫**紙上目標權重 PASS／BLOCK，不複製訂單程式 | 邊界相等／超限、未知風險、非有限值、現金空組合、指數別名；D 與有部位的合成整合測試 |
| T `trader-budget/size.py::allocate`；D `size.py::size_trade`／`size`，兩版 budget 設定／測試 | T 按目標權重反覆 cap 與重分配、最小單、取整／碎股；D 新增按 stop 風險、現金與較高新預設 cap 的單筆 bracket sizing | 原模擬器已有固定等權、cap 與含費用現金計算 | 未來多日組合可比較分數／反波動權重 | 中：要先定義多日持倉／台股股數 | 非有限輸入、忽略成本／價格變化、依賴美股 fractional／bracket | **延後、僅比較**；D 使用原等權加固定現金上限，沒有複製 budget | 目前保留原費用／資金回歸；未執行分數／反波動配置比較 |
| T `trader-rebalance/rebalance.py::capped_targets`／`plan`、fixtures／測試 | 權重偏離百分點、超過 band 才回目標、先賣後買、現金不足同比縮買 | h1 每晚清倉，沒有跨日權重漂移 | 多日策略建立後才有增量價值 | 高：需持倉／成交／稅費狀態 | 上游未建完整含成本 NAV；照搬會重複配置或忽略台股股數限制 | **延後參考**，未移植、未比較換手／band | 來源離線測試通過；本次多日再平衡標為未執行 |
| D `trader-timemachine/timemachine.py::build_pool`／`simulate`／`history_replay`、fixture／測試 | 前 60 日完整 78 根 5 分鐘 session，按同時段 ±2 根獨立抽 bar，固定 seed；stop 優先、歷史 ORB 重播、R／百分位 | 已有日線 walk-forward、成本帳與日期區塊損失抽樣 | 固定 seed 與假設揭露值得維持；不能提供新日線方向機率 | 高：必須改寫抽樣與成交假設 | 短歷史、破壞序列／跨股相依、跨 stop gap 仍算 -1R、未計成本、同根先後不明；條件模擬不是真實上漲機率 | **不採用計算模型**；保留現有回測／區塊診斷，沒有 Monte Carlo 漲跌機率 | 47 個來源測試僅證明 fixture 重現；本次沒有 5 分鐘／未來機率實驗 |
| D `trader-entry/entry.py::evaluate`、`trader-exit/exit.py::compute`／`atr_of`、對應 SKILL／測試 | 已完成 bar 突破確認、waiting／triggered／expired、R 倍目標、價差／ATR／區間 stop | 決策時間與持有終點已固定；缺分鐘級成交證據 | 未來明確定義出場策略時可參考狀態機 | 中至高 | 把當沖確認當日線可得資訊、stop fill 與盤中先後無法從日線得知 | **僅研究、未複製**；不移植整套流程 | 來源 39／14 測試通過；日線出場改造未執行 |

`trader-analyst`、`trader-news`、`trader-alerts` 仍屬後續候選；它們需要的 SEC／Yahoo／新聞或常駐監控沒有接入。本次排除 broker、pelosi、copycat、tax，沒有交易委託或任何全域 Skills 安裝。

### 實際重現的上游缺陷

- Screener 將 `NaN != 0` 判為通過；`check_rules` 也沒有驗證有限門檻或正權重。兩版 Risk 的 `_f` 只轉 float，`notional='NaN'` 可取得 **PASS**。本次隔離探針已重現，沒有執行網路或下單。
- D Risk 對既有交易缺少 `max_loss` 只加入警告，可能低估日風險。新版採未知風險 BLOCK，不沿用帳本推測。
- 上游 RSI 使用最初 14 個差分均值作 Wilder seed，平盤無 loss 回 100；原專案使用 EWM seed、0–1 尺度、全平盤 0.5。保留原定義，避免默默改變 baseline。
- 上游各自以逐 bar／日資料字典覆蓋重複值，適合其 endpoint 但不是研究資料契約。本版拒絕重複／反向日期，以完整市場日曆留出缺口。

## 3. 實際修改與數學定義

| 檔案 | 修改 |
|---|---|
| `src/twquant/skill_screener.py` | 由 T 比較／midrank 邏輯修改移植；一次只處理一個截面，完整結果包含每條 PASS／FAIL／MISSING／INVALID、分數及排名缺口。NaN 可顯式 skip，無限值／錯誤數值永不因 skip 放行。排名只按有值部分的正權重正規化，同分取 midrank，單例 0.5。 |
| `src/twquant/skill_signals.py` | 由 D 五個日線條件修改移植。收盤新高為 `c[t] > max(c[t-20:t])`；flag 為 `c[t-5]/c[t-15]-1 > .10` 且最近五日收盤範圍除以今日價 `< .05`；reclaim 為昨日收盤低於昨日 MA20、今日高於今日 MA20；超賣為原 RSI `< .30`；反彈為昨日 RSI `< .30` 且今日價高於昨日。 |
| `src/twquant/skill_risk.py` | 獨立重寫原子式目標組合檢查；所有投資權重／風險必須有限、非負，限制單股、總投入、現金、單股波動及 `sum(weight * sigma)` 完全正相關歷史波動界。只允許本專案明確宣告的股票／ETF，未知代號與指數別名 BLOCK。 |
| `src/twquant/research.py` | 抽出原選股邏輯供 D 共用；新增可選 `decisions`，完整覆蓋 h1 date/ticker，嚴格布林 eligible、有限 weight，拒絕額外／答案欄位。預設 `decisions=None` 保持原始數值；`rule_only=True` 可獨立按規則排序，不讀模型機率／預期報酬。 |
| `src/twquant/skill_integration.py` | 相同既有切分的 A/B 與共享原預測的 C/D、獨立 R；保留逐筆預測／transfer／切分／曲線／檢查、先寫 protocol、18 項產物 checksum，禁止覆寫已存在輸出目錄。另報 AUC／校準誤差、各 fold、決策前 20 日市場正負狀態及成對 Brier 日期區塊抽樣。 |
| `src/twquant/cli.py`、`configs/skill_screen.json`、`configs/skill_risk.json` | 新增離線 `screen` 與研究用 `skill-study`，沒有 API／網頁工作 allowlist 或排程變更。 |
| `tests/test_skill_*.py` | 篩選、訊號、風控、可成交模擬、整合與 CLI 的自動測試，包含原結果不變、外部／保留答案隔離、有部位風控阻擋與固定 seed 重現。 |
| `docs/licenses/TRADER_SKILLS_MIT.txt`、`MANIFEST.in`、`pyproject.toml` | 保留完整 MIT／著作權，sdist 與 wheel 都包含此第三方聲明；沒有替本專案選定整體 LICENSE。 |

訊號只能讀 `build_features` 已可見的還原收盤／RSI。20 日新高與 reclaim 需要 21 個連續完整市場日，flag 需要 16 日，超賣／反彈至少 15／16 日。缺失／晚收件不壓縮窗口，不夠歷史、算術溢位保留 NaN；不以 AI 生成數值。

風控權重是含買入費用的 NAV 投入比例。D 每筆預先使用 `min(原等權, risk單股cap, 總曝險/最多檔數, 可用現金/最多檔數, 本金風險上限/最多檔數)`，不把少數剩餘標的重新放大。預設最多五檔，每檔 0.16、總投入最多 0.8；任一組合限制不符即當日整體留現金。`max_capital_at_risk` 是投入本金全損情境上限，**不是可保證的 stop／單日損失或 VaR**；本次沒有實作停損成交。

## 4. 啟用、停用與重現

所有新增功能預設不進入 `research`、`predict`、`daily`、方向候選搜尋或網站。呼叫下列獨立指令才啟用；停用即繼續原指令，或模擬時不傳 `decisions`。研究完成不會寫 `current.json`／`champion.json`、模型檔或預測帳本。

```bash
# 已有特徵截面的離線篩選。數值採專案原單位，代號為字串。
.venv/bin/python -m twquant screen /path/existing-features.csv \
  --rules configs/skill_screen.json --out artifacts/screen-result.json
# --out 亦支援 CSV，兩種格式均包含失敗／缺失列。

# 只讀既有快照；輸出目錄必須尚不存在。以下是本次實際歷史比較。
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/fixed-ten skill-study \
  --snapshot 20261105T151537109342Z_b157f58774 \
  --config configs/research.json --rules configs/skill_screen.json \
  --risk-limits configs/skill_risk.json --holdout-dates 126 \
  --out artifacts/skill-integration-20261112/historical-final
```

再執行時使用新的 `--out` 或省略該參數，由 CLI 建立獨立 UUID 目錄。只有確實在同一截止時間可得的既有事件才傳 `--events`；本次沒有使用外部事件或取得新行情。

Python 可直接使用 `screen(feature_cross_section, spec)`、`add_daily_signals(frame, market_calendar)`、`check_target_weights(weights, volatility, limits)`。Screener 單次輸入必須每個 ticker 一筆；歷史資料先按 date 分組，external transfer 另組，不能把跨日或外部標的放進訓練排名。完整指標快照可由既有 `PriceStore.read_prices` → `build_features` 取得，不需要另一套擷取系統。

## 5. 實驗設計與結果

本次固定 `configs/research.json`：25 個 portable_all 原特徵；B 另加五個 0/1 訊號。最少訓練 252 日、756 日上限、63 日校準／測試、每個 horizon 最近五折、embargo 1、seed 42、chronological_shrinkage。原問題仍是收盤報酬，不因策略表現改成其他目標。保留期起於 **2026-05-05**，本次沒有開封。

資料是既有 45,907 列、2014-01-02 至 2026-11-05 的十五資產快照；可得時間標記 `reconstructed_eod`，不是觀測到的歷史 PIT 或 live 紀錄。外部五個目標使用同 fold／同模型 transfer，沒有參與 fit、校準、排名或選擇。所有結果屬已檢視歷史的探索性比較。

| 版本 | 定義 | 預測／策略範圍 | 執行情形 |
|---|---|---|---|
| A | 原系統、原特徵 | 1/5/20 日預測；h1 原策略 | 已執行，另以修改前 `9aaf6e8` 的研究程式完整重跑，逐筆預測／原模擬曲線及指標完全一致 |
| B | A 模型加入五個日線訊號 | 同一資料／切分／目標，另訓練共享模型 | 已執行 |
| C | A 預測＋MA20 乖離 >0、相對量 >=1 | 方向／機率分數完整沿用 A；只限制 h1 入選 | 已執行，篩選 2,858 列中 811 列通過，但與原交易條件的交集為 0 |
| D | C＋固定現金／權重與波動風控 | 完整方向分數沿用 A；限制 h1 配置 | 已執行流程，但 C 無部位，無法量測風控增益；合成有部位測試另外驗證 BLOCK |
| R | 20 日收盤新高獨立規則 | 無模型機率門檻，等權、同分依 ticker；只比較 h1 策略 | 已執行，不將旗標當分類機率或模型準確率 |

各 horizon 保持 A/B 相同日期與樣本；沿用原框架，horizon 之間測試日期可以不同。h1／h5／h20 的測試範圍分別為 2025-02-18–2026-04-30、2025-03-03–2026-04-24、2025-01-03–2026-04-01。沒有把 5/20 日重疊報酬複利為策略 NAV。

### 預測品質

下表 Accuracy 只表示原評估器有答案的樣本，不能用來取代下一張完整覆蓋表。MAE／RMSE 單位為 simple return。

| Horizon | 樣本數 | Accuracy A → B | Brier A → B | ROC-AUC A → B | MAE A → B | RMSE A → B | Rank IC A → B |
|---|---:|---|---|---|---|---|---|
| 1 | 2,858 | 50.455% → 50.805% | .249493 → .249211 | .518630 → .516670 | .021540 → .021538 | .030870 → .030882 | .015646 → .016208 |
| 5 | 2,698 | 59.970% → 59.970% | .243309 → .243807 | .502591 → .503043 | .053689 → .053661 | .076063 → .076026 | -.003472 → -.002835 |
| 20 | 2,878 | 55.142% → 54.309% | .248023 → .250643 | .453027 → .452257 | .122028 → .121804 | .170036 → .169789 | .000979 → .007723 |

以市場日期而非股票列配對重抽，區塊 20 日、500 次、seed 42，B−A Brier 差的 95% 描述性區間：

| Horizon | 平均日期差 | 區間 |
|---|---:|---|
| 1 | -.00028192 | [-.00109192, .00028443] |
| 5 | .00049788 | [-.00162746, .00224902] |
| 20 | .00262982 | [-.00025582, .00655865] |

三者均跨零，不能宣稱新特徵改善機率。h20 五折 Accuracy A 為 31.11／19.28／76.47／77.62／69.35%，B 為 31.11／14.74／76.47／77.62／69.35%，並未修復弱時段。按決策前 20 日市場報酬分群，h20 非負群 Brier 從 .237195 惡化至 .241032，負群從 .285890 降至 .284250，沒有一致狀態增益。完整可靠度分箱、ECE、各 fold／狀態與 transfer 指標留在本機 report。

### 完整每日覆蓋：缺報與弱股保留在分母

| Horizon | 應評估日數 | 覆蓋率 A/B | 缺報筆數 | 完整分母 Accuracy A → B | 最弱單股 A → B | 最差五檔平均 A → B |
|---|---:|---:|---:|---|---|---|
| 1 | 292 | 97.877% | 62 | 49.384% → 49.726% | 44.521% → 45.548% | 47.671% → 48.151% |
| 5 | 280 | 96.357% | 102 | 57.786% → 57.786% | 48.929% → 48.929% | 52.286% → 52.286% |
| 20 | 298 | 96.577% | 102 | 53.255% → 52.450% | 38.591% → 36.577% | 46.913% → 45.973% |

原框架對缺少基準答案的日期會沒有模型評估列；額外 audit 將這些應有日期與缺股列重新納入分母。因此上表比有答案 Accuracy 更低。C/D 沒有刪掉低信心、未入選或不交易日，完整 audit 與 A 相同；所有 252 種固定十檔五檔組合保留在 report。**沒有達到完整每日／任意五檔 75% 目標。**

### 策略品質與成本

佣金每邊 .1425%、滑價每邊 .1%、股票賣出稅 .3%、ETF 賣出稅 .1%。各版本同樣的 286 個 h1 觀測日；不成交或無訊號資金留現金，沒有追溯替補。

| 版本 | 成本後累計報酬 | 最大回撤 | 原模擬器觀測日 Sharpe | 買賣單數 | 總換手／NAV | 平均曝險 |
|---|---:|---:|---:|---:|---:|---:|
| A | 4.305% | -6.508% | .438 | 102 | 20.478 | 3.558% |
| B | 4.520% | -6.508% | .457 | 100 | 20.080 | 3.488% |
| C | 0% | 0% | 0 | 0 | 0 | 0% |
| D | 0% | 0% | 0 | 0 | 0 | 0% |
| R | -50.138% | -50.138% | -4.488 | 1,008 | 201.218 | 35.159% |

同起訖的被動 0050 端點成本後報酬為 **94.408%**，A/B 的累計報酬差分別為 **-90.103／-89.887 個百分點**。此比較有曝險與持有模式差異；沒有市場曝險調整後 alpha。測試日期有缺口，原模擬器正確撤回被動基準的年化／Sharpe／回撤，只保留端點總報酬；表列策略 Sharpe 沿用原觀測日定義，也不能視為完整連續市場日風險估計。

不能以 C/D 的零回撤宣稱風控有效：它們完全沒有投資。獨立重新計算 C 的原模型門檻、成本與兩條規則交集也為零，排除了篩選器輸出錯位的解釋。本次不調整門檻。R 在此 **h1 平倉／成本假設**下很差，不支持繼續擴大該策略；它不構成對所有突破策略或其他持有期的結論。

## 6. 測試與驗證紀錄

以失敗測試開始，依序實作篩選、訊號、風控、投資組合 overlay、整合與 CLI。來源測試均在隔離目錄離線執行：

| 來源 | 指令模式／模組 | 通過數 |
|---|---|---:|
| T | `.venv/bin/python -m unittest discover -s /private/tmp/0050-trader-skill/tests -p 'test_<module>.py'`；screener／risk／budget／rebalance | 10／17／18／10，共 55 |
| D | 同指令改用 day-trader 路徑；scanner／risk／timemachine／entry／exit／budget | 36／43／47／39／14／31，共 210 |

來源共 265 項通過，仍能用上列 NaN 探針重現缺陷，故沒有用來源測試取代本地驗證。

本地檢查指令：

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m pytest -q
node --test tests/web_state.test.mjs
.venv/bin/python -m twquant secrets audit
.venv/bin/python -m build --outdir /private/tmp/0050-skill-integration-dist
git diff --check
```

第一輪完整 Python 為 468 passed；補上有部位 D gate 與指數別名／未知資產回歸後，再次完整驗證，最終數量見 [VERIFICATION.md](VERIFICATION.md)。Node 10 項通過。工作樹 secrets audit 已通過，提交前另對 staged 集合執行。wheel／sdist 成功建立，MIT 聲明均存在，檔名檢查沒有私人資料／runtime 檔案。

新增測試涵蓋正常／空資料、None／NaN／inf／布林／非數字、極端有限排名權重與價格比值、全平盤／零量、短歷史、遺漏／晚收件、重複／反向排序、不合法參數、相等與超限風控、無交易、無法成交不遞補、含費用權重、規則策略不讀模型／未來答案、保留期擾動、外部標的不影響訓練／篩選，以及相同 seed 成對診斷重現。

開發中修正並保留回歸：權重正規化僅按有值 rank parts，避免極端權重下溢；溢位價格比值不形成假 flag；未滿期望區塊長度的樣本不提供信賴區間；未知 ticker／TAIEX 別名不能因既有 ticker 分類器的預設 equity 而取得可交易許可。部分 Parquet 讀取會在 sandbox 印出 Arrow CPU `sysctlbyname` 權限診斷，未改變計算或測試結果。

數值環境：Python 3.14.3、NumPy 2.5.4、pandas 3.1.0、scikit-learn 1.9.1、PyArrow 24.0.0。來源／設定／資料 hash 與原始快照 ID 留在 protocol。首次完整 A/B 擬合約 8.24／7.89 秒，整體約 23.2 秒；這是同機單次量測，包含其他測試並行負載，不代表五個旗標讓訓練加速。維護增加四個小模組、兩個獨立指令與對應測試，沒有新服務或依賴。

完整實驗與逐筆資料僅保存在忽略的 `artifacts/skill-integration-20261112/`，不提交／發布供應商資料。`historical-final/` 是完成指數 guard 後的最終程式重跑；早先 `historical/` 保留作追溯。修改前研究程式的精確重跑與 18 個產物 checksum 檢查記錄另存在本機 `validation/`。

## 7. 已知限制與後續優先順序

1. **先保持正式系統原樣。** B 只作候選特徵，沒有統計穩健增益；C/D 無交易，不能推論配置／風控績效。模型啟用、holdout 與連續影子門檻不變。
2. **停止擴大目前 R 的 h1 策略及美股當沖整包移植。** 不投入 ORB、5 分鐘獨立 bar Monte Carlo、券商 ticket／PDT／美元規則。固定種子不能補救錯誤的資料頻率、成本或相依性假設。
3. **保留篩選器及風控作可測的研究工具。** PASS 表示符合明確條件，不能替代上漲機率或使用者交易授權。現有份額模擬沒有整股／零股、漲跌停排隊、容量或停損滑價。
4. **日後如比較新規則，先另固定協定及新驗證期。** 不在這次樣本反覆調參後宣稱獨立；區塊重抽、各市場狀態、當期固定股票池存活偏誤及歷史還原版本限制仍存在。
5. **Budget／rebalance 延後。** 先有正確的 5/20 日重疊持倉與含成本 NAV，才比較等權／模型分數／波動調整、不同 band 的換手與成本。這些版本本次未執行。
6. **沒有新增取數、排程、監控或交易。** 沒有重新開封最終保留期，沒有把歷史回放當成實際逐日績效，也沒有發布原始資料或變更託管合成網站。
