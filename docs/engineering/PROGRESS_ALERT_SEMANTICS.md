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


## r8 installed import API compatibility

The r6 replacement delegated the original CLI through `base.main()` but did not expose the original module API to existing `import commit as c` consumers. That would leave unchanged launchers unable to resolve original names such as `Ledger`, `DISPATCHED`, and `handle_status` even though those names remained present on the byte-for-byte `commit.py.pa1-base`.

r8 adds only module-level missing-attribute delegation to that loaded original module. PA1's explicit wrapper names, including its evidence-aware `evaluate`, remain authoritative in the wrapper; `base.evaluate` still points to that same evaluator before the unchanged original CLI is invoked. No original Ledger/constants/helpers are copied or reimplemented, so lock ownership, acceptance/history/dedupe and the original namespace remain sourced from the backup. Missing `commit.py.pa1-base` still refuses installation before delegation is available.

The existing install mapping and rollback are unchanged. After independent exact-head acceptance, install the complete accepted `installed_commit.py` replacement at `plugins/commitment-gate/commit.py` alongside the byte-identical original `commit.py.pa1-base` and existing accepted PA1 siblings, then read back the replacement/backup hashes. Import readback must additionally confirm legacy `commit.Ledger`, `commit.DISPATCHED`, `commit.handle_status`, `commit.OPEN`, `commit.VERIFIED`, `commit.main`, `commit.touch`, and `commit.home` resolve to the original module objects while `commit.base.evaluate is commit.evaluate`. No live installation is claimed here.

The r7 launcher integration remains separately BLOCKED by the recorded safety denial. r8 neither retries nor supplies `installed_auto_accept.py` or any other r7 launcher candidate, and it does not establish whole-workflow or unattended acceptance.


## r9 deployed evidence integration

r9 repairs two demonstrated defects in the existing shared watcher seam; it does not add a launcher or parallel evidence CLI.

### Bounded cron-output identity

`assess_profile` still reads only `cron/output/<job-id>/*.md`, but each record is now parsed independently. The path-selected job id must agree with an optional serialized `**Job ID:**`. Filename wall time remains timezone-validated and DST ambiguity fails closed. The writer may independently serialize filename and `**Run Time:**` one second apart; only an absolute difference of at most one second is accepted. Both raw times, the path, and a SHA-256 of the original text are retained. Larger skew and body-job disagreement are malformed evidence.

A malformed historical record no longer aborts assessment of a newer due slot. A malformed record at/after the current due slot is `MALFORMED_CURRENT_OUTPUT`. Conflicting different records in the same current serialized second are `CONFLICTING_CURRENT_OUTPUT`. Current Response `[CRON_FAILURE]` remains a failure; a quoted Prompt marker remains inert. No-agent silent records retain the existing silent-response normalization. The r6 fractional-anchor same-second rule remains bounded to persisted second identity.

### Dynamic artifact/journal resolver

`assess_profile` re-reads configured evidence on every invocation, including invocations reached through installed `commit.py list/attention/check` and the installed watcher. `required_artifacts` names do not prove anything by themselves. Each required name may have an `artifact_sources.<name>` object with:

- `path`: safe profile-relative file path. Absolute paths, traversal outside the profile, symlink targets, missing files, and non-files are rejected.
- `format`: `jsonl` or `json`.
- `time_field`: dotted field selector. For the real append-only journal serialization use `time_format: "journal_utc"` for values like `2026-10-16 19:11 UTC`.
- one or more explicit binding selectors: `item_field`, `job_field`, or `slot_field`. A title, timestamp, or comma-separated `refs` mention is not inferred as a binding.
- optional `status_field` plus explicit `success_values`; when configured, nonmatching/nonzero-like results are not success.
- optional `content_field` plus `required_text` for content coverage.

Resolved evidence must be at/after the current due slot and not in the future. Exactly one qualifying candidate is required for a required artifact. Missing, malformed, stale, unbound, conflicting, failed, or content-incomplete evidence remains `MISSING_ARTIFACT`/UNKNOWN rather than success; future evidence remains SAFETY. The resolver never treats file existence or mtime alone as coverage.

The production journal shape `lab/journal/entries.jsonl` has `id`, `at`, `kind`, `title`, optional string body/data/result/verdict/next/refs, and optional script/script_sha. It has no default structured item/job/run/rc binding, and `refs` is a comma-separated string. Therefore existing entries such as J-0015 or J-0433..J-0437 are not automatically promoted to current coverage. Synthetic tests add explicit binding fields solely to prove the configurable resolver contract.

Operational consequences remain conservative: C0003 `coverage=not_implemented` remains GAP; C0026 `durable_scorecard_alert_checked_result` remains missing until a real producer emits evidence matching an explicitly configured binding contract; timezone-only C0027/C0028/C0041/C0062/C0069/C0095 remain `REQUIREMENTS_UNKNOWN`. Paused paper research is not reclassified as healthy progress.

