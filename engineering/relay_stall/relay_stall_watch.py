#!/usr/bin/env python3
"""relay_stall_watch.py - Observe-only relay stall detector."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import time
from typing import Any

SCHEMA_VER = 1
CHAT_MAX_AGE = 600
WAIT_APPR_SEC = 300
STALL_SEC = 1200
CONSUMER_STALE = 1200

ST_WAIT_APPR = "WAIT_APPROVAL"
ST_EXPIRED = "EXPIRED"
ST_OWNER_DEAD = "OWNER_DEAD"
ST_UNKNOWN = "UNKNOWN"
ST_STALLED = "STALLED"
ST_REFUSAL = "POLICY_REFUSAL"

DISP_REVIEW = "INTERNAL_REVIEW"
ACT_PRESERVE = "preserve/human"
ACT_INVESTIGATE = "investigate_fault"


class StateLock:
    """Exclusive flock guard for state directory."""

    def __init__(self, state_dir: str):
        self.lock_file = os.path.join(state_dir, ".lock")
        self.fd: int | None = None

    def __enter__(self):
        self.fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None


def atomic_write(path: str, data: Any) -> None:
    """Write data to path atomically with fsync."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_jsonl(path: str, record: dict) -> None:
    """Append a line to a jsonl file with fsync."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def validate_state_dict(state: Any) -> dict:
    """Strictly validate state.json dictionary structure."""
    if not isinstance(state, dict):
        raise ValueError("state.json must be a dict")
    if "jobs" not in state or not isinstance(state["jobs"], dict):
        raise ValueError("state.json missing valid jobs dict")
    if "alerted_causes" not in state or not isinstance(state["alerted_causes"], dict):
        raise ValueError("state.json missing valid alerted_causes dict")
    if "last_scan_at" not in state:
        raise ValueError("state.json missing last_scan_at")
    lsa = state["last_scan_at"]
    if type(lsa) is bool or not isinstance(lsa, (int, float)) or not math.isfinite(lsa):
        raise ValueError("state.json last_scan_at must be finite numeric epoch")
    return state


def validate_pending_list(pending: Any) -> list:
    """Strictly validate pending.json list and element entries."""
    if not isinstance(pending, list):
        raise ValueError("pending.json must be a list")
    for item in pending:
        if not isinstance(item, dict):
            raise ValueError("pending.json entry must be a dict")
        ev_id = item.get("event_id")
        fp = item.get("fingerprint")
        ident = item.get("identity")
        states = item.get("states")
        reasons = item.get("reasons")
        fs = item.get("first_seen")
        ls = item.get("last_seen")
        if not isinstance(ev_id, str) or not ev_id.strip():
            raise ValueError("pending entry missing valid event_id")
        if not isinstance(fp, str) or not fp.strip():
            raise ValueError("pending entry missing valid fingerprint")
        if not isinstance(ident, dict):
            raise ValueError("pending entry missing valid identity dict")
        if not isinstance(states, list) or not isinstance(reasons, list):
            raise ValueError("pending entry states/reasons must be lists")
        if type(fs) is bool or not isinstance(fs, (int, float)) or not math.isfinite(fs):
            raise ValueError("pending entry first_seen must be finite epoch")
        if type(ls) is bool or not isinstance(ls, (int, float)) or not math.isfinite(ls):
            raise ValueError("pending entry last_seen must be finite epoch")
    return pending


def parse_job_identity(job: dict) -> dict | None:
    """Extract valid full identity or return None."""
    if not isinstance(job, dict):
        return None
    r, b, i, j = job.get("repository"), job.get("branch"), job.get("instruction_id"), job.get("job_id")
    if all(isinstance(x, str) and x.strip() for x in (r, b, i, j)):
        return {"repository": r, "branch": b, "instruction_id": i, "job_id": j}
    return None


def identity_to_key(ident: dict | None, idx: int = 0) -> str:
    """Convert identity dict to unique string key."""
    if ident and all(k in ident for k in ("repository", "branch", "instruction_id", "job_id")):
        return f"{ident['repository']}#{ident['branch']}#{ident['instruction_id']}#{ident['job_id']}"
    return f"unident_{idx}"


def make_fp(ident_str: str, states: list[str], reasons: list[str], head: str) -> str:
    """Generate distinct fault fingerprint."""
    raw = f"{ident_str}|{','.join(sorted(states))}|{','.join(sorted(reasons))}|{head}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def eval_job(job: dict, now: float, prev_state: dict) -> tuple[dict, list[str], list[str], str, float | None, dict]:
    """Evaluate job conditions and return updated state and active fault states."""
    states, reasons = [], []
    ident = parse_job_identity(job)
    if ident is None:
        states.append(ST_UNKNOWN)
        reasons.append("invalid_identity")
        ident_dict = {"raw": str(job)}
    else:
        ident_dict = ident

    first_seen = prev_state.get("first_seen", now)
    head = job.get("head") if isinstance(job, dict) else None
    head_is_valid = isinstance(head, str) and bool(head.strip())
    if not head_is_valid:
        states.append(ST_UNKNOWN)
        reasons.append("invalid_head")
        head_str = prev_state.get("head", "")
        p_seen = prev_state.get("head_seen_at")
        head_seen_at = float(p_seen) if p_seen is not None else now
    else:
        head_str = head
        p_head, p_seen = prev_state.get("head"), prev_state.get("head_seen_at")
        head_seen_at = float(p_seen) if p_head == head_str and p_seen is not None else now

    exp = job.get("expires_at") if isinstance(job, dict) else None
    if type(exp) is bool or not isinstance(exp, (int, float)) or not math.isfinite(exp):
        states.append(ST_UNKNOWN)
        reasons.append("invalid_expiry")
        exp_val = None
    else:
        exp_val = float(exp)
        if now >= exp_val:
            states.append(ST_EXPIRED)
            reasons.append("expiry_passed")

    owner = job.get("owner") if isinstance(job, dict) else None
    if not isinstance(owner, dict):
        states.append(ST_UNKNOWN)
        reasons.append("missing_owner")
    else:
        k, pid, sid = owner.get("kind"), owner.get("pid"), owner.get("start_id")
        alv, ok = owner.get("alive"), owner.get("identity_ok")
        if (
            k != "pid"
            or type(pid) is bool
            or not isinstance(pid, int)
            or not isinstance(sid, str)
            or not sid.strip()
            or type(alv) is not bool
            or type(ok) is not bool
        ):
            states.append(ST_UNKNOWN)
            reasons.append("invalid_owner_fields")
        elif not alv or not ok:
            states.append(ST_OWNER_DEAD)
            reasons.append("owner_dead_or_mismatch")

    s_status = job.get("send_status") if isinstance(job, dict) else None
    if s_status == "SEND_UNKNOWN":
        states.append(ST_UNKNOWN)
        reasons.append("send_unknown")
    elif s_status != "CONFIRMED":
        states.append(ST_UNKNOWN)
        reasons.append("invalid_send_status")

    raw_handback = job.get("handback_valid") if isinstance(job, dict) else None
    if type(raw_handback) is not bool:
        states.append(ST_UNKNOWN)
        reasons.append("invalid_handback")
        handback_is_false = False
    else:
        handback_is_false = (raw_handback is False)

    raw_refusal = job.get("policy_refusal") if isinstance(job, dict) else None
    if type(raw_refusal) is not bool:
        states.append(ST_UNKNOWN)
        reasons.append("invalid_refusal")
    elif raw_refusal is True:
        states.append(ST_REFUSAL)
        reasons.append("policy_refusal")

    chat = job.get("chat") if isinstance(job, dict) else None
    chat_fresh = False
    pending_since = prev_state.get("pending_since")
    if not isinstance(chat, dict):
        states.append(ST_UNKNOWN)
        reasons.append("missing_chat")
    else:
        obs, p_card, busy = chat.get("observed_at"), chat.get("pending_card"), chat.get("busy")
        if (
            type(obs) is bool
            or not isinstance(obs, (int, float))
            or not math.isfinite(obs)
            or type(p_card) is not bool
            or type(busy) is not bool
        ):
            states.append(ST_UNKNOWN)
            reasons.append("invalid_chat_fields")
        else:
            age = now - float(obs)
            if age < 0:
                states.append(ST_UNKNOWN)
                reasons.append("future_chat")
            elif age > CHAT_MAX_AGE:
                states.append(ST_UNKNOWN)
                reasons.append("stale_chat")
            else:
                chat_fresh = True
                if p_card is True:
                    if pending_since is None:
                        pending_since = now
                    if (now - pending_since) >= WAIT_APPR_SEC:
                        states.append(ST_WAIT_APPR)
                        reasons.append("pending_over_300s")
                else:
                    pending_since = None

    is_idle = chat_fresh and isinstance(chat, dict) and chat.get("busy") is False
    no_card = chat_fresh and isinstance(chat, dict) and chat.get("pending_card") is False
    if head_is_valid and (now - head_seen_at) >= STALL_SEC and is_idle and no_card and handback_is_false:
        states.append(ST_STALLED)
        reasons.append("head_unchanged_idle")

    new_state = {
        "identity": ident_dict,
        "first_seen": first_seen,
        "head": head_str,
        "head_seen_at": head_seen_at,
        "pending_since": pending_since,
        "last_seen": now,
    }
    return ident_dict, sorted(set(states)), sorted(set(reasons)), head_str, exp_val, new_state


def emit_notify(cause: str, detail: dict) -> None:
    """Print human_notify alert envelope to stdout."""
    env = {"envelope": "human_notify", "cause": cause, "detail": detail, "action_required": "human_intervention"}
    sys.stdout.write(json.dumps(env) + "\n")
    sys.stdout.flush()


def run_scan(input_path: str, state_dir: str, now: float) -> int:
    """Execute one observe-only detector round."""
    os.makedirs(state_dir, exist_ok=True)
    with StateLock(state_dir):
        f_state = os.path.join(state_dir, "state.json")
        f_pending = os.path.join(state_dir, "pending.json")
        f_events = os.path.join(state_dir, "events.jsonl")
        f_latest = os.path.join(state_dir, "latest.json")

        has_state = os.path.exists(f_state)
        has_pending = os.path.exists(f_pending)
        has_latest = os.path.exists(f_latest)
        has_events = os.path.exists(f_events)

        is_first_init = not (has_state or has_pending or has_latest or has_events)
        if is_first_init:
            state = {"jobs": {}, "alerted_causes": {}}
            jobs_state = state["jobs"]
            alerted = state["alerted_causes"]
            prev_pending = []
            prev_pending_by_fp = {}
        else:
            if not has_state:
                raise ValueError("Missing state.json in initialized state directory")
            if not has_pending:
                raise ValueError("Missing pending.json in initialized state directory")

            with open(f_state, "r", encoding="utf-8") as f:
                state_raw = json.load(f)
            state = validate_state_dict(state_raw)
            jobs_state = state.get("jobs", {})
            alerted = state.get("alerted_causes", {})

            with open(f_pending, "r", encoding="utf-8") as f:
                pending_raw = json.load(f)
            prev_pending = validate_pending_list(pending_raw)
            prev_pending_by_fp = {p["fingerprint"]: p for p in prev_pending}

        consumer_data, jobs = None, []
        if not os.path.isfile(input_path):
            jobs = [{"invalid_reason": "missing_input_file"}]
        else:
            try:
                with open(input_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VER:
                    jobs = [{"invalid_reason": "invalid_schema"}]
                else:
                    consumer_data = raw.get("consumer")
                    raw_jobs = raw.get("jobs")
                    jobs = raw_jobs if isinstance(raw_jobs, list) else [{"invalid_reason": "invalid_jobs"}]
            except Exception:
                jobs = [{"invalid_reason": "corrupt_json"}]

        cons_fault: str | None = None
        if not isinstance(consumer_data, dict):
            cons_fault = "consumer_missing"
        elif type(consumer_data.get("enabled")) is not bool or consumer_data.get("enabled") is not True:
            cons_fault = "consumer_disabled"
        else:
            rx = consumer_data.get("last_receipt_at")
            if type(rx) is bool or not isinstance(rx, (int, float)) or not math.isfinite(rx):
                cons_fault = "consumer_bad_receipt"
            elif rx > now:
                cons_fault = "consumer_future_receipt"
            elif (now - rx) > CONSUMER_STALE:
                cons_fault = "consumer_stale"

        if cons_fault:
            if cons_fault not in alerted:
                emit_notify(cons_fault, {"now": now, "consumer": consumer_data})
                alerted[cons_fault] = now
        else:
            for k in ["consumer_missing", "consumer_disabled", "consumer_bad_receipt", "consumer_future_receipt", "consumer_stale"]:
                alerted.pop(k, None)

        round_events, new_pending_by_fp, active_refusals = [], {}, set()
        seen_ident_keys: set[str] = set()

        for idx, job in enumerate(jobs):
            ident_dict = parse_job_identity(job)
            ident_key = identity_to_key(ident_dict, idx)
            seen_ident_keys.add(ident_key)

            prev_j = jobs_state.get(ident_key, {})
            ident_res, states, reasons, head_str, exp_val, new_j = eval_job(job, now, prev_j)
            jobs_state[ident_key] = new_j

            if states:
                fp = make_fp(ident_key, states, reasons, head_str)
                orig_receipt = job.get("receipt_ref") if isinstance(job, dict) else None
                if fp in prev_pending_by_fp:
                    ev = dict(prev_pending_by_fp[fp])
                    ev["last_seen"] = now
                    if orig_receipt and not ev.get("receipt_ref"):
                        ev["receipt_ref"] = orig_receipt
                else:
                    ev = {
                        "event_id": f"ev_{fp}_{int(now)}",
                        "fingerprint": fp,
                        "first_seen": now,
                        "last_seen": now,
                        "identity": ident_res,
                        "states": states,
                        "reasons": reasons,
                        "head": head_str,
                        "original_expires_at": exp_val,
                        "receipt_ref": orig_receipt,
                        "ack_receipt_id": None,
                        "consumer_receipt_ref": None,
                        "disposition": DISP_REVIEW,
                        "internal_action": ACT_PRESERVE if ST_REFUSAL in states else ACT_INVESTIGATE,
                    }
                new_pending_by_fp[fp] = ev
                round_events.append(ev)

                if ST_REFUSAL in states:
                    ref_key = f"refusal:{ident_key}"
                    active_refusals.add(ref_key)
                    if ref_key not in alerted:
                        emit_notify("policy_refusal", {"identity": ident_res, "now": now})
                        alerted[ref_key] = now

        # Retain unresolved pending faults for jobs absent from current input
        for prev_ev in prev_pending:
            prev_ident = prev_ev.get("identity")
            prev_key = identity_to_key(prev_ident)
            if prev_key not in seen_ident_keys:
                absent_states = sorted(set(prev_ev.get("states", []) + [ST_UNKNOWN]))
                absent_reasons = sorted(set(prev_ev.get("reasons", []) + ["absent_from_input"]))
                ev_absent = dict(prev_ev)
                ev_absent["states"] = absent_states
                ev_absent["reasons"] = absent_reasons
                ev_absent["last_seen"] = now
                fp = prev_ev.get("fingerprint")
                new_pending_by_fp[fp] = ev_absent
                round_events.append(ev_absent)

        for k in [k for k in alerted if k.startswith("refusal:") and k not in active_refusals]:
            del alerted[k]

        if round_events:
            for ev in round_events:
                append_jsonl(f_events, ev)
        else:
            append_jsonl(f_events, {"event_type": "round_healthy", "timestamp": now, "jobs_evaluated": len(jobs)})

        new_pending = list(new_pending_by_fp.values())
        atomic_write(f_pending, new_pending)
        atomic_write(f_latest, {
            "schema_version": SCHEMA_VER,
            "timestamp": now,
            "jobs_evaluated": len(jobs),
            "round_faults": len(round_events),
            "pending_faults": len(new_pending),
            "consumer_fault": cons_fault,
        })
        atomic_write(f_state, {"jobs": jobs_state, "alerted_causes": alerted, "last_scan_at": now})

        summary = {
            "summary": "stall_watch_run",
            "timestamp": now,
            "jobs_count": len(jobs),
            "faults_count": len(round_events),
            "pending_count": len(new_pending),
            "events": [{"event_id": e.get("event_id"), "states": e.get("states"), "head": e.get("head")} for e in round_events],
        }
        sys.stdout.write(json.dumps(summary) + "\n")
        sys.stdout.flush()

    return 0


def run_ack(state_dir: str, event_id: str, action: str, now: float) -> int:
    """Record internal consumer receipt for an event."""
    if not os.path.isdir(state_dir):
        sys.stderr.write(f"State dir does not exist: {state_dir}\n")
        return 1

    with StateLock(state_dir):
        pending_file = os.path.join(state_dir, "pending.json")
        if not os.path.exists(pending_file):
            sys.stderr.write(f"Missing pending.json: {pending_file}\n")
            return 1

        with open(pending_file, "r", encoding="utf-8") as f:
            pending_raw = json.load(f)
        pending = validate_pending_list(pending_raw)

        found_ev = None
        for item in pending:
            if item.get("event_id") == event_id:
                found_ev = item
                break

        if found_ev is None:
            sys.stderr.write(f"Event {event_id} not found in pending\n")
            return 1

        rcpt_file = os.path.join(state_dir, "receipts.jsonl")
        rcpt_hash = hashlib.sha256(f"{event_id}:{action}:{now}".encode("utf-8")).hexdigest()[:8]
        rcpt_id = f"rcpt_{int(now)}_{rcpt_hash}"
        rcpt = {"receipt_id": rcpt_id, "event_id": event_id, "action": action, "recorded_at": now}
        append_jsonl(rcpt_file, rcpt)

        # Record consumer ack id independently without touching receipt_ref
        found_ev["ack_receipt_id"] = rcpt_id
        found_ev["consumer_receipt_ref"] = rcpt_id
        atomic_write(pending_file, pending)

        out = {"status": "ACK_RECORDED", "receipt_id": rcpt_id, "event_id": event_id}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = argv if argv is not None else sys.argv[1:]
    try:
        if args and args[0] == "ack":
            parser = argparse.ArgumentParser(description="Ack pending fault event.")
            parser.add_argument("--state-dir", required=True, help="State directory")
            parser.add_argument("--event-id", required=True, help="Target event ID")
            parser.add_argument("--action", required=True, help="Action/reading description")
            parser.add_argument("--now", type=float, default=None, help="Explicit epoch timestamp")
            parsed = parser.parse_args(args[1:])
            now_ts = parsed.now if parsed.now is not None else time.time()
            if type(now_ts) is bool or not isinstance(now_ts, (int, float)) or not math.isfinite(now_ts):
                sys.stderr.write("now must be finite epoch\n")
                return 1
            return run_ack(parsed.state_dir, parsed.event_id, parsed.action, now_ts)

        parser = argparse.ArgumentParser(description="Relay stall observe-only detector.")
        parser.add_argument("--input", required=True, help="Path to normalized.json")
        parser.add_argument("--state-dir", required=True, help="State directory")
        parser.add_argument("--now", type=float, default=None, help="Explicit epoch timestamp")
        parsed = parser.parse_args(args)
        now_ts = parsed.now if parsed.now is not None else time.time()
        if type(now_ts) is bool or not isinstance(now_ts, (int, float)) or not math.isfinite(now_ts):
            sys.stderr.write("now must be finite epoch\n")
            return 1
        return run_scan(parsed.input, parsed.state_dir, now_ts)
    except Exception as e:
        sys.stderr.write(f"Relay stall watch error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
