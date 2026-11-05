# 值得補入的資料與開源專案

查核：2026-11-05。以下明確區分已接入部分及剩餘條件。「可免費讀取」與「允許重散布」須分開；供應商資料不隨程式碼開源。可直接執行的無Key計畫、費用及手動資源見[CERTIFICATES_AND_COSTS.md](CERTIFICATES_AND_COSTS.md)。

## 資料

| 優先 | 來源／狀態 | 可補足什麼 | 剩餘條件 |
|---|---|---|---|
| P0 | [TWSE開休市](https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule)：年度事件／候選匯出已接入 | 區分最後／開始交易日、放假及補假 | 不是完整逐日官方is_open表；候選`verified=false`，仍需歷年及臨時休市公告覆寫 |
| P0 | [元大0050持股](https://www.yuantaetfs.com/product/detail/0050/ratio) | 現行前十大核對與逐日持股留存 | 今日持股不能回填過去；Fugle持股端點有方案限制，官方文件載歷史自2019年起，2014年至今的動態股票池仍需更早資料 |
| P0 | [MOPS](https://mops.twse.com.tw/)／原始財報公告；TWSE重大訊息已接入 | 財報、營收、重大訊息的原發布與更正時點 | 公告可按發言時間與實際收件保留，歷史版本及財報金額、股數、累計／單季和金融科目字典仍缺；今天財報快照不當過去已知 |
| P1 | [TDCC股權分散](https://openapi.tdcc.com.tw/v1/opendata/1-5)：最新快照與數值映射已接入 | 籌碼與持股分散，核對供應商數值 | BOM／代號空白已正規化；仍缺更早歷史及原發布證據。週資料不按日補造新資訊，更正／總計級距不入模 |
| P1 | [主計總處](https://www.stat.gov.tw/)、[央行](https://www.cbc.gov.tw/)、[經濟部](https://www.moea.gov.tw/) | 臺灣GDP／CPI、利匯率、外銷訂單與工業生產 | Twinkle已能搜尋資料，但各dataset仍須明確數值映射、發布日及修訂版本 |
| P1 | FRED的[NASDAQCOM](https://fred.stlouisfed.org/series/NASDAQCOM)／[NASDAQSOX](https://fred.stlouisfed.org/series/NASDAQSOX) | 美股市場及半導體指數因子，補充部分FMP符號未授權時的研究 | 已實測可讀，計畫加入但預設停用，啟用前核對使用條件；它們不是QQQ／SOXX ETF，時間延遲與收益定義不同。系列標有Nasdaq版權，不能當無限制開放資料公開轉售 |
| P1 | [SEC EDGAR API](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) | 美股／ADR原始申報、申報接受時間及XBRL版本 | 無須API Key，但需遵守Fair Access及識別要求；美國GAAP／IFRS、幣別與ADR比例需標準化，不能直接代替臺股估值 |
| P2 | [GDELT GKG](https://gdeltproject.org/data.html)：最新15分鐘批次已接入 | 免費累積URL／主題／組織中繼資料，補充新聞方案 | 有界下載、清單大小／MD5校驗及去重；尚缺全時段排程、歷史批次回補、精確實體／事件辨識。沒有全文／標題／原發布時間，不等同取得媒體全文授權，BigQuery運算可能有費用 |

已接入的TWSE行情／公司／營收及TAIFEX當期快照不在「未接入」清單重複列入；它們仍不能替代完整十年還原行情、夜盤逐筆與公告版本。盤後資訊[官方資料集](https://data.gov.tw/dataset/11549)另有開放授權說明。

## 可接到現有模組的開源套件

| 順序 | 專案 | 放在哪裡／價值 | 使用前驗收 |
|---|---|---|---|
| 1 | [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars)（含XTAI） | 日曆候選，輸出`date,is_open,source` | 與TWSE逐年比對；颱風、臨時休市及公告更正以交易所為準，套件不能自行取得verified標記 |
| 2 | [arch](https://arch.readthedocs.io/en/latest/bootstrap/bootstrap.html) | 為誤差、Brier及策略表現加入時間區塊bootstrap | 按日期／時間區塊抽樣；不能把同日10檔股票當10個獨立市場樣本 |
| 3 | [LightGBM](https://github.com/lightgbm-org/LightGBM) | 取代／比較目前histogram boosting，可建分位數模型 | pyproject只有optional依賴宣告，尚無訓練adapter；相同purge、校準、成本及封存期比較，不能只裝套件就宣稱改善 |
| 4 | [Optuna](https://github.com/optuna/optuna) | 訓練模組的超參數搜尋 | 僅使用開發期巢狀時間切分，封存最終期不進搜尋；保留試驗總數，處理多次試驗偏誤 |
| 5 | [MLflow](https://www.mlflow.org/docs/latest/tracking/) | 匯出既有run_id、設定、程式／資料摘要、評估指標 | 先用本機tracking；不把Key、私人帳號與供應商原始資料上傳追蹤服務 |

上述套件能沿現有深層模組邊界接入，但仍須adapter、版本相容性及回歸測試；不是免設定的獲利模型。暫不優先全面遷移Qlib／FinGPT：目前最大瓶頸是資料時點、股票池與驗證證據，框架遷移或LLM新聞分析需先用消融實驗證明增益。