### Quota-held interval scheduler shape

For an interval job whose `last_status=error` and explicit `quota_hold_until` moves `next_run_at` away from the nominal cadence, the adapter recognizes `quota_held` rather than reporting `INVALID_SCHEDULE`. It does not rewrite cadence, mutate the hold, or infer current health: absent current output returns `QUOTA_HELD_UNKNOWN`. Actual current output, including Response failure, is still evaluated and is not masked by the hold. Historical failure is preserved as source history; current availability remains unknown without current evidence.

### Live entrypoint and acceptance mapping

The source path is `engineering/progress_semantics/watcher_adapter.py` plus the pure checks in `progress_semantics.py`. After independent acceptance, those complete bytes map to the already documented installed sibling `plugins/commitment-gate/pa1/progress_semantics/watcher_adapter.py` and `progress_semantics.py`; the accepted r8 `plugins/commitment-gate/commit.py` wrapper remains unchanged and calls this same `assess_profile` seam. Existing operator read-only acceptance commands remain the installed `commit.py list`, `commit.py attention`, and `commit.py check`, plus the existing installed watcher check path. No r9 installation is claimed here.

r7 remains separately BLOCKED by its recorded OpenAI safety denial. r9 does not create, retry, rename, split, or emulate `engineering/workflow_pipeline/installed_auto_accept.py`, and does not establish full-workflow C0376 or unattended completion.


## r10 scheduler fixture correction

Main's independent r9 CI run 39055385989 executed 63 progress-semantics tests and had exactly one failure: `test_latest_scheduler_error_applies_only_to_matching_run` expected `SAFETY/CRON_FAILED` but received `SAFETY/INVALID_SCHEDULE`. The exporter step passed; later workflow/PAPER/security baselines were skipped, so r9 was not accepted.

The failure was an impossible synthetic fixture, not a reason to change production precedence. The shared `interval()` helper represents a 60-minute schedule with `last_run_at=12:10:33.375004` and `next_run_at=13:10:33.375004`. The r9 test overrode only `last_run_at` to `13:10:33.375004`, accidentally leaving `next_run_at` equal to the new last anchor. The production contract deliberately rejects `next_run_at <= last_run_at` before binding output/status, so `INVALID_SCHEDULE` was correct.

r10 changes no scheduler production code and widens no tolerance. The matching-current-failure fixture now uses the valid pair `last_run_at=13:10:33.375004`, `next_run_at=14:10:33.375004`; a current output in the serialized 13:10:33 second matches the persisted last-run identity, so a non-success `last_status` must yield `SAFETY/CRON_FAILED`. Independent regressions retain the opposite boundaries: equal last/next anchors are genuinely invalid and must remain `SAFETY/INVALID_SCHEDULE`; a scheduler error attached to persisted `last_run_at=12:10:33.375004` must not contaminate an independently present successful 13:10:33 output. The quota-held exception remains separate: an explicit error plus `quota_hold_until` may explain a deliberately displaced next anchor, but absent current evidence is `QUOTA_HELD_UNKNOWN`, not health, and matching current failure is never masked.

All r9 output identity, bounded one-second writer skew, raw provenance/hash, malformed/current conflict handling, dynamic artifact resolution, explicit binding, path confinement, freshness/result/content checks, multiple-candidate negatives, and r8 public API delegation remain unchanged.

### r10 candidate/deployed acceptance mapping

Candidate source remains `engineering/progress_semantics/watcher_adapter.py` and `engineering/progress_semantics/progress_semantics.py`; r10 does not alter those source bytes. After independent acceptance, their complete accepted bytes map to `plugins/commitment-gate/pa1/progress_semantics/watcher_adapter.py` and `plugins/commitment-gate/pa1/progress_semantics/progress_semantics.py`. The already accepted r8 wrapper maps to `plugins/commitment-gate/commit.py` and is unchanged by r10. Existing candidate test command is `python3 -m unittest discover -s engineering/progress_semantics/tests -v`. Existing deployed read-only acceptance entrypoints remain `plugins/commitment-gate/commit.py list`, `plugins/commitment-gate/commit.py attention`, `plugins/commitment-gate/commit.py check`, and the existing installed watcher check path. No installation or deployed success is claimed by r10.


## r11 formal result reason-code correction

Main's independent exact-head r10 CI run 39055938357 executed 65 progress-semantics tests and had exactly one failure: `test_historical_scheduler_error_does_not_contaminate_current_success` expected `(OK, SUCCESS)` but the formal evaluator returned `(OK, EVIDENCE_CURRENT)`. This is a test-layer contract error, not a runtime defect.

