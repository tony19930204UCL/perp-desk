# F1H 家族預先登記：BTCUSDT、SOLUSDT、XRPUSDT 閉合 1 小時做多（候選，尚未部署，尚未開窗口）

登記：2026-10-10 UTC，由交易台經營者在看到這三個標的任何前瞻資料之前寫下。父版本：H2-PAPER-004（lab/PAPER_V4_PREREGISTRATION.md）。僅紙盤。不暗示任何實盤授權。

## 為什麼
使用者要求加速。H2 單臂預估 21 天只有 15 到 25 個來回。規則用各標的自己的波動度標準化，所以信號頻率與標的無關。把同一規則複製到更多標的是不降低證據標準的最快加速方式。

## 家族定義
F1H = 閉合 1 小時 K 線，前 60 筆報酬，當根報酬低於均值減 1.5 個樣本標準差，且成交量大於前 60 根中位數的 1.2 倍，做多，目標為前一根閉合收盤。成員：ETHUSDT (H2，已在跑)、BTCUSDT、SOLUSDT、XRPUSDT。

## 標的怎麼選（規則，不是績效）
依長期流動性與穩定性選大型永續合約，並排除近期出現異常量能的小型標的。10-10 篩選（1,000 根 1 小時 K 線，約 41 天，只數頻率不看盈虧）：

| 標的 | 1 小時報酬標準差 | 信號數 | 過 2 倍成本門檻 | 最小名目 |
|---|---|---|---|---|
| BTCUSDT | 0.361% | 55 | 55 | 50 |
| ETHUSDT | 0.480% | 49 | 49 | 20 |
| SOLUSDT | 0.583% | 54 | 54 | 5 |
| XRPUSDT | 0.698% | 58 | 58 | 5 |

## 凍結參數
- 規則參數、風險契約 PAPER-RISK-001、成本與時效門檻、執行模型：與 H2 完全相同。
- 最長持有 12 根閉合 1 小時 K 線。
- 窗口 21 天，審查目標 30，各自從各自部署時刻起算。
- **止損距離用與 H2 相同的原則：約 2 倍該標的 1 小時報酬標準差，四捨五入到 0.05%，在登記時凍結為常數**：BTCUSDT 0.70%，SOLUSDT 1.15%，XRPUSDT 1.40%（H2 的 ETH 為 1.00%）。理由：用固定 1.0% 對 BTC 等於 2.8 倍標準差，對 XRP 只有 1.4 倍，會讓同一個假說在不同標的意義不同。
- 每個標的獨立帳戶 100 USDT，獨立狀態目錄，獨立窗口，不合併損益。

## 家族層級的判讀（主要指標）
- 主要指標：**叢集去重後的有效來回數**與全成本淨期望。去重規則：同一個 6 小時叢集內，跨標的只算第一個來回。原始來回數與各標的結果一律完整報告，不得只報好的。
- 各標的高度相關（1 小時尺度），不是獨立證據。

## 反方
- 四個標的在大跌時同時觸發，一次行情可能同時造成多筆虧損。
- 止損比例由登記時的波動度推得，行情情境改變後可能不合適。
- 大型標的目標距離較小，邊際較薄，BTC 尤其如此。
- 多個標的加多個窗口，增加多重檢定的假陽性。

## 失效與停止條件
- 與 H2 相同：累計淨損達總損上限 10 USDT 停。窗口作廢條件：連續來源缺口超過 3 小時，或輪詢錯誤率超過 5%，或工程修補改變規則或成本模型。
- 家族層級的「不支持」：去重後至少 15 個來回且全成本淨期望為負，或勝率低於 45%。結論只針對這個家族版本，不事後調參。
- 早期損益不能結束或延長窗口。

## 需要的工程（交外部 agent，我驗收）
把 1 小時偵測器與執行環境的標的參數化（symbol 與止損比例由設定檔給），用新檔案實作，**不修改**已部署的 signals_v4.py、paper_runtime_v4.py、paper_config_v4.json（它們的來源雜湊記在 H2 的狀態裡）。每個標的一個服務實例，各自狀態目錄與狀態檔。

