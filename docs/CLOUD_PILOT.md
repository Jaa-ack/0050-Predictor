# 將10交易日試驗移到常駐Linux主機

**可以讓行情收集與評分不依賴Mac在線。** 本次已準備Linux部署包與systemd排程，尚未建立雲端資源、上傳富邦憑證、修改既有雲端服務或切換本機排程。實際遷移需先確認目標主機及憑證存放授權。

目前的Google Cloud CLI已有可用登入與專案；唯讀檢查發現一台執行中的e2-micro主機，位於美國且已有其他服務。尚未登入該主機、確認剩餘記憶體／磁碟或改動它。既有主機不代表新增工作必然免費或容量足夠；計費查詢回應429，未宣稱已核實新增資源預算。

## 建議執行方式

使用Ubuntu24.04、Python3.12的常駐Linux x86-64主機，以一般使用者執行唯讀行情程序、持久化磁碟保存SQLite與報告，SSH取回分析。2vCPU／4GiB RAM是初步容量規畫，需依實際尖峰行情負載量測；全時段52股票／ETF的五檔更新量尚未量測，不能保證某個固定磁碟大小足夠。

可先評估既有主機；若需新主機，應先選地區、磁碟與預算，再建立。亞洲地區值得優先測試，但雲端地點不等於已量測延遲保證。不採可被任意搶占的主機來承諾完整覆蓋。原有靜態網站預覽沒有行情後端；本次方案是獨立執行程序及私人資料庫。

[富邦官方SDK頁](https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/)列出Linux與Python3.8～3.13支援。為避免在遷移時更換SDK，本次另外從官方載點取得與本機相同的2.4.0 Linux x86-64 wheel，SHA256為`065c636ea58e00db9fe67aa6ba0936b234ac131da0d4697e9c6f33dbe42bc1ba`。套件保留在忽略的準備目錄，不進Git或公開部署包；尚未在目標Linux主機執行它的登入／盤中接收測試。官方最新版本不同，沒有因此自動升級。

費用應按實際主機報價核對。例如[DigitalOcean公開價目](https://www.digitalocean.com/pricing/droplets)列出的2vCPU／4GiB／80GiB方案為US$0.03571／小時、月上限US$24；連續14天的主機基礎項目約US$12。這是比較用例，不是Google Cloud報價，也未包含額外儲存、備份、流量、稅費或方案差異。試驗結束停用程序不等於刪除主機或停止磁碟計費，不會替使用者刪除已有其他服務的主機。

## 可重現的部署準備

```bash
.venv/bin/python scripts/prepare_cloud_pilot.py --root artifacts/live-pilot-20261116-10d --out artifacts/cloud-pilot-preparation/new-bundle
```

準備程序先驗證原本凍結來源的完整SHA，再產生：

- `source.tar.gz`：只包含凍結Python來源、公開套件metadata與授權文字。
- `manifest.json`：逐檔與封裝SHA、SDK版本與SHA、日期及尚未啟用的狀態。
- `units/`：三個service、三個timer，以及僅停用本次工作名稱的期末收尾service。
- `install.sh`：供核准的Linux主機安裝；驗證封裝及SDK的SHA，不覆寫既有部署，且不複製憑證、不啟用timer。
- `finish.py`：只有期末報告完成才建立停止標記。

包內不含`.env`、憑證、金鑰、私人協定、模型二進位、行情、SQLite、SDK日誌或SDK安裝檔。原本的模型來源摘要、股票池、10個日期、先驗回退與60／300秒定義維持不變。部署工具位於`scripts/`，不改寫凍結模型來源或原來的Mac執行快照。

## 主機核准後的步驟

1. 核實主機資源、持久化磁碟、對時與出站網路；保留既有服務，準備專用作業目錄與使用者。新建資源前先核對費用。
2. 將無憑證部署包與官方Linux wheel傳到指定主機。系統需提供`python3.12`及venv；安裝器建立`/opt/twquant-pilot`，單元維持未啟用。
3. 在得到明確授權後，以SSH設置原始試驗協定及必要的富邦連線資料。僅使用必要的富邦身分、API Key、憑證密碼與憑證，不搬整份包含其他供應商的Mac `.env`。不可透過聊天或網站表單提供秘密值。
4. 憑證只放`/opt/twquant-pilot/.secrets/fubon/`，目錄0700、檔案0600；`.env`由執行使用者擁有且為0600，憑證路徑改成雲端私人路徑。來源程式唯讀，行情與SDK原生日誌只寫私人可寫目錄。
5. 在雲端執行唯讀`--mode probe`，確認全部105訂閱、來源SHA、對時與輸出權限。訂閱確認不是逐成交完整性證明，首個盤中接收仍需核對序號與延遲。
6. 驗證獨立發報及報告程序後啟用三個timer，再停用Mac同一試驗的工作。避免在10:00～10:05評分窗口切換。未完成切換前，不能宣稱Mac可離線而仍有完整真實驗證。

以下命令只在已核准且完成私人設定的主機上執行：

```bash
sudo bash install.sh /private/path/to/official-linux-sdk.whl
sudo -u twquant-pilot sh -c 'cd /opt/twquant-pilot && venv/bin/python -m twquant.live_market --root runtime --mode probe'
sudo systemctl enable --now twquant-pilot-collect.timer twquant-pilot-forecast.timer twquant-pilot-report.timer
```

安裝器不提供建置公開HTTP服務或收取憑證的表單。初始化協定、憑證與資料庫權限需先完成，不能只啟用空服務就稱已上線。尚未在Linux主機實際跑過上述安裝器，因此本輪驗證是封裝、排程生成及shell語法檢查，不是已通過雲端運行驗收。

## 排程與結束行為

每個正式日期均寫成含年份的Asia/Taipei排程：08:45收集、09:59預熱等待10:00發報、13:45產生日報。收集程序退出後每60秒檢查重啟，報告程序每300秒檢查；既有程序仍會拒絕宣告日期及時段以外的收集。這些systemd單元不呼叫Mac的caffeinate或launchctl。

timer使用`AccuracySec=1us`及`RandomizedDelaySec=0`，但[systemd文件](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)指出實際觸發仍受系統排程影響；因此發報程序提前1分鐘啟動，並保留實際發布封印與晚發報懲罰，未把設定精度當成發報保證。

11/27期末評估完成後，停止標記會使收尾service停用本次三個timer及收集／發報service。這只結束應用程式工作，不關機、不刪除主機、不移除私人資料，也不影響其他應用程式。仍需按主機是否專用及資料保留需要決定何時停止雲端資源計費。

報告仍為私人`runtime/evaluation.md`與`runtime/evaluation.json`，包括缺報、未知、晚發報、逾時及逐股／逐日明細。原始10日統計限制及供應商成交片段完整性限制不因搬到雲端而消失，見[試驗協定](LIVE_PILOT_10D.md)。
