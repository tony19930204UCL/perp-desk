# OA1-r0 — Owner activity Monitor design and engineering evidence

Status: engineering implementation on draft PR #63, NOT deployed, NOT accepted visually.

## Read sources and base
- Read in full: `docs/engineering/OWNER_ACTIVITY_DASHBOARD_BRIEF.md` blob `3ae41757ab31a4c9ab484a638cb085102d5c505f`.
- Read in full: `docs/engineering/OWNER_ACTIVITY_SOURCE_CONTRACT.md` blob `99e33bcf82fe66ecc24041e4732f26a130d6b25f`.
- Inspected actual `dashboard_v2_server.py`, `dashboard_v2_reader.py`, `dashboard_v2_model.py`, `dashboard_v2.html`, `dashboard_v2_ui.js`, `dashboard.py`, `work_status.py`, `automation/review_sync.py` metadata, existing dashboard tests, `.github/workflows/ci.yml`, and PR #63 HEAD.
- Base PR58 commit `ab36919cd159c97e7666bc11ebb114bd836b9487`.
- Public interface references: no official public design-system or accessibility page was successfully inspected/verified for implementation; this design is grounded in existing repo code and specification, not a claim of external user research.

## Architecture candidates
1. **Roadmap-first:** high-level stage view with task cards below. Good for quarterly planning; obscures what is truly executing now.
2. **Chronology-first:** a stream of activities and proofs. Good for incident archaeology, but scheduled idle and blocked dependencies become hard to discover.
3. **Activity-first split pane (selected):** compact epistemic overview, task list/search/status/stage, persistent detail, then stage/dependency and sanitized output lists. Separates worker execution proof from idle cron and makes inconsistencies visible; integrates as a route within existing observer UI, preserving account panels and C-0112.

Responsive layout: 1480px max, 4 KPI cards; split list/detail above 850px; stacked columns on tablet/mobile; 2 KPI columns under 850px. Text is never passed to HTML parsing. Keyboard-readable buttons/labels and aria-live status. No controls can mutate work, trade, approve or deploy.

## Source mapping
| UI element | Authority | Rule / limitation |
|---|---|---|
| Commitment, why, acceptance, due, deferral, recheck, history | `commitments/ledger.json` | allowlisted strings only; raw errors, handles, paths never exposed |
| Stage, roadmap next, dependency chain | `lab/journal/roadmap.json` | roadmap goal and ledger joins; unmatched C-IDs = unclassified; exit gates and targets unsupported |
| Step, started, updated, work claim | `lab/shared/work_status.json` | running/test/verifying alone = activity_unconfirmed |
| Verified worker | ledger handle + injected execution proof | strict PID + start_identity + alive + recent observed_at + recent work progress; otherwise unconfirmed |
| Scheduled idle, missed, failed, result proven | `cron/jobs.json` and injected result proof | missed only when recorded next_run_at overdue and last_run_at before slot; no arbitrary 72h cutoff; cron result ref requires status+run identity |
| Event list | `lab/journal/entries.jsonl` | bounded tail; public_summary and symbolic proof_ref only; no unreviewed body |
| Source health | each bounded source | missing, invalid, stale, future-clock conflict visible |
| Engineering acceptance | ledger VERIFIED | not deployment; deployment and unattended closure remain unconfirmed |
| Totals | source arrays and union of joined task IDs | preserve all ledger IDs including ones unmapped to stages; no invented progress percent |
| Source time | ledger/work/cron/roadmap declared fields | no verification from bookkeeping timestamp; unsupported displayed as unconfirmed |
| Stage current | roadmap current_stage | absent = unconfirmed |

## Safety boundaries
One injected absolute trusted profile root, fixed relative filenames; directory-by-directory `O_NOFOLLOW` and regular-file checks, size caps, bounded journal tail. No API-selected paths. GET/HEAD exact-route only; single-thread loopback, no CORS, no mutating verbs, Host/Origin/Sec-Fetch-Site restrictions and generic errors. Note that static HTML can be viewed without configured root; the API returns 404 until configured. Never connect to another profile.

