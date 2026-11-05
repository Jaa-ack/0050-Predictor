# 真實歷史檢查與每日影子預測

## 能做的事與限制

本機系統可用真實原始／還原日線、FRED版本及美股市場序列進行歷史研究、保留期檢查、1／5／20日預測與到期評分。研究預設固定十檔及0050基準，尚未重建各歷史時點的前十大權重。下載成功不代表還原版本、歷史可用時間、資料授權或模型優勢已通過正式驗收。

`configs/research.json`只報告最近最多5個測試區段；`configs/research_history.json`上限64個區段，供目前歷史範圍逐段檢查。兩者皆先保留最後126個市場日期，較早資料用於依日期分組、成熟標籤隔離及時間校準。各折訓練期間是擴張窗口，不是把所有年份混在一起隨機分割。

## 首次取得與研究

```bash
.venv/bin/python -m twquant --root artifacts/live-research ingest --provider fugle --start 2014-01-01 --end 2026-11-04
.venv/bin/python -m twquant --root artifacts/live-research sources sync --plan configs/market_context_plan.json
.venv/bin/python -m twquant --root artifacts/live-research sources export-events
.venv/bin/python -m twquant --root artifacts/live-research research --config configs/research_history.json --events artifacts/live-research/sources/events.csv
.venv/bin/python -m twquant --root artifacts/live-research holdout
```

上述日期為本次固定驗收快照，後續依實際完整交易日更新。`holdout`只開封凍結的相同程式／設定／快照；再次呼叫只讀原結果。已看過相同日期的研究不能重新宣稱那段是從未接觸的樣本；修改策略後應累積新的未來驗證。

市場因子計畫只含長期FRED與選定美股序列，不把今天下載的財報、集保或新聞補造為過去已知資訊。FRED以St. Louis當地日限制版本結束日，並保守處理只有發布日期的可用時間；美股收盤依紐約時區及DST對齊。新週期公告與新聞可另外透過`configs/public_source_plan.json`累積，再做短期增強實驗。

## 每日執行

在當日臺股行情完整、**臺北時間20:00之後**依序執行：

```bash
.venv/bin/python -m twquant --root artifacts/live-research sources sync --plan configs/market_context_plan.json
.venv/bin/python -m twquant --root artifacts/live-research sources export-events
.venv/bin/python -m twquant --root artifacts/live-research daily --provider fugle --start 2014-01-01 --config configs/research_history.json --events artifacts/live-research/sources/events.csv --paper
```

只有前一步成功才執行下一步。`daily`取得新行情、結算已到期預測、建立新候選模型與校準，再保存當日30筆影子預測。它不下單，也不覆寫舊預測。未開市、欠最新行情、來源失敗或缺模型輸入時，依檢查阻擋／暫緩，不能把舊價當新價。

目前日線每次按完整歷史分段下載，並非增量更新；長期歷史回測也會重跑多折。可先按需使用較短的`configs/research.json`作工程循環，但會形成不同程序版本，不能混算同一個模型家族的實績。未完成增量下載、每日只重訓與定期完整研究分離前，不宣稱已最佳化營運效率。

現在沒有安裝背景排程。若要每天自動執行，還需常駐且不睡眠的主機、臺北時區排程、跨程序互斥、失敗告警及備份；範本在`configs/schedule.crontab.example`，不能直接把範本視為已啟用服務。

## 查看結果

```bash
.venv/bin/python -m twquant --root artifacts/live-research serve --port 8000
```

開啟<http://127.0.0.1:8000>，選「目前研究環境」。總覽讀取模型與門檻、歷史回測讀取樣本外曲線、個股預測讀取帳本、到期追蹤讀取成熟結果。完整封存結果與逐筆預測在本機`runs/<run_id>/`；歷史重播會標示`is_replay=true`，不計入至少60日／500筆成熟一日預測的真正影子門檻。

線上私人Site仍只含合成展示；不把真實行情、帳號權限、逐筆預測或模型上傳網站。API驗證可讀`artifacts/api-audit/sources/verification.json`，富邦獨立相容環境則讀`artifacts/fubon-current-audit/sources/verification.json`，都不進Git。
