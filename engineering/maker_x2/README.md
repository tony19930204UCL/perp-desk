# Maker X2 — offline short exit engineering unit

This research namespace retains the independently accepted r4 ledger
`accounting.py` and its 20 unique accounting tests **byte-identical**.
New in r5: `maker_exit_lab.py` (short-only state machine) and
`tests/test_x2.py` (synthetic controls and mutation entrypoints).
No real orders, private account data, market backtest, strategy or deployment.

## Commands (cwd: engineering/maker_x2)

```sh
# First CONTROL, including original 20 accounting regressions:
python3 -m unittest discover -s tests -v

# Standalone X2 controls (mutations excluded from discovery):
python3 tests/test_x2.py control

# Run individually AFTER CONTROL succeeds. Each deliberately broken test
# must exit nonzero with a real AssertionError (not merely a mocked status):
python3 tests/test_x2.py mut-fee
python3 tests/test_x2.py mut-funding
python3 tests/test_x2.py mut-be
python3 tests/test_x2.py mut-duplicate
```

Standalone commands run from this directory in a clean clone without external
PYTHONPATH setup. The test script adds its package-local parent to sys.path.
The normal discovery suite includes the positive duplicate/cancel/race baseline.
Economic BE comparisons explicitly derive their independent expected result
inside a Decimal 50-digit local context matching the production precision,
without epsilon or float tolerance.

Real acceptance requires recorded stdout, exit code and the failing assertion
and test ID for each mutation; these revised r6 commands have **NOT_RUN** status at
GitHub-source delivery. The independent r5 clean-head discovery previously
reported 31 unique tests (original20 PASS, X2 9 PASS/2 FAIL), and standalone
control ImportError; these historical failures are retained as evidence,
not relabeled successful. Existing push/PR CI from r4 does not validate r5.

## Frozen scope and conservative processing

Maker entry fills are explicit confirmed partial sells (2 bp maker fee each);
`arm()` fixes weighted entry E and two targets for half the original filled
quantity each: TP1 E*(1-0.0045), TP2 E*(1-0.0090).
Initial hard stop is E*(1+0.006). Other SL settings, including .0085,
are rejected. During a bar, existing stop is tested before any Maker
profit. Adverse gap stop uses max(bar open, existing stop) * 1.0002,
then 5 bp taker fee. For unknown intrabar ordering, SL/SL_GAP defeats
Maker TP; this is an OHLC conservative proxy, not guaranteed execution.
For OHLC-simulated Maker buybacks, a candidate must equal the posted limit
exactly and bar low must be at or below limit*(1-0.0002), including equality.
Fills are emitted at the exact posted limit, never a cheaper candidate price.
Touch or insufficient penetration does not create an execution. Exit fills on the entry-fill bar
are rejected. Limit quantities, cancellation and duplicate event identities
are guarded. Closed SL positions cannot accept stale TP fills.

Finalized settlement input uses unique event and settlement IDs,
the actual supplied UTC-ms timestamp and mark, with no future-timestamp
settlement accepted. Within an end-of-bar boundary, existing short quantity
is funded **before** the same bar's confirmed Maker exit; funding events
precede exits in the emitted ledger sequence. No future rate is read.
The first TP1 fill never changes its own bar's stop. On the next bar
the economic BE reference is

`(R + q*E) / (q*(1+0.0002)*(1+0.0005))`

where `R=realized_gross-fees_paid-funding_cost` and q is remaining positive
short size. Updated stop is the minimum of the original stop, old stop
and reference. Nonpositive BE reference is explicitly audited as unavailable.
Existing risk is processed before newly supplied bar-end settlements or
passive wins, and new funding is reflected in the next bar's BE assessment.

Every emitted fill/funding event matches immutable `accounting.replay`.
`snapshot()` also retains open quantity, costs, stop, remaining pending
orders and audit transitions. Test controls independently assert hand-derived
Decimal values, then compare replay's cash, basis, fees and funding.

## Limits — not full A01–A16 acceptance

