# PAPER entry quantity/execution contract (candidate, not deployed)

Branch: `fix/paper-entry-risk-sizing`. Base: `27be995f404df34d7d8a4e85294a8353d804690a`.
This is an engineering bug fix, not a change to signal thresholds or frozen risk parameters.

## Root cause and actual observation

Actual decision `1790910609267`, arrival `1790910611267`, expiry `1790910624267`, BUY TAKER quantity `0.061`, fixed stop `2702.44`. Decision ask `2716.01` plus two adverse ticks gave `2716.03`. Tick is `0.01`, LOT/MARKET step `0.001`, minimum notional `20`, taker fee `0.0005`.

The next recorded executable book had source `1790910616071`, receipt `1790910615861`, and actual dispatch `1790910616497`. That is **7,230 ms after decision**, not a fill exactly two seconds later. Two seconds is the eligibility minimum. Source was 4,804 ms after arrival. Best ask was `2716.48`, with enough quantity at that level. Its simulated adverse execution price is `2716.50`.

Decision planned risk was `0.995472725 USDT`. At the delayed best executable price the unchanged `0.061` quantity would risk `1.024157060 USDT`, so rejecting it was correct. The old broker additionally bounded against unused deepest ask `2716.69` (effective `2716.71`), producing `1.036973465`. Unused depth inflated the bound but was not the sole cause: even the best ask exceeded the cap.

Hypotheses examined: delayed price versus fixed decision stop (confirmed main cause), unused depth bound (confirmed secondary inflation), and already-paid entry fees lost on later adds/partials (confirmed separate contract safety defect). There is no evidence here of a clock repair or a strategy edge.

## Exact economics

For long decision sizing, with tick `t`, ask `a`, entry ticks `ne`, exit ticks `nx`, stop fraction `d`, and taker fee `f`:

```
P = a + t * ne
S = floor(P * (1-d) / t) * t
X = S - t * nx
L_unit = P - X + f*P + f*X
q_plan = floor_to_common_grid(min(1/L_unit, equity*3/P, max_qty))
```

The common grid is the rational least common multiple of LOT_SIZE and MARKET_LOT_SIZE, not merely decimal precision. Stop rounding is downward for this long-only strategy. Decision reward/cost eligibility remains unchanged. `entry_price_bound` describes the decision estimate, not a promise about a future arrival price.

At arrival, simulated entry price is actual observed ask plus entry ticks for BUY, or observed bid minus entry ticks for SELL. Exit bound is fixed stop minus/plus `tick * max(exit_slippage_ticks, exit_fill_slippage_ticks)`. Planned loss is:

```
q * (abs(entry - stop) + exit_slip + entry*taker_fee + exit_bound*taker_fee)
```

For existing positions the reserve additionally includes already-paid entry fees, plus remaining quantity times entry/stop distance, exit slip and exit fee. Fees persist with the position and are allocated proportionally on reductions. Old positions missing that field use a conservative sum of entry fees since opening, without rewriting fills or historical orders.

The 1 USDT figure is a **hard admission cap on modeled stop risk**, not a guarantee of realized maximum loss. Unknown future funding, gaps, delayed stops and REST-missed marks can exceed it. Funding still uses the unchanged finalized ledger. Exit orders still wait for a later real book and can realize worse prices; the patch never fabricates a stop-price exit.

## New opt-in execution semantics

- Only v2 new entry intents set `risk_limited=True`. Their original quantity is an immutable **ceiling**, not a promise that all of it must execute. Optional `risk_quantity_step` carries the common market/lot grid. Fixed legacy orders, makers, reductions, old persisted orders and old v1 routing do not silently acquire this contract.
- On the first eligible actual book, binary-search the quantity grid for the largest quantity satisfying the original broker risk gates, bounded by the intent ceiling and **observed** depth. Check only levels that this candidate would consume. Stop, expiry, source/receipt/dispatch timestamps, fees and configured slippage stay fixed.
- This is maximum quantity **under the conservative consumed-level risk bound**, not VWAP optimization. Worst consumed entry price bounds stop risk. Exposure and entry-fee reserves use the higher consumed price (including the best bid for SELL), and immediate adverse mark difference remains reserved. Bounds are monotone under the frozen fee/model; this is not an arbitrary risk buffer.
- Validate minimum quantity and actual selected multilevel execution notional after finding the safe maximum. Never round up to meet filters. If no admissible quantity exists, reject without a fill. A best executable price on the wrong side of the fixed stop rejects, even if deeper levels could be on the other side.
- Quantity can only decrease, never increase after favorable movement. Full executable amount consumes observed levels, with explicit per-level fees. No beyond-depth modeled fill is allowed for this contract.
- If less than the ceiling executes, the leftover is **canceled**, not left pending to retry: `executable_qty`, `canceled_qty`, `remaining=0`, status `CANCELED`, reason `risk_limited_remainder`. Fills and the open position remain real simulator state. If the entire ceiling executes, status is `FILLED`. Filled quantity plus canceled quantity equals original intent quantity.
- The contract currently requires the symbol to be flat before execution. Add-to-position orders retain strict fixed-quantity checks. Daily 3 USDT, total 10 USDT, exposure 3x, funding, positions, marks, expiry and risk-approval gates remain enforced without relaxed values.

