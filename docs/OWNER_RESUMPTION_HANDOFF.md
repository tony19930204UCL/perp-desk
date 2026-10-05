# Owner Resumption Handoff (Issue #26)

Status: **staged source/tests/docs only; not deployed**.

This closes one specific gap left after the accepted Issue #22 event gate: a
minute-level gate can detect overdue work and successfully wake a cron worker,
yet the worker may truthfully stop because unattended approval/capability policy
denies the required read-only analysis. A wake, a queued task, or a delivered
message is not execution and is never treated as completion here.

This change does not modify the one-minute gate semantics, approvals, model or
provider, trading/runtime state, the 120-second health writer, storage/risk
limits, research windows, or account history.

## Official Hermes capability boundary

Current Hermes documentation supports:

- cron jobs running in fresh sessions;
- unattended cron approval policy, including fail-closed `cron_mode: deny`;
- one-shot zero-LLM delivery with `hermes send`;
- cron delivery targets and execution/delivery incident readback.

Current public documentation does **not** provide an API that atomically resumes
a previously blocked cron turn inside an already-authorized interactive owner
session. This implementation therefore does not claim automatic owner-session
resumption.

References:
- https://hermes-agent.nousresearch.com/docs/user-guide/security/
- https://hermes-agent.nousresearch.com/docs/user-guide/features/cron/
- https://hermes-agent.nousresearch.com/docs/guides/pipe-script-output
- https://hermes-agent.nousresearch.com/docs/reference/cli-commands

No approval mode, allowlist, interpreter, model/provider, cron table, host service
or messaging credential is changed by this PR.

## Lifecycle truth model

Issue #26 makes the following states explicit:

1. **detected / queued** — the Issue #22 gate observed actionable work.
2. **claimed** — a cron worker owns a bounded claim lease.
3. **policy/capability blocked** — the worker has a real handle/evidence but
   unattended policy denied the needed operation.
4. **awaiting_owner** — the cron claim is released; the work remains unfinished
   and has a durable owner handoff.
5. **escalation delivered** — the supported `hermes send` transport returned
   success. This is only transport evidence.
6. **owner_received** — the authorized owner explicitly acknowledges the event.
   Delivery alone never sets this state.
7. **executing** — the owner explicitly starts work with a public-safe owner
   handle and evidence reference. A touched timestamp or source label
   `running` cannot create this state.
8. **completed** — the same owner supplies terminal result evidence, and for a
   task-backed event the authoritative work source is already
   `completed` or `cancelled`.

An **external-prerequisite** block remains terminal `blocked` and does not
generate owner escalation. This keeps "waiting for an external prerequisite"
separate from "analysis could proceed in an appropriately authorized owner
context".

If an owner execution lease expires, the event returns to `owner_received`.
It is no longer reported as executing. The original receipt remains durable.

## Policy-block worker command

The cron worker must classify an unattended approval/capability denial as:

```sh
python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json \
  finish --claim-token "$CLAIM" --worker-handle "$WORKER" \
  --outcome blocked --block-class policy_capability \
  --evidence-ref "evidence:approval-denial:<id>" \
  --reason "approval_denied_unattended"
```

Do not retry the denied command through another interpreter or shell shape.
Do not change `approvals.cron_mode`, command allowlists, tool approvals or
provider settings to make the unattended worker pass.

For a genuine external prerequisite, use
`--block-class external_prerequisite` (the default).

## One-time supported owner escalation

When local config enables `owner_handoff`, the existing pre-check wrapper calls
`operator_owner_handoff.deliver_once()` before the normal deterministic gate
check. There is no second reasoning cron job and no second health writer.

A due handoff obtains a bounded delivery claim and sends this minimal public-safe
message using the officially supported command:

```sh
hermes send --to <configured-owner-target> --json
```

The message contains only stable event ID, kind, symbolic evidence reference,
permitted next action, and the fact that the item is awaiting owner. It does not
contain ledgers, account state, private snapshots, credentials, local absolute
paths, or the denied command.

Successful delivery records a hashed/symbolic delivery handle and is not retried.
If the process dies during a delivery claim, the lease expires into bounded
backoff. A transport/notification failure uses the configured retry ladder and
eventually marks only the **delivery path** blocked; the underlying work remains
`awaiting_owner`. Repeated failure never marks the work completed.

Default staged limits in the example config:

- delivery claim lease: 120 seconds;
- retry backoff: 5 / 15 / 30 minutes;
- maximum delivery attempts: 4;
- owner execution lease: 30 minutes.

The same stable event ID is used on every retry, so a rare transport ambiguity
(after external send but before local receipt persistence) remains deduplicable
by lifecycle ID. Hermes public `send` documentation does not advertise an
idempotency key guaranteeing exactly-once external delivery.

### Escalation latency and dependencies

For the installed 60-second pre-check, a newly policy-blocked item is eligible
for escalation on the next scheduler tick: normally up to roughly 60 seconds,
plus the messaging transport call (the adapter caps its call at 30 seconds).

This is not an owner-response SLA. Human receipt and execution remain
owner-dependent.

