# 任意五檔方向準確率研究

目標是研究未來收盤相對決策日收盤的漲跌方向，分別檢查 1、5、20 個交易日。零報酬歸入「未上漲」，與原研究引擎一致。75% 是待驗證的研究目標，程式與合成情境通過不代表真實市場已達標。

## 評分範圍

訓練仍只使用固定十檔：2330、2454、2317、2308、3711、1303、2383、2881、2891、3037。預設驗收這十檔的全部252種五檔組合；加入`--membership`後，另驗收完整當期持股名單的全部五檔組合。當期名單**不代表過去各期0050成分股**，固定名單及當期名單的事後選樣限制仍存在。

主要條件要求十檔**每一檔**的準確率均至少 75%，同時報告各五檔組合的平均準確率與最差組合。這比只要求某個五檔組合的合計準確率更嚴格。每組「五檔在同一天全對」另列 `all_five_correct_rate`，其分母是決策日期，不能與逐檔準確率互換。

漏報、棄權、未成熟或缺失的答案都留在共同日期網格，不能藉刪除樣本提高準確率。整個股票面板缺漏的日期亦保留 0050 參考日曆的分母。`coverage` 必須為 100%、至少 126 個測試日期，各檔還須有兩種方向的答案、平衡準確率超過 0.5，並勝過該檔永遠上漲／永遠下跌的常數方向，`empirical_target_met` 才會通過。只有平均準確率高並不足夠；[平衡準確率的官方定義](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.balanced_accuracy_score.html)可協助解讀單一方向偏差。

95% 區間採共同市場日期的循環區塊 bootstrap，統計量是「十檔中最低準確率」，區塊至少涵蓋預測期間，預設 20 日。它保留同日股票相依性，但日期缺口、區塊長度及歷史選模仍限制推論，不能當成未來準確率保證。

## 架構與選模

`twquant.direction` 將方向分類研究與原本的報酬／交易模擬引擎分開，透過 `search_direction_models` 與 `audit_five_stock_accuracy` 產生可重現的本機研究成果。它不切換正式模型、發出預測、啟用排程或更改交易行為。

預先宣告八組候選：訓練上漲頻率、價格特徵的 252／756 日邏輯迴歸、加入外部特徵的 756 日邏輯迴歸、價格／全部特徵的 756 日梯度提升、756 日 Extra Trees 及 Random Forest。各模型仍是十檔共用模型；日期窗口以候選自身設定為準，不使用其他標的訓練。

