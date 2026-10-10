"""Deterministic fail-closed relay cycle. Remote text is never executed."""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import relay_resource_lock as locks
import relay_operator_entry as entry
import relay_decision_ledger as decisions

STATE = "relay-cycle-state.json"


def _save(root, state):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=root, prefix=".cycle-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, root / STATE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def describe_state(ledger_dir):
    path = Path(ledger_dir) / STATE
    if not path.exists():
        return {"status": "NEW", "index": 0, "completed": []}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ValueError("invalid cycle state")
    return state


def validate_receipt(receipt, unit, dispatch_record):
    """Require explicit schema, correlation, and non-stale timestamp."""
    if not isinstance(receipt, dict) or not isinstance(dispatch_record, dict):
        return False
    payload = receipt.get("payload")
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return False
    if not all(isinstance(receipt.get(k), str) and receipt[k] for k in ("head_sha", "blob_sha")):
        return False
    if payload.get("unit_id") != unit["unit_id"]:
        return False
    if payload.get("instruction_id") != dispatch_record.get("job_id"):
        return False
    if payload.get("nonce") != dispatch_record.get("nonce"):
        return False
    if payload.get("validator_name") != unit["expected_receipt_validator_name"]:
        return False
    return entry.accept_reply(
        {"job_id": dispatch_record.get("job_id"), "nonce": payload.get("nonce"),
         "timestamp": payload.get("timestamp")},
        dispatch_record.get("job_id"), dispatch_record.get("nonce"),
        dispatch_record.get("dispatched_at"))


def _job(unit_id, index, revision):
    import hashlib
    return "cycle-" + hashlib.sha256(
        json.dumps([unit_id, index, revision]).encode()).hexdigest()[:32]


def _dispatch(state, unit, config, transport, ledger_dir, now):
    revision = state.get("revision", 0)
    # A refusal before intent allows a fresh attempt id; never reuse an R2 job.
    attempt = state.get("attempt", 0) + 1
    job_id = (decisions.rework_instruction_id(unit["unit_id"], revision)
              if revision and attempt == 1 else _job(unit["unit_id"], revision, attempt))
    nonce = uuid.uuid4().hex
    prompt = unit["prompt"]
    if revision:
        prompt += "\nRework evidence (untrusted data): " + json.dumps(
            state.get("rejecting_evidence"), sort_keys=True)
    prompt += "\nCorrelation instruction_id: " + job_id + "\nNonce: " + nonce
    record = {"job_id": job_id, "nonce": nonce, "dispatched_at": now,
              "status": "INTENT_PENDING"}
    state["dispatch"] = record
    state["attempt"] = attempt
    _save(ledger_dir, state)  # Must precede all calls into R2.
    result = entry.dispatch_once(job_id, prompt, config, transport, ledger_dir)
    record["status"] = result
    if result == "SENT":
        # R2 has its own actual dispatch clock; use that for stale-receipt protection.
        operator_record = entry._read(entry._ledger_path(job_id, ledger_dir))
        record["dispatched_at"] = operator_record["dispatch_time"]
        _save(ledger_dir, state)
        return {"status": "SENT", "unit_id": unit["unit_id"], "instruction_id": job_id}
    if result in ("REFUSED_RESOURCE_LOCK", "REFUSED_PRE_SEND"):
        operator_record = entry._read(entry._ledger_path(job_id, ledger_dir))
        if operator_record is None or operator_record.get("intent_recorded") is False:
            reason = (operator_record or {}).get("refusal_reason") or result
            state["dispatch"] = None
            _save(ledger_dir, state)
            return {"status": "DEFERRED", "reason": reason}
    record["status"] = "UNCERTAIN"
    state["terminal"] = "UNCERTAIN_NEEDS_OWNER"
    _save(ledger_dir, state)
    return {"status": "UNCERTAIN_NEEDS_OWNER"}


