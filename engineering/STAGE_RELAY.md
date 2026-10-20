# C0041 Stage Relay: source-only operator map

This is NOT installed. No AGY access, denied operation retry, approval modification, deployment, trading, paper/live, credential use or Maker acceptance. PR75/r12 is separately owned. Only the authorized operator can install in the active profile.

## Invocation
Linux/Python 3 stdlib (flock and /proc). Replace every example placeholder with verified real identity, authorized explicit command arrays and isolated evidence root. Never infer nonexistent product entrypoints. State is fsync/atomic replace and flock protected; unexpected graph change, unknown PID, expired lease, refused/UNKNOWN outcome stops, not retries. A managed subprocess exit 0 is necessary but insufficient.

    python3 engineering/stage_relay.py init --config "$CONFIG" --state "$STATE"
    python3 engineering/stage_relay.py tick --config "$CONFIG" --state "$STATE"
    python3 engineering/stage_relay.py status --config "$CONFIG" --state "$STATE"
    python3 engineering/stage_relay.py adopt --config "$CONFIG" --state "$STATE" --identity "$IDENTITY" --lease 900
    python3 engineering/stage_relay.py renew --config "$CONFIG" --state "$STATE" --token "$TOKEN" --lease 900
    python3 engineering/stage_relay.py complete --config "$CONFIG" --state "$STATE" --receipt receipts/r12.json --acceptance acceptances/r12.json

Bootstrap must be independently ACCEPTED r11 evidence: matching stage, 40hex source, 64hex input hash, reviewer, evidence_path and verified evidence_sha256. Init never overwrites existing state. External r12 must be ADOPTED, never launched again: identity JSON fields pid (integer), boot_id, start_ticks (Linux proc value), argv (exact array), operator (string), started_at (optional epoch). Unknown/mismatched or vanished process identity is blocked; bound live identity can be renewed before lease expiry by owner token. The module only verifies Linux PID/boot/start, not ownership authorization; operator must independently verify authorization.

## State transitions and trusted terminal contract

READY -> RUNNING for configured exec, or READY -> WAITING_ADOPTION -> RUNNING for external owner. Managed exit 0 -> WAITING_ACCEPTANCE; exit nonzero -> FAILED, repair_request recorded, never auto-retried. Failure before an authenticated terminal receipt, REFUSED or UNKNOWN => no advancement/retry. READY without configured executor => BLOCKED. External owner gone => WAITING_RECEIPT (not presumed accepted). After independent accepted receipt, lock-serialized advancement starts next configured worker immediately. Duplicate exact receipt is idempotently ignored. Last stage => DONE. This is not an approver and cannot override separate safety policies.

Parent receiver must invoke complete with exact terminal facts; generic ledger watcher is insufficient. Receipt JSON fields: stage, source_head, input_sha256, graph_sha256 (SHA256 of graph bytes), owner {pid,start_ticks,boot_id,token}, exit_code=0, outcome=SUCCEEDED, reason, output_count, reports:[{path,sha256,rows}]. For managed stage, worker must first have observed real exit0; for adopted stage, exact owner must be known DEAD and trusted parent must independently supply real exit0 (death alone is not success). Failed or refused receipts stop. Acceptance JSON separately includes stage, source_head, input_sha256, graph_sha256, receipt_sha256, decision=ACCEPTED, reviewer. All source/config/input identities and report file hashes must match. Real reviewer trust/authenticity must be checked by operator; self-written local JSON is NOT a security-grade authentication mechanism.

Report JSON fields: stage, source_head, input_sha256, graph_sha256, rows array. Each row requires symbol and cell_id, with unique symbol/cell pairs. Report contracts require exact row counts. Historical stage mandates BTCUSDT 24 distinct cells AND ETHUSDT 24 distinct cells = 48. Operator must independently validate completeness of real account replay, matched placebo and reflection referencing actual reports; structural checking is not economic validation. Historical and reflection stages remain unaccepted until actual reports and independent acceptance exist.

State records stage/source/input/config/report hashes, owner PID/boot/start/lease, exact argv, product launch timestamp, process log, exit, row count, accepted receipt, pending stage and events. accepted_to_process_seconds records actual accepted-completion-to-next-product-start latency; no zero-idle guarantee.

## R2: official Hermes cron JOB --script (NO operator source edits)

