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


### r10 test-acceptance correction (synthetic only; NOT_RUN)

No production formula, sampler, entry, exit, ledger or account module was
modified. This correction strengthens **discoverable** tests rather than
claiming an established production defect.

`StrategyApiControls.test_s1_real_api_below_above_and_controlled_exact_equality`
evaluates actual 1m `eligible_decisions` and `signal_records` for
below/above threshold. Exact `z==2.5` is tested using a **controlled
threshold-only Decimal constructor injection**, because finite fictional
Decimal OHLC data does not guarantee literal equality after sqrt(6).
The production feature calculation, API, and strict comparison still run;
that exact equality case is NOT an unmocked OHLC equality proof.
`test_s2_real_api_positive_close_equality_and_volatility_gate`
evaluates complete 15m UTC bars for a real positive S2, close==prior L12
strict rejection, and a high-std12 rejection; its sample standard deviations
are derived by a separate test reference with Decimal50.

Additional `sample_completed` controls use complete synthetic 1m and
15m bars, a [start,end) causal decision window, tail/suffix stability,
non-triggered eligible decisions, and identical framework-level samples
for all 12 k/TTL/exit combinations. Existing default 6000 unique samples,
seed metadata, global RNG preservation, insufficient and duplicate controls
remain in normal discovery.

Mutation `mut-lookahead` now actually reads the last input minute's
quote/base volume and injects it into historical feature VWAP; prefix
invariance must fail. `mut-resample` keeps reported seed constant but
changes the actual selected timestamps on alternate identical invocations;
the framework-sharing control must fail. `mut-replacement` still injects
duplicate selected times. These are explicitly fault-injected synthetic
tests, not production edits.

From `engineering/maker_x2`, positives first:

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_signal_features.py control
python3 tests/test_matched_signals.py control
```

Only if all controls PASS, separate processes:

```sh
python3 tests/test_signal_features.py mut-lookahead
python3 tests/test_matched_signals.py mut-replacement
python3 tests/test_matched_signals.py mut-resample
```

Each negative requires a genuine AssertionError, loaded executed test ID
and nonzero exit code, not a harness/import error. In this GitHub-only
delivery these r10 commands are **NOT_RUN**. Independently accepted r9
history remains: discovery 78/78; standalone signal5/5, matched5/5,
original three mutations with named AssertionError rc1, exact-head CI
39460187430/39460195244 success (CI did not itself discover nested tests).
That r9 mutation history did not prove future-read/resampling behavior.

A15 here remains only a partial signal sampler. Matched account replay,
bootstrap/BH24, historical runner/24-grid, and full A01–A16/product or
paper/live acceptance are **NOT_ACCEPTED**.


### r11 exact input-tail sampler acceptance (tests-only)

Added discoverable positive controls
`Control.test_sample_completed_1m_exact_input_tail_included` and
`Control.test_sample_completed_15m_exact_input_tail_included`.
Both call the unmocked public `sample_completed` on fully closed,
fictional 1m input bars, with the evaluation end **strictly after** the
actual last input decision time and a fixture count equal to the
**entire** eligible interval, so sampling cannot omit the tail by chance.

The 1m fixture has 302 bars: final open=301*60000 ms; final
decision=302*60000 ms; [start,end)=[300,303) minutes; selected decision
minutes **exactly [300,301,302]**, including the input tail. The 15m
fixture has 294 complete UTC-aligned buckets (4410 input minutes):
final decision=294*15*60000 ms; [start,end)=[292,295) 15m periods;
selected decisions **exactly [292,293,294]** framework boundaries.
Each independently asserts READY, eligible=3, requested=3, unique
complete selected set and tail identity. No execution bar, future fill,
or PnL is required. Existing narrower fixed-window, RNG, duplicate,
insufficient and 12-variant tests remain unchanged.

From `engineering/maker_x2`, run positives in fresh subprocesses:

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_matched_signals.py control
python3 tests/test_signal_features.py control
```

Only after every positive passes, run each genuine negative in a
separate subprocess, requiring AssertionError/test ID/nonzero rc:

