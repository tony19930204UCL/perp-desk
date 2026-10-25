#!/usr/bin/env python3
"""relay_dispatch_worker.py - model-free dispatch + tracking holder for ONE instruction.

Loop until --deadline-min: wait for a logged-in dedicated Chrome, run preflight, call
chatgpt_page_io.py send EXACTLY ONCE (receipt state machine forbids a second attempt),
then read-only track the GitHub mailbox for the exact instruction id.
Writes <job-dir>/worker-state.json every cycle and worker-result.json at the end.
Never clicks approval cards, never resends after SEND_INTENT, never touches main.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import product_dispatch as pd

HERE = Path(__file__).resolve().parent
IO = str(HERE / "chatgpt_page_io.py")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mailbox_matches(content_b64, instruction_id):
    """True only for a schema-v2 mailbox JSON carrying exactly this instruction id."""
    try:
        d = json.loads(base64.b64decode(content_b64).decode("utf-8"))
    except Exception:
        return False
    return isinstance(d, dict) and d.get("schema_version") == 2 and d.get("instruction_id") == instruction_id


def preflight(expect_head, actual_head):
    """expect_head None = no head pin (new branch). Mismatch aborts the dispatch (state changed since authorisation)."""
    if expect_head and actual_head != expect_head:
        return False, "HEAD_CHANGED expected=%s actual=%s" % (expect_head[:12], str(actual_head)[:12])
    return True, "OK"


def may_send(receipt_stage):
    """Worker-level guard mirroring the receipt machine: send only when nothing was ever clicked/intended."""
    return receipt_stage in (None, "NEW", "PRESEND_FAILED", "DRAFT_VERIFIED", "DRY_RUN_CLEARED")


def run_json(cmd, timeout):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    last = [l for l in p.stdout.splitlines() if l.startswith("{")]
    return p.returncode, (json.loads(last[-1]) if last else {"raw": p.stdout[-300:], "err": p.stderr[-300:]})


def gh_json(args):
    p = subprocess.run(["gh", "api"] + args, capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None


def write_atomic(path, obj):
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", required=True)
    ap.add_argument("--instruction-id", required=True)
    ap.add_argument("--prompt-file", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--chat-url")
    g.add_argument("--new", action="store_true")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--mailbox-path", required=True)
    ap.add_argument("--expect-head")
    ap.add_argument("--deadline-min", type=float, default=240)
    ap.add_argument("--poll-s", type=float, default=90)
    a = ap.parse_args(argv)
    job = Path(a.job_dir)
    job.mkdir(parents=True, exist_ok=True)
    policy = json.loads((job / "job.json").read_text(encoding="utf-8"))
    if policy.get("repository") != a.repo or policy.get("branch") != a.branch:
        raise pd.DispatchBlocked("WORKER_SCOPE_MISMATCH")
    template = (Path(__file__).with_name("instruction-template.txt")).read_text(encoding="utf-8")
    guidance = template.format(repository=policy["repository"], pr_number=policy["pr_number"],
        branch=policy["branch"], chat_url=policy["dispatch_chat_url"],
        instruction_id=a.instruction_id, write_allowlist=json.dumps(policy["write_allowlist"]),
        valid_until=policy["valid_until"])
    body = Path(a.prompt_file).read_text(encoding="utf-8")
    prompt_path = job / ("bounded_" + a.instruction_id + ".txt")
    rpath = job / ("receipt-%s.json" % a.instruction_id)
    if prompt_path.exists() and not rpath.exists():
        raise pd.DispatchBlocked("BOUNDED_PROMPT_EXISTS")
    if not prompt_path.exists():
        prompt_path.write_text(guidance + "\n\n" + body, encoding="utf-8")
    owner = pd.claim(policy, job, a.instruction_id,
                     chat=policy.get("dispatch_chat_url"))
    deadline = time.monotonic() + a.deadline_min * 60
    st = {"instruction_id": a.instruction_id, "started": now(), "phase": "WAIT_LOGIN", "send": None, "handback": False, "cycles": 0}
    sent_done = False
    while time.monotonic() < deadline:
        st["cycles"] += 1
        st["at"] = now()
        stage = json.loads(rpath.read_text())["stage"] if rpath.exists() else None
        if not sent_done and not may_send(stage):
            sent_done = True  # a previous attempt exists: never send again, only track
            st["send"] = {"status": "PRIOR_RECEIPT_" + str(stage)}
        if not sent_done:
            rc, lg = run_json([sys.executable, IO, "login-status"], 150)
            st["logged_in"] = lg.get("logged_in")
            if lg.get("logged_in"):
                br = gh_json(["repos/%s/branches/%s" % (a.repo, a.branch)])
                head = br["commit"]["sha"] if br else None
                ok, why = preflight(a.expect_head, head)
                st["preflight"] = why
                if not ok:
                    st["phase"] = "ABORTED_PREFLIGHT"
                    break
                cmd = [sys.executable, IO, "send", "--job-dir", str(job), "--instruction-id", a.instruction_id,
                       "--prompt-file", str(prompt_path),
                       "--dispatch-ticket", owner["send_ticket"]] + (["--new"] if a.new else ["--chat-url", a.chat_url])
                rc, res = run_json(cmd, 420)
                st["send"] = res
                if res.get("status") in ("CONFIRMED", "SEND_UNKNOWN"):
                    sent_done = True
                    st["phase"] = "TRACKING" if res["status"] == "CONFIRMED" else "TRACKING_SEND_UNKNOWN"
                else:
                    st["phase"] = "PRESEND_RETRY_NEXT_CYCLE"
            else:
                st["phase"] = "WAIT_LOGIN"
        if sent_done:
            f = gh_json(["repos/%s/contents/%s?ref=%s" % (a.repo, a.mailbox_path, a.branch)])
            br = gh_json(["repos/%s/branches/%s" % (a.repo, a.branch)])
            st["branch_head"] = br["commit"]["sha"] if br else None
            if f and mailbox_matches(f.get("content", ""), a.instruction_id):
                st["handback"] = True
                st["phase"] = "HANDBACK_FOUND"
                st["mailbox_blob"] = f.get("sha")
                break
        write_atomic(job / "worker-state.json", st)
        time.sleep(a.poll_s)
    if st["phase"] not in ("HANDBACK_FOUND", "ABORTED_PREFLIGHT"):
        st["phase"] = "EXPIRED_" + st["phase"]
    st["finished"] = now()
    write_atomic(job / "worker-state.json", st)
    write_atomic(job / "worker-result.json", st)
    print(json.dumps(st, ensure_ascii=False))
    return 0 if st["phase"] in ("HANDBACK_FOUND",) else 3


if __name__ == "__main__":
    sys.exit(main())
