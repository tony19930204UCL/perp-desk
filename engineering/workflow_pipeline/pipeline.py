"""Pure workflow projection and lease rules. No I/O."""
from datetime import datetime, timezone

TERMINAL = {"VERIFIED", "DROPPED"}

def normalize_item(item, meta=None):
    """Project an existing ledger item without changing its identity/history."""
    meta = dict(meta or {})
    handle = item.get("handle")
    return {
        "task_id": item["id"], "root": meta.get("root", item["id"]),
        "parent": meta.get("parent", "N/A"), "kind": meta.get("kind", "one_shot"),
        "goal": meta.get("goal", item.get("text") or "N/A"),
        "acceptance": meta.get("acceptance", item.get("accept") or "N/A"),
        "priority": meta.get("priority", "N/A"), "source": meta.get("source", item.get("source") or "N/A"),
        "created": item.get("created"), "dependencies": meta.get("dependencies", [item["blocked_by"]] if item.get("blocked_by") else []),
        "owner_role": meta.get("owner_role", "N/A"), "executor": handle or "N/A",
        "attempt": int(meta.get("attempt", 1)), "instruction": meta.get("instruction", "N/A"),
        "repo": meta.get("repo", "N/A"), "branch": meta.get("branch", "N/A"),
        "source_head": meta.get("source_head", "N/A"), "evidence_uri": meta.get("evidence_uri", item.get("evidence") or "N/A"),
        "evidence_hash": meta.get("evidence_hash", "N/A"), "next_action": meta.get("next_action", "N/A"),
        "next_check": meta.get("next_check", "N/A"), "last_progress": meta.get("last_progress", item.get("updated")),
        "blocker": meta.get("blocker", item.get("blocked_by") or "N/A"), "release": meta.get("release", "N/A"),
        "responsible_owner": meta.get("responsible_owner", "N/A"), "retry_budget": int(meta.get("retry_budget", 0)),
        "terminal_outcome": meta.get("terminal_outcome", item["state"] if item.get("state") in TERMINAL else "N/A")
    }

def transition(task, actor, at, to_state, action, reason, evidence="N/A", next_owner="N/A"):
    """Return an append-only transition event; never mutates the ledger."""
    return {"event": "transition", "attempt": task["attempt"], "task_id": task["task_id"],
            "actor": actor, "time": at, "from": task.get("state", "N/A"), "to": to_state,
            "action": action, "reason": reason, "evidence": evidence, "next_owner": next_owner}

def acquire_lease(task_id, attempt, holder, now, ttl, leases):
    """Single lease per task/attempt. Unknown live lease is never overwritten."""
    key = "%s:%s" % (task_id, attempt)
    old = leases.get(key)
    if old and float(old.get("expires", 0)) > float(now) and old.get("holder") != holder:
        return None, "LEASE_HELD"
    lease = {"task_id": task_id, "attempt": attempt, "holder": holder, "acquired": now, "expires": float(now) + float(ttl)}
    return lease, "ACQUIRED"

def reconcile(observation):
    """Choose observation only; callers own any authorized write."""
    if observation.get("send") == "unknown":
        return "OBSERVE_ONLY"
    if observation.get("quota_lost"):
        return "WAIT_AVAILABILITY"
    if observation.get("handback"):
        return "ACCEPT"
    if observation.get("worker") == "dead":
        return "REPAIR"
    if observation.get("ci") == "pending":
        return "WAIT_CI"
    if observation.get("worker") == "alive":
        return "OBSERVE"
    return "BLOCKED"