每折依序使用訓練、校準、測試區間，沿用標籤成熟時間的 purge 與 embargo。補值及標準化僅擬合訓練資料；其後校準期比較模型與五個門檻（0.4、0.45、0.5、0.55、0.6），按最差單檔準確率、整體平衡準確率、Brier、接近 0.5 的程度排序，模型同分優先採宣告順序。選定之後才評分外層測試。這是時序的巢狀選擇；[官方選模說明](https://scikit-learn.org/stable/auto_examples/model_selection/plot_nested_cross_validation_iris.html)解釋為何應將選擇與評分資料分開。

外部 0050、1216、2002、2412、IX0001 使用每折同一個凍結模型與門檻，另存延伸預測和診斷，完全不參與候選排名。加權指數只評方向，不模擬下單。

最後 126 個 0050 日期和跨界未成熟答案先排除。本輪歷史期間先前已被研究檢視，不能重新宣稱未接觸保留期。`holdout_opened=false` 只表示此命令沒有打開最後預留區間，不代表其他研究從未看過該期間。

## 執行與成果

已有本機完整行情與外部事件時：

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/fixed-ten direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv
```

設定預先固定八個外層測試折，每折最多 252 日，分別評 1／5／20 日。八模型乘五門檻、三期間、八折，合計 960 次校準候選比較，不能描述成 960 次獨立市場驗證。每次命令建立獨立的 `direction/<run_id>/`，不覆寫舊成果：

- `protocol.json`：計算前保存的候選、設定、門檻、開發資料雜湊與來源程式雜湊。
- `predictions.parquet`：十檔逐筆外層預測、門檻、模型版本及答案。
- `transfer_predictions.parquet`：同一凍結模型的外部標的預測。
- `report.json`：每折選擇、全部候選校準分數、逐檔及 252 組結果。
- `data_provenance.json`：行情快照識別碼、證據種類及可用時間依據。

模型版本包含程式、特徵、模型設定、訓練與校準內容及門檻。原始／提供者資料、實際研究數值、模型檔及帳本繼續只留忽略的本機成果中，不上傳 Git。原有研究流程與帳本不受新命令改寫。

## 模擬尚未取得的資訊

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant direction-simulate --sessions 1200 --seeds 17,29,43
```

預設輸出到隔離的 `artifacts/direction-synthetic/direction/<run_id>/`。情境預先固定，保留每個種子，不重抽直到達標。每個種子各有 493 個樣本外決策日期；此合成測試**僅研究下一日方向**，不延伸宣稱 5／20 日也達標。

生成假設為：下一日對數報酬 = 0.01 ×（資訊強度 × 決策時已知的標準常態潛在訊號 + 獨立未來創新）。同日股票共享部分資訊與衝擊。完整、無延遲資訊下，理想符號預測的母體準確率為 `0.5 + atan(strength) / pi`；強度是**假設**，不是從尚未取得的實際資料估計。沒有補造新聞、財報、行情供應商回覆或歷史發布證據。

2026-11-07 的三種子合成驗證結果如下。平均值涵蓋三種子的全部十檔；最低值涵蓋全部種子及每檔股票：

| 假設情境 | 平均整體準確率 | 最低單檔準確率 | 三個種子均達模擬條件 |
|---|---:|---:|---|
| 無額外有效資訊 | 49.4% | 45.0% | 否 |
| 弱資訊，強度 0.5 | 62.9% | 56.8% | 否 |
| 強資訊，強度 3 | 88.9% | 86.2% | 是 |
| 強資訊延遲一天 | 49.8% | 45.0% | 否 |
| 強資訊缺漏一半 | 68.8% | 63.5% | 否 |

這項實驗驗證學習與評分管線能使用已存在的訊號，也顯示延遲與缺漏會破壞它；**它沒有發現真實股票的 75% 方法**。強資訊情境的五檔同日全部正確率也低於逐檔命中率，不可混稱。

`simulated_target_met` 只適用於合成假設；`empirical_target_met` 是該份樣本的完整覆蓋篩選結果。兩者都不能開啟 `real_world_target_verified`。該欄目前固定為 false，必須先取得可驗證的真實 PIT 資料，再有事前凍結、獨立未來期間與足量及時簽發的紀錄，才能另行實作正式認證；本輪不降低既有上線門檻。

## 本輪判斷與下一步

八組方向候選的真實歷史比較未達 75%，也沒有建立比原模型可靠的優勢。因此保留原模型與既有驗收門檻，新流程繼續作研究工具。真正需要解決的是可取得資料是否有穩定的決策前訊息，而不是讓合成標籤更容易預測。

後續候選須登記歷史檢視與試驗總數，針對可驗證發布時間的因子進行消融，與原模型、訓練頻率及常數方向在相同日期比較；新設計若反覆使用本輪結果，仍屬開發研究。真正達標仍需新的獨立未來驗證；若研究全部 0050 成分股，還須在固定訓練宇宙之外取得其完整歷史並作隔離的同模型延伸測試。

## 第二輪：機率選模與市場／個股分離

上一輪有多個20日外層測試折幾乎只預測下跌，與隨後市場方向不同而失準。這是歷史開發診斷，不能憑事後檢視直接選擇一個測試期贏家。第二輪預先比較三種規則，原規則保持為預設：

| `--selection-policy` | 校準期排名 | 分類門檻 |
|---|---|---|
| `worst_stock_accuracy` | 最大化最低單檔準確率，再比較平衡準確率、Brier及接近0.5的程度 | 原五門檻 |
| `pooled_brier` | 最低整體Brier，同分再比最高單檔Brier | 固定0.5 |
| `worst_stock_brier` | 最低的最高單檔Brier，同分再比整體Brier | 固定0.5 |

Brier同時衡量機率品質的多個面向，不能單獨證明校準或分類方向具有優勢；[官方說明](https://scikit-learn.org/stable/modules/calibration.html)解釋其作為proper scoring rule的用途。這兩個候選規則減少分類門檻的選擇自由度，是否改善仍由後續外層測試評分；沒有變更75%目標、完整覆蓋及逐檔方向門檻。

`--candidate-set factor`在原八組候選之外，再加入756日窗口的`factor_ridge`與`factor_hist`。`twquant.direction_models`提供共用的訓練介面：

1. 只用當時已成熟的固定十檔訓練資料，依日期計算十檔平均未來對數報酬，作為共同市場目標；每個市場日期只出現一次。
2. 以各檔報酬減去上述平均形成個股殘差，再以同一個共用殘差模型學習所有股票；推論時將共同市場與個股殘差估計相加。
3. 市場輸入限決策時的固定十檔市場特徵及確實向十檔等值廣播的FRED／美股因子；個別公司或部分填入的外部欄位只留在殘差模型。市場均值不是0050未來報酬，也不使用外部測試股票來計算。
4. 線性／提升樹各自擬合兩個報酬模型。機率分數採訓練殘差尺度的常態近似，**不是正式校準機率**；訓練內殘差可能過度樂觀，限制保留在每折模型資訊。

分離模型只用十檔資料完整的訓練日期；不足十個共同日期或缺少市場特徵時，該候選記為`unavailable`，其固定原因碼留在校準紀錄，其餘候選繼續。最早歷史折確實缺少完整十檔共同訓練日期，不能補造上市前資料。每折另記實際擬合日期與筆數，未執行的候選不計入評分試驗數；若全部候選不可用，命令明確失敗。

可重現比較命令如下，每個命令均建立獨立研究目錄：

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/fixed-ten direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set factor --selection-policy worst_stock_accuracy
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/fixed-ten direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set factor --selection-policy pooled_brier
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/fixed-ten direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set factor --selection-policy worst_stock_brier
```

三個方案都保存並報告，不用外層結果自動切換正式模型。第二輪歷史比較仍未達75%；較高的整體準確率主要出現在大量選用訓練頻率基準的方案，不能混稱個股辨識能力。完整實際分數、逐折方向分布、模型選擇及資料／程式指紋只保存在本機成果。

## 第三輪：完整當期成分股的任意五檔驗收

原始目標的範圍是0050任意五檔成分股，不能用固定十檔內的252組取代完整名單。完整50檔的五檔組合數是`C(50,5)=2,118,760`，本輪增加獨立的成分股驗收層。

[`audit_constituent_accuracy`](../src/twquant/direction_audit.py)保留每檔相同的共同市場日期分母，缺漏／棄權／沒有答案仍不得刪除。若每檔的準確率為`a_i`，五檔平均就是所選五個`a_i`的算術平均；交換其中較高者為較低者不會增加平均，因此**最低五個逐檔準確率的平均，就是全部五檔組合的精確最小值**。它不依賴股票相互獨立。主要門檻仍要求每一檔都至少75%，比只看最差五檔平均更嚴格。

組合不超過1,000時保留完整枚舉；更大宇宙改保存精確最差／最佳五檔、全部逐檔指標、組合總數及`all_combinations_enumerated=false`，不是抽樣或事後挑選。最差「五檔同日全對率」不能從逐檔準確率排序推得；`all_five_correct_rate`僅是已列組合的額外診斷，不能混稱為全部組合的聯合極值。

加入本機持股快照：

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set factor --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json
```

可分別以原三種規則重跑比較，全部仍是開發研究。`--membership`自動將固定十檔之外的成分加入`evaluation_tickers`，不將它們加入訓練、校準、同儕背景或選模。每個決策日期的成分股預測必須有相同模型版本與分類門檻；原0050／三股票／指數診斷繼續另外保存，ETF和指數不計入股票成分驗收。

持股JSON須包含`tickers`、相符的`count`、`as_of`日日期、普通官方`source_url`、含時區的`retrieved_at`及`html_sha256`。入口拒絕少於50檔、重複代號、ETF／指數、含憑證查詢的來源網址及不一致筆數；未知欄位和文件中的`training_tickers`不傳入研究。若同目錄存在`yuanta_0050.html`，會核對其內容雜湊。原始HTML、名單及提供者資料都只留本機，不加入Git。

本機名單來源為[元大官方持股頁](https://www.yuantaetfs.com/product/detail/0050/ratio)，快照保留來源、收件時間和內容雜湊。名單日期是當期持股日期，不是歷史成分證據，也不是原始PIT行情證據；不能把今天取得的名單／還原價格標成過去已知。

完整名單驗收另存於`report.json`的`constituents`及`constituent_predictions.parquet`，`membership_snapshot.json`在擬合前保存經白名單處理的名單中繼資料。固定十檔指標、完整成分股指標及額外延伸標的三者分開，任何成分缺漏都會降低完整名單的覆蓋率，而不是讓缺失股票消失。`real_world_target_verified`仍為false。

方向專用切分使用固定十檔與0050的實際觀測市場日期聯集，外部股票不提供日曆。已觀測日曆的期間終點決定標籤何時成熟，個股方向不再要求0050在未來期間終點也有報價；0050缺價不能阻止仍有完整資訊的個股預測。日曆亦保留整個十檔面板在測試折邊界缺漏的日期。最後預留區間及跨界答案繼續排除，個股自身的未解答案仍保留；原報酬／投資組合研究的預設行為不變。

歷史資料可用起點不齊時，完整長期指標會受到資料缺口影響，不能把供應商最早一筆行情直接等同正式上市日。另有一份次要診斷，事先以「每檔都已有61筆過去行情的第一個共同日期」設定起點，再使用原凍結預測評分；日期不按報酬或準確率挑選。它保留全部50檔、原模型選擇及完整長期報告，仍屬探索性歷史資訊，不能當成新的獨立保留期。

## 第四輪：盤後與盤前資訊截止點

`direction-search --mode postclose`為預設，在錨點交易日20:00形成資訊集合；`--mode preopen`改在下一個實際市場日08:30形成資訊集合。兩者都預測`close(d+h)/close(d)-1`的方向，價格特徵仍以錨點`d`的已完成收盤為基礎，沒有把下一日收盤價放入特徵。盤前1日期間的答案是當個交易日的收盤相對前收盤，**不是從08:30起再經過一整個交易日**。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set factor --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen
```

同一行情快照、名單及未來收盤標籤，分別以三種原有選模規則執行兩種模式，全部六個結果保留。這是比較不同已宣告資訊截止點的研究，不能混稱同一決策時刻的效果，也不因盤前方向較準而假設能以昨收價格成交。外部市場資料的重建可用時間仍不等於原始PIT交付證據。

模式寫入`protocol.json`、`decision_mode`預測欄位、資料來源紀錄及模型版本。研究介面會拒絕主要標的決策時間與模式不符的矩陣；終端盤前時點在沒有下一個已知市場日以前仍為NaT，不猜測開市日。程式介面使用`search_direction_models(..., mode='preopen')`時，須先以相同模式建立特徵。

主要價格特徵與未來標籤可逐筆相同，美股報酬及事件年齡則依較晚截止點更新。事件只按宣告的`available_at <= decision_ts`向後對齊，08:30以後才可得的紀錄不提前使用。固定十檔仍唯一參與擬合、校準及選擇，完整當期50檔與額外延伸標的維持同一模型／門檻測試。

保留期凍結時間另限定由固定十檔與0050決定：改動外部股票的特徵、答案，甚至保留期的外部時間戳，都不會改變核心擬合或預測。原有日曆成熟排除仍保守地要求評分目標終點在保留期錨點之前，兩種模式都不會因盤前已知更多資訊而讀取預留期答案。

六組實際比較已完成，三組盤後控制亦逐筆重現上一輪的數值預測。較晚資訊截止點的一日結果改善，但較長期間沒有一致改善；完整名單與事先定義的資訊就緒診斷均未達75%。逐筆配對、日期區塊重抽、原始報告及全部失敗方案只存本機，沒有把已反覆檢視的歷史稱為新未接觸驗證。

## 第五輪：美股市場資訊消融

`--candidate-set market`保留原有十組，另加入六組`portable_market`候選，共十六組。新增的logistic、hist、extra_trees、random_forest、factor_ridge及factor_hist全部使用756個訓練日期，與原來相同演算法的`portable_all`候選比較；沒有更改模型參數、時間門檻或驗收條件。原`base`八組及`factor`十組仍可分別重現。

`portable_market`保留可攜價格特徵與FMP美股報酬／事件年齡，排除FRED等其他事件與絕對收盤價。這一輪只根據固定十檔的盤前一日結果提出假說，以原有三種規則在訓練／校準區間選擇；外部50檔不決定候選、規則或門檻。所有規則及失敗結果均保存，相同歷史再次使用仍是探索性證據。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set market --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen
```

本機協定事先凍結十六組候選、三種規則、程式／事件摘要、行情快照與上一輪盤前控制；價格錨點、標籤、最後126日期間及完整名單診斷起點均沿用。後續比較亦核對原十組的逐折校準分數，避免把額外候選以外的變動混入結果。

三組實際比較已完成：原十組的全部逐折校準分數與上一輪完全相同，開發矩陣摘要及逐筆答案也相同。新增候選的一日改善有限，較長期間並非一致改善；核心、完整50檔及資訊就緒次要診斷均未達75%，也沒有改善所有最弱股票。未以最好的單一平均取代任意五檔驗收，正式模型與門檻維持原狀。

## 第六輪：決策窗口內新增的海外資訊

使用者於2026-11-07再次確認每天完整預測的驗收標準；低信心日、缺漏及弱股仍計入，不使用選擇性預測的命中率代替任意五檔目標。

`--overseas-window`在原矩陣旁增加全球FMP收盤事件的`f_ext_fmp_<代號>_window_return`及`_window_updated`。前者為決策時刻最後可用的海外價格，除以台股錨點日20:00最後可用的同一海外價格後減一；後者表示決策時點最後可用收件是否晚於錨點20:00。兩個截止均包含恰好在截止時點可得的事件，但「新增」只計錨點以後。個股本身的歷史不參與這個全球轉換。

臺灣連假期間多個海外交易日的累積變動因此可保留；沒有新增可用收件時兩欄為零，收到相同價格的新紀錄時則為零報酬、一個更新指標。缺少起始／終點價格或終端盤前時點未知時仍為NaN，不能把缺失報價當作沒有市場變動。這是可用報價窗口，並非海外開市日曆或完整行情交付證據。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set market --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window
```

原日報酬、事件年齡、價格特徵與未來答案完整保留，新增欄位只進入原有`portable_market`／`portable_all`候選，價格候選不使用它們。預設不啟用，正式研究／預測流程不增加這些輸入。來源紀錄及CLI摘要保存`overseas_window`，研究協定在擬合前保存實際`overseas_window_features`，模型版本亦受實際輸入欄位及程式摘要約束。

本機協定在執行前凍結三種原規則與同十六組候選、原盤前控制、行情／事件摘要、原保留期及共同資訊起點。固定十檔仍唯一決定訓練、校準及選擇；完整50檔僅同模型驗收。新收件仍可能是修訂而非新交易，重建可用時間與最新還原價不等同原始PIT證據，且本輪歷史已被檢視，不能重標成未接觸測試。

三組實際研究已完成，純價格候選的全部逐折校準分數與上一輪完全相同，逐筆決策時刻及未來答案亦相同。三種規則的核心一日配對區塊區間均涵蓋零，新增窗口尚無穩定提升證據；核心、完整50檔及原資訊就緒次要診斷均未達75%，弱股與缺漏仍保留，未自動啟用新特徵或更換正式模型。

## 第七輪：個股市場反應係數與波動分數

`--candidate-set exposure`保留原十六組，再加入`factor_beta_ridge`、`factor_beta_hist`、`factor_beta_vol_ridge`及`factor_beta_vol_hist`的市場／完整事件版本，共二十四組。新增八組皆使用756日訓練窗口，線性與提升樹參數沿用原分離模型。原`base`、`factor`、`market`集合與預設行為不變。

對完整固定十檔的每個訓練日期，以決策前已知的`f_beta_60`作市場曝險，先將成熟的未來對數報酬投影為共同因子`g = sum(beta * target) / sum(beta**2)`，再以`target - beta*g`擬合共享個股殘差。推論時回傳`beta*預測因子 + 預測殘差`，因此同一因子不再對每檔股使用相同幅度。這是模型的訓練目標，並非可交易指數或新增真實行情。

係數在兩端固定截為-3與3，缺失以單位市場曝險1替代；原特徵資料仍保留缺失。若係數欄位或可用歷史完全缺少，候選標記不可用。單日曝險能量不超過`1e-8`時只排除該候選的該訓練日，若不足十個可識別日期則跳過候選，外層日曆與其他模型不受影響。外部股票只使用原本已允許的自身過去價格係數，不進入任何訓練或共同因子估計。

`vol`版本另用決策前的`f_volatility_20`調整機率分數：缺失／非正值只用實際訓練列中正且有限的中位數替代，下限為該中位數十分之一與`0.0001`的較大者；再以訓練殘差除以當時波動後的均方根估計分數尺度。外部推論批次不重算中位數或殘差尺度。這仍是高斯近似分數，訓練殘差可能過度樂觀，不能稱為已校準的不確定性或75%保證。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set exposure --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window
```

本機先核對開發矩陣摘要與上一輪相同，再凍結全部候選、係數／尺度規則、三種原選模政策及資料／程式摘要。所有模型仍只在固定十檔訓練及校準；相同日期、資訊截止點、50檔名單、保留期與共同資訊起點均沿用，並核對原十六組的逐折分數。每天完整預測、缺漏分母、弱股及未達標方案繼續保留；候選數增加不等於取得新的獨立市場證據。

三組實際比較已完成，原十六組的全部逐折校準分數與控制完全相同。從開發矩陣重建每折實際訓練列，核對選中新模型的係數缺失／截斷數、波動中位數、下限及樣本數皆相符。一日最佳結果維持原值，部分規則與期間退步；核心、完整50檔及原資訊就緒次要診斷均未達75%，正式模型與門檻維持原狀。

另行取得的期交所官方日／夜盤月、年度檔放在獨立本機目錄，沒有回填本輪特徵或選模。它們提供真實價格資料，但盤前原始交付時間、合約換月及可用夜盤欄位仍須驗證；尚未產生新的夜盤預測事件。

## 第八輪：本地夜盤的盤前外生資訊

`direction-search --taifex-history <本機CSV或ZIP>`解析官方歷史格式，只保留TX、TE、TF月契約的日期、合約、交易時段與OHLCV；週契約、價差組合及其他商品不進此研究。結算價、未平倉、歷史極值等欄位不輸出。支援UTF-8／CP950及單一CSV的ZIP，只有已識別的空尾欄、缺失末尾價差成交量及DOS EOF可正規化；來源原始檔與雜湊仍留本機。

依[官方日期說明](https://www.taifex.com.tw/cht/3/futDailyMarketView)，夜盤日期為交易量歸屬日。每個歸屬日只從當次已觀測、有成交的夜盤月契約選最近月，再找**嚴格更早、相同商品及相同月份**的日盤收盤。缺少參考價時保持未知，不改用同日日盤或其他月份補值；因此換月不會把兩個合約的價差當成夜盤報酬。

每商品加入夜盤報酬、夜盤振幅及相對參考日盤成交量，各有事件年齡。這些是外生資訊，固定十檔的市場／同儕價格特徵、訓練／校準／選模及IX0001延伸角色均不變。`portable_market`允許這些相對資訊，共同因子亦只接受在固定十檔間按日期一致的欄位。

依[官方金融期貨時段](https://www.taifex.com.tw/cht/2/tF)，夜盤至次日05:00；但歷史檔不能證明原始交付時間。本輪**假設**歸屬日05:00發布、06:00可得，並在08:30含截止瞬間到期；週末歸屬日期的處理亦保持此保守重建，不宣稱實際收件。`availability_basis`與`original_pit_verified=false`隨事件保存。結果只在這個時間假設下解讀。

`point_in_time_join`尊重`expires_at`，過期的值及年齡皆為NaN，不恢復較舊值；沒有宣告到期的原事件維持原行為。另將顯式時區時間統一為UTC奈秒，修正不同精度會使as-of失敗的情況；只改表示精度，不改實際時刻。原有價格特徵與答案已逐筆核對不變。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set exposure --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window --taifex-history artifacts/taifex-night-research-20261107/futures_selected_2017_2026.csv
```

擬合前保存歷史檔雜湊、外部可用時間依據、到期特徵清單與實際夜盤欄位。沿用二十四組候選、三規則、同價格／名單／日期與預留期，全部50檔及資料缺口保留；當年度資料已補至原股票快照終點，不以資料較齊的日期取代原驗收。所有假設及失敗結果均保留，正式模型不自動替換。

三組實際比較已完成。純價格候選逐折分數與原控制完全相同，逐筆決策時點、答案、共用模型及保留期隔離核對通過。在宣告的收件假設下，一日期間有改善，較長期間未一致改善；核心、完整50檔及原資訊就緒次要診斷均未達75%。實際分數、配對區塊診斷與全部失敗方案只留本機，未將探索性改善標成及時未來實績。

## 個股期貨曝險與夜盤交互研究

在上述命令另加`--taifex-exposure`，可測試每檔股票對臺指、電子及金融期貨的不同反應；此選項必須同時提供`--taifex-history`。原夜盤欄位、價格特徵、未來答案及預設行為保持原值。

每商品先選當日有成交的最近月日盤，用嚴格更早、相同月份的日盤報酬作比較。參考日期必須等於上一個固定十檔／0050市場日，缺報告或換月缺參考時保持未知。每檔只用自己的錨點日及過去報酬，在60個主要市場日內以至少30對有效觀察計算共變異數／配對樣本變異數，將曝險限制在[-3,3]，再乘決策時已有效的夜盤報酬。三商品各產生曝險及交互作用，共六欄；歷史不足、零變異、缺夜盤或終端決策未知皆不補造訊號。

日盤資料另**假設錨點日20:00已收到**，早於此時的決策會被拒絕；夜盤仍沿用06:00收件與08:30到期假設。這些是假設交付時間，並非原始收件證據。新增欄位只進入`portable_market`與`portable_all`，純價格候選不變。外部股票只計算自身已知價格的機械特徵，不能供應主要日曆、固定十檔同儕、訓練、校準或選模；所有外部股票沿用同一個已選模型與門檻。

比較協定在擬合前保存六個新增欄位、原18個夜盤欄位、程式及資料摘要。仍比較原24候選與三規則，沿用相同50檔、全日期分母、預留期及先前宣告的資訊就緒次要診斷，不以選股或略過低信心日期取代每日完整預測。

三組比較與逐筆核對已完成。純價格候選逐折校準分數、原欄位與答案均保持相同。一日期間部分規則的平均結果小幅改善，最弱五檔仍遠低於目標，其他規則與較長期間亦有退步；全部範圍仍未達75%。完整失敗結果與配對區塊診斷留在本機，沒有將探索性改善當成已驗證的未來預測能力。

## 過去價格方向分布研究

`direction-search --candidate-set path`沿用原24組候選，另加六組`portable_path`候選：logistic、hist、extra_trees、random_forest、factor_beta_ridge與factor_beta_vol_ridge，各使用756日訓練視窗。此模式自動加入13個`f_path_`欄位；原三種portable特徵組排除這些欄位，因此原24組輸入保持相同。`portable_path`採原`portable_market`價格及相對海外／期貨資訊，再加入這13欄。

每檔只從其錨點日已知的`f_return_1/5/20`，分別計算60／252個主要市場日內的正報酬與持平比例，最少需要30／126筆有效觀察；另計算60日內相鄰單日報酬符號相同的比例，最少需要30對有效觀察。符號分成正、零、負，持平是獨立價格狀態；驗收目標仍為上漲與非上漲兩類。未知與非有限報酬不當成持平或下跌，缺報告會中斷相鄰配對，視窗按主要市場日滾動。

新欄位不讀未來答案，也不擬合各股專屬參數、門檻或校準模型。外部股票只提供自己的已知價格作機械特徵，不能供應主要日曆、固定十檔同儕、訓練、校準或選模；全體仍沿用同一已選模型與門檻。決策須在錨點日20:00假設收件時間之後，終端盤前NaT及主要日曆以外的外部日期保持未知。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set path --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window --taifex-history artifacts/taifex-night-research-20261107/futures_selected_2017_2026.csv --taifex-exposure
```

原價格、答案、夜盤與曝險欄位逐筆相同的核對，以及新增欄位、全部候選與三種選模規則，均在擬合前保存。沿用相同50檔、全部日期分母、126日預留期與原資訊就緒次要診斷；已多次檢視的開發歷史及重建資料交付時間仍是結論限制。

三規則比較與核對已完成，原24組候選的逐折校準分數完全相同。部分資訊就緒弱股診斷小幅改善，整體與較長期間未一致改善；固定十檔、完整50檔與原資訊就緒範圍均未達75%。全部候選、退步方案、缺漏與配對診斷保存於本機，正式模型仍維持原狀。

另為後續研究獨立準備XLF、XLP、XLC、XLI、XLB、XLE及SMH的海外產業代理歷史；標的角色可參考[State Street產業目錄](https://www.ssga.com/us/en/individual/capabilities/equities/sector-investing/select-sector-etfs)與[VanEck半導體基金說明](https://www.vaneck.com/us/en/investments/semiconductor-etf-smh-fact-sheet.pdf)。FMP回覆402的失敗紀錄保留，替代資料來自Yahoo公開歷史圖表回應，不能冒稱FMP交付。已核對商品、時區日期、價格範圍、唯一日鍵與原檔雜湊；較短歷史保持缺漏。這批資料不進入本輪價格分布比較，原始檔與實際數據只存本機；最新還原價亦不證明原始PIT資料或歷史收件時間。

## 海外產業相對資訊研究

`--candidate-set sector --sector-history PATH`在原30組候選之外，新增六組`portable_sector`候選：logistic、hist、extra_trees、random_forest、factor_beta_ridge與factor_beta_vol_ridge，訓練視窗均756日。此組採`portable_path`加產業相對資訊；原四種portable組排除`f_ext_us_sector_`，保留原30組全部輸入。

CSV須有`date,symbol,adj_close,source`，包含上述七個商品，來源明確標為`yahoo_historical_chart`或`synthetic`。轉換器拒絕重複鍵、非日日期、非有限／非正價格與不明來源，沒有網路或憑證讀取。以七條已觀測海外市場日期的聯集配對相鄰報價，個別缺報告會中斷報酬配對；尚無歷史的期間不補造。

每商品產生調整收盤與單次報酬事件，**假設美東當日17:00收件**，按紐約當地時間處理夏令時間，`original_pit_verified=false`。`add_overseas_window_features(..., event_prefix='us_sector')`再比較臺灣錨點日20:00與盤前08:30可見的最新收盤，取得累積變動及新收件指標。預設`fmp`行為不變，Yahoo事件不冒稱FMP。每商品共六個新增欄位，包含值、年齡與視窗資訊；模型排除絕對收盤及其年齡，只用28個相對欄位。

候選的市場因子僅接受在固定十檔間按日期一致的產業欄位；公司特有或不完整廣播的值留在殘差模型。七條美股序列只作全球輸入，沒有美股目標加入訓練。固定十檔唯一參與訓練、校準與選模，全體外部標的沿用同一模型及門檻。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set sector --sector-history artifacts/sector-context-research-20261107/sector_prices.csv --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window --taifex-history artifacts/taifex-night-research-20261107/futures_selected_2017_2026.csv --taifex-exposure
```

上述`sector_prices.csv`是本機已驗證報價加上誠實來源標籤的衍生檔。擬合前凍結原欄位逐筆相同核對、來源／價格／名單／程式摘要與全部36候選、三規則。仍使用相同50檔、完整日期分母、126日預留期與原資訊就緒次要診斷；最新還原價與重建收件時間不能證明及時未來實績。

三規則實際比較及核對已完成，原30組候選的逐折校準分數完全相同。部分一日平均小幅改善，弱股及較長期間未一致改善；固定十檔、完整50檔及原資訊就緒範圍仍未達75%。全部退步方案、缺漏與配對診斷保存在本機。

另已獨立取得完整50檔與0050／2002的FinMind法人歷史，逐檔核對原檔雜湊、商品／日期、非負數量及唯一分類日鍵，作為後續公司層級訊號研究；未放入本輪產業比較。保留原分類、缺漏與實際取得時間，不把缺少紀錄當成零，也不把當前下載回填成歷史原始收件。依[官方文件](https://finmind.github.io/tutor/TaiwanMarket/Chip/)，資料晚間更新，數值可能事後補計，並無固定最終定案時間；較長延遲假設亦不能證明原始PIT。原始與衍生數據僅存忽略目錄。

## 個股法人流向研究

`--candidate-set flow --flow-history PATH`沿用全部36組控制，另加六組`portable_flow`候選：logistic、hist、extra_trees、random_forest、factor_beta_ridge與factor_beta_vol_ridge，訓練視窗均756日。此組採`portable_sector`加八個`f_flow_`欄位，原五種portable組排除法人欄位。仍須提供`--sector-history`，讓原控制組輸入保持一致。

CSV須包含`date,stock_id,name,buy,sell,source`，股票代碼以字串讀取並保留0050的前導零，來源明確為`finmind_historical_institutional`或`synthetic`。只使用原表報導的`Foreign_Investor`與`Investment_Trust`分類，沒有將舊制Dealer與後來拆分項目重複相加。各類別計算買賣不平衡`(buy-sell)/(buy+sell)`、完整5／20個主要市場日的平均及是否有交易；明確的零買賣是中性值且活動為零，沒有報告則保持未知，缺漏會中斷滾動平均。

主要研究**假設延遲兩個固定十檔／0050已觀測市場日，於20:00收到**，有效至下一已觀測市場日08:30含截止瞬間。直接按日曆延遲對齊，每筆只供應指定決策日，不會將消失的分類或缺報告無限延續。未來資料、外部股票與未來答案不參與當前個股特徵的計算，市場因子與同儕亦不接收法人欄位；指數沒有法人資料時保持未知。

這些是可用時間假設，不能證明初次公布或最終定案時間。`--flow-lag-sessions 0`僅提供另一個明確記錄的敏感度設定，未執行的設定不能宣稱有績效。收件契約寫入協定與模型版本，合成法人資料會使整次實驗標為合成證據；原始快照的實際取得時間不被改寫。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_direction.json --events artifacts/fixed-ten/sources/events.csv --candidate-set flow --sector-history artifacts/sector-context-research-20261107/sector_prices.csv --flow-history artifacts/institutional-flow-research-20261108/institutional_history.csv --flow-lag-sessions 2 --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window --taifex-history artifacts/taifex-night-research-20261107/futures_selected_2017_2026.csv --taifex-exposure
```

擬合前凍結來源雜湊、八個新增欄位、收件假設、全部42候選與三規則，核對原36組所有輸入與未來答案保持相同。固定十檔唯一參與訓練、校準及選模，全50檔沿用同模型與門檻，完整日期分母、缺值、126日預留期與原資訊就緒診斷保持原樣。

本輪主比較使用兩個市場日延遲及三規則，另在檢視主結果前凍結一組同日20:00敏感度，只使用預先指定的`pooled_brier`。兩者使用相同42候選及全部股票／日期，收件契約分別保存，不將較早可得的假設當成實際歷史交付。

四組實驗與配對核對均已完成。原36組候選逐折校準分數完全相同，兩種收件假設的答案、日期與決策時間也一致。新增法人訊號沒有帶來一致改善；固定十檔、完整50檔及原資訊就緒範圍的1／5／20日驗收全部未達75%，完整日期與缺漏均保留。實際數字、退步方案及日期區塊配對區間只存本機，不發布供應商資料。少量舊快照未見修訂，也不能證明歷史08:30以前已可取得或已定案。

## 第十三輪：合成盤前開盤參考價

`--candidate-set opening-proxy --mode preopen`是獨立的**合成可得性研究**。依[證交所說明](https://shl.twse.com.tw/page/library/trade/2.html)，盤前接受委託時會揭示試撮價格；試撮價格不能當成最終開盤價。本輪沒有取得歷史試撮報價，而是明確假設08:30可得到帶誤差的下一市場日原始開盤參考，檢查這類額外公司資訊是否值得進一步取得。

`add_opening_reference_proxy`刻意使用研究矩陣的`next_open`評估欄位。其輸入為下一日原始開盤與已知前收盤的對數比值，加上以基點表示的獨立常態對數誤差；另產生按比例隨機缺報的情境。隨機值按種子、股票及錨點日期固定，不隨資料排序、外部股票或未來答案改變。它不讀未來收盤、高低價、成交量或調整因子；使用原始價格也表示除權息等原始價差保留在代理中。誤差與缺報分布尚未由真實試撮歷史校準。

三個新增欄位為`f_openref_log_gap`、按已知20日波動度縮放的`f_openref_scaled_gap`及`f_openref_present`。原六種portable特徵組全部排除這三欄；`portable_opening`沿用`portable_flow`再加代理欄位。缺報、無開盤價及終端決策日未知時，代理保持未知且不刪列；模型仍以原有特徵或訓練期先驗給出預測。原價格、答案、08:30決策時鐘與126日保留期保持原樣。

此組只有五個預先指定候選：prior、756日portable_price logistic、756日portable_flow logistic，以及756日portable_opening的logistic／hist。它是集中檢驗資訊假說的小型比較，並非原42候選再加兩組；前三組作為原比較的校準控制。固定十檔仍唯一用於擬合與選擇，全部50檔與延伸商品使用同一凍結模型及0.5門檻。

```bash
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m twquant --root artifacts/full-membership-transfer-20261107 direction-search --config configs/research_opening_proxy.json --events artifacts/fixed-ten/sources/events.csv --candidate-set opening-proxy --opening-noise-bps 50 --opening-missing-fraction .1 --opening-seed 17 --sector-history artifacts/sector-context-research-20261107/sector_prices.csv --flow-history artifacts/institutional-flow-research-20261108/institutional_history.csv --flow-lag-sessions 2 --selection-policy pooled_brier --membership artifacts/current-membership/20261107/membership.json --mode preopen --overseas-window --taifex-history artifacts/taifex-night-research-20261107/futures_selected_2017_2026.csv --taifex-exposure
```

收件假設、種子、誤差及缺報率寫入來源紀錄、協定與模型版本。研究引擎只要看到這類欄位就必須驗證合成契約，即使呼叫端傳入`historical`也會強制改為`synthetic`；沒有契約會拒絕執行。CLI也拒絕在其他候選組使用代理參數，或用盤後模式執行代理研究。這條路徑不加入正式預測工作流程。

本輪於擬合前固定六種情境及17／29／43三個種子：無參考價、零誤差參考價、25／50／100基點誤差，以及50基點誤差加10%缺報。共18組只研究下一日原有收盤相對前收盤方向，保留完整50檔、每日分母與全部失敗結果；另用同一預測評估開盤至收盤方向作為對照，不能當成該目標的獨立選模成果，也不延伸宣稱5／20日表現。

全部18組與核對已完成，前三個原始控制候選的逐折校準分數完全相同。相對於無參考價的同種子控制，參考假設改善了平均表現，但即使零誤差情境，固定十檔、完整50檔與原資訊就緒範圍仍未達75%。全部原始稽核由預測列和完整日期重算並完全相符；完整歷史的上市前日期、缺資料與未解答案沒有被刪除。實際數字、配對區間及所有種子結果只存本機。理想化情境不是數學效能上限，亦不能證明任何可取得報價具有這種精確度、時效或前瞻訊息。

## 第十四輪：開盤參考與剩餘報酬分解

`--candidate-set opening-residual`沿用前輪五候選，再加四個756日`portable_opening`模型：`opening_residual_ridge`、`opening_residual_hist`及各自的`_vol_`變體。CLI的資料需求、`--opening-*`參數、盤前時鐘與強制合成證據標記皆沿用上一輪；使用相同一日設定與來源時，只須將命令的候選組改成`opening-residual`。

新增模型把原對數收盤報酬寫成「合成開盤價差＋待估剩餘報酬」。訓練目標是成熟的`target_1`減去當時假設可見的`f_openref_log_gap`，以共享ridge／hist估計條件平均；推論時再加回個股代理價差，以訓練殘差的常態近似產生上漲機率。剩餘模型也能根據代理欄位學習誤差修正，不假設帶雜訊的價差具有完美單位係數。所有擬合、補值與誤差尺度只使用固定十檔訓練子集，沒有按外部股票另擬模型。

`_vol_`變體按決策時已知20日波動度調整誤差尺度；缺失波動度使用訓練正值中位數，下限為該中位數10%與0.0001的較大值。缺開盤參考時，其已知價差貢獻為零，剩餘模型仍提供預測；整個訓練視窗沒有參考價則將新增候選標為不可用，保留原五候選。原始價格除權息價差及收件假設的限制仍適用，常態機率不是已校準的不確定性保證。

本輪於擬合前凍結相同六種情境、三個代理種子、九候選與單一`pooled_brier`規則，共18組。資訊、原有答案、完整50檔、0.5門檻、每日日期分母與126日保留期不變；只改模型的報酬參數化方式，不把上一輪理想化參考結果當成效能上限。

全部18組與核對已完成。原五候選的逐折校準分數完全相同，已選剩餘模型的訓練統計也重新擬合核對一致；無參考價時，新增候選全部不可用，原預測保持相同。固定十檔、完整50檔及原資訊就緒診斷仍未達75%，弱股未見一致改善。所有新增模型、退步情境、配對差異、未解答案與完整日期分母均保存在本機，沒有開封保留期或更改正式模型。