The formal contracts are intentionally distinct. The Response parser may classify a successful non-silent response internally as `SUCCESS`; after evidence selection and freshness checks, `evaluate()` returns the public result reason `EVIDENCE_CURRENT` for accepted current evidence. The historical-error isolation regression exercises the latter result object, so r11 changes only its expected reason code from `SUCCESS` to `EVIDENCE_CURRENT`. Its state assertion and the historical scheduler-error isolation scenario remain unchanged.

No production source changes are made in r11. In particular, r10's valid matching-current scheduler failure remains `SAFETY/CRON_FAILED`, genuinely invalid anchors remain `SAFETY/INVALID_SCHEDULE`, historical scheduler status remains scoped to its matching persisted run, and quota-held intervals remain a separate conservative identity. All r9 bounded one-second writer identity/raw provenance, malformed/conflicting-current behavior, dynamic JSON/JSONL resolver, explicit item/job/slot binding, freshness/result/content requirements, path traversal/symlink confinement, multiple-candidate negatives, and r8 public API delegation remain unchanged.

The deployment mapping is also unchanged: accepted `engineering/progress_semantics/watcher_adapter.py` maps to `plugins/commitment-gate/pa1/progress_semantics/watcher_adapter.py`; accepted `engineering/progress_semantics/progress_semantics.py` maps to `plugins/commitment-gate/pa1/progress_semantics/progress_semantics.py`; the accepted r8 wrapper remains `plugins/commitment-gate/commit.py`. Candidate acceptance remains `python3 -m unittest discover -s engineering/progress_semantics/tests -v`. Existing deployed read-only acceptance remains `plugins/commitment-gate/commit.py list`, `plugins/commitment-gate/commit.py attention`, `plugins/commitment-gate/commit.py check`, plus the existing installed watcher check path. r11 is a candidate only: NOT_INSTALLED and UNATTENDED_NOT_PROVEN.


## r12 workflow ownership and patrol root-cause candidate

r12 is a new candidate built on the operator-seeded installed baselines. It does not retry, recreate, rename, split, or emulate the separately denied r7 launcher operation.

### Locked ledger compare-and-set

