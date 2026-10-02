# Parent acceptance and deployment commands (NOT run by child)

Candidate root `C=/home/chihcheng/.hermes/profiles/perp-desk/learning-speed-v3`
Original live root `L=/home/chihcheng/.hermes/profiles/perp-desk/lab`
Branch `feat/paper-learning-speed-v3`, base `27be995f404df34d7d8a4e85294a8353d804690a`.

## Independently rerun acceptance

From `$C/lab`, run each as a separate foreground command with 290s allowance:

```sh
python ../evidence/run_learning_suite.py 1
python ../evidence/run_learning_suite.py 2
python ../evidence/run_learning_suite.py 3
python ../evidence/run_learning_suite.py 4
python ../evidence/run_learning_suite.py 5
python ../evidence/run_learning_suite.py 6a
python ../evidence/run_learning_suite.py 6b
python ../evidence/run_learning_suite.py 6c
python ../evidence/run_learning_suite.py 6d
python ../evidence/run_learning_suite.py 7
python ../evidence/run_learning_suite.py 8
python ../evidence/run_learning_suite.py 9
python ../evidence/run_learning_suite.py 10
```

Discovery currently 199 unique IDs (155 original + 10 imported batch-time + 21 imported sizing + 13 new). The current 20-ID batch6 is split into four five-ID batches. Parent re-run logs overwrite named acceptance logs, so first preserve child evidence or use a parent worktree/copy. Child initial batch1 failed because an imported captured-source fixture was missing, then supplied the exact saved fixture, not altered assertions. Initial batch6 timed out at290s; excluded. Child final precision-only V3 metrics update has all13 feature IDs reverified, selected instead of earlier feature executions. Exact no-missing/no-duplicate accounting is in `evidence/learning_suite_validation.json`.

Use a NEW **probe-only** parent namespace, never replace the account with it:

```sh
python ../evidence/probe_learning_v3.py --state-dir data/parent-v3-probe --status shared/parent_v3_probe.json --label parent_v3_probe
```

This performs six real public polls with one restart, read-time freshness, source hashes, full audit-chain and cash replay validation. Probe initializes 100 only in its isolated namespace. Probe returns zero trades honestly if no qualified signal occurs. It is not performance evidence or the deployment window.

## Stop/migrate only after acceptance

No child deployment. Keep the candidate worktree available for the running process. Do not checkout or overwrite the DIRTY `engineering` branch. Starting the candidate FROM its worktree while pointing to the **original account directory** avoids modifying live source. Dashboard can keep reading its existing live JSON target; no fixture status should ever be put there.

Before stopping, read original broker state with read-only SQLite and preserve its whole JSON (cash, initial cash, day baselines, orders, fills, ledger, marks/funding and audit). Check current process identity explicitly:

```sh
C=/home/chihcheng/.hermes/profiles/perp-desk/learning-speed-v3
L=/home/chihcheng/.hermes/profiles/perp-desk/lab
python -c "from pathlib import Path; p=Path('/proc/2621427'); assert str((p/'cwd').resolve())=='$L'; assert (p/'cmdline').read_bytes().split(b'\0')[:-1]==[b'python',b'-u',b'paper_runtime_v2.py',b'--state-dir',b'data/paper-v2',b'--status',b'shared/paper_v2_live.json']; print('exact old PAPER process verified')"
# PARENT ONLY, after accepted independent tests/probe and account precheck:
kill -INT 2621427
# Wait for THAT process to exit. Do not kill an unrelated PID or proceed while locks remain.
```

Use a fresh, unique backup directory. Migration acquires BOTH runtime and broker locks, fails on non-flat/pending account, checks exact old/new config and cash-ledger identity, saves all four SQLite databases, atomically appends an audited migration with previous full runtime state. Broker DB is not rewritten. Initial capital, account version/forward start, all old orders/fills/ledger, risk baselines/halts and runtime audit prefix persist. New signal registration/window starts at actual migration time. A losing account stays losing. Non-flat/pending is a blocker, NOT permission to wipe it.

```sh
python "$C/lab/paper_migrate_v3.py" --apply --state-dir "$L/data/paper-v2" --backup-dir "$L/data/pre-v3-parent-backup" --config "$C/lab/paper_config_v3.json"
python "$C/lab/paper_runtime_v3.py" --once --state-dir "$L/data/paper-v2" --status "$L/shared/paper_v2_live.json"
python "$C/lab/accept_learning_v3.py" --state-dir "$L/data/paper-v2" --status "$L/shared/paper_v2_live.json" --backup-dir "$L/data/pre-v3-parent-backup" --out "$C/evidence/parent_v3_account_acceptance.json"
```

Only after all checks pass, parent persists the deployment marker and publishes a fresh snapshot:

```sh
cd "$C/lab"
python -c "from paper_runtime_v3 import PaperRuntime; from paper_runtime_v2 import publish; r=PaperRuntime('$L/data/paper-v2','paper_config_v3.json'); s=r.poll(); assert not s['latest_error']; r.mark_deployed(); s=r.snapshot(); publish(s,'$L/shared/paper_v2_live.json'); print(s['candidate_not_deployed'],s['research']); r.close()"
# Launch long-term with the parent's managed process tool, not this child:
python -u "$C/lab/paper_runtime_v3.py" --state-dir "$L/data/paper-v2" --status "$L/shared/paper_v2_live.json"
```

Verify exact new PID/cwd/argv, single account runtime, fresh status/heartbeat/source ages, engine H1-PAPER-003, candidate_not_deployed=false, immutable config/source hashes, previous account prefixes and risk baselines, audit and ledger equality. Recheck actual dashboard Windows HTTP/browser/API. The old dashboard does not add research fields automatically. Parent should use the new brief command for the already-existing daily job, list/exact-ID update/read-back, and check live work status before marking completed:

```sh
python "$C/lab/brief_learning_v3.py" --status "$L/shared/paper_v2_live.json"
```

The brief is eight lines, separates cumulative account from new-window round trips/costs, includes rejection categories and learning limitation. No cron or dashboard was changed by this child.

## Rollback / blocker handling

Keep old live source and all backup DBs. A failed first probe must retain the migrated account's original cash/ledger; eagerly restored broker state prevents falsely showing a fresh100 on network failure. Do not reset a window to hide failures. If rollback is needed, stop/verify the exact candidate first. Restore ALL backed-up DBs only if no post-migration trading/funding/account change occurred. Otherwise preserve the current account and do a separately reviewed continuity migration, never overwrite new fills or losses with the backup. No automatic rollback is supplied because it would risk erasing outcomes.

Source hashes, candidate config hash and exact source diff hash are in `evidence/candidate_manifest.json`. The complete original source snapshot manifest is preserved unchanged, not advertised as the candidate manifest.
