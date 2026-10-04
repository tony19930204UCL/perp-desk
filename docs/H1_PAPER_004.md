# H1-PAPER-004 staged preregistration and same-account migration

Status: candidate source/tests only. **Not deployed.** Operator owns research acceptance and activation.

## Frozen definition

H1-PAPER-004 is exactly the Issue #20 operator preregistration.

Unchanged from H1-PAPER-003:

- ETHUSDT long-only PAPER
- closed 1m bars
- 60 prior return samples
- downside sigma 1.5
- volume multiple 1.2
- existing entry-reference semantics
- 0.5% stop-distance semantics
- 30 minute max holding
- minimum gross reward strictly above 2x the same estimated full cost
- taker-only execution
- existing maker/taker assumptions and finalized funding accounting
- spread / observed depth / two-tick entry and exit slippage
- 2,000 ms execution latency
- 15,000 ms source-age gate
- future/source-after-arrival checks
- PAPER-RISK-001 loss/exposure/position limits
- storage protection, no-auto-restart and bounded supervisor stop contracts

The only strategy hypothesis change is the target.

## Frozen target

For a raw downside/volume signal on minute S:

1. take exactly the fifteen immediately preceding closed 1m bars;
2. the bars must be contiguous and must end immediately before S;
3. exclude S itself;
4. require finite positive closes, finite non-negative volume and positive total volume;
5. compute, at Decimal precision 50:

```
sum(close_i * volume_i) / sum(volume_i)
```

This is called a **VWMA proxy**, not trade-level VWAP.

The target and its fifteen source bar timestamps/closes/volumes are persisted in the signal intent. Later prices never revise the target.

If the target context is missing/noncontiguous/invalid or has zero total volume, the raw trigger remains diagnosable but admission fails closed with a null target. No target is moved merely to pass the cost gate.

## Candidate economics / funnel

Each unique raw signal gets one bounded durable diagnostic record.

The record separates:

- signal-bar close timestamp and its exchange closed-bar timestamp domain
- detector timestamp and runtime UTC wall domain
- actual routing-decision timestamp and runtime UTC wall domain
- timely status against the unchanged 15,000 ms gate
- source-valid status
- contemporaneous public source timestamps
- existing entry reference
- frozen target
- contemporaneous bid / ask
- entry bound
- stop price
- favorable gross distance
- cost components:
  - spread
  - configured entry slippage
  - configured exit slippage
  - taker-fee round trip
- estimated full cost per unit
- gross-to-cost ratio
- frozen minimum ratio = 2
- cost-qualified boolean
- actual admission status/reason

A diagnostic computation has no broker side effect.

If position/pending/risk/storage state blocks a real entry, economics may still be recorded for the raw opportunity, but the real admission reason remains the blocker and no synthetic order is submitted.

`cost_qualified=true` is an opportunity classification only. It is never a fill, completed trade, PnL sample or profitability claim.

The state keeps at most 3,000 candidate records. Since the preregistered 48h interval has at most 2,880 one-minute signal slots, the bound covers the complete candidate window without an unbounded per-poll log.

## Effective first-8h minute coverage

Only the first eight hours of the new strategy window contribute to the checkpoint.

For each first-8h closed minute, the runtime records at most one coverage record containing:

- close timestamp
- first evaluation/detection timestamp
- timely boolean
- source-valid boolean
- successful source-validation timestamp when present

Maximum records: 480.

A historical minute processed after reconnect may be source-valid at the later fetch but remains `timely=false` and does not count as effective live coverage.

Expected slots at the 8h checkpoint: 480.

Threshold:

```
timely_source_valid_slots / 480 >= 0.90
```

Therefore 431/480 fails and 432/480 reaches the frozen threshold.

## 8h throughput checkpoint

At `strategy_start_ms + 28,800,000`:

1. if effective coverage < 90%:
   - status = `data_quality_inconclusive`
   - stop new entries
2. otherwise count unique raw signal IDs that were timely, source-valid and cost-qualified during the first eight hours;
3. if fewer than 4:
   - status = `throughput_infeasible`
   - stop new entries
4. exactly 4 or more:
   - status = `passed`
   - checkpoint adds no entry blocker

Duplicate IDs count once.

A candidate blocked by existing exposure/pending/risk/storage may still count as a cost-qualified opportunity if its contemporaneous economics satisfy the unchanged gate. That does not create a fill/order.

Existing risk/storage/integrity blockers remain independently authoritative before and after the checkpoint.

The checkpoint is not a PnL stopping rule and never changes the 48h deadline.

## Poll/recovery diagnostics

Existing `poll_error` audit events remain the event-level evidence.

H1-PAPER-004 additionally keeps bounded state summary fields for:

- poll error event count
- consecutive error polls
- error-period start observations
- recovery observations
- at most 32 completed error-period summaries

An error count is not called an Internet outage count.

DNS or transport text is not treated as proof of private host/ISP root cause.

## New 48h strategy window, same account

A successful 004 migration sets only a new strategy window:

- `strategy_start_ms = operator activation time`
- `research_deadline_ms = strategy_start_ms + 48h`
- strategy fill baseline = current cumulative fill length
- strategy ledger baseline = current cumulative ledger length
- strategy signal baseline = all already-handled signal IDs
- decision cutoff = activation time

It preserves:

