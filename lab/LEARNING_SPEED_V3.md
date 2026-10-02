# H1-PAPER-003 preregistration (candidate, NOT DEPLOYED)

Registered 2026-10-02T15:38:21Z, before public candidate probe. Parent H1-PAPER-002. No old-price backtest or parameter winner selection was used.

## Root causes before strategy change

`evidence/no_fill_diagnosis.json` is a read-only live snapshot at 15:27:42.701 UTC. Nine signals, zero fills. Seven unique signals rejected `late_closed_bar_signal`, one `risk_per_trade`; an additional submitted order expired. A signal may be routed before its later broker rejection, so routing counts alone were inadequate. 3,087 cumulative polling errors and 31 gaps. The batch-time candidate has original raw-source/clock-bounds evidence of future-source rejection. The entry-risk candidate has original-book replay proving the unchanged .061 quantity was over the original 1 USDT modeled risk cap after adverse delayed price movement. Imported candidates remain independently attributable in the exact diff and existing evidence. No strategy change can repair these engineering blockers by itself.

## Frozen hypothesis

ETHUSDT long-only, closed **1m** bars, 60 preceding returns, downside **1.5 sample standard deviations**, volume **greater than 1.2 median**. Previous closed-bar close remains the target. Changes increase decision opportunities without forcing orders. Sixty-one genuine contiguous historical closed bars are indicator context only. They never generate trades, samples or profits.

Unchanged: stop .5%, previous-close target, max hold 30 minutes, reward must exceed twice full estimated cost, taker .05% each way (user-supplied assumption, not verified VIP account fee), actual finalized funding, observed depth, each-side two adverse ticks, minimum 2,000ms latency and a later source timestamp than arrival. Original 15,000ms freshness, 60s gap policy, 5s polling, min notional/filter grid stay strict. Risk: per-trade modeled loss 1 USDT, UTC-day 3, total 10, exposure/leverage ceiling 3x, one position. Gaps/funding and actual exits can exceed planned loss caps. No stop-price fabricated fills.

Engineering changes: imported batch-time real local waits capped at two monotonic seconds, independently strict broker delivery gate; imported opt-in execution risk quantity ceiling (downsize on actual observed book and cancel remainder); durable strategy-specific runtime registration on the SAME account and original broker identity. Old rejected/expired orders are never retried. No keys, private API or real money.

## New window and sample definition

Parent migration atomically persists strategy activation and **48h deadline**, target **30 complete round trips**. A sample is one flat-to-flat position episode. Multiple depth/partial entry or exit fills are NOT independent samples. Open episodes are not completed samples. Past orders, fills, fee/funding ledger, risk baselines, original account forward start, handled outcomes and audit prefixes remain. New strategy metrics use persistent fill/ledger/signal baselines; cumulative cash/equity are never reset to 100. Restart never resets deadline or samples. A probe's window is NOT the deployed window.

At deadline stop new entries, continue authentic risk/exit handling. Review net all-cost cash PNL, completed rounds, open episodes, rejection categories, elapsed time, cost breakdown and data reliability. Thirty rounds is only a review target, not an edge or profitability criterion. Insufficient samples means unproven. Risk/integrity faults can stop earlier. No arbitrary entries to reach quota. No automatic retuning.

## Contrary case and limits

The unchanged cost gate may reject nearly every 1m dip because expected reversal is too small. More signals can increase fee drag and information-driven continuation losses. Thirty rounds within 48h may be impossible within frozen risk budgets. Zero fills during a public probe is not an advantage and no trade is fabricated. REST can miss intrabar stops. Two-second clock wait has a hard limit and fails closed for persistent future/stale sources. OS clock offset is not repaired. SQLite growth and WSL reboot auto-start remain operational limits. PAPER tests do not validate a private exchange adapter.

## Deployment boundary

Child does NOT stop PID2621427, modify live source/state/status/dashboard/cron, push, or mark completed. Parent must independently run acceptance scripts, inspect diff/hash, stop ONLY the verified engine, rerun flat/no-pending continuity checks under migration locks, and migrate IN PLACE with backup. Non-flat or pending accounts are a blocker, not a reason to discard records. Separate proposed versions appendix will be supplied. Exact commands and accepted evidence are appended after actual execution.
