# Operator Event Gate (Issue #22)

Status: **staged source/tests/docs only; not installed or deployed**.

This change replaces an unconditional reasoning wakeup design with a deterministic
60-second read-only pre-check. It does not alter trading, PAPER risk, storage
limits, account state, health ownership, supervisor behavior, research windows,
model/provider configuration, or the existing dashboard.

## Verified Hermes scheduler capabilities

The implementation is based on the current Hermes cron documentation:
- the built-in in-process scheduler ticks every 60 seconds;
- a reasoning cron job may attach a pre-run `script`;
- a pre-run script whose final JSON line is `{"wakeAgent": false}` skips the
  reasoning agent entirely for that tick;
- script-only `no_agent` jobs use zero LLM calls and do not enter the inference
  layer; empty stdout is a silent tick;
- current Hermes also exposes monitor/monitor-script change-gating semantics for
  "wake only when this changes" cases, but simple change detection is not used as
  this lab's durable work-completion contract;
- Hermes persists cron attempt history before provider dispatch, distinguishes
  execution and delivery failures, exposes `hermes cron runs`,
  `hermes cron incidents`, and `hermes cron doctor`, and automatically
  re-runs recurring jobs after 5/15/30 minutes only for transient network/DNS
  failures that happened before any model call;
- Hermes explicitly advises integrations not to query its internal `state.db`.

References:
- https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- https://hermes-agent.nousresearch.com/docs/guides/cron-script-only
- https://hermes-agent.nousresearch.com/docs/reference/cli-commands

No live Hermes cron table was changed by this PR.

### Why this uses a pre-check, not a second no-agent/monitor job

The existing operator-followthrough job still needs reasoning when an event is
actionable, so the primary integration is the documented **agent job + pre-run
script + `wakeAgent`** contract. A `no_agent` job is useful when script stdout
is already the final message, but making it the primary Issue #22 worker would
remove the required reasoning step. A pure `monitor` / `monitor_script`
change gate is also insufficient by itself: an unfinished ready task must survive
provider failure, lease expiry, and a previous wake even when the observable
signature no longer changes.

Therefore this PR does not install a second no-agent watchdog or monitor job.
That avoids duplicate workers/deliveries. The deterministic script itself still
runs with zero model invocation on quiet ticks; only its durable queue can emit
`wakeAgent:true` for the existing reasoning job. If a future installed Hermes
build exposes a monitor wrapper around the same pre-check, the operator may
evaluate it separately, but it must preserve this claim/lease/backlog state and
must not create a duplicate operator-followthrough job.

## Architecture

`lab/operator_event_gate.py` owns a new isolated namespace only. Its inputs are
read-only paths configured by the operator:

- existing `shared/health_status.json`;
- current PAPER snapshot;
- existing `shared/work_status.json`;
- optional operator-confirmed hold file;
- explicitly registered research timer sources.

The gate never calls `health_watchdog.once()`. Therefore it cannot become a
second health writer. When the supervisor is running normally, the existing
120-second supervisor health writer remains authoritative. When the supervisor
is intentionally stopped/held, the gate only reads old health/snapshot evidence
and classifies the state.

The gate writes only:
- `gate_state.json` — durable event/queue/claim lifecycle;
- `gate_status.json` — small local summary;
- `gate_state.lock` — single-writer lock.

These files live beneath the configured isolated `namespace_dir`, never in a
trading, ledger, health, or research database.

## Stable event semantics

The following changing values are intentionally excluded from event identity:
timestamps while still inside the same state, age counters, poll counts and
cumulative counters. They may determine a threshold crossing, but once a subject
is already in the same semantic transition, later minute-by-minute changes do
not mint new events.

Material transitions include:
- health missing/corrupt/stale and recovery;
- PAPER snapshot missing/corrupt/stale/unsupported identity and recovery;
- exact runtime absent/duplicate/unverified and recovery;
- new or recovered existing health incidents;
- executable queued/failed work, or active work becoming genuinely overdue;
- configured authoritative checkpoint/deadline becoming due.

A normal unchanged tick prints exactly:

`{"wakeAgent":false}`

and performs no model call when installed as a Hermes pre-check script. Tests also
verify the durable gate-state bytes remain unchanged across normal ticks even
when source timestamps/counters advance.

## Storage hold regression contract

An operator-confirmed hold file has this minimal shape:

