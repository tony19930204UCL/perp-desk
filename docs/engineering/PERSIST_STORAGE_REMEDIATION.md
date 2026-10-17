# PERSIST storage remediation — SR1-r2

SR1-r2 integrates accepted PR59/PR60 persistence into the later deployed
storage/risk/off-tick contract. It does not deploy, start a paper service, read a
live DB, sample public sources, reset an account/window, or change strategy,
fees, risk, freshness, tick/step, arrival or funding rules.

## Operational storage classes

* `sim_broker_state` is compact hot recovery state. Seen-event bodies are not
  rewritten into it. `sim_seen_events` is the independent event-ID/digest
  index and `sim_seen_event_meta` carries validated event type/time. Funding
  IDs never expire; only sufficiently old nonfunding IDs are eligible for the
  accepted PR60 retention rule.
* `runtime_causal_receipts` is immutable observation evidence for every
  runtime fetch that can affect a signal/order/fill/funding decision:
  exchangeInfo, depth, premiumIndex, bookTicker, klines, fundingRate and
  fundingInfo. Each observation retains endpoint/params/envelope/source and
  receipt timing and points to immutable `runtime_receipt_payloads`. Equal
  payload bytes share one SHA256 payload row; observations are never collapsed.
  A missing or corrupt payload refuses reconstruction/reopen.
* `/fapi/v1/time` is explicitly noncausal clock/transport raw input. It is
  written to `runtime_noncausal_raw_receipts`, bounded to 256 rows by default.
  Its expiry cannot delete a causal observation or payload. Existing audit rows
  are never deleted.
* Public audit remains append-only. New causal receipt audit entries carry the
  immutable receipt sequence and payload digest rather than duplicating the
  payload. Off-tick diagnosis reconstructs the exact depth receipt through that
  pointer before matching source/receipt/dispatch timing. Signal/order/fill
  causal inputs therefore do not depend on the bounded raw table.
* `runtime_equity_history` is append-only; only the status view is capped at
  2048 recent points. `runtime_exact_receipts` content-addresses complete
  exchangeInfo receipts used by runtime state.

Receipt-store reopen validation is O(causal observation rows), using indexed
payload lookup for each observation. It deliberately does **not** claim all
persistent state is bounded: immutable causal evidence and equity history grow
append-only. The bounded raw table is not scanned on reopen.

## Atomic migration and failure behavior

The accepted broker full-event/digest migration, equity migration, reference
archive and new receipt schemas all use caller-owned SQLite transactions.
Schema creation/import/pointer writes roll back on ordinary errors and deferred
COMMIT failures. A rolled-back attempt is retryable. Existing account identity,
`forward_start`, cash, orders, positions, fills, ledger, marks, funding state,
day baselines and audit are retained; migration never creates a fresh account or
paper window.

New tests exercise payload-byte corruption, missing pointers, reopen refusal
without repair/fabrication, rollback/deferred COMMIT, raw expiry versus causal
reconstruction, permanent funding replay after restart/nonfunding expiry, and
base/derived runtime fetch paths. Legacy PR59/60 full/digest migration tests
remain in place.

## Twelve stopped deployed arms: runtime/config mapping

The twelve later deployed research arms are the four F1H symbols, four W1
symbols and four T1 symbols. Every family creates the broker at
`<state-dir>/broker.sqlite3` and runtime state at
`<state-dir>/runtime.sqlite3`.

| family/runtime | configs |
| --- | --- |
| F1H ETH / `paper_runtime_v4.PaperRuntime` | `paper_config_v4.json` |
| F1H / `paper_runtime_v5.PaperRuntime` | `paper_config_f1h_btcusdt.json`, `paper_config_f1h_solusdt.json`, `paper_config_f1h_xrpusdt.json` |
| W1 / `paper_runtime_v6.PaperRuntime` | `paper_config_w1_ethusdt.json`, `paper_config_w1_btcusdt.json`, `paper_config_w1_solusdt.json`, `paper_config_w1_xrpusdt.json` |
| T1 / `paper_runtime_v7.PaperRuntime` | `paper_config_t1_btcusdt.json`, `paper_config_t1_ethusdt.json`, `paper_config_t1_solusdt.json`, `paper_config_t1_xrpusdt.json` |

The old v2 default is an ancestry/base implementation, not an extra one of
these twelve arms. Derived runtimes inherit its save/fetch storage seam.

## Operator migration commands — stopped services only

