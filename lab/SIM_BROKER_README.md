# Offline conservative simulated broker

`sim_broker.py` is a Python 3.14 standard-library **engine library**, not a strategy, collector, account client, risk approver, optimizer, or runtime service. It has no network calls, credentials, private/trading APIs, or live execution interface. All executions are explicitly tagged `sim_only=True`.

No runtime paper process or live trades were activated by this work. No risk configuration was authored, read, or changed. The numerical contracts in `tests/test_sim_broker.py` are **ARTIFICIAL UNIT-TEST FIXTURES ONLY**, not approval for any account. Initial cash is caller-supplied capital, not risk approval.

## Run verification

From this directory:

```sh
PYTHONDONTWRITEBYTECODE=1 python3.14 -m unittest discover -s tests -p test_sim_broker.py -v
PYTHONDONTWRITEBYTECODE=1 python3.14 sim_broker.py
PYTHONDONTWRITEBYTECODE=1 python3.14 -m unittest discover -s tests -v
```

`self_check()` and the CLI run the real artificial unit fixtures, excluding the self-check wrapper test to avoid recursion. They return/print **ARTIFICIAL ENGINEERING SELF-CHECK — NOT MARKET PERFORMANCE**. They do not manufacture market returns, balances, or approvals. All fixture databases live in temporary directories under the active profile's `cache/scratch` and are cleaned up. Actual sequential RED/GREEN outputs, including regressions and fixture corrections, are in `evidence/tdd_sim_broker.txt`.

## API and externally supplied inputs

```python
from decimal import Decimal
from sim_broker import InstrumentSettings, ExecutionModel, RiskContract, Intent, SimBroker

# All variables below come from the caller's verified observations/settings.
settings = InstrumentSettings(
    symbol=symbol, maker_fee=maker_fee, taker_fee=taker_fee,
    quantity_step=quantity_step, tick=tick, min_notional=min_notional,
    max_quantity=max_quantity, min_quantity=min_quantity, category=category,
)
model = ExecutionModel(
    latency_ms=latency_ms,
    exit_slippage_ticks=exit_risk_reservation_ticks,
    depth_extra_ticks=None,  # explicit reject-beyond-depth policy
    entry_slippage_ticks=entry_adverse_fill_ticks,
    exit_fill_slippage_ticks=exit_adverse_fill_ticks,
)
contract = RiskContract(**externally_approved_contract)
broker = SimBroker(
    isolated_database_path, initial_cash=initial_cash,
    instruments=[settings], execution=model, risk=contract,
    version_id=externally_frozen_version_id, forward_start=forward_start_ms,
)
try:
    broker.on_event(public_event)
    order = broker.submit(intent)
finally:
    broker.close()
```

Use `Decimal` for all instrument settings, contract monetary/exposure values, and intent prices/quantities. Event numeric values may be exact decimal strings, integers, or `Decimal`; floats, booleans-as-numbers, NaN, and infinities are rejected. Public methods isolate arithmetic in a deterministic 40-significant-digit `ROUND_HALF_EVEN` context. There is no binary floating-point accounting or fee rounding to cents. Arithmetic beyond the fixed context precision can round; repeating weighted averages are not rational-number storage.

Fees are explicit **nonnegative fractional rates**, not percentages. Zero fees must be explicitly supplied. No VIP tier, TradFi/crypto promotion, exchange fee schedule, leverage, or risk limit is guessed. `min_quantity` and `category` are optional external metadata/filter inputs; callers should supply them from verified instrument specifications when available. An omitted minimum quantity does not establish that the instrument has no additional exchange filter.

`ExecutionModel` separates three concepts:

- `latency_ms`: explicit nonnegative decision-to-arrival delay.
- `exit_slippage_ticks`: explicit stop-risk reservation, including conservative exit fees; at least the optional actual exit-fill impact is reserved.
- `entry_slippage_ticks` / `exit_fill_slippage_ticks`: optional explicit adverse price adjustments on taker fills. `None` means observed book pricing without additional impact. Positive values worsen buys upward and sells downward, preserve observed available quantities, and are tagged in each fill with `observed_price` and `fill_slippage_ticks`.
- `depth_extra_ticks`: `None` rejects an opening that exceeds recorded depth; a positive supplied integer permits a distinctly tagged modeled-depth remainder at the last recorded level plus adverse ticks. An entirely missing side never receives a fabricated quote. Reductions can instead consume recorded depth partially and remain pending.

