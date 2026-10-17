# PERSIST storage remediation (SR1-r1)

This candidate integrates the already accepted PR59/PR60 persistence with the
later deployed broker rejection diagnostics. It does not deploy, start a paper
service, touch a live database, reset an account/window, or alter strategy/risk
thresholds.

## Storage classes

1. **Compact hot recovery state.** `sim_broker_state` keeps orders, positions,
fills, ledger, causal broker audit, marks, funding accounting, funding status,
day baselines and the original cash/account/window identity. Seen-event bodies
are not rewritten in the singleton.
2. **Independent broker dedup indexes.** `sim_seen_events` stores event ID ->
SHA256. `sim_seen_event_meta` records validated type/observation time. Funding
IDs are permanent. Only non-funding IDs strictly older than the configured
processed-clock retention are pruned. Legacy full-event and digest singleton
forms migrate transactionally; unknown digest-only legacy rows are not guessed
to be reconstructible or prunable.
3. **Immutable causal receipt evidence.** `runtime_causal_receipts` appends one
row for every fundingInfo observation, retaining endpoint, params and the full
receipt envelope (including receipt/source timestamps). Its payload points to
`runtime_receipt_payloads`, keyed by canonical SHA256. Equal payloads therefore
store payload bytes once without collapsing observations. A missing/corrupt
pointer fails closed.
4. **Bounded ordinary noncausal raw receipts.**
`runtime_noncausal_raw_receipts` is independent and defaults to the latest 256
rows. Expiry cannot remove causal rows or payloads. No existing audit evidence
is deleted by this migration.
5. **Append-only equity/history.** `runtime_equity_history` retains every
accepted original point; only the status view is bounded to 2048 recent points.
6. **Content-addressed exchangeInfo.** `runtime_exact_receipts` stores the
complete original exchangeInfo receipt by SHA256 while the singleton carries a
digest pointer.

The fundingInfo public audit event now carries the immutable causal receipt
sequence and payload SHA256 plus original endpoint/params/receipt/source timing
metadata. Reconstructibility comes from the causal table plus payload archive,
not from adjacency or a hash-only assertion. Existing non-funding public audit
rows remain unchanged, preserving the deployed off-tick source correlation.

## Atomicity and rollback

All migrations and receipt writes use caller-owned SQLite transactions. Schema
creation, legacy import, singleton pointer updates and archive inserts are
rolled back together on an exception or failed deferred COMMIT. Reopening
validates schemas, canonical payload hashes and causal pointers. Migration is
repeatable: durable markers/digest rows make a completed import idempotent, and
a rolled-back attempt can be retried.

No journal/durability pragma is weakened. No destructive compaction of existing
evidence is performed.

## Operator migration / rollback (services must remain stopped)

The operator, not this engineering run, owns execution. Use Python's SQLite
backup API; do not byte-copy a live database. The following exact commands are
for a stopped state directory `$STATE`.

Backup runtime DB:

    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/runtime.sqlite3" "$STATE/runtime.sqlite3.pre-sr1.bak"

Backup broker DB:

    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/broker.sqlite3" "$STATE/broker.sqlite3.pre-sr1.bak"

Migration is intentionally driven by one isolated runtime open/close against the
candidate source and existing immutable config; do **not** start a service:

    python3 -c 'import sys; from paper_runtime_v2 import PaperRuntime; r=PaperRuntime(sys.argv[1],sys.argv[2]); r.close()' "$STATE" "$CONFIG"

For rollback, keep services stopped, preserve the failed/new DBs for evidence,
and restore through SQLite backup API (destination DB must not be concurrently
open):

    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/runtime.sqlite3.pre-sr1.bak" "$STATE/runtime.sqlite3"
    python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$STATE/broker.sqlite3.pre-sr1.bak" "$STATE/broker.sqlite3"

These commands were **NOT_RUN** by SR1-r1. No live migration or service restart
is authorized here.

## Acceptance evidence status

Source and executable tests are included, but SR1-r1 cannot execute local
commands under its GitHub-file-only tool contract. Therefore differential replay
>=2000 events, real SQLite migration/restart/corruption/deferred-COMMIT probes,
40000+20 Linux write_bytes, isolated synthetic ten-minute bytes/s, full lab and
automation suites, and exact-head push/pull_request CI are all **NOT_RUN** here
and remain for Main CIO/CI. A fixture does not establish 1h/24h live stability;
that evidence remains pending separate authorization.

File SHA values must be taken from the final exact source commit by Main's
independent verifier; SR1-r1 does not fabricate hashes from unexecuted local
tools.
