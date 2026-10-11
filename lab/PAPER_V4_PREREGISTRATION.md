# H2-PAPER-004 preregistration (candidate, NOT DEPLOYED, no window started)

Registered 2026-10-10 by the desk operator, before any engineering for it exists and before any forward bar is observed. Parent: H1-PAPER-003 (abandoned, see evidence/h1-family-abandon-decision-20261010.md). Paper only. No live authorization is implied.

## Why this version exists
H1-PAPER-003 could not trade at the stated taker fee. On public ETHUSDT 1m bars the 1m return sd was 0.0349 percent against a round trip cost of 0.100 percent and a gate of 0.200 percent. Same rule gave 58 signal bars in about 25 h and 0 passed the cost gate. The only change here is the time scale, so any difference in result can be attributed to it.

## Frozen hypothesis
ETHUSDT, long only, closed **1h** bars. Entry signal when the closed bar return is below the mean of the 60 preceding returns minus **1.5** sample standard deviations AND bar volume is above **1.2** times the median volume of the preceding 60 bars. Target is the previous closed bar close. Rationale: after an outsized, high-volume hourly drop, part of the move may mean revert. This is a hypothesis, not a known edge.

## Frozen parameters (chosen by principle, not by any backtest of outcomes)
- Rule parameters identical to H1-PAPER-003 (60, 1.5, 1.2, previous close target).
- interval 3600000 ms, name 1h.
- Stop distance **1.0 percent** of entry. Principle: wider than normal noise. Measured 1h return sd is 0.528 percent, so 1.0 percent is about 2 sd. The old 0.5 percent would be about 1 sd.
- Max holding **12 closed bars** (12 hours), then exit at the next real book.
- Reward must exceed **2x** full estimated cost (unchanged). Taker fee 0.05 percent each way is the user supplied assumption, not account verified. Two adverse ticks slippage each side, 2000 ms latency, observed depth, actual finalized funding. All unchanged.
- Risk contract unchanged: PAPER-RISK-001, max loss per trade 1 USDT, daily 3, total 10, one position, 3x paper leverage, equity 100 USDT.
- At 1.0 percent stop, a 1 USDT risk is about 100 USDT notional.

## Measured before registration (frequency only, no outcome was evaluated)
- ETHUSDT 1h, 1500 bars (62 days): 77 rule signals, about 1.2 per day, 77 of 77 pass the 2x cost gate. Target distance min 0.30, median 0.88, max 3.97 percent.
- Expected qualifying signals about 17 per 14 days, 37 per 30 days. Many will be blocked by single position or pending, so completed round trips will be fewer.

## Window and sample definition
- Fresh forward window of **21 days** from the moment the parent marks the version deployed, with durable deadline. Review target **30** flat-to-flat episodes. It is a review target, not an edge criterion. Realistic expectation is roughly 15 to 25 completed episodes, so the likely verdict is still small sample and I will say so.
- One sample is one flat-to-flat position episode. Partial fills do not count separately. Open episodes do not count.
- At deadline stop new entries, keep authentic risk and exit handling, then review net all-cost cash PNL, completed rounds, open episodes, rejection categories, cost breakdown, data gaps.

## Contrary case (reasons this may simply lose)
- Large high-volume hourly drops in crypto often continue (liquidation cascades, news). The reversion hypothesis could be backwards at this scale.
- Reward to stop is about 0.9 to 1 at the median, so the win rate has to be above roughly 53 percent after costs to break even. A coin flip loses money to fees.
- Signals cluster after crashes, so samples are not independent and one violent day can supply several correlated entries or one big stop.
- Funding can drag long positions in a crowded market.
- 21 days is one market regime. A good or bad result may be regime, not rule.

