# 開發與更新

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,publish]'
python -m twquant secrets init
pre-commit install
```

真實Key只填入本機.env，權限0600。貢獻時以合成資料測試；不得提交供應商原始資料、模型、憑證或帳戶資訊。

每個行為修改先建立可重現的失敗測試，完成後執行：

```bash
LOKY_MAX_CPU_COUNT=1 python -m pytest -q
node --test tests/web_state.test.mjs
python -m twquant secrets audit
```

更新相應文件與CHANGELOG，再以清楚的commit記錄問題、結果與驗證。建議使用 `feat:`、`fix:`、`docs:`、`test:`、`chore:` 等前綴。避免把重新產生的本機資料或無關變更混入提交。向main推送前，確認遠端沒有新增紀錄；不使用force push。

UI共享在 `src/twquant/static/`。本機模式連接FastAPI；線上快照模式讀取匯出的合成JSON並停用執行操作。修改後須同時檢查兩種模式、空資料／錯誤狀態與行動版。

依賴更新由Dependabot按設定建立PR；不自動合併。完整發布前另跑Git歷史掃描，並確認資料來源授權。程式碼的正式開源授權尚待維護者選定，公開可瀏覽不代表可再散布供應商資料。
