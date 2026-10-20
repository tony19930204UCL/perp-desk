# Maker X2: offline short-fill accounting checks

This namespace contains an independent, standard-library-only Decimal ledger
candidate (`accounting.py`) and deterministic unit tests
(`tests/test_accounting.py`). It is engineering research, **not** a trading
engine, deployment, approval, or live/paper execution.

## Run

From `engineering/maker_x2` at this branch's checked-out source:

```sh
python3 -m unittest discover -s tests -v
```

The tests import `accounting.replay(initial_cash, events)` and
`accounting.LedgerError` directly. They use public fictional prices, sizes,
and timestamps, plus independently hand-derived literal Decimal expectations.
No external packages, private configuration, real balances, network access, or
exchange accounts are needed.

## Input contract

Each event is a dictionary with unique nonempty string `id`, contiguous
integer `seq` starting at zero, nondecreasing nonnegative integer UTC-ms
`ts`, and `type`. Ordered events are **not sorted**.

* `fill`: `side` (`sell` or `buy`), positive `qty`, positive `price`,
  `liquidity` (`maker` or `taker`).
* `funding`: unique nonempty `settlement_id`, positive `mark`,
  signed finite `rate`.

Numeric inputs use decimal strings, integers, or finite Decimal values; binary
floats, booleans, nonfinite values and malformed transitions are rejected.
The module is short-only and rejects buying beyond an existing short.

Maker fee is 0.0002 and taker fee is 0.0005 on **every actual fill**.
Opening sale notional is not deposited to cash. Closing realized gross PnL is
(entry basis average - actual fill price) times closed quantity.
Funding cost equals remaining signed quantity times mark times rate; negative
cost credits short cash for positive rates. At equal timestamps, the event
with lower seq executes first, including settlement versus partial exit.

Returned cash reconciles to initial cash + realized gross - fees - funding
cost. A partially open short retains its signed quantity and remaining entry
basis; no unrealized PnL, external mark, liquidation, synthetic close, or
final equity is inferred.

## Coverage and limitations

Tests exercise two unequal-price short entries, weighted half close, maker
and taker fees by leg, funding of both signs, funding after partial close,
same-timestamp sequencing, actual gap exit price, full close and open-position
semantics, reconciliation, and malformed IDs, settlement IDs, ordering,
timestamps, types, quantities, prices, and numeric inputs.

This commit provides test **source**, not an independently executed pass.
The GitHub contents connector does not run `python3 -m unittest`; that
command must be executed on the exact committed source by Main CIO.
Existing repository CI may not discover this nested namespace, so do not
interpret unrelated green checks as Maker X2 acceptance.

The operator-provided `accounting.py` was left byte-for-byte unchanged.
Its provenance hash and readable nonce do not certify original CodeMirror
fidelity or actual test behavior. The broader Maker V3/X2 engine and A01-A16
are outside this deliverable and remain unverified. No 24-grid/12-arm research,
paper or live activation, deployment, or change to main is authorized.
