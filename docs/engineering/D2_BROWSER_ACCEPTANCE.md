# D2 real-browser acceptance entrypoint

This is an **opt-in, un-deployed engineering acceptance test** for PR58 D2 only. It serves the frozen `dashboard_v2.html` and `dashboard_v2_ui.js` through the existing `dashboard_v2_server.create_server` on an ephemeral 127.0.0.1 port; it does not use `page.setContent`, a DOM mock, production account data, a live dashboard, or any trading service. No new Python dependencies, browser downloads or CI workflow changes are introduced.

## Exact operator command

From a clean checkout of the intended PR HEAD, with a **preinstalled** Google Chrome/Chromium and Python 3 available:

```bash
python3 lab/browser_dashboard_v2_acceptance.py --expected-head "$(git rev-parse HEAD)" --browser /usr/bin/chromium --output artifacts/d2-browser
```

Replace `/usr/bin/chromium` with the absolute path of a preinstalled Chromium binary if necessary; omit `--browser` to search common installed binary names. Do not install a browser from the harness. The `--expected-head` argument must resolve to the actual 40-hex checkout SHA. The operator must independently verify the intended PR HEAD first; no claim that `git rev-parse HEAD` itself proves branch authorization. The default deadline is 120 seconds, adjustable through `--deadline-seconds` (30–240). Run in an isolated shell without credentials or access to a logged-in browser profile. Screenshots and report files go only to the chosen output directory.

Chromium scrollbar measurement: on headless Chromium a visible vertical scrollbar can reduce `document.documentElement.clientWidth` (for example, CSS innerWidth 1280 but clientWidth 1265), even when the page has no horizontal overflow. The private Chromium launch uses `--hide-scrollbars` so the existing strict `innerWidth == clientWidth == requested width` and both root/body `scrollWidth <= clientWidth` checks remain unchanged for 1280x800 and 390x844. This is a harness environment normalization, not a dashboard layout change or a relaxed assertion.

The command uses `git rev-parse HEAD`: it requires an actual checkout with `.git` metadata. An exported source tree without `.git` cannot supply checkout HEAD and cannot use that exact command as written. Do not substitute a guessed SHA or treat exported-tree testing as source-bound acceptance.

Bounded stdlib-only fixture/contract tests (not a browser pass):

```bash
cd lab && python3 -m unittest -v tests.test_dashboard_v2_browser_acceptance
```

Normal `python3 -m unittest discover -s tests -v` imports the contract tests but does **not** start Chromium, and must not silently equate this with browser acceptance.

## Prerequisites and fixture contract

The harness uses Python standard library only: `http.server`, sockets, WebSocket framing for Chromium DevTools Protocol (CDP), `subprocess`, JSON, SHA256 and temporary directories. It reuses repository D2 reader/model/server and existing CDP patterns rather than introducing Playwright. It creates one isolated Chromium process/profile, one ephemeral loopback D2 HTTP server and three temporary synthetic source files; shuts them down after a bounded run. No arbitrary file serving, external service, production port, paid service or runtime account/window writes.

`lab/tests/fixtures/dashboard_v2_browser.json` is synthetic data with `schema_version:1`, fixed integer `now_ms`, `scorecard` (`at` and `arms`), `journal` rows, `ledger.items`, `hostile` literal and `long_text`. The harness serializes scorecard and journal as newline-delimited JSON (with terminating newline) and ledger as JSON into its own temporary directory. The existing `read_dashboard` and `build_model` project these values. Zero-valued source metrics and null fields intentionally differ; health, quotes, T1 and equity remain unsupported/unknown.

The source HEAD, fixture/server/reader/model/HTML/JS SHA256, byte lengths and `mtime_ns` are captured before use, compared after 100 actual readonly HTTP API reads, and included in `report.json`. Output artifacts are intentionally untracked. The browser binary override and output path are not interpreted as dashboard source locations.

## Required named probe mapping

| Probe ID | What the real browser must demonstrate |
| --- | --- |
| `D2-BROWSER-POPULATED-1280-390` | Navigate real `/` using the served D2 HTML/JS, actual populated reader-model API, desktop CSS 1280x800 and mobile CSS 390x844; assert populated arms, journal and commitments, long text, exact `innerWidth/clientWidth/scrollWidth`, no root/body horizontal overflow; write matching SHA256 PNG screenshots. |
| `D2-BROWSER-HOSTILE-TEXT` | Hostile arm, journal, verdict and commitment remain literal text, not executable DOM nodes; detect injected elements or side-effect marker. |
| `D2-BROWSER-UNKNOWN-VS-ZERO` | Known zero count/money displays `0`/`0.00` with exact `0`; null day/count/money shows `未知`, without fabricated zero. |
| `D2-BROWSER-STALE-FETCH-CLEARING` | Start populated; CDP Fetch intercepts only actual `/api/dashboard` requests for isolated 503, broken JSON, invalid model, and failed network; assert prior arms clear each time. A deliberately held older response must not overwrite a newer completed response. Actual page/JS/server remain unchanged. |
| `D2-BROWSER-NO-EXTERNAL-REQUESTS` | Subscribe to Network and Fetch **before navigation**, log requests/responses/completions/failures/pauses including controlled injected failures; fail and locally block any URL outside the four exact routes of the ephemeral loopback origin. Resource Timing alone does not count. |

## Report and exit contract

The machine-readable terminal JSON and `<output>/report.json` include `schema_version`, classification, instruction ID, actual command/config, checkout HEAD, browser name, origin, `input_state_before`/`input_state_after`, `readonly_reads`, five individual `probes` (each `id`, `status`, `duration_ms`, evidence), `artifacts` with screenshot SHA256/pixel dimensions, `network_trace`, `network_violations`, cleanup, failures and `exit_code`. Probe status is always one of `PASS`, `FAIL`, `NOT_RUN`, `TIMEOUT`. A failed prerequisite leaves named probes `NOT_RUN` and produces a non-success acceptance exit; it does not silently skip or claim PASS.

Exit 0: all five real browser probes passed and cleanup clean. Exit 1: a probe/cleanup assertion failed. Exit 2: prerequisite unavailable or named probe not run. Exit 3: named probe exceeded the acceptance deadline. Operator should inspect all probe evidence and report even if a screenshot exists. A browser report is distinct from green unit CI and does not itself approve production changes, deployment, merging or trading.

The operator (Main CIO) performs independent source allowlist/CI verification and product/visual review. Any D2 production defect demonstrated by the browser must be reported with the precise source HEAD and failed probe; this harness has no authority to change production code.