## Interface launch
Use existing v2 CLI with original required flags plus `--profile-root /ABSOLUTE/TRUSTED_PROFILE_ROOT`:
`python3 lab/dashboard_v2_server.py --scorecard /ABSOLUTE/SCORECARD --journal /ABSOLUTE/JOURNAL --ledger /ABSOLUTE/LEDGER --profile-root /ABSOLUTE/TRUSTED_PROFILE_ROOT --port 8766`
Then locally open `http://127.0.0.1:8766/owner_activity.html`. This is a launch instruction, **not evidence a service was launched or deployed**. Without profile-root, Monitor API remains disabled. The existing account view remains `/dashboard_v2.html`.

## Test evidence and limitations
The checked-in synthetic fixture contains C-0026 daily and C-0028 weekly examples, no real private records. Test files cover deterministic model, source missing/corrupt/symlink/oversize, loopback route, Host, Origin, mutation rejection, static markup and JS DOM behavior. Node harness exercises JS behavior; it is **not a Chromium desktop/mobile screenshot** and is not presented as one. Real browser screenshots, desktop/mobile accessibility assessment and dynamic refresh screenshots remain outstanding until a browser-equipped acceptance run.

Expected CI commands: `python3 -m unittest discover -s automation/tests -v` from repo root; `cd lab && python3 -m unittest discover -s tests -v`. GitHub push and pull_request workflows check these at individual heads. Only mark green after observing exact-head workflow conclusions.

Remaining work: independently verify exact-head CI, browser screenshot review at desktop/mobile sizes, inspect source semantics for real local scheduler expression timezone, verify source totals against sanitized live adapter, visually approve, and optionally deploy outside this engineering authorization. Never claim live trading, unattended closure, or runtime activation.

## OA1-r1 source-shape correction (2026-10-15)
The original r0 adapter assumed incorrect source shapes. The actual inspected local schema, represented **only by synthetic fixtures**, is:
- `ledger.items[].blocked_by` is a typed string `item:ID`, `cmd:...`, `file:...` or `user:...`. Only `item` carries a safe C-ID dependency. Other types display their type and withheld private value; no command, file or question is executed or opened. Resolved item prerequisites require a matching ledger item with VERIFIED state; absent/nonverified entries remain unknown/unresolved.
- `defer` is a string reason (conservatively redacted when it looks private); `due` is the re-evaluation time when deferral is present. `created`, `updated`, `due` and history array timestamps are epoch seconds; normalization rejects booleans, nonfinite, invalid and out-of-range dates. `history` entries use `[epoch_seconds,note]` and suppress private-looking note content.
- `handle` uses `{kind:'cron',ref:job_id}`, `{kind:'pid',pid,start,ref}`, or `{kind:'manual',ref}`. Cron joins by `kind/ref` to one existing C-ID, not a duplicate JOB row. A PID is never considered live without independent matching pid/start/alive/fresh proof **and** recent work progress. A DISPATCHED executor without proof is activity_unconfirmed. A disabled cron remains disabled, and unknown proof suppliers are explicitly unsupported in production.
- Source freshness uses declared `cron.updated_at`, `roadmap.updated` (including `YYYY-MM-DD HH:MM UTC`), ledger item bookkeeping updated, work item updated_at, and a public summary journal event time. Missing/unparseable source timestamps display `unverifiable`; stale/future produce separate states. Ledger bookkeeping timestamp is **not** a worker heartbeat. Weekly not-yet-due is not declared missed using generic 72-hour aging.
- JS detail is rebound to each new model. If the selected ID is removed entirely, the stale detail is cleared and an unavailable message shown; when only hidden by a filter but still in the current model, detail remains current.
- Limits: a single read obtains separate bounded source snapshots, **not** an atomic multi-file transaction. If writers change files mid-read, cross-file version consistency cannot be proven; no cross-profile reads or private runtime evidence supplied. Production proof suppliers are not wired, so none is invented. Original v2 root route `/` continues to serve HTML. Frozen test `test_dashboard_v2_server.py` expects 404 for `/` while `test_dashboard_v2_serving.py` expects HTML 200. This is an independently reproduced, pre-existing out-of-allowlist test conflict and must not be concealed or resolved through unauthorized test changes.

OA1-r1 synthetic regression scenarios: typed blocker variants, verified/nonverified prerequisite, deferral overdue and history arrays, actual handle shapes with 11 cron joins, numeric/bookkeeping freshness, future/invalid source timestamps, DISPATCHED without proof and refresh after deletion. No local private runtime records uploaded. Independent earlier Chromium assessment at r0: 15/16 checks passed, one stale detail failure; r1 browser screenshot assessment must be independently rerun. No deployment or acceptance claim.
