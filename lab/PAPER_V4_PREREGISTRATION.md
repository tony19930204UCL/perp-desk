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
