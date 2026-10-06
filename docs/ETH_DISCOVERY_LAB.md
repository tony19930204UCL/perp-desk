# ETH Discovery Lab (Issue #23) — staged operator guide

Status: **source/tests/docs only; not deployed or activated**.

This guide covers the preregistered three-arm ETHUSDT discovery lab. It does not
activate H1-PAPER-004, modify the default PAPER account, replace health/supervisor
ownership, enable TradFi, or install a service/scheduler.

## Frozen design

All arms share one explicit operator activation timestamp, one fresh 48h forward
window and one causal public source. Each arm has its own artificial 100 USDT
research account, broker DB, risk baselines, orders, fills, finalized funding
ledger, audit and report. Capital/PnL/sample counts are never pooled.

- A — exact 004 long signal/preceding-15 closed-1m VWMA proxy target, taker entry
  and taker protective/profit exits.
- B — same long signal/target; post-only maker entry and maker profit exit.
  Stop, max-hold and source-gap safety exits are reduce-only taker. If a maker
  profit limit would cross at arrival, it is rejected as maker and replaced by
  an explicitly labeled reduce-only taker fallback.
- C — A plus the preregistered symmetric short trigger: current 1m return strictly
  above the preceding-60-return mean + 1.5 sample sigma, with the same volume
  threshold. Short target is the same frozen prior-15 VWMA and must be below
  contemporaneous entry to clear the same strict 2x full-cost gate.

Common values are frozen in `lab/discovery_config_v1.json`: 1m closed bars,
60 prior returns, 1.5 sigma, 1.2 volume multiplier, 15-bar frozen VWMA proxy,
0.5% stop distance, 30m maximum holding, strict reward >2x estimated cost,
2s arrival, 15s source age, maker 0.0002 and taker 0.0005 configured fee
assumptions, and PAPER-RISK-001 1/3/10 USDT + <=3x + one position per arm.
These fee assumptions match the operator-verified ordinary-user crypto fee-page
evidence (maker 0.02% / taker 0.05%). They do not assume a BNB discount or any
private-account VIP tier; private VIP/BNB applicability remains unknown.

## Shared source and aggTrade contract

`discovery_feed.py` owns one durable source cursor/hash/retention store. Callers
fetch a public input once and submit it once. The feed normalizes closed bars,
book, mark, finalized funding/status and aggTrade, then fans the same event to
all subscribed arms. There is no per-arm REST polling.

aggTrade requires explicit integer aggregate-trade identity, exact price/qty,
explicit BUY/SELL aggressor and source/receipt timestamps. The included Binance
USD-M adapter maps the actual aggregate-trade fields `a/p/q/f/l/T/m`; buyer-is-
maker `m=true` means the aggressor was SELL, and `m=false` means BUY.
Duplicate IDs do not dispatch twice and conflicting duplicate IDs reject.
Out-of-order IDs reject. A sequence gap marks maker trade flow unknown until an
explicit forward reconnect cursor is established. While unknown, B cannot place
a new maker entry and any pending/resting B entry remainder is canceled before
protective handling. Gap recovery is context only: missing historical trades are
never synthesized or replayed as fills. Late/source-invalid events remain
unknown and are not sent to the maker fill path.

Raw shared events retain canonical SHA-256 and are bounded to 20,000 records by
default. The three arm brokers run the new default-off compact dedup mode: they
persist only event SHA-256 values for conflict/idempotency checks, not three
copies of each raw market payload. Existing/default SimBroker callers retain the
original full seen-event behavior. The isolated lab storage budget is 32 MiB; at
90% of that budget new entries are inhibited without changing the default PAPER
storage policy.

## Arm B maker semantics

At the causal routing decision B quotes the contemporaneous best bid exactly,
without price improvement/stretching. The intent is post-only and expires at
decision+15s. Arrival remains +2s.

The primary queue is not supplied at decision time. `SimBroker` has one new
additive opt-in flag, `maker_queue_from_arrival`; only when enabled it freezes
the positive displayed quantity at the resting limit from the validated arrival
book. Existing maker callers retain their original explicit `queue_ahead_qty`
behavior. The primary arm uses 1x displayed queue. A separate preregistered 2x shadow
diagnostic consumes the **same causal aggTrade sequence** with the same strict
trade-through/opposite-aggressor/source-after-arrival/expiry rules. It records
waiting, partial, filled, expired/canceled or source-gap-unknown outcomes and is
reported beside the paired primary order. It has no broker account, ledger, PnL,
risk authority or checkpoint vote and can never become a fourth selectable arm
or retune the 1x queue.