```sh
python3 tests/test_signal_features.py mut-lookahead
python3 tests/test_matched_signals.py mut-replacement
python3 tests/test_matched_signals.py mut-resample
```

r11 is **NOT_RUN** in the connector-only source handback; r10
discovery82/82, signal7/7 and matched7/7 plus three named mutation
AssertionError rc1 and successful push/PR CI are historical r10
evidence only. CI does not itself discover the nested namespace.
This closes just exact-input-tail test coverage of the partial A15
sampler. Matched account replay, BH24/bootstrap, runner/24grid,
whole A01–A16/product/strategy/paper/live remain **NOT_ACCEPTED**.


## r12 — offline historical integration (source delivered, blocked acceptance)

New source, all stdlib and each .py shorter than 350 lines:
`data_adapter.py` reads raw Binance 12-field kline arrays or the
cached `open_time,open,high,low,close,volume,quote_volume` CSV,
plus JSON/JSONL/JSON.GZ and finalized
`funding_time_ms,rate` CSV or funding dictionaries. It inventories
gaps and hashes source bytes without retimestamping or padding.
Missing funding mark uses a causally known completed close proxy.
`market_replay.py` actually advances the existing immutable
`OfflineAccount` one closed 1m bar at a time, with true slot, TTL,
Maker penetration, LIQ/SL priority, funding, halts, tail, cash/equity,
and raw episode/ledger events. Synthetic OHLC means full-fill-or-miss,
not order-book partial-fill calibration.
`matched_replay.py` routes placebo timestamps into the **same**
account replay, rather than averaging signal-only outcomes.
`grid_runner.py` loops ETHUSDT and BTCUSDT **separately**, each with
two frameworks and twelve k/TTL/exit choices (48 symbol-cells),
reusing each symbol/framework eligible universe and matched sample
across all twelve variants, with no combined capital.
`backtest_stats.py` provides descriptive cash/PF/bp rates, full
episode entry-notional denominators, UTC13–16 strata, explicit
TP/(TP+SL+SL_GAP) denominator, moving 7-calendar-day
null-centered 9,999-block bootstrap and separate ETH BH24,
BTC BH24 and combined BH48 sensitivity flags; zero days are retained.
Insufficient episodes/entry dates return p=1 **placeholder**
with unavailable CI. Statistical outcomes are exploratory only.
`backtest_reports.py` persists raw strategy/placebo events, episodes,
ledger/equity timelines and per-cell results, plus config/input,
source/test and output SHA256; `--verify` checks those hashes.
`backtest_entry.py` provides the CLI, and `backtest_controls.py`
runs a synthetic money/position control plus actual test discovery.
`tests/test_backtest.py` and `tests/test_backtest_mutations.py`
contain synthetic adapter, closed X1 cash/fee, account/replay, checksum
and negative controls. No historic data or market runner was executed
during this GitHub-only source delivery.

All account capital, reserved margin, leverage, daily-loss/streak/24h
halt and entry_qty come from the operator's explicit local JSON config.
That config is never committed and no private amount defaults exist.
It must contain `capital,per_trade_margin,leverage,daily_loss_limit,
loss_streak_limit,halt_hours,liq_pct,liq_fee_pct,entry_qty,
evaluation_start,evaluation_end`. History bounds are fixed at
2023-11-01 00:00 UTC (inclusive) to 2026-10-19 00:00 UTC
(exclusive); 2023-10-01 warmup must be covered. No silent
shortening is accepted. The local input manifest shape is:

```json
{
  "ETHUSDT": {"bars": "eth-bars.csv", "funding": "eth-funding.csv"},
  "BTCUSDT": {"bars": "btc-bars.json", "funding": "btc-funding.csv"}
}
```

These are **placeholder filenames**, not attached or distributed market
files. Raw Binance arrays and funding dictionaries are supported as
specified above. When the operator's actual local paths are known,
only the operator supplies them; no network fetching is performed.

### Runnable CLI (cwd: engineering/maker_x2)

