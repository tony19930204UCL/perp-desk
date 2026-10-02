# Deployment and unresolved gaps

This file describes reviewed scope, not a continuously live telemetry feed. Use latest local health/status for current operation.

## Deployed baseline at export preparation
- H1-PAPER-002, `lab/paper_runtime_v2.py --state-dir data/paper-v2 --status shared/paper_v2_live.json`.
- Dashboard loopback 18767 reads that snapshot.
- ENG-008 freshness patch accepted. See append-only `context/versions.md` for exact acceptance hashes and evidence references.

## Candidate, not deployment
- `lab/staging/batch_time/`: bounded real local wait for small future timestamps. Tested candidate is not an OS clock fix or live acceptance.
- `lab/staging/health/`: health endpoint/UI. Watchdog files and scheduler wrappers alone do not prove a deployed schedule.
- `lab/staging/freshness/`: retains candidate development history. Latest version log distinguishes the accepted ENG-008 patch.

## Must remain visible
- Repeated `ValueError: batch source age exceeded` in historical live probes. A candidate test pass cannot close this fault.
- Persistent health schedule and accepted live dashboard deployment need explicit parent acceptance/readback.
- Runtime restart/autostart, capacity growth, polling gaps and unknown long-duration availability remain operational concerns.
- No demonstrated strategy profitability. Initial zero fills/zero PNL is not a performance result.
- Seven-day and at least 30 closed forward-trade review gate is not claimed passed.
- Real execution adapter, private events, partial-fill/cancel races, ambiguous outcome recovery and real-account reconciliation are not claimed accepted.

Do not edit past results or silently close incidents. Recovery observations differ from accepted tested resolution.
