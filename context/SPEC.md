# Perp Desk 規格書：影子交易引擎與策略研究

Sep 30, 2026 · @Chih-Cheng Chang

## 目標與設計原則

這個系統的第一階段目標是證明（或否證）某個策略在扣除手續費和 funding 後有正期望值，不是用 5 歐賺錢。資金規模跟著證據放大，不跟著槓桿放大。

**五條設計原則**

1. **可證偽優先。** 每個策略都要事先寫下「什麼結果代表它沒用」，跑到樣本數就判決。
2. **LLM 不持有私鑰，不直接下單。** LLM 只產出交易意圖或否決意見，下單由確定性的 Python 執行層負責。
3. **降風險可自動，加風險要人按。** 平倉、減倉、移動停損可以自動。開新倉在前 50 筆實單前一律要在 Telegram 核准。
4. **影子帳本永遠跑在實盤旁邊。** 實盤每一筆都有對應的模擬成交，兩者差距本身就是校準數據。
5. **成交模型寧可悲觀。** 模擬賺錢不代表實盤賺錢，模擬虧錢幾乎保證實盤虧錢。

**5 歐規模的硬限制**

- Binance USDT-M 每個合約都有最小名目金額（MIN_NOTIONAL，BTCUSDT 約 100 USDT，其他較低），以 exchangeInfo 為準。
- taker 手續費約 0.05%，100 USDT 名目來回約 0.1 USDT，等於帳戶約 1.7%。所以策略要以 maker（限價掛單）為主，或者每筆期望獲利要遠大於 0.1% 名目。
- 實務上第一階段應優先挑最小名目低的合約（主流山寨幣、TradFi 合約），而不是 BTC。

## 系統架構

所有訊號都由確定性 Python 規則產生，LLM 只負責否決，唯一能下單的地方是 risk 閘門後面的 broker。影子帳和實盤走完全相同的路徑，差別只在最後一個 broker。

資料流（原圖的文字版）：

```
Binance 公開行情 (websocket)
  -> feed + reference（標準化事件、exchangeInfo 快取）
  -> signals（S1 S2 S3 確定性規則，每個訊號寫入 DB）
  -> llm_gate（Hermes，回 take / skip，兩者都記錄）
  -> risk（硬規則，唯一的下單閘門）
       -> sim_broker（all / llm / live_mirror 三本影子帳）
       -> live_broker（階段 2 才啟用，持有 API key）
  -> ledger + report（audit.jsonl 雜湊鏈、每日報告）
  -> Telegram

私鑰只存在執行層（risk、live_broker 所屬的獨立 Unix 使用者）。
Hermes 只看得到 llm_gate 的輸入與輸出，以及 shared/ 裡的快照。
```

skip 掉的訊號仍會進 `all` 影子帳跑完，這是 A/B 驗證 LLM 價值的前提。

### Hermes profile 設定

- profile 名稱建議 `perp-desk`，從空白建立，不用 `--clone`，避免帶入 Main CIO 的記憶。
- SOUL.md 定義身份與語氣，AGENTS.md 定義建置任務與權限。否決者的輸出格式以本文件「否決者的輸入與輸出」一節為準。
- cron 只排兩個工作：週日 21:15 UTC 跑 S1 新聞掃描，每天 06:00 UTC 讀報告寫覆盤。訊號觸發由 Python 事件驅動，不靠 cron 輪詢。
- `approvals.cron_mode: deny`，Telegram 上不按任何「Always」。

## Hermes 落實方式

Hermes 是大腦和操作台，不是引擎。行情、訊號、模擬成交、下單都在獨立的 Python daemon 裡。Hermes 負責需要語言理解、網路搜尋、對話的部分，並且永遠不持有 API key、不下單、不處理開倉核准或 /kill。

### 五個角色

