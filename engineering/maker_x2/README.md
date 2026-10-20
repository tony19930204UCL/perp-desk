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

signal = ShortEntry("fictional-1", 0, "100", "100", "1",
                    ".25", 2, "2")
first_minute = {"ts": 0, "open": "100", "high": "100.27005",
                "low": "99", "close": "100", "closed": True}
signal.process(first_minute, fill_qty="2")
withdrawal = ShortExit(signal, "X1", vwap="99")
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
