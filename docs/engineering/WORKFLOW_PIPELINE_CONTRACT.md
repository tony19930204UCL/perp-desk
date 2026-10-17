# Workflow pipeline contract
The existing commitment ledger is the sole task authority. This package is a reconstructible projection and append-only event contract; it never creates a second task database.

Observed exported source: PR65 branch `engineering/manual_lifecycle/commit.py` defines an object with `items`, C ids, OPEN/DISPATCHED/VERIFIED/BLOCKED/DROPPED states and pid/cron/manual handles. `ledger_wake.py` reads that ledger and emits START/READY/VERIFY/REEVALUATE observations. PR65 is evidence only and is not treated as accepted.

`normalize_item(item, meta)` preserves task identity and projects root/parent/kind/goal/acceptance/priority/source/dependencies/owner/executor/attempt/instruction/repo/branch/head/evidence/next action/progress/blocker/retry/outcome. Unknown fields are explicit N/A.

`transition(...)` returns an append-only event. `acquire_lease(...)` permits one unexpired holder per task/attempt. `reconcile(...)` makes UNKNOWN send read-only, quota loss a bounded availability wait, handback an acceptance event, CI pending a wait, and dead workers repair candidates. Diagnostic completion is not product completion.

Progress semantics require caller-provided source evidence. Cron rc/output alone cannot satisfy a declared missing coverage contract. Required artifacts need nonempty content, rc=0 and current-slot timestamp. Safety failures are not cooldown-suppressed.

Private watcher source is not present in this repository/branch. Required read-only integration contract is: watcher entrypoint path, its item-to-schedule mapping, cron table/output reader, journal/state-file readers, and alert-state persistence schema. Until those are supplied, private watcher adapter integration remains BLOCKED.