| 角色 | 觸發方式 | 讀 | 寫 |
| --- | --- | --- | --- |
| 否決者（perp-gate） | daemon 以 single-query 模式呼叫 | 訊號 JSON、新聞 | 固定 schema 的 verdict |
| 週末新聞掃描（weekend-news-scan） | Hermes cron，週日 21:15 UTC | 網路搜尋 | `shared/news/` |
| 覆盤員（perp-review） | Hermes cron，每天 06:00 UTC | `shared/reports/` | journal、Telegram 報告 |
| 操作台（perp-status） | 使用者在 Telegram 提問 | `shared/status.json` | 對話回覆 |
| 研究助理 | 使用者要求 | repo 程式碼 | 草稿與 `proposals/` |

### 兩個 Telegram bot

- **Ops bot**（daemon 擁有）：開倉核准按鈕、成交通知、`/kill`。必須是確定性程式，Hermes 當掉或額度用完時仍可運作。階段 2 才建置。
- **Hermes bot**（Hermes gateway）：與使用者對話、里程碑回報、覆盤報告。

### 目錄與權限

```
/opt/perp-desk/                  <- Unix 使用者 perpd（建置完成後由使用者移交）
├── engine/                      <- daemon 程式碼
├── config/risk.yaml             <- 只有 perpd 能寫
├── secrets/.env                 <- Binance key、Ops bot token（使用者自行放置）
├── data/perp.db                 <- 只有 perpd 能讀寫
└── shared/                      <- 交換區
    ├── status.json              <- daemon 寫，Hermes 讀
    ├── reports/                 <- daemon 寫，Hermes 讀
    ├── news/                    <- Hermes 寫，daemon 讀
    ├── pending/                 <- daemon 寫待審訊號
    └── verdicts/                <- Hermes 寫，daemon 讀
```

Hermes 對 `shared/` 以外沒有寫入權限，績效資料一律透過 daemon 匯出的快照讀取，不直接連資料庫。

### 否決者呼叫方式

S1 的決策點沒有延遲壓力，daemon 用 subprocess 呼叫 `hermes -p perp-desk chat -q "<prompt>"`，timeout 600 秒，從輸出擷取 JSON 並驗證 schema。失敗時記為 `no_verdict`：`llm` 帳視為 skip（fail-closed），`all` 帳照做。`no_verdict` 比例要出現在每日報告裡。`approvals.single_query_mode` 維持 deny。

### daemon 的執行方式

daemon 以 systemd service（或 docker compose）常駐，不由 Hermes 的 terminal session 啟動。Hermes 負責撰寫 unit 檔，由使用者安裝並啟用。

## 影子交易引擎 spec

影子引擎訂閱 Binance 實盤公開行情，用刻意悲觀的規則模擬成交，並套用真實的手續費、funding 和交易所限制。它同時是回測後的 forward test 環境，也是實盤的對照組。

### 模組

| 模組 | 職責 | 輸入 | 輸出 |
| --- | --- | --- | --- |
| `feed` | 維持 websocket 連線、斷線重連、補資料缺口標記 | Binance USDT-M 公開串流 | 標準化事件寫入 queue |
| `reference` | 每天拉 exchangeInfo，快取 tick size、step size、MIN_NOTIONAL | REST | `symbols.json` |
| `signals` | 各策略的確定性訊號產生器，一策略一檔 | 事件 queue | `signals` 表 |
| `llm_gate` | 把訊號加情境送 Hermes，收回 take/skip | 訊號、事件日曆 | `llm_reviews` 表 |
| `sim_broker` | 保守成交模型、部位、保證金、funding 結算 | 意圖、事件 | `orders_sim`、`fills_sim` |
| `live_broker` | 真實下單（第二階段才啟用），同一介面 | 意圖 | `orders_live`、`fills_live` |
| `risk` | 硬規則檢查，對 sim 和 live 一視同仁 | 意圖、帳戶狀態 | 放行或拒絕 + 理由 |
| `ledger` | append-only 稽核日誌，每行帶前一行雜湊 | 所有事件 | `audit.jsonl` |
| `report` | 每日與每 20 筆的績效和校準報告 | 資料庫 | Telegram 訊息、markdown |

### 資料串流（全部公開，不需要 API key）

