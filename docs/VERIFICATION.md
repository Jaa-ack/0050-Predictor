# 本機實作與驗證紀錄

驗證日期：2026-11-04（Asia/Taipei）。依本次確認，交付範圍為本機系統及合成資料示範；尚未接入使用者的正式金融資料，未安裝背景排程。

## 真實來源驗收增補（2026-11-05）

後續已依使用者授權完成本機Key／端點及真實多來源整合實測。帳號權限、供應商數值、模型與逐筆預測證據保存在忽略的本機工作目錄，沒有加入Git或託管預覽。部分服務認證可用但端點無權限；券商認證需有效本機憑證，不能只憑Key判定通過。

修正FRED分段／時間、Twinkle SSE／202、TAIFEX CSV、FMP方案參數及0050分割停牌案例後，Python完整測試 **138 passed in12.36s**、前端7項通過，兩個Python環境的pip check通過。真實研究、paper預測、歷史重播到期結算及封存測試均已實際執行；保留測試仍未通過成本後優於被動0050，正式預測準備度未通過。沒有把歷史重播計入及時影子實績。

可重複指令、修正與限制見 [API_VERIFICATION.md](API_VERIFICATION.md)，補充資源見 [OPEN_RESOURCES.md](OPEN_RESOURCES.md)。以下較早紀錄保留供追溯。

## 網頁與發布驗證（2026-11-05）

- 最終Python測試：`124 passed in 12.03s`；前端Node內建測試7項通過，JavaScript語法檢查通過。
- `compileall`、`pip check`通過。0.2.0 wheel／sdist已成功建立，逐檔確認包含HTML／CSS／JS，沒有私人.env、原始資料、模型或設定中的Key值。
- Playwright實測六頁導覽、20日期間、中文搜尋、個股價格與因子視窗、曲線圖例、9來源、工作區、P0篩選及本機demo任務完成。
- 390px手機版頁面寬度等於視窗寬度，個股視窗可操作；瀏覽器Console為0 errors／0 warnings。截圖只保留於被忽略的 `output/playwright/`。
- 新增API固定工作區／任務、nonce／同源、單工作執行、輸出遮罩及合成匯出回歸測試；內建稽核拒絕被強制加入Git的.env與二進位模型。
- Gitleaks 8.30.1官方發行檔經SHA256核對，對80份待發布檔案及首次提交完整Git歷史掃描：no leaks found。公開Git只追蹤空白 `.env.example`，不含私人.env、模型、資料庫或artifacts。
- 線上預覽僅含合成快照，運算按鈕停用；不將部署成功解讀成真實市場模型已通過。
- 靜態版瀏覽器驗證通過：個股與歷史圖表可讀、六個任務停用、沒有請求本機API。部署工具已回報 `succeeded`，網址為 <https://tw0050-research-lab.jaaack1203.chatgpt.site>，維持擁有者私人存取。

GitHub首次發布commit為 [`791d3fa`](https://github.com/Jaa-ack/0050-Predictor/commit/791d3fa76a64de901138667ce86d9f88a86c3634)。[Python 3.11／3.14與前端測試CI](https://github.com/Jaa-ack/0050-Predictor/actions/runs/41940834572)、[金鑰與完整歷史掃描CI](https://github.com/Jaa-ack/0050-Predictor/actions/runs/41940834552)均通過。Dependabot已實際建立依賴更新PR，等待維護者審核；沒有自動合併。

[合成預覽建置CI](https://github.com/Jaa-ack/0050-Predictor/actions/runs/41940834565)亦通過，產生 `synthetic-web-preview` artifact（149,036 bytes，保存7日）。託管頁面與GitHub產物均不包含真實來源資料。

## 多來源與開源保護增補

同日後續新增七來源連接器與Key保護，最新完整測試為 **115 passed in12.27s**。原始76項紀錄保留如下供追溯。

- 已實際取得TWSE行情1,390筆、公司資料1,099筆、月營收1,088筆及TAIFEX期貨日行情2,221筆；原始回應與快照保存在 `artifacts/real/sources/`。
- 私人來源以契約測試核對請求／正規化／錯誤處理；Key尚未由使用者填入，沒有宣稱七個帳號均已實際連通。
- 新增FRED分頁與修訂時點、舊期修訂不取代最新期、FMP美股DST與調整價、Fugle上市前404／還原對齊、ETF權重單位、News去重、Twinkle SSE與外層呼叫、富邦API Key唯讀SDK路徑等測試。
- Key測試涵蓋0600／symlink拒絕、不可直接列印或JSON序列化、回應／錯誤／日誌遮罩、URL編碼回顯、前端不回傳值、來源允許主機與拒絕redirect、被強制加入Git的.env阻擋。
- 內建開源前掃描通過；pre-commit設定已驗證。已檢查本機wheel／sdist不含私人.env或憑證；GitHub CI與完整Git歷史掃描尚未執行。

最新可用資源與完整缺口見 [DATA_SOURCES.md](DATA_SOURCES.md)、[PROJECT_STATUS.md](PROJECT_STATUS.md)，安全範圍見 [SECRETS.md](SECRETS.md)。

## 自動驗證

```text
LOKY_MAX_CPU_COUNT=1 .venv/bin/python -m pytest -q
76 passed in 11.47s

.venv/bin/python -m compileall -q src tests
exit 0

.venv/bin/python -m pip check
No broken requirements found.

Dashboard JavaScript syntax: OK
```

測試覆蓋不可覆寫快照、檢查碼、還原價格對齊、時區與收盤時間下限、PIT 修正版本、未來資料不改變過去特徵、日期分組與成熟標籤隔離、獨立校準、缺失未來答案與無法成交時保留現金、交易成本、估值分支、預測冪等、來源混用拒絕、重播排除、盤前／盤後推論與最終保留期。

## 端到端示範

```text
.venv/bin/python -m twquant demo
.venv/bin/python -m twquant --root artifacts/demo holdout
```

合成行情包含十檔目標與0050，共800個平日曆日期。示範先在較早截止時間訓練／預測，再結算1／5／20日各10筆結果，最後重新訓練並產生30筆最新預測。

| 項目 | 結果 |
|---|---:|
| 帳本預測總數 | 60 |
| 已到期結果 | 30 |
| 最新待驗證預測 | 30 |
| 可計入真實逐日影子實績 | 0 |
| 正式可行性 | 未通過 |
| 合成資料最終保留測試 | 未通過，依實際指標保留失敗結果 |

目前最新示範產物由 `artifacts/demo/current.json` 指向。各 `runs/<版本>/report.json` 與 `holdout_report.json` 保存完整指標；不把合成資料結果解讀為真實投資表現。

## 瀏覽器驗證

使用 Playwright 開啟本機介面、切換20交易日期間、點選個股查看估值及線性基準解釋，並保留畫面於 `output/playwright/`。儀表板直接讀取持久化預測與回測產物。

本機服務指令：

```bash
.venv/bin/python -m twquant --root artifacts/demo serve --port 8000
```

## 真實資料邊界

已以最小請求驗證 FinMind 原始行情可讀取；無權限的還原行情請求會被拒絕，系統不以原始價格或合成資料代替。資料會員權限及當時方案以 [FinMind 官方技術文件](https://finmind.github.io/tutor/TaiwanMarket/Technical/)與帳號實際回應為準。

真實樣本外成績、最終保留測試與連續每日影子預測都尚未執行。完整待補項目及驗收條件見 [GAPS.md](GAPS.md)。