### RiskContract

Opening requires all of these externally approved fields:

- `approved=True`, nonempty `version`
- finite positive `Decimal` values for `max_loss_per_trade_usdt`, `max_daily_loss_usdt`, `max_effective_exposure_x`, `total_loss_limit_usdt`
- positive integer `max_positions`
- `require_stop=True`

There are **no production default risk numbers**. Missing, incomplete, unapproved, or invalid contracts reject openings with `risk_missing_or_unapproved`; the audit retains the reason. Valid reductions of already-known positions do not depend on this contract or on funding readiness. This broker conservatively requires stops for every opening even if a supplied contract attempts to waive them.

Opening checks include signed BUY/SELL, finite positive quantity, exact quantity-step grid, externally matched tick/step/notional/max-quantity filters, optional minimum quantity, on-tick stop/limit, min-notional at arrival, correct stop side at execution price, distinct-position limit, aggregate same-symbol stop risk, existing portfolio stop-cost reservations against daily/total loss budgets, and pre/post effective notional exposure. Fees and explicit exit slippage are reserved. Daily accounting uses UTC millisecond event-day buckets and the equity before the first observed event in that day; gaps across midnight are counted when the next mark arrives. Missing required marks/day baseline fails closed. Opposite-side opening is not a reversal shortcut: close/reduce first.

These are **ex-ante modeled bounds**, not guarantees: gaps and actual funding can make realized losses exceed a stop reservation. The library records such adverse outcomes rather than clipping a loss or restoring cash. It is not a liquidation or exchange margin model. Leverage selection, scheduling, maximum holding time, and strategy decisions belong to the external caller; closing decisions use ordinary reducing intents.

### Intent

Required dataclass fields are:

```text
intent_id, symbol, side ('BUY'|'SELL'), qty, stop,
decision_ts (integer milliseconds), quantity_step, tick,
min_notional, max_quantity, kind ('TAKER'|'MAKER'), reduce_only (bool)
```

Maker-specific optional fields: `limit_price`, `queue_ahead_qty`, `expires_ts`.

- IDs are caller supplied and deterministic. Orders use `order:<intent_id>`; fills/ledger records use monotonically persisted sequence IDs.
- Exact repeated intents return their existing order; conflicting reuse raises `ValueError`.
- Openings need stops. Reductions may use `stop=None`, but must have the correct opposite direction and not exceed the currently known position.
- Symbols and instrument specifications must match externally registered settings. Stops cannot be silently changed by adding to a position.
- `submit()` returns an independent snapshot; later matching never mutates that returned object.

## Public event schema

Canonical shared fields:

```text
event_id: stable nonempty string
symbol: registered symbol
type: book | aggTrade | mark | funding | funding_status
ts: integer millisecond observation/processing timestamp
source_ts: optional exact underlying source timestamp, <= ts
source/category/other provenance: optional externally supplied metadata
```

For market events, **without `source_ts`, `ts` must represent the actual source event timestamp**, not a fabricated timestamp for an old quote. Out-of-order market observations are rejected; exact duplicates are idempotent even after restart. Inputs and decisions before `forward_start` are never accepted. Funding alone has a historical-arrival exception described below. Caller-supplied metadata is not independently authenticated by this offline library.

| type | Required event-specific fields |
|---|---|
| `book` | `bids`, `asks`: best-first arrays of `[exact_price, positive_available_qty]`; either side may be empty |
| `aggTrade` | `price`, `qty`, `aggressor='BUY'|'SELL'` |
| `mark` | `price` (actual mark observation, not last trade) |
| `funding` | `settlement_ts`, `rate`, `mark`, `finalized=True`; optional `rate_type='Regular'|'Special'` |
| `funding_status` | `complete` (explicit bool), `valid_until_ts` (externally supplied exact deadline) |

Books must be unique-price, uncrossed, on-tick, positive, best-first snapshots. This library does not reconstruct depth deltas. The upstream adapter must reconstruct and verify a snapshot before supplying it.

