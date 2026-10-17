# Progress alert semantics (PA1)

Pure stdlib source: `progress_semantics.py` plus `watcher_adapter.py`. Public imports are `evaluate`, `parse_output`, `assess_item`, and `assess_profile`.

`parse_output(text)` considers `[CRON_FAILURE]` a failure only when it is the first non-empty line of the Response section. Prompt quotations do not count. `[SILENT]` is successful execution evidence.

`evaluate(item, now, schedule, evidence, state, cooldown_hours=6)` returns state, reason_code, evidence_ref, evidence_age_hours, alert, suppressed_by_cooldown and suggested_kind. Reclassification remains advisory.

`assess_profile(home, item_id, now, requirements=None)` reads only `commitments/ledger.json`, `cron/jobs.json`, and `cron/output/<job_id>/*.md` under the explicit profile home. It never writes the ledger, runs subprocesses, or dispatches commands. `assess_item` accepts already-read records for isolated integration.

The installed watcher contract keeps `scripts/commit_watch.py` as the scheduled no-agent entrypoint and keeps the existing commitment gate `commit.py check` as authority for deterministic auto-verification, history/touch behavior, and persisted `alert_sig`/`alerted` dedupe. The adapter is called by that existing check only for recurring cron progress classification; it does not replace one-shot acceptance or mutation logic.

Cron job schedules supported by the adapter: interval minutes, daily hour/minute, weekly weekday/hour/minute, with an explicit timezone supplied by integration. Disabled/paused/missing jobs fail closed. A no-agent silent output proves only that execution occurred. If a recurring item has no supplied coverage/artifact requirement, assessment fails closed with missing evidence rather than treating rc0 as product progress. Known `coverage=not_implemented` returns GAP.

## Install bytes and mapping

Install the accepted bytes of these sibling files together: `engineering/progress_semantics/__init__.py`, `progress_semantics.py`, and `watcher_adapter.py`. Their hashes must be recorded by the installer before activation. The existing private `commit.py check` needs one call seam: for a nonterminal DISPATCHED cron item, before its generic `now-updated > STALE_H` problem is produced, call `assess_profile(profile_home, item["id"], aware_now, requirements_for_item)`. Keep existing `accept_ok` auto-verification before this seam and keep existing `alert_sig`/`alerted` suppression/persistence after it. Do not refresh `updated` merely because cron ran.

The only still-unavailable concrete field is **`requirements_for_item` mapping/source**: per recurring item, timezone and any required artifact names/coverage state. The supplied profile contract contains no journal/coverage reader or mapping. Therefore missing mapping must fail closed; C0003 can be explicitly configured `coverage=not_implemented` by the owner only from authoritative configuration/evidence.

Rollback restores the backed-up `commit.py` and removes the three installed PA1 sibling files if they were newly installed. Ledger/jobs/output files are never rolled back or rewritten by PA1.

CI discovers `python3 -m unittest discover -s engineering/progress_semantics/tests -v`.
