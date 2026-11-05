# Key保護與開源安全檢查

更新日期：2026-11-05。範圍：Python／FastAPI本機研究系統、來源HTTP／富邦SDK、私人.env、日誌與錯誤、來源快照、瀏覽器回應、Git／套件發布排除、本機固定任務及合成託管頁面。私人服務帳號權限仍待實際驗證；本機Git歷史掃描及GitHub安全CI已通過。

## 已處理項目

| 編號 | 嚴重度／狀態 | 證據與修正 |
|---|---|---|
| SEC-001 | 高／已處理 | [credentials.py](src/twquant/credentials.py#L35) 檢查owner-only權限與symlink；值包成SecretStr，公開狀態只含布林；.env初始化不覆寫既有檔案 |
| SEC-002 | 高／已處理 | [credentials.py](src/twquant/credentials.py#L79) 遞迴遮罩明文及URL編碼Key；原FinMind原始回應的URL編碼回顯漏洞已加入失敗後通過的回歸測試；HTTP logger增加遮罩與例外細節抑制 |
| SEC-003 | 高／已處理 | [cli.py](src/twquant/cli.py#L231) 對結果／錯誤作最後遮罩，防止供應商或解析錯誤把Key回顯到終端；網路／SDK不攜帶原始認證例外鏈 |
| SEC-004 | 高／已處理 | [sources.py](src/twquant/sources.py#L96) 固定HTTPS來源主機、禁redirect、TLS維持啟用；FRED／FMP所需query key不寫入原始歸檔 |
| SEC-005 | 高／已處理 | .gitignore、MANIFEST.in、.dockerignore排除.env、憑證、原始金融資料與日誌；工作目錄掃描會找出已被強制加入Git的秘密檔；本機sdist與wheel已查無私人檔案 |
| SEC-006 | 中／已處理 | [api.py](src/twquant/api.py) 僅本機綁定、Host白名單、禁止快取、首頁CSP與禁止嵌入；來源介面只輸出設定狀態，測試確認不回傳Key；不提供Web寫Key／下單路由 |
| SEC-007 | 高／已處理 | 本機任務限定允許清單動作／工作區、nonce及同源；不用shell、不接受任意命令，一次只執行一項；輸出再次遮罩 |
| SEC-008 | 高／已處理 | `export-web`拒絕真實行情／預測；託管僅含合成JSON，金鑰、機器路徑及模型不進部署檔。靜態版六項任務停用，Site維持私人登入 |

## 保留限制與待完成項目

1. **中：本機.env是明文。** 目前以0600及檔案擁有者限制存取；沒有把它宣稱為加密儲存。正式環境應用OS Keychain／Secret Manager注入環境。Windows ACL尚未驗證，應使用環境注入或另行核對ACL。
2. **歷史掃描需持續執行。** 首次公開推送前Gitleaks 8.30.1已掃描完整1筆提交，無洩漏；GitHub安全CI亦通過。後續提交hook及CI持續檢查。掃描不能保證識別所有自訂秘密，若曾外洩仍須先撤銷／輪替Key，不能只刪檔。
3. **中：富邦官方SDK與實際行情Key權限待確認。** 只實作行情讀取，優先API Key登入，沒有存取庫存／交易；實際Key仍應由供應商限制為行情權限，SDK輸出與相容性需在安裝後再驗證。
4. **中：遠端部署尚未設計認證。** 本版只綁定127.0.0.1；部署到外網前必須另外完成身分認證、權限及部署秘密注入。模型joblib只載入自己產生的可信本機產物，SHA256不是第三方模型的信任或簽章保證。
5. **發布授權待選定。** 未替使用者選擇程式碼LICENSE；資料供應商／新聞的授權另行確認，不隨程式開源釋出原始資料或模型。

操作與輪替說明：[docs/SECRETS.md](docs/SECRETS.md)。
