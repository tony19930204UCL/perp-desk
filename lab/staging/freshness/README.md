# PAPER v2 freshness engineering handoff (STAGED, NOT DEPLOYED)

## Outcome

The staged `paper_runtime_v2.py` fixes quote reuse after slow candle work without changing H1-PAPER-002 signals, risk, account state, or the 15000 ms maximum age. Live runtime PID 2558035 and dashboard PID 2558307 were not stopped or changed. The deployed source remains SHA-256 `f2132b03726940a00f2060140942dcaacfdc045ccdd809244335ad4b90003ae0`.

Only the runtime patch should be considered for deployment. The other Python files here are unchanged dependency copies for isolated testing, not replacements to deploy. Fixtures and real independent probes use profile scratch namespaces. No staged snapshot was published to the dashboard.

## Root cause and evidence

Read-only deployed audit ID 1324 recorded `ValueError: decision source age exceeded after kline request` at **2026-10-01 22:04:50.543 UTC**. Raw original rows (including their hashes) are retained in `../../evidence/freshness_actual_audit_raw.json`. Parsed timings are in `freshness_actual_timing.json`.

Ranked hypotheses and discriminating observations:

1. Sequential ETH → XAU → Klines work ages an initially valid input. Confirmed: ETH ticker source `1790892275345`, receipt `1790892275401` (56 ms old at receipt), and failure `1790892290543` (15198 ms old). The exact oldest ticker, not depth or mark, exceeded 15000 ms.
2. The ETH response was already stale at receipt. Rejected for this failure: ETH depth age 60 ms and mark age 194 ms at receipt, as well as ticker age 56 ms.
3. The previous negative source/receipt timestamp problem caused this failure. Rejected as the explanation for ID1324: all ETH timestamps were in the past at actual dispatch and at failure. XAU depth was 31 ms ahead of receipt but in the past at the later batch validation. The existing strict dispatch safeguards remain unchanged.
4. Missing or malformed closed-bar data caused the poll error. Not supported: audit1323 contains the current still-open 22:00–22:05 candle. No new bar needed processing at 22:04:50. Re-fetching that same candle every cycle was unnecessary.

The last XAU receipt at 22:04:39.741821 UTC and the Kline receipt at 22:04:50.527198 UTC are **10786 ms apart** after millisecond conversion. This is receipt-to-receipt separation, NOT proven HTTP duration. Original request-start/attempt timings were not persisted, so timeout, retry, or server latency cannot be distinguished from that original audit. The deterministic regression replays the measured relative offsets and reproduces the exact original error before the repair.

## Narrow changes

1. Deliver the validated actual ETH book and mark before requesting XAU observation, so unrelated XAU failure cannot prevent a gap exit trigger or its later real-book fill. XAU stays TradFi observation, is still separately validated, and failures continue blocking new entries.
2. Use the existing durable next-bar-open cursor to fetch forward Klines only when `cursor + 300000 <= real_clock`. Missing due bars are retried because the cursor moves only after actual processing. Bootstrap and gap context validation are unchanged.
3. After slow candle/bootstrap work, if any source is no longer decision-admissible, refresh the actual public batch once and validate again before ENTRY routing/sizing. This is bounded, not an unbounded retry loop. Old receipts are never retimestamped. Stale or future new inputs still fail closed.

`emit`, broker arrival/source rules, 2000 ms execution latency, stop prices, sizing, funding, signal rules, and config bytes are unchanged. Late closed-bar signals still get `late_closed_bar_signal`; refreshing a quote does not authorize a historical signal. The existing H1-PAPER-002 forward window and deployed account must be preserved by the parent.

## TDD and regression results

Three vertical RED→GREEN slices are saved under `../../evidence/freshness_{red,green}*`:

- Measured slow pipeline reproduces the exact age error, then recovers through genuinely new inputs.
- XAU failure initially prevents an ETH gap exit, then ETH exits before XAU using a later actual source book.
- Same unclosed candle initially gets fetched repeatedly, then is skipped across a restart without changing the account/cursor/cutoff.

Five added engineering tests also cover old-source vs new-receipt arrival, 2000 ms latency, restart cash/fill/signal continuity, no duplicates, and rejection of a signal made late by slow candles. The staged v2 alias `test_quote_age_rechecked_after_slow_kline_request` was explicitly updated to test stale *refreshed* inputs rather than require rejection even when actual new inputs are available. Original v1 tests are unchanged. No tests were silently deleted.

