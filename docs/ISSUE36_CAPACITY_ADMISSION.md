# Issue 36 — fixed-budget 48h admission

This delivery is an **admission test**, not a new research window. It does not
activate, restart, reset or modify an operator root.

## What is measured

`lab/issue36_capacity_admission.py` drives the repository's actual
`SharedFeed`, `DiscoveryLab`, three broker SQLite stores and append-only
`CausalEvidence` store in isolated temporary roots. It reports every file/store
size, raw rollover counters, evidence counts and payload bytes by evidence kind,
source/receipt/dispatch samples, poll success/failure/gap/recovery counters, and
same-root restart readback.

Two accelerated 48h engineering profiles are explicit:

1. Continuous valid source: one poll/minute with one book, mark,
   funding-status, aggTrade and closed forward bar per minute.
2. Heavy invalid/outage: the exact tested accelerated bulk load materializes
   **40 late/invalid aggTrades + 1 late/invalid book + 1 late/invalid mark =
   42 invalid raw events/minute**, plus a recovery marker for a declared
   30-minute transport-outage lifecycle every six hours. This is the same
   workload recorded by the accepted PR37 artifact; it is not reduced here.

These are synthetic engineering loads. They are not 48h of real market data,
not profitability evidence, and do not identify the cause of the historical
operator-root growth.

Evidence classes are deliberately separate:
- **accelerated bulk same-schema materialization** proves byte growth and
  retention/capacity behavior in the actual SQLite schemas. Its
  `fetch_calls=0` is expected and explicitly means it did **not** execute
  2,880 real engine polls.
- **actual engine lifecycle smoke** uses the real SharedFeed/DiscoveryLab path
  for a bounded valid source plus late/invalid input, source-gap/recovery and
  same-root restart readback. It proves lifecycle semantics only, not a 48h run.
- **real-public source evidence** must come from the existing bounded public
  probe. A successful workflow step with HTTP 451/error output is still
  externally BLOCKED; a placeholder contract string is not a completed public
  probe.

A valid-only capacity PASS is therefore not complete source admission. Full
engineering admission requires every declared capacity profile plus causal and
safety reconstruction to pass, and still does not imply real-public source
health, real 48h polling or profitability.

## Admission rule

Budget remains exactly 32 MiB and the existing entry stop remains 90%.

- **PASS**: both profiles complete all 2,880 minutes below the existing 90%
  entry-stop, with 4096 raw rollover, causal reconstruction store, all broker
  stores and restart readback included.
- **NOT_FEASIBLE**: either required profile reaches the existing 90% gate
  before 48h.
- **BLOCKED**: real-public probing is unavailable or the operator's read-only
  preflight cannot establish required forward-source evidence. GitHub HTTP451
  or network restrictions are recorded as blocked, never converted to PASS.

A NOT_FEASIBLE result is an engineering capacity result only. It does not
invalidate strategy PnL because the historical window lacked adequate data.

## Operator entrypoints

Before any separately preregistered future window, use the existing read-only
report entrypoint on the intended root and do **not** run/activate it:

```sh
python3 lab/discovery_runner.py --root ROOT report
```

Run the isolated admission probe from candidate source:

```sh
python3 lab/issue36_capacity_admission.py --output issue36-admission.json
```

Use the existing public timing/source probe when the environment permits it;
HTTP451/network inability is BLOCKED evidence, not a reason to synthesize a
public success.

Do not start a new window unless the candidate admission result is PASS, the
operator separately preregisters the window/root, and the existing report/probe
preflight is acceptable. No glue code is required.

## Preserved contracts and current limitation

PR35 transport retry, sticky concurrent stop, protective reduce-only exits,
4096 raw rollover/compaction and fixed-capacity stop remain unchanged. PR33
readonly dashboard and three-arm/history isolation are untouched.

This delivery deliberately does not prune or rewrite causal history, increase
the budget, move bytes outside the budget, reset the old root, relax source
age/arrival/chronology/queue/risk/cost rules, or synthesize fills.

If the heavy invalid/outage profile is NOT_FEASIBLE, the minimum unresolved gap
is a bounded causal representation for repeated unknown-source events that still
preserves the decisions needed for reconstruction. That semantic change is not
silently introduced by the admission probe; it requires separate exact-head
engineering and regression evidence before any future window.
