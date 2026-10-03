# PAPER v3 operational health integration

Status: reviewed candidate for operator acceptance. Not deployed by this PR.

## Reused components

This change reuses the existing read-only watchdog lifecycle in `lab/health_watchdog.py`: snapshot validation, process inspection, source/receipt freshness, storage thresholds, bounded telemetry, stable incident lifecycle, duplicate-delivery suppression, explicit resolution evidence, and work-status separation.

The old staged dashboard copy is **not** deployed wholesale. Health is integrated into the current observer dashboard so the existing research window, closed-trade analytics and observer security behavior stay intact.

## Current deployment identity

The supported trading process is exactly:

```
python [supported interpreter flags] paper_runtime_v3.py --state-dir data/paper-v2 --status shared/paper_v2_live.json
```

The monitor requires:
- deployed `H1-PAPER-003`;
- `candidate_not_deployed=false`;
- `candidate_implementation=paper-engine-v3`;
- exactly one matching process with the expected cwd, script, state directory and status path.

Zero matching processes is `runtime-absent`. More than one is `runtime-duplicate`. Interpreter flags such as `-u` are recognized before script matching.

## Health semantics

Healthy means all currently observed operational checks pass. It is never inferred from an engineering task being `completed`.

The monitor checks:
- exact runtime process identity/count;
- snapshot heartbeat age;
- feed connection and last-success age;
- raw market source timestamps and receipt age;
- explicit runtime errors/blockers/halted state;
- storage budget/minimum free-space observations;
- pending engineering work freshness.

Missing, malformed, future or stale evidence is not healthy. A stale health report is forced unhealthy by `/api/health` at request time.

Incidents retain stable IDs and first-seen evidence. Three clear monitor observations move an incident only to `recovered_monitoring`; they do not prove root-cause resolution. Explicit accepted test evidence is still required for `resolved`.

## No autonomous repair

The monitor does not:
- start/stop/restart the PAPER runtime;
- edit strategy, risk, account state, databases or research window;
- rewrite timestamps;
- create coding tasks or wake a coding agent.

`scripts/paper_health_engineering_gate.py` is retained as a compatibility/operator helper but now only emits a read-only GitHub-Issue handoff recommendation. `wakeAgent` behavior is not used.

## Dashboard integration

The current observer dashboard adds:
- `GET /api/health`, independent of `/api/status` and `/api/work`;
- a health/unresolved-incident panel beside the existing research metrics;
- request-time stale-health detection (>180 seconds).

Existing localhost Host/Origin/cross-site restrictions and read-only HTTP methods apply to health as well. Missing market data may make `/api/status` unavailable without hiding a valid health report.

## Operator-only scheduling/deployment contract

No schedule is created by this PR.

After accepting exact source hashes and CI:

1. Apply the accepted files locally without changing trading state.
2. Run one isolated `health_watchdog.py --once` and inspect `shared/health_status.json`.
3. Verify exact v3 PID/cwd/argv identity and that stale/fault fixtures cannot report healthy.
4. Restart only the dashboard if the dashboard files are accepted; do not restart trading for presentation changes.
5. Read back `/api/status`, `/api/health`, and `/api/work`; verify observer research metrics are unchanged and health remains independent.
6. If persistent monitoring is desired, the operator may schedule only `scripts/paper_health_monitor.py` at a cadence comfortably below the 180-second dashboard stale threshold (the staged design used 2 minutes). Read back the scheduler entry and manually exercise it.
7. Do **not** schedule autonomous engineering repair. New unresolved incidents are handed off through the normal GitHub Issue workflow.
8. Roll back only health/dashboard integration files if acceptance fails. Do not roll back or mutate trading/account state.

The monitor writes only its separate health namespace (`shared/health_status.json`, `data/health/*`) and never trading databases.
