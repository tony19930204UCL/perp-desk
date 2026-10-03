# Deployment and unresolved gaps

This file describes reviewed scope, not a continuously live telemetry feed. Use latest local health/status for current operation.

## Deployed baseline at export preparation
- H1-PAPER-003, `lab/paper_runtime_v3.py --state-dir data/paper-v2 --status shared/paper_v2_live.json`.
- Current observer dashboard reads that snapshot and presents the research window/closed-trade analytics.
- Account/state namespace continuity remains `data/paper-v2`; the v3 strategy migration did not reset the account.

## Candidate, not deployment
- `lab/staging/batch_time/`: bounded real local wait for small future timestamps. Tested candidate is not an OS clock fix or live acceptance.
- `lab/staging/health/`: historical v2 health candidate retained as evidence; do not apply its old dashboard wholesale.
- Issue #4 integrates the reused watchdog with current v3 identity/current observer dashboard. A PR/CI pass still does not prove operator deployment or persistent scheduling.
- `lab/staging/freshness/`: retains candidate development history. Latest version log distinguishes the accepted ENG-008 patch.

## Must remain visible
- Repeated `ValueError: batch source age exceeded` in historical live probes. A candidate test pass cannot close this fault.
- Persistent health schedule and accepted live dashboard deployment need explicit parent acceptance/readback.
- Runtime restart/autostart, capacity growth, polling gaps and unknown long-duration availability remain operational concerns.
- No demonstrated strategy profitability. Initial zero fills/zero PNL is not a performance result.
- Seven-day and at least 30 closed forward-trade review gate is not claimed passed.
- Real execution adapter, private events, partial-fill/cancel races, ambiguous outcome recovery and real-account reconciliation are not claimed accepted.

Do not edit past results or silently close incidents. Recovery observations differ from accepted tested resolution.
