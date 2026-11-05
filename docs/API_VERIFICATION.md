# API與完整串接驗收

更新：2026-11-05。公開程式與測試不包含使用者Key、帳號權限結果、供應商資料或模型。帳號實測證據只留在被忽略的本機工作目錄。

## 可重複執行的實測

```bash
# 少量唯讀測試；不覆寫主行情，不登入缺少必要條件的券商帳號。
.venv/bin/python -m twquant --root artifacts/real sources verify

# 擴充至逐系列及歷史範圍權限。
.venv/bin/python -m twquant --root artifacts/real sources verify --full

# 只測指定來源，減少配額消耗。
.venv/bin/python -m twquant --root artifacts/real sources verify --providers fred,fmp
```

報告位於所選root的 `sources/verification.json`，每次不可覆寫的證據在 `sources/verifications/<版本>/`。只回傳筆數、日期、格式／映射狀態、錯誤及必要條件；Key值不進報告。部分端點無權限或尚缺條件時exit 2；不代表所有Key都失效。

`passed`只表示該項小量請求、格式及快照正規化通過。`complete: false`、`numeric_mapping: false`或0筆事件仍需另外驗收；不能把HTTP 200視為完整歷史或已能作模型特徵。歷史抽樣通過也不保證所有年份均無缺漏。新聞與政府快照仍需實體／事件及產業數值映射。

## 本次實測找到並修正的程式問題

| 問題 | 修正與驗證 |
|---|---|
| FRED日頻資料超過單次2,000個vintage日期 | 同時按實時版本日期分段，逐段分頁並重置offset；不是只縮短觀測期間 |
| 觀測日期晚於vendor vintage的日頻資料 | 保留原始版本，在觀測日與vintage日兩者都結束後才提供特徵；避免提前使用 |
| Twinkle SSE中文被requests預設Latin-1誤解碼 | 依SSE使用UTF-8，只按協定換行分段；初始化通知接受空202 |
| TAIFEX端點回CSV而非JSON | 只在明確官方期貨路由解析CSV，驗證必要欄位；不把任意HTML當行情 |
| FMP財報要求1,000筆超過部分方案上限 | 預設5筆，明確標示非完整歷史；計畫可指定`limit`及`period`，年度預期明確設定annual |
| 非JSON的402回應被誤判格式壞掉 | 保留HTTP狀態及端點權限分類，不回顯原始body／認證網址 |
| Fugle長期下載可能超過60次／分鐘 | 同一transport的Fugle請求間隔至少1.05秒；多程序仍需共享限流或統一排程 |
| 0050停牌導致個股交易日被刪除／到期錯位 | 特徵與帳本共用已觀測市場日期聯集；不補造ETF停牌價。基準當日無法交易則留現金，個股交易繼續；被動基準缺估值時保留端點總報酬、停算每日風險 |

0050於2025-06-11至06-17因分割停牌，其他股票仍有交易，官方[公告](https://www.twse.com.tw/zh/ETFortune/announcement?company=A00005&date=20250509&fund=0050&seq=1&type=other)可供核對。行情日期聯集只能證明已觀測的開市日；全部來源同時缺資料或未來開休市，仍須完整且可核對的交易所日曆。

本次特徵版本更新為 `pit-eod-v2`，舊模型須重新研究／校準；示範可重跑 `demo`。不改寫舊預測或已結算答案。若保留了跨停牌期間的舊版未結算重播，應另建root重播並記錄程序修正，避免與新的逐日影子證據混用。

## 富邦環境

官方Python SDK目前支援3.8至3.13，未支援3.14；參考[官方下載與版本說明](https://www.fbs.com.tw/TradeAPI/en/docs/download/download-sdk/)。本機另以既有Python 3.13建立 `artifacts/fubon-venv`，安裝官方macOS arm64 SDK 2.4.0並通過import；SDK不隨Git散布。

```bash
artifacts/fubon-venv/bin/python -m twquant --root artifacts/fubon-verification sources verify --providers fubon
```

使用者仍須自行申請／匯出有效憑證，在私人.env設定正確的`FUBON_CERT_PATH`、身分及必要密碼，檔案權限0600。缺少條件時不嘗試登入；安裝成功不能證明Key、憑證及行情權限已通過。只使用行情，不呼叫下單、出入金、庫存或損益。

## 真實資料驗收邊界

本次另在隔離root做過多來源下載、事件匯出、特徵、歷史研究及paper預測實測。這能發現單元測試未涵蓋的供應商格式、方案及停牌案例；可行性仍由真實樣本外、封存測試、PIT品質與連續影子紀錄判斷。歷史重播的到期結算不計入及時逐日紀錄。

還需要的資料／工程項目見 [PROJECT_STATUS.md](PROJECT_STATUS.md)，值得接入但尚未接入的來源與套件見 [OPEN_RESOURCES.md](OPEN_RESOURCES.md)。
