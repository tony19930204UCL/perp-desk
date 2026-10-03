# PAPER v3 storage protection

Status: candidate for operator acceptance. This document is operational configuration, not a strategy/risk change.

## Why this exists

Namespace growth and disk exhaustion are different conditions. A namespace may cross an observation warning while the filesystem still has substantial free space. Do not label that state "disk full".

The policy therefore separates:

- **warning threshold**: observation only; normal PAPER behavior may continue.
- **new-risk limit**: new exposure is inhibited. Existing pending entry orders are canceled before further market delivery when a durable cancellation write is still safe.
- **hard namespace limit**: `new_risk_limit_bytes + exit_reserve_bytes`. The reserve is not permission for new risk; it exists only to leave bounded headroom for accounting/cancel/exit work.
- **minimum free-space reserve**: an exit/accounting cycle is allowed only when the configured worst-case cycle can complete while leaving `min_free_bytes` untouched.
- **disk_full**: reported only when observed free bytes are exactly zero. A low configured reserve is not called disk full.

No policy path deletes, truncates, compacts, rewrites, fabricates or resets fills, ledger, audit chain, account baselines or research evidence.

## Operator-owned policy

Use a local JSON file outside the mirror-owned source tree, for example:

```json
{
  "schema_version": 1,
  "warning_bytes": 1000000000,
  "new_risk_limit_bytes": 1200000000,
  "exit_reserve_bytes": 268435456,
  "min_free_bytes": 2147483648,
  "max_exit_cycle_bytes": 67108864
}
```

The numbers above are illustrative only. They are **not** recommended live values and are not applied by this PR. The operator chooses values from actual filesystem/database observations.

Required relationships:

- all byte values are positive integers;
- `warning_bytes < new_risk_limit_bytes`;
- `max_exit_cycle_bytes <= exit_reserve_bytes`.

The configured hard namespace limit is:

```
new_risk_limit_bytes + exit_reserve_bytes
```

## Runtime invocation

Existing PAPER v3 invocation remains unchanged except for the explicit operational policy argument:

```sh
python -u paper_runtime_v3.py \
  --state-dir "$STATE_DIR" \
  --status "$STATUS_PATH" \
  --storage-policy "$STORAGE_POLICY"
```

Omitting `--storage-policy` preserves legacy behavior and does **not** claim capacity protection is active.

## Runtime behavior

At each protected cycle the runtime performs bounded metadata-only namespace/free-space observation.

### Normal / warning

Below the new-risk limit, the runtime operates normally. Crossing only the warning threshold is visible but does not become a storage halt.

### Protect

When new risk is no longer permitted:

1. no new signal may open exposure because `storage_new_risk_inhibited` joins runtime blockers;
2. any already-pending non-reduce-only order is canceled before further market delivery, provided that cancellation can itself be durably recorded;
3. if the account is flat after cancellation, the runtime publishes a final protected snapshot and exits with a non-zero operational stop instead of continuing unbounded research writes;
4. if an existing position or reduce-only exit is present, a cycle is allowed only when both namespace reserve and free-space reserve can cover the configured `max_exit_cycle_bytes`.

The reserve is specifically for **existing exposure accounting/exit**, not for extending research or admitting new risk.

### Halt

If a protected exit/accounting cycle cannot fit inside either reserve, the runtime stops before further market delivery and reports `StorageProtectionHalt`. It does not claim an exit occurred.

If an actual durable write fails despite preflight, the broker/runtime transaction failure is propagated as an operational storage halt. SQLite transaction rollback plus broker in-memory rollback must preserve the previously committed prefix. Recovery is by correcting capacity/configuration and restarting the same namespace; no reset or alternate namespace is created.

## Boundedness

`max_exit_cycle_bytes` is an operator-declared conservative bound. Protected cycles measure actual namespace growth. If one cycle exceeds the configured bound, the snapshot is marked halt and the process stops after that snapshot is published.

This policy never fills a disk to test limits. Unit/integration acceptance injects synthetic byte observations and synthetic interrupted SQLite writes.

## Recovery invariants

After interruption/restart:

- all prior fills remain an exact prefix;
- all prior ledger rows remain an exact prefix;
- all prior broker audit events remain an exact prefix;
- account/research baselines remain unchanged;
- the same failed event can be retried only through existing event/order idempotency rules;
- no automatic evidence deletion or loss-erasing reset is permitted.

## Health integration

When runtime storage protection is present in the PAPER snapshot:

- `warning` is exposed as `storage-capacity-warning` and does not by itself mean unhealthy/disk full;
- `protect` is exposed as `storage-new-risk-inhibited`;
- `halt` is exposed as `storage-capacity-halt`.

Before storage protection is deployed, the health monitor keeps its older read-only observation-budget fallback. That fallback is explicitly an observation, not proof that runtime protection exists.

## Operator acceptance

Before deployment:

1. verify the exact PR head and green CI;
2. choose local policy values from real namespace/free-space observations without changing strategy/risk/research settings;
3. run isolated `--once` acceptance in a copied/synthetic namespace, never the live account;
4. confirm warning, protect and halt states are distinct;
5. confirm insufficient free space is not called disk full unless free bytes are actually zero;
6. confirm interrupted writes preserve committed fill/ledger/audit prefixes and restart succeeds;
7. confirm a pre-existing position can still produce a durable reduce-only exit while new risk is inhibited;
8. confirm too little reserve stops before any unaudited exit;
9. only then update the operator-managed runtime invocation/configuration.

This PR does not deploy, restart the live runtime or alter live capacity values.
