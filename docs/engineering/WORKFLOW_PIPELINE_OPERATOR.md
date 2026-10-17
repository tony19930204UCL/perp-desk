# Operator contract
Undeployed engineering candidate only.

## Existing exported entrypoints
Source evidence from PR65: `python3 engineering/manual_lifecycle/commit.py --ledger ABS add TEXT ...`, `done ID --evidence`, `block ID --reason`, `drop ID --reason`; `ledger_wake.py` is a read-only wake gate. These files are not copied into this branch and PR65 is not declared accepted.

## Candidate read-only CLI
`python3 -m engineering.workflow_pipeline.cli --ledger ABS status`
`python3 -m engineering.workflow_pipeline.cli --ledger ABS one-tick`
`start --plan`, `stop --plan`, `install --plan`, and `rollback --plan` emit PLAN_ONLY receipts and perform no operation. This is deliberate: no real scheduler/PID/install target is available or authorized in this handoff.

## Install mapping and rollback
An authorized installer must back up the local watcher source and alert-state file, adapt its existing ledger read to call `progress_semantics.evaluate` with real schedule/output/journal/state evidence, then read back representative standing and one-shot items. Rollback restores those exact backups and removes only the adapter import. Do not replace the commitment ledger or rewrite C ids/history.

The watcher chain is now known: `scripts/commit_watch.py` invokes the existing `plugins/commitment-gate/commit.py check`. PA1 supplies installable adapter bytes and the exact call seam in `PROGRESS_ALERT_SEMANTICS.md`. The per-item `requirements_for_item` mapping (timezone/artifact/coverage requirements) remains unavailable, so installation must fail closed for unmapped recurring items rather than guess coverage. `start/stop/install/rollback --plan` remain PLAN_ONLY and are not receipts of installation.

## Public-safe acceptance
Verify exact branch head; run focused progress and workflow tests; confirm CI executes both; inspect no live/trading/config data changed; independently test private watcher adapter; install only after authorization; read back; retain rollback backups. `unattended_proven` requires a genuine no-new-user-message interval with handback -> independent acceptance/revise -> authorized readback/next owner plus paper-fault disposition. This candidate cannot self-certify it.

Remaining obligations C0053, C0071, C0079/C0080, C0093, C0112, C0122/C0136, C0168, C0178, C0169 and C0180 remain external ledger obligations and are not completed or modified here.
