#!/usr/bin/env python3
"""Installed commitment-gate entrypoint.

This replacement loads the byte-for-byte pre-install checker backup, overrides
only evaluate(), and delegates its unchanged CLI/locks/acceptance operations.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
BASE = HERE.with_name("commit.py.pa1-base")
PA1 = HERE.with_name("pa1")
if str(PA1.parent) not in sys.path:
    sys.path.insert(0, str(PA1.parent))

if not BASE.is_file():
    raise SystemExit("PA1 install invalid: base checker backup unavailable")
loader = SourceFileLoader("_pa1_commit_base", str(BASE))
spec = importlib.util.spec_from_loader(loader.name, loader)
if not spec:
    raise SystemExit("PA1 install invalid: base checker backup unavailable")
base = importlib.util.module_from_spec(spec)
loader.exec_module(base)

# Installed sibling directory is <plugin>/pa1/progress_semantics/.
from pa1.progress_semantics.watcher_adapter import assess_profile

REQ = "commitments/progress_requirements.json"

def _requirements(home, cid):
    path = Path(home) / REQ
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return None
    rows = data.get("items")
    if not isinstance(rows, dict):
        return None
    row = rows.get(cid)
    return dict(row) if isinstance(row, dict) else None

def _progress_problem(it, now):
    """Return None for healthy recurring work, otherwise an alert problem."""
    handle = it.get("handle") or {}
    if handle.get("kind") != "cron":
        return None
    req = _requirements(base.home(), it["id"])
    # Missing mapping is truthful unknown, but does not globally exempt cron:
    # preserve the original baseline stale/dead/due behavior for unmapped items.
    if req is None:
        return None
    try:
        result = assess_profile(base.home(), it["id"],
                                base.datetime.fromtimestamp(now).astimezone(), req)
    except Exception as exc:
        return "進度證據讀取失敗（%s）" % str(exc)[:80]
    state, reason = result.get("state"), result.get("reason_code")
    if state == "OK":
        return ""
    if state == "GAP":
        return "進度覆蓋缺口（%s）" % reason
    if state in ("SAFETY", "OVERDUE"):
        return "週期工作失敗（%s）" % reason
    if state in ("STALE", "NEEDS_RECLASSIFY"):
        return "週期進度證據不足（%s）" % reason
    return "週期進度狀態未知（%s）" % reason

def evaluate(led, now=None):
    """Original evaluator plus recurring source evidence before generic staleness."""
    now = now or time.time()
    alerts = []
    for it in led.data["items"]:
        if it["state"] in base.TERMINAL:
            continue
        problem = None
        hs, detail = base.handle_status(it.get("handle")) if it["state"] == base.DISPATCHED else ("dead", "no handle")
        # Preserve deterministic acceptance and its exact mutation/history path.
        if it["state"] == base.DISPATCHED and it.get("accept") and it["accept"] != "manual":
            ok, ev = base.accept_ok(it["accept"], it)
            if ok:
                it["state"] = base.VERIFIED
                it["evidence"] = ev
                base.touch(it, "auto-verified: " + ev)
                continue
        if it["state"] == base.OPEN:
            if it.get("blocked_by"):
                sat, detail = base.blocker_state(led, it["blocked_by"])
                if sat:
                    problem = "阻擋已解除（%s）但還沒派工" % detail
                elif "dropped" in detail or "does not exist" in detail:
                    problem = "阻擋無效（%s），須重新決定" % detail
                elif it["blocked_by"].startswith("user:") and now - it["created"] > base.OPEN_GRACE_H * 3600:
                    problem = "等使用者回答已超過 %.0f 小時：%s" % ((now - it["created"]) / 3600, it["blocked_by"][5:80])
            elif it.get("defer"):
                if it.get("due") and now > it["due"]:
                    problem = "延後到期，須重新決定：派工、再延後（附新理由）或放棄"
            else:
                problem = "沒有有效理由（舊式免證據理由）：須派工、掛上可檢查的阻擋、或明說延後"
        elif it["state"] == base.BLOCKED:
            problem = "被擋住：%s" % (it.get("reason") or "無原因")
        elif it["state"] == base.DISPATCHED:
            if hs == "dead":
                problem = "handle 已消失（%s）而驗收未通過" % detail
            elif hs == "finished":
                problem = "handle 已結束（%s）但驗收未通過或待人工驗收" % detail
            elif it.get("due") and now > it["due"]:
                problem = "已逾期（到期 %s），handle 狀態：%s" % (base.fmt_t(it["due"]), hs)
            else:
                recurring = _progress_problem(it, now)
                if recurring:
                    problem = recurring
                elif recurring == "":
                    problem = None
                elif now - it["updated"] > base.STALE_H * 3600:
                    problem = "%.0f 小時沒有任何進展紀錄" % ((now - it["updated"]) / 3600)
        if problem:
            sig = problem.split("（")[0]
            if it.get("alert_sig") == sig and now - it.get("alerted", 0) < base.REPEAT_H * 3600:
                continue
            it["alert_sig"], it["alerted"] = sig, now
            alerts.append("%s %s：%s" % (it["id"], it["text"][:50], problem))
    return alerts

base.evaluate = evaluate

SUCCESSOR_SCHEMA = 1
SUCCESSOR_NAME = "needs_successor.json"
MANUAL_LEASE_S = float(os.environ.get("COMMIT_MANUAL_LEASE_S", 6 * 3600))

def _successor_paths():
    root = Path(base.home())
    return root / "commitments" / "ledger.json", root / "commitments" / SUCCESSOR_NAME

def _manual_heartbeat(root, item):
    h = item.get("handle") or {}
    ref = str(h.get("ref") or "")
    if not ref or "/" in ref or "\\" in ref or ref in (".", ".."):
        return None
    p = root / "commitments" / "heartbeats" / (ref + ".json")
    try:
        data = json.loads(p.read_text("utf-8"))
        at = float(data["at"])
        if data.get("item_id") != item.get("id") or data.get("handle_ref") != ref:
            return None
        return at
    except Exception:
        return None

def _successor_reason(root, item, now, lease_s):
    if item.get("state") == base.DISPATCHED:
        h = item.get("handle") or {}
        if h.get("kind") == "pid":
            state, detail = base.handle_status(h)
            if state == "dead":
                return "dead pid handle: " + detail
            return None
        if h.get("kind") == "manual":
            age = now - float(item.get("created") or now)
            if age < lease_s:
                return None
            beat = _manual_heartbeat(root, item)
            if beat is not None and -1 <= now - beat <= lease_s:
                return None
            return "manual executor lease expired without fresh heartbeat"
    if item.get("state") == base.BLOCKED:
        blocker = item.get("blocked_by")
        if not isinstance(blocker, str) or not blocker.startswith(("item:", "file:", "user:", "cmd:")):
            return "blocked item has no checkable blocker"
    return None

def scan_successors(now=None, lease_s=None):
    """Read ledger under its existing lock and atomically refresh successor queue."""
    now = time.time() if now is None else float(now)
    lease_s = MANUAL_LEASE_S if lease_s is None else float(lease_s)
    ledger_path, queue_path = _successor_paths()
    lock_path = Path(str(ledger_path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "a+")
    try:
        base.fcntl.flock(lock, base.fcntl.LOCK_SH)
        try:
            data = json.loads(ledger_path.read_text("utf-8"))
        except Exception as exc:
            raise RuntimeError("successor scan cannot read ledger: %s" % str(exc)[:100])
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("successor scan ledger schema invalid")
        prior = {}
        if queue_path.exists():
            try:
                q = json.loads(queue_path.read_text("utf-8"))
                if q.get("schema_version") == SUCCESSOR_SCHEMA:
                    prior = {x["id"]: x for x in q.get("items", []) if isinstance(x, dict) and "id" in x}
            except Exception:
                prior = {}
        rows = []
        root = Path(base.home())
        for item in data["items"]:
            reason = _successor_reason(root, item, now, lease_s)
            if reason:
                old = prior.get(item.get("id"), {})
                rows.append({"id": item.get("id"), "kind": (item.get("handle") or {}).get("kind") or "blocked",
                             "evidence": reason, "first_seen": old.get("first_seen", now), "last_seen": now})
        rows.sort(key=lambda x: x["id"])
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema_version": SUCCESSOR_SCHEMA, "items": rows}, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, queue_path)
        return rows
    finally:
        base.fcntl.flock(lock, base.fcntl.LOCK_UN)
        lock.close()

def successor_lines():
    rows = scan_successors()
    return ["NEEDS_SUCCESSOR %s %s: %s" % (x["id"], x["kind"], x["evidence"]) for x in rows]

_base_main = base.main

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[-1] in ("check", "attention"):
        rc = _base_main(argv)
        try:
            for line in successor_lines():
                print(line)
        except Exception as exc:
            print("NEEDS_SUCCESSOR_SCAN_ERROR: %s" % str(exc)[:160])
            return 4
        return rc
    return _base_main(argv)

base.main = main

def __getattr__(name):
    """Preserve the original commit.py public module API for legacy importers.

    The wrapper owns only its explicit PA1 names; every otherwise missing public
    attribute is delegated to the byte-for-byte original module. This keeps
    existing consumers on the original Ledger/constants/helpers without copying
    or reimplementing their authority.
    """
    try:
        return getattr(base, name)
    except AttributeError:
        raise AttributeError("module %r has no attribute %r" % (__name__, name)) from None

def __dir__():
    return sorted(set(globals()) | set(dir(base)))

if __name__ == "__main__":
    sys.exit(main())