```sh
python3 backtest_entry.py --self-test
python3 backtest_entry.py --run --config config.json --input inputs.json --output new-results
python3 backtest_entry.py --verify --config config.json --input inputs.json --output new-results
```

The CLI's `--run` re-runs the discoverable positive test gate, rejects
shortened historical coverage, computes 48 strategy/matched baseline
account cells and a **separate** 48-cell actual-arrival delay2
sensitivity, then writes immutable raw reports and manifests.
Delay2 moves eligible order *delivery*, not the feature decision
clock. `--verify` checks original data/config/source and output
hashes; a zero exit status is necessary but is not acceptance.

### CONTROL and mutation commands (fresh independent subprocesses)

```sh
python3 -m unittest discover -s tests -v
python3 tests/test_backtest.py -v
python3 tests/test_backtest_mutations.py control
python3 tests/test_x2.py control
python3 tests/test_entry_lifecycle.py control
python3 tests/test_exit_lifecycle.py control
python3 tests/test_account_lifecycle.py control
python3 tests/test_account_ledger.py
python3 tests/test_signal_features.py control
python3 tests/test_matched_signals.py control
```

Only after **every** positive control PASS, test actual failures:

```sh
python3 tests/test_backtest_mutations.py mut-fee
python3 tests/test_backtest_mutations.py mut-funding
python3 tests/test_backtest_mutations.py mut-fake-time
python3 tests/test_backtest_mutations.py mut-future
python3 tests/test_backtest_mutations.py mut-overfill
python3 tests/test_x2.py mut-be
python3 tests/test_x2.py mut-duplicate
python3 tests/test_signal_features.py mut-lookahead
python3 tests/test_matched_signals.py mut-replacement
python3 tests/test_matched_signals.py mut-resample
```

Require named executed test IDs, actual AssertionError and nonzero
process rc. An unrelated exception or failure to import is NOT
a caught mutation. No r12 Python execution or CI run took place
through this connector. The independently accepted r11 84 controls
and earlier mutations/CI are *historical*, not evidence for these
new modules.

### Explicit r12 acceptance blockers — not a full engine PASS

- `fee_slip_1_5x_status=NOT_IMPLEMENTED_NEEDS_AUTHORIZED_GLUE`.
  Current frozen account/ledger and X1/X2/X3 modules embed fee and
  slip constants in actual fills, fee booking, risk, LIQ and halt
  decisions. A report-only cash adjustment would be false account
  replay. Fully correct 1.5x sensitivity requires separately approved
  optional injection of scenario cost rates (frozen defaults unchanged)
  in the protected `maker_exit_lab.py`, `entry_lifecycle.py`,
  `exit_lifecycle.py`, `account_lifecycle.py`,
  `account_ledger.py` and `liquidation.py`, plus differential tests.
  No such outside-allowlist edit was attempted.
- Same-bar favorable tick-order *upper bound* is not calculated.
  Conservative LIQ>SL>TP is the actual primary replay; conflicts are
  counted and favorable bound explicitly `NOT_COMPUTED`.
- Real depth, partial maker queue fills, actual exchange LIQ and
  mark-price settlement are unverified. Synthetic tests cannot
  establish historical fill realism; no private account data were used.
- The new end-to-end code, run-time memory/performance,
  statistics and report integration have **NOT_RUN** independent
  acceptance. The code must not be used to assert research alpha,
  strategy approval, full A01–A16 or deployment readiness.

All 48 baseline and 48 delay2 rows are designed to be descriptive,
with all neighbors retained; no results were selected, optimized or
observed in this response. The original historical data are considered
exploration-contaminated with no holdout. ETH/BTC capital is never
summed into a single portfolio. Full 1.5x rows and corresponding
final preregistration acceptance remain **BLOCKED**, not silently
fabricated.


### r12 final source-scope notes

