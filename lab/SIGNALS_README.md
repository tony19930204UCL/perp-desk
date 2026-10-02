# Deterministic H1 research detector

Engineering-only library. It creates **long research intents**, not orders, positions, fills, or market-performance evidence. It contains no quantities, leverage, stops, risk defaults, credentials, network calls, or trading activation. It does not change S1–S4 v0, the collector, dashboard, brief, observations store, risk configuration, or `versions.md`.

A downstream broker must require its externally approved risk contract, including a mandatory stop, and **reject opening without that contract**. This module cannot authorize an opening. Broker/risk-contract enforcement and live activation are not implemented here.

## Integration API

```python
from datetime import datetime, timezone
from decimal import Decimal
from signals import Detector

# These are caller-supplied values, not defaults chosen by the library.
engine = Detector(
    store_path=dedicated_signal_sqlite_path,
    version_id=registered_research_version_id,
    forward_start=registered_forward_start_utc,  # aware datetime, UTC offset zero
    symbols=explicit_symbol_categories,         # e.g. {symbol: 'crypto'}
)
result = engine.ingest(
    symbol, category,                           # exactly 'crypto' or 'TradFi'
    now_ms=observed_current_utc_epoch_ms,        # integer; bool is invalid
    session_open=session_context_or_none,       # context dict below; NOT bool
    open_time_ms=bar_open_utc_epoch_ms,
    close_time_ms=bar_close_exclusive_utc_epoch_ms,
    closed=True,
    open=Decimal(open_text), high=Decimal(high_text),
    low=Decimal(low_text), close=Decimal(close_text),
    volume=Decimal(volume_text),
)
# {'diagnostic': <string>, 'signal': <intent dict or None>}
# gap_reset also includes {'gap': {'expected_open_ms': <string>,
#                                'actual_open_ms': <string>}}
```

The lower-level API is `engine.process(bar_dict, now=aware_utc_datetime, session=context_or_none)`. It returns the same diagnostics but uses the key `intent` instead of `signal`. Its exact bar keys are `symbol`, `open_time_ms`, `close_time_ms`, `closed`, `open`, `high`, `low`, `close`, `volume`; extra fields are rejected, not coerced into persistence. `ingest` supplies `symbol`; pass the remaining eight keys as keyword fields.

### TradFi context (fail closed)

```python
session_context = {
    'is_open': True,
    'symbol': symbol,
    'valid_from_ms': calendar_interval_start_utc_epoch_ms,
    'valid_until_ms': calendar_interval_end_utc_epoch_ms,
    'source': explicit_calendar_or_session_authority_reference,
}
```

Both endpoints must be integers and the interval must cover the **whole evaluated bar**. Missing, closed, incomplete, wrong-symbol, or non-covering contexts reject with `reject_session`, without advancing state. A bare `session_open=True` is insufficient. Crypto does not require session context. No trusted trading-calendar integration is provided or claimed: validity here means the explicit caller context passes structural, identity, and interval checks, not that the detector has independently verified the calendar. Production callers must supply independently validated session authority; unknown sessions must remain blocked.

## Exact H1 rule

Evaluate only a closed five-minute bar, after **61 prior closes** have generated 60 prior simple-return samples:

- Prior return `r[i] = close[i+1] / close[i] - 1`, for 60 prior samples only.
- Mean `sum(prior_returns) / 60`.
- **Sample** standard deviation `sqrt(sum((r - mean)**2) / 59)`.
- Volume median is the average of sorted indices 29 and 30 of the **60 immediately prior bar volumes**. The extra oldest close provides the first return denominator, not an extra volume sample.
- Current return `current_close / prior_close - 1`.
- Trigger only if `current_return < mean - 2 * sample_stdev` **and** `current_volume > 2 * median_volume`.
- Both comparisons are strict. Equality on either threshold does not trigger.
- `side='long'`; both `entry_reference` and `reversion_target` are the immediately prior close. These are references, not prices at which a fill is promised.

All financial arithmetic uses an isolated Decimal context: precision 50, `ROUND_HALF_EVEN`. No binary float conversions or display rounding are used in decisions. Decimal division and square root are deterministic finite-precision operations, not claims of infinite-precision arithmetic. Unrepresentable arithmetic returns `reject_arithmetic` without consuming the candidate bar.

### Bar/time contract

- Epoch millisecond timestamps are UTC integers. Open time must be nonnegative and aligned to a 300000-ms boundary.
- `close_time_ms` is the **exclusive end**, exactly `open_time_ms + 300000`. Feed adapters must explicitly translate any different source convention before ingestion; this library does not silently repair timestamps.
- `closed` must be the boolean `True`; future exclusive closes reject even if a feed marks them closed. `now` must be timezone-aware UTC.
- OHLC and volume must be finite `Decimal` objects (not float or text). Prices must be positive, volume nonnegative, and low/high must enclose open and close.
- Symbols and categories are explicit registered inputs; no inference from symbol strings.
- A forward gap resets warmup, records the actual incoming bar as the first new close, emits `gap_reset`, and never interpolates. Missing one five-minute bar therefore prevents a signal until the fresh 61-close warmup is complete.
- An exact repeat of the latest accepted bar is `duplicate`, with no intent. Older bars and changed bars at the current cursor are `reject_out_of_order`; they never revise prior signals or rewind the cursor. Replays of older bars are therefore idempotent rejections, not repeated signals.
- Rejections do not advance persisted state. Rejected data can produce a later observable gap when the next valid bar arrives.

