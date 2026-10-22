"""Conservative per-source metro projection, no acceptance inference."""
from owner_metro_sources import load, processes, processes_managed, parts, SOURCE_AGE_MS
from owner_metro_nodes import derive
from owner_metro_results import read as read_results
from owner_activity import read_activity, _read_source, _stamp

RECENT_LIMIT = 12
PENDING_LIMIT = 64

ROUTES = (
    ("maker", "PR75 · Maker", "blue",
     ("已驗收帳務與 reduce-only 測試", "真實市場特徵與執行驗證",
      "48 格 baseline / matched / stress", "反思及下一輪")),
    ("collectors", "Collectors", "green",
     ("跨交易所 15 分鐘收集", "真實清算事件與 heartbeat",
      "PR78 原生安裝與實際收集", "有效十四日涵蓋率")),
    ("relay", "PR76 · Stage Relay", "purple",
     ("安裝 stage controller", "目前 packet / identifier",
      "原子化工程投遞", "無人值守閉環驗收")),
)


def read_metro(root, *, now_ms, max_age_ms=300000, max_bytes=1048576, proc_root="/proc"):
    docs, health, paths = load(root, now_ms, max_bytes)
    activity = read_activity(root, now_ms=now_ms, max_age_ms=max(max_age_ms, 86400000),
                             max_bytes=max_bytes)
    for name, source in activity["sources"].items():
        if source.get("state") != "available":
            continue
        updated = _stamp(source.get("updated_at"))
        if updated is not None and now_ms - updated > SOURCE_AGE_MS.get(name, 900000):
            source["state"] = "stale"
            source["age_ms"] = now_ms - updated
    launch, adoption = docs.get("launch", {}), docs.get("adoption", {})
    proc = processes(launch, adoption, now_ms, proc_root=proc_root)
    managed = processes_managed(paths.get("managed", []), docs, health,
                                now_ms, proc_root=proc_root)
    verified = {p["pid"] for p in proc if p["state"] == "verified"}
    for candidate in managed:
        if candidate["state"] == "verified":
            if candidate["pid"] in verified:
                continue
            verified.add(candidate["pid"])
        proc.append(candidate)
    directive, stage = docs.get("directive", {}), docs.get("stage", {})
    heartbeat, liq = docs.get("heartbeat", {}), docs.get("liquidations", {})
    outputs = docs.get("results", {})
    result = {"state": "unbound", "count": None, "total": 48, "metrics": []}
    if isinstance(outputs, dict) and outputs.get("root") and outputs.get("attempt"):
        try:
            result = read_results(root, parts(outputs["root"]), outputs["attempt"], outputs.get("expected"))
        except (TypeError, ValueError):
            result = {"state": "invalid", "count": None, "total": 48, "metrics": []}
    phases = stage.get("products", {}) if isinstance(stage.get("products"), dict) else {}
    owners = directive.get("active_owners") if isinstance(directive.get("active_owners"), list) else []
    owners = [{"item": x.get("item"), "actor": x.get("actor"),
               "scope": x.get("scope"), "product": x.get("product")}
              for x in owners[:128] if isinstance(x, dict) and
              x.get("product") in ("maker", "collectors", "relay")]
    canonical, ledger_state = _read_source(root, "ledger", 4 * 1024 * 1024)
    raw_rows = canonical.get("items") if isinstance(canonical, dict) else None
    overrides, node_warnings = derive(root, raw_rows, paths.get("nodes", []),
                                      now_ms, proc_root=proc_root)
    routes = []
    for ident, label, color, steps in ROUTES:
        rows = phases.get(ident, {}) if isinstance(phases.get(ident), dict) else {}
        verified = rows.get("verified_steps") if isinstance(rows.get("verified_steps"), list) else []
        active = rows.get("active_step")
        statuses = []
        for i, caption in enumerate(steps):
            # Explicit verified evidence only; candidate CI is not installation.
            evidence = rows.get("evidence", {})
            proof = evidence.get(str(i)) if isinstance(evidence, dict) else None
            done = (health.get("stage", {}).get("state") == "available" and
                    i in verified and isinstance(proof, dict) and proof.get("accepted") is True)
            live = (i == active and any(p["state"] == "verified" and
                        p.get("product") == ident and
                        p["kind"] in ("compute", "collector", "coordination") for p in proc))
            override = overrides.get((ident, i))
            statuses.append({"title": caption,
                             "state": override["state"] if override else
                                      "done" if done else "active" if live else
                                      "waiting" if i == active else "next",
                             "note": override.get("note") if override else None,
                             "item": override.get("item") if override else None,
                             "evidence": proof.get("ref") if done and isinstance(proof.get("ref"), str) else None})
        selected = next((x for x in reversed(statuses) if x.get("item")), None)
        current_text = (selected.get("note") or selected.get("item")) if selected else (
            rows.get("current") if health.get("stage", {}).get("state") == "available" else None)
        routes.append({"id": ident, "title": label, "color": color,
                       "steps": statuses, "current": current_text,
                       "next": rows.get("next"), "verified_at": rows.get("verified_at")})
    tasks = activity["tasks"]
    past_states = ("VERIFIED", "DROPPED", "SUPERSEDED", "FROZEN")
    raw_rows = raw_rows if isinstance(raw_rows, list) else []
    history_count = sum(isinstance(t, dict) and t.get("state") in past_states for t in raw_rows)
    history = [{"id": t.get("id"), "title": t.get("text"), "state": t.get("state"),
                "note": t.get("reason") if isinstance(t.get("reason"), str) else None}
               for t in raw_rows if isinstance(t, dict) and t.get("state") in past_states]
    current = [t for t in tasks if t.get("ledger_state") not in past_states]
    pending_list = None
    pending = directive.get("pending_decisions") if isinstance(directive, dict) else None
    if isinstance(pending, list):
        pending = sum(isinstance(x, dict) and x.get("status") in
                      ("PENDING", "WAITING_HUMAN") for x in pending)
    pending = pending if type(pending) is int and pending >= 0 and health.get(
        "directive", {}).get("state") == "available" else None
    pending_source = "directive" if pending is not None else None
    if ledger_state == "available":
        selected = [x for x in raw_rows if isinstance(x, dict) and
                    x.get("state") in ("OPEN", "DISPATCHED") and
                    isinstance(x.get("blocked_by"), str) and
                    x["blocked_by"].startswith("user:")]
        pending_list = [{"id": str(x.get("id", ""))[:100],
                         "question": x["blocked_by"][5:][:2000],
                         "reason": str(x.get("text") or "")[:2000],
                         "created": x.get("created") if type(x.get("created")) in
                         (int, float) else None} for x in selected[:PENDING_LIMIT]]
        if health.get("ledger", {}).get("state") == "available" or (
                pending is None and health.get("directive", {}).get("state") == "stale"):
            pending = len(selected)
            pending_source = "ledger"
    recent = []
    if ledger_state == "available":
        for x in raw_rows:
            if not isinstance(x, dict):
                continue
            times = [_stamp(x.get(k)) for k in ("updated", "created")]
            at = max((t for t in times if t is not None), default=None)
            if at is None:
                continue
            alive = None
            if x.get("state") == "DISPATCHED" and isinstance(x.get("handle"), dict):
                from owner_metro_nodes import _live
                if x["handle"].get("kind") in ("pid", "cron"):
                    alive = "存活" if _live(root, x, proc_root) else "已死"
            recent.append({"at": at, "kind": "ledger",
                           "id": str(x.get("id") or "")[:100],
                           "text": str(x.get("text") or x.get("state") or "")[:200],
                           "alive": alive})
    if activity["sources"].get("work", {}).get("state") == "available":
        for x in tasks:
            if "work" not in x.get("source_keys", []):
                continue
            at = _stamp(x.get("progress_at"))
            if at is not None:
                recent.append({"at": at, "kind": "work", "id": x["id"],
                               "text": str(x.get("step") or x.get("title") or "")[:200],
                               "alive": None})
    recent = sorted(recent, key=lambda x: x["at"], reverse=True)[:RECENT_LIMIT]
    count = heartbeat.get("events_total") if isinstance(heartbeat, dict) and health.get("heartbeat", {}).get("state") == "available" else None
    if count is None and health.get("liquidations", {}).get("state") == "available":
        count = liq.get("event_count") if isinstance(liq, dict) else None
    if count is None and health.get("heartbeat", {}).get("state") == "available":
        count = heartbeat.get("liquidation_event_count")
    count = count if type(count) is int and count >= 0 else None
    return {"schema_version": 1, "observer_only": True, "installed": False,
            "generated_ms": now_ms, "sources": {**activity["sources"], **health},
            "kpis": {"processes": sum(p["state"] == "verified" for p in proc),
                     "maker_cells": result["count"], "liquidations": count,
                     "pending_decisions": pending, "pending_source": pending_source,
                     "pending_list": pending_list},
            "processes": proc, "maker": {"phase": stage.get("maker_phase"),
                                        "results": result,
                                        "lease": stage.get("lease_status"),
                                        "output": stage.get("output_status")},
            "heartbeat": {"at": heartbeat.get("ts_iso") or heartbeat.get("at"), "status": heartbeat.get("status"),
                          "age_ms": health.get("heartbeat", {}).get("age_ms")},
            "directive": {"owners": owners}, "current": current[-4096:],
            "routes": routes, "recent": recent, "history": history[-4096:],
            "history_count": history_count,
            "source_totals": activity["totals"],
            "warnings": activity["warnings"] + node_warnings}
