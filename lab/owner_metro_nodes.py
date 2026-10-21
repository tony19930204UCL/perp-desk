"""Deterministic read-only ledger node overrides, never engineering acceptance."""
import os
import re
from owner_activity import _read_source

_ID = re.compile(r"^C-[0-9]{1,6}$")


def _live(root, row, proc_root):
    handle = row.get("handle")
    if not isinstance(handle, dict):
        return False
    kind = handle.get("kind")
    if kind == "pid":
        pid, start = handle.get("pid"), handle.get("start")
        if type(pid) is not int or type(start) is not int or pid < 1:
            return False
        try:
            with open(os.path.join(proc_root, str(pid), "stat"), "rb") as fd:
                raw = fd.read(4096).decode()
            return int(raw[raw.rfind(")") + 2:].split()[19]) == start
        except (OSError, IndexError, ValueError):
            return False
    if kind == "cron" and isinstance(handle.get("ref"), str):
        jobs, health = _read_source(root, "cron", 1048576)
        return health == "available" and any(isinstance(job, dict) and
            job.get("id") == handle["ref"] and job.get("enabled") is True
            for job in jobs.get("jobs", []))
    return False


def derive(root, ledger_items, bindings_nodes, now_ms, proc_root="/proc"):
    overrides, warnings = {}, []
    if not isinstance(bindings_nodes, list) or len(bindings_nodes) > 24:
        return overrides, ["nodes: invalid bindings"]
    if not isinstance(ledger_items, list):
        return overrides, ["nodes: ledger unavailable"]
    by_id = {row["id"]: row for row in ledger_items
             if isinstance(row, dict) and isinstance(row.get("id"), str)}
    for binding in bindings_nodes:
        if not isinstance(binding, dict):
            warnings.append("nodes: invalid binding")
            continue
        route, step, ids = binding.get("route"), binding.get("step"), binding.get("items")
        gate = binding.get("gate")
        if (route not in ("maker", "collectors", "relay") or type(step) is not int
                or not 0 <= step <= 3 or not isinstance(ids, list)
                or not 1 <= len(ids) <= 8 or any(not isinstance(i, str)
                or not _ID.fullmatch(i) for i in ids) or
                (gate is not None and (not isinstance(gate, str) or not _ID.fullmatch(gate)))):
            warnings.append("nodes: invalid id or type")
            continue
        current = by_id.get(ids[-1])
        if current is None or any(i not in by_id for i in ids):
            warnings.append("nodes: missing ledger id")
            continue
        def user_block(row):
            blocker = row.get("blocked_by")
            return isinstance(blocker, str) and blocker.startswith("user:")
        state = None
        note = None
        chosen = current
        gate_row = by_id.get(gate) if gate is not None else None
        if gate is not None and gate_row is None:
            warnings.append("nodes: missing gate id")
            continue
        if gate_row and gate_row.get("state") not in ("VERIFIED", "DROPPED") and user_block(gate_row):
            state = "user"
            chosen = gate_row
            note = str(gate_row.get("text") or "")[:80]
        elif current.get("state") == "DISPATCHED" and _live(root, current, proc_root):
            previous = [by_id[i] for i in ids[:-1]
                        if by_id[i].get("state") in ("DROPPED", "BLOCKED")]
            state = "retest" if previous else "active"
            if previous:
                note = str(previous[-1].get("reason") or "")[:80]
                note = str(len(previous)) + " · " + note
        elif current.get("state") in ("DISPATCHED", "BLOCKED", "DROPPED"):
            state = "failed"
            note = str(current.get("reason") or "")[:80]
        if state is not None:
            overrides[(route, step)] = {"state": state, "note": note,
                                         "item": chosen.get("id")}
    return overrides, warnings
