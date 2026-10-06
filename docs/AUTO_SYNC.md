# Owner-controlled GitHub synchronization transition

This is a one-way review mirror, not a deployment directory. Main belongs to
normal local synchronization; engineering proposals belong in non-main branches
and PRs. The exact target is `tony19930204UCL/perp-desk`, with fetch and push URL
`https://github.com/tony19930204UCL/perp-desk.git`.

## Explicit modes (Issue 40)

The exporter and scheduler wrapper default to **private**. There is no automatic
visibility detection or authorization inferred from API metadata. Both modes
reuse the existing exporter, collection policy, scanner and transaction gates.

From the operator's already configured profile directory:

```sh
# Before the owner flips visibility: existing command, no flags.
python3 scripts/paper_review_sync.py

# After the owner flips visibility AND the accepted candidate is deployed:
python3 scripts/paper_review_sync.py --visibility public --authorize-public-repo tony19930204UCL/perp-desk
```

The scheduler's configured invocation must use the corresponding exact command;
interactive flags do not persist or silently modify the scheduler. The wrapper
accepts only these visibility/authorization options, not a profile, repository,
remote override, `--force`, or arbitrary exporter options. Public mode requires
both explicit flags and the literal exact target above. Private mode rejects a
public authorization flag. A public invocation against a still-private remote
fails closed; a private invocation against a public remote also fails closed.
The API response must contain exact `full_name` and a genuine boolean `private`
matching the selected mode. Missing/malformed visibility or identity is rejected.
Fetch and push URLs must remain exact. No tool here changes repository visibility.

## Candidate, CI and deployment ordering

Local engineering evidence is **not** green hosted CI and does not authorize
deployment. Staging this branch in an isolated engineering directory is allowed
before the visibility change; installing it into the operator profile is not
allowed until the original exact-head green-CI requirement is satisfied.

1. Refresh main, the candidate head and currently installed source hashes. Review
   the narrow diff and public-readiness evidence independently. If installed
   wrapper/tests/docs drifted, stop and request exact-head integration; do not
   overwrite newer work or any other PR's deployed contracts. Preserve the
   exporter allowlist, workflow and all runtime/research/UI sources.
2. If hosted CI becomes green while still private, accept and deploy only this
   narrow candidate through the existing operator process, backed up first.
   Keep the original no-flag invocation while private. Verify normal sync's
   exact remote SHA, its existing debounce and scan gates before any flip.
3. **If CI remains blocked by the private account prerequisite:** leave the
   legacy wrapper installed. The operator pauses ONLY normal mirror sync and
   waits for any in-flight sync to finish before the owner changes visibility.
   This is a deliberate bounded publication gap, not a trading/runtime stop.
   Do not start a candidate public invocation or deploy based on local tests.
   Even if an old invocation accidentally runs after the flip, its existing
   strict private gate rejects the public metadata before publication; this is
   verified, but is a backstop, not a replacement for quiescing sync.
4. The owner may then explicitly make this exact repository public, subject to
   independent public-readiness acceptance. Read back visibility/identity without
   retaining raw authenticated API responses. That decision does not approve
   candidate deployment and cannot erase material already exposed in history.
5. After the flip, request **one** hosted workflow execution for the unchanged
   candidate branch, using the workflow's existing dispatch entry. Example:
   `gh workflow run ci.yml --repo tony19930204UCL/perp-desk --ref fix/issue-40-public-transition-sync`.
   Read back the actual run/head, jobs and conclusions; require the candidate
   head exactly and all original checks green. Dispatch alone is not success,
   visibility alone does not guarantee account prerequisites are resolved, and
   an unchanged failed private run does not turn green automatically. If still
   blocked, keep sync paused and report BLOCKED; no repeated reruns, weakened
   checks, runner changes or billing changes.
6. Only after exact-head hosted CI is green and parent acceptance is complete,
   deploy the narrow files using the original operator workflow and backup/hash
   checks. Mirror mappings are `scripts/paper_review_sync.py` to the same relative
   operator path, `automation/tests/test_review_sync.py` to
   `repo_sync/tests/test_review_sync.py`, and `docs/AUTO_SYNC.md` to
   `repo_sync/docs/docs/AUTO_SYNC.md`. The exporter itself is unchanged; evidence
   sidecars are not runtime inputs. Do not merge/push remote main to install.
7. While sync remains paused, change only its existing configured invocation to
   the explicit public command above, retaining all schedule/delivery/ownership
   settings. The operator must read back that exact configuration. Run a normal
   authorized tick; allow the existing 15-second settling interval (no forced
   gate bypass), then verify scans, status and exact remote SHA readback. Resume
   the existing sync task only after its own acceptance. Normal mirror publication
   owns main; its publication CI is recorded separately from candidate-head CI.

None of these operator steps was performed by this candidate. Current schedule
state and current repository visibility must be read back by the operator, not
inferred from historical README text. Trading/runtime/root, cron, visibility and
main remain untouched by engineering delivery.

## Configuration rollback (no history rewrite)

Pause and drain sync first. While the repository is still private, restore the
backed-up wrapper if needed and use the original no-flag invocation. If it is
public, reverting to a legacy/private-only wrapper or removing public flags
**cannot** restore successful synchronization: leave sync paused/fail-closed.
Only an explicit owner return to private permits the private invocation to work
again after strict identity/visibility readback. Alternatively keep the accepted
candidate and exact authorized public invocation. Do not force push, reset main,
rewrite history, restore old runtime databases, or widen any authorization.
Making a repository private again does not retract copies of published history.

## Existing security and export contracts

Changed source fingerprints settle for at least 15 seconds. Each tick rechecks
exact identity, selected visibility and fetch/push URLs; fetches all advertised
refs into a scanner-only namespace; scans current exported bytes and local,
all-ref/reflog history with real gitleaks default rules; commits only changed
owned files; pushes without force; and reads back the exact remote SHA.
`--gitleaks-ignore-path /dev/null`, `--ignore-gitleaks-allow` and sanitized
GITLEAKS environment prevent repository-level suppression. Hooks, Git config,
permissions, symlink/hardlink and whole-tree byte gates remain unchanged.
No-change produces no commit. Failure records blocked and preserves last success.
Remote-main divergence/unowned history requires review, never overwrite.

Local telemetry, ownership/status files, credentials, gateway/chat/session data,
runtime DBs, raw snapshots and private config remain excluded. No allowlist is
broadened. Established public collection omits context/SOUL.md and obsolete
context/SPEC.md, filters the personal/account/environment section of context/AGENTS.md,
and hashes actual exported bytes. Original profile sources are not modified by
collection. New snapshots do not erase prior history. Secret scanning is not a
certification of absence of personal or operational information; public-readiness
assessment, including unadvertised/deleted history limitations, is separate.
