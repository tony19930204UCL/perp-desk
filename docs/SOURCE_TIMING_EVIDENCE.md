# PAPER v3 source-timing evidence contract

Status: engineering observability candidate. Not a strategy-performance dataset and not an OS-clock diagnosis.

## What is measured

Timing evidence separates four different things that must not be conflated:

1. **Logical fetch elapsed**: wall and monotonic time from runtime fetch entry to return/failure.
2. **Real HTTP attempts**: each actual public `urlopen/read` attempt gets its own wall/monotonic start, end, elapsed, HTTP status/error type and response byte count.
3. **Local waits**: the existing 250 ms pre-attempt throttle and retry/backoff are recorded separately with requested and actual elapsed time.
4. **Batch/peer aging**: market sources are aged again at batch/decision time. If one peer ages beyond the unchanged freshness gate before the already-deployed bounded refresh, that pre-refresh age is retained separately.

A logical fetch duration is therefore never labeled HTTP latency unless it is the individual `http_attempt` interval.

## Clock domains

Every timing interval records both local wall-clock and monotonic elapsed time plus:

`wall_elapsed_ms - monotonic_elapsed_ms`

That difference is evidence of the two local clock domains disagreeing over the measured interval. It does **not** establish why they differ.

The original OS-clock anomaly root cause remains unknown. This change does not claim NTP, timezone, scheduler, VM/WSL, kernel, exchange clock, or any other cause without independent evidence.

## Existing execution gates are unchanged

This work does not change:

- source freshness gate: **15,000 ms**
- execution latency gate: **2,000 ms**
- future-source rejection
- source-after-arrival requirement
- actual broker-delivery freshness validation
- bounded future-source local wait: at most **2 monotonic seconds**
- already-deployed one-shot stale-peer refresh
- fail-closed behavior

No source timestamp or receipt timestamp is rewritten.

## Retry behavior is unchanged

The public client still performs at most three attempts.

For each attempt it records:

- the 250 ms pre-attempt throttle as a separate `wait`
- one `http_attempt`
- on retryable failure, the existing bounded retry/backoff as a separate `wait`

The evidence layer does not add attempts, increase timeout, create an infinite retry loop, or fabricate an API outcome.

## Bounded storage

Runtime timing evidence is a rolling list capped at **32 records**.

A logical fetch stores at most **12 internal timing events**.

The rolling list lives in the existing durable runtime state and is rewritten with that bounded state. This change does not create a new unbounded audit table or append-only timing log.

Existing audit events remain unchanged for integrity/history; the new detailed transport evidence is not duplicated into an unlimited audit stream.

After restart the retained 32-record tail remains available. Older timing records fall out of the rolling window without altering fills, ledger, broker audit, runtime audit, account baselines or research history.

## Evidence classifications

Runtime/public observations use:

- `engineering_observation_not_strategy_performance`

Injected or gate-focused timing uses:

- `synthetic_or_runtime_gate_evidence_not_strategy_performance`

The public probe uses:

- `PUBLIC_HTTP_ENGINEERING_OBSERVATION_NOT_STRATEGY_PERFORMANCE`

None of these observations are trade samples, edge evidence, PNL evidence or strategy performance.

## Public HTTP observation

CI performs exactly one bounded logical public observation:

```sh
python3 scripts/public_source_timing_probe.py
```

It calls only Binance USD-M public `/fapi/v1/time` through the same bounded public transport client.

The probe:

- uses no credentials
- has no account/private endpoint
- emits no local absolute path
- records actual attempts/waits/outcomes
- always labels its result engineering-only
- does not claim the GitHub runner network is representative of the PAPER host
- does not claim wall/monotonic discrepancy explains OS clock behavior

A public failure/HTTP error is retained as the observed outcome rather than converted to a fake success.

## Independent isolated acceptance

Tests cover:

- a synthetic slow HTTP response where throttle, real attempt and outer logical elapsed are numerically distinct
- a failed first attempt followed by explicit retry/backoff and bounded second attempt
- peer aging beyond 15,000 ms before the already-deployed bounded refresh
- future-source bounded wait with wall/monotonic evidence
- unchanged future and 2,000 ms execution-latency gates
- existing full-suite source-after-arrival/pre-arrival rejection
- bounded 32-record storage
- restart persistence of the retained timing tail

## Scope limitations

This change does not:

- diagnose the original OS clock cause
- modify host/WSL clock settings
- modify supervisor lifecycle/autostart
- alter storage policy
- alter strategy/risk/account/research window
- backfill offline data
- change the deployed freshness-refresh behavior