For each arm set `STATE` to that arm's existing state directory and `CONFIG`
to the exact config above. Never point at a new directory. Back up both SQLite
files using SQLite backup API:

    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/runtime.sqlite3" "$STATE/runtime.sqlite3.pre-sr1.bak"
    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/broker.sqlite3" "$STATE/broker.sqlite3.pre-sr1.bak"

Use the matching family for a **non-polling constructor/open-close**. These
commands perform no `poll()`, no public request and no service start:

    python3 -c 'import sys; from paper_runtime_v4 import PaperRuntime; r=PaperRuntime(sys.argv[1],sys.argv[2]); r.close()' "$STATE" "$CONFIG"
    python3 -c 'import sys; from paper_runtime_v5 import PaperRuntime; r=PaperRuntime(sys.argv[1],sys.argv[2]); r.close()' "$STATE" "$CONFIG"
    python3 -c 'import sys; from paper_runtime_v6 import PaperRuntime; r=PaperRuntime(sys.argv[1],sys.argv[2]); r.close()' "$STATE" "$CONFIG"
    python3 -c 'import sys; from paper_runtime_v7 import PaperRuntime; r=PaperRuntime(sys.argv[1],sys.argv[2]); r.close()' "$STATE" "$CONFIG"

Use exactly one matching command per arm. Before and after, independently verify
the existing account/version/forward-start/cash/ledger identities and schema.
A constructor refusal is a migration failure: keep the failed/new DB files and
backups as evidence and do not start the service.

**Code rollback is preferred after a compatible additive migration.** The new
tables are additive and old evidence is retained; reverting candidate source
does not require restoring old account data. Restore a pre-SR1 database backup
only when an independently reviewed schema/account compatibility failure
requires it. Such restoration is an exceptional operator recovery and must
first preserve the failed/new database for evidence. If authorized, restoration
also uses SQLite backup API, never a byte copy:

    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/runtime.sqlite3.pre-sr1.bak" "$STATE/runtime.sqlite3"
    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/broker.sqlite3.pre-sr1.bak" "$STATE/broker.sqlite3"

None of these operator commands was executed by SR1-r2.

## Executable acceptance entrypoints

Existing PR51 partial differential and 40000+20 write probes remain available:

    cd lab && SIM_BROKER_P2_DIFFERENTIAL=1 python3 -m unittest -v tests.test_sim_broker_persistence.P2EvidenceTests.test_parent_differential_2048_events
    cd lab && SIM_BROKER_P2_WRITE_PROBE=1 python3 -m unittest -v tests.test_sim_broker_persistence.P2EvidenceTests.test_linux_write_bytes_40000_history

SR1-r2 adds a pinned deployed-source differential. The local immutable deployed
broker blob must equal Git blob
`b34ef7e7a30e35c51019420937f505513679c29c`; the test refuses otherwise. It
covers >2000 events with fills/stops/finalized funding, duplicate/conflicting
IDs, out-of-order/off-tick rejection, risk rejection, restart and permanent
funding replay while comparing broker causal state/audit:

    cd lab && STORAGE_SR1_DEPLOYED_DIFFERENTIAL=1 python3 -m unittest -v tests.test_sim_broker_persistence.SR1DeployedDifferentialTests.test_2048_events_restart_and_rejection_matrix tests.test_runtime_equity_history.ReferenceRuntimeTests.test_runtime_stale_future_source_rejection_matrix

The second named test explicitly exercises future, >15000ms stale, exact-boundary
acceptance and source<=dispatch without pretending broker-only code owns the
runtime freshness gate. Existing off-tick runtime tests retain exact
receipt/source/dispatch linkage.

The ten-minute entrypoint runs for real elapsed >=600 seconds, performs no
network I/O, uses isolated SQLite files, attributes Linux process write_bytes
around each broker-hot/history/reference/fundingInfo write, reports each
bytes/s contribution and SQLite file size, asserts each contribution
<50000 B/s, one fundingInfo payload row for repeated content, and raw <=256:

    cd lab && STORAGE_SR1_TEN_MINUTE=1 python3 -m unittest -v tests.test_runtime_receipt_storage.TenMinuteStorageEvidenceTests.test_ten_minute_contribution_separated_storage_rate

Full suites remain:

    python3 -m unittest discover -s automation/tests
    cd lab && python3 -m unittest discover -s tests

SR1-r2 is GitHub-file-only, so every command above is **NOT_RUN** here. CI must
report named test IDs and skips; the independently observed r1 Ran counts had 13
multiline-output parsing gaps per run, so no count reconciliation is claimed.
Fixture evidence cannot establish 1h/24h live stability, and no paper restart is
authorized.