This is a deliberately narrow state machine without entry post-only
transport, TTL 2/5 management, 4h timeouts, X1 or X3 logic,
LIQ/full-account margin and liquidation calculation, day-halt or
consecutive-loss controls, exchange tick/queue simulation, equity MTM,
A14 complete account/episode coverage, CONTROL market placebo, 24-grid
or 12-arm experiments. It does not establish full A01–A16 PASS,
does not claim guaranteed break-even under a gap, future funding,
model error or excess slippage, and does not justify paper/live use.

The frozen accounting candidate is not changed. Work is restricted to
source/tests/document/mailbox on existing draft PR75; no main merge,
research-window activation, credentials, paid tools or account settings.


## r7 callable closed-bar lifecycle (new; NOT_RUN at delivery)

API uses dictionary minute bars with `ts` as UTC **open milliseconds** at
60,000 ms intervals, `open/high/low/close` positive decimal strings, and
`closed=True`. `signal_time` is the signal close UTC ms: first eligible
minute opens at that time. Forming bars and noncontiguous ATR/VWAP windows
are forbidden. Example with entirely fictional inputs:

```python
from entry_lifecycle import ShortEntry, atr14, vwap60
from exit_lifecycle import ShortExit

signal = ShortEntry.for_x1(
    "fictional-1", 0, "100", "100", "1", ".25", 2, "2",
    signal_vwap="99", known_funding_cost=None)
first_minute = {"ts": 0, "open": "100", "high": "100.27005",
                "low": "99", "close": "100", "closed": True}
signal.process(first_minute, fill_qty="2")
withdrawal = ShortExit(signal, "X1", vwap=signal.x1_vwap,
                       known_funding_cost=signal.x1_known_funding_cost)
next_minute = {"ts": 60000, "open": "100", "high": "100.1",
               "low": "98.9", "close": "99", "closed": True}
withdrawal.on_bar(next_minute, maker_fill=True)
print(withdrawal.snapshot()["ledger"]["cash"])
```

`atr14(bars)` uses the last 14 complete 1m true ranges with the
preceding complete close (15 contiguous minutes required).
`vwap60(bars)` uses the last 60 complete minutes'
`sum(quote_volume)/sum(base_volume)`, with positive base volume.
Only existing known signals are consumed. `ShortEntry` calculates the
frozen short limit signal high + max(k*ATR14, close*0.0002)
(k=.25/.50), rejects offset over .006 and applies TTL 2/5 minutes
on [j0, j0+TTL), including the last eligible minute but excluding
the expiry bar. Entry delivery open >= limit post-only rejects; already
resting higher gap opens differ. High must reach limit*(1+.0002)
inclusive and fills occur at limit; mere touch cannot fill. Partial
fills retain actual outstanding size and ledger events. A skipped minute
marks a gap and forbids further new entry; prefix/tail never prefetches
future bars.

`ShortExit(entry, style, vwap=None, known_funding_cost=None)`
supports X1, X2 or X3, composed with the **unchanged**
`MakerX2`. X1 freezes signal-time vwap as profit-side target,
requiring strictly positive projected net of known entry/exit Maker
fees and optionally known funding. Unknown future funding is marked,
not predicted. X2 uses both frozen halves/BE from the core. X3
observes three complete held close-to-close positive returns using
the previous known close as anchor; a zero or negative return resets.
A triggered passive buyback becomes eligible only next minute, is
post-only cross rejected when delivery open <= limit, and expired
TTL2 orders reprice from the last known close without waiting for
another three bars. Hard SL/SL_GAP takes priority, using actual
remaining quantity, adverse .0002 slippage, and .0005 taker fee.
Confirmed settlement event IDs, timestamps and seq are preserved
and reconciled via unchanged `accounting.replay`. A deadline
240 minutes after the first actual fill switches into repeated
X3-style passive withdrawal: never a forced taker TIME exit.
`snapshot()` separates actual cash/fees/funding/events from
unrealized marked value, estimated future close fee, status,
staleness and truncation.

### r7 independently executable acceptance

Run from `engineering/maker_x2`, each command a fresh process.
Both new CONTROL modules and the complete discovery must genuinely
pass before negative mutation failures count:

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_entry_lifecycle.py control
python3 tests/test_exit_lifecycle.py control

