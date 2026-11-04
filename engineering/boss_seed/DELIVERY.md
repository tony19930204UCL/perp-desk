# BOSS18769-r0 — source delivery (not deployment)

- Repository: `tony19930204UCL/perp-desk`; draft PR88; branch `engineering/boss-18769-20261104`; verified initial seed `61e078a5caa06a9c414bd20c476342414a9528da`.
- Added `lab/boss.html`, `lab/boss.js`, `lab/boss.css` with three initially expanded business sections and four weekly cards; bottom-only collapsed engineering detail; external CSS/JS only; `textContent`/DOM creation only; no server write action.
- Preserved existing `boss_readonly.py` byte-identical. Flat PnL, missing valuation, authoritatively reconciled journal/cash, weekly per-book basis, unverified Binance product, and source timestamps retain adapter semantics.
- Brought `/engineering` to a real existing read-only `owner_activity.html` asset. Absent optional seeded `desk_readonly.py` no longer prevents server startup; `/api/desk-readonly` returns 404 without it. This does **not** establish restoration of the original intended desk UI.
- Added synthetic projection fixture, stdlib adapter/route-security tests and optional actual Chromium 1280/390 DOM tests. Browser checks intentionally SKIP when Chromium not installed; this is NOT a PASS.
- Historical seed observation preserved: eight adapter tests PASS; ninth homepage test FAIL HTTP500 from missing assets. These are prior operator observations, not executions by this recipient.
- **Actual tests run by recipient: NONE (NOT_RUN)**. GitHub Contents connector can read/write files but does not execute tests. Parent must run both `umask 0022` and `0002`, including browser and all existing suites. No CI success is claimed.
- **Outstanding blockers:** seeded `desk_home.js`, `owner_cards.js`, `desk_readonly.py` do not exist in this branch; full old route coverage and real operational watchdog argv/expect/required_paths must be verified by the parent. These paths are outside allowed writes. Real Chrome DOM and horizontal overflow measurements not executed.
- Deployment/rollback specification only in `engineering/boss_seed/DEPLOYMENT.md`, including Git blob digest manifest and parent-side SHA-256 capture. No merge, deployment, account/trade action or credential access.
- Handoff schema-v2 JSON is `docs/engineering/BOSS_18769_HANDOFF.json`, committed last with pre-mailbox source HEAD.
