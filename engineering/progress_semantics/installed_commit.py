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

if __name__ == "__main__":
    sys.exit(base.main())
