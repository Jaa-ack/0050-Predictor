# 憑證保護、API費用與免費替代

查核日期：2026-11-05。金額是官方頁面列價，方案／稅費／權限以訂閱當下為準；本專案沒有替使用者購買或升級。

## 富邦憑證

富邦匯出的PKCS#12憑證通常是`.p12`／`.pfx`。可以保留一份在本機專案的`.secrets/fubon/`，但副檔名本身不保證有效，仍需正確帳號、憑證密碼與效期。[官方登入規格](https://www.fbs.com.tw/TradeAPI/docs/trading/library/python/login/loginAPIKey/)

安全複製指令（來源改成本機實際檔案，不要把檔案或密碼貼入聊天）：

```bash
.venv/bin/python -m twquant secrets import-cert --from /absolute/path/exported.p12
```

此指令以固定檔名複製到私人目錄，資料夾0700、檔案0600，拒絕symlink與自動覆寫；不登入、不讀出密碼、不更動既有.env。將私人.env的`FUBON_CERT_PATH`設定為複本的**絕對路徑**，其餘必要欄位為`FUBON_PERSONAL_ID`、`FUBON_API_KEY`及`FUBON_CERT_PASSWORD`。官方預設憑證密碼規則請自行依[官方匯出說明](https://www.fbs.com.tw/TradeAPI/docs/trading/api-key-apply/)設定，不在輸出顯示。

`.gitignore`、Docker context與Python封裝都排除私人目錄及憑證。提交掃描器允許未追蹤的私人複本，仍拒絕被`git add --force`追蹤的憑證。不要手動force加入、打包整個工作目錄、分享雲端備份或公開除錯記憶體；原始下載檔與備份也需保護。這是檔案權限保護，未宣稱為加密保險箱。

既有隔離環境可測試富邦，憑證未齊不會嘗試登入：

```bash
artifacts/fubon-venv/bin/python -m twquant --root artifacts/fubon-verification sources verify --providers fubon
```

## 擴充權限是否值得

| 服務 | 免費與限制 | 付費列價／建議 |
|---|---|---|
| [Fugle](https://developer.fugle.tw/docs/pricing/) | 基本用戶免費；歷史行情各方案均60次／分鐘；盤後籌碼非免費功能 | 開發者NT$1,499／月、進階NT$2,999／月。日線基準先用現有免費還原行情＋官方集保／公告；有歷史ETF持股或完整夜盤需求再核對方案，升級不會加快歷史行情配額 |
| [FMP](https://site.financialmodelingprep.com/pricing-plans) | Basic免費250次／日、滾動30日500MB，符號／端點／參數仍有限制 | 年繳折算Starter US$19／月、Premium49、Ultimate99。不能只憑升級名稱保證QQQ／SOXX或特定端點解鎖，購買前核對。公開展示或重散布另需資料授權；目前維持免費較合適 |
| [News API](https://newsapi.org/pricing) | Developer每日100次、24小時延遲、近1個月，僅開發／測試，不可當正式或內部production方案 | Business US$449／月、Advanced1,749／月（月繳）；標準付費搜尋至5年，也不含完整文章全文。現階段不建議為尚未驗證的新聞因子先升級；需正式即時跨媒體供料且證明增益後再評估 |

既有已通過驗證的免費Key可沿用，重複申請帳號／金鑰或換成MCP並不會增加合法授權，也不應用來規避配額。MCP只是呼叫介面；底層資料範圍、費用與展示條款仍適用。

## 已直接接入的免費資料

| 來源 | 目前接入能力 | 邊界 |
|---|---|---|
| TWSE重大訊息 | 官方每日公告、公司代號、發言時間、內容與版本留存／去重 | 不等同全部新聞；使用發言時間而非事實發生日；無歷史公告修正推定或自動情緒 |
| TWSE年度開休市 | 保存年度事件、區分開始／最後交易日、放假及補假，匯出逐日候選表 | 候選表`verified=false`；含工作日推導且缺臨時休市，禁止直接啟用正式日曆 |
| TDCC官方OpenAPI | 最新股權分散快照，修正BOM欄位及代號空白，保存目標股分級持有比例／人數事件 | 新網址為`https://openapi.tdcc.com.tw/v1/opendata/1-5`；舊CSV網址本次轉到一般頁面，不跟隨降級到HTTP。只按實際收件時間使用，不補造歷史發布時點 |
| GDELT官方GKG檔案 | 下載最新15分鐘批次、驗證清單檔大小／MD5、ZIP結構與上限，保留關鍵字相關URL／主題／組織資訊 | DOC搜尋API本次超時，改用官方檔案通道。批次可能0篇符合關鍵字；不是全天新聞、沒有全文／標題或原文發布時間。有限重試，失敗如實回報，不補假新聞 |

上述端點已做本機唯讀實測；外部服務仍可能中斷或改版，沒有宣稱永久可用或免費SLA。保存不可覆寫快照，失敗保留舊版本與錯誤，不能把舊資料當成新資料。

```bash
.venv/bin/python -m twquant --root artifacts/public-live sources sync --plan configs/public_source_plan.json
.venv/bin/python -m twquant --root artifacts/public-live sources export-events
.venv/bin/python -m twquant --root artifacts/public-live sources export-documents
.venv/bin/python -m twquant --root artifacts/public-live sources export-calendar --year 2026
```

也已加入主要來源計畫與驗證指令，網頁來源清單會顯示TDCC及GDELT。沒有自動安裝電腦排程；需要常駐機器才能長期收集。GDELT抓一次僅涵蓋一個15分鐘批次，不能以每天一次宣稱完整新聞歷史。現有模型需重新研究／校準後才可使用新增特徵。

## 仍需人工或其他方向

- 元大完整持股：2026-11-05的本機HTTPS測試曾遇到憑證鏈失敗，未停用TLS檢查；2026-11-07已從[官方頁面](https://www.yuantaetfs.com/product/detail/0050/ratio)展開並保存當期完整名單及內容雜湊，可供隔離的同模型成分股驗收。這只補齊當期範圍，不把最新名單回填成歷史成分證據；操作見[方向研究協定](DIRECTION_RESEARCH.md)。
- SEC EDGAR：正式自動存取需符合Fair Access並設定可識別的User-Agent聯絡資訊；目前未替使用者捏造聯絡資料。若要接入，先選定資料擷取聯絡方式。這是美股／ADR財報，不能直接代替臺股財報。
- 臺灣總體：Twinkle已可搜尋，仍需選定資料集、單位、頻率與修訂規則。可先用已驗證FRED及臺股價格／公告／營收做基準，避免將未知政府JSON直接當模型輸入。
- 長期新聞與歷史權重：沒有單一免費來源能保證2014年至今完整繁中金融新聞、原發布版本及全部歷史ETF權重。可採「長期價格基準＋從現在累積的較短期事件實驗」，再以消融測試決定是否值得付費。