python3 tests/test_entry_lifecycle.py mut-lookahead
python3 tests/test_entry_lifecycle.py mut-touch
python3 tests/test_exit_lifecycle.py mut-time
python3 tests/test_exit_lifecycle.py mut-reverse
```

Each mutation intentionally fails with real AssertionError,
a reported test ID, and nonzero return code, not an import or harness
exception. Results are **NOT_RUN** in this connector-only handback;
previous 33/13 X2 controls and four mutations were independently
accepted on parent HEAD, not rerun against r7.

| Frozen reference | Discoverable r7 positive controls |
|---|---|
| A02 entry penetration | test_a02_touch_insufficient_exact_boundary |
| A03 activation/open gap | test_a03_activation_cross_and_resting_gap |
| A04 TTL, prefix, signal | test_a04_ttl2_5_last_and_expiry_without_suffix; test_a04_prefix_tail_gap_no_lookahead |
| A06 X1 frozen economics | test_a06_x1_signal_frozen_vwap_profit_side_net_and_unknown |
| A08 X3 3 bars, retry | test_a08_x3_held_three_adverse_closes_next_bar_only; test_a08_zero_resets_and_delivery_cross_then_retry; test_a08_ttl2_expiry_reprice_without_three_new_bars |
| A10 4h no fake close | test_a10_four_hour_unfilled_stays_open_real_cost_funding_mtm |
| A11 truncated/gap | test_a11_tail_gap_preserves_open_and_closed_immutable |

All fixtures use hand-derived Decimal expectations and public invented
OHLC/funding inputs. This small module does **not** implement full
A09 LIQ/margin, A12 account/day-kill, whole A01-A16, real exchange
queue/fill models, market 24-grid, X1/X3 strategy generation,
paper/live activation or account statistics. No market experiment was run.


### r7 correction-1: X1 must be admitted BEFORE a fill

For X1, use `ShortEntry.for_x1(..., signal_vwap=...,
known_funding_cost=...)` instead of creating an unqualified
`ShortEntry` and discovering rejected economics only when constructing
`ShortExit`. The frozen signal VWAP and already-known funding cost are
evaluated against the candidate entry limit and quantity before a fill
can be admitted. Both Maker legs pay 0.0002; future unknown funding
uses zero for the projection and sets `unknown_future_funding=True`,
never reads or predicts later settlements. When profit side is invalid or
projected net is nonpositive, status is `X1_ADMISSION_REJECTED`;
`events=[]`, open quantity and paid fees are zero, and `process()`
does not accept a fill. Subsequent partial fills are checked against
the same frozen target and known-cost projection. Existing post-entry
X1 checks remain defensive, not the primary admission gate.

```python
from entry_lifecycle import ShortEntry
from exit_lifecycle import ShortExit

candidate = ShortEntry.for_x1(
    "fictional-x1", 0, "100", "100", "1", ".25", 2, "2",
    signal_vwap="99", known_funding_cost=None)
assert candidate.status == "PENDING"
candidate.process({"ts": 0, "open": "100", "high": "100.27005",
                   "low": "99", "close": "100", "closed": True},
                  fill_qty="2")
if candidate.snapshot()["open_qty"] > 0:
    withdrawal = ShortExit(candidate, "X1", vwap=candidate.x1_vwap,
                           known_funding_cost=candidate.x1_known_funding_cost)