The installed base CLI keeps the existing \`Ledger\` lock and adds optional \`--expect-state\` to \`done\`, \`block\`, and \`drop\`. Allowed values are the real states \`OPEN\`, \`DISPATCHED\`, \`VERIFIED\`, \`BLOCKED\`, and \`DROPPED\`; argparse rejects unknown names. State comparison occurs after the ledger has been read under its existing exclusive file lock and before mutation. A mismatch prints id/current state/handle status/last history note and exits with \`CAS_REFUSED=3\`. Raising inside the lock context prevents \`Ledger.__exit__\` from rewriting the file. Ordinary unknown id remains the baseline error.

Independently of the flag, \`block\` refuses a \`VERIFIED\` item with code 3. \`done\` on \`VERIFIED\` refuses different evidence; identical evidence returns success after printing current state but deliberately raises before context exit, so it does not rewrite evidence/history or the ledger bytes. Non-terminal transitions without the flag retain baseline behavior.

### Deterministic NEEDS_SUCCESSOR queue

The accepted wrapper candidate exposes \`scan_successors()\` and runs it after existing \`commit.py check\` and \`attention\`. The scan takes a shared lock on the same \`ledger.json.lock\`, reads but never mutates the ledger, and atomically replaces only \`commitments/needs_successor.json\`:

\`{"schema_version":1,"items":[{"id":"C-0001","kind":"pid|manual|blocked","evidence":"...","first_seen":<epoch>,"last_seen":<epoch>}]}\`.

Entries exist only for: a DISPATCHED pid whose existing \`handle_status\` is \`dead\` (not \`finished\`); a DISPATCHED \`manual:*\` handle older than the configurable \`COMMIT_MANUAL_LEASE_S\` default six hours without a fresh heartbeat; or a BLOCKED item lacking a checkable \`item:/file:/user:/cmd:\` blocker. \`updated\` is never treated as heartbeat. Manual heartbeat is an executor-owned read-only input at \`commitments/heartbeats/<handle-ref>.json\` with \`{"item_id":"C-0001","handle_ref":"sa-name","at":<epoch>}\`; it is fresh only when its item/ref match and its timestamp is no more than the lease old and not materially future. Queue \`first_seen\` is preserved while an entry remains; recovered/closed items disappear on the next scan.

Unreadable/corrupt ledger raises a scan error, leaves the existing queue untouched, and makes the invoking check/attention return nonzero. The queue is surfaced as \`NEEDS_SUCCESSOR ...\` lines. The existing \`commit_watch.py\` needs no behavior change because it already invokes installed \`commit.py check\` and prints that output. This is ownership triage only: it never closes, blocks, drops, restates, refreshes, or dispatches an item.

### Owner-pause patrol marker

The patrol reads optional \`lab/journal/paused_units.json\`:

\`{"schema_version":1,"units":{"t1-btcusdt":{"reason":"owner pause","authorized_by":"operator","authorized_at":"2026-10-17T12:00:00+02:00","review_by":"2026-10-18T12:00:00+02:00"}}}\`.

All four strings are required and both timestamps must be ISO-8601 with offsets. Unknown unit names produce \`PAUSE_UNKNOWN_UNIT\`. Wrong schema/malformed records produce \`PAUSE_MARKER_INVALID\` and suppress nothing. An expired \`review_by\` produces \`PAUSE_EXPIRED\` and the unit is treated exactly as unpaused.

For a valid unexpired pause on an inactive unit, only the baseline inactive \`SERVICE\` and stale-file \`STALE_STATUS\` findings are suppressed. POLL_ERRORS, WRITE_RATE, DISK_FREE, MACHINE_GAP, FINDING and repeat suppression remain on their baseline paths. A marked unit that is actually active emits \`PAUSE_UNIT_ACTIVE\` and all normal checks, including SERVICE restart, STALE_STATUS and POLL_ERRORS, apply.

Exposure is fail-closed and read-only. Patrol opens \`lab/data/paper-v4/runtime.sqlite3\` for h2 or \`lab/data/paper-<unit>/runtime.sqlite3\` otherwise in SQLite read-only mode, reads \`state(id=1,payload)\`, recursively recognizes serialized \`position\`, \`positions\`, and \`open_orders\` fields, and reports \`PAUSE_OPEN_EXPOSURE\` if a recognized field is nonzero/nonempty. It reports \`PAUSE_EXPOSURE_UNKNOWN\` for missing/unreadable SQLite, absent state row, malformed JSON, unrecognized state shape, or an unclassifiable recognized value. It never assumes an unfamiliar runtime is flat.

When the marker does not exist, the original systemctl/status/poll/write/disk/heartbeat and patrol-state paths are unchanged; no service or systemd state is written.

### r12 source-to-installed mapping and copy replay

Candidate mapping after independent acceptance only:

- \`engineering/progress_semantics/installed_commit.py\` -> \`plugins/commitment-gate/commit.py\`
- \`engineering/progress_semantics/installed_commit_base.py\` -> \`plugins/commitment-gate/commit.py.pa1-base\`
- \`engineering/progress_semantics/installed_commit_watch.py\` -> \`scripts/commit_watch.py\`
- \`engineering/progress_semantics/installed_desk_patrol.py\` -> \`scripts/desk_patrol.py\`
- \`engineering/progress_semantics/{__init__.py,progress_semantics.py,watcher_adapter.py}\` -> \`plugins/commitment-gate/pa1/progress_semantics/\`

Main's copy replay should use a temporary \`HERMES_HOME\`, reproduce that exact layout, and use only temporary ledger/runtime/status files. Existing candidate test command remains \`python3 -m unittest discover -s engineering/progress_semantics/tests -v\`. Deployed-layout replay commands are:

\`HERMES_HOME=<tmp> python3 <tmp>/plugins/commitment-gate/commit.py --ledger <tmp>/commitments/test-ledger.json done C-0001 --expect-state DISPATCHED --evidence proof\`

\`HERMES_HOME=<tmp> python3 <tmp>/plugins/commitment-gate/commit.py --ledger <tmp>/commitments/test-ledger.json block C-0001 --expect-state DISPATCHED --reason synthetic\`

\`HERMES_HOME=<tmp> python3 <tmp>/plugins/commitment-gate/commit.py --ledger <tmp>/commitments/test-ledger.json drop C-0001 --expect-state BLOCKED --reason synthetic\`

\`HERMES_HOME=<tmp> python3 <tmp>/plugins/commitment-gate/commit.py check\`

\`HERMES_HOME=<tmp> python3 <tmp>/plugins/commitment-gate/commit.py attention\`

\`HERMES_HOME=<tmp> python3 <tmp>/scripts/commit_watch.py\`

\`PATH=<fake-systemctl-dir>:$PATH HERMES_HOME=<tmp> python3 <tmp>/scripts/desk_patrol.py\`

Run the last command once with no \`paused_units.json\`, then with a synthetic valid marker and synthetic read-only runtime SQLite fixture. The replay must also provide the stub \`scripts/heartbeat_gate.py\` used by tests. These are candidate/deployed-layout verification commands, not evidence that r12 was installed or run.

r12 does not change the 15-second/tick gate, cost/risk checks, scheduler quota state, PAPER services, provider/configuration, or the denied r7 launcher boundary. C0376 remains UNATTENDED_NOT_PROVEN.