Maker fills require a unique validated aggTrade after resting, strict
trade-through and opposite aggressor volume. Price touch, OHLC and static depth
never fill. Trade volume first depletes frozen queue, then may partially fill.
Entry expiry cancels only the unfilled remainder; existing exposure remains.
No duplicate aggTrade volume is consumed twice inside an arm.

On any B protective exit, any still-pending entry remainder and resting maker
profit orders are canceled first, with the cancellation reason durably audited.
The exit then uses reduce-only taker semantics. This includes stop, max-hold and
maker-source gap safety. Checkpoint/storage/deadline entry stops also cancel
pending entry orders rather than allowing them to fill after the stop boundary.
A crossed profit limit is never labeled maker.

## Direct staged public-source CLI

The operator does not need to write Python glue. The staged entrypoint is:

`python3 lab/discovery_runner.py --root ./discovery-eth-lab <command>`

It uses public Binance USD-M REST GETs only and never calls a private/order endpoint.
A public HTTP failure (including regional HTTP451) is a source failure, not permission
to fabricate data or a reason to start the window.

Exact sequence:

1. **Reference check only — does not start a window**

   `python3 lab/discovery_runner.py --root ./discovery-eth-lab prepare`

   This fetches/validates ETHUSDT reference filters against the frozen maker/taker
   assumptions and persists the exact instrument contract. It creates no research
   account/window and submits no simulated or live order.

2. **Explicit activation**

   `python3 lab/discovery_runner.py --root ./discovery-eth-lab activate --operator-accepted`

   The command waits to an exact closed-minute boundary, revalidates reference
   filters, fetches exactly causal historical 1m bars, keeps only the last 61
   contiguous bars ending at the activation boundary, records them as
   **historical warmup only**, and then starts the new 48h discovery window.
   Warmup bars are never counted as forward coverage or fills. Activation is
   rejected if the namespace was already activated.

3. **Foreground shared-source pump**

   `python3 lab/discovery_runner.py --root ./discovery-eth-lab run --poll-seconds 1`

   One process performs one common public polling cycle and fans the same normalized
   causal events to A/B/C. It fetches ETH depth/book, mark, finalized funding/status,
   closed 1m bars and aggTrade plus reference verification. It never starts three
   per-arm pollers. `run --once` is available for operator diagnosis without a
   daemon/service.

   aggTrade starts/restarts only from an explicit forward cursor. Sequence gaps,
   duplicate/conflicting IDs, stale source, no source-after-arrival coverage and
   reconnect discontinuities remain unknown/fail-closed; missing trades are never
   backfilled into maker fills. An aggTrade transport/coverage failure is scoped to
   Arm B maker execution: B pending entry risk is canceled and B exposure follows
   the normal taker gap-protection path, while A/C are not falsely stopped by a
   maker-only source outage. A shared depth/mark/reference/funding/closed-bar
   transport failure is a shared-source gap and fails closed across all arms.
   Both gap types are durable across restart and are cleared only after a complete
   valid recovery cycle. In continuous `run` mode, a rejected public poll is
   persisted as a source failure and does **not** count as a successful poll; the
   same process waits for the next poll and may recover only when a later complete
   causal cycle passes the unchanged timestamp/age/gap gates. `run --once` remains
   strict and exits non-zero on that rejection. Restart after a process failure must
   use the **same root**: activation/start, 8h checkpoint, 48h deadline, warmup,
   evidence, orders/fills/ledgers and prior failure history are never reset or
   extended by recovery.
   Closed bars missed beyond the 15s source-age gate are
   unknown and force normal causal gap handling rather than retroactive signals.

4. **Reports**

   `python3 lab/discovery_runner.py --root ./discovery-eth-lab report`

   `reports/latest.json` is rewritten atomically each poll. The first report at or
   after 8h is frozen as `reports/checkpoint-8h.json`. At/after the original 48h
   deadline, `reports/final-48h.json` is written. Restart reads the same activation,
   deadline, source cursor, warmup/history, broker ledgers and evidence stores; it
   never extends the window or stops early on PnL.

5. **Orderly stop**

   From another shell:

   `python3 lab/discovery_runner.py --root ./discovery-eth-lab stop`

   This writes an operator stop request; it does **not** kill a process. The running
   pump cancels new/pending entry risk, keeps the public source running for normal
   reduce-only protective completion, and exits once all three arms are flat. A
   subsequent `run`/ `report` reopens the same namespace and preserves the
   original start/deadline. Ctrl-C is only a process interruption; use the stop
   command for the staged orderly-stop contract.

The CI fixture mode (`--engineering-fixture --transport-fixture ... --now-ms ...`)
exists only for isolated subprocess acceptance using public-shaped synthetic
receipts. It is explicitly labeled synthetic and is not an operator market-data
mode or performance evidence.

## Activation prerequisites