### Parent-compatible aliases

`on_event()` also accepts the caller's normalized form:

```python
{
    'kind': 'book', 'symbol': symbol,
    'ts_ms': exact_source_ms, 'observed_ms': actual_observed_ms,
    'bids': bids, 'asks': asks, 'id': stable_id, 'source': source,
}
{
    'kind': 'funding', 'symbol': symbol,
    'ts_ms': exact_finalized_settlement_ms,
    'observed_ms': actual_observed_ms,  # optional for historical funding
    'rate': exact_finalized_rate, 'mark_price': exact_settlement_mark,
    'id': stable_id, 'source': source, 'finalized': True,
    'rate_type': exact_rate_type,
}
```

For these aliases, `ts_ms` is the actual source/settlement timestamp and optional `observed_ms` is the real observation timestamp. Market aliases use `ts_ms` for quote eligibility and `observed_ms` for the chronological event watermark. Funding can omit `observed_ms` and arrive historically without rewinding the watermark or inventing an observation timestamp. **The adapter must explicitly add `finalized=True`** after verifying that the rate is a finalized funding-history row; a source label alone is not approval. Preserve the full integer `fundingTime`, including milliseconds past a scheduled hour. Map actual upstream `rateType` to `rate_type`; absent type is treated as legacy `Regular`.

Funding readiness defaults to unknown/blocked, not ready. The external adapter must emit a truthful `funding_status` after reconciling finalized history and set its deadline no later than the next point where reconciliation is required. Opening is blocked if `complete=False`, no status exists, or decision/fill time is at/after `valid_until_ts`. Pending maker fills also recheck readiness. `funding` events do not automatically extend that deadline. Feed gaps do not cause a zero fee to be fabricated. Existing positions can still be reduced/closed.

## Matching and accounting rules

### Taker

Arrival is `decision_ts + latency_ms`. Execution requires the earliest supplied book with **underlying quote source timestamp strictly greater than arrival**. At-equal-timestamp books never fill. BUY consumes asks; SELL consumes bids. Recorded levels are consumed best-first, volume weighted by separate per-level fills. Multiple orders share one event's observed depth budget, so volume is not reused within that observation. Opening shortfall rejects before any fill unless explicit modeled-depth impact is supplied. Reducing shortfall may fill partially, with the remainder pending for subsequent feasible books. Cash changes only through fees, realized PnL and finalized funding, not gross futures entry notional.

### Maker

A maker is post-only and is rejected if its limit would cross the opposite best quote at arrival. `queue_ahead_qty` must be an explicit finite nonnegative observation; unknown queue is rejected, never silently treated as zero. Only a later **strict trade-through**, with the relevant aggressor direction and available aggregate-trade volume, can consume queue and fill:

- BUY maker: SELL aggressor with trade price strictly below limit.
- SELL maker: BUY aggressor with trade price strictly above limit.

At-touch trades, wrong-side aggressors, and books alone never fill makers. Price is the limit; partial fills each receive the externally supplied maker fee. The event-volume budget is shared across resting orders in persisted submission order, not lexical ID order. Explicit cancellation and expiry prevent future fills; expiry is exclusive (`ts >= expires_ts` cannot fill). This is an intentionally strict queue abstraction, not an exchange queue-position reconstruction. REST-only feeds without verified aggTrade/queue observations must not use it.

### Stops and gaps

Only mark events trigger stops: long `mark <= stop`, short `mark >= stop`. Trigger creates a reducing taker with its own explicit latency and cancels unfilled opening remainders for that symbol. Trades/books cannot trigger the stop. A gap executes at the first feasible post-latency book price (plus any explicit adverse fill impact), **not the stop price**. Missing quotes leave the position open; missing reducing depth can leave a partially closed position. No fabricated closure is recorded.

### Funding

For finalized settlement `T`, reconstruct signed quantity from **all historical fills with `fill.ts < T`**, even when the position has already closed when the funding record arrives. Do not inspect only the current position. Exact timestamp boundaries are deliberately conservative and independent of equal-timestamp processing order:

