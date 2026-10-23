# Relay Stall Observer & Adapter Package

`engineering.relay_stall` is an observe-only detection, adaptation, and acknowledgement suite designed to monitor relay dispatch pipelines without active execution capabilities.

## Architecture

```
[External Read-Only Snapshot]
           │
           ▼
┌─────────────────────────────────┐
│     relay_stall_adapter.py      │
│  - Extracts explicit PR jobs    │
│  - Matches ledger governance    │
│  - Reads chat/head/mailbox      │
│  - Reads consumer heartbeat     │
└─────────────────────────────────┘
           │ (normalized.json)
           ▼
┌─────────────────────────────────┐
│      relay_stall_watch.py       │
│  - Evaluates 6 distinct states  │
│  - Deterministic head clock     │
│  - Atomic state.json/pending    │
│  - Appends fsynced events.jsonl │
└─────────────────────────────────┘
           │ (pending.json)
           ▼
┌─────────────────────────────────┐
│       Consumer Intake (ack)     │
│  - Evaluates governing decision │
│  - Writes consumer_journal.jsonl│
│  - Calls core ack (receipt_ref) │
│  - Writes consumer_runs.jsonl   │
└─────────────────────────────────┘
```

## Core Schema (`schema_version: 1`)

Normalized input format consumed by `relay_stall_watch.py`:

```json
{
  "schema_version": 1,
  "consumer": {
    "enabled": true,
    "last_receipt_at": 1700000000.0
  },
  "jobs": [
    {
      "repository": "example-org/sample-repo",
      "branch": "feature/example-pipeline",
      "instruction_id": "example-inst-001",
      "job_id": "PR101_example-inst-001",
      "expires_at": 1700003600.0,
      "owner": {
        "kind": "pid",
        "pid": 12345,
        "start_id": "proc-uuid-1001",
        "alive": true,
        "identity_ok": true
      },
      "head": "0123456789abcdef0123456789abcdef01234567",
      "chat": {
        "observed_at": 1700000000.0,
        "pending_card": false,
        "busy": false
      },
      "send_status": "CONFIRMED",
      "handback_valid": false,
      "policy_refusal": true,
      "receipt_ref": "00000000-0000-0000-0000-000000000000"
    }
  ]
}
```

## CLI Usage

### 1. Adapter: Produce Normalized Document
```bash
python3 -m engineering.relay_stall.relay_stall_adapter produce \
  --snapshot source-snapshot.json \
  --out normalized.json \
  --state-dir ./state
```

### 2. Core Watch: Detect Faults & Maintain State
```bash
python3 -m engineering.relay_stall.relay_stall_watch \
  --input normalized.json \
  --state-dir ./state \
  --now 1700000000.0
```

### 3. Consumer Intake: Journal & Acknowledge
```bash
python3 -m engineering.relay_stall.relay_stall_adapter consume \
  --state-dir ./state \
  --snapshot source-snapshot.json
```

### 4. Periodic Cron Wrapper
```bash
python3 -m engineering.relay_stall.relay_stall_cron \
  --config ./config.json
```

## State Invariants
- Observe-only: No sending, retrying, approving, clicking, or network write operations.
- Fail-open: Unparseable, corrupted, or missing inputs are classified as `UNKNOWN`.
- Clock preservation: `head_seen_at` updates strictly on genuine HEAD change. Missing HEAD preserves clock.
- Consumer runs: Successful consumer intake persistently writes durable run receipts into `consumer_runs.jsonl`.