The external coding agent does not execute this sequence.

1. Accept this PR and independently confirm Issue #20/PR #21 staged source status.
   Do not treat this lab as authorization to migrate/activate H1-PAPER-004.
2. Select a new isolated discovery state root. It must not be the default PAPER
   state directory or a copy/restore of it.
3. Run `prepare` on the chosen isolated root. The CLI itself fetches and
   validates the current public ETHUSDT reference filters and persists the exact
   instrument contract; the operator does not supply or hand-code filters.
4. Verify adequate isolated storage under the 32 MiB budget.
5. Run the explicit `activate --operator-accepted` command once. The CLI seeds
   exactly historical warmup bars ending at the activation cutoff and then fixes
   the new forward start/checkpoint/deadline. Restart must use the same root and
   therefore the same durable activation timestamp; changing it is rejected.
6. Run the single `discovery_runner.py ... run` foreground pump. It owns the one
   shared public fetch cycle and fans normalized events to all three arms; do not
   construct separate per-arm pollers or write Python glue.
7. Use the documented `report` and `stop` commands for snapshots/orderly stop.
   A stop request cancels new/pending entry risk but does not kill the process or
   erase state; normal reduce-only completion continues until flat.
8. Register read-only dashboard ledgers only through the existing configured
   multi-ledger allowlist if desired. Keep `default` bound to the existing
   default PAPER status. Do not auto-discover paths.
9. Do not change health writer/supervisor configuration for this staged lab.

## 8h and 48h semantics

The 8h checkpoint is operational feasibility, not PnL selection.

Coverage denominator is 480 closed-minute slots. 431/480 fails; 432/480 meets
90%. Late/reconnected bars do not count as timely coverage.

- A: >=4 unique first-8h fresh/full-cost-qualified candidates.
- B: >=4 unique first-8h entry orders with **actual simulated maker fills** under
  the frozen queue/trade-through model. Trigger/cost-qualified counts remain
  separate diagnostics.
- C: >=4 unique first-8h fresh/full-cost-qualified candidates, with long/short
  counts reported separately.

Below threshold, new entries stop at the checkpoint while existing exposure keeps
normal exits. Passing never extends the original 48h deadline and never implies
profitability. No PnL-based early stop or in-window retuning is allowed.

## Raw retention and post-rollover causal reconstruction

`shared-feed.sqlite3` remains the bounded raw source buffer (20,000 records by
default). Raw rollover is allowed and is not treated as proof that older raw
payloads remain reconstructible.

Facts that actually caused research decisions/outcomes are copied at the time of
use into `causal_evidence.sqlite3`, an isolated append-only hash chain. It stores
compact signal decisions/economics, maker arrival book identity + 1x/2x queue,
relevant maker aggTrade price/quantity/aggressor/source IDs, scoped runner-level
source-gap/recovery facts, order-state
transitions/rejections/cancellations, fills, fees/realized/finalized-funding ledger
rows, and source-gap/unknown facts. It does not copy every raw market payload and
does not auto-prune. Thus a fill/rejection/queue decision can still be reconciled
after its raw feed row rolls out. Raw context that was never causally retained is
reported as unavailable/unknown and is never reconstructed into a trade.

The evidence store is inside the same isolated 32 MiB discovery budget. At 90%
usage, new entries and pending entry risk stop/cancel under the existing storage
gate, but evidence is not deleted and already-open positions continue normal
reduce-only protective exits. The gate is not raised to prolong the experiment;
default PAPER history/storage is untouched.

## Reporting

Report every arm, not a selected winner: raw/cost-qualified/submitted/expired/
nonfilled/filled-order/flat-to-flat counts, long/short attribution, gross
realized, actual fees, finalized funding, net ledger, exposure/risk stops,
coverage, missing/unknown source data and B queue-stress diagnostics. A/B may be
paired by the same source signal; this correlation is not extra independent
evidence. C short events are separately attributed.

Synthetic CI fixtures and any bounded public probe are engineering evidence only.
They are not deployment, live fills, PnL evidence or confirmation.

## Shutdown and rollback

Stop feeding new events, then allow/carry out only required reduce-only protective
exits under the existing risk/storage rules. Close the lab processes and preserve
all shared-feed and arm stores. Do not delete failed/nonfilled outcomes. Do not
restore an old DB or copy any discovery account over the default account.

Rollback is code/process/config rollback only. If the lab has authentic forward
research activity, preserve it as immutable discovery evidence. A future
confirmation experiment requires a new preregistration and new forward data.

## Explicitly unverified

This staged PR does not verify operator activation, real host resource usage,
actual future aggTrade continuity, actual maker queue position, profitability,
actual account/VIP fees, or any TradFi venue/session/index/funding behavior.
