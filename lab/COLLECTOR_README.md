# Public Binance shadow observation collector

Parent update: ENGINE ENG-002 is running as observation-only. Full integration verification is in evidence/parent_full_tests.txt and evidence/parent_validation.json. Dated child measurements below remain history, not current price guarantees.

This is **real public-market observation**, not a completed paper-trading simulator. It never authenticates, reads credentials, sends signed requests, submits orders, changes risk settings, or enables live trading. No third-party packages are required (Python standard library; Linux/WSL `fcntl` file locking).

## Run and verify

```sh
cd /home/chihcheng/.hermes/profiles/perp-desk/lab
python -m unittest discover -s tests -p 'test_[cb]*.py' -v
python /home/chihcheng/.hermes/profiles/perp-desk/lab/perp_collector.py --once
python /home/chihcheng/.hermes/profiles/perp-desk/lab/brief.py --status /home/chihcheng/.hermes/profiles/perp-desk/lab/shared/status.json
```

Collector exits: `0` = one complete fresh observation; `1` = observation failed or stale (status still exported); `2` = another collector owns the lock. The brief prints **only the message**, up to eight Traditional Chinese lines. A missing/malformed snapshot produces an explicit unavailable-data message rather than inferred balances. Printing a report does not schedule or send Telegram messages.

Continuous foreground command for the parent to launch, **not launched during this task**:

```sh
python /home/chihcheng/.hermes/profiles/perp-desk/lab/perp_collector.py --interval 15
```

Intervals are at least 15 seconds after each iteration. Failed iterations back off exponentially, capped at 300 seconds; successful recovery restores the requested interval. Ctrl-C stops the foreground loop. Request timeout is 10 seconds, with up to three attempts, 0.25-second inter-request pacing, 2-/4-second retry delays, and bounded HTTP 429 `Retry-After` handling. HTTP 418 is not retried inside a request. These are **transport/heartbeat settings**, not trading-risk limits.

## What is actually collected

Only allowlisted GET paths at `https://fapi.binance.com`:

- `/fapi/v1/exchangeInfo` is cached from persisted raw receipts for less than 24 hours, refreshed on expiry. Metadata must identify ETHUSDT as `PERPETUAL`/`COIN` and XAUUSDT as `TRADIFI_PERPETUAL`/`COMMODITY`, both USDT quote/margin and `TRADING`. Tick size, lot step and minimum notional are parsed from the response, not hard-coded.
- `/fapi/v1/ticker/bookTicker`: best bid/ask and source timestamp.
- `/fapi/v1/premiumIndex`: mark price, funding rate and next funding timestamp.
- `/fapi/v1/depth?limit=5`: raw depth-five snapshot, including exchange event/matching timestamps.
- `/fapi/v1/klines?interval=5m`: forward-only window beginning no earlier than the **persisted first collector start**; `endTime` is the current receipt-side clock minus one millisecond. Only fully closed bars are admitted into `closed_bars`, with a symbol/open-time uniqueness constraint and persistent cursor. No pre-start warmup history is requested. Raw REST responses can include an open bar; the separate closed-bar table never does.

Transport is explicitly **REST polling, not WebSocket**. Each successful HTTP response records its source timestamp where provided and its UTC receipt timestamp; source timestamps that are unavailable are `null`, never fabricated. All decimal market/filter strings retain precision. Invalid, nonfinite, crossed or nonpositive prices fail closed.

Source ages for book/mark/depth must be at most 120 seconds, with at most five seconds of future-clock tolerance. A request succeeding with stale source data is not a successful feed heartbeat. Every iteration atomically replaces `shared/status.json`, including failures. Failed iterations keep the previous genuine `last_success_at`, add audit/error/gap information, and mark the feed disconnected. A heartbeat gap after process downtime is recorded on restart. `brief.py` recomputes heartbeat, receipt and source freshness **at read time**, so a stopped collector cannot remain falsely fresh in the report.

## Persistent artifacts

- `data/observations.sqlite3`: authoritative state, raw observations, deduplicated closed forward bars and chained audit events.
- `shared/status.json`: atomic snapshot for read-only consumers; same-directory replace with file/directory fsync.
- `collector.lock`: OS-managed exclusive nonblocking lock; file existence alone does not mean a collector is running. Do not unlink the lock file to bypass another instance.
- `evidence/tdd_collector.txt`: actual RED/GREEN commands, failures, passing runs, two live public-network passes and persistence checks.

Canonical `100` USDT PAPER starting equity is created once in SQLite. Restart reads existing state, preserving the original start, counters, versions and balances instead of resetting them. Observation code never updates balances through invented fills or PNL.

The exported schema includes `schema_version=1`, `mode=shadow`, `paper_start_equity_usdt=initial_equity_usdt=cash_usdt=equity_usdt='100'`, PNL strings `'0'`, `positions=[]`, `fills_count=0`, both trading enablement flags false, and blockers:

```text
risk_limits_not_set
sim_broker_not_implemented
signals_not_implemented
```

Signals and blocked signals are zero **because no diagnostic/strategy signal engine is implemented**, not because a strategy passed testing. H1, simulated broker execution, performance confirmation and retrospective trades are deferred. `OBS-001` is the observation identity. The old `OBS-002` is a preserved historical poll batch, not a strategy. Later polls never mint new versions. Actual wallet equity is unknown.

## Verification actually performed

Final owned test command above returned:

```text
Ran 17 tests in 2.388s

OK
exit_code=0
```

The final public `--once` completed at `2026-10-01T13:50:50.905909Z`, retaining the first start `2026-10-01T13:33:18.095077Z`. Both markets were `TRADING`:

| Symbol | Category | Bid | Ask | Mark | Tick | Step | Min notional |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ETHUSDT | crypto | 2692.00 | 2692.01 | 2692.52635659 | 0.01 | 0.001 | 20 |
| XAUUSDT | TradFi | 4166.93 | 4166.94 | 4166.93000000 | 0.01 | 0.001 | 5 |

These are **dated observed values**, not current price guarantees. The database readback verified 18 raw receipts, six closed forward bars (three per symbol), `OBS-001` and `OBS-002`, an eight-line brief, and zero fabricated trades/positions/PNL. One missed-heartbeat gap between the two deliberate `--once` runs was recorded; network errors remained zero.

The first nine real receipts were captured before receipt-digest chaining was implemented during TDD and remain honestly unchained. The later nine receipts have verified payload digests; all 11 current audit events form a verified SHA-256 chain. No historical events were fabricated to retrofit the earlier receipts. This local chain is an integrity diagnostic, **not external attestation or protection against someone rewriting the entire database**.

Tests are labeled hand-made fixtures, run exclusively in temporary directories under the active profile's `cache/scratch`, and never populate runtime `lab/data`. The very first discovery run also encountered a parallel dashboard engineer's then-missing server; all subsequent cycles scoped discovery to owned collector/brief tests. Dashboard files and risk configuration were not edited.

## Remaining limits

- No broker, signal engine, risk approval or trading was implemented/enabled. The parent subsequently added the local read-only UI, continuous observation processes and a daily Telegram schedule; this remains observation-only.
- REST snapshots are not a continuous event stream; outages and market closures can make observations stale. Forward bar catch-up is bounded to 1,000 requested rows per iteration; no pre-start history is fetched.
- Reference is now cached for 24 hours. A 256 MiB engineering database budget pauses further raw collection with an explicit blocker. Complete retention/rotation is still not implemented; no forward evidence is silently deleted.
- SQLite corruption or inability to write/fsync is a storage failure, not a reason to reset canonical state or invent a healthy snapshot. An atomic export cannot succeed if its underlying storage is unavailable.