`equity_path` stores the final **observed** close valuation per UTC
calendar day (including zero-trade days), with `stale` flag and the
original observed close timestamp. It never creates a midnight mark.
All actual orders, funding, margin and liquidation account events are
preserved individually, with the complete flat-to-flat episode stream.
The new control `test_grid_has_48_isolated_cells_and_shared_sampler`
checks 48 unique symbol-cells and 96 account-replay calls (48 strategy
plus 48 matched) through a mocked orchestration fixture; it is **not**
a replacement for an actual historical run. The separate tests
`test_handderived_full_x1_flat_episode_reconciles` and
`test_delay2_moves_real_pending_arrival_not_feature_decision`
call the real frozen account modules with hand-derived fictional
money and actual changed arrival time.

Eligible decisions are computed once per symbol/framework and shared
with all 12 real and matched variants and the delay2 scenario;
production signal features and sampler constants remain unchanged.
The CLI tests all existing/new discoverable controls before allowing
`--run`, and only afterward checks full October-2023 input warmup and
the exact frozen November-2023 to October-2026 window. It does not
automatically run the negative mutations; the operator must execute
those separately and record real failures before historical execution.

r12 remains **BLOCKED for complete preregistration**, because an
economically truthful fee/slippage1.5x account replay requires
protected-module scenario injection, and the favorable same-bar
upper bound is not yet computed. The baseline/delay2 executable
paths are reviewable source but have NOT_RUN acceptance here.
`OUTPUT_WRITTEN_NOT_ACCEPTED` and an exit code of zero must never be
interpreted as an approved 48-grid research result.


## r12-correction-1 — bounded full-account scenario and positive-test repair

**Status at source delivery: execution NOT_RUN.** Parent r12 was independently
tested at its exact head: 96 unique, 92 PASS, 3 FAIL, 1 ERROR, rc1;
negative mutations were NOT_RUN. Those failures remain recorded, not
relabeled as passing. This revision fixes the observed fixture defects
and adds executable acceptance controls, but requires the Main CIO's
fresh exact-head Python execution before accepting any result.

Measured repairs: finalized funding CSV may normalize `.01` to
`0.01`; compare exact Decimal equality. A fictional high=close+1
previously made posted entry infeasible under frozen >0.006 rejection;
test minutes now have a feasible low-volatility high/low and retain
real slot, TTL and no-chase. Delay2 checks actual new reserve event
timestamps with the same immutable signal-time and a full two-minute
arrival shift. Tail TIME control checks actual event type/side/qty and
does not confuse a legitimate `time-open:` signal scope with a time
exit. The deliberately introduced fake TIME *buy fill* is schema-valid
and must be caught by an actual AssertionError.

### Optional cost-scenario interface (BASE unchanged)

`scenario_rates.py` is the only new module. `ScenarioRates()`
has frozen baseline Maker fee=0.0002, Taker fee=0.0005,
adverse SL slip=0.0002, LIQ fee=0.0005. The sole alternate
`ScenarioRates("1.5")` multiplies those **economic** costs by 1.5.
Neither scenario changes 2bp Maker queue penetration, original
hard SL=.006, LIQ crossing=.0145, TP targets, UTC timing,
signals, fill limits or position direction.

Authorized minimal protected-file changes (all default arguments
retain exact baseline behavior):
- `maker_exit_lab.py`: optional `rates` in MakerX2, actual fill fees,
  adverse SL price, and X2 next-bar economic BE denominator
- `entry_lifecycle.py`: optional rates, X1 pre-entry paid-fee gate and
  filled hard-SL slip; baseline 2bp penetration remains unchanged
- `exit_lifecycle.py`: same rates through core/X1 projection/actual
  risk exit and estimated future close fee; Decimal50 paid-entry-fee
  reconstruction
- `account_lifecycle.py`: propagate scenario to entry/exit/account
  replay; actual fee-aware reservation, risk, LIQ, and separate
  estimated unpaid close cost
- `account_ledger.py`: scenario Maker/Taker actual per-fill fee
  reconstruction while retaining funding and LIQ writeoff identity
- `liquidation.py`: optional LIQ fee multiplier, not an alteration of
  the frozen .0145 threshold or remaining-margin allocation

