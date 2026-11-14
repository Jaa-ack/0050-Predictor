# 將10交易日試驗移到常駐Linux主機

**可以讓行情收集與評分不依賴Mac在線。** 使用者於2026-11-15選擇沿用既有Google Cloud主機。程式已安裝到該機的獨立目錄，完成無憑證Linux與低權限服務測試；三個timer仍停用，尚未移入富邦金鑰或憑證、登入富邦、接收真實行情或切換Mac排程。主機選擇不等於憑證搬移同意。

既有主機為美國地區e2-micro、Debian12、Python3.11.2，約1GB記憶體；初次檢查可用約600MB、磁碟剩餘約23.5GiB、NTP同步且負載低。它已有其他服務，這些初始數值不是整段期間的容量保證。本輪未新增VM、磁碟或付費方案，也未改動其他服務；既有計費及網路額度仍適用。

## 建議執行方式

一般主機可用Ubuntu24.04／Python3.12；既有主機直接使用已測試的Debian12／Python3.11，不升級系統Python。程式、SDK與套件放在獨立venv，以一般使用者執行，持久化磁碟保存SQLite與報告，SSH取回分析。2vCPU／4GiB RAM仍是較寬裕的初步規畫；沿用小主機時使用下述限制，實際尖峰行情量與網路延遲尚待連線驗證。

## 既有小主機的收集設定

[設定](../configs/live_pilot_existing_host.json)將雲端收集窗口定為09:30～10:15，預測仍為每天10:00全50檔，60／300秒期限、股票池、模型版本、回退先驗與完整分母均不變。這是明列的資料取得窗口變更，並非原Mac08:45～13:40全時段收集。雲端協定另存版本及原協定摘要，不能混稱兩者收集範圍相同。

目前反彈規則只讀已收到的最後成交及最佳買賣報價，輸入年齡上限60秒；09:30開始提供初始快照及預熱，10:15結束涵蓋發報後最長300秒及額外收件時間。沒有補造被省略時段的歷史；薄股、缺輸入或未知答案仍完整保留。這不是供後續全日流量模型使用的完整交易日資料集。

專用systemd slice將本試驗的程序合計限制於512MiB，其他既有服務不加入該slice。報告在09:58～10:06延後執行，減少與發報競爭。收集監督程序在窗口結束時停止子程序，磁碟可用量低於4GiB時也會停止收集並記錄`storage_reserve_reached`，不刪資料、不購買容量、不偽造缺失答案。容量仍可能不足，這些限制保護共用主機，不能保證行情零缺漏。

既有Debian的Python沒有ensurepip。安裝器可選Python3.11，並以官方pip26.0 wheel在私人venv引導，SHA為`98436feffb9e31bc9339cf369fd55d3331b1580b6a6f1173bacacddcf9c34754`；不需安裝或升級系統Python套件。

實際安裝也驗證了root的0077遮罩會使程式目錄無法由一般服務帳號讀取。安裝器只將公開`app/`與`venv/`調整為可讀取／執行，私人runtime、log與憑證目錄維持0700，秘密檔案仍要求0600。修正後以服務帳號成功載入原生SDK，並在512MiB群組內執行狀態查詢。

systemd語法驗證已通過。三個新服務在非交易日以短暫空白設定逐一執行，均正常退出；收集程序因日期窗口而未啟動券商連線，報告顯示0個成熟日期。空白設定已移除，雲端沒有`.env`或憑證檔。這是程式／服務環境驗證，不是實際行情接收驗收。

可先評估既有主機；若需新主機，應先選地區、磁碟與預算，再建立。亞洲地區值得優先測試，但雲端地點不等於已量測延遲保證。不採可被任意搶占的主機來承諾完整覆蓋。原有靜態網站預覽沒有行情後端；本次方案是獨立執行程序及私人資料庫。

