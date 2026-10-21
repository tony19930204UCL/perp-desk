"""Conservative per-source metro projection, no acceptance inference."""
from owner_metro_sources import load, processes, processes_managed, parts
from owner_metro_nodes import derive
from owner_metro_results import read as read_results
from owner_activity import read_activity, _read_source

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
    activity = read_activity(root, now_ms=now_ms, max_age_ms=max_age_ms,
                             max_bytes=max_bytes)
    launch, adoption = docs.get("launch", {}), docs.get("adoption", {})
    proc = processes(launch, adoption, now_ms, proc_root=proc_root)
    managed = processes_managed(paths.get("managed", []), docs, health,
                                now_ms, proc_root=proc_root)
    verified = {p["pid"] for p in proc if p["state"] == "verified"}
    proc += [p for p in managed if p["state"] != "verified" or p["pid"] not in verified
             if p["state"] != "verified" or not verified.add(p["pid"])]
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
        routes.append({"id": ident, "title": label, "color": color,
                       "steps": statuses, "current": rows.get("current"),
                       "next": rows.get("next"), "verified_at": rows.get("verified_at")})
    tasks = activity["tasks"]
    past_states = ("VERIFIED", "DROPPED", "SUPERSEDED", "FROZEN")
    raw_rows = raw_rows if isinstance(raw_rows, list) else []
    history_count = sum(isinstance(t, dict) and t.get("state") in past_states for t in raw_rows)
    history = [{"id": t.get("id"), "title": t.get("text"), "state": t.get("state"),
                "note": t.get("reason") if isinstance(t.get("reason"), str) else None}
               for t in raw_rows if isinstance(t, dict) and t.get("state") in past_states]
    current = [t for t in tasks if t.get("ledger_state") not in past_states]
    pending = directive.get("pending_decisions") if isinstance(directive, dict) else None
    if isinstance(pending, list):
        pending = sum(isinstance(x, dict) and x.get("status") in
                      ("PENDING", "WAITING_HUMAN") for x in pending)
    pending = pending if type(pending) is int and pending >= 0 and health.get("directive", {}).get("state") == "available" else None
    pending_source = "directive" if pending is not None else None
    if pending is None and ledger_state == "available":
        pending = sum(isinstance(item, dict) and
                      item.get("state") not in ("VERIFIED", "DROPPED") and
                      isinstance(item.get("blocked_by"), str) and
                      item["blocked_by"].startswith("user:") for item in raw_rows)
        pending_source = "ledger"
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
                     "pending_decisions": pending, "pending_source": pending_source},
            "processes": proc, "maker": {"phase": stage.get("maker_phase"),
                                        "results": result,
                                        "lease": stage.get("lease_status"),
                                        "output": stage.get("output_status")},
            "heartbeat": {"at": heartbeat.get("ts_iso") or heartbeat.get("at"), "status": heartbeat.get("status"),
                          "age_ms": health.get("heartbeat", {}).get("age_ms")},
            "directive": {"owners": owners}, "current": current[-4096:],
            "routes": routes, "history": history[-4096:],
            "history_count": history_count,
            "source_totals": activity["totals"],
            "warnings": activity["warnings"] + node_warnings}