- original account `forward_start_ms`
- broker initial cash/account identity
- current cash/equity history through durable broker state
- all prior orders/fills/funding/ledger
- broker audit
- runtime audit prefix
- daily loss baselines
- total/daily halt state
- all old handled signals
- old H1 signal registrations/results
- H1-PAPER-003 research start/deadline in `strategy_window_history`

No old signal is re-routed because H1 signal IDs are version-bound and all old handled IDs are separately baselined.

## Hard prerequisite: accepted H1-PAPER-003 closeout

Migration is rejected unless all are true:

- exact runtime is stopped and account locks are exclusively obtainable
- runtime DB, broker DB and signals DB pass SQLite quick checks
- runtime audit hash chain is valid
- durable config hash is the exact accepted H1-PAPER-003 hash
- durable deployment marker is H1-PAPER-003
- durable runtime account identity matches the broker's persisted account version identity and original forward start
- current time is at/after the original H1-PAPER-003 deadline
- account is flat with no pending/resting orders
- broker ledger exactly reconciles to broker cash
- operator supplies an explicit closeout acceptance JSON matching this same account's:
  - original forward start
  - H1-PAPER-003 strategy start
  - H1-PAPER-003 fixed deadline
- closeout acceptance time is at/after that deadline and not in the future

Example schema (values illustrative):

```json
{
  "schema_version": 1,
  "version_id": "H1-PAPER-003",
  "operator_accepted": true,
  "account_forward_start_ms": 1000000,
  "strategy_start_ms": 1200000,
  "research_deadline_ms": 2000000,
  "accepted_at_ms": 2500000
}
```

The migration persists only the SHA-256 of this operator acceptance file, not a machine path.

## Migration command

Only after stopped-state operator capture and accepted 003 closeout:

```sh
python lab/paper_migrate_v4.py \
  --apply \
  --state-dir "$STATE_DIR" \
  --config lab/paper_config_v4.json \
  --accepted-closeout "$CLOSEOUT_JSON"
```

This migration does **not** create a backup account copy.

It validates broker/signals/runtime state read-only, then performs one SQLite transaction in the runtime DB that:

- appends one migration audit event
- changes runtime strategy registration/config identity to H1-PAPER-004
- adds the new strategy baselines/window
- removes the old deployment marker so the candidate remains not deployed

It does not write broker.sqlite3 or signals.sqlite3.

Operator should independently hash/read back those files before and after migration.

## Runtime / health / supervisor identity

Staged H1-PAPER-004 uses:

- `paper_runtime_v4.py`
- `paper_config_v4.json`
- the same state directory
- the same status namespace
- the same storage policy

Health/supervisor configuration now supports an optional explicit:

```json
"runtime_script": "paper_runtime_v4.py"
```

Default remains `paper_runtime_v3.py`, so current H1-PAPER-003 operation is unchanged until the operator explicitly changes the local config.

The health monitor accepts only the exact supported script/version/implementation pair:

- paper_runtime_v3.py / H1-PAPER-003 / paper-engine-v3
- paper_runtime_v4.py / H1-PAPER-004 / paper-engine-v4

Supervisor process identity still requires exact cwd + script + state + status. Duplicate ownership remains fail-closed.

## Operator activation sequence

This PR does not execute these steps.

After PR acceptance and **after** accepted H1-PAPER-003 closeout:

1. record exact current runtime/dashboard/health identities and account hashes;
2. stop the exact current manager/runtime according to the accepted bounded-stop contract;
3. verify no exact runtime remains;
4. capture stopped-state hashes / audit head / broker cash+ledger counts;
5. create and independently review the 003 closeout acceptance JSON;
6. run migration;
7. verify:
   - broker DB unchanged
   - signals DB unchanged by migration
   - runtime audit old rows are an exact prefix plus one migration row
   - forward start unchanged
   - day/loss baselines unchanged
   - old orders/fills/ledger/history retained
   - new deadline is exactly activation + 48h
8. instantiate 004 once against the same namespace to create/read the new H1 registration; do not backfill old eligible signals;
9. independently verify source/arrival/storage/risk health;
10. mark deployment only after operator acceptance;
11. change operator-local health/supervisor `runtime_script` to `paper_runtime_v4.py` together with the v4 runtime config;
12. verify exactly one runtime and the same status/storage/dashboard account view.

The 48h strategy window begins from the operator migration/activation time, not from CI/tests, old bars or PR creation.

## Rollback

Rollback must never restore an old account DB copy.

Before 004 is marked deployed, operator may stop the candidate and correct/review the staged migration only through an explicitly reviewed forward migration; do not overwrite live stores from rehearsal copies.

After any authentic 004 account activity exists, rollback is process/config management only:

- stop the exact runtime safely
- preserve current broker/runtime/signals stores
- do not reset account capital/loss baselines
- do not delete 004 fills/orders/funding/audit
- do not move either the old 003 deadline or new 004 deadline
- do not re-route old signals

Any strategy-version rollback after authentic activity requires a new explicit forward registration/migration, not database restoration.

## Evidence classification

All PR fixtures are synthetic engineering evidence.

They are not:

- strategy PnL
- proof of profitability
- old-price backtests
- a reason to optimize the preregistered parameters
- deployment proof

Actual deployment continuity remains operator-owned and unverified until the stopped live account is independently captured and migrated.