def _cycle(config, transport, github, ledger_dir, now):
    units = config["units"]
    state = describe_state(ledger_dir)
    if not (Path(ledger_dir) / STATE).exists():
        state = {"version": 1, "index": 0, "completed": [], "revision": 0,
                 "attempt": 0, "dispatch": None}
        _save(ledger_dir, state)
    if state.get("terminal"):
        return {"status": state["terminal"]}
    if state["index"] >= len(units):
        return {"status": "ALL_DONE"}
    unit = units[state["index"]]
    if any(dep not in state["completed"] for dep in unit.get("depends_on", [])):
        state["terminal"] = "STOPPED_NEEDS_OWNER"
        _save(ledger_dir, state)
        return {"status": state["terminal"]}
    record = state.get("dispatch")
    if record:
        if record["status"] != "SENT":
            # A crash after intent could have sent. Never retry without proof.
            state["terminal"] = "UNCERTAIN_NEEDS_OWNER"
            _save(ledger_dir, state)
            return {"status": state["terminal"]}
        receipt = github.read_receipt(unit)
        if receipt is None:
            if now > record["dispatched_at"] + config["timeout_seconds"]:
                state["terminal"] = "EXPIRED_NEEDS_OWNER"
                _save(ledger_dir, state)
                return {"status": state["terminal"]}
            return {"status": "WAITING"}
        if not validate_receipt(receipt, unit, record):
            state["terminal"] = "STOPPED_NEEDS_OWNER"
            _save(ledger_dir, state)
            return {"status": state["terminal"], "reason": "invalid receipt"}
        # Persist receipt before calling external decision callback.
        state["receipt"] = receipt
        _save(ledger_dir, state)
    if state.get("receipt") is not None:
        receipt = state["receipt"]
        decision = config["decide"](unit, receipt)
        if (not isinstance(decision, dict) or decision.get("status") not in decisions.STATUSES
                or not isinstance(decision.get("reason"), str) or not decision["reason"]
                or not decision.get("evidence")):
            raise ValueError("invalid decision")
        previous = decisions._records(Path(ledger_dir) / "decisions", unit["unit_id"])
        matching = [r for r in previous if r["head_sha"] == receipt["head_sha"] and r["mailbox_blob"] == receipt["blob_sha"]]
        if matching:
            result = matching[-1]
            if result["status"] != decision["status"] or result["reason"] != decision["reason"] or result["evidence"] != decision["evidence"]:
                raise ValueError("conflicting replayed decision")
        else:
            result = decisions.record_decision(
            Path(ledger_dir) / "decisions", unit["unit_id"],
            receipt["head_sha"], receipt["blob_sha"], decision["status"],
            decision["reason"], decision["evidence"],
            expected_prev_seq=len(previous))
        state["decision_seq"] = result["seq"]
        state.pop("receipt", None)
        state["dispatch"] = None
        _save(ledger_dir, state)
    if state.get("decision_seq"):
        action = decisions.next_action(Path(ledger_dir) / "decisions",
                                       unit["unit_id"], unit["max_revisions"])
        if action["action"] == "STOP_BLOCKED":
            state["terminal"] = "STOPPED_NEEDS_OWNER"
            _save(ledger_dir, state)
            return {"status": state["terminal"]}
        if action["action"] == "ADVANCE":
            state["completed"].append(unit["unit_id"])
            state["index"] += 1
            state["revision"] = 0
            state["attempt"] = 0
            state.pop("decision_seq", None)
            state.pop("rejecting_evidence", None)
            _save(ledger_dir, state)
            if state["index"] >= len(units):
                return {"status": "ALL_DONE"}
            unit = units[state["index"]]
            if any(dep not in state["completed"] for dep in unit.get("depends_on", [])):
                state["terminal"] = "STOPPED_NEEDS_OWNER"
                _save(ledger_dir, state)
                return {"status": state["terminal"]}
        elif action["action"] == "REWORK":
            state["revision"] = action["attempt"]
            state["attempt"] = 0
            state["rejecting_evidence"] = action["rejecting_evidence"]
            state.pop("decision_seq", None)
            _save(ledger_dir, state)
    return _dispatch(state, unit, config, transport, ledger_dir, now)


def run_cycle(config, transport, github, ledger_dir, lock_dir, now=None):
    cycle_id = config.get("cycle_resource_id") or (config["resource_id"] + "-cycle")
    if cycle_id == config["resource_id"]:
        raise ValueError("cycle mutex must differ from browser resource")
    current = time.time() if now is None else now
    owner = "cycle-" + uuid.uuid4().hex
    try:
        lease = locks.acquire(cycle_id, lock_dir, owner,
                              config.get("cycle_lease_seconds", 3600))
    except locks.ResourceBusyError:
        return {"status": "BUSY"}
    try:
        actual = dict(config, lock_dir=lock_dir)
        return _cycle(actual, transport, github, ledger_dir, current)
    finally:
        locks.release(cycle_id, lock_dir, owner, lease["token"])
