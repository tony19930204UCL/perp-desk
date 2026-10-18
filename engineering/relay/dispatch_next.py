#!/usr/bin/env python3
"""Source-only, fail-closed one-shot dispatch orchestrator. No runtime installation."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def absolute_file(value):
    p = Path(value)
    if not p.is_absolute() or not p.is_file():
        raise ValueError("ABSOLUTE_EXISTING_FILE_REQUIRED:" + value)
    return p

def atomic(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("RECEIPT_EXISTS_NO_RETRY")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())

def invoke(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        record = {"argv": argv, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
        try:
            record["json"] = json.loads(p.stdout)
        except (ValueError, TypeError):
            record["json_error"] = "MALFORMED_JSON"
        return record
    except Exception as exc:
        return {"argv": argv, "launch_error": repr(exc)}

def run(config):
    receipt = {"started_at": utc(), "status": "PRECHECK_FAILED", "source_only": True,
               "deployed": False, "steps": [], "send_attempted": False}
    for key in ("preflight", "queue", "commit", "job", "job_dir", "state_dir", "receipt_path"):
        if not isinstance(config.get(key), str):
            raise ValueError("CONFIG_REQUIRED:" + key)
    for key in ("preflight", "queue", "commit", "job"):
        absolute_file(config[key])
    for key in ("job_dir", "state_dir", "receipt_path"):
        if not Path(config[key]).is_absolute():
            raise ValueError("ABSOLUTE_PATH_REQUIRED:" + key)
    if not Path(config["job_dir"]).is_dir():
        raise ValueError("JOB_DIR_MISSING")
    job = json.loads(Path(config["job"]).read_text(encoding="utf-8"))
    if not isinstance(job, dict) or not isinstance(job.get("state_dir"), str) or Path(job["state_dir"]).resolve() != Path(config["state_dir"]).resolve():
        raise ValueError("JOB_STATE_MISMATCH")
    for key in ("repository", "branch", "task_id"):
        if not job.get(key) or job.get(key) != config.get(key):
            raise ValueError("EXACT_TARGET_MISMATCH:" + key)
    if not isinstance(config.get("instruction_id"), str) or not config["instruction_id"]:
        raise ValueError("INSTRUCTION_ID_REQUIRED")
    receipt.update({k: config[k] for k in ("repository", "branch", "task_id", "instruction_id")})
    receipt["job"] = config["job"]
    state = Path(config["state_dir"])
    receipt_path = Path(config["receipt_path"])
    if receipt_path.exists():
        raise FileExistsError("RECEIPT_EXISTS_NO_RETRY")
    # Do not launch a sending command until ownership and a send-incapable observer are explicit.
    approval = config.get("exact_target_approval_owner")
    observer = config.get("observer_argv")
    if not isinstance(approval, str) or not approval.strip() or not isinstance(observer, list) or not observer:
        receipt["blocker"] = "MISSING_EXACT_TARGET_APPROVAL_OWNER_OR_OBSERVER"
        atomic(receipt_path, receipt)
        return receipt
    if any(not isinstance(x, str) or not x for x in observer):
        raise ValueError("OBSERVER_ARGV_INVALID")
    absolute_file(observer[0])
    if config.get("observer_send_incapable_verified") is not True:
        receipt["blocker"] = "OBSERVER_SEND_INCAPABILITY_UNVERIFIED"
        atomic(receipt_path, receipt)
        return receipt
    if config.get("model") == "Instant" and config.get("check_model"):
        receipt["blocker"] = "INSTALLED_CHECK_MODEL_HIGH_ONLY"
        atomic(receipt_path, receipt)
        return receipt
    if (state / "active_instruction.json").exists() or (state / ("send_" + config["instruction_id"] + ".json")).exists():
        receipt["status"] = "UNSENT"
        receipt["blocker"] = "ACTIVE_OR_PRIOR_SEND_NO_RETRY"
        atomic(receipt_path, receipt)
        return receipt
    pre = [sys.executable, config["preflight"], config["job_dir"]]
    if config.get("renew"): pre.append("--renew")
    if config.get("check_model"): pre.append("--check-model")
    result = invoke(pre)
    receipt["steps"].append({"kind": "preflight", **result})
    if result.get("returncode") != 0 or not isinstance(result.get("json"), dict) or result["json"].get("ok") is not True:
        atomic(receipt_path, receipt)
        return receipt
    # A durable intent is created before the sole potentially sending operation.
    receipt["status"] = "SEND_UNKNOWN"
    receipt["send_attempted"] = True
    atomic(receipt_path, receipt)
    dispatch = invoke([sys.executable, config["queue"], "step", "--job", config["job"]])
    receipt["steps"].append({"kind": "queue", **dispatch})
    outcome = dispatch.get("json")
    receipt["queue_status"] = outcome.get("status") if isinstance(outcome, dict) else None
    receipt["item"] = outcome.get("item") if isinstance(outcome, dict) else None
    receipt["dispatched_instruction_id"] = outcome.get("instruction_id") if isinstance(outcome, dict) else None
    send_file = state / ("send_" + config["instruction_id"] + ".json")
    if send_file.is_file():
        try:
            receipt["send_receipt"] = json.loads(send_file.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            receipt["send_receipt_error"] = repr(exc)
    send = receipt.get("send_receipt")
    if (receipt["queue_status"] == "DISPATCHED" and
        receipt["dispatched_instruction_id"] == config["instruction_id"] and
        isinstance(send, dict) and send.get("status") == "CONFIRMED" and
        send.get("evidence_kind") == "EXACT_RENDERED_USER_BUBBLE" and
        send.get("text_hash") and send.get("confirmed_at")):
        receipt["status"] = "CONFIRMED_DELIVERY"
        # commit add can only run with a verified observer and explicit approval owner.
        cmd = [sys.executable, config["commit"], "add", config["ledger_text"],
               "--run", *observer, "--accept", "manual", "--due", "+2h"]
        reg = invoke(cmd)
        receipt["steps"].append({"kind": "ledger_registration", **reg})
        if reg.get("returncode") == 0 and reg.get("stdout", "").strip():
            receipt["status"] = "WATCH_REGISTERED"
            receipt["ledger_stamp_raw"] = reg["stdout"]
        else:
            receipt["status"] = "REGISTRATION_FAILED"
    elif receipt["queue_status"] in ("WAITING", "ALL_DONE", "REVIEW_REQUIRED") and not send:
        receipt["status"] = "SEND_UNKNOWN"  # step was capable of sending; never infer UNSENT
    receipt["finished_at"] = utc()
    # The original intent is immutable: a distinct outcome file preserves both snapshots.
    outcome_path = receipt_path.with_name(receipt_path.name + ".outcome.json")
    atomic(outcome_path, receipt)
    return receipt

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    cfg = json.loads(absolute_file(args.config).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError("CONFIG_OBJECT_REQUIRED")
    result = run(cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "WATCH_REGISTERED" else 2

if __name__ == "__main__":
    sys.exit(main())