- `<symbol>@bookTicker`：最佳買賣價與掛單量，用於 taker 成交價
- `<symbol>@aggTrade`：逐筆成交，用於判斷限價單是否被「穿過」，以及計算 taker 主動買賣量
- `<symbol>@depth5@100ms`：前五檔，用於估計吃單滑價
- `<symbol>@markPrice@1s`：標記價、指數價、當期 funding rate、下次結算時間
- `<symbol>@kline_15m`：策略 S2 的 K 線
- `!forceOrder@arr`：全市場強平單（交易所會節流，只能當代理指標）

### 保守成交模型

1. **市價單（taker）**：以決策時間加 250 ms 延遲後的第一筆 bookTicker 為準，買用賣一價、賣用買一價。數量超過第一檔就往 depth5 走，超過五檔的部分再加 2 個 tick。
2. **限價單（maker）**：只有當 aggTrade 價格「嚴格穿過」掛單價才算成交（買單要有成交價低於掛單價），碰到掛單價不算。不模擬排隊位置，不做部分成交。
3. **停損單**：以標記價觸發，觸發後當作 taker 成交並套用第 1 條。價格跳空時用跳空後第一個可成交價，這在 TradFi 合約週一開盤時特別重要。
4. **手續費**：maker 和 taker 費率寫在設定檔，預設用 VIP0 費率，每筆都扣。
5. **Funding**：在每個結算時間點，以該時刻的標記價名目金額乘上 funding rate 結算。多單在正 funding 時付錢，空單收錢，反之亦然。
6. **交易所限制**：下單前依 exchangeInfo 的 step size 取整、檢查 MIN_NOTIONAL，不符合就拒單並記錄，跟實盤行為一致。
7. **強平**：用設定檔裡偏保守的維持保證金率估算，保證金率低於它就以 taker 規則強制平倉，並記一筆額外 0.5% 的清算費用。

### 設定檔範例

```yaml
account:
  start_equity_usdt: 5.8
  mode: shadow          # shadow | live | both
fees_bps:
  maker: 2.0
  taker: 5.0
sim:
  latency_ms: 250
  extra_ticks_beyond_depth: 2
  maker_fill_rule: trade_through
  maintenance_margin_rate: 0.01
  liquidation_fee_pct: 0.5
risk:
  max_risk_per_trade_pct: 4.0
  max_effective_exposure_x: 3.0
  max_daily_loss_pct: 10.0
  max_open_positions: 1
  require_stop_loss: true
  kill_drawdown_pct: 50.0
  refill_cooldown_days: 30
```

### 資料表（SQLite）

```sql
CREATE TABLE signals (
  id TEXT PRIMARY KEY, ts INTEGER, strategy TEXT, symbol TEXT,
  side TEXT, entry_ref REAL, stop REAL, target REAL,
  features_json TEXT            -- 觸發當下的所有輸入，方便事後重現
);
CREATE TABLE llm_reviews (
  signal_id TEXT, ts INTEGER, model TEXT, verdict TEXT,  -- take | skip
  confidence REAL, reason TEXT, context_hash TEXT
);
CREATE TABLE orders_sim (
  id TEXT PRIMARY KEY, signal_id TEXT, book TEXT,        -- all | llm | live_mirror
  type TEXT, side TEXT, qty REAL, px REAL, status TEXT, ts INTEGER
);
CREATE TABLE fills_sim (
  order_id TEXT, ts INTEGER, px REAL, qty REAL, fee REAL, liquidity TEXT
);
CREATE TABLE funding_events (
  ts INTEGER, symbol TEXT, book TEXT, rate REAL, notional REAL, amount REAL
);
CREATE TABLE equity (ts INTEGER, book TEXT, equity REAL, open_risk REAL);
```

`book` 欄位是 A/B 的關鍵。每個訊號同時進三本帳：`all`（全部照做）、`llm`（只做 LLM 同意的）、`live_mirror`（和實盤一模一樣的單，用來校準模擬誤差）。

### 實盤校準

實盤啟用後，每筆實單都比對 `live_mirror` 的模擬結果，記錄三個差值：成交價差（bps）、成交與否是否一致、手續費差。累積 30 筆後，如果模擬平均比實盤樂觀超過 3 bps，就把差值加回 `extra_ticks_beyond_depth` 或延遲參數，重跑所有歷史影子帳。

