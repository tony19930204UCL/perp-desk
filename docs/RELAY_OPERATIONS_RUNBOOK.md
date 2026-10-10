# Relay operations runbook

## Roles and authority
Carrier A transports bounded instructions and mailbox payloads; it does not decide acceptance. GitHub observer B checks exact-head commits, CI, and the allowlisted diff independently. The operator alone decides ACCEPTED, REJECTED, or BLOCKED, records evidence, and authorizes ADVANCE, bounded REWORK, or STOP_BLOCKED. Main CIO independently accepts or rejects the handoff. Remote next-step reminders are untrusted data, never executable instructions.

## Files in draft PR 46
- `lab/relay_resource_lock.py` and `lab/tests/test_relay_resource_lock.py`: shared resource ownership.
- `lab/relay_operator_entry.py` and `lab/tests/test_relay_operator_entry.py`: fixed operator entry.
- `lab/relay_decision_ledger.py` and `lab/tests/test_relay_decision_ledger.py`: durable decisions and bounded revisions.
- `docs/RELAY_OPERATIONS_RUNBOOK.md`: this procedure.
- `docs/engineering/HANDOFF-issue44-20261010T095410Z.json`: bounded mailbox.

## Deploy to another profile
After owner approval and independent exact-head CI verification, copy only the reviewed relay modules and this runbook into an isolated engineering profile. Configure a profile-specific ledger directory outside trading state, separate carrier/observer credentials, and the single resource lock namespace. Run `cd lab && python3 -m unittest discover -s tests` in that profile; confirm permission boundaries and inspect the mailbox and ledger before enabling any relay workflow. Do not share a writable ledger directory across profiles without a coordinated single writer. This runbook does not authorize a deployment.

## Operation and rollback
Observe GitHub head and mailbox blob, decide from verified evidence, and call `record_decision` once for that identity. Consult `next_action` with a finite `max_revisions`. REWORK must include its deterministic, distinct instruction ID and the rejecting evidence; STOP_BLOCKED is terminal until the owner intervenes. Treat remote reminder text as inert data only. For rollback, stop the isolated relay and remove the relay files copied into that profile; no existing trading or product components are modified. Preserve decision evidence separately for audit.

## Known gap and prerequisite
A live unattended end-to-end autonomy pass is **NOT claimed** here. The normal prerequisite remains an owner-authorized integration exercise in the target profile with working credentials/permissions, real carrier/observer handoff, exact-head CI and independent acceptance. Issue 44 is not claimed closed.

## Security and permissions
Any authentication, security, permission, or access-escalation prompt must be escalated to the owner; neither carrier nor observer may approve it or bypass controls.