## 不宣稱
沒有優勢。沒有實盤。H2 與舊帳戶不動。

## 部署記錄（2026-10-10 晚，在任何 F1H 前瞻資料產生之前寫下）

實作與驗證（工程由外部 agent 完成，我獨立驗收，細節見 cache/scratch/relay-f1h-*/main-review-*.json）：
- 偵測器 lab/signals_v5.py（繼承 signals_v4，單一標的綁定）。驗收時發現並修正一個真缺陷：seed() 的 super() 在 v5 繼承鏈解析到會套用決策截止點的版本，造成暖機 K 線被拒。修正後 seed 與 v4 完全一致（差分測試）。
- 執行環境 lab/paper_runtime_v5.py，三份設定檔 lab/paper_config_f1h_{btcusdt,solusdt,xrpusdt}.json。與 H2 設定只差三個欄位：version_id、strategy.symbol、strategy.stop_distance_fraction（0.0070、0.0115、0.0140）。decision.reason 的說明文字仍寫 ETHUSDT，不影響行為。
- 真實行情乾跑抓到第二個缺陷：公開行情客戶端 perp_collector.PublicClient 把允許標的寫死為 ETHUSDT 與 XAUUSDT。修正為類別屬性 ALLOWED_SYMBOLS，預設值不變（H2 行為不變，已用 v4 乾跑確認），F1H 的客戶端子類放寬為再加 BTCUSDT、SOLUSDT、XRPUSDT。perp_collector.py 位元組因此與 H2 啟動時不同，H2 的報告雜湊會在 H2 下次重啟後反映這點。這不改規則也不改成本模型，不構成作廢條件。
- 與 H2 的差異（除標的與止損比例外）：F1H 不觀察 XAUUSDT（觀察本來就不交易，關掉避免無關的資料缺口影響加密貨幣部位）。其餘成本、時效、風控、執行模型相同。
- 簡報 lab/brief_learning_v4.py 讀 H2-PAPER- 與 F1H- 快照，窗口長度與目標從快照取，金額兩位小數。

測試證據：本機 test_signals_v5 15、test_paper_runtime_v5 22、test_brief_learning_v4 20、test_perp_collector_f1h_symbols 14 全過；分支 CI（pull_request 與 push）成功。乾跑（真實公開行情、獨立目錄）三個標的暖機 61/61、無錯誤、報價為各自真實價格。直接呼叫真實 route_signals：BTC、SOL、XRP 各自送出 BUY，止損比 0.9930、0.9886、0.9862。

已知限制：假環境走不到 poll() 的完整成交路徑（H2 的測試也是），成交與出場是分別直接測的。完整成交要等真實行情的前瞻窗口。

檔案雜湊（sha256 前 16 碼）：
- paper_config_f1h_btcusdt.json 6eb1b677f704fc12
- paper_config_f1h_solusdt.json 30acaf745d5ab353
- paper_config_f1h_xrpusdt.json ccf33eb79df94d69
- paper_config_v4.json（基準，H2 使用中）e27b4b4b05ec66a2
- signals_v5.py 5f80989424587316
- paper_runtime_v5.py 8de9b7ea9d5262bf
- perp_collector.py 7b24ed5ace9ee9e4

部署方式：三個 systemd 使用者服務 perp-paper-f1h-{btcusdt,solusdt,xrpusdt}，與 H2 相同的重啟與儲存保護設定。窗口起點為服務首次成功輪詢的時間（記錄在各自狀態檔的 research.strategy_start_ms），到期為起點加 21 天。中斷超過 3 小時作廢重開，不拼接。
儲存預估：每實例 21 天約 10 GB，低於 12 GiB 警戒線。公開 API 請求估算合計約每分鐘 400 權重（上限 2400），部署後以 429/418 回應次數實測。
