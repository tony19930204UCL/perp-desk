# Independent AI review brief

1. Read README, ARCHITECTURE, DEPLOYMENT_AND_GAPS and context/versions.
2. Inventory tests against actual implementation. Do not trust checklist ticks alone.
3. Run isolated commands in VERIFICATION. Never run runtime CLI with production namespaces.
4. Separate production source, staged candidates, historic proof and current operational measurements.
5. Check raw timestamps, close cursors, arrival/latency, forward start, accounting equality, Decimal precision, idempotency and evidence retention.
6. Check dashboard host/origin/path security, stale-data failure modes and safe textContent rendering.
7. Check incident lifecycle/dedup/ownership. Engineering completed must not erase new operational faults.
8. Check auto-sync allowlist, secret scans, remote private identity, remote SHA verification, no force push and retry behavior.
9. Identify untested real execution concerns separately from paper behavior.

Report: severity, exact file/line, minimal repro command, observed versus expected, scope (live/candidate), and missing proof.
Use GitHub issues or a separate branch for proposals. Do not modify mirror main or imply permission for live trading.
