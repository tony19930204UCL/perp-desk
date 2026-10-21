"""Conservative per-source metro projection, no acceptance inference."""
from owner_metro_sources import load, processes, parts
from owner_metro_results import read as read_results
from owner_activity import read_activity

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


def read_metro(root, *, now_ms, max_age_ms=300000, max_bytes=1048576):
    docs, health, paths = load(root, now_ms, max_bytes)
    activity = read_activity(root, now_ms=now_ms, max_age_ms=max_age_ms,
                             max_bytes=max_bytes)
    launch, adoption = docs.get("launch", {}), docs.get("adoption", {})
    proc = processes(launch, adoption, now_ms)
    directive, stage = docs.get("directive", {}), docs.get("stage", {})
    heartbeat, liq = docs.get("heartbeat", {}), docs.get("liquidations", {})
    outputs = docs.get("results", {})
    result = {"state": "unbound", "count": 0, "total": 48, "metrics": []}
    if isinstance(outputs, dict) and outputs.get("root") and outputs.get("attempt"):
        try:
            result = read_results(root, parts(outputs["root"]), outputs["attempt"])
        except (TypeError, ValueError):
            result = {"state": "invalid", "count": 0, "total": 48, "metrics": []}
    phases = stage.get("products", {}) if isinstance(stage.get("products"), dict) else {}
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
            done = i in verified and isinstance(proof, dict) and proof.get("accepted") is True
            live = (i == active and any(p["state"] == "verified" and
                        p["kind"] in ("compute", "collector", "coordination") for p in proc))
            statuses.append({"title": caption,
                             "state": "done" if done else "active" if live else
                                      "waiting" if i == active else "next",
                             "evidence": proof.get("ref") if done and isinstance(proof.get("ref"), str) else None})
        routes.append({"id": ident, "title": label, "color": color,
                       "steps": statuses, "current": rows.get("current"),
                       "next": rows.get("next"), "verified_at": rows.get("verified_at")})
    tasks = activity["tasks"]
    history = [t for t in tasks if t.get("ledger_state") in
               ("VERIFIED", "DROPPED", "SUPERSEDED", "FROZEN")]
    pending = directive.get("pending_decisions") if isinstance(directive, dict) else None
    pending = pending if type(pending) is int and pending >= 0 else None
    count = liq.get("event_count") if isinstance(liq, dict) else None
    count = count if type(count) is int and count >= 0 else None
    return {"schema_version": 1, "observer_only": True, "installed": False,
            "generated_ms": now_ms, "sources": {**activity["sources"], **health},
            "kpis": {"processes": sum(p["state"] == "verified" for p in proc),
                     "maker_cells": result["count"], "liquidations": count,
                     "pending_decisions": pending},
            "processes": proc, "maker": {"phase": stage.get("maker_phase"),
                                        "results": result,
                                        "lease": stage.get("lease_status"),
                                        "output": stage.get("output_status")},
            "heartbeat": {"at": heartbeat.get("at"), "status": heartbeat.get("status")},
            "directive": {"owner": directive.get("owner") if isinstance(directive.get("owner"), str) else None},
            "routes": routes, "history": history[-4096:],
            "history_count": len(history),
            "source_totals": activity["totals"],
            "warnings": activity["warnings"]}
