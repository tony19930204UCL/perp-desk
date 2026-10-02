# H1-PAPER-001 durable public PAPER runtime

`paper_runtime.py` integrates the actual Detector, sizing, public normalizers and SimBroker contracts. This is paper-only REST polling. No credentials, private endpoints or exchange orders exist in its CLI.

## Verification (run from this lab)

```bash
python -m unittest discover -s tests -v
python paper_runtime.py --once
python brief.py --status shared/paper_status.json
```

The final delivered suite ran 113 tests successfully. Evidence: `evidence/runtime_full_tests_final.txt`. Vertical RED/GREEN evidence is `evidence/runtime_red1.txt` through `runtime_red17.txt` and the matching `runtime_green*.txt`. An initial full-suite failure remains in `evidence/runtime_full_tests.txt`: the older fixture jumped over the heartbeat gap and incorrectly expected its missed candle to count. The fixture now maintains explicit forward heartbeats. The fail-closed gap behavior was not loosened.

Real public snapshots/stdout: `evidence/runtime_public_once_final.json`, with stderr in `evidence/runtime_public_once_final_stderr.txt`. Initial real probe failure and successful correction remain in `runtime_public_once_stdout.json` and `runtime_public_once_stdout2.json`. An actual ETH fundingInfo row has adjusted caps but an unchanged eight-hour interval. Actual finalized funding timestamps include millisecond offsets. The implementation validates regular schedule coverage but retains each exact settlement timestamp and rate type for accounting.

## State and safety

- Default state directory: `data/paper`. Authoritative files: `runtime.sqlite3` (state, hash-linked full public receipts and diagnostics), `signals.sqlite3` (durable detector registration/cursor/intents), `broker.sqlite3` (orders, fills, cash, ledger, funding and event dedupe).
- A nonblocking `runtime.lock` prevents concurrent runtimes in the same namespace. The broker also takes its own lock. Use one accepted namespace, never alternate namespaces to reset loss counters.
- `paper_config.json` is unchanged and byte-hash pinned to `ee5aad2ee390985ad5046587af18782cfe50c7575388255e954b40e43f5bb026`. Unknown or altered models are rejected, including in a new namespace. Snapshot model proof includes hashes of all six execution modules.
- Snapshot export is atomic, validated by the existing dashboard schema. CLI also fsyncs the export and directory. Default separate target: `shared/paper_status.json`. The CLI refuses any `status.json` target. Existing collector and dashboard processes were not changed or stopped.
- Fresh forward start is persisted before market collection. Candles opened before it cannot warm the detector. Only actual closed 5m API candles are ingested. API millisecond close timestamps are converted from inclusive end to the detector's exclusive end. No historical warmup, interpolation, or synthetic confirmations.
- Gaps over 60 seconds reset forward warmup and advance the resume cursor past offline candles. They cancel pending entries before recovery books, including after restart. Open positions request a delayed data-gap exit when valid observations return, never a fabricated offline fill.
- Each entire ETH/XAU observation batch is validated before broker events. Quote/source ages are rechecked after fetching candles, before new decisions. Missing, stale, inconsistent or malformed feeds are visible blockers. TradFi remains observation-only.
- Signal submission is idempotent. A crash between broker commit and runtime routing commit is reconciled from the persisted detector intent before recovery books can fill or trigger exits.
- Entries and all exits require a later actual book source timestamp strictly beyond the frozen 2000ms arrival time. Both sides use observed depth and adverse 2-tick fill costs. No beyond-observed-depth liquidity is invented. Reduce orders may fill only observed partial depth and remain pending for later books.
- Actual mark events trigger stop/target exits. Max hold is 30 minutes measured from the first real paper fill. Stop, target, duration and data-gap triggers are not execution prices.
- Finalized funding is refreshed at 30 seconds, validated against the supported eight-hour regular schedule, cursor coverage and the observed nextFundingTime. Missing/truncated/changed-interval coverage blocks the batch. Predicted lastFundingRate is never a ledger charge. Regular settlement scheduling allows at most 5000ms publication offset but accounting preserves exact returned timestamps. Unsupported schedules fail closed rather than inventing funding coverage.
- Risk gating includes per-trade costs, exposure, one position, broker arrival checks, UTC daily loss and persistent total loss. Daily/total circuit breakers latch when actual marked equity breaches the budget. Reductions remain allowed. Budgets cannot guarantee realized maximum loss across a jump/offline interval.
- Cash is reconciled by replaying the actual broker cost ledger under its 40-digit Decimal execution convention. Corruption produces an error rather than fictitious PNL. Snapshot financial values are decimal strings. Fees/funding are not subtracted twice. Failed-feed equity points explicitly carry `valuation_stale=true`.
- All test clients, trades and state copies are labelled ARTIFICIAL FIXTURES and isolated under the active profile scratch directory. They never enter the public state or PAPER metrics.

## Parent acceptance and launch

No persistent runtime was launched by this integration task. The existing observation collector/dashboard remain untouched. The public `--once` evidence only demonstrates functioning data integration and a zero-fill warmup, not strategy performance or continuous activity.

After independently rerunning tests, public probe, snapshot/schema and audit validation, the parent may replace the observer process with:

```bash
python -u paper_runtime.py --state-dir data/paper --status shared/paper_status.json
```

This resumes the persisted public probe namespace and preserves start, gaps, balances and counters. If the parent requires a fresh acceptance start before any confirmations, choose a new empty namespace once, document that choice, and keep it thereafter (for example `--state-dir data/paper-accepted`). Do not delete/reset existing evidence.

For the dashboard, stop only its verified PID after acceptance, then relaunch on its existing port with the separate export:

```bash
python -u dashboard.py --port 8767 --status shared/paper_status.json
python brief.py --status shared/paper_status.json
```

Do not rename/copy paper output over `shared/status.json` or kill processes by a broad pattern. Verify the new runtime PID, listener, HTTP freshness and source ages after parent launch. Updating a scheduled brief's `--status` argument is separate parent scope and must be read back.

## Limits

The engine remains a REST approximation and may miss mark swings between polls. The strategy is frozen and unproven. The real probe has no trades, no profitability claim and no sample-size judgment. No background activity between chat turns is promised. SQLite storage/retention remains inherited engineering debt: the broker stores growing dedupe/history payloads and runtime full receipts are append-only. Monitor disk and performance before long unattended runs, never prune forward evidence to hide a loss.
