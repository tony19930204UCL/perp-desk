# SA1-r0 source aging repair (engineering candidate only)

## Boundary and diagnosis
Issue #64 records H2 ETH depth source 1792097814457, receipt 1792097814631,
dispatch 1792097816356, broker call monotonic 171820994.040658 to
171851168.468456, then rejection at 1792097846573 (31573ms old).
These are full-client/broker wall boundaries, **not HTTP or SQLite latency attribution**.
The existing singleton persisted every historical seen event per event and
deep-copied the same history on each event; this is a demonstrable avoidable
O(history) write/copy path, not proof it caused the whole observed delay.
PR59/60 are undeployed; do not merge or copy their stacked retention/history policies.

## Narrow implementation
- Broker keeps exact complete public input and the original audit/ledger/order fields.
  New `sim_seen_events` stores only append-only (event ID, canonical event
  SHA256), in the **same SQLite transaction** as the singleton update.
  Duplicate/conflict identities survive restart. Pre-existing embedded full or
  digest history is migrated atomically, with incompatible/foreign databases
  rejected; there is **no event pruning**, timeout extension, or window reset.
  The separate table differs from undeployed PR59's constraints/schema and
  MUST NOT be pointed at a PR59/60 experimental namespace without a separate
  schema/migration compatibility review.
- SA1-r2 restores the first traded-book delivery **before** bootstrap or
  candle processing so real position reduction cannot be blocked by a failed
  context rebuild. After candle work, only peers whose original source age is
  greater than the unchanged 15000ms gate are fetched again (one bounded
  refresh), using actual new receipts rather than adjusted timestamps.
  Both H2 and F1H use the same base refresh method. All refreshes still pass
  actual broker-delivery checks, full batch validation, and existing signal
  arrival/cost/risk gates; future sources remain rejected.
  Main's F2 failing fixture models a production safety regression (exit must
  precede context work), not a bad assertion. F1's measured replay models
  the decision-freshness contract; moving collection after klines changed its
  request order and left the saved input too old. Neither frozen test changed.
- When broker work itself exceeds 15000ms, subsequent source checks still
  reject; there is no automatic retry of an old input and no claim of cure
  under arbitrary broker latency.

## Isolated operator runbook (not executed here)
Start only in a new ephemeral namespace; never point at a deployed state root.
From a clean local clone at the exact PR head:
```sh
python3 -m unittest discover -s automation/tests -v
cd lab && python3 -m unittest discover -s tests -p 'test_source_aging_repair.py' -v
cd lab && python3 -m unittest discover -s tests -v
```
To run the existing public one-shot paper runtime **only after validating its
public-network policy and with an intentionally new isolated state/status path**:
```sh
cd lab
python3 paper_runtime_v5.py --once --state-dir /tmp/source-aging-public-isolated-unique --status /tmp/source-aging-public-isolated-unique.json --config paper_config_f1h_btcusdt.json
```
The one-shot may fail closed for unavailable public reference/funding coverage
or stale/future data. Do not interpret return 0 as live strategy acceptance.
For timing analysis, preserve each public_receipt.request_start_ms,
request_return_ms, receipt.received_at/source_timestamp_ms, broker_dispatch
source_ms/received_ms/dispatch_ms/call_monotonic_ms/return_monotonic_ms,
logical_fetch sub-boundaries and source_validation outcomes; compare source
to **actual delivery** and decision, not receipt-to-receipt as HTTP latency.
Use synthetic injected monotonic clock and slow client/broker in the independent
acceptance harness and prove 14999/15000 accepted, 15001/future rejected, peer
aging, rollback and before/after differential; these are outstanding where not
covered by the narrow test file.

## Independent acceptance
Main CIO must verify a fresh clean clone, exact source SHA, both push and
pull_request CI runs with actual IDs, full legacy and new test-ID counts,
controlled slow broker/persistence and peer aging traces, public isolated
dry-run timings, PR59/60 compatibility, and correct behavior after restart.
Do not deploy or infer root cause from the optimization alone. Any deployment
requires a separate operator-approved, flat-only, backed-up, hash-verified
readback and must never restart a position-bearing arm experimentally.