[富邦官方SDK頁](https://www.fbs.com.tw/TradeAPI/docs/download/download-sdk/)列出Linux與Python3.8～3.13支援。為避免在遷移時更換SDK，本次另外從官方載點取得與本機相同的2.4.0 Linux x86-64 wheel，SHA256為`065c636ea58e00db9fe67aa6ba0936b234ac131da0d4697e9c6f33dbe42bc1ba`。已在目標主機成功載入原生SDK；套件不進Git或公開部署包。尚未使用真實憑證在雲端登入或接收盤中行情，沒有自動升級SDK。

2026-11-15的獨立環境測試使用Python3.11.2、pandas3.1.1、SciPy1.17.1及SDK2.4.0，17項核心測試全部通過。1萬筆合成寫入約4.98秒，約2010筆／秒，單程序最高RSS約191MiB。這不包含真實SDK登入、網路接收、尖峰五檔訊息或多程序同時運行，不是市場吞吐量或新鮮度保證。合成資料用完即移除，沒有當成真實行情樣本。

費用應按實際主機報價核對。例如[DigitalOcean公開價目](https://www.digitalocean.com/pricing/droplets)列出的2vCPU／4GiB／80GiB方案為US$0.03571／小時、月上限US$24；連續14天的主機基礎項目約US$12。這是比較用例，不是Google Cloud報價，也未包含額外儲存、備份、流量、稅費或方案差異。試驗結束停用程序不等於刪除主機或停止磁碟計費，不會替使用者刪除已有其他服務的主機。

## 可重現的部署準備

```bash
.venv/bin/python scripts/prepare_cloud_pilot.py --root artifacts/live-pilot-20261116-10d --out artifacts/cloud-pilot-preparation/new-bundle
.venv/bin/python scripts/prepare_cloud_pilot.py --root artifacts/live-pilot-20261116-10d --out artifacts/cloud-pilot-preparation/debian-bundle --python python3.11 --memory-max 512M
```

準備程序先驗證原本凍結來源的完整SHA，再產生：

- `source.tar.gz`：只包含凍結Python來源、公開套件metadata與授權文字。
- `manifest.json`：逐檔與封裝SHA、SDK版本與SHA、日期及尚未啟用的狀態。
- `units/`：三個service、三個timer，以及僅停用本次工作名稱的期末收尾service。
- `install.sh`：供核准的Linux主機安裝；驗證封裝及SDK的SHA，不覆寫既有部署，且不複製憑證、不啟用timer。
- `finish.py`：只有期末報告完成才建立停止標記。
- `resource_guard.py`與選用slice：小主機的窗口監督、磁碟保留量及報告延後；不修改凍結模型來源。

包內不含`.env`、憑證、金鑰、私人協定、模型二進位、行情、SQLite、SDK日誌或SDK安裝檔。原本的模型來源摘要、股票池、10個日期、先驗回退與60／300秒定義維持不變。部署工具位於`scripts/`，不改寫凍結模型來源或原來的Mac執行快照。

## 主機核准後的步驟

1. 核實主機資源、持久化磁碟、對時與出站網路；保留既有服務，準備專用作業目錄與使用者。新建資源前先核對費用。
2. 將無憑證部署包與官方Linux wheel傳到指定主機。系統需提供`python3.12`及venv；安裝器建立`/opt/twquant-pilot`，單元維持未啟用。
3. 在得到明確授權後，以SSH設置原始試驗協定及必要的富邦連線資料。僅使用必要的富邦身分、API Key、憑證密碼與憑證，不搬整份包含其他供應商的Mac `.env`。不可透過聊天或網站表單提供秘密值。
4. 憑證只放`/opt/twquant-pilot/.secrets/fubon/`，目錄0700、檔案0600；`.env`由執行使用者擁有且為0600，憑證路徑改成雲端私人路徑。來源程式唯讀，行情與SDK原生日誌只寫私人可寫目錄。
5. 在雲端執行唯讀`--mode probe`，確認全部105訂閱、來源SHA、對時與輸出權限。訂閱確認不是逐成交完整性證明，首個盤中接收仍需核對序號與延遲。
6. 驗證獨立發報及報告程序後啟用三個timer，再停用Mac同一試驗的工作。避免在10:00～10:05評分窗口切換。未完成切換前，不能宣稱Mac可離線而仍有完整真實驗證。

目前既有主機已完成未啟用安裝，無須重跑安裝器；它會拒絕覆寫既有部署。以下安裝命令供新目錄重建使用，缺ensurepip的主機需附上已驗證pip wheel。probe及timer啟用仍須先取得憑證移轉授權並完成私人設定：

```bash
sudo bash install.sh /private/path/to/official-linux-sdk.whl /private/path/to/pip-26.0-py3-none-any.whl
sudo -u twquant-pilot sh -c 'cd /opt/twquant-pilot && venv/bin/python -m twquant.live_market --root runtime --mode probe'
sudo systemctl enable --now twquant-pilot-collect.timer twquant-pilot-forecast.timer twquant-pilot-report.timer
```

安裝器不提供建置公開HTTP服務或收取憑證的表單。已在目標主機完成獨立venv、原生SDK載入、合成測試及非交易日服務啟動，並放置不含秘密的雲端試驗協定；實際行情登入、105筆訂閱及盤中運行仍未驗收。須待明確准許將必要的富邦身分、API Key、憑證及憑證密碼存入指定私人目錄，才能進行下一步。

## 排程與結束行為

每個正式日期均寫成含年份的Asia/Taipei排程：一般設定08:45開始收集，選用小主機監督設定時在協定窗口09:30開始；09:59預熱等待10:00發報，13:45產生日報。收集程序退出後每60秒檢查重啟，報告程序每300秒檢查；小主機監督也會拒絕窗口以外的啟動。這些systemd單元不呼叫Mac的caffeinate或launchctl。

timer使用`AccuracySec=1us`及`RandomizedDelaySec=0`，但[systemd文件](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)指出實際觸發仍受系統排程影響；因此發報程序提前1分鐘啟動，並保留實際發布封印與晚發報懲罰，未把設定精度當成發報保證。

11/27期末評估完成後，停止標記會使收尾service停用本次三個timer及收集／發報service。這只結束應用程式工作，不關機、不刪除主機、不移除私人資料，也不影響其他應用程式。仍需按主機是否專用及資料保留需要決定何時停止雲端資源計費。

報告仍為私人`runtime/evaluation.md`與`runtime/evaluation.json`，包括缺報、未知、晚發報、逾時及逐股／逐日明細。原始10日統計限制及供應商成交片段完整性限制不因搬到雲端而消失，見[試驗協定](LIVE_PILOT_10D.md)。
