# Issue 38 — bounded repeated-unknown causal evidence

This candidate changes only how a **future new discovery root** records repeated
source-unknown observations that do not themselves change a trading/risk
decision. It does not change strategy, source-age/arrival/chronology gates,
queue/fill semantics, fees, funding, risk, storage budget, the 90% entry stop,
PR21, or the deployed PR33/PR35 contracts.

## Causal and unknown contract

Decision-changing facts remain append-only hash-chained evidence exactly as
before: signal decisions, order states/rejections/cancellations, maker arrival
queue, relevant maker trade observations, fills, fees/funding/realized ledger,
first runner gap, recovery, stop/risk/protective decisions.

A repeated non-decision-changing unknown is stored in
`unknown_ranges` using a five-minute bucket keyed by scope and reason code.
Each durable range preserves:

- exact count
- first/last dispatch timestamp
- first/last source and receipt timestamp when available
- first/last event ID and trade ID when available
- first/last reason
- a rolling SHA-256 digest over every observation summary
- a row-integrity hash

For the declared heavy 48h envelope there are exactly three aggregated
source-event scopes (aggTrade/book/mark) and 576 five-minute buckets, giving
1,728 source-event range rows for those 120,960 invalid raw events. The eight
declared 30-minute outages additionally model one repeated transport unknown
per minute after each individually preserved first gap; those repeated outage
observations occupy at most 48 more five-minute range rows. The declared total
bound is therefore **1,776 unknown-range rows**. The acceptance artifact reports the actual
`causal_evidence.sqlite3` bytes and all other root bytes; no fixed per-row byte
size is assumed.

The range explicitly records `reconstructible_events=0`. Rolled raw payloads
inside the range **cannot be reconstructed event-by-event** and remain unknown.
Range evidence cannot be used to infer a price, queue depletion or maker fill.

The raw `SharedFeed` remains authoritative for cursor/dedup/conflict/order
checks and keeps the existing 4096 replay window. Aggregation happens only
after the existing normalization/chronology/source-validity result. It does not
relax late/future/out-of-order/conflicting-input rejection.

If an unknown observation would newly cancel an entry, change maker stress
state, create protective reduce-only handling, or otherwise change broker/risk
state, that observation remains an individual append-only `source_unknown`
record. First runner gap and recovery also remain individual evidence.

## Legacy root immutability

The Issue38 table is **new-root only**. Opening an existing legacy
`causal_evidence.sqlite3` without `unknown_ranges` does not auto-migrate it
and is byte-stable. Attempting the new bounded-unknown write path on such a
legacy root fails closed.

Therefore the frozen original ETH-DISCOVERY-LAB-001 root is not rewritten,
converted, pruned, reset or used as the Issue38 candidate.

## Declared engineering envelope

The admission artifact tests exactly the required 48h accelerated capacity
profiles under the unchanged 32 MiB budget and 90% entry gate:

- valid profile: same declared PR37 valid engineering load
- heavy profile: **40 late/invalid aggTrades + 1 late/invalid book + 1
  late/invalid mark = 42 invalid events/minute**, plus a 30-minute outage every
  six hours; each outage preserves the first gap individually, materializes one
  repeated transport-unknown/minute for the remaining 29 minutes, then preserves
  recovery individually

Both profiles retain 4096 raw events and account for every file in the isolated
root, including SQLite sidecars if any. Clean-close WAL/SHM absence is reported
rather than assumed.

The accelerated profile is **same-schema capacity materialization**, not 2,880
real engine polls. A separate actual SharedFeed/DiscoveryLab lifecycle smoke
exercises maker partial-fill evidence, a decision-changing invalid aggTrade,
bounded repeated unknowns, protective reduce-only exit, gap/recovery, operator
stop and same-root restart reconstruction.

Engineering admission is PASS only when valid and heavy are both below the
existing entry gate and reconstruction/safety checks pass. PASS still does not
mean a real 48h market run, public-source health or profitability.

This is a declared envelope, not a claim of permanent retention at arbitrary
event rates. If actual bytes exceed the envelope, the unchanged 90% storage
gate must halt new entry risk; protective reduce-only handling remains in force.

## Operator entrypoints

Read-only status on any existing root uses the already accepted CLI syntax:

```sh
python3 lab/discovery_runner.py --root ROOT report
```

Run the isolated Issue38 engineering acceptance from candidate source:

```sh
python3 lab/issue38_acceptance.py --output issue38-acceptance.json
```

Real-public evidence remains separate. Use the existing bounded public timing
probe in an environment where it is allowed. HTTP 451 or another transport
restriction is BLOCKED external-source evidence, not capacity/lifecycle PASS.

No future research window is started by these commands. A future new root/window
requires separate operator preregistration and decision.

## Rollback

Rollback is code/process/config rollback only. Stop the Issue38 candidate before
using an older implementation. Preserve the candidate root and all evidence;
do not drop the `unknown_ranges` table, back-convert it into synthetic
per-event facts, copy it over the old root, reset brokers, clear stop state or
extend a research deadline.

The original frozen root remains untouched throughout.

## Operator candidate handoff

For an accepted candidate, install reviewed source bytes into the existing operator source tree and use a **new, never-activated** namespace `<NEW_ROOT>`. The public repository remains a review mirror; it is not a live deployment directory.

### Existing source-to-local mapping

- `lab/discovery_evidence.py` -> existing operator `lab/discovery_evidence.py`
- `lab/discovery_lab.py` -> existing operator `lab/discovery_lab.py`
- `lab/issue38_acceptance.py` -> existing operator `lab/issue38_acceptance.py`
- `lab/tests/test_issue38_bounded_unknown.py` -> existing operator test path
- `docs/ISSUE38_BOUNDED_UNKNOWN.md` -> existing review-sync docs source
- `automation/review_sync.py` corresponds to the existing `repo_sync/review_sync.py` review/export source; it is not trading runtime.

Do not construct or bind replacement runtime objects manually. The existing runner owns those dependencies and gates.

### Existing executable entrypoints

```sh
# bounded public engineering probe; no private/account endpoint and no strategy-performance claim
python3 scripts/public_source_timing_probe.py

# reference/filter preflight only; does not start a research window
python3 lab/discovery_runner.py --root <NEW_ROOT> prepare

# only this explicit accepted action starts the new immutable forward window
python3 lab/discovery_runner.py --root <NEW_ROOT> activate --operator-accepted

# foreground shared-source run
python3 lab/discovery_runner.py --root <NEW_ROOT> run --poll-seconds 1

# bounded one-cycle diagnostic
python3 lab/discovery_runner.py --root <NEW_ROOT> run --once --poll-seconds 1

# existing status/report entrypoint
python3 lab/discovery_runner.py --root <NEW_ROOT> report

# durable stop request; never resets or extends a window
python3 lab/discovery_runner.py --root <NEW_ROOT> stop

# engineering-only Issue38 capacity/safety acceptance
python3 lab/issue38_acceptance.py --output issue38-acceptance.json
```

### Namespace and rollback

Preserve the old root/window/history unchanged. A new root does not reopen or extend any old window. The fixed 32 MiB envelope includes every store plus SQLite WAL/SHM and metadata whenever present. Rollback is source/process rollback only: stop the candidate, preserve the entire candidate root/evidence, and restore the operator-owned pre-install source backup. Never back-convert `unknown_ranges` into synthetic event facts, copy over the historical root, weaken source/arrival/risk/cost gates, or use GitHub `main` as the deployment tree.
