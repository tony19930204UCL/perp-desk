# OBSERVABILITY / ENGINEERING: white metro live owner dashboard

Local one-way mirror; use only the designated non-main branch and draft PR. Do not merge/push main, deploy, trade, restart processes, change risk/config/controller state/ledger, or read credentials. This new UI scope is independent of earlier owner-dashboard permission history and independent concurrent engineering products.

## Existing source and outcome
The exact served source is commit `0bb1e5cb6570290d2b18968b3f66165aa9c6e638`: lab/dashboard_v2_server.py, dashboard_v2_reader.py, dashboard_v2_model.py, owner_activity.py, owner_activity.html, owner_activity.js, dashboard_v2.html, dashboard_v2_ui.js, tests/test_owner_activity.py, tests/test_owner_activity_browser.py, tests/fixtures/owner_activity_sanitized.json. Operator verified all eleven remote blobs byte-for-byte against installed source. Extend the actual server/dependency closure, not a substitute server. The old observer deliberately has no process discovery, rejects collections above 500, and cannot project the valid larger canonical ledger. Fix the complete source binding, not just its item ceiling.

Deliver one coherent backend+UI unit with deterministic tests and documented isolated operational/browser acceptance entrypoints. Engineer owns all implementation/tests; operator supplies private bindings and runs shipped acceptance/deployment only. Initial implementation plus at most one evidence-focused correction; finite acceptance below, no rolling new gates.

## Presentation
Traditional Chinese, body #F8FAFC, cards #FFFFFF, primary text #0F172A, rounded restrained borders. Four KPI cards above a desktop 68/32 route/Spotlight grid; 390px stacks without body horizontal overflow, route-local scrolling allowed.
1. Identity-verified real processes/PIDs (compute/collector, lease renewal, observer, model coordination distinct; wrappers deduplicated).
2. Maker current phase and valid baseline X/48.
3. Actual liquidation event count and heartbeat time/age.
4. Actual pending human decisions, not a fixed zero or old prohibited paper restarts.

Three product routes: blue PR75 (accepted accounting/reduce-only bounded tests; actual first witness was entry-risk, not proven scale-out root cause; actual market features/execution; complete 48 plus matched/stress validation; reflection/next cycle); green Collectors (cross-venue 15m, liquidation, PR78 native installation/collection, effective 14-day coverage prerequisite); purple PR76 (installed stage controller, current packet/identifier/atomic engineering, genuinely accepted unattended loop). Derive Done/Active from evidence; candidate CI is not installed or unattended acceptance. Historical prerequisites remain unknown unless evidenced.

Done = white check on green dot + green solid track. Active = enlarged colored dot + CSS pulse only for verified activity. Next = hollow dot and right-moving dashed track. Failure/wait/unknown remain explicit. Respect reduced-motion without losing state labels. Main view aggregates current products with current/next/evidence/last verification. All verified/dropped/superseded/frozen paper history goes into bottom details, closed by default, exact derived count; retain failures and restart prohibitions without changing ledger.

## Source, liveness, refresh, safety
Every 3 seconds, nonoverlapping fetch with stale-response protection and last-known values/age on partial failure. Independent ledger/directive/stage/heartbeat/proc/results sources degrade independently: stale documents cannot erase current verified process evidence. Canonical ledger is root {next,items}, valid JSON can exceed 1100 entries and 1MB; retain latest items and exact history totals with reasonable finite bounds, metadata/mtime cache. Do not touch timestamps to manufacture health.

Read only a fixed trusted profile with explicit allowlisted relative source bindings. Trusted launch/adoption records must supply expected start ticks plus applicable boot/argv/cwd identity. Compare /proc against those expected values; never fill expected values from observed /proc as self-proof. Reject reuse/mismatch/gone. Read runtime RSS/elapsed only after identity match. Parent wrappers and actual product child count once. A manual subagent id is not a PID; cron enabled is not executing. Resolve current engineering owners from directive and new ledger dynamically, never hardcode actor or commitment identities; absent authoritative survival evidence = unknown/recent activity, not alive from log mtime.