`market_replay.py` computes candidate quantity in Decimal50:
`qty=(per_trade_margin*leverage)/posted_entry_limit`, using the
actual symbol/decision limit. Fixed `entry_qty` is no longer
required by the CLI and is not used to size BTC or ETH. Real
accepted X1/X2/X3 and matched placebo pass through the same scenario
account, not a post-hoc fee subtraction. BTC/ETH are never pooled.

`grid_runner.py` executes 48 baseline symbol-cells, 48 separate
delay2 cells and 48 separate **1.5x economic-cost** cells, each with
a real strategy account and a real matched-placebo account. One causal
eligible universe and one matched sample per symbol/framework are
reused across all variants and scenarios. Hashes include the symbol
identity even if ETH/BTC have identical timestamps. Separate ETH/BTC
BH24 and combined BH48 remain descriptive only. Open holdings, known
funding, paid fees, LIQ margin writeoff, daily halts and kill gates
all use the actual scenario account, not synthetic terminal returns.

The account exhaustion flag is derived from actual insufficient-funds
admissions and final available cash versus configured margin;
nonexhaustion and no administrative tail-open are required for
candidate eligibility. The 7-day null-centered calendar bootstrap
retains zero-trade days, seed 20261020, 9999 samples, with
return statistics expressed in **bp per initial entry notional**
rather than mixing USD with bp confidence bounds. Each complete
flat-to-flat episode is one observation. Insufficient observations
still yield p=1 **placeholder**, CI unavailable. No optimization or
post-hoc holdout claim is authorized.

Observed same-bar risk/TP conflicts are separately counted. The
favorable-order field is a non-executable upper-bound *research proxy*
over frozen one-bar alternative stop and remaining-qty LIQ/TP
allocations, never a change to primary conservative
LIQ>SL/SL_GAP>Maker ordering or a claim about real ticks.

### Corrected controls, differential checks and mutation execution

From `engineering/maker_x2`, run positives **before any mutation or
historical --run**:

```sh
python3 -m unittest discover -s tests -v
python3 backtest_entry.py --self-test
python3 tests/test_backtest.py -v
python3 tests/test_backtest_mutations.py control
python3 tests/test_x2.py control
python3 tests/test_entry_lifecycle.py control
python3 tests/test_exit_lifecycle.py control
python3 tests/test_account_lifecycle.py control
python3 tests/test_account_ledger.py
python3 tests/test_signal_features.py control
python3 tests/test_matched_signals.py control
```

The new standalone discoverable tests include
`test_baseline_differential_and_1_5x_real_account_cash`
(default BASE equals explicit BASE byte-identically, X1 fees 0.0797,
1.5x fees 0.11955, net cash 1002.38045),
`test_cost_sensitivity_changes_actual_sl_slippage_and_fees`
(actual 1.5x short stop cash 998.22770455 vs BASE
998.3184798),
`test_explicit_margin_leverage_sets_actual_candidate_notional`,
`test_samebar_counterfactual_does_not_overwrite_primary_liq`,
and `test_real_48_grid_baseline_and_full_placebo` which calls
unmocked 1m/15m signal and matched account playback with public
fictional bars and a deliberately reduced sampler count.
No real market prices/performance are asserted.

Only after every positive passes, execute separate negative processes:

```sh
python3 tests/test_backtest_mutations.py mut-fee
python3 tests/test_backtest_mutations.py mut-funding
python3 tests/test_backtest_mutations.py mut-fake-time
python3 tests/test_backtest_mutations.py mut-future
python3 tests/test_backtest_mutations.py mut-overfill
python3 tests/test_x2.py mut-be
python3 tests/test_x2.py mut-duplicate
python3 tests/test_signal_features.py mut-lookahead
python3 tests/test_matched_signals.py mut-replacement
python3 tests/test_matched_signals.py mut-resample
```

