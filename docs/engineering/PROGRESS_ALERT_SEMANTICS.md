# PA1 progress alert semantics

## Delivered bytes
Source package: `progress_semantics.py`, `watcher_adapter.py`.
Install replacements: `installed_commit.py`, `installed_commit_watch.py`.
Tests cover Response-only failure parsing, real cron expressions, anchored intervals, Europe/Berlin wall timestamps, missing requirements, coverage gaps, disabled/overdue jobs, weekly not-due, original stale fallback, deterministic acceptance and alert dedupe.

The actual schedule contract is `kind=cron` with five-field expressions such as `0 21 * * *`, `0 20 * * 0`, `30 20 * * *`, or `kind=interval` with 1/2/5/10/15/60/1440 minutes. Intervals use the supplied `last_run_at`/`next_run_at` anchors; they are never rounded to clock hours. Cron output filenames and Run Time are interpreted only with an explicit requirements timezone. This delivery uses `Europe/Berlin` in replay fixtures; missing timezone fails closed.

A no-agent silent record proves execution only. Prompt text containing `[CRON_FAILURE]` is ignored; only the first non-empty Response line can signal that failure. Missing requirements mapping returns truthful unknown at the adapter level. In the installed checker, an unmapped recurring item preserves the original generic stale/dead/due semantics; it is not auto-verified or globally exempted. A mapped `coverage=not_implemented` produces GAP.

## Requirements contract
Profile-relative `commitments/progress_requirements.json`:
`{"schema_version":1,"items":{"C-0001":{"timezone":"Europe/Berlin","coverage":"implemented","required_artifacts":[]}}}`.
This is evidence configuration, not task authority. It must contain only validated requirements. No journal/coverage success is inferred. Absence means original checker semantics remain active.

## Exact install mapping
No engineer-side installation was performed. After independent acceptance, the authorized installer copies accepted bytes as follows while keeping all original state files untouched:

1. Back up `plugins/commitment-gate/commit.py` byte-for-byte to sibling `commit.py.pa1-base`. The replacement refuses to start without it and loads this intentionally non-`.py` backup name with Python's explicit source loader; the backup remains the original checker bytes, never a wrapper/self-copy.
2. Create `plugins/commitment-gate/pa1/progress_semantics/` and copy accepted `__init__.py`, `progress_semantics.py`, and `watcher_adapter.py` there.
3. Copy accepted `installed_commit.py` to `plugins/commitment-gate/commit.py`.
4. Back up `scripts/commit_watch.py` byte-for-byte to `scripts/commit_watch.py.pa1-base`; copy accepted `installed_commit_watch.py` to `scripts/commit_watch.py`.
5. Read back hashes of both replacements, both backups, and the three PA1 sibling dependencies before allowing the unchanged scheduler to invoke them.
6. Read-only verification: invoke the installed `commit.py list`, `attention`, and `check` against the existing profile and verify C ids/history remain intact. Do not use arbitrary acceptance commands as a PA1 readback.

Rollback: restore `commit.py.pa1-base` to `commit.py` and `commit_watch.py.pa1-base` to `commit_watch.py`; remove only the installed `pa1/progress_semantics` directory after hash/readback confirmation. Never roll back or rewrite ledger/jobs/output/requirements state.

The exact filesystem copy/hash commands depend on the operator's existing approved file-copy mechanism; PA1 supplies source/destination mapping rather than pretending installation occurred.

## Authority preservation
`installed_commit.py` loads the byte-for-byte backup and delegates its complete CLI. Only `evaluate` is replaced. The replacement retains deterministic `accept_ok` auto-verification, `touch` history, blockers/defer, dead/finished/due checks and the original `alert_sig`/`alerted` REPEAT_H persistence. Recurring evidence is consulted only before generic STALE_H.

## Remaining system boundaries
PA1 does not repair mechanical exact-chat approval, GitHub delivery watching, old auto_accept/accept_unit/ledger_wake owner-loss selectors, launcher/skills/prompts/dependency orchestration, quota disposition, or unattended closed-loop proof. Their actual Hermes sources are not in this repository.

Smallest additional privacy-safe contracts needed are: exact approval dispatch/receipt function; GitHub delivery watcher entrypoint and receipt schema; auto_accept/accept_unit/ledger_wake selector functions and owner/attempt fields; launcher dependency/handback selector; quota-error classifier plus pending-work persistence/recovery acceptance; and the patrol entrypoint that owns unattended handback-to-next-owner transitions. AP3 SEND_UNKNOWN remains unresolved and is not resent.

PR0 coordinator rejection at JOB_REQUIRED_FIELDS/PR_FORMAT is a separate compatibility boundary and is unchanged.

## r5 measured repair
Exact r4 CI 39045199259/job 117432430545 exposed two integration defects. The installed checker now uses an explicit source loader for the required sibling `commit.py.pa1-base`; missing backup still raises the same install-invalid refusal. Anchored interval absence becomes OVERDUE only after the most recent due slot plus configured grace; absence inside grace is healthy pending execution, while disabled/failed/coverage states remain independent safety/gap classifications.

## r6 serialized evidence identity
Exact r5 CI 39045557067/job 117433447463 ran 42 progress tests and failed six adapter cases because interval anchors retain fractional seconds while the installed writer serializes filenames and Run Time at second precision. The adapter now uses one bounded identity rule: an output belongs to an anchored slot when its job id matches and its timestamp is genuinely at/after the anchor, or when flooring both timestamps to the writer's one-second precision produces the same second. This is not arbitrary slack: a previous-second record is not current, a wrong-job record is not current, and path validation still applies.

For a same-second match, only the comparison timestamp passed to the pure evaluator is normalized to the true fractional anchor; the original parsed timestamp is retained as `source_at` and the source ref is unchanged. Tests cover fractional and exact-second anchors, previous/next-second boundaries, a long-script completion bucket, wrong job/path, missing-after-grace versus within-grace absence, current Response failure versus quoted Prompt text, missing coverage, missing required artifact, explicit timezone, and DST ambiguity. Future evidence and disabled/failed cron states remain fail-closed.
