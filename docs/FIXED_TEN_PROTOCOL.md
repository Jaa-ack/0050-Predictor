# 固定十檔訓練與跨標的測試

本專案目前的主要目標是：只用使用者選定的十檔訓練一個共用模型，再預測同十檔、0050、三檔未參與訓練的股票及臺股加權指數。名單固定，不隨最新ETF權重變動；權重欄位不作為模型訓練權重。

| 群組 | 標的 | 用途 |
|---|---|---|
| 固定訓練十檔 | 2330、2454、2317、2308、3711、1303、2383、2881、2891、3037 | 訓練、時間校準、開發期模型選擇、樣本外及保留期檢查 |
| ETF延伸測試 | 0050 | 同一模型，使用0050自身過去行情及固定十檔市場背景推論 |
| 股票延伸測試 | 1216統一、2002中鋼、2412中華電 | 同一模型，不納入訓練、校準或模型選擇 |
| 大盤延伸測試 | IX0001／TAIEX加權價格指數 | 同一模型，以指數點數及價格報酬評分，不模擬下單 |

`src/twquant/universe.py`是訓練名單的唯一程式來源。`evaluation_tickers`可指定未參與訓練的測試股票；更換測試名單不會把它們加入訓練。新增股票需另取得相同契約的完整歷史行情。模型學習各標的自身過去的技術特徵與共同市場資訊，並非只憑十檔當日價格、不使用待測標的歷史就能產生可靠報價。

## 已落實的優化

- 固定十檔限定訓練／校準與同儕因子；市場背景由十檔等權報酬計算，0050僅保留參考日曆及績效基準用途，不作訓練因子，其他股票不參與同儕平均或交易日曆。每折外部測試使用與十檔相同的模型版本與測試日期。
- 三組可轉移特徵：`portable_price`（行情）、`portable_market`（再加美股市場報酬）、`portable_all`（再加FRED）。排除絕對成交量、價格尺度及美股絕對收盤，減少跨標的單位差異；仍需實證驗證泛化。
- `training_window_dates=756`限制最近約三年訓練日期；較早折使用當時已有資料。`None`可作擴張窗口比較。校準區間與訓練／測試依標籤成熟時間隔離。
- `chronological_shrinkage`在校準期前段擬合機率校準器，再用後段選擇向訓練期上漲頻率收縮的比重，候選0／0.25／0.5／0.75／1。內部分界也排除跨界標籤，測試標籤不能選擇比重。
- 報告固定0.5機率、訓練上漲頻率、永遠上漲、預測上漲比例、漲跌召回、混淆矩陣、平衡準確率及可靠度分箱。門檻要求優於機率基準及平衡準確率大於0.5。
- 收盤至收盤的股價模型，與下一開盤至收盤的可成交目標分開。新增直接擬合簡單報酬的算術期望估計；原`expected_return`仍是對數報酬轉換後的中心，不能混稱算術期望。
- 1日策略使用算術估計篩選；同一批選股及名目配置另報成本前績效和成本拖累，不以零成本重選贏家。日線模擬仍假設開盤可成交，未涵蓋排隊及部分成交。
- 成對Brier損失按市場日期、20日循環區塊抽樣，提供探索性95%區間。這不是獨立樣本檢定，也不證明事後設計具備優勢；跨折缺口與區塊選擇限制明列報告。
- 延伸標的只允許影子預測，其結果不計入固定十檔的60日／500筆正式門檻。各標的獨立檢查特徵漂移，外部標的異常不會令全部十檔一同棄權。

## 可重現操作

既有本機驗收資料位於`artifacts/fixed-ten`，舊研究及帳本保留不覆寫。新環境先下載：

```bash
.venv/bin/python -m twquant --root artifacts/fixed-ten ingest --provider fugle --start 2014-01-01 --tickers 2330,2454,2317,2308,3711,1303,2383,2881,2891,3037,0050,1216,2002,2412,IX0001
.venv/bin/python -m twquant --root artifacts/fixed-ten sources sync --plan configs/market_context_plan.json
.venv/bin/python -m twquant --root artifacts/fixed-ten sources export-events
.venv/bin/python -m twquant --root artifacts/fixed-ten compare --config configs/research_fixed_ten.json --events artifacts/fixed-ten/sources/events.csv
```

比較器先排除最後126個0050交易日及跨界未成熟標籤，只用固定十檔開發期的平均Brier相對訓練頻率增益選特徵組；延伸測試分數不參與選擇。結果與逐筆預測存`comparison/`。本次比較是探索性實驗，過去看過的日期不能重新宣稱未接觸。

確認`configs/research_fixed_ten.json`的`feature_profile`符合開發期選擇後，凍結設定：

```bash
.venv/bin/python -m twquant --root artifacts/fixed-ten research --config configs/research_fixed_ten.json --events artifacts/fixed-ten/sources/events.csv
.venv/bin/python -m twquant --root artifacts/fixed-ten holdout
.venv/bin/python -m twquant --root artifacts/fixed-ten serve --port 8000
```

`runs/<run_id>/`包含十檔`oos_predictions.parquet`、`holdout_predictions.parquet`及另外的`transfer_oos_predictions.parquet`、`transfer_holdout_predictions.parquet`。網頁「歷史回測」分開呈現兩期延伸指標，可切換1／5／20日。

每日20:00後使用完整當日行情及最新外部因子，先結算、再研究／校準，最後產生預測：

```bash
.venv/bin/python -m twquant --root artifacts/fixed-ten daily --provider fugle --start 2014-01-01 --tickers 2330,2454,2317,2308,3711,1303,2383,2881,2891,3037,0050,1216,2002,2412,IX0001 --config configs/research_fixed_ten.json --events artifacts/fixed-ten/sources/events.csv --paper
```

執行前需重新同步並匯出外部因子。每次預測15標的×3期間＝45筆；到期附加答案，不改寫原始預測。只用現有已訓練模型時，另用`predict --as-of <含時區的當日20:00後時間> --paper --events ... --tickers ...`。`configs/research_execution.json`只研究1日開盤至收盤任務，請用隔離工作目錄，不用來產生收盤基準的股價區間。

## 尚缺環節

主要固定名單目標不要求動態成分權重。若未來改研究「各歷史時點的前十大策略」，才另需歷史ETF成分、公告及生效權重；固定名單的事後選樣限制仍須揭露。

行情已有長期資料，但最新向後還原及20:00重建可用時間不是原始PIT版本證據。真實歷史財報／籌碼／新聞發布與更正版本、財務TTM及單位轉換、夜盤合約對齊仍不完整。新增因子先證明資料時點與獨立增益。

5／20日目前評估預測品質，尚無逐日重疊部位及資金占用回測；條件分位數、個股超額報酬、產業適應和完整成本／容量壓力仍待實作。富邦獨立相容SDK環境已可驗證，但主研究Python的自動橋接尚未完成。

沒有自動安裝排程。增量行情與還原版本重建、獨立重訓排程、跨程序互斥、告警、常駐主機及備份仍需營運工作。新模型未來至少60交易日／500筆及時成熟的十檔預測，以及成本後穩定優勢，均不能用歷史重播代替。