```

A03 resting-gap fixture uses an open below E*1.006 when expecting
an open short, and a separate crossing-stop fixture asserts
`CLOSED_SL`, real gap execution fees and independent cash. A pending
order whose frozen offset exceeds .006 produces explicit
`PLACEMENT_INFEASIBLE`, not an exception or a chased order.
Positive controls include discoverable
`test_a06_x1_preentry_rejection_no_paid_fee_or_position` and
`test_a06_x1_preentry_positive_freezes_and_partial_size_gate`.

Prior independent r7 (before this correction) results remain:
49 unique discovery / 47 PASS / 2 FAIL; entry standalone
8 unique / 6 PASS / 2 FAIL; exit standalone 8/8 PASS.
The four mutation modes were NOT_RUN because positive controls failed.
This correction has no Python execution evidence yet; use the existing
r7 commands above in fresh processes and run mutations only after
all positive controls pass.

### r7 correction-2: missing X1 target is a hard pre-entry rejection

`ShortEntry.for_x1(..., signal_vwap=None)` now raises `X2Error`
**before** constructing an entry lifecycle: no pending order, event,
fee, or position is created. The signal-time VWAP target is mandatory
for X1, unlike `known_funding_cost=None`, which remains valid and
sets the explicit unknown-future-funding flag with zero assumed future
cost. X2/X3 may still use ordinary `ShortEntry` without an X1 target.
Do not use the legacy pattern of filling a generic `ShortEntry`
and only afterward passing it to `ShortExit(..., "X1", ...)`.
The discoverable regression is
`Control.test_a06_x1_missing_vwap_target_blocks_before_any_entry`.
Independent correction-1 evidence (51 discovery PASS, 10 entry CONTROL
PASS, 8 exit CONTROL PASS, four mutations caught with AssertionError
rc1) is historical, not a correction-2 test result. The correction-2
suite and mutations are NOT_RUN at source delivery.


## r8 — single offline account (source delivered; acceptance NOT_RUN)

New bounded modules (all standard-library Python and each under 350 lines):
`account_lifecycle.py` orchestrates one active pending entry or short
position, configured cash/margin/leverage, mark-to-market, UTC daily limits,
loss-streak and fixed-duration entry halt. It composes the existing r7
`ShortEntry`/`ShortExit` and does not replace original X2 economics.
`liquidation.py` is a conservative high-crossing LIQ *research proxy*:
frozen threshold E*(1+.0145), and terminal loss equals the **remaining
allocated margin** plus remaining entry notional * .0005.
It is NOT a maintenance-margin/exchange liquidation implementation.
`account_ledger.py` independently replays cash, reservations, fills,
funding and an explicit non-fill `liquidation` event, with closed episode
net PnL. Original `accounting.replay` remains immutable and is compared
on actual ordinary fills/funding (it cannot represent LIQ writeoff).
`tests/test_account_lifecycle.py` and `tests/test_account_ledger.py`
contain publicly invented Decimal fixtures; no optional fixture module
or new dependency is used.

All capital, margin, leverage, loss limit, streak threshold, halt duration,
LIQ percentage and LIQ fee are mandatory explicit arguments — no real
account configuration or amounts are bundled. Example (fictional only):

```python
from account_lifecycle import OfflineAccount

account = OfflineAccount(
    capital="1000", per_trade_margin="50", leverage="5",
    daily_loss_limit="5", loss_streak_limit=2, halt_hours="24",
    liq_pct=".0145", liq_fee_pct=".0005")
account.submit(
    symbol="FICTION", signal_id="example-one", signal_time=0,
    high="100", close="100", atr="1", k=".25", ttl=2,
    qty="2", style="X1", signal_vwap="99",
    known_funding_cost=None)
account.bar({"ts": 0, "open": "100", "high": "100.27005",
             "low": "99", "close": "100", "closed": True},
            entry_qty="2")
result = account.snapshot()
```

An X1 target must pass `ShortEntry.for_x1` before any paid fill.
One pending/open slot is shared across symbols. Reservations reduce
*available* cash but are not cash costs; actual cash changes on gross
realizations, maker/taker fees, settled funding and LIQ debits only.
Partial exits release the proportional margin allocation. Event IDs
are scoped by episode and retain the original `source_id`; seq and UTC
timestamps are checked by independent account replay. The account
checkpoint/recover interface deep-copies trusted in-memory state and
revalidates events; it is NOT a durable filesystem persistence layer.
Tail/gap preserves the last valuation, costs and occupied slot.

UTC day-baseline equity uses the prior known valuation; no invented
midnight mark. Daily realized net and current mark-to-market loss can
block new entries. Loss-streak counts completed net episodes only.
Halts last 24h from the trigger and are latched instead of extended
every unchanged-loss bar. Existing risk exits continue while entry
admission is halted. The account-level LIQ check precedes SL, SL_GAP
and Maker wins (including a conservative observed entry-fillbar
LIQ). Ordinary future execution, queue priority and funding are not
invented. Open equity is cash plus unrealized signed-short PnL;
estimated close fee is separate and not paid.

### r8 independent controls and mutation commands

Run from `engineering/maker_x2` in fresh subprocesses. **First** all
positive controls, including the immutable parent regression suite:

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_account_lifecycle.py control
python3 tests/test_account_ledger.py
```