- entry at `T` is excluded;
- exit at `T` is excluded from the historical fill sum, so pre-exit holdings pay/receive funding;
- an entry before `T` and exit after `T` are included.

Settled cash delta is `-signed_qty_at_T * exact_settlement_mark * exact_finalized_rate`. Positive rate: longs pay, shorts receive; negative rate reverses this. Historical late arrival appends a settlement entry without altering prior fills or resetting cash. Identity is `(symbol, exact settlement_ts, rate_type)` in addition to the supplied event ID. Regular and Special events at the same millisecond are distinct. Repeated equivalent settlements do not charge twice; conflicting rate/mark values reject instead of rewriting past outcomes.

Never substitute an indicative `premiumIndex.lastFundingRate`, extrapolated interval, current mark, or rounded scheduled funding timestamp for a finalized event.

### Equity and ledger

`cash = original_initial_cash + sum(ledger.amount)`; ledger amounts are fees, realized PnL and funding. `equity = cash + sum(signed_qty * (last_supplied_mark - weighted_entry))`. Equity is `None` when a position has no supplied mark, rather than an invented valuation. Ledger entries are logically append-only within the API, and fills keep `version_id`, `forward_start`, liquidity, fee, observed/adverse pricing, and simulated provenance. The inspection attributes are intended to be read, not mutated by caller code; they are not a security boundary against a caller editing Python memory or SQLite directly.

## Persistence / version boundaries

The caller supplies an isolated local database path with an existing parent directory. No default path exists. The engine refuses known observation/status/shared paths and databases with foreign tables. Only `sim_broker_state` is used. Snapshot updates are SQLite transactions; event-processing exceptions roll back in-memory effects as well. A POSIX `flock` on the state file prevents two cooperating workers from operating the same state. Use WSL/Linux local storage, do not replace/unlink the database while a worker holds it, and always call `close()`.

Restart loads the persisted cash, orders, fills, ledger, marks, funding settlements, seen events, daily baselines and order sequence. It never seeds a new balance over an existing state or replays fills/funding. Version ID, forward start, instrument settings and execution model are frozen in the namespace; changed metadata requires a fresh isolated namespace. Risk caps are not persisted as a configuration or self-approved by this engine; a contract must be externally supplied again on restart. Artifact hashing/freezing belongs to the parent integration.

## Limits / integration responsibilities

- No collector/status/cron/UI integration, market strategy, scheduler, hold-time enforcement, account API, fee discovery, funding-history fetcher, or risk authorization is included.
- Only linear quantity-times-price USDT PnL/funding semantics are modeled. No inverse contracts, hedge-mode dual positions, margin calls, liquidations, spread-credit margin, precision-specific exchange fee truncation, or guaranteed loss caps.
- Additional exchange-specific filters, source freshness deadlines, deduplication of snapshots delivered under *different* IDs, complete historical funding reconciliation, category verification, and actual observation provenance remain upstream responsibilities.
- Minimum quantity/category are enforced only when explicitly supplied. Partial fills retain exact observed decimal volume rather than rounding up to an order lot; any exchange-specific execution-granularity policy belongs in verified upstream data/model constraints.
- State retains event payloads and serializes a full snapshot per event. It is designed for an inspectable small offline simulation, not a validated high-frequency or large-history storage engine. No stress/crash-injection or long-duration performance claim is made.
- A missed midnight mark uses the prior last supplied valuation as the day-start baseline, conservatively counting the next observed price gap. It is not a reconstructed midnight market price.
- The artificial tests prove engineering behavior only; they are not forward market evidence and cannot promote a version.

## Primary implementation references read

- Python 3.14 `decimal`: https://docs.python.org/3.14/library/decimal.html — exact decimal construction, finite values, arithmetic context, precision and rounding.
- Python 3.14 `sqlite3`: https://docs.python.org/3.14/library/sqlite3.html — bound SQL parameters, transactions, connection lifecycle, and same-thread connection semantics.

The attempted Binance funding-history/depth documentation fetches returned empty content in this environment, and the search backend did not recover those pages. No unread API documentation is claimed as verified. Public event field mappings above are the explicit caller integration contract; the adapter must verify its current primary exchange specification independently. No market/private API was called during implementation.