## Invalidation and stop conditions (decided now)
- Stop the version and record failure if cumulative net all-cost PNL falls to the total loss limit of 10 USDT, or if the daily or total loss halts trigger.
- Declare the hypothesis not supported if at deadline there are at least 15 completed episodes and net all-cost PNL is negative, or win rate is below 45 percent. This is a conclusion about this version only. No parameter is tuned afterwards on this window.
- Invalid window (restart with a new window, do not stitch): continuous source gaps longer than 3 hours, name resolution or source errors above 5 percent of polls, or any engineering fix that changes the rule or the cost model.
- Not a valid reason to stop: a good looking early result. Early PNL does not end or extend the window.

## Data path precondition
- Name resolution was repaired on 2026-10-10 (WSL resolver failed 188 of 367 queries in 25 min, after fix 0 of 533 system lookups and 0 of 90 gh calls over 18 min). A window is only valid if the host stays up and the resolver stays on the fixed servers.

## Not claimed
- No edge. No live trading. Old accounts, fills and ledgers are untouched. No old-price winner selection was used for any parameter.

## Engineering boundary
- Engineering needed: a 1h detector and runtime/config variant on the existing H1 contracts, with the late closed bar gate, kline seeding, source age gates and max hold expressed for 1h bars. Coding goes through the relay to an external agent in a non-main branch and PR. The operator defines outcome and acceptance only.

## 窗口 1 作廢記錄（2026-10-11 約 08:00 UTC，我依上方作廢條件自己判定）
- 作廢條件：來源錯誤超過 5% 輪詢。實測：成功決策週期 4153、輪詢錯誤 900，整體 17.8%。分小時：第 0 到 5 小時 0 到 4%，第 6 小時 15%，第 9 小時起 28 到 37%。
- 原因：本機（WSL 虛擬機）時鐘不穩。外部三個獨立時間來源（Binance 期貨、Binance 現貨、Cloudflare）彼此一致，本機相對它們呈鋸齒形偏差，最大約 3.5 秒；同樣睡 30 秒，單調時鐘走 30.0 秒，牆上時鐘走 33.6 秒。引擎的「來源時間領先本機超過 2000 毫秒就失敗即關閉」規則因此頻繁觸發。確切成因未知（Windows 的 w32time 服務為停止，WSL 內有 Hyper-V 校時與 timesyncd 兩套機制，偏差約 10 小時前開始變大）。
- 窗口內容：0 信號、0 成交、空倉、無掛單。沒有任何證據被丟棄。
- 處理：窗口 1 的資料目錄與狀態檔原樣封存，不刪、不拼接。時鐘驗證通過後以不變的設定（雜湊 e27b4b4b…）開窗口 2，21 天，起點為新服務首次成功輪詢。F1H 三臂等同處理：時鐘驗證通過前不開窗口。
- 這個結果也說明我當初把錯誤率門檻當成「不太可能發生」的條件是低估了環境風險。以後開窗口前必須先過時鐘檢查（scripts/clock_check.py）。

## 窗口 2（2026-10-11 08:26:51 UTC 起，設定不變）
- 時鐘修復後開：根因是 Windows 主機的時間同步服務（w32time）停止、主機時鐘落後 1.3 到 2.2 秒並漂移，WSL 內的 Hyper-V 校時與 NTP 互相拉扯。使用者以系統管理員啟動 Windows Time 並對時後，主機偏差約 1 到 2 毫秒，WSL 時鐘檢查（偏差 51 毫秒、擺幅 11 毫秒）通過，才啟動。
- 設定檔與雜湊不變（e27b4b4b05ec66a2…），狀態目錄是新的，起點 1791707211 附近（見狀態檔 research.strategy_start_ms），到期為起點加 21 天（2026-11-01 08:26:51 UTC）。窗口 1 的資料封存在 lab/data/_void_windows/h2-window1-20261011T0824Z（含 ARCHIVE_MANIFEST.json），不拼接。
- 看門程式現在也檢查本機時鐘偏差（超過 1000 毫秒就報警）。判讀窗口 2 時，除了 latest_error，也要看分小時的輪詢錯誤率。
