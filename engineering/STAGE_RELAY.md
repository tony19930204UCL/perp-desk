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

## R3 test-fixture distinction (no production change)

The direct CLI/unit fixture intentionally defaults to `kind=synthetic_stage_relay`. Only the temporary installed-profile no-argument integration fixture explicitly uses `kind=stage_relay`, matching the existing production supervisor contract. Its reports, identities, and independent acceptance files remain synthetic test-only data and are not Maker product evidence. Production continues to reject `synthetic_stage_relay` at the installed no-argument entrypoint. R3 preserves all original 9 plus 6 no-argument tests; the changed R3 suite and both exact-head CI runs require independent execution before any installation. Operator-reported previous no-arg drill rc0 is not a substitute for the failed R2 CI.

## R5 business-schema bridge (not a new Maker runner)

Install unchanged sibling files `scripts/stage_relay.py` and `scripts/maker_stage_bridge.py` in the authorized active profile. The profile-local graph resides at `stage-relay/C0041/graph.json` and its evidence at `stage-relay/C0041/evidence`. This source-only PR does not install anything. No-argument relay dynamically loads only its sibling adapter when the graph has `maker_bridge`. Missing adapter/config/provenance blocks; never silently changes graph SHA or existing state. Operator must explicitly reconcile a changed frozen source/input/config through a new independently reviewed graph and initialized state, retaining archived old evidence; never edit an active graph in place.

The graph's `maker_bridge` must name `historical_stage` (mode `adopt`, not `exec`), `reflection_stage` (mode `adopt`), `producer` {item,job,instruction}, a relative `capacity_acceptance_path`, exact `source_sha256` business-code dictionary, `input_manifest_sha256`, `config_sha256`, `symbol_hashes` keyed BTCUSDT and ETHUSDT with bars/funding SHA256, absolute local `cwd`, `config_path`, `input_path`, `output_dir`, explicit nine-element `run_argv` and `verify_argv` corresponding to the **same** Maker backtest_entry.py invocation. The actual command shapes are:

    python3 backtest_entry.py --run --config LOCAL --input LOCAL --output NEWDIR
    python3 backtest_entry.py --verify --config LOCAL --input LOCAL --output EXISTINGDIR

These two shapes describe the external producer; the bridge invokes **only** configured `--verify`, never `--run`. The producer must be independently admitted, must preserve the fixed 5GiB cap and original frozen observation window/sample, and must never restart a prior MemoryError run. This adapter never invents private paths, observations or credentials.

The independently reviewed capacity acceptance is a JSON object at the configured evidence-relative location with `decision:"ACCEPTED"`, nonempty `reviewer`, exact `stage`, `source_head`, `input_sha256`, `graph_sha256`, `input_manifest_sha256`, `config_sha256`, `source_sha256` object, and `symbol_hashes` object. The graph must bind the accepted **business source**, not the adapter source. Missing/mismatched approval returns `WAITING_CAPACITY_ACCEPTANCE` plus actual configured producer item/job/instruction on every due scheduler invocation and **does not launch or adopt** a research process. A known terminal failure/UNKNOWN/REFUSED is allowed to be processed as a STOP, never treated as a success. Independent authenticity of acceptance and trusted parent is an operator responsibility.

After *an actual externally owned already-adopted* Maker process terminates, the trusted parent writes a terminal JSON under evidence with exact stage/source/input/graph and owner PID/start_ticks/boot_id/token, `outcome:"SUCCEEDED"`, `exit_code:0`, and `trusted_parent:true`. A missing PID is not proof of success. Operator then invokes the installed sibling CLI:

    python3 /ACTIVE/PROFILE/scripts/maker_stage_bridge.py capacity --config /ACTIVE/PROFILE/stage-relay/C0041/graph.json
    python3 /ACTIVE/PROFILE/scripts/maker_stage_bridge.py normalize --config /ACTIVE/PROFILE/stage-relay/C0041/graph.json --state /ACTIVE/PROFILE/stage-relay/C0041/state.json --terminal terminal/verified-parent.json

Normalization checks the source admission, exact trusted external owner, immutable local input/config, actual Maker `--verify` result, manifest input+source SHA, 289 distinct artifact checksums, all 288 strategy/matched account outputs (3 scenarios x 48 x 2), 48 unique frozen cell IDs in **each** scenario (BTC24 ETH24), and row/raw identity. It writes three 48-row reports with original metrics, account artifact hashes, per-symbol bar/funding hashes, business source hashes, config/input/graph/stage identities and a single bound receipt with `output_count:144`. This is *normalization*, not independent acceptance. The operator's independent reviewer then creates `inbox/<historical-stage>.acceptance.json` with exact stage/source/input/graph/receipt_sha256, decision ACCEPTED and reviewer. Only the existing scheduled no-argument inbox consumer may then advance once; duplicates, UNKNOWN or refusal must not resend.

The next stage emits `READY_OPERATOR_REFLECTION` with actual historical report hashes, source, producer and total output count. It does **not** fabricate trading conclusions or an operator decision. Operator reflection remains externally owned, and its real terminal decision artifact + independent accepted receipt are required to complete it. `EXPLORATORY_UNACCEPTED` classification of source results is preserved; verifying checksums is not economic or Maker-product acceptance. PR75 capacity candidate r13 and failed 5GiB historical run are **not accepted** based on this bridge.

This bridge is source-only. Additional offline tests are discovered by the already existing C0041 CI unittest step, no workflow change. Both exact-head GitHub checks and finite installed-layout integration require independent verification before any authorized operator installation.
