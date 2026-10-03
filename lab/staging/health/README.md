# PAPER operational health watchdog (historical v2 candidate)

> Historical evidence only. This document describes the pre-v3 staged design and must not be used as the current deployment/scheduling contract. In particular, do **not** schedule the historical autonomous engineering-gate proposal below. Current PAPER v3 integration and operator-only Issue handoff rules are in `docs/HEALTH_V3_OPERATOR.md`.


No live runtime, strategy, risk configuration, trading SQLite, existing served dashboard or cron configuration was changed. The only live writes are the separate health report/incident namespace and genuine reliability-watchdog checkpoints through work_status.py. This is not yet persistent scheduled coverage.

## Ready commands

From `/home/chihcheng/.hermes/profiles/perp-desk/lab`:

```sh
python health_watchdog.py --once
python /home/chihcheng/.hermes/profiles/perp-desk/scripts/paper_health_monitor.py
python health_watchdog.py --link paper-v2:runtime-error --task batch-time-recurrence
```

The explicit link command requires an existing noncompleted task. It never creates work, marks work completed or claims execution. Additional related incidents can be linked using their exact IDs from shared/health_status.json.

- Inputs (read only): shared/paper_v2_live.json, shared/work_status.json, exact /proc cwd/cmdline, stat sizes of data/paper-v2, filesystem free space.
- Outputs: shared/health_status.json, data/health/incident_state.json and its advisory lock. The engineering gate additionally writes data/health/engineering_gate.json and its lock.
- An exact deployed H1-PAPER-002 PAPER v2 identity and candidate_not_deployed=false are required. Financial/schema validation delegates to the unchanged dashboard validator. Missing/malformed/wrong-identity data cannot be healthy.
- Operational snapshot/last-success/raw source/raw receipt freshness: 60 seconds, with future timestamps rejected. These are monitoring thresholds, not trading permission. The engine's stricter 15000 ms decision gate and all trading rules remain untouched.
- Active work updates older than 300 seconds are activity-unconfirmed; other nonterminal work older than 3600 seconds is overdue. current_step is never treated as execution proof.
- Storage alert thresholds: 512 MiB total files in data/paper-v2, or 1 GiB free filesystem space. Monitoring only alerts; it never prunes trading evidence or stops collection.
- Runtime discovery handles the actual `python -u paper_runtime_v2.py` invocation, requiring exact cwd/script/namespace/status arguments. Zero or multiple matching processes are unhealthy; no process is started or stopped.

## Incident lifecycle / delivery

Stable incident IDs are `paper-v2:<fault-kind>`. First_seen means the watchdog's first actual observation, not an invented original failure time. Latest observed evidence includes original error, raw snapshot/source/receipt timestamps and counters. Duplicate failure ticks update observation evidence but do not duplicate incidents or engineering tasks.

Three consecutive clear observations move open -> recovered_monitoring. They NEVER resolve engineering work. Explicit parent acceptance with existing tested-resolution evidence is required for resolved. A recurrent fault reopens the same ID. Parent API:

```python
from datetime import datetime, timezone
from pathlib import Path
from health_watchdog import resolve_incident
resolve_incident(Path('data/health/incident_state.json'), 'paper-v2:FAULT_ID',
    {'tested_resolution': True, 'accepted_by_parent': True,
     'evidence_paths': ['evidence/ACTUAL_PASSED_TEST_LOG']},
    datetime.now(timezone.utc))
```

Do not use this API for runtime-error merely because latest_error becomes null or a repair task is completed.

Error rate is unavailable on the first observation. Subsequent reports include the actual previous count/time baseline, delta, elapsed seconds and rate per minute. Counter decrease/clock regression triggers counter-reset, not a fabricated negative rate.

Stdout contains only stable meaningful changes (incident status/error/message/owner or pending-task state/unconfirmed changes). checked_at and observation ages do not create delivery spam. Normal operational faults and handled unavailable state are data, so successful collection returns 0. Unexpected script/infrastructure errors remain nonzero. A corrupt incident file is preserved; a separate unavailable health report is published and its repeated alert is deduplicated.

Samples are bounded independently of trading evidence: latest 240 real tick samples plus 720 actual hourly aggregate buckets. No nonexistent hours or fake heartbeat are inserted. Gate generation history is capped at 512 tokens. Samples are persistent health telemetry, not performance/trading evidence.

## Staged dashboard

Only `staging/health/dashboard.py` and `staging/health/dashboard.html` were changed. Exact patch is `staging/health/dashboard_health.diff`. Parent must validate base hashes, accept/apply the patch, then restart only the verified dashboard process. Do not restart trading for this change.

`/api/health` reads a fixed shared/health_status.json independent of /api/status and /api/work. Missing/malformed/future data returns 503; report age >180 seconds is recomputed on every read and forces operational_healthy=false. Host, Origin, cross-site, traversal and read-only method protections apply to all routes. Market failure does not hide work or health.

Top panel is 現在健康／未解問題. Actual errors and open/recovered-monitoring incidents appear separately from completed job history. All external values use textContent. A recovered runtime can still show unresolved engineering incidents. No hardcoded healthy fixture is served.

Staged standalone QA command (do not deploy until parent acceptance):

