# 紙盤模擬券商存檔效率規格（PERSIST-1）

## 問題（已量測）
- 12 個紙盤服務合計寫入約 45 MB/s（約 3.9 TB/天），每個服務 2.7 到 5.4 MB/s。
- `lab/sim_broker.py` 的 `_save()` 把整個券商狀態序列化成單一一列 JSON（表 `sim_broker_state`，id=1），每個新的公開事件（`on_event`）提交一次。
- 這一列裡 `seen_events`（事件編號對完整事件內容）佔幾乎全部：W1-ETH 約 2.3 天後 27.1 MB、42,542 筆，每天約增 12 MB，只增不減。
- 在狀態副本上量：該列改動少量位元組再提交，每次寫入 18.1 MB。同一檔案的 runtime 狀態列（2.9 MB）更新一次寫 0.59 MB，audit 附加一列寫 0.025 MB。所以主因是 broker 這一列。
- `on_event` 還在每個事件前對全部狀態做 `deepcopy`（含 `seen_events`），CPU 與記憶體成本同樣隨時間增加。

## 成果（驗收條件）
1. 每個新事件的提交寫入量降到原來的 5% 以下，且不再隨 `seen_events` 增長而增加。量測方法：建立含 40,000 筆既有事件的狀態，再處理 20 個新事件，用 `/proc/self/io` 的 `write_bytes`（每次提交後 `os.sync()`）平均，每次提交須小於 1 MB。測試要印出量到的數字。
2. 行為完全不變（逐位元等價）：同一串確定性的事件與下單意圖，餵給舊實作與新實作，最後的 `cash`、`orders`、`positions`、`fills`、`ledger`、`audit`、`marks`、`funding_events`、`funding_status`、`day_baselines`、`last_ts` 全部相同。事件至少 2,000 筆，含成交、停損、資金費率事件。
3. 重複事件偵測不變：相同 id 且內容相同回傳不重複處理；相同 id 但內容不同拋 `ValueError('conflicting event ID')`；同時涵蓋「舊格式（完整事件內容）」與「摘要格式」兩種已存內容。
4. 舊狀態遷移：開啟一個舊格式的狀態檔（`seen_events` 在 JSON 裡）時，在單一交易內把每筆轉成摘要（SHA-256，與現行 `event_digest` 的算法完全相同）寫入新表，並從 JSON 移除該鍵；遷移前後重複偵測結果一致；遷移後再開啟不重做；遷移中途失敗整個回復，原檔不變。
5. 失敗回復不變：`on_event` 處理中拋例外時，記憶體與資料庫都回到事件前狀態（包含新表）。
6. 所有現有測試仍通過，不放寬、不刪除任何現有測試。

## 設計方向（可在不違反驗收的前提下調整）
- 新表 `sim_seen_events(event_id TEXT PRIMARY KEY, digest TEXT NOT NULL)`。記憶體只留 `event_id -> digest` 供偵測。每個新事件只 `INSERT` 一列，與其他狀態變更同一交易。
- `sim_broker_state` 的 JSON 不再含 `seen_events`。
- 取消對 `seen_events` 的 `deepcopy`；回復改成交易回滾加還原記憶體字典項。
- `compact_seen_events` 旗標保留相容：新實作永遠只存摘要，舊旗標為 False 時行為等同新格式（不能再把完整事件放進記憶體字典）。

## 範圍與限制
- 只改：`lab/sim_broker.py`、新增 `lab/tests/test_sim_broker_persistence.py`、交接檔。不改任何 `paper_runtime_*.py`、設定檔、信號檔。
- 基底：現行 T1 分支頭（包含所有已部署引擎檔）。這是基礎設施變更，不改任何策略與風控參數，不改已登記的窗口。
- `sim_broker.py` 變更後，狀態檔裡記錄的來源雜湊會跟著改。這要在部署時寫進 `lab/versions.md` 作為基礎設施變更紀錄，並註明等價測試的證據。
- 不部署、不重啟服務、不碰 main。部署由負責人在驗收通過後另行處理。

## 本機量測腳本
`scripts/probe_commit_bytes.py` 可在狀態的一致性副本（SQLite backup）上量測每次提交的寫入量，驗收時用它與測試輸出對照。
