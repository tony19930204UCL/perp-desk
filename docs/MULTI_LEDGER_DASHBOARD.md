# Configured multi-ledger observer dashboard

Status: operator-deployable candidate for the read-only dashboard only.

## Scope

This extends the existing observer dashboard that already shows:

- account funds / PnL / positions
- current research-window progress
- closed flat-to-flat trades
- rejection-reason distribution
- fee/funding cost statistics
- health/work panels

It does **not** change trading runtime, strategy, risk, source gates, storage, account history or research windows.

## Backwards-compatible single-ledger default

If the dashboard is started exactly as before:

```sh
python dashboard.py \
  --port 18767 \
  --status /absolute/path/to/shared/paper_v2_live.json \
  --html /absolute/path/to/dashboard.html
```

it runs in single-ledger mode.

Internally that status path becomes one configured ledger with id `default`. Existing supervisor/dashboard startup therefore remains compatible without any new flag.

## Optional operator ledger allowlist

Multi-ledger mode is enabled only with an explicit operator-owned config:

```sh
python dashboard.py \
  --port 18767 \
  --status /absolute/root/alpha.json \
  --html /absolute/path/to/dashboard.html \
  --ledgers /absolute/path/to/ledger-config.json
```

Example synthetic structure:

```json
{
  "schema_version": 1,
  "root": "/absolute/root",
  "default": "alpha",
  "ledgers": [
    {
      "id": "alpha",
      "label": "Alpha PAPER",
      "status": "alpha.json"
    },
    {
      "id": "beta",
      "label": "Beta PAPER",
      "status": "nested/beta.json"
    }
  ]
}
```

Rules:

- the config path must be absolute and not a symlink;
- `root` must be an absolute existing directory;
- ledger ids are restricted to a short alphanumeric / underscore / dash token;
- ledger labels are display strings only;
- ledger `status` is a safe path **relative to the configured root**;
- absolute status paths, `..`, unsafe dot components and symlink targets are rejected;
- ids must be unique;
- the configured default id must exist;
- the configured default's resolved status must equal the existing `--status` path.

That final rule preserves the existing dashboard/supervisor identity instead of silently changing which account the legacy launch command means.

## Client selection contract

The browser never sends or receives a filesystem path.

It may request:

```
GET /api/ledgers
GET /api/status
GET /api/status?ledger=beta
```

`/api/ledgers` exposes only:

- id
- display label
- whether the ledger is the default

It does not expose configured paths.

Unknown ids, malformed ids, traversal-shaped values, multiple ledger parameters or extra status-query parameters fail closed.

A selected missing or invalid snapshot returns unavailable for that **selected ledger**. It never falls back to another ledger.

A selected stale but otherwise valid snapshot remains the selected ledger's last-known view with existing stale semantics. It is not replaced by a fresh default ledger.

Observer reconstruction/reconciliation failure likewise remains attached to the selected snapshot. Base account values may remain readable under the existing strict dashboard contract, but research/trade statistics remain unavailable; another ledger's analytics are never substituted.

## Ledger isolation

Every `/api/status?ledger=<id>` request:

1. resolves the id only through the server-side allowlist;
2. reads exactly that configured snapshot;
3. validates that snapshot independently;
4. computes observer analytics from that same snapshot;
5. returns a `ledger_view` id/label describing that selected ledger.

No state is written.

Synthetic acceptance performs:

`alpha -> beta -> alpha`

and byte-compares both snapshot files before/after to prove switching does not mutate either ledger.

Acceptance also uses different:

- starting capital
- market symbols
- research start/deadline
- closed-trade symbol
- fee/funding evidence

to prove the dashboard does not mix capital, windows, trades or costs across ledgers.

## High-precision presentation

Ledger/account values are never rounded or rewritten.

For readability, long decimal strings use a bounded presentation form. Examples:

- exact `12345678901234567890.1234500`
- display `1.23456789e19`

The original exact string remains in the DOM on a focusable element via:

- `data-exact`
- `title`
- an accessibility label containing the exact value

Desktop users may hover/focus; mobile users may tap/focus.

The same pattern is used for:

- headline account amounts
- closed-trade entry/exit prices
- net PnL
- fee/funding values
- displayed ratios/statistics
- position numeric values
- public market numeric cells

This is presentation only. Accounting reconciliation continues to use the original snapshot strings.

## Responsive layout

The root page is bounded against horizontal viewport overflow. Wide tables remain inside their existing scroll containers.

Real browser acceptance uses headless Chrome against a real ephemeral localhost dashboard with synthetic ledgers:

- desktop viewport: 1366 x 768
- mobile viewport: 390 x 844

The DOM must report root `layoutOverflow=false` after render.

Screenshots and an evidence JSON are uploaded by CI as:

`issue-3-browser-evidence`

The screenshots contain synthetic account data only and are not strategy-performance evidence.

## Existing strict observer behavior retained

This change reuses the deployed observer analytics/reconciliation logic.

It does not weaken:

- full-account ledger reconciliation
- duplicate fill/ledger/funding event checks
- partial-fill episode reconstruction
- open-position cost accounting
- research-window count reconciliation
- missing-window behavior
- rejection distribution reconciliation
- stale/error semantics
- Host / Origin / cross-site protections
- read-only HTTP methods

Undefined ratios and zero-sample statistics remain undefined rather than implying profitability.

## Supervisor compatibility

The startup supervisor accepts an optional operator-local `dashboard_ledgers` path.

When absent, the dashboard command is byte-for-byte equivalent in shape to the previous single-ledger launch contract.

When present, the supervisor only appends:

```
--ledgers /absolute/path/to/operator/ledger-config.json
```

to the dashboard process. Runtime state/status identity, storage policy and health configuration remain unchanged.

## Operator deployment

1. Verify exact PR head and green CI.
2. Keep the currently accepted runtime/state/status/storage/health configuration unchanged.
3. For single-ledger deployment, use the existing dashboard command unchanged.
4. For multi-ledger deployment, create the operator-local allowlist JSON and keep the existing `--status` ledger as the configured default.
5. Run the dashboard manually first.
6. Read back `/api/ledgers` and verify that no paths are exposed.
7. Read each configured `/api/status?ledger=<id>` and verify capital/window/trades/costs correspond only to that ledger.
8. Verify unknown and traversal-shaped ledger ids fail closed.
9. Verify stale/invalid selected ledgers do not switch to the default.
10. Verify desktop/mobile rendering and exact-value access.
11. Only then update the optional supervisor `dashboard_ledgers` field if the manager layer is in use.

No runtime restart is required for a dashboard-only deployment.

## Rollback

1. Stop only the dashboard process being changed.
2. Remove the optional `--ledgers` argument / supervisor `dashboard_ledgers` field.
3. Restart the dashboard with the previous `--status` and `--html` arguments.
4. Verify `/api/status` is the same single configured snapshot as before.

Rollback never restores an old account copy, changes the trading namespace or alters research history.