```sh
PYTHONPATH=/home/chihcheng/.hermes/profiles/perp-desk/lab python staging/health/dashboard.py \
  --port PORT_CONFIRMED_FREE_BY_PARENT \
  --status /home/chihcheng/.hermes/profiles/perp-desk/lab/shared/paper_v2_live.json \
  --html /home/chihcheng/.hermes/profiles/perp-desk/lab/staging/health/dashboard.html
```

The tests exercise real ephemeral HTTP servers and executed Node VM UI rendering. Parent still owns final Windows/browser deployment acceptance.

## Parent-only schedule suggestions (NOT CREATED)

Official cron documentation was retrieved into evidence/health_cron_docs.html. Scripts must live within this profile's HERMES_HOME/scripts. Parent must first list existing jobs, use exact IDs to update instead of duplicating, then read back and manually exercise accepted jobs.

1. Deterministic monitor: name `perp-paper-operational-health`, schedule `2m`, script `paper_health_monitor.py`, no_agent=True, workdir `/home/chihcheng/.hermes/profiles/perp-desk/lab`, delivery to the current perp-desk conversation. Empty stdout is silent. This keeps the health report within the 180-second API stale budget while the scheduler remains online.
2. Engineering gate: name `perp-paper-engineering-response`, schedule `5m`, script `paper_health_engineering_gate.py`, no_agent=False, same workdir, prompt below. Last-line JSON wakeAgent=false suppresses inference. Gate emits one wake per newly observed, unowned critical evidence generation. Known active repair IDs or explicitly linked recent active tasks suppress duplicate dispatch. This is ownership coordination, NOT proof that the task really has a live execution handle. Gate never starts repairs or edits code itself.

Gate invocation:

```sh
python /home/chihcheng/.hermes/profiles/perp-desk/scripts/paper_health_engineering_gate.py
```

Do not manually consume a production wake without handling it. Fixture tests exercise the gate, while the actual public live probe exercises the monitor. A wake records an attempted dispatch, not successful engineering completion. If a cron agent fails, parent must inspect the scheduler failure incident and unresolved overview; identical faults do not endlessly respawn agents. WSL/gateway offline still prevents scheduling/delivery, and neither job is an autostart service.

### Self-contained engineering cron prompt

你是 perp-desk 的 PAPER 工程值班代理。只在 script context 列出的新／未擁有 critical incident 上工作。先載入 perp-shadow-lab 與 test-driven-development，讀本 profile 的 AGENTS.md、SOUL.md、versions.md、shared/health_status.json、shared/work_status.json。不要因工程清單 completed 或 latest_error=null 把事件結案。

重新核對 incident 仍 open、實際最新 fault/evidence、linked_task_id、已知 repair task、更新年齡，以及可取得的真實執行 handle／PID／證據。相同問題已有真實執行中的修復就不重派，不用 current_step 或 watcher 更新當執行證明。queued 任務可重用，completed 舊任務必須保留。若已有 stale running task，先診斷其實際 handle，無執行證據就誠實標 blocked／queued，不造 heartbeat。一次最多接手一個相同根因的 critical 問題。

確定需要新調查時，先用 work_status.py CLI 登記穩定 task ID，使用既有對應未完成任務，並用 health_watchdog.py --link 顯式連結相關 incident。只有立即開始真實工具命令時才標 running。立刻執行一個有界（120 秒內）的 staged 調查，讀原始公開 live snapshot、source/receipt 時序、exact process identity、storage stat 或 read-only audit 證據，寫到本 profile lab/staging/health-investigation/<task-id>/ 與 evidence/。有修復需求先寫重現測試，親跑 RED，再只修改隔離 staged 候選，親跑 GREEN。不改 live trading modules/config/SQLite，不送真單、不碰憑證或其他 profile、不停止／重啟 runtime/dashboard、不改 cron。

長測試或正式部署交父代理驗收。工具確實失敗就標 failed／blocked並保留證據，不用合理-looking output代替。回報真實已执行步驟／證據與下一步。交付標 verifying，不能自行 completed 或 resolved。恢復觀察不是根因解決，父代理完成測試與部署讀回才可工程結案。沒有新動作時不發例行成功／重複故障通知。

## Probe correction that parent must acknowledge

The first developmental live probe incorrectly failed to recognize the existing python -u invocation and created paper-v2:runtime-absent. This was an observer identity bug, NOT evidence of an actual runtime outage. A new RED/GREEN real-process regression fixed it; subsequent probes confirm PID 2621427. The original observation is retained rather than erased. Once recovered_monitoring, parent may explicitly resolve this false-positive detection incident using health_green12.txt/health_green16.txt plus the corrected real-process readback. Do not use that resolution to close genuine batch-source-age incidents.

Actual fault probe at 2026-10-02T06:15:02.854186+00:00: errors_count=856, latest_error='ValueError: batch source age exceeded', connected=false, exact live PID 2621427. Monitor registered runtime-error/feed-disconnected/error-growth and independently reported overdue work. The baseline was errors_count=842 at 06:10:35.345201 UTC, not an invented rate. Raw snapshot, monitor output and command stdout are preserved in evidence/health_live_probe_samples.json.