If the host/gateway does not run the cron tick, there is no escalation attempt.
For bot-token platforms Hermes documents that `hermes send` can usually operate
without the gateway, but it still requires the configured profile credentials,
network access and a valid target. Plugin-backed transports may require a live
adapter/gateway.

## Owner receipt, execution, terminal readback

After receiving the escalation, the authorized owner performs explicit readback:

```sh
python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json pending
```

A delivered-but-unacknowledged item remains `awaiting_owner`.

The owner acknowledges receipt:

```sh
python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json \
  owner-receive --event-id "<event-id>" --receipt-ref "owner-receipt:<handle>"
```

Expected readback: status `owner_received`, with no execution object.

The owner starts authorized work only after entering the appropriate interactive
context:

```sh
python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json \
  owner-start --event-id "<event-id>" --owner-handle "owner:<run-handle>" \
  --evidence-ref "evidence:owner-start:<id>"
```

Expected readback: status `executing` and a matching owner handle/evidence ref.
A source task saying `running` without this transition is not execution proof.

When the analysis has a real terminal result, update the authoritative work
source through its normal owner-owned workflow. Then:

```sh
python lab/operator_event_gate.py --config lab/shared/operator_event_gate_config.json \
  owner-complete --event-id "<event-id>" --owner-handle "owner:<run-handle>" \
  --result-ref "evidence:owner-result:<id>"
```

For task-backed work, completion is rejected until the authoritative task is
already terminal. A successful message delivery, worker wake, owner receipt, or
owner start can never substitute for this result.

## Exact staged installation

Operator-owned only; engineering does not execute these live steps.

1. Record the current deployed Issue #22 file hashes, cron job readback and gate
   namespace status.
2. Install accepted `lab/operator_event_gate.py`,
   `lab/operator_owner_handoff.py`, and
   `scripts/paper_operator_event_gate.py` byte-for-byte.
3. Update the existing local excluded
   `lab/shared/operator_event_gate_config.json` with an
   `owner_handoff` object. Keep existing source/runtime/timer paths unchanged.
   Choose only an already configured owner delivery target. Do not add
   credentials to the config.
4. Keep `enabled=false` until the installed source and config parse are
   verified. Then set only `owner_handoff.enabled=true`.
5. Do **not** create a second operator-followthrough job. The existing accepted
   Issue #22 cron job still runs the same wrapper path.
6. Read back the exact existing cron definition and confirm its schedule,
   model/provider, delivery destination, skills/toolsets and workdir did not
   change.
7. Trigger or await a normal tick and verify `wakeAgent=false` still skips the
   agent when there is no actionable gate work or due handoff.

## Portable synthetic acceptance

Hosted CI uses public-safe synthetic data matching the real failure class
(`approval_denied_unattended`) and exercises:

- policy denial -> `awaiting_owner`, not completed;
- external prerequisite -> terminal blocked, no owner escalation;
- one delivery claim at a time;
- delivery success without owner receipt/execution;
- owner receipt without execution;
- execution only with handle + evidence;
- execution interruption/lease expiry;
- notification/interruption retry budget and deduplication;
- terminal result only after authoritative work completion;
- unchanged normal ticks remain zero-model;
- isolated subprocess escalation adapter with synthetic transport.

CI does not send a production message and is not live owner-resumption proof.

## Operator live acceptance

Do not fabricate trading/research work to pass acceptance.

For the next genuine policy/capability-blocked operator analysis:

1. Read `pending` and verify the event is `awaiting_owner`, not
   `completed`, and the original cron worker handle/evidence is retained.
2. On the next scheduler tick, verify the handoff changes from `pending` to
   `delivered` exactly once (or enters bounded retry on real delivery failure).
   Read Hermes delivery/incident evidence separately.
3. Confirm the owner message contains only the minimal public-safe handoff.
4. Before owner acknowledgement, verify there is no execution object.
5. Run `owner-receive`; read back `owner_received` and still no execution.
6. Run `owner-start` from the authorized owner context with a real run handle
   and evidence reference; read back `executing`.
7. Complete the analytical work without changing approvals or trading policy.
   Record the result through the normal authoritative work source.
8. Run `owner-complete`; read back `completed`, matching owner handle and
   result evidence, with no active claim/execution.
9. Verify the existing 120-second health writer, operator-hold state, trading
   files and research/account history were unchanged.

If the platform never supplies an authorized interactive owner, acceptance stops
at `awaiting_owner` or `owner_received`. That is a truthful limitation, not a
failed reason to retry the denied unattended command.

## Rollback

Rollback is source/config only:

1. Set `owner_handoff.enabled=false`.
2. Restore the previously read-back accepted Issue #22 gate/wrapper source if
   necessary.
3. Preserve the gate namespace and all handoff lifecycle evidence; do not delete
   pending owner work to make the rollback appear clean.
4. Do not edit approval policy, create another cron job, restart PAPER, restore an
   account DB, alter storage/risk/source gates, or change research deadlines.

No claim in this guide treats hosted CI, synthetic delivery, or a successful
`hermes send` call as live owner execution, deployment, or research completion.