## 策略研究

主推策略是 S1「週末科技股恐慌回補」：它有機制、有兩個獨立來源的證據、單筆預期移動遠大於成本，而且交易頻率低到適合 5 歐帳戶。其他三個是研究軌道，只在影子引擎跑。

### 先講勝率陷阱

研究裡有兩個案例直接說明「勝率 50% 以上」不是目標：

- 一篇 2026 年 8 月的 arXiv 論文發現，183 個 Binance 幣對在 15 分鐘尺度有顯著反轉，BTC 上單純「反著上一根 K 棒下注」的方向準確率約 52.3%。但平均每筆毛利只有約 1.3 bp，最便宜的來回成本是 5 bp，所以勝率過半還是穩賠（[Kitron & Wengrowicz](https://arxiv.org/html/2608.21888v1)）。
- Crypto.com 的研究顯示 NVDA 永續合約在週日 22:00 UTC 預測週一開盤方向的準確率 78.9%，但照這個方向做 1 倍部位，淨損益是負的，因為做空那一側持續虧錢（[Crypto.com Research](https://crypto.com/en/research/rwa-perps-find-predictive-edge-apr-2026)）。

所以篩選標準改成兩條：**扣成本後期望值為正**，而且**單筆預期移動至少是來回成本的 10 倍**。第二條會自動把你推離高頻指標交易，推向事件型、低頻的策略。

### 候選總覽

| 代號 | 策略 | 市場 | 頻率 | 預期移動 / 成本 | 證據 | 狀態 |
| --- | --- | --- | --- | --- | --- | --- |
| S1 | 週末科技股恐慌回補 | TradFi 永續 | 每月 1–4 次 | 約 25 倍 | 兩個獨立來源 | 主推，影子後小額實盤 |
| S3 | 貴金屬週末下跌延續 | TradFi 永續 | 每月 0–2 次 | 約 15 倍 | 單一來源，13 個週末 | 影子觀察 |
| S2 | 做市型短線反轉 | crypto 永續 | 每天多次 | 小於 1 倍 | 強（學術、含保留期） | 研究軌道，預期會失敗 |
| S4 | 財報文字判讀 | TradFi 永續 | 每季數次 | 未知 | 弱 | 純紙上 |

### S1 週末科技股恐慌回補

**大家怎麼用週末永續：** 當成週一開盤的「預言機」，跟著方向做。Binance Research 的數據顯示週末價格已經反映週一跳空的中位數 92%，所以跟著做能賺的剩餘空間很小（[Insider Monkey 引述 Binance Research](https://www.insidermonkey.com/news/what-weekend-on-chain-prices-say-about-monday-and-what-they-dont-1842401/)）。

**反過來看：** 同一份 Crypto.com 研究發現，週末跟著負面訊號做空科技股持續虧損，原因是週末流動性薄、恐慌被放大，週一機構買盤把它拉回來。一篇代幣化股票的學術研究也發現，只有一邊市場開著的時候，價格會出現短期反轉（[Coincub 引述 Cong 等人 2025](https://coincub.com/blog/24-7-tokenized-asset-trading/)）。這兩件事指向同一個機制：**沒有新聞支撐的週末科技股下跌，是流動性造成的超跌。**

**額外的結構性好處：** 美股休市時 Binance 把價格指數凍結在最後現貨價（[BitMEX Q1 2026 報告](https://www.bitmex.com/blog/2026q1-derivatives-report)）。永續價格跌到凍結指數下方時，funding 會轉負，空方付錢給多方。所以這筆多單可能「一邊等回補一邊收 funding」。這點要在影子引擎實際驗證 TradFi 合約是否套用同一公式。

**規則 v0（事先寫死，跑完再改）：**

1. 標的：Binance 上的大型科技股與那斯達克指數類 TradFi 永續。排除 pre-IPO 合約和小型股，因為它們的週末波動完全不同（[Phemex](https://phemex.com/academy/tradfi-perpetuals-moving-during-us-market-closed)）。
2. 觸發：週日 21:45 UTC，從週五美股收盤到現在的報酬 ≤ −1 倍日波動標準差（60 日計算）。
3. LLM 否決：檢查週末有沒有公司層級的硬新聞（財報預告、訴訟、監管、管理層異動）或重大總經事件。有就跳過。這是 LLM 在整個系統裡最有價值的一個判斷。
4. 排除：未來 3 個交易日內有財報的標的，以及美國長週末。
5. 進場：週日 22:00 UTC 做多。
6. 出場：週一 13:45 UTC（美股開盤後 15 分鐘）市價平倉。
7. 停損：放在該標的歷史最大週末逆向波動之外。NVDA 的參考值約 7%，換算槓桿上限約 14 倍（Crypto.com 同一份研究）。部位大小讓觸發停損時虧損等於帳戶的每筆風險上限。

**失效條件：** 累積 30 個合格事件後，影子帳的平均淨報酬 ≤ 0，或者被 LLM 否決的那組表現不比被接受的差，就停用或拿掉 LLM 層。

**已知風險：** 樣本數長得很慢（每個標的每年大概十幾個合格週末），而且各科技股高度相關，30 個事件的有效樣本數其實更少。週一開盤的跳空可能直接跳過停損。

### S3 貴金屬週末下跌延續

同一份 Crypto.com 研究發現黃金、白銀的週末負面訊號比較可靠，順著做空有獲利，理由是金屬沒有週一「逢低買進」的散戶買盤。但它只有 13 個有效週末。另外 CME 期貨週日 22:00 UTC 就開盤，套利者會在那時立刻把缺口補掉，所以可交易窗口要在 22:00 前結束。先在影子引擎觀察，不上實盤。

### S2 做市型短線反轉（研究軌道）

上面那篇 arXiv 論文的反轉效果集中在「主動吃單推動的大幅度 K 棒」之後，而且隨上一根 K 棒的幅度單調增強，這是流動性提供者被補償的特徵。作為 taker 去抓一定虧，唯一可能的形式是當 maker：在大幅度、高主動買賣不平衡的 15 分鐘 K 棒之後，於反方向掛被動限價單，下一根 K 棒結束平倉。

老實說這個大概率會失敗，因為 maker 手續費加上逆向選擇（你的單剛好在價格繼續往壞方向走時成交）可能吃光全部優勢。放它在影子引擎跑的價值是校準你的成交模型：它交易次數多，能很快告訴你模擬和現實差多少。**300 筆影子交易淨值 ≤ 0 就結案。**

### S4 財報文字判讀（紙上）

讓 LLM 讀財報新聞稿和指引，分類成「超預期、符合、低於」，看之後一個交易日的永續價格漂移。這是唯一一個 LLM 做「方向判斷」而不只是否決的策略，所以風險最高。LLM 的訓練資料污染在 forward test 裡不是問題，但不能拿過去的財報回測。

### 刻意不做的

- **純技術指標交叉、網格、馬丁格爾：** 沒有機制說明為什麼扣成本後有優勢。
- **期現基差套利：** 需要現貨腿和資本，Ethena 用 Binance 股票永續做這件事，過去六個月的年化基差才約 3.56%（[TokenPost](https://www.tokenpost.com/news/business/24089)），對 5 歐沒有意義。
- **高槓桿山寨幣短線：** 手續費佔比最高，而且最容易被插針。

## LLM 角色與 A/B 驗證

LLM 在這個系統裡是否決者和研究員，不是預測者。它的價值要用影子帳本的 A/B 結果證明，證明不了就拿掉。

### 四個角色

| 角色 | 做什麼 | 何時跑 | 模型建議 |
| --- | --- | --- | --- |
| 否決者 | 對每個訊號判斷 take 或 skip，重點是「這個價格變動有沒有新聞支撐」 | 訊號觸發時 | Codex 主力 |
| 新聞分類器 | 把週末新聞分成硬新聞、軟新聞、無新聞 | S1 觸發前 30 分鐘 | Ollama 先篩，有疑問再升級 |
| 覆盤員 | 每 20 筆寫一次覆盤，分開評決策品質與結果品質 | 批次 | Gemini（額度獨立） |
| 研究員 | 寫新策略的回測程式、找 bug、審查規則 | 離線 | 任一 |

### 否決者的輸入與輸出

輸入固定為：訊號 JSON、觸發當下的 features、過去 72 小時該標的新聞標題清單、未來 3 天事件日曆。刻意**不給** K 線圖或長串價格序列，因為 LLM 處理數值時間序列不可靠，而且這會讓它有理由去「預測價格」。

輸出固定 schema：

```json
{
  "signal_id": "S1-NVDA-2026-10-04",
  "verdict": "take",
  "news_class": "none",
  "confidence": 0.7,
  "reason": "一句話",
  "what_would_change_my_mind": "一句話"
}
```

### A/B 設計

1. 每個訊號同時進 `all` 帳（全部照做）和 `llm` 帳（只做 take）。
2. 被 skip 的訊號繼續在 `all` 帳裡跑完，所以你永遠知道「如果沒聽 LLM 會怎樣」。
3. 評估指標是 **skip 組與 take 組的平均淨報酬差**，附 bootstrap 信賴區間。
4. 判決：30 個以上事件後，如果 skip 組平均報酬不顯著低於 take 組，LLM 否決層就沒有增加價值，關掉以省額度。
5. 同時追蹤校準：把 confidence 分成三組，看每組的實際勝率是否單調遞增。

### 防止 LLM 汙染結果

- 所有 prompt 和模型版本寫進 `llm_reviews.context_hash`，換 prompt 就是新實驗，舊結果不混用。
- LLM 的判斷只能在觸發時做一次，不能事後修改。
- 不拿過去的事件做 LLM 回測，只用 forward 資料。

## 風控硬規則與 kill switch

所有規則寫在 `risk` 模組裡，對影子帳和實盤一視同仁，設定檔由另一個 Unix 使用者持有，Hermes 只能讀。

### 下單前檢查（任何一條不過就拒單）

| 規則 | 值（第一階段） | 理由 |
| --- | --- | --- |
| 必須附停損 | 是，且為交易所端 reduce-only | 電腦關機時停損仍在 |
| 每筆風險 | ≤ 帳戶 4% | 燒錢期的激進旋鈕放這裡，不放槓桿 |
| 有效曝險 | ≤ 3 倍 | 交易所槓桿設定可以更高，但名目 / 權益不超過 3 |
| 同時持倉 | ≤ 1 | 5 歐帳戶無法分散 |
| 每日虧損 | ≤ 帳戶 10%，觸發後當日停止開倉 | 擋報復性交易 |
| MIN_NOTIONAL | 依 exchangeInfo | 和交易所一致 |
| 財報黑名單 | 3 個交易日內有財報的標的禁做 | S1 規則的一部分 |

### Kill switch

- Telegram 指令 `/kill` 由執行層直接監聽，不經過 Hermes。收到後取消所有掛單、市價平掉所有部位、把 `mode` 改成 `shadow`。
- 帳戶從起始資金回撤 50%：自動觸發 kill，寫 post-mortem，30 天內拒絕任何入金後的開倉。
- 行情串流中斷超過 60 秒：停止開新倉，已有部位靠交易所端停損保護。

### API key 規則

- 只開合約交易權限，不開提領，綁 IP 白名單。
- 合約錢包只放要玩的錢，主帳戶其他資產不要放在同一個可被 key 動到的錢包。
- key 只存在執行層的環境變數，Hermes 的 HERMES_HOME 裡不能出現。

## 評估指標、升級門檻、時程

每個階段靠證據升級，不靠日期。時程只是估計，S1 一年大約只有幾十個合格事件，所以要有耐心。

### 指標

| 指標 | 定義 | 為什麼看它 |
| --- | --- | --- |
| 每筆淨期望值 | 扣手續費、funding、滑價後的平均報酬（以 R 計，1R = 每筆風險） | 唯一真正重要的數字 |
| 期望值信賴區間 | moving-block bootstrap 95% CI | 區分運氣與優勢 |
| 移動 / 成本比 | 平均絕對報酬 ÷ 平均來回成本 | 低於 10 就代表太依賴成本估計 |
| LLM 否決增益 | take 組減 skip 組的平均淨報酬 | 決定 LLM 層的去留 |
| 模擬誤差 | live 與 live_mirror 的成交價差（bps） | 校準影子引擎 |
| 最大回撤 | 權益曲線峰值到谷底 | 風控檢查 |
| 勝率 | 只作參考 | 見策略研究的勝率陷阱 |

### 階段

1. **階段 0 建置（約 2–3 週）**：feed、reference、sim_broker、ledger、report 上線。驗收標準是連續 7 天不斷線，所有 S2 影子交易都能完整重現。
2. **階段 1 純影子（約 2–3 個月）**：S1、S2、S3 在影子帳跑，LLM 否決層啟用。升級門檻：S1 累積 ≥ 15 個事件且期望值 > 0。
3. **階段 2 微額實盤（5–7 歐）**：只做 S1，每筆都要 Telegram 核准，`live_mirror` 同步記錄。升級門檻：S1 累積 ≥ 30 個事件、期望值 CI 下緣 > 0、模擬誤差 < 3 bps。
4. **階段 3 按證據加資金**：每次加碼不超過當前資金的 1 倍，每筆風險降到 2%。任何時候期望值 CI 下緣跌破 0，退回階段 1。
5. **任一階段的結案條件**：策略的失效條件被觸發就結案，寫 post-mortem，不改參數硬救。

## 資料來源與參考文獻

本文件的研究數字都來自下列實際打開過的頁面。文中的手續費、最小名目等交易所參數是近似值，實作時以 Binance exchangeInfo 和帳戶費率為準。這不是投資建議。

- [Kitron & Wengrowicz (2026), Short-horizon mean reversion in cryptocurrency markets, arXiv 2608.21888](https://arxiv.org/html/2608.21888v1)：15 分鐘反轉、1.3 bp 毛利對 5 bp 成本
- [Crypto.com Research (2026-05), Real-World Asset Perpetuals: Find The Predictive Edge](https://crypto.com/en/research/rwa-perps-find-predictive-edge-apr-2026)：週末預測準確率、科技股做空陷阱、最大週末逆向波動
- [Insider Monkey (2026-09), What Weekend On-Chain Prices Say About Monday](https://www.insidermonkey.com/news/what-weekend-on-chain-prices-say-about-monday-and-what-they-dont-1842401/)：週末反映週一跳空的中位數 92%
- [CoinDesk (2026-04), Crypto perpetuals predict the direction of Wall Street's Monday open](https://www.coindesk.com/markets/2026/04/11/crypto-perpetuals-predict-the-direction-of-wall-street-s-monday-open-with-89-accuracy-data-shows)
- [Coincub (2026-09), 24/7 Tokenized Asset Trading](https://coincub.com/blog/24-7-tokenized-asset-trading/)：Cong 等人 2025 年代幣化股票研究摘要
- [BitMEX (2026), Q1 2026 Derivatives Report](https://www.bitmex.com/blog/2026q1-derivatives-report)：Binance 休市時的凍結指數與 EWMA 標記價
- [Phemex Academy (2026-09), Which TradFi Perpetuals Actually Move While the US Market Is Closed](https://phemex.com/academy/tradfi-perpetuals-moving-during-us-market-closed)
- [Crypto Economy (2026-09), Binance Futures Expands TradFi Offering](https://crypto-economy.com/binance-futures-expands-tradfi-offering-with-five-new-perpetual-contracts/)：20 倍上限、funding ±2% 每 8 小時
- [Binance 開發者文件：USDⓈ-M Futures General Info](https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info)：demo-fapi 測試網址
- [Binance 開發者文件：Demo Mode General Info](https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info)：Demo 與 testnet 差異
- [TokenPost (2026-09), Ethena Adds Tokenized U.S. Stocks to USDe Backing Strategy](https://www.tokenpost.com/news/business/24089)：股票基差年化約 3.56%
