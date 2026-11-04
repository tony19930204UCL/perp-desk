"""Conservative per-source metro projection, no acceptance inference."""
from owner_metro_sources import load, processes, processes_managed, parts, SOURCE_AGE_MS
from owner_metro_nodes import derive
from owner_metro_results import read as read_results, read_catalog
from owner_activity import read_activity, _read_source, _stamp
import hashlib
import json

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


def _evaluate_prohibitions(docs, health, raw_rows):
    """Evaluate typed decision_policy with exact manifest SHA and per-item pin checks."""
    prohibitions = {}
    policy = docs.get("decision_policy")
    frozen_manifest = docs.get("frozen_manifest")
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        return prohibitions
    expected_manifest_sha = policy.get("manifest_sha256")
    if not isinstance(expected_manifest_sha, str):
        return prohibitions
    if not isinstance(frozen_manifest, dict):
        return prohibitions
    manifest_bytes = json.dumps(frozen_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual_manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_sha != expected_manifest_sha:
        return prohibitions

    rules = policy.get("rules", [])
    if not isinstance(rules, list):
        return prohibitions

    manifest_items_list = frozen_manifest.get("items")
    if not isinstance(manifest_items_list, list):
        return prohibitions
    manifest_items = {m["id"]: m for m in manifest_items_list
                      if isinstance(m, dict) and isinstance(m.get("id"), str)}
    by_id = {x.get("id"): x for x in raw_rows if isinstance(x, dict) and isinstance(x.get("id"), str)}
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("classification") != "prohibition":
            continue
        rid = rule.get("id")
        if rid not in manifest_items or rid not in by_id:
            continue
        m_item = manifest_items[rid]
        orig_payload = m_item.get("original_payload")
        if not isinstance(orig_payload, dict) or orig_payload.get("id") != rid:
            continue
        calc_m_sha = hashlib.sha256(json.dumps(orig_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if m_item.get("original_payload_sha256") != calc_m_sha:
            continue
        pin = rule.get("pin_sha256")
        calc_pin = hashlib.sha256(f"{orig_payload.get('text', '')}|{orig_payload.get('blocked_by', '')}".encode("utf-8")).hexdigest()
        if pin not in (calc_m_sha, calc_pin):
            continue
        item = by_id[rid]
        if item.get("text") != orig_payload.get("text") or item.get("blocked_by") != orig_payload.get("blocked_by") or item.get("id") != orig_payload.get("id"):
            continue
        prohibitions[rid] = {
            "id": rid,
            "title": str(item.get("text") or ""),
            "state": "FROZEN",
            "note": rule.get("note") or "frozen prohibition policy"
        }
    return prohibitions


def directive_matches(doc, health, ledger_state, ledger_health, rows, selected):
    """Independently corroborate a stale directive without changing its KPI origin."""
    if (health.get("state") != "stale" or
            type(health.get("age_ms")) is not int or
            not 300000 < health["age_ms"] <= 86400000 or
            ledger_state != "available" or
            ledger_health.get("state") != "available" or
            not isinstance(rows, list) or not isinstance(doc, dict)):
        return False
    declared = doc.get("pending_decisions")
    if type(declared) is int:
        if declared < 0 or declared != len(selected):
            return False
    elif isinstance(declared, list):
        if any(not isinstance(item, dict) or
               not isinstance(item.get("id"), str) or
               item.get("status") not in ("PENDING", "WAITING_HUMAN",
                                           "RESOLVED", "CLOSED")
               for item in declared):
            return False
        expected = {item["id"] for item in declared
                    if item["status"] in ("PENDING", "WAITING_HUMAN")}
        actual = {item.get("id") for item in selected
                  if isinstance(item.get("id"), str)}
        if expected != actual or len(actual) != len(selected):
            return False
    else:
        return False
    owners = doc.get("active_owners", [])
    if not isinstance(owners, list):
        return False
    states = {item.get("id"): item.get("state") for item in rows
              if isinstance(item, dict) and isinstance(item.get("id"), str)}
    return all(isinstance(owner, dict) and
               isinstance(owner.get("item"), str) and
               owner["item"] in states and
               states[owner["item"]] not in
               ("VERIFIED", "DROPPED", "SUPERSEDED", "FROZEN")
               for owner in owners)


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
    if isinstance(outputs, dict):
        if outputs.get("view") == "DERIVED_VIEW":
            result = read_catalog(outputs, outputs.get("expected"))
            if result.get("state") == "complete":
                if "results" in health:
                    health["results"]["state"] = "available"
                    health["results"]["completed"] = True
                    health["results"]["completed_at"] = result.get("completed_at")
            else:
                if "results" in health:
                    health["results"]["state"] = "invalid"
                    health["results"]["completed"] = False
                    health["results"]["error"] = "invalid_projection"
        elif outputs.get("root") and outputs.get("attempt"):
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
    canonical_owners = []
    if ledger_state == "available" and isinstance(raw_rows, list):
        active_ids = {x.get("id") for x in raw_rows if isinstance(x, dict)
                      and x.get("state") not in ("VERIFIED", "DROPPED", "SUPERSEDED", "FROZEN")}
        owners = [x for x in owners if x.get("item") in active_ids]
        for x in raw_rows:
            if isinstance(x, dict) and x.get("state") == "DISPATCHED":
                handle = x.get("handle")
                if isinstance(handle, dict):
                    actor = handle.get("actor")
                    product = handle.get("product")
                    scope = handle.get("scope")
                    kind = handle.get("kind")
                    ref = handle.get("ref") or handle.get("id") or handle.get("task")
                else:
                    actor = x.get("actor")
                    product = x.get("product")
                    scope = x.get("scope")
                    kind = None
                    ref = None
                canonical_owners.append({
                    "item": x.get("id"),
                    "actor": actor,
                    "product": product,
                    "scope": scope,
                    "kind": kind,
                    "ref": ref,
                    "state": "DISPATCHED"
                })
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
    prohibitions = _evaluate_prohibitions(docs, health, raw_rows)
    history = [{"id": t.get("id"), "title": t.get("text"), "state": t.get("state"),
                "note": t.get("reason") if isinstance(t.get("reason"), str) else None}
               for t in raw_rows if isinstance(t, dict) and t.get("state") in past_states]
    for pid, pdata in prohibitions.items():
        if not any(h.get("id") == pid for h in history):
            history.append(pdata)
    history_count = len(history)
    current = [t for t in tasks if t.get("ledger_state") not in past_states and t.get("id") not in prohibitions]
    pending_list = None
    pending = None
    pending_total = None
    pending_truncated = False
    pending_source = None
    if ledger_state == "available":
        selected = [x for x in raw_rows if isinstance(x, dict) and
                    x.get("state") not in past_states and
                    x.get("id") not in prohibitions and
                    isinstance(x.get("blocked_by"), str) and
                    x["blocked_by"].startswith("user:")]
        pending_list = [{"id": str(x.get("id", ""))[:100],
                         "question": x["blocked_by"][5:][:2000],
                         "reason": str(x.get("text") or "")[:2000],
                         "created": x.get("created") if type(x.get("created")) in
                         (int, float) else None} for x in selected[:PENDING_LIMIT]]
        pending = len(pending_list)
        pending_total = len(selected)
        pending_truncated = len(selected) > PENDING_LIMIT
        pending_source = "ledger"
    else:
        pending_dir = directive.get("pending_decisions") if isinstance(directive, dict) else None
        if isinstance(pending_dir, list):
            pending_dir = sum(isinstance(x, dict) and x.get("status") in
                              ("PENDING", "WAITING_HUMAN") for x in pending_dir)
        if type(pending_dir) is int and pending_dir >= 0 and health.get("directive", {}).get("state") == "available":
            pending = pending_dir
            pending_total = pending_dir
            pending_source = "directive"
    match_warnings = []
    if health.get("directive", {}).get("state") == "stale":
        if directive_matches(directive, health["directive"], ledger_state,
                             activity["sources"].get("ledger", {}),
                             raw_rows, selected if ledger_state == "available" else []):
            health["directive"]["state"] = "available"
            health["directive"]["basis"] = "ledger_match"
        else:
            match_warnings.append("directive_mismatch")
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
    real_progress = None
    if activity.get("sources", {}).get("work", {}).get("state") == "available":
        for t in tasks:
            if "work" in t.get("source_keys", []) and t.get("progress_at"):
                real_progress = t["progress_at"]
    clocks = {
        "observed_ms": now_ms,
        "historical_completed_at": result.get("completed_at") if result.get("state") == "complete" else None,
        "directive_updated_at": directive.get("updated_at") if isinstance(directive, dict) and health.get("directive", {}).get("state") in ("available", "stale") else None,
        "progress_at": real_progress,
        "heartbeat_at": heartbeat.get("ts_iso") or heartbeat.get("at") if isinstance(heartbeat, dict) and health.get("heartbeat", {}).get("state") == "available" else None,
    }
    return {"schema_version": 1, "observer_only": True, "installed": False,
            "generated_ms": now_ms, "sources": {**activity["sources"], **health},
            "kpis": {"processes": sum(p["state"] == "verified" for p in proc),
                     "maker_cells": result["count"], "liquidations": count,
                     "pending_decisions": pending, "pending_total": pending_total,
                     "pending_truncated": pending_truncated,
                     "pending_source": pending_source,
                     "pending_list": pending_list},
            "processes": proc, "maker": {"phase": stage.get("maker_phase"),
                                        "results": result,
                                        "lease": stage.get("lease_status"),
                                        "output": stage.get("output_status")},
            "heartbeat": {"at": heartbeat.get("ts_iso") or heartbeat.get("at"), "status": heartbeat.get("status"),
                          "age_ms": health.get("heartbeat", {}).get("age_ms")},
            "directive": {"owners": owners, "canonical_owners": canonical_owners},
            "canonical_owners": canonical_owners, "clocks": clocks, "current": current[-4096:],
            "routes": routes, "recent": recent, "history": history[-4096:],
            "history_count": history_count,
            "source_totals": activity["totals"],
            "warnings": activity["warnings"] + node_warnings + match_warnings}
