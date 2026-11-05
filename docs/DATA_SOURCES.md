# 資料來源整合與仍需提供的資源

查核日期：2026-11-05（Asia/Taipei）。這七項來源足以支援第一階段日線量化研究；仍須逐一驗證帳號方案、資料起訖、發布時間及修正版本，才能認定適合歷史回測。持有 API Key 並不表示所有端點或十年歷史都已開通。

## 各來源的實際分工

| 來源 | 本專案分工／已實作連接器 | 還需核實 |
|---|---|---|
| [FinMind](https://finmind.github.io/tutor/TaiwanMarket/DataList/) | 既有原始／還原日線；新增法人、融資券、借券、財報、營收、股利、期貨選擇權及美股資料集的唯讀擷取；部分明確數值欄位可匯出事件特徵 | 會員方案、各表實際起點、配額、財報更正及原公告時間。今天下載的財報不當作原公告時已知版本 |
| [FRED／ALFRED](https://fred.stlouisfed.org/docs/api/fred/series_observations.html) | GDP、CPI、PCE、失業率、非農、利率、2／10年債、貨幣供給、VIX、USD/TWD；下載 real-time periods、分頁、保留版本並轉成事件特徵 | 各系列 vintage 起點與發布時間精度。只有發布日期時，保守延後至次一紐約午夜可用；不是即時匯率或股票報價 |
| [FMP](https://site.financialmodelingprep.com/developer/docs/stable/historical-price-eod-dividend-adjusted) | 美股／ETF 原始或股利調整價格、財報、分析師預期及新聞快照；市場序列與部分財務欄位可匯出事件特徵 | 商品／符號權限、歷史長度、調整規則。分析師預期只保存今日快照，尚不具歷史共識預期版本；非行情端點不宣稱完整歷史 |
| [News API](https://newsapi.org/docs/endpoints/everything) | 分頁搜尋、URL＋發布時間去重、標題摘要與來源留存、實際取得時間 | 方案延遲、搜尋年限、商業／正式使用權利及臺灣新聞涵蓋；未產生情緒、意外程度或價格預測因子 |
| [Twinkle Hub](https://hub.twinkleai.tw/docs?aud=api) | MCP initialize／session／SSE，依官方格式呼叫 `execute_tool({tool_id, arguments})`；政府資料搜尋與指定唯讀工具快照 | 權限／費用、工具 schema、原始 license 與可用歷史。政府資料仍須先對應公司／產業，再建立明確數值因子；不直接把未知 JSON 塞進模型 |
| [Fugle](https://developer.fugle.tw/docs/data/http-api/getting-started/) | 原始與還原日線分開擷取、不到一年分段、ETF 每日持股、法人、集保分散、當日報價／成交／K線快照 | 歷史端點權限、分K起點、ETF成分資料最早日期及資料發布時間；ETF權重按百分比轉為比例，不補造歷史公告時間 |
| [富邦 Neo API](https://www.fbs.com.tw/TradeAPI/docs/trading/library/python/login/loginAPIKey/) | 官方 SDK 的行情唯讀路徑、優先 `apikey_login`、原始／還原日線及交叉核對用途 | 需身分識別、API Key、當地憑證與密碼、相容 SDK；還原K線需支援該功能的新版SDK。沒有呼叫下單、出入金、庫存或損益介面 |

目前已完成多來源本機唯讀實測與修正，提供 `sources verify --full` 重複核對；帳號權限與原始證據只保留於被忽略的本機目錄。部分端點需方案權限，不能以其他可用端點推論全部開通。詳見 [API驗收](API_VERIFICATION.md)。完整使用者權限清單可填入 `configs/source_access.example.json` 的副本；只記錄方案與資料範圍，不填 Key、帳號或身分資料。

Fugle 官方文件列出還原價格及 ETF 持股介面，可能直接補齊初版的還原價格與權重來源缺口；實際方案仍須測試。還原序列會隨後續公司行動向前重算，沒有指定過去時點的還原版本，因此每次下載都須留存。[歷史K線規格](https://developer.fugle.tw/docs/data/http-api/historical/candles/)、[ETF持股規格](https://developer.fugle.tw/docs/data/http-api/ownership/etf-holdings/)

富邦行情服務由 Fugle 技術團隊提供，兩者不應視為完全獨立的交叉驗證資料源。原始收盤價應再與交易所資料核對。[富邦行情來源說明](https://www.fbs.com.tw/TradeAPI/docs/market-data/intro/)

News API 官方開發方案有24小時延遲、較短歷史範圍及開發用途限制；付費方案的搜尋年限也須依實際訂閱核對。此處沒有以開發方案即時新聞作假設。[方案與限制](https://newsapi.org/pricing)

## 已接入並實際下載的官方公開資料

以下四個介面已透過 `sources sync` 實際取得並封存。它們提供當期快照，不能代替十年還原行情或歷史公告版本。

| 來源 | 端點 | 本次留存筆數 |
|---|---|---:|
| TWSE 上市每日行情 | [STOCK_DAY_ALL](https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL) | 1,390 |
| TWSE 公司基本資料 | [t187ap03_L](https://openapi.twse.com.tw/v1/opendata/t187ap03_L) | 1,099 |
| TWSE 月營收 | [t187ap05_L](https://openapi.twse.com.tw/v1/opendata/t187ap05_L) | 1,088 |
| TAIFEX 期貨日行情 | [DailyMarketReportFut](https://openapi.taifex.com.tw/v1/DailyMarketReportFut) | 2,221 |

原始回應與檢查碼位於 `artifacts/real/sources/raw/`；資料快照、來源及擷取時間位於 `sources/snapshots/`；本次結果見 `sources/sync_report.json`。未向主價格表填入未還原的交易所行情。

## 其他已整理的官方入口

下列來源先列入資料目錄及驗收清單，**尚未全部建立自動爬取器**。網站可瀏覽不等於有穩定歷史API；優先使用已授權供應商或正式開放資料。

| 官方入口 | 用途／接入方向 |
|---|---|
| [公開資訊觀測站 MOPS](https://mops.twse.com.tw/) | 財報、月營收、重大訊息的原公告及更正佐證；需保留原始公告，補齊歷史可用時間 |
| [元大0050持股](https://www.yuantaetfs.com/product/detail/0050/ratio) | 核對最新權重；每日保存以逐步建立自己的持股檔，不能把最新名單回填過去 |
| [臺灣集保股權分散表](https://www.tdcc.com.tw/portal/zh/smWeb/qryStock) | 官方分散資料核對；可先經 Fugle／FinMind 授權端點取得，再對齊統計及發布日 |
| [中央銀行](https://www.cbc.gov.tw/) | 臺灣利率、匯率、貨幣與外匯統計；FRED匯率為特定來源及頻率，不能當即時USD/TWD |
| [主計總處統計資訊網](https://www.stat.gov.tw/) | 臺灣GDP、CPI、就業及發布日程 |
| [經濟部](https://www.moea.gov.tw/) | 外銷訂單、工業生產、產業統計；由 Twinkle 搜尋核對資料集ID後再建數值映射 |
| [政府資料開放平臺](https://data.gov.tw/) | 政府資料目錄；優先透過 Twinkle 或資料集所列正式下載／API |
| [期交所歷史資料說明](https://www.taifex.com.tw/cht/9/tradersQAIS) | 完整夜盤、逐筆／委託歷史必要時採正式訂購，公開近期下載不能冒充十年完整分K |

## Key 與資源設定

執行 `secrets init` 會建立私人 `.env` 的空白欄位，權限0600；可提交的 `.env.example` 始終保持空白。

```text
FINMIND_TOKEN=
FRED_API_KEY=
FMP_API_KEY=
NEWS_API_KEY=
TWINKLE_API_KEY=
FUGLE_API_KEY=
FUBON_API_KEY=
FUBON_PERSONAL_ID=
FUBON_CERT_PATH=
FUBON_CERT_PASSWORD=
FUBON_PASSWORD=
```

富邦優先用 API Key，`FUBON_PASSWORD` 可留空；身分及憑證仍依官方SDK要求提供。金鑰應限制為行情用途，設定可用的IP／到期限制；憑證放在本機私人路徑或被忽略的 `.secrets/`，權限0600。[富邦Key權限說明](https://www.fbs.com.tw/TradeAPI/docs/trading/api-key-apply/)

除了Key，本專案還需要：各方案權限／配額及歷史起訖、有效富邦憑證／相容環境、核實交易日曆、股票主檔及歷史成分、財務單位字典與公告更正記錄、資料保留／再利用條款，以及後續固定運行與備份的環境。沒有要求你提供帳戶庫存、資金或交易權限。

## 執行順序

```bash
# 顯示欄位缺漏，不顯示值。
.venv/bin/python -m twquant sources status

# 設定私人.env後，按明確計畫擷取。首次完整日線會分段發出多次請求。
.venv/bin/python -m twquant --root artifacts/real sources sync --plan configs/source_plan.json

# 匯出已具明確數值映射的來源。未映射新聞／政府JSON仍保留快照。
.venv/bin/python -m twquant --root artifacts/real sources export-events

# Fugle可直接作為主還原價格來源；也可選finmind或fubon。
.venv/bin/python -m twquant --root artifacts/real ingest --provider fugle --start 2014-01-01

.venv/bin/python -m twquant --root artifacts/real research --events artifacts/real/sources/events.csv

# 若已有兩份來源快照，交叉比對原始收盤價，不自動混合兩個還原版本。
.venv/bin/python -m twquant --root artifacts/real sources reconcile --left fugle --right fubon
```

`source_plan.json` 明確選 Fugle 作為主價格來源，富邦目前只核對0050／2330；FinMind法人与財報範例目前先列2330，驗證後再擴充至訓練股票池。SPY、QQQ、SOXX為市場代理ETF，不能稱為S&P500、NASDAQ綜合指數或SOX本身。FMP目前沒有直接生成臺股估值輸入；FinMind財務原始數值仍須經單位、TTM、股數及科目映射後才能自動供應DCF／P-B模型。

缺Key或SDK的工作會跳過並報告；無權限、欄位異常或不完整分頁不會偷偷補成合成資料。新增來源後要重新訓練，舊版模型不能直接沿用新欄位。更多完整系統缺口見 [PROJECT_STATUS.md](PROJECT_STATUS.md)。