```json
{"active":true,"operator_confirmed":true,"reason":"storage_protection"}
```

Accepted reasons are `storage_protection`, `operator_hold`, or `manual_hold`.
When active:
- stale old PAPER snapshot is **not called healthy**;
- absent runtime is classified `operator_hold`, not a restart fault;
- no automatic restart/kill/repair is attempted;
- unchanged hold state does not wake the reasoning model every minute.

Clearing the hold does not erase the old observations. If the runtime/snapshot is
still faulty after hold removal, that is a new actionable transition and wakes
once.

## Research timers

Timer sources are **opt-in paths in local config**. Nothing is auto-discovered.
Therefore staged PR #24 files cannot make a timer live merely by existing.

Supported timer adapters:
- `paper_status`: requires deployed PAPER identity
  (`mode=paper`, `candidate_not_deployed=false`) and reads the current
  `research` registration;
- `discovery_state`: requires `activated=true`;
- `registration`: requires `active=true` and `operator_accepted=true`.

The gate derives start/checkpoint/deadline from that authoritative record. It
does not hardcode H1-PAPER-003, H1-PAPER-004, or a strategy deadline, and it
never edits or extends a window.

A configured timer path that is missing/corrupt, or an active registration with
invalid window fields, is an explicit once-per-transition `timer source fault`,
not "no timer". A deliberately inactive/unaccepted registration remains
non-actionable. Recovery to a valid active authority wakes once. This preserves
the distinction between unavailable evidence and a staged-but-not-live PR #24
registration.

## Durable queue / lease / evidence

An actionable transition becomes a durable record with timestamped lifecycle
evidence:
`observed -> queued -> claimed -> completed|blocked`.

A pre-check wake claims one event for a bounded lease. A second scheduler tick
cannot duplicate-claim the same active lease. The worker must explicitly adopt
that claim with a public-safe worker handle, read local health/work/evidence/
pending state, perform only permitted operator work, and then explicitly finish
with a durable evidence reference.

Wake delivery is **not** completion. A `running` label is not completion.
Completion requires the explicit terminal gate operation. For a task-backed work
event, `finish --outcome completed` is additionally rejected unless the
authoritative configured work source now reports that task `completed` or
`cancelled`. A successful agent response therefore cannot make a still-queued
or still-running backlog disappear.

If a worker/provider disappears, lease expiry requeues with bounded backoff.
Defaults in the example are 5m / 15m / 30m and max 4 attempts; these are
operational queue limits, not trading parameters. Explicit `provider`,
`notification`, and `worker_interrupted` failure hooks use the same bounded
retry path. Exhaustion becomes `blocked`, never an infinite reasoning loop.

The state is bounded. It never prunes active evidence to keep operating; capacity
exhaustion sets a local blocker instead of deleting trading/history evidence.

## Minimal scheduler payload

A wake contains only:
- event ID;
- kind and lifecycle transition;
- symbolic evidence reference;
- permitted next action;
- lease token and lease duration.

It never contains ledger rows, account balances, market history, private paths,
credentials or chat identifiers. The reasoning worker is expected to read the
authoritative local files after wake.

## Operator worker procedure

The scheduled reasoning prompt should be self-contained and include this exact
contract:

> A deterministic Perp Desk gate selected one operator event. Read the pre-run
> context for event_id and claim_token. First adopt the claim with
> `python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json adopt --claim-token <token> --worker-handle cron-worker:<run-handle>`.
> Then read local gate pending state, work status and referenced evidence before
> deciding anything. Never infer completion from the wake itself. Do not restart
> trading or relax storage/risk/source gates. When work is genuinely complete,
> finish the claim with a durable relative evidence reference. When manual access,
> permission, unavailable input, or another unsafe blocker remains, finish as
> blocked with the reason. Routine progress is local only; only material
> unresolved faults or genuine human action should appear in the final response.

Example terminal commands:

```sh
python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json \
  adopt --claim-token "$CLAIM" --worker-handle "cron-worker:$HANDLE"

python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json \
  finish --claim-token "$CLAIM" --worker-handle "cron-worker:$HANDLE" \
  --outcome completed --evidence-ref "evidence:operator-work:<id>"
```

For a genuine blocker use `--outcome blocked --reason "..."`.

## Exact staged install / replacement

1. Copy accepted `lab/operator_event_gate.py` into the profile lab and accepted
   `scripts/paper_operator_event_gate.py` into the same profile's `scripts/`.
   Hermes requires cron scripts to resolve within that profile's scripts root.
