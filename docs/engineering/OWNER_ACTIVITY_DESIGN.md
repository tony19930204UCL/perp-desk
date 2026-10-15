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
