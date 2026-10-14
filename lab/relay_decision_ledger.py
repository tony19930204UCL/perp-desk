"""Durable, immutable operator decisions; no remote execution."""
import hashlib
import json
import os
from pathlib import Path
import time
import fcntl

STATUSES = frozenset(("ACCEPTED", "REJECTED", "BLOCKED"))


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unit_dir(root, unit_id):
    if not isinstance(unit_id, str) or not unit_id or len(unit_id) > 200:
        raise ValueError("invalid unit_id")
    return Path(root) / _digest(unit_id)


def _records(root, unit_id):
    folder = _unit_dir(root, unit_id)
    records = []
    if folder.exists():
        for path in folder.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("unit_id") != unit_id:
                raise ValueError("ledger unit mismatch")
            records.append(data)
    return sorted(records, key=lambda item: item["seq"])


def record_decision(ledger_dir, unit_id, head_sha, mailbox_blob, status, reason, evidence, expected_prev_seq=None):
    """Create once via exclusive filesystem creation; never overwrite a decision."""
    if status not in STATUSES:
        raise ValueError("invalid status")
    for value in (head_sha, mailbox_blob, reason):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("nonempty head, blob and reason required")
    if not isinstance(evidence, (str, dict, list)) or not evidence:
        raise ValueError("nonempty evidence required")
    folder = _unit_dir(ledger_dir, unit_id)
    key = _digest(_canonical([unit_id, head_sha, mailbox_blob]))
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = folder / ".ledger.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            existing = _records(ledger_dir, unit_id)
            previous = max((item["seq"] for item in existing), default=0)
            if expected_prev_seq is not None:
                if type(expected_prev_seq) is not int or expected_prev_seq != previous:
                    raise ValueError("expected_prev_seq mismatch")
            record = dict(schema_version=1, unit_id=unit_id, head_sha=head_sha,
                          mailbox_blob=mailbox_blob, status=status, reason=reason,
                          evidence=evidence, key=key)
            for item in existing:
                if item["key"] == key:
                    if all(item.get(k) == v for k, v in record.items()):
                        return item
                    raise ValueError("conflicting immutable decision")
            record["seq"] = previous + 1
            path = folder / ("%06d-%s.json" % (record["seq"], key))
            with path.open("x", encoding="utf-8") as stream:
                stream.write(_canonical(record) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return record
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def rework_instruction_id(unit_id, attempt):
    if type(attempt) is not int or attempt < 1:
        raise ValueError("attempt must be positive integer")
    return "rework-" + _digest(_canonical([unit_id, attempt]))[:24]


def next_action(ledger_dir, unit_id, max_revisions):
    if type(max_revisions) is not int or max_revisions < 0:
        raise ValueError("max_revisions must be nonnegative")
    records = _records(ledger_dir, unit_id)
    if not records:
        return {"action": "STOP_BLOCKED", "reason": "no decision"}
    rejected = [r for r in records if r["status"] == "REJECTED"]
    if len(rejected) > max_revisions:
        return {"action": "STOP_BLOCKED", "reason": "revision limit"}
    latest = records[-1]
    if latest["status"] == "ACCEPTED":
        return {"action": "ADVANCE"}
    if latest["status"] == "BLOCKED":
        return {"action": "STOP_BLOCKED", "reason": latest["reason"]}
    attempt = len(rejected)
    return {"action": "REWORK", "attempt": attempt,
            "instruction_id": rework_instruction_id(unit_id, attempt),
            "rejecting_evidence": latest["evidence"]}


def remote_reminder_data(text):
    """Return untrusted remote text as inert data, never as a command."""
    if not isinstance(text, str):
        raise ValueError("reminder must be text")
    return {"remote_reminder_text": text}