Negative success requires a genuine named AssertionError, executed
test ID and nonzero rc, never an import error, unsupported operation
or harness failure. Then check exact push and pull_request CI;
CI by itself need not discover the nested maker_x2 tests.
Only Main, after accepting the complete engineering gate, may run
the operator's **existing local** history/config with:
`python3 backtest_entry.py --run --config config.json --input inputs.json
--output new-results`, followed by `--verify` with the same arguments.
No historical CLI, market acquisition or CI was executed in this
GitHub-only turn.

Full 2023-10-01 warmup and
[2023-11-01,2026-10-19) exploration coverage remains mandatory.
The adapter never interpolates gaps, retimestamps bars or filters on
future returns. Raw reports are tagged EXPLORATORY_NOT_ACCEPTED
even if a CLI process exits zero. This is still not paper/live,
strategy approval, a new holdout or whole A01–A16 acceptance.
Performance on the actual multi-year local data is **not benchmarked**;
daily observed equity replaces millions of per-cell minute snapshots,
but reading original bars and repeated account updates may remain
resource intensive. Main should record actual runtime and output
integrity before admitting historical exploration.


## r12-correction-2 — final authorized tests-only revision

The prior independent immutable checkout of correction-1 ran
`python3 backtest_entry.py --self-test`: **101 unique tests,
100 PASS, 1 ERROR, rc1**. All original 84 controls and the real
non-mocked 48-cell account test passed. The sole error,
`Control.test_grid_has_48_isolated_cells_and_shared_sampler`, came
from a deliberately mocked replay with an incomplete ledger missing
`available`. That was a test fixture error, not evidence that
production exhaustion semantics should be weakened. The prior exact
push/PR CI runs 39507232863 and 39507238516 were successful but
did not discover this nested suite. Mutations and historical market
runs remained NOT_RUN.

This revision changes only `tests/test_backtest.py`, this README,
and the formal mailbox. The fictional flat mocked account now exposes
the independent replay ledger contract: cash=1000, reserved=0,
available=1000, qty=0, basis/gross/fees/funding/writeoff=0, no
episodes or IDs, equity=1000, unrealized=0. The test separately
asserts `available=cash-reserved`, `equity=cash+unrealized`,
and the zero position/reservation, while retaining **all** original
48-cell, four sampler universe, 96 strategy+matched replay, and unique
cell-ID assertions. No production code or other test changed.
There is no third bounded correction authorized.

In the `engineering/maker_x2` directory, Main CIO runs positives
before any negative mutation:

```sh
python3 backtest_entry.py --self-test
python3 -m unittest discover -s tests -v
python3 tests/test_backtest.py -v
python3 tests/test_backtest_mutations.py control
python3 tests/test_x2.py control
python3 tests/test_entry_lifecycle.py control
python3 tests/test_exit_lifecycle.py control
python3 tests/test_account_lifecycle.py control
python3 tests/test_account_ledger.py
python3 tests/test_signal_features.py control
python3 tests/test_matched_signals.py control
```

Only if **all** positives PASS, run each genuine mutation as an
independent process and inspect an executed test ID, real
`AssertionError` and nonzero rc (not import/harness failure):

```sh
python3 tests/test_backtest_mutations.py mut-fee
python3 tests/test_backtest_mutations.py mut-funding
python3 tests/test_backtest_mutations.py mut-fake-time
python3 tests/test_backtest_mutations.py mut-future
python3 tests/test_backtest_mutations.py mut-overfill
python3 tests/test_x2.py mut-be
python3 tests/test_x2.py mut-duplicate
python3 tests/test_signal_features.py mut-lookahead
python3 tests/test_matched_signals.py mut-replacement
python3 tests/test_matched_signals.py mut-resample
```

All correction-2 execution/CI results are **NOT_RUN** in this
GitHub-file-only delivery. Do not relabel the prior 101-test failure
or successful broad CI as correction-2 acceptance. Only after
independent exact-head engineering gates PASS may the operator use
already-local full BTC/ETH historical inputs and explicit local
config with the documented `--run` and `--verify` CLI. No data
window, sample count, account economic threshold or other strategy
parameter is changed by this fixture repair. Results remain
exploratory, not A01-A16/product/trading approval.
