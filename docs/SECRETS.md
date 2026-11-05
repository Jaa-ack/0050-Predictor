# 開源專案的Key與憑證保護

以 `secrets init` 建立私人 `.env` 的空白欄位後，請只在本機編輯；不透過聊天、issue、PR、截圖、前端欄位或公開JSON傳送。儲存庫只包含空白 `.env.example`。

## 儲存與讀取

- `.env.example` 可以提交，值保持空白；`.env`、`.env.*`、私人憑證、副本與日誌由 `.gitignore` 排除。
- `.env`由程式以0600建立；讀取時拒絕symlink、其他擁有者或群組／其他人可讀的檔案。已存在檔案不會被初始化命令覆寫。
- 富邦憑證以`secrets import-cert --from /absolute/path/exported.p12`複製至`.secrets/fubon/client.p12`（亦支援.pfx），資料夾0700／檔案0600，不覆寫既有檔；匯入不等於已驗證PKCS#12密碼、效期或登入。將私人.env的`FUBON_CERT_PATH`設為複本絕對路徑；原始下載檔與備份也需保護，詳見[CERTIFICATES_AND_COSTS.md](CERTIFICATES_AND_COSTS.md)。
- 環境變數優先於.env；值包成 Pydantic `SecretStr`，普通 `repr`／`str` 只顯示遮罩，不能直接序列化為JSON。
- Key只在伺服端網路／SDK邊界解封。前端與 `/api/sources` 只收到設定狀態，不會拿到Key。
- 本機.env是**受作業系統權限保護的明文檔**，不是加密保險箱。正式部署應由OS Keychain或雲端Secret Manager注入環境，啟用磁碟加密；此版未實作遠端Secret Manager連接器，也不把「Base64編碼」視為加密。

## 網路、日誌與產物

- 來源固定為經核對的HTTPS主機，拒絕任意網址、非HTTPS與自動redirect；不關閉TLS驗證。
- 支援的認證優先放Header；FRED／FMP官方參數要求query key時，僅送到允許主機，原始存檔不保留值。
- 原始回應、來源參數及錯誤訊息遞迴遮罩；連provider意外回傳的明文／URL編碼Key也遮罩。網路／SDK錯誤不附帶可能含認證URL／帳戶物件的原始例外鏈。
- 常見HTTP／SDK logger設為WARNING並套用遮罩；不輸出登入結果、帳戶姓名、帳號、庫存或損益。不要自行開啟HTTP wire debug或在除錯器擷取全部區域變數後公開。
- 富邦原生SDK會另寫`log/`輪替檔，副檔名可能是日期，並不經Python logger遮罩。建立SDK前先限制目錄0700並收緊既有檔案；整個目錄由Git／Docker／sdist排除，強制追蹤會被稽核拒絕。這些原生日誌可能含認證／帳號資訊，不應直接附加至issue、聊天或支援信件；只提供固定錯誤碼與環境摘要。
- 原始資料、模型、快照與日誌位於被忽略的工作目錄；sdist／wheel及Docker context另外排除.env、憑證及資料產物。
- 本機伺服器只綁127.0.0.1，限制Host、停用快取、限制框架嵌入與首頁Script CSP。沒有透過Web寫入Key或下單的路由；遠端部署仍須另加登入及權限控制。

## 開源前檢查

```bash
.venv/bin/python -m twquant secrets init
.venv/bin/python -m twquant secrets audit
.venv/bin/python -m pip install -e '.[dev,publish]'

# 初始化Git之後、在已啟用專案venv的終端執行：
pre-commit install
pre-commit run --all-files
```

內建掃描器只輸出檔案／行號／規則，**不顯示匹配到的秘密**；會檢查已被強制加入Git的.env，新增ignore規則不能掩蓋已追蹤的檔案。它是工作目錄的檢查，不代替完整Git歷史掃描。

未追蹤的`.secrets/`與`credentials/`私人檔案不屬於發布集合；被強制追蹤的憑證仍會阻擋。Hosting管理工具回應可能包含存取權杖或簽名網址，須先採明確欄位允許清單，不能整份輸出到聊天或日誌。

掃描器也拒絕已追蹤的資料／產物目錄、模型與資料庫，即使二進位檔無法以文字掃描。提交 hook 使用專案 venv 執行，不需要把真實Key寫進CI。線上靜態匯出只接受合成行情及預測，移除機器路徑與來源設定，所有操作按鈕停用。本機寫入任務限定固定動作、工作區、同源及 session nonce，單一任務執行；這不是可公開綁定的遠端管理服務。

`.github/workflows/security.yml` 已加入Gitleaks完整歷史掃描並停用包含細節的PR留言／掃描產物上傳。2026-11-05首次公開提交前的本機歷史掃描，以及[GitHub安全CI](https://github.com/Jaa-ack/0050-Predictor/actions/runs/41940834552)均通過。組織帳號使用該Action可能需要 `GITLEAKS_LICENSE`，放GitHub Secrets，勿寫進設定檔。[Gitleaks官方說明](https://github.com/gitleaks/gitleaks-action)

若曾經洩漏，先到供應商撤銷／輪替該Key，再清理Git歷史與外部副本；只刪除檔案或補.gitignore無法撤回既有Key。資料授權與程式碼授權分開，開源發布不包含你的Key、憑證、付費金融資料、新聞全文或模型產物。
