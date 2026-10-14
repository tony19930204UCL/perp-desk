# Progress-Aware Acceptance Runner

Dedicated engineering acceptance tooling. Staged candidate for deterministic test execution monitoring without premature termination of progressing suites.

> [!IMPORTANT]
> **Engineering Only — Not Live Deployment**: Source files under `engineering/acceptance/` are copies staged for engineering validation. They do not alter live trading engines, databases, or deployment environments. `accept_unit.py` and `auto_accept.py` remain byte-identical in this unit.

---

## 1. Background & Architecture

The full lab suite contains 621–625 unit tests taking 900–1232 seconds. A fixed 14-minute (`subprocess.run(timeout=840)`) hard timeout prematurely terminates healthy test runs that are making steady progress.

`progress_runner.py` provides progress-aware execution observation:
- **Observational Soft Checkpoints (`--soft-seconds 840`, `--checkpoint-seconds 300`)**: An observation window (never a cutoff line). When tests are completing, execution continues under recurring observation windows with the same child PID and start identity. When no tests have completed in a window, stall is suspected and thread stacks and kernel wait channels (`wchan`) are recorded, but the child is **not** terminated.
- **Hard Suite Limit (`--hard-seconds 2400`)**: Enforces an absolute 40-minute ceiling across the entire suite. If reached, the process group is cleanly terminated with `TIMEOUT` and diagnostic traces.
- **Explicit Item Budget (`--item-seconds`)**: An optional explicit limit for individual tests. If an item exceeds this explicit budget, it terminates with `STALL`. High CPU alone is never mistaken for progress.
- **Process Group Isolation & Zombie-Free Termination**: Linux process groups (`os.setsid`) ensure bounded termination of the runner, worker, and any fixture descendants on timeout, stall, or cancellation. `stop_process_group` reaps owned `Popen` handles during grace so zombie states never delay termination.
- **Continuous Unbuffered Streams**: Worker runs unbuffered (`python3 -u`) and writes real-time JSONL events (`events.jsonl`) with flush and `os.fsync`. Raw `stdout.log` and `stderr.log` stream continuously to disk.
- **Faulthandler Diagnostics**: `faulthandler` is enabled on `sys.stderr` and registered to `SIGUSR1`. On inactivity or item timeout, stack traces of all threads are dumped to `stderr.log` for immediate diagnosis.
- **Flock Deduplication & Restart Recovery**: Persists exact configuration keys (canonical SHA256 of head, suite, cwd, and budgets). Concurrent invocations with the same key wait and reuse the running handle; completed results with the exact key are reused; changed head/suite/budget never silently reuses. Restart recovery detects live orphan child/owner processes and fails closed, retaining elapsed run deadlines without resetting clocks.
- **ID Reconciliation & Lifecycle Integrity**: Discovered test IDs are counted and reconciled against completed IDs. Incomplete suites, missing discovery, missing `suite_completed` events, or subtest failures cannot pass.
- **Unittest Contract Support**: `--buffer` (`-b`) and `--failfast` (`-f`) options are supported and propagated. Unsupported arguments are rejected strictly rather than claiming false contract identity.

---

## 2. CLI Options & Exit Codes

```text
progress_runner.py [OPTIONS]

Options:
  --cwd PATH               Working directory for test suite execution (default: .)
  --head SHA               Pinned commit SHA (required)
  --suite ARGS             Unittest suite arguments (default: "discover -s tests")
  --state-dir PATH         State directory for locks, logs, and events (default: .progress_runner)
  --soft-seconds SECONDS   Initial soft checkpoint deadline in seconds (default: 840)
  --checkpoint-seconds SEC Checkpoint observation interval in seconds (default: 300)
  --hard-seconds SECONDS   Absolute total suite hard deadline in seconds (default: 2400)
  --item-seconds SECONDS   Optional explicit budget for a single test item
  --stall-seconds SECONDS  Optional inactivity budget before suspecting stall
  --out PATH               Path to write final JSON report
```

### Exit Codes & Verdicts
- `0`: `PASS` — All discovered tests ran and passed (skips reported distinctly; subtests passed; suite complete).
- `1`: `FAIL` — Real assertion failure, error, subtest failure, empty suite, or incomplete suite execution.
- `2`: `TIMEOUT` — Suite hard deadline exceeded.
- `3`: `STALL` / `STALL_SUSPECTED` — Test item budget exceeded.
- `4`: `CANCELLED` — Process terminated by `SIGTERM` or `SIGINT`.

---

## 3. Independent Fixture Commands

To run the independent acceptance test suite (21 real subprocess fixture tests covering slow progress, long tests, stalls, busy loops, buffer, descendant cleanup, dedupe, concurrent handle reuse, signal cancellation, orphan detection, subtests, failfast, and skips):

```bash
python3 -m unittest lab/tests/test_acceptance_progress_runner.py -v
```

To run `progress_runner.py` directly against an isolated fixture:

```bash
python3 engineering/acceptance/progress_runner.py \
  --cwd . \
  --head $(git rev-parse HEAD) \
  --suite "engineering.acceptance.fixture_suites.SlowProgressSuite" \
  --state-dir .scratch/fixture-state \
  --soft-seconds 30 \
  --checkpoint-seconds 10 \
  --hard-seconds 120 \
  --out .scratch/fixture-state/result.json
```

---

## 4. Full Real Lab Command (840s Soft / 2400s Hard)

To execute the complete lab test suite under the 40-minute hard ceiling and 14-minute soft checkpoint:

```bash
python3 engineering/acceptance/progress_runner.py \
  --cwd lab \
  --head $(git rev-parse HEAD) \
  --suite "discover -s tests" \
  --state-dir .scratch/lab-progress-state \
  --soft-seconds 840 \
  --checkpoint-seconds 300 \
  --hard-seconds 2400 \
  --out .scratch/lab-progress-state/verdict.json
```

---

## 5. Checkpoint Evidence & Verification

Inspect the generated artifacts in `--state-dir`:
1. `state.json`: Check the `checkpoints` array. Each entry records:
   - `child_pid`: Verifies child PID remains identical across soft checkpoints.
   - `reason`: `progress_observed`, `legitimate_long_test_within_budget`, or `stall_suspected`.
   - `old_observation_deadline` and `new_observation_deadline`.
   - `completed_count` vs `discovered_count`.
   - `last_completed_id` and `current_id`.
   - Continuous state snapshots ensure `discovered_ids`, `completed_ids`, `current_id`, and `last_progress_monotonic` are persisted from initial discovery onwards.
2. `events.jsonl`: Real-time lifecycle entries for `discovered`, `start`, `subtest`, `end`, and `suite_completed`.
3. `stdout.log` / `stderr.log`: Continuous unbuffered output.
4. `diagnostics`: If stalled or timed out, inspect the captured thread stack trace and kernel wait channel evidence (`wchan`).

---

## 6. Deployment Preflight & Rollback Guide

### Preflight Verification
1. Ensure the pinned `--head` SHA strictly matches the target review commit:
   ```bash
   git rev-parse HEAD
   ```
2. Verify independent test suite passes cleanly:
   ```bash
   python3 -m unittest lab/tests/test_acceptance_progress_runner.py
   ```
3. Confirm that `--state-dir` is isolated, clean, and writable with `flock` capability on Linux filesystem.

### Rollback Plan
- `accept_unit.py` and `auto_accept.py` are kept untouched and byte-identical in this unit.
- If operational rollback is required, remove or disregard `engineering/acceptance/progress_runner.py` and `engineering/acceptance/unittest_worker.py`. Callers continue using standard `unittest` subprocess invocations.
- No database migrations, daemon configurations, or systemd services are created.