Frozen inventory: all **137 original test IDs plus 5 new IDs = 142**, all passed once in eight stable batches. No missing or duplicate IDs. Longest batch 190.703 s. `freshness_validation.json`, `freshness_inventory.json`, and `freshness_batch{1..8}.{json,txt}` preserve exact IDs and outcomes.

One intermediate harness run (`freshness_green2.txt`) failed because original tests used their original `LAB` path while the staged runtime used `__file__` relative namespace checks. The constructor rejected the fixture/live conflict before saving account state. The final harness copies tests/assets/dependencies into the stage so reserved-path tests never target deployed namespaces. That failed run is preserved, not counted as passing.

## Real public probes (not strategy performance)

All raw actual public receipts and audit hashes are preserved in `freshness_public*_audit.json`, so evidence survives scratch expiry.

- Ordinary isolated probe and restart: no errors, context61, cash100, zero signals/fills, 16 actual receipts. No candle query on the second no-new-close cycle.
- First attempted delayed probe: blocked before reaching candles because real ETH mark source was still 110 ms and then 29 ms in the future relative to local receipt. The actual validation occurred before source became admissible. Both errors and zero fills are preserved in `freshness_public_slow_probe.json`. No safeguard was relaxed.
- Second independent delayed probe and restart: no errors, context61, cash100, zero signals/fills, 22 actual receipts. It intentionally waited **16 seconds locally BEFORE making the real bootstrap GET**. This is labelled engineering delay, not claimed API/network latency. Actual API elapsed was **655.161 ms**, total monotonic wait+GET **16655.293 ms**, receipt **23:17:22.952376 UTC**. The refreshed ETH ticker/depth/mark sources were `1790896643703 / 1790896644455 / 1790896645000`, all newer than that Kline receipt, with real receipts at **23:17:23.705369 / 24.369765 / 25.012318 UTC**. Their actual GET elapsed times were **664.060 / 644.872 / 630.335 ms**. The second poll after restart did not fetch Klines again.

The future-source rejection remains an operational limitation, now more visible because ETH is delivered sooner instead of incidentally waiting for XAU. The patch intentionally does not manufacture delay timestamps or admit future data. Parent should evaluate this limitation during acceptance. Ordinary/public slow success does not establish continuous reliability or trading advantage.

## Exact artifacts and hashes

- Candidate runtime: `9efba1777f4cc2b7ed1c295c58cad9cf8a885fb10608cc9f22c22c1ec1e66288`
- `../../evidence/freshness_runtime.patch`: `2b7035ee8996c7de10e81a78891dedd294a8092209add9a905d86c0ec9fb864e`
- `../../evidence/freshness_tests.patch`: `6737977466b70fcad7d9c5a6eef729fd7583adcd33fee17085ec459b2bbeb245`
- Frozen config: `dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96`

## Reproduction commands

Run from `/home/chihcheng/.hermes/profiles/perp-desk/lab/staging/freshness`:

```bash
PYTHONPATH=tests:. python -m unittest test_freshness -v
python run_inventory.py --inventory
# Each batch is a separate foreground call, timeout <=300s.
python run_inventory.py --batch 1
python run_inventory.py --batch 2
python run_inventory.py --batch 3
python run_inventory.py --batch 4
python run_inventory.py --batch 5
python run_inventory.py --batch 6
python run_inventory.py --batch 7
python run_inventory.py --batch 8
python public_probe.py --tag parent
python public_probe.py --candle-wait-seconds 16 --tag parent
python verify_handoff.py
```

`verify_handoff.py` checks the fixed saved probe names from this handoff and verifies source hashes, config, account continuity within the probes, raw receipt hashes, full audit chains, test inventory, and exact deployment diff. It does not verify an account migration or perform deployment. Probe scripts' exit0 means the evidence was recorded; inspect their actual `latest_error` fields (a blocked poll is not public success).

Parent acceptance is still required: independently inspect/apply the exact runtime and test patches, rerun tests/probe, and verify live account continuity before/after the parent's controlled restart. Do not replace `data/paper-v2`, reset cash/cutoff, copy scratch databases, or publish fixture metrics. Work task remains `verifying`, not completed.
