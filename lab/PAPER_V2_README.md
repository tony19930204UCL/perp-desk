# H1-PAPER-002 staged candidate (PAPER ONLY, not deployed)

Working directory: `/home/chihcheng/.hermes/profiles/perp-desk/lab`.

## Verified delivery

- Six vertical RED/GREEN cycles: `evidence/v2_red1.txt` through `v2_red6.txt`, corresponding GREEN files.
- Full discovery inventory: 136 unique tests, all exercised exactly once across successful batches (123 + 6 + 7). `evidence/v2_suite_inventory.json`, `v2_full_batch1.txt`, `v2_full_batch2_retry.txt`, `v2_full_batch3.txt`, `v2_full_suite_validation.json`. Failed first isolated old-runtime batch retained as `v2_full_batch2.txt` (runner needed explicit importlib.util, no original files changed).
- Actual Binance public once: `evidence/v2_public_once_stdout.json`, stderr and exit files. Exit 0 at 2026-10-01T21:17:02.338000+00:00, observing 61/61, initial cash/equity 100, no signals/orders/fills/ledger. This is engineering readiness, not performance.
- Candidate `shared/paper_v2_candidate.json`, durable `data/paper-v2/`. `candidate_not_deployed=true`. Existing snapshot schema requires implementation='paper-engine-v1', so candidate_implementation separately identifies v2. Source hashes and version_id distinguish it unambiguously. Do not infer deployment from paper_trading_enabled=true in the candidate.
- `evidence/v2_public_validation.json`: schema, 12-event complete audit chain, 11 real public receipts, history receipt/bar hashes, 61 closed contiguous source bars strictly before activation, no historical intents/performance, asset equality and old-source immutability validated.

## Commands

```sh
python paper_runtime_v2.py --once --state-dir data/paper-v2 --config paper_config_v2.json --status shared/paper_v2_candidate.json
PYTHONPATH=tests:. python -m unittest test_paper_v2 -v
python evidence/v2_verify.py
```

`v2_verify.py` checks the original zero-trade one-point probe. Once the candidate is exercised again, additional real snapshot points or actual future fills can legitimately invalidate that initial-probe assertion. Preserve original evidence, do not erase evolved state to make it pass. For repeat/parent acceptance, validate the evolved state against its actual ledger and newly audited cutoff.

Full suite in medium stable batches (avoids tool's 420s single-call cutoff):

```sh
# Use committed discovery inventory, group indices 0, 1, 2 in separate calls.
PYTHONPATH=tests:. python -c "import unittest,json,sys,importlib.util; from pathlib import Path; group=json.loads(Path('evidence/v2_suite_inventory.json').read_text())['groups'][0]; result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(group)); sys.exit(not result.wasSuccessful())"
```

Set the group index to 1 and 2 for the other batches. Current tests can also be run with `python -m unittest discover -s tests -v` if the execution environment allows sufficient time. Do not call a timed-out run a success.

## Activation and continuity

Preregistration precedes actual candidate activation: versions.md at 2026-10-01T21:15:51Z, persisted activation 21:16:54.853 UTC. Seed baseline ends at 21:15 UTC. First theoretically eligible fresh close is 21:20 UTC. The candidate is NOT running continuously and this does not promise a 21:20 trade. Restart after >60s necessarily creates a new decision cutoff/context epoch before it can consider the next fresh close. Old activation/forward window remains recorded and no historical/offline signal becomes a trade.

Parent must independently verify, then stop only the exact old runtime and read old account again before a switch. At 21:18:35.976 UTC read-only check: old H1-PAPER-001 warmup 16/61, cash/equity100, no fills/ledger/positions/pending orders, day baseline100, no daily/total halt. This is a point-in-time precheck, not authorization to ignore a later trade. Any historical fill/ledger or loss counter is a migration blocker, not grounds to reset equity. Preserve old database/results and record continuity explicitly. Parent can switch the dashboard to the candidate namespace after acceptance rather than rewriting old shared/paper_status.json.

## Operational correction

See `evidence/v2_timestamp_failure_actual.json`, `v2_timestamp_semantics.md`, and RED/GREEN 6. Actual ETH depth source E was 57ms ahead of local receipt. v2 retains source E and receipt time, dispatches at the actual local current clock, and still rejects any source future at dispatch or older than15000ms. Broker's source-after-order-arrival requirement remains unchanged. No execution module other than staged runtime/signals is altered. Current official USD-M REST document returned empty202. Official indexed COIN-M REST/USD-M WebSocket semantics were retrieved, full USD-M REST page was not.

## Frozen hashes

- paper_config_v2.json: `dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96`
- paper_runtime_v2.py: `d820e98547947b50d68ec1c7bdb98caa27c8db6ad3336a1c8656b13d5876c80e`
- signals_v2.py: `b76a70cceb780488c49706a9db6d62ee678a064d19b7145f42886f2245df0064`

Real-fund trading remains disabled. No persistent process was started or stopped by this upgrade. No source rule, cost or risk limit was relaxed to force trades. Strategy edge remains unproven. SQLite retention/capacity monitoring and REST missed-tick limitations remain inherited operational risks.
