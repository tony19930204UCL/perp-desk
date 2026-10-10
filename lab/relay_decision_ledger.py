"""Durable, immutable operator decisions; no remote execution."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

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
    return records


def record_decision(ledger_dir, unit_id, head_sha, mailbox_blob, status, reason, evidence):
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
    record = dict(schema_version=1, unit_id=unit_id, head_sha=head_sha,
                  mailbox_blob=mailbox_blob, status=status, reason=reason,
                  evidence=evidence, key=key)
    serialized = _canonical(record) + "\n"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + ".json")
    # Heads are ordered by their first observed decision, not SHA lexical order.
    # Once another head is observed, an older head cannot be newly decided.
    existing = _records(ledger_dir, unit_id)
    for item in existing:
        if item["key"] == key:
            if item == record:
                return item
            raise ValueError("conflicting immutable decision")
    if any(item["head_sha"] == head_sha for item in existing):
        pass  # Another mailbox blob on the current head is permitted.
    elif existing and any(item["head_sha"] != head_sha for item in existing):
        # A SHA alone has no chronology: callers must supply ancestry evidence.
        # Fail closed rather than guessing which unrelated SHA is newer.
        raise ValueError("new head requires explicit ordering; stale head refused")
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != record:
            raise ValueError("conflicting immutable decision")
        return current
    return record


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
    if len({r["status"] for r in records}) > 1:
        return {"action": "STOP_BLOCKED", "reason": "ambiguous decision history"}
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