## Forward registration and persistence

The caller selects the research version ID and a genuine new forward start **before** collecting that version's forward sample. Reusing a version ID requires identical UTC start and symbol/category mapping; attempts to change them raise `ValueError`. Registration is inserted once, never rewritten. Caller mutation of the input mapping or returned registration cannot change it.

Each version/symbol has independent warmup and cursor state. No bar with an open time earlier than that version's `forward_start` can enter warmup or generate an intent. A new ID never inherits any older version's detector state. The caller remains responsible for using a genuinely new forward window; the detector cannot certify observation provenance from a bar dict or turn historical fixtures into forward market evidence.

Pass a **dedicated existing-parent-directory SQLite path**, not `data/observations.sqlite3`, `status.json`, or any broker store. Tables:

- `h1_versions(version_id, registration)` — immutable version registration JSON.
- `h1_state(version_id, symbol, bars)` — last at most 61 accepted closes/complete bars per symbol; overwritten only as the forward cursor advances.
- `h1_signals(signal_id, version_id, intent)` — append-only frozen intent JSON, including all reproducible features.

Stores containing foreign tables are rejected before adding H1 tables. Every ingestion reads current state inside `BEGIN IMMEDIATE`; cursor updates and new signal insertion commit atomically before an intent is returned. Restart or multiple detector instances sharing the same dedicated store therefore use the persisted cursor and seen signal IDs. SQLite/filesystem errors propagate rather than fabricating successful processing.

Signal IDs are SHA-256 of the UTF-8 compact JSON array `[version_id, symbol, bar_open_time_ms]`, with separators `(',', ':')`. The persisted intent is detached from the caller's returned dict: later bars or caller edits do not rewrite earlier stored snapshots. No broker acknowledgment/outcome table exists here. An integration should consume persisted intent IDs idempotently; a crash after detector commit but before broker receipt must not be handled by manufacturing a repeated detector signal.

## Frozen feature schema and reproduction

Each intent includes `signal_id`, `version_id`, `symbol`, `side`, `entry_reference`, `reversion_target`, and `features`. Feature names:

- `mean`, `sample_stdev`, `median_volume`, `current_return`, `reference_close`.
- `current_close`, `current_volume`, `return_threshold`, `volume_threshold`.
- `prior_closes` (61 strings) and `prior_volumes` (60 strings).
- `decimal_precision='50'`, `rounding='ROUND_HALF_EVEN'`.
- `bar_open_ms`, `bar_close_ms`, `cutoff_ms`, `prior_start_ms`, `prior_end_ms` — decimal integer strings, not floating timestamps.

All feature financial values and timestamp numbers are JSON strings. Recompute under the documented context:

```python
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext

f = signal['features']
with localcontext(Context(prec=int(f['decimal_precision']), rounding=ROUND_HALF_EVEN)):
    closes = list(map(Decimal, f['prior_closes']))
    returns = [b / a - 1 for a, b in zip(closes, closes[1:])]
    mean = sum(returns, Decimal(0)) / 60
    stdev = (sum((r - mean) ** 2 for r in returns) / 59).sqrt()
    volumes = sorted(map(Decimal, f['prior_volumes']))
    median = (volumes[29] + volumes[30]) / 2
    current = Decimal(f['current_close']) / closes[-1] - 1
    decision = current < mean - 2 * stdev and Decimal(f['current_volume']) > 2 * median
```

Only prior bars and the current closed bar are used. `cutoff_ms` equals the current bar's exclusive close. `prior_end_ms` equals the current open, so no prior sample extends past the cutoff. Later bars cannot revise stored features. The reproducibility test also exercises a nonzero-variance, nonconstant-volume fixture, not just the flat example below.

## Verification commands and fixture labeling

From this lab directory:

```sh
python -B -m unittest discover -s tests -p test_signals.py -v
python -B signals.py --self-check
```

The self-check uses a **handcrafted unit fixture only**: 61 prior closes of 100, prior volumes of 10, current close 90 and volume 21. Its mean and sample stdev are zero, current return is -0.1, volume median 10, thresholds 0 and 20, and reference/target 100. These are fabricated test inputs openly labeled as such, not exchange data, fills, backtest performance, or evidence of strategy profitability.

The CLI prints three JSON lines: a `kind='diagnostic'` research intent with its frozen snapshot, a `kind='diagnostic'` duplicate with no signal, and a `kind='self_check'` summary with `passed=true` and `fills=0`. It has no collection, live, broker, daemon, or activation mode.

All detector tests and CLI fixtures use temporary directories under `/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch`, removed afterward. They never write fixture data into `lab/data` or `lab/shared`.

`evidence/tdd_signals.txt` records genuine tool-returned RED/GREEN test output per vertical behavior slice, including intermediate failures, corrected test fixtures, and an intermediate SQLite ResourceWarning resolved with explicit connection closing. Final passing output and direct CLI output are appended there. This is engineering verification only; no formal market confirmation window or performance claim is made.
