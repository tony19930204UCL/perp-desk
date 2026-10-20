# C0112 多臂 PAPER 唯讀觀察台

本功能是未部署的獨立本機觀察頁面；不取代或修改 D2。只能顯示模擬資料，並非即時交易控制面板，不提供寫入、下單、撤銷或平倉。資料過期仍可展示最近快照但標示 STALE，資料不可讀時標示未知，禁止將未知當作零。

## 操作指令

在包含 `.git` metadata 的指定 PR checkout 中，先由操作員確認來源目錄及分支（不要改動紙盤檔案）。啟動唯讀服務：

```bash
python3 lab/dashboard_multiarm_server.py --data-root "$(pwd)/lab/data" --port 18771 --stale-after-s 90
```

僅監聽 `127.0.0.1:18771`；不得使用 18767、18769、8766。以本機瀏覽器開啟 `http://127.0.0.1:18771/`。所有輸入來源是固定 allowlist 下的 `broker.sqlite3`，不是 URL 中的路徑。

確定性 unit suites：

```bash
python3 -m unittest discover -s automation/tests
cd lab && python3 -m unittest discover -s tests -v
```

選用真實 Chromium 驗收（預先安裝 Chromium；不得在程式執行時下載）：

```bash
python3 lab/browser_dashboard_multiarm_acceptance.py --expected-head "$(git rev-parse HEAD)" --browser /usr/bin/chromium --output artifacts/multiarm-browser
```

此命令必須在具有 `.git` 的 checkout 執行；匯出但沒有 `.git` 的樹無法證明 checkout HEAD。Browser acceptance 非 CI unittest，報告 `report.json` 有每項 PASS/FAIL/NOT_RUN/TIMEOUT 與執行時間、網路 trace、原始 SHA256/mtime、兩張 1280x800 與 390x844 截圖、清理狀態與 exit code。失敗或未執行不得推定 PASS。

## 固定臂名稱

`h2 → paper-v4`；`f1h-{btcusdt,solusdt,xrpusdt}`；`w1-{btcusdt,ethusdt,solusdt,xrpusdt}`；`t1-{btcusdt,ethusdt,solusdt,xrpusdt}` 分別映射到同名 `paper-` 資料夾。排除 `paper`、`paper-v2`、`_void_windows`。

## 欄位到來源

| 畫面 | 原始欄位或衍生式 | 範圍及限制 |
|---|---|---|
| 初始現金 | `initial_cash` | exists, Decimal string |
| 現金 | `cash` | exists, Decimal string |
| 帳戶權益 | cash + Σ qty × (mark − entry) | derived; 缺 mark 則未知 |
| 未實現損益 | Σ qty × (mark − entry) | derived; signed qty |
| 開倉部位 | `positions` + `marks` | exists; SHORT 使用負 qty |
| 完整平倉 | `fills` flat-to-flat episodes | derived; partial close 不算 completed |
| 淨利損 | realized + fee + funding | derived; 歸屬不明應標未知 |
| 待處理委託 | `orders` PENDING/RESTING | exists |
| 委託歷史 | `orders` | exists; 其他狀態未知 |
| 成交歷史 | `fills` | exists |
| 現金核對 | initial_cash + Σ ledger amount | derived |
| 保證金、可用保證金、強平價、ROE、cross/isolated | 無 | 未建模，禁止推算 |
| 年齡 / STALE | `last_ts` / --stale-after-s | derived；不是服務存活訊號 |

設計取捨：固定單一 HTTPServer、單次請求讀取全部十二臂，以減少多執行緒並避免 URL 選擇 SQLite 路徑。僅執行 mode=ro SQLite SELECT，query_only 開啟，短 busy timeout；對可能有 WAL/journal 的檔案採保守的 UNKNOWN。每表最多回傳最新 500 列，顯示總數及截斷註記；不傳 `seen_events`。UI 深色高密度、可水平捲動、綠紅損益與五個表格分頁；15 秒自動刷新並保留臂與分頁選擇，值以字串保留原始精度作 tooltip。沒有外部字體或 JavaScript CDN。

## 真實瀏覽器驗收探針

`MA-BROWSER-POPULATED-1280-390`、`MA-BROWSER-ALL-TABS`、`MA-BROWSER-UNKNOWN-VS-ZERO`、`MA-BROWSER-HOSTILE-TEXT`、`MA-BROWSER-STALE-FETCH-CLEARING`、`MA-BROWSER-NO-EXTERNAL-REQUESTS`、`MA-BROWSER-READONLY`。操作員須檢查真實檔案副本及所有探針 evidence，不能以本文件或 unit CI 取代功能與視覺驗收。

## 已知限制

本次新增來源與測試尚待 Main CIO 獨立執行、比對真實資料及 Chromium。無法確認的 ledger attribution、zero-cross 或缺欄位需要保守呈現；不可把目前合成資料當成真實紙盤帳戶證據。歷史來源來自單一 broker snapshot，不提供真正 live positions、外部行情或風險計算。原先 D2、紙盤服務與使用者視覺驗收各自獨立。