2. Copy `lab/operator_event_gate_config.example.json` to the **local excluded**
   path `lab/shared/operator_event_gate_config.json`, set `enabled=true`, and
   replace every placeholder with explicit absolute local paths.
3. Keep the isolated namespace outside trading/health stores. Configure only
   timer sources that are actually accepted/active. Do not list PR #24 discovery
   state until the operator separately activates that research.
4. Manually run the wrapper twice while inputs are unchanged:
   `python scripts/paper_operator_event_gate.py`.
   Both outputs must be byte-identical `{"wakeAgent":false}`.
5. List the existing cron table and identify the **existing unconditional
   operator-followthrough job by exact ID**:
   `hermes cron list`.
6. Prefer editing that exact existing job in place rather than creating a second
   reasoning job. Preserve its existing delivery destination and model/provider
   settings; this PR does not change them. With the accepted prompt stored in the
   shell variable `$PROMPT`, the documented Hermes CLI shape is:

   ```sh
   hermes cron edit "$JOB_ID" --agent \
     --schedule "every 1m" \
     --script paper_operator_event_gate.py \
     --prompt "$PROMPT"
   ```

   If the installed Hermes build exposes the same fields through
   `cronjob_manage update`, that is equivalent. Do not hand-edit `jobs.json`
   or Hermes' internal state database. If the installed CLI rejects any of these
   documented flags, stop and treat scheduler installation as operator-blocked
   until `hermes cron edit --help` is reconciled; do not create a duplicate job
   as a workaround.
7. Read back `hermes cron list`, manually trigger once with
   `hermes cron run <job_id>`, then inspect `hermes cron runs <job_id>` and
   `hermes cron doctor`. Confirm there is only one operator-followthrough
   reasoning job and the normal tick did not enter an agent run.
8. Leave the existing 120s supervisor health writer unchanged. In operator-hold,
   do not install a replacement health writer.

Effective detection latency is scheduler tick (up to about 60s) plus source
publication delay. Health freshness is still owned by the 120s supervisor and
its existing dashboard stale contract; this gate does not shorten or relax a
trading/data gate.

## Provider and delivery failures

Hermes itself durably distinguishes agent/provider failure from delivery failure,
records execution history/incidents, and exposes them via `cron runs`,
`cron incidents`, and `cron doctor`. Current documentation specifies the
5/15/30 minute automatic re-run ladder only for recurring runs that fail with a
transient network/DNS error **before any model call**; it is not treated here as
a general work-completion retry contract. The gate independently retains its
claim until explicit terminal evidence, so provider recovery or successful wake
delivery is never enough to lose unfinished work.

**Platform integration limitation:** current public Hermes documentation does not
describe a post-delivery callback from the delivery router back into the pre-run
script. Therefore this staged PR does not claim an automatic atomic gate-state
update from `last_delivery_error`. The isolated scheduler-adapter acceptance
exercises the gate's explicit `notification` failure hook and bounded retry.
In live installation, Hermes' own delivery incident remains authoritative and
must be checked with `hermes cron doctor`; do not mark a human-only blocker
completed merely because the agent produced text.

This limitation is explicit rather than querying Hermes internal databases or
inventing an unsupported API.

## Offline / permissions limits

If the gateway/host is offline, no 60s scheduler tick can occur. The gate cannot
repair that blind interval. On recovery, authoritative source age/timer/work
state is evaluated at the next tick. A host outage does not justify replaying
trades or extending a research window.

The gate has no permission to:
- start/stop/restart/kill/flatten trading;
- submit/cancel exchange or PAPER orders;
- mutate default PAPER, health, supervisor, dashboard or ledger state;
- change risk/source/storage limits;
- edit model/provider settings;
- install services or cron jobs itself.

## Rollback

Operator rollback is scheduler/config/source rollback only:

1. pause the edited operator-followthrough job;
2. restore its previously read-back prompt/schedule/script fields if desired;
3. preserve the isolated gate namespace as evidence;
4. never restore an old trading DB, alter a research deadline, or restart PAPER
   solely to make monitoring look healthy.

No claim in this document treats synthetic CI as deployment, live observation,
token savings, trading performance, or market correctness. The only guaranteed
cost property being tested is **zero model invocation on unchanged gate ticks**.
