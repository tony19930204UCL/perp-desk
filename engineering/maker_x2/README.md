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
