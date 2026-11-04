# BOSS-18769 deployment and rollback specification — NOT EXECUTED

**Status: source-only proposal.** Parent/Main CIO alone performs deployment after independent acceptance. Do not modify the existing trading/runtime supervisor, account, risk thresholds, credentials or watcher. Preserve accepted release `183604e2cfd45b5020955bd02ab4485bdda6986f`.

## Candidate manifest (Git blob SHA-1, not file SHA-256)

These are independently read from the current PR88 candidate; SHA-256 must be computed and recorded **locally by the parent** against the exact downloaded release before any cutover.

| Relative file | Git blob SHA-1 |
|---|---|
| lab/boss.html | ba8d27fcb2b9299646a1a8b87f21c7aac999b0cc |
| lab/boss.js | 3db6f661705f1e7d74a0e53a2b6e2db8a95c315d |
| lab/boss.css | 892499628b19583737fa778abd07606276e8854d |
| lab/boss_readonly.py | 076642cf71f03d3a81a0cf8c84123a99d04b16a6 |
| lab/dashboard_v2_server.py | 06517ba13f9e1a15361754ae8f0161f01fa64180 |
| lab/tests/test_boss_data.py | 95860330bb5c1555328cfbd13f94231a6d542a09 |
| lab/tests/test_boss_ui.py | 07c41154743856f3f359939d56b13209b12ce370 |
| lab/tests/fixtures/boss_synthetic.json | 9d60a41cddaf5794433c339fc1c46ca66ac83e19 |

Additional release dependencies **must remain the existing accepted files**: `lab/dashboard_v2_reader.py`, `lab/owner_activity.py`, `lab/owner_metro_model.py`, `lab/desk_readonly.py` and its dependencies, `lab/owner_activity.html`, `lab/owner_activity.js`, `lab/dashboard_v2.html`, `lab/dashboard_v2_ui.js`, `lab/owner_cards.js`, and any other unchanged original route asset. The seeded server additionally names `lab/desk_home.js` and `lab/owner_cards.js`, absent from this branch. The seeded optional `desk_readonly.py` is also absent; its API now fails closed with 404. These paths are outside this task's allowed writes and must be explicitly reconciled by the parent before claiming full old-route acceptance. This list is not evidence that the files exist in the current seed; preflight must fail closed if any required route asset/import is absent. No unverified hash is claimed for untouched dependencies.

## Parent-only preflight

1. Record exact PR88 head, branch, CI run IDs and digest of every materialized file. Inspect full `git diff 183604e2..HEAD` to distinguish inherited changes from this job. Test on a fresh checkout first.
2. Use a **new fixed immutable release directory**, e.g. `/operator-owned/releases/boss-18769-<accepted-full-sha>/lab`, owned by the operator (not a live account/data path). Preserve existing live release directory unchanged; copy it to a distinct immutable backup and capture its complete manifest and SHA-256. Never overwrite state, journal, SQLite or ledger files.
3. To record SHA-256 locally without exposing secrets: `sha256sum lab/boss.html lab/boss.js lab/boss.css lab/boss_readonly.py lab/dashboard_v2_server.py lab/tests/test_boss_data.py lab/tests/test_boss_ui.py lab/tests/fixtures/boss_synthetic.json`. Verify against the downloaded git objects and record results in the parent acceptance log, not as invented checksums here.
4. Record current exact read-only dashboard argv/cwd, socket/listener owner, health/watchdog configuration, original `expect`, `required_paths` and restart policy **from the real operator config**. These runtime values were not supplied to this recipient, so do not fabricate or overwrite them. Preserve existing health ownership: **no duplicate watcher**.
5. The dashboard server's actual CLI requires `--scorecard ABS`, `--journal ABS`, `--ledger ABS`; retains `--port` (default 8766), `--max-age-ms` (300000), `--max-bytes` (1048576), and new monitor API needs `--profile-root ABS`. Preserve the operator's exact accepted values for the first six flags. The trusted profile root must resolve to the existing read-only source tree; the release directory is not a substitute for a data root.
6. In the existing watchdog/service definition, record the **literal exact argv** and `expect` identity before and after cutover; check the `required_paths` list against all server imports, fixed route assets and `boss_readonly.py`. If the real watchdog cannot express the new identity or dependencies without changing its accepted contract, **block cutover** and request separate authorization. Do not silently rename argv/expect, insert a new watcher, or start a duplicate instance.

## Independent acceptance commands (parent ONLY)

From a clean candidate checkout, with a workspace-owned temp directory, run **both** permissions regimes:

```sh
mkdir -p .boss-tmp
chmod 777 .boss-tmp
umask 0022
TMPDIR="$PWD/.boss-tmp" python3 -m unittest discover -s lab/tests -p 'test_boss_*.py' -v
umask 0002
TMPDIR="$PWD/.boss-tmp" python3 -m unittest discover -s lab/tests -p 'test_boss_*.py' -v
TMPDIR="$PWD/.boss-tmp" python3 -m unittest discover -s lab/tests -v
```

Check that the optional Chromium tests actually ran rather than `skipped`; if Chromium is missing, UI acceptance stays **NOT_RUN**. Run a browser at 1280px and 390px, confirm all three expanded sections, four book cards, collapsed final diagnostics, null vs authoritative zero, hostile text rendered literally, and **measure** `document.documentElement.scrollWidth <= innerWidth` at both widths. Synthetic tests do not prove real live reads.

With the real exact server argv in a controlled read-only test release, verify GET/HEAD for `/`, `/boss.js`, `/boss.css`, `/engineering`, all original fixed paths, `/api/boss-dashboard` and every old API; ensure Host/Origin/cross-site and query/path traversal requests fail, POST returns 405, CSP has external scripts/styles only, and errors expose no source paths. Confirm the small projection never forwards full owner-metro JSON. Observe several refresh intervals and verify source-time versus served-time.

## Parent-only cutover and rollback

No deployment authorized by this file. Parent first records full source/status/account baselines and backups, then verifies single dashboard listener ownership and accepted watchdog identity. Start only the approved read-only dashboard candidate through the existing operator-managed mechanism, recheck all routes and old contracts, no new trader, no new watcher. If any preflight, security check, browser test, digest, source-time check, or post-cutover contract fails: **stop only the candidate dashboard by verified exact PID/argv**, restore previous dashboard release and original watcher configuration/required paths/expect values from saved backup, verify old routes, and retain failure logs. Never roll back or overwrite account, positions, ledger, audit, research window or risk baselines. If exact PID identity is ambiguous, do not signal anything; escalate to parent.