Only if all controls genuinely PASS, execute each separate negative:

```sh
python3 tests/test_account_lifecycle.py mut-double-entry
python3 tests/test_account_lifecycle.py mut-kill-bypass
python3 tests/test_account_lifecycle.py mut-partial-liq
python3 tests/test_account_lifecycle.py mut-open-mtm
```

Capture each real AssertionError with a named executed test ID and nonzero
exit code. A harness/import/validation error is **not** a caught mutation.
This connector-only source delivery executed no Python, so ALL r8 commands
are `NOT_RUN`. Prior r7 correction-2 independently passed 52 discovery,
11 entry, 8 exit controls, and four mutations; none count as r8 results.
Existing broad CI status does not by itself prove nested r8 tests ran.

| Contract focus | New discoverable control |
|---|---|
| One slot and margin admission | `test_slot_cash_margin_pending_cross_symbol_and_dedup` |
| X1 missing target | `test_x1_missing_target_cannot_pay_or_reserve` |
| MTM/estimated fee | `test_open_position_actual_fee_mtm_no_future_exit_fee` |
| Remaining-qty LIQ | `test_liq_does_not_double_charge_original_margin` |
| Daily MTM halt/risk survival | `test_daily_open_mtm_halt_does_not_block_risk` |
| Two losses and fixed halt | `test_two_complete_losses_halt_exact_24h_and_no_extension` |
| Expiry and open checkpoint | `test_pending_expiry_releases_without_fake_fill`, `test_checkpoint_restores_open_cost_ids_and_gap_tail` |
| Independent reconciliation | `test_independent_real_fees_funding_and_episode_net`, `test_remaining_only_liq_writeoff_not_fake_fill` |

This work is **not** A15/full A16, full A01–A16, production LIQ
or exchange margin validation, full A12 day-kill statistics, whole
A09 scale-out integration, market 24-grid, actual strategy evaluation,
paper/live deployment or performance evidence.


r8 partial pending cancellation returns `PARTIAL_OPEN` when actual entry
quantity exists: only unused order margin is released and a composed
`ShortExit` retains the filled short, hard risk, fees, funding and occupied
slot. With no fill, cancellation releases the full reservation. The
discoverable `test_cancel_pending_partial_releases_unused_only`
checks this handoff. Account-level observed LIQ precedence on entry
fill bars, and gap risk on already open positions, do not imply actual
exchange queue or liquidation ordering.

r8 `checkpoint()`/`recover()` use trusted detached in-memory Python
state, not a disk-backed or cross-process durable format; future
persistence would require its own approved engineering unit.


### r8 correction-1: signed funding and exact pending expiry

Account replay stores the short's remaining `qty` as a **positive**
magnitude, whereas immutable `accounting.replay` stores a negative
signed position. Therefore positive finalized funding rate credits
the short: `cash_delta = remaining_qty * mark * rate` and
`funding_cost = -cash_delta`. The fictional two-unit fixture has
gross `2`, fees `.0796`, funding cost `-2`, cash `1003.9204`
and net episode `3.9204`. The positive test now crosschecks original
`accounting.replay` independently.

An entirely unfilled pending entry continues to reserve margin and occupy
the single slot through both eligible TTL2 minutes j0 and j1. On the
first expired bar opening j2, original `ShortEntry.process` returns
`EXPIRED` and account-level cleanup releases its reservation. This
does not shift TTL cutoff or create a synthetic fill. Previous independently
observed r8 failures (discovery 68: 66 pass/2 fail, account standalone
13: 12 pass/1 fail, ledger standalone 3: 2 pass/1 fail) remain
historical evidence. Correction-1 execution is NOT_RUN at handback.