Install the **unchanged** engineering/stage_relay.py bytes as the executable source file at EXACTLY `<active-profile>/scripts/stage_relay.py`. This is a standard file copy/install performed by the authorized operator, **not** new code to write into an existing script. The module has a full **NO-ARG** entrypoint; it infers its profile exclusively from that script's physical parent; rejects symlink entrypoints, paths other than `scripts/stage_relay.py`, and never searches HOME or falls back to any default profile. Install the verified graph at `<active-profile>/stage-relay/C0041/graph.json`, the initialized state at `<active-profile>/stage-relay/C0041/state.json`, and all evidence under `<active-profile>/stage-relay/C0041/evidence/`. The graph evidence_root must equal the exact installed evidence directory. Installing only the module without a graph is permitted and emits `NOT_CONFIGURED` with a model/operator decision needed **every due run**. Do NOT automatically init an unreviewed graph or synthesize acceptance.

The fixed terminal inbox (populated by the independently trusted parent/operator, not by relay) is:

- `<active-profile>/stage-relay/C0041/evidence/inbox/<stage-id>.receipt.json`
- `<active-profile>/stage-relay/C0041/evidence/inbox/<stage-id>.acceptance.json`

Each invocation validates receipt stage/source/input/config hashes, exact owner token/PID/start/boot, observed exit0 for managed process (trusted terminal exit0 for adopted r12), report file hashes, exact row counts, 24 BTC plus 24 ETH unique cells for historical, then **separately** checks explicit independent accepted decision/reviewer and receipt hash. Missing receipt/acceptance, hash mismatch, unexpected stage, failure, UNKNOWN, and REFUSED cannot advance. Fixed inbox names are per current stage; duplicate receipts remain in place and are consumed once by durable state. The complete function starts the next eligible configured executor under the same state lock; simultaneous cron/parent calls cannot start another. Stage source/identity/lease, process PID+start/argv, report count, blocker/reason and ready status are printed as JSON on *every* no-arg invocation, including unchanged responses. If a terminal inbox is invalid, the supervisor returns INBOX_BLOCKED and refuses dispatch.

Authorized operator must use the official `hermes cron list` -> `hermes cron edit JOB --script stage_relay.py` -> `hermes cron list` and the official job readback mechanism to verify the target job's script is exactly `stage_relay.py`. Preserve existing prompt, cadence, model, skills, permissions and safety controls. Do not edit scheduler JSON. **The model receives each due run's full printed JSON**, not a changed-output-only watcher. Do not alter any separate job or install script here. The requested edit changes script routing only; operator must verify it does not accidentally replace required preexisting supervisor behavior. If that cannot be safely preserved, stop and report integration blocker rather than overwriting existing work. No guarantee of zero idle.

Real Maker configuration may remain unconfigured pending independent acceptance of r12 source/input and executors. The module does not invent those commands or true market performance. Offline synthetic drill now invokes the actual copied installed `scripts/stage_relay.py` **without arguments** and drops synthetic accepted inbox pairs after each genuine subprocess, causing exactly-once dispatch through the no-arg consumer; it is never market/performance proof.

## Isolated synthetic real-subprocess drill

After authorized install, in active profile isolated evidence namespace use a fresh directory each time:

    python3 /ABSOLUTE/ACTIVE_PROFILE/engineering/stage_relay.py demo --dir /ABSOLUTE/ACTIVE_PROFILE/stage-relay/C0041/drill-UNIQUE

It creates synthetic source/input identifiers, bootstrap proof, four real launched subprocess stages (1,2,48,1 rows), per-stage argv/PID/start/latency/log/output report/receipt, positive progression and negative hash-mismatch guard, all persisted. Clearly marked SYNTHETIC_SUBPROCESS_ONLY. Its generated acceptances are isolated synthetic test fixtures, NOT real approval. Not historical market observation, not full Maker account replay, not matched placebo performance, not product-chain proof.

Offline tests: python3 -m unittest discover -s engineering/tests -p test_stage_relay.py -v

Operator reports r1 nine tests rc0 and four-stage synthetic CLI drill rc0 (not independently rerun in this r2 conversation). R2 additional tests and exact-head CI remain NOT_RUN here. No shell/cron/install execution performed by this source-only delivery.