This is a PAPER execution-instruction contract. It must not be confused with changing the quantity of an already accepted exchange order. A future live adapter must size before wire submission, handle real rejection/partial/private events and price movement, and separately verify exchange semantics. This work does not implement or authorize that adapter. Decision-time reward eligibility is unchanged; delayed execution can reduce economic reward. No stop widening or new target/reward policy is bundled.

## Evidence and verification

All new evidence is under `engineering/evidence`, outside live:

- `entry_risk_red1..11*.txt` and matching GREEN logs: eleven vertical behavior cycles. RED 1 and 7 initially attached the opt-in field to the old dataclass to reach the old behavior, then final tests use the real constructor. Ten other new tests are regression/control coverage, not new feature cycles.
- `entry_risk_actual_audit.json` and `entry_risk_actual_broker_excerpt.json`: read-only original receipts/order/book/mark excerpts.
- `entry_risk_replay.py` and `entry_risk_actual_replay.json`: original-source versus candidate counterfactual replay. Original and candidate fixed orders both reject without fills. Opt-in candidate selects `0.059` at `2716.50`, modeled risk `0.990578140`, cancels `0.002`. Next grid quantity `0.060` risks `1.007367600` and is unsafe. Original source/receipt/dispatch timestamps remain unchanged. Readiness is artificially injected to isolate sizing, so this is **not** a full funding replay or forward performance.
- `entry_risk_baseline_ids.json`, `entry_risk_final_ids.json`, `entry_risk_batch1..5_{suite.txt,validation.json}`, and `entry_risk_suite_validation.json`: all **176 unique IDs** passed (155 baseline retained plus 21 new), no missing IDs, duplicates or skips. Accepted batch times: 9.120, 18.182, 296.334, 21.379, 3.670 seconds (348.684 seconds combined).
- Coverage includes fastest rebound, favorable BUY, adverse SELL, partial multilevel depth with canceled remainder, min notional, common grid, next-step boundaries across 121 prices, wrong stop side, daily/total remaining budgets, latency/source/expiry, approval revocation, paid-entry-fee reserve, legacy idempotency, runtime routing and durable restart. Existing risk-rejection tests are unchanged and retain unsafe no-fill assertions.
- `entry_risk_candidate_manifest.json`: explicit candidate hashes and live/base byte comparisons. It is **not** an update to the preserved `source_manifest.json`, nor a deployed manifest. Frozen v2 configuration remains `dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96`. Signals/configs and live core byte hashes match the original base.

Run from `engineering/lab`:

```
python ../evidence/entry_risk_run_suite.py 1
python ../evidence/entry_risk_run_suite.py 2
python ../evidence/entry_risk_run_suite.py 3
python ../evidence/entry_risk_run_suite.py 4
python ../evidence/entry_risk_run_suite.py 5
python ../evidence/entry_risk_replay.py
```

The initial single-run suite was killed by the tool at 420 seconds and is preserved in `entry_risk_full_suite.txt`, excluded from acceptance. Earlier GREEN 3 had a Decimal/string comparison fixture issue, RED 5 first encountered minimum-notional masking before its dedicated exposure fixture was corrected, the revocation control initially used an already-expired test order, and the first replay used an invalid forward-start boundary. Failed attempts remain in evidence and are not counted as passes. No existing test was relaxed or deleted.

## Handoff boundary

No live deployment, restart, DB/config edit, fresh copied staging tree, push, or clock patch. Only the `entry-risk-sizing` work task is updated outside engineering, preserving other tasks; handoff state is `verifying`. Parent must independently accept the exact diff, preserve the original account/order/funding history and namespace, and control any deployment. Existing rejected orders must not be retried/backfilled with this contract. Old evidence and source snapshot manifest remain preserved.
