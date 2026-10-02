# Batch-time recurrence: staged engineering handoff

Status: **verifying, awaiting parent acceptance and deployment**. No live source, process, namespace, account, dashboard, OS clock or timezone changed.

## Root evidence

- `../../evidence/batch_time_diagnostic.json`: read-only, transaction-consistent live audit through ID19229 at 2026-10-02T05:47:25.182424+00:00. Since ENG008 persisted restart cutoff1790897859350, 770 errors were recorded. 766 have a source **still future at poll_error**. 4 have sources older than15000ms **at poll_error**, not a claimed exact validation age. `poll_error.at_ms` is an upper boundary on the historical validation, not a measured dispatch clock.
- `batch_time_actual_failure.json` retains the actual +61ms mark failure. `batch_time_live_audit_prefix.json` retains the entire verified original19229-row prefix with original canonical payloads and hashes. No receipt spacing is labeled HTTP elapsed/retry.
- `batch_time_clock_probe.json`: six actual `/fapi/v1/time` request intervals all bound server time above local receipt. Bounds range from [563.878,1200.493]ms to [963.211,1616.663]ms. Local wall and monotonic elapsed both11.615222 seconds. This proves measured exchange/local offset, not why the OS clock has that offset. Changing OS time/timezone is out of scope.
- Persisted snapshot observations in the diagnostic interval:2345, blocked770 (fraction0.3283582). This is explicitly a snapshot-observation rate, not inferred HTTP-attempt timing.

## Candidate behavior

Only `paper_runtime_v2.py` changes production behavior. Add `tests/test_batch_time.py`. Frozen config and all broker/detector/risk/strategy modules are byte-identical.

Before ETH delivery, and then before full-batch decision, an eligible source up to2000ms ahead may cause **actual local waiting** for at most2 monotonic seconds per gate. Sleep and clocks are injectable. Each wake rechecks every source, including the unchanged15000ms maximum age. Stalled/backward/persistent clocks, over-budget scheduler wake, too-large future, stale or invalid timestamps fail closed. No future source is ever emitted to broker. `emit` independently revalidates at actual dispatch. ETH risk/exits still precede XAU observation, context and candles. Existing2000ms order latency and strict source-after-arrival checks are unchanged.

This is an availability/dispatch-delay change to the REST engineering model. It does not fix OS clock synchronization and is not a measured-server-clock reinterpretation. It cannot guarantee no future failures if offset exceeds the2s waiting budget. Waiting may make another source stale or a signal late, which must remain rejected.

Public receipts retain their original `received_at`, source timestamp and payload. Audit gains true request-call wall/monotonic bounds (including client throttle/retries), exact local source-validation values, local waiting duration, and broker-call boundaries. Additional audit rows increase storage. Public probe separately measures actual opener/read HTTP elapsed and actual `on_event` entry monotonic time. Funding timestamps/rates/ledger are not rewritten.

## Verification

- Three actual feature RED→GREEN loops in `evidence/batch_time_red{1,2,3_final}.txt` and green logs. Initial red3 error retained separately, corrected to an assertion-failure RED before implementation.
- Original142 frozen ENG008 IDs plus10 new IDs = **152 unique acceptance IDs**, all actually `ok`, no omissions/duplicates/skips. Original3 concurrently developing health-watchdog tests are explicitly outside scope and not claimed tested by this patch.
- One large originalbatch3 timed out at290s, retained and **not accepted**. Re-run as3a–3d. Final12 complete accepted batches all below300 seconds, maximum178.284 seconds. `batch_time_validation.json` checks each actual log ID against inventory.
- New regressions include captured +61ms, real local-clock wait, persistent future/stalled wall, >2s skew, stale-on-wake,15000/15001 boundaries, oversleep, actual timing provenance, source exactly at order arrival, no duplicate entry/exit, restart accounting, future mark with stop/gap/max-hold and XAU failure. All old risk/ledger/config gates remain tested.
- Actual public candidate probe:6 polls plus2 after one restart, all8 successful,16 actual broker entries,4 actual local waits (max434.940ms), raw receipt future lead up to899ms,105 verified audit records. Context61, cash100, fills/signals0, candidate_not_deployed=true. Only a short isolated series, not continuous live proof.

## Exact delivery

`runtime.patch`, `tests.patch`, `exact.patch` were generated from the still-live ENG008 base. `git apply --check staging/batch_time/exact.patch` succeeded without applying live. `batch_time_validation.json` has artifact SHA256s.

- Base runtime: `9efba1777f4cc2b7ed1c295c58cad9cf8a885fb10608cc9f22c22c1ec1e66288`
- Candidate runtime: `6791abcfd1b9911d12160ce92992ac03f2d66732402d17fbb51e798560608c6c`
- Combined patch: `f609ac57b2c32c2a9590d9a875578de24ac65c635840502ba82df74ae0b62dfa`
- Config: `dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96`

Commands from lab:

```sh
python evidence/batch_time_run_batch.py 1
python evidence/batch_time_run_batch.py 2
python evidence/batch_time_run_batch.py 3a
python evidence/batch_time_run_batch.py 3b
python evidence/batch_time_run_batch.py 3c
python evidence/batch_time_run_batch.py 3d
# Then separately: 4,5,6,7,8,9 (keep each foreground batch <=300s).
python staging/batch_time/paper_runtime_v2.py --once --state-dir staging/batch_time/parent-public-state --status staging/batch_time/parent-public-status.json
```

Parent must use separate acceptance log paths, refresh live state immediately before/after stopping the exact verified runtime PID, then apply only the accepted exact diff while stopped. Preserve `data/paper-v2`, version H1-PAPER-002, deployment, forward_start, signal IDs, risk baselines, orders, fills, **entire ledger**, and all historical audit. Do not start a new100 account or strategy window.

`batch_time_live_readback.json` confirms PID2621427 in lab with expected `-u` command. At06:27:33.609 UTC, live cash100.0000000000000000, fills0, flat,4 signals. Contrary to an earlier zero-record premise, the live account already has a REJECTED risk_per_trade order and one zero-amount finalized funding ledger row. Preserve both. Re-read at actual deployment because this is an observation, not a deployment lock or permission to reset.

Live restart/deploy, parent independent tests, exact live hashes, full continuity and dashboard read-back are **not performed by this child**. Work remains verifying, never completed here.