## r9 — causal S1/S2 signals and shared matched placebo sampler

New modules: `signal_features.py` (completed-minute feature and signal
generation) and `matched_signals.py` (single framework-level matched
decision-time sample). New synthetic test modules:
`tests/test_signal_features.py` and `tests/test_matched_signals.py`.
They use only standard-library Decimal50 and local `random.Random`,
with no market source, CSV, runner, private configuration, paid tool or
real fills/PnL.

Public API from `engineering/maker_x2`:

```python
from signal_features import validate_minutes, framework_bars
from signal_features import eligible_decisions, signal_records
from matched_signals import select_matched, sample_completed

# closed_minutes: fictional dicts with ts UTC 1m open-milliseconds,
# open/high/low/close/base_volume/quote_volume and closed=True.
eligible = eligible_decisions(closed_minutes, framework_minutes=1)
s1 = signal_records(closed_minutes, framework_minutes=1)
match = select_matched(eligible, framework_minutes=1,
                       evaluation_start=0, evaluation_end=60000000000)
```

Framework is **only** 1m/S1/C1 or 15m/S2/C2. For 15m, every UTC-aligned
15-minute bucket must contain 15 contiguous, complete 1m bars; no forming
bucket, padded gap or interpolated close is allowed. Signal timestamp is
the close of the last component minute, i.e. its open time + 60,000ms.
Both frozen high and close are of the completed framework bar. The result
retains the last underlying 1m timestamp and original input index,
`atr14` and `vwap60` (always from complete **1m** bars up through the
decision), immutable signal ID, type and precise feature values.

Returns are `close[i]/close[i-1]-1`; prior-only sample
`sigma288` has 288 complete returns with ddof=1. Prior-only
`std12` has 12 returns with ddof=1. `H12/L12` exclude current bar.
`r6=close[i]/close[i-6]-1`, and `z=r6/(sigma288*sqrt(6))`.
S1 fires **strictly** at z > 2.5 (1m only).
S2 fires at std12 <= 0.5*sigma288 **and** current close < prior L12
(15m only). Zero sigma, invalid volumes, incomplete history or
discontinuous windows exclude eligibility explicitly rather than
synthesizing returns. No downstream fill or future settlement condition
filters any record, including a valid signal at the data tail.

The matched sampler consumes all feature-eligible closed decisions,
**not** only signals. A caller-supplied [start,end) evaluation window is
applied to decision timestamps. Canonical sorted unique times are
hashed with framework/window information using SHA256 before sampling.
The fixed research default is **6000 without replacement** per framework,
with seed **20261020 + framework_minutes** (1m=20261021,
15m=20261035). One identical set is shared by every .25/.50,
TTL2/5, X1/X2/X3 combination within its framework. Smaller `count`
is for clearly fictional unit fixtures only. Short universes return
`INSUFFICIENT_UNIVERSE`, requested/eligible counts and an empty
selection; duplicates reject instead of inflating size. This is a
signal sampler, not matched account replay, and no zero placebo-gross
assumption is imposed.

### r9 exact controls and negative mutations

From `engineering/maker_x2`, run positive controls **first** in
fresh subprocesses:

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_signal_features.py control
python3 tests/test_matched_signals.py control
```

Only after ALL positive controls pass, execute each negative process
separately; each must fail with a named genuine AssertionError and
nonzero rc, not merely ImportError or an unrelated harness exception:

```sh
python3 tests/test_signal_features.py mut-lookahead
python3 tests/test_matched_signals.py mut-replacement
python3 tests/test_matched_signals.py mut-resample
```

r9 source-delivery execution is **NOT_RUN** because the authorized
GitHub file connector has no Python execution facility. Parent r8
independent 68/68 discovery, account 13/13, ledger 3/3, entry11/11
and exit8/8 successes and four caught r8 mutations are historical
evidence only, not r9 acceptance.

This is a **partial A15 signal sampler**. Matched account replay,
bootstrap/BH24, market 24-grid runner/statistics, full A01–A16,
strategy/product acceptance and paper/live deployment are
**NOT_ACCEPTED** and have not been executed.