Project lease/status/stage/completed only, never owner token or full private state. Read bounded last-stage log tails and age, not fake current percentages when logs are silent. Three-second polls must not scan raw market/transaction streams or invoke GitHub, models, backtests or verification. Pure read-only loopback GET; preserve Host/Origin/cross-site/traversal/symlink/XSS/prohibited-env defenses. No arbitrary path/PID/command/URL endpoints, control or trading POST, private argv/log/token export.

Operator binds real sources privately after handback using a documented fixed relative binding contract. Supply a sanitized example shape and safe root-contained validation; do not require public raw state, absolute machine paths or real private identities. Read existing shipped Maker summary/runner contract from repository to map real results instead of inventing keys. Product binding may store source identity and fixed profile-relative paths, never use an arbitrary HTTP query as binding.

## Spotlight and exact result semantics
Without valid results show actual last recorded phase+age, elapsed, RSS, lease/output/verification state. Automatically discover bounded result summaries under an explicitly trusted output root. Count a baseline cell only with valid cell/source/input/config/attempt identity and complete parseable content. Reject empty/partial/bad JSON, old attempts, duplicates. Partial valid cells show '初步結果 X/48，尚未全局驗收', never global green or strategy PASS.

Map gross bp, net bp, TP/(TP+SL+SL_GAP) conditional win rate, actual TIME share, PF and X1/X2/X3 from shipped summary contracts, units/denominators shown, missing NA not zero. Administrative horizon and open tails are not fake TIME fills. Baseline 24 per symbol / 48 total, matched and stress separate; independent accounts never added as a shared portfolio. Exploratory ranking is not NetAlpha/PF/multiplicity acceptance. No simulation-result controls, random performance, fixed fake event totals or fake best rows in production; fixtures isolated and labeled.

## Finite acceptance and handback
- Larger valid ledger plus newest record; malformed/missing source partial degradation; trusted process live/start mismatch/gone/manual expired/cron idle/wrapper dedupe; dynamic owner and pending decision changes.
- All three routes; closed exact-count history; four dynamic KPIs; no fabricated completion.
- Refresh every 3 seconds across two cycles; failed fetch preserves known data/age; source restoration.
- Missing/partial/complete/bad/old/duplicate results automatic transition; exact metric mapping and NA semantics.
- Existing read-only security defenses, bounded read/cache behavior.
- Real Chromium 1280 and 390 CSS viewports, computed colors/layout/animation, DOM/console/fetch proof, fixture/production separation. Ship existing runnable acceptance entrypoints and commands, not instructions for operator to invent a harness.
- Both push and pull_request CI success at exact full PR HEAD. Report test command/IDs/rc or truthful NOT_RUN; no invented CI evidence. Operator reuses exact-head CI and runs only missing acceptance.

Only operator may stage a complete immutable release on a spare port, verify, then switch the designated independent dashboard listener. Existing shared release stays unchanged; other dashboards and protected market/lease/collector processes must remain uninterrupted and byte/identity unchanged. Installation mapping, full source hashes, backup/rollback invocation, staging and production Linux+Windows native HTTP, API, real screenshots, and before/after protected identities are operator acceptance, not a remote deployment claim.

Prefer each update_file packet <=180 changed lines; every new diagnostic constant/error code/test_ function name <=25 chars. Preserve required old identifiers and complete provenance. Only allowed GitHub create_file/update_file content writes on designated branch; report unsupported tree/commit/ref mutations before presenting such a card. Do not bypass a real refusal by renaming/splitting/changing tools. Do not claim a new larger packet policy is installed.

Formal handback schema v2: repository, pr_number, branch, task_id, instruction_id, status READY_FOR_REVIEW/WAITING_CI/BLOCKED, real pre-mailbox source_head_sha, summary, changed_files, tests [{command,result,evidence}], remaining_work, next_step_reminder {to:'Main CIO',verify_now,if_pass,if_fail,blockers:[]}. Write exact designated mailbox; source SHA is actual pre-mailbox commit, not fabricated self-reference. Short final message includes PR, full HEAD, actual CI, uninstalled state and blockers.
