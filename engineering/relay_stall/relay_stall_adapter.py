#!/usr/bin/env python3
"""relay_stall_adapter.py - Production adapter and consumer intake wrapper."""

import argparse
import base64
import datetime
import fcntl
import json
import math
import os
import subprocess
import sys
import time
from typing import Any

import relay_stall_watch as rsw

ADAPTER_VER = 1
REGISTRY_PRS = {75, 78}

DEC_WAIT_PROV = "WAIT_PROVIDER_REVIEW"
DEC_WAIT_HUMAN = "WAIT_HUMAN"
DEC_INTERNAL = "INTERNAL_REVIEW"

TERMINAL_STATES = {"COMPLETED", "CLOSED", "DROPPED", "VERIFIED"}


def parse_iso_epoch(val: Any) -> float | None:
    """Parse ISO8601 string or numeric timestamp to epoch float."""
    if val is None or type(val) is bool:
        return None
    if isinstance(val, (int, float)) and math.isfinite(val):
        return float(val)
    if isinstance(val, str) and val.strip():
        try:
            clean = val.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(clean)
            return dt.timestamp()
        except Exception:
            return None
    return None


def check_pid_liveness(pid: Any, start_id: str | None = None) -> tuple[bool | None, bool | None]:
    """Check process liveness via /proc and compare start identity."""
    if type(pid) is bool or not isinstance(pid, int) or pid <= 0:
        return None, None

    proc_stat = f"/proc/{pid}/stat"
    if os.path.exists(proc_stat):
        try:
            with open(proc_stat, "r") as f:
                content = f.read()
            r_paren = content.rfind(")")
            if r_paren != -1:
                after_comm = content[r_paren + 1:].split()
                if len(after_comm) >= 20:
                    real_ticks = after_comm[19]
                    if start_id and str(start_id).strip():
                        return True, (str(start_id).strip() == real_ticks)
            return True, bool(start_id)
        except Exception:
            return True, bool(start_id)
    return False, False


def match_governing_item(
    pr_number: int | None,
    instruction_id: str,
    ledger_items: list[dict],
    transfer_data: dict | None = None,
    governing_item_id: str | None = None,
) -> dict | None:
    """Match governing ledger item dynamically by canonical pointer, transfer, or exact instruction."""
    target_ids = set()
    if governing_item_id:
        target_ids.add(str(governing_item_id).strip())

    if isinstance(transfer_data, dict):
        succ = transfer_data.get("successor")
        orig = transfer_data.get("original_full_item")
        if isinstance(succ, dict) and succ.get("id"):
            target_ids.add(succ.get("id"))
        elif isinstance(orig, dict) and orig.get("id"):
            target_ids.add(orig.get("id"))

    matched = []
    for item in ledger_items:
        if not isinstance(item, dict):
            continue
        st = str(item.get("state", "")).upper()
        # Exclude terminal states (VERIFIED, COMPLETED, CLOSED, DROPPED)
        if st in TERMINAL_STATES:
            continue
        item_id = item.get("id")

        # Canonical pointer or structured transfer match
        if target_ids and item_id in target_ids:
            matched.append(item)
            continue

        # Structured item field matching (no loose substring guessing)
        if instruction_id and (
            item.get("instruction_id") == instruction_id
            or (isinstance(item.get("scope"), dict) and item["scope"].get("instruction") == instruction_id)
        ):
            matched.append(item)

    if matched:
        matched.sort(key=lambda x: x.get("updated", x.get("created", 0)), reverse=True)
        return matched[0]
    return None


def adapt_job_entry(raw_entry: dict, ledger_items: list[dict], now: float) -> dict:
    """Adapt a raw snapshot job entry into normalized core schema."""
    job_meta = raw_entry.get("job", {})
    files = raw_entry.get("files", {})
    pr_num = job_meta.get("pr_number")

    act_inst = files.get("active_instruction.json", {})
    inst_id = act_inst.get("instruction_id") or job_meta.get("instruction_id") or ""
    repo = job_meta.get("repository") or ""
    branch = job_meta.get("branch") or ""
    job_id = job_meta.get("job_id") or (f"PR{pr_num}_{inst_id}" if pr_num and inst_id else "UNKNOWN_JOB")

    # Expiry extraction
    exp = parse_iso_epoch(job_meta.get("valid_until"))

    # Head extraction
    head_obj = raw_entry.get("head")
    head_sha = head_obj.get("sha", "") if isinstance(head_obj, dict) else ""

    # Owner extraction
    w_launch = files.get("worker-launch.json", {})
    claim = files.get("POSITIVE_CLAIM_RECONCILE.json", {}).get("owner", {})
    claim_rb = files.get("claim_readback", {}).get("owner", {})

    pid = w_launch.get("pid") or claim.get("pid") or claim_rb.get("pid")
    start_id = str(w_launch.get("start") or claim.get("start_ticks") or claim_rb.get("start_ticks") or "")

    # Live check via /proc readback
    is_alive, id_ok = check_pid_liveness(pid, start_id)

    owner = {
        "kind": "pid",
        "pid": pid,
        "start_id": start_id,
        "alive": is_alive,
        "identity_ok": id_ok,
    }

    # Chat extraction: check chat_snapshot or chat
    chat_rc = raw_entry.get("chat_rc")
    chat = None
    if chat_rc == 0 or (chat_rc is None and ("chat_snapshot" in raw_entry or "chat" in raw_entry)):
        raw_chat = raw_entry.get("chat_snapshot") or raw_entry.get("chat")
        if isinstance(raw_chat, dict):
            p_card = raw_chat.get("pending_card")
            busy_val = raw_chat.get("busy")
            c_obs = raw_chat.get("observed_at") or raw_entry.get("chat_observed_at") or raw_entry.get("observed_at")
        else:
            p_card = raw_entry.get("pending_card")
            busy_val = raw_entry.get("busy")
            c_obs = raw_entry.get("chat_observed_at") or raw_entry.get("observed_at")

        chat = {
            "observed_at": c_obs,
            "pending_card": p_card,
            "busy": busy_val,
        }

    # Send status and immutable receipt_ref
    send_file = files.get(f"send_{inst_id}.json", {})
    rcpt_file = files.get(f"receipt-{inst_id}.json", {})
    send_status = "SEND_UNKNOWN"
    receipt_ref = None

    if send_file.get("status") == "CONFIRMED" or send_file.get("stage") == "CONFIRMED":
        send_status = "CONFIRMED"
        receipt_ref = send_file.get("reconciliation_artifact") or send_file.get("original_receipt") or send_file.get("message_id")
    elif rcpt_file:
        send_status = rcpt_file.get("stage", "SEND_UNKNOWN")
        receipt_ref = rcpt_file.get("receipt") or rcpt_file.get("target")

    # Mailbox & Handback check
    mb_rc = raw_entry.get("mailbox_rc")
    mb = raw_entry.get("mailbox")
    handback_valid = None
    if mb_rc == 0 and isinstance(mb, dict):
        if mb.get("schema_version") == 2 and mb.get("instruction_id") == inst_id:
            head_match = bool(head_sha and mb.get("source_head_sha") == head_sha)
            is_ready = (mb.get("status") == "READY_FOR_REVIEW")
            handback_valid = (head_match and is_ready)
        else:
            handback_valid = False

    # Extract bound instruction structurally from metadata/files without guessing
    bound_inst = (
        job_meta.get("dispatch_release_grant", {}).get("next_instruction")
        or files.get("worker-state.json", {}).get("instruction_id")
        or files.get("POSITIVE_CLAIM_RECONCILE.json", {}).get("approval_grant", {}).get("instruction_id")
        or files.get("claim_readback", {}).get("approval_grant", {}).get("instruction_id")
        or files.get("NORMAL_CHECKER_RESULT.json", {}).get("claim_readback", {}).get("approval_grant", {}).get("instruction_id")
    )

    transfer_data = (
        files.get("PARENT_PROVIDER_BLOCK_SCOPE_TRANSFER.json")
        or files.get("PARENT_OWNER_TRANSFER.json")
    )

    # Transfer data and policy evidence are only valid if active inst_id matches bound_inst
    valid_transfer = transfer_data if (inst_id and bound_inst and inst_id == bound_inst) else None
    gov_item_id = raw_entry.get("governing_item_id")

    # If registry tuple mismatch was flagged, do not apply canonical pointer or transfer
    if raw_entry.get("tuple_matched") is False:
        gov_item_id = None
        valid_transfer = None

    # Policy refusal evaluation
    gov_item = match_governing_item(pr_num, inst_id, ledger_items, valid_transfer, gov_item_id)
    policy_refusal = None  # Default None (UNKNOWN) if no evidence or unverified instruction

    if inst_id and bound_inst and inst_id == bound_inst and raw_entry.get("tuple_matched") is not False:
        is_refused = False

        if gov_item:
            text = str(gov_item.get("text", ""))
            blocked = str(gov_item.get("blocked_by", ""))
            if "PROVIDER_REFUSAL_REPORTED" in text or "PROVIDER_NORMAL_REVIEW" in blocked or "安全封鎖" in text:
                is_refused = True
            elif "TOKEN_LIKE" in text or "TOKEN_LIKE" in blocked or "明確拒絕" in text:
                is_refused = True

        safety_stop = files.get("SAFETY_STOP.json")
        if isinstance(safety_stop, dict):
            s_status = str(safety_stop.get("status") or "")
            if s_status == "PROVIDER_REFUSAL_REPORTED_BY_RECIPIENT":
                is_refused = True

        chk_res = files.get("NORMAL_CHECKER_RESULT.json")
        if isinstance(chk_res, dict):
            c_res = chk_res.get("result")
            c_err = chk_res.get("error")
            if c_res == "STOPPED" and "token-like" in str(c_err or "").lower():
                is_refused = True

        if is_refused:
            policy_refusal = True
        else:
            policy_refusal = None

    job_data = {
        "repository": repo,
        "branch": branch,
        "instruction_id": inst_id,
        "job_id": job_id,
        "expires_at": exp,
        "owner": owner,
        "head": head_sha,
        "send_status": send_status,
        "handback_valid": handback_valid,
        "policy_refusal": policy_refusal,
        "receipt_ref": receipt_ref,
    }
    if chat is not None:
        job_data["chat"] = chat

    return job_data


def get_consumer_status(state_dir: str | None, snapshot: dict | None = None) -> dict:
    """Read true latest consumer run receipt and enabled flag."""
    enabled = True
    if isinstance(snapshot, dict):
        if "consumer" in snapshot and isinstance(snapshot["consumer"], dict):
            if "enabled" in snapshot["consumer"]:
                enabled = bool(snapshot["consumer"]["enabled"])
        elif "cron_enabled" in snapshot:
            enabled = bool(snapshot["cron_enabled"])
        elif "c0041_enabled" in snapshot:
            enabled = bool(snapshot["c0041_enabled"])

    if not state_dir or not os.path.isdir(state_dir):
        return {"enabled": enabled, "last_receipt_at": None}

    cfg_file = os.path.join(state_dir, "consumer_config.json")
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if "enabled" in cfg:
                    enabled = bool(cfg["enabled"])
        except Exception:
            pass

    latest_ts = None
    cruns_file = os.path.join(state_dir, "consumer_runs.jsonl")
    if os.path.isfile(cruns_file):
        try:
            with open(cruns_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    if row.get("status") == "SUCCESS":
                        rec = row.get("recorded_at")
                        if isinstance(rec, (int, float)) and math.isfinite(rec):
                            if latest_ts is None or rec > latest_ts:
                                latest_ts = float(rec)
        except Exception:
            pass

    if latest_ts is None:
        rcpt_file = os.path.join(state_dir, "receipts.jsonl")
        if os.path.isfile(rcpt_file):
            try:
                with open(rcpt_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        row = json.loads(line)
                        rec = row.get("recorded_at")
                        if isinstance(rec, (int, float)) and math.isfinite(rec):
                            if latest_ts is None or rec > latest_ts:
                                latest_ts = float(rec)
            except Exception:
                pass

    return {"enabled": enabled, "last_receipt_at": latest_ts}


def build_normalized(snapshot_path: str, state_dir: str | None, now: float) -> dict:
    """Build normalized core schema document from source snapshot."""
    with open(snapshot_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    ledger_items = snapshot.get("ledger_items", [])
    raw_jobs = snapshot.get("jobs", [])

    adapted_jobs = []
    for raw_j in raw_jobs:
        adapted = adapt_job_entry(raw_j, ledger_items, now)
        adapted_jobs.append(adapted)

    consumer = get_consumer_status(state_dir, snapshot=snapshot)
    return {
        "schema_version": 1,
        "consumer": consumer,
        "jobs": adapted_jobs,
    }


def record_consumer_decision(event: dict, ledger_items: list[dict], now: float) -> str:
    """Determine durable consumer intake action for a pending event."""
    ident = event.get("identity", {})
    job_id = ident.get("job_id", "")
    inst_id = ident.get("instruction_id", "")
    states = event.get("states", [])

    if "POLICY_REFUSAL" in states:
        pr_tag = "PR75" if "PR75" in job_id or "maker" in inst_id.lower() else "PR78"
        for item in ledger_items:
            text = item.get("text", "")
            if pr_tag in text:
                if "PROVIDER_REFUSAL_REPORTED" in text or "PROVIDER_NORMAL_REVIEW" in text or "安全封鎖" in text:
                    return DEC_WAIT_PROV
                if "TOKEN_LIKE" in text or "明確拒絕" in text:
                    return DEC_WAIT_HUMAN
        return DEC_WAIT_PROV if pr_tag == "PR75" else DEC_WAIT_HUMAN

    return DEC_INTERNAL


def run_consumer_intake(state_dir: str, snapshot_path: str | None, now: float) -> int:
    """Read pending faults, record durable journal entry, perform core ack, and write consumer run receipt."""
    if not os.path.isdir(state_dir):
        sys.stderr.write(f"State dir missing: {state_dir}\n")
        return 1

    pending_file = os.path.join(state_dir, "pending.json")
    if not os.path.isfile(pending_file):
        sys.stderr.write("No pending.json in state dir\n")
        return 0

    try:
        with open(pending_file, "r", encoding="utf-8") as f:
            pending = json.load(f)
        if not isinstance(pending, list):
            sys.stderr.write("pending.json is not a list\n")
            return 1
    except Exception as e:
        sys.stderr.write(f"Failed to read pending.json: {e}\n")
        return 1

    ledger_items = []
    if snapshot_path and os.path.isfile(snapshot_path):
        try:
            with open(snapshot_path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            ledger_items = snap.get("ledger_items", [])
        except Exception:
            pass

    journal_file = os.path.join(state_dir, "consumer_journal.jsonl")
    journal_count = 0
    ack_count = 0
    ack_refs = []

    for ev in pending:
        if not isinstance(ev, dict) or not ev.get("event_id"):
            continue
        ev_id = ev["event_id"]
        if ev.get("ack_receipt_id"):
            continue

        decision = record_consumer_decision(ev, ledger_items, now)
        journal_entry = {
            "event_id": ev_id,
            "fingerprint": ev.get("fingerprint"),
            "decision": decision,
            "recorded_at": now,
            "receipt_ref": ev.get("receipt_ref"),
        }
        rsw.append_jsonl(journal_file, journal_entry)
        journal_count += 1

        rc = rsw.run_ack(state_dir, ev_id, decision, now)
        if rc != 0:
            sys.stderr.write(f"Failed to ack event {ev_id}\n")
            return rc
        ack_count += 1
        ack_refs.append(ev_id)

    run_receipt = {
        "run_id": f"crun_{int(now)}_{os.urandom(4).hex()}",
        "recorded_at": now,
        "intake_count": len(pending),
        "journal_count": journal_count,
        "ack_count": ack_count,
        "ack_refs": ack_refs,
        "status": "SUCCESS",
    }
    rsw.append_jsonl(os.path.join(state_dir, "consumer_runs.jsonl"), run_receipt)
    return 0


def collect_live_snapshot(
    registry_path: str,
    now: float,
    explicit_profile_home: str | None = None,
) -> dict:
    """Collect fresh live snapshot by reading explicit job files and read-only commands."""
    if not os.path.isfile(registry_path):
        raise FileNotFoundError(f"Registry not found: {registry_path}")

    with open(registry_path, "r", encoding="utf-8") as f:
        registry = json.load(f)

    profile_home = explicit_profile_home or registry.get("profile_home")
    if not profile_home:
        raise ValueError("explicit_profile_home or registry.profile_home is required")
    profile_home = os.path.realpath(os.path.abspath(profile_home))
    if not os.path.isdir(profile_home):
        raise ValueError(f"profile_home does not exist or is not a directory: {profile_home}")

    # Read full valid ledger items
    ledger_items = []
    ledger_file = os.path.join(profile_home, "commitments", "ledger.json")
    if os.path.isfile(ledger_file):
        try:
            with open(ledger_file, "r", encoding="utf-8") as f:
                ldata = json.load(f)
            raw_items = ldata.get("items", [])
            items_list = list(raw_items.values()) if isinstance(raw_items, dict) else raw_items
            ledger_items = [i for i in items_list if isinstance(i, dict)]
        except Exception:
            pass

    jobs = []
    for job_spec in registry.get("jobs", []):
        if not isinstance(job_spec, dict):
            continue

        raw_job_path = job_spec.get("job_path")
        job_path = None
        source_err = None
        if raw_job_path:
            resolved_job_path = os.path.realpath(os.path.abspath(raw_job_path if os.path.isabs(raw_job_path) else os.path.join(profile_home, raw_job_path)))
            try:
                if os.path.commonpath([resolved_job_path, profile_home]) != profile_home:
                    source_err = "PATH_TRAVERSAL_REJECTED"
                else:
                    job_path = resolved_job_path
            except ValueError:
                source_err = "PATH_TRAVERSAL_REJECTED"
        else:
            source_err = "MISSING_JOB_FILE"

        repo = job_spec.get("repository", "")
        branch = job_spec.get("branch", "")
        inst_id = job_spec.get("instruction_id", "")
        job_id = job_spec.get("job_id", "")
        pr_num = job_spec.get("pr_number")
        gov_item_id = job_spec.get("governing_item_id")

        job_cfg = None
        if source_err is None:
            if not os.path.isfile(job_path):
                source_err = "MISSING_JOB_FILE"
            else:
                try:
                    with open(job_path, "r", encoding="utf-8") as f:
                        job_cfg = json.load(f)
                    if not isinstance(job_cfg, dict):
                        job_cfg = None
                        source_err = "CORRUPT_JOB_FILE: not a dict"
                except Exception as e:
                    source_err = f"CORRUPT_JOB_FILE: {e}"

        # If job_path unreadable or rejected, retain registry identity with source_error
        if job_cfg is None:
            synth_job = {
                "repository": repo,
                "branch": branch,
                "instruction_id": inst_id,
                "job_id": job_id or (f"PR{pr_num}_{inst_id}" if pr_num and inst_id else "UNKNOWN_JOB"),
                "pr_number": pr_num,
            }
            jobs.append({
                "source_job": raw_job_path or "",
                "job": synth_job,
                "files": {},
                "source_error": source_err,
                "governing_item_id": gov_item_id,
                "head_rc": 1,
                "mailbox_rc": 1,
                "chat_rc": 1,
            })
            continue

        job_dir = os.path.dirname(os.path.abspath(job_path))
        files = {}

        # Read standard job directory files first
        for std_name in [
            "active_instruction.json", "worker-launch.json", "worker-state.json",
            "send-process.json", "POSITIVE_CLAIM_RECONCILE.json", "SAFETY_STOP.json",
            "NORMAL_CHECKER_RESULT.json"
        ]:
            std_path = os.path.join(job_dir, std_name)
            if os.path.isfile(std_path) and std_name not in files:
                try:
                    with open(std_path, "r", encoding="utf-8") as f:
                        files[std_name] = json.load(f)
                except Exception:
                    pass

        fresh_repo = job_cfg.get("repository")
        fresh_branch = job_cfg.get("branch")
        act_inst_obj = files.get("active_instruction.json")
        fresh_inst = act_inst_obj.get("instruction_id") if isinstance(act_inst_obj, dict) else None

        reg_repo = job_spec.get("repository")
        reg_branch = job_spec.get("branch")
        reg_inst = job_spec.get("instruction_id")

        def _is_exact_tuple_match(a, b) -> bool:
            return (type(a) is str) and (type(b) is str) and bool(a) and (a == b)

        # Registry tuple guard: repository, branch, instruction_id must be non-empty strings and exact matches (no strip/normalization)
        tuple_matched = bool(
            _is_exact_tuple_match(reg_repo, fresh_repo)
            and _is_exact_tuple_match(reg_branch, fresh_branch)
            and _is_exact_tuple_match(reg_inst, fresh_inst)
        )

        applicable_gov_id = gov_item_id if tuple_matched else None

        # Only apply registry transfer and policy evidence when tuple matches
        if tuple_matched:
            for raw_fp in job_spec.get("transfer_files", []) + job_spec.get("policy_files", []):
                try:
                    resolved_fp = os.path.realpath(os.path.abspath(raw_fp if os.path.isabs(raw_fp) else os.path.join(profile_home, raw_fp)))
                    if os.path.commonpath([resolved_fp, profile_home]) != profile_home:
                        continue
                    if os.path.isfile(resolved_fp):
                        fname = os.path.basename(resolved_fp)
                        with open(resolved_fp, "r", encoding="utf-8") as f:
                            files[fname] = json.load(f)
                except Exception:
                    pass

        next_inst = fresh_inst
        if next_inst:
            for pat in [f"send_{next_inst}.json", f"receipt-{next_inst}.json"]:
                p = os.path.join(job_dir, pat)
                if os.path.isfile(p):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            files[pat] = json.load(f)
                    except Exception:
                        pass

        job_entry = {
            "source_job": job_path,
            "job": job_cfg,
            "files": files,
            "governing_item_id": applicable_gov_id,
            "tuple_matched": tuple_matched,
        }
        if not tuple_matched:
            job_entry["source_error"] = "REGISTRY_TUPLE_MISMATCH"

        # 1. HEAD readback via gh api
        j_repo = job_cfg.get("repository") or repo
        j_branch = job_cfg.get("branch") or branch
        if j_repo and j_branch:
            try:
                r = subprocess.run(["gh", "api", f"repos/{j_repo}/commits/{j_branch}"], capture_output=True, text=True, timeout=30)
                job_entry["head_rc"] = r.returncode
                if r.returncode == 0:
                    job_entry["head"] = json.loads(r.stdout)
                else:
                    job_entry["head_error"] = r.stderr[:500]
            except Exception as e:
                job_entry["head_rc"] = 1
                job_entry["head_error"] = str(e)
        else:
            job_entry["head_rc"] = 1

        # 2. Mailbox readback via gh api
        mailbox_rel = job_cfg.get("mailbox")
        head_sha = job_entry.get("head", {}).get("sha")
        if j_repo and mailbox_rel and head_sha:
            try:
                r = subprocess.run(["gh", "api", f"repos/{j_repo}/contents/{mailbox_rel}?ref={head_sha}"], capture_output=True, text=True, timeout=30)
                job_entry["mailbox_rc"] = r.returncode
                if r.returncode == 0:
                    b = json.loads(r.stdout)
                    job_entry["mailbox_blob"] = b.get("sha")
                    job_entry["mailbox"] = json.loads(base64.b64decode(b.get("content", "")))
                else:
                    job_entry["mailbox_error"] = r.stderr[:500]
            except Exception as e:
                job_entry["mailbox_rc"] = 1
                job_entry["mailbox_error"] = str(e)
        else:
            job_entry["mailbox_rc"] = 1

        # 3. Chat snapshot readback under shared lock at profile_home/cache/auto_allow.lock
        t_cmd = job_cfg.get("transport_command")
        lock_path = os.path.join(profile_home, "cache", "auto_allow.lock")
        if t_cmd and isinstance(t_cmd, list):
            lock_f = None
            lock_acquired = False
            try:
                os.makedirs(os.path.dirname(lock_path), exist_ok=True)
                lock_f = open(lock_path, "a+")
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lock_acquired = True
                except (BlockingIOError, OSError) as e:
                    job_entry["chat_rc"] = 1
                    job_entry["chat_error"] = f"LOCK_FAILED: {e}"

                if lock_acquired:
                    r = subprocess.run(t_cmd, input=json.dumps({"op": "snapshot"}), capture_output=True, text=True, timeout=45)
                    job_entry["chat_rc"] = r.returncode
                    job_entry["chat_observed_at"] = time.time()  # Real readback timestamp
                    if r.returncode == 0:
                        try:
                            job_entry["chat_snapshot"] = json.loads(r.stdout)
                        except Exception:
                            job_entry["chat_error"] = "INVALID_JSON"
                    else:
                        job_entry["chat_error"] = r.stderr[:1500]
            except Exception as e:
                job_entry["chat_rc"] = 1
                job_entry["chat_error"] = str(e)
            finally:
                if lock_f:
                    if lock_acquired:
                        try:
                            fcntl.flock(lock_f, fcntl.LOCK_UN)
                        except Exception:
                            pass
                    try:
                        lock_f.close()
                    except Exception:
                        pass
        else:
            job_entry["chat_rc"] = 1

        jobs.append(job_entry)

    return {
        "observed_at": time.time(),
        "jobs": jobs,
        "ledger_items": ledger_items,
    }


def run_producer_scan(
    state_dir: str,
    snapshot_path: str | None = None,
    registry_path: str | None = None,
    profile_home: str | None = None,
    now: float | None = None,
) -> int:
    """Producer execution: collect fresh snapshot (if registry), produce normalized.json, run scan (no consume)."""
    now_ts = now if now is not None else time.time()
    os.makedirs(state_dir, exist_ok=True)
    norm_file = os.path.join(state_dir, "normalized.json")

    if registry_path:
        snap_file = os.path.join(state_dir, "snapshot.json")
        snap = collect_live_snapshot(registry_path, now_ts, profile_home)
        rsw.atomic_write(snap_file, snap)
        doc = build_normalized(snap_file, state_dir, now_ts)
    elif snapshot_path:
        doc = build_normalized(snapshot_path, state_dir, now_ts)
    else:
        sys.stderr.write("Either --snapshot or --registry is required\n")
        return 1

    rsw.atomic_write(norm_file, doc)
    return rsw.run_scan(norm_file, state_dir, now_ts)


def main(argv: list[str] | None = None) -> int:
    """Adapter CLI entrypoint."""
    args = argv if argv is not None else sys.argv[1:]
    try:
        parser = argparse.ArgumentParser(description="Relay stall production adapter & consumer.")
        subparsers = parser.add_subparsers(dest="command")

        p_produce = subparsers.add_parser("produce", help="Produce normalized.json from snapshot")
        p_produce.add_argument("--snapshot", required=True, help="Path to source-snapshot.json")
        p_produce.add_argument("--out", required=True, help="Output path for normalized.json")
        p_produce.add_argument("--state-dir", default=None, help="State dir to read consumer receipt")
        p_produce.add_argument("--now", type=float, default=None, help="Epoch timestamp")

        p_consume = subparsers.add_parser("consume", help="Run durable consumer intake & ack")
        p_consume.add_argument("--state-dir", required=True, help="State dir with pending.json")
        p_consume.add_argument("--snapshot", default=None, help="Source snapshot for governing items")
        p_consume.add_argument("--now", type=float, default=None, help="Epoch timestamp")

        p_pscan = subparsers.add_parser("produce-scan", help="Fresh produce and scan (no auto-consume)")
        p_pscan.add_argument("--snapshot", default=None, help="Path to snapshot JSON")
        p_pscan.add_argument("--registry", default=None, help="Path to registry JSON")
        p_pscan.add_argument("--profile-home", default=None, help="Explicit profile home directory")
        p_pscan.add_argument("--state-dir", required=True, help="State directory")
        p_pscan.add_argument("--now", type=float, default=None, help="Epoch timestamp")

        p_scan = subparsers.add_parser("run-all", help="Alias for produce-scan (no auto-consume)")
        p_scan.add_argument("--snapshot", default=None, help="Path to snapshot JSON")
        p_scan.add_argument("--registry", default=None, help="Path to registry JSON")
        p_scan.add_argument("--profile-home", default=None, help="Explicit profile home directory")
        p_scan.add_argument("--state-dir", required=True, help="State directory")
        p_scan.add_argument("--now", type=float, default=None, help="Epoch timestamp")

        p_ack = subparsers.add_parser("ack-event", help="Consumer explicit event ack")
        p_ack.add_argument("--state-dir", required=True, help="State directory")
        p_ack.add_argument("--event-id", required=True, help="Event ID to ack")
        p_ack.add_argument("--decision", required=True, help="Governing decision")
        p_ack.add_argument("--now", type=float, default=None, help="Epoch timestamp")

        p_live = subparsers.add_parser("collect-live", help="Collect live snapshot from registry")
        p_live.add_argument("--registry", required=True, help="Path to private registry JSON")
        p_live.add_argument("--profile-home", default=None, help="Explicit profile home")
        p_live.add_argument("--out", required=True, help="Output path for snapshot JSON")
        p_live.add_argument("--now", type=float, default=None, help="Epoch timestamp")

        parsed = parser.parse_args(args)
        now_ts = parsed.now if getattr(parsed, "now", None) is not None else time.time()
        if type(now_ts) is bool or not isinstance(now_ts, (int, float)) or not math.isfinite(now_ts):
            sys.stderr.write("now must be finite epoch\n")
            return 1

        if parsed.command == "produce":
            doc = build_normalized(parsed.snapshot, parsed.state_dir, now_ts)
            rsw.atomic_write(parsed.out, doc)
            return 0

        if parsed.command == "consume":
            return run_consumer_intake(parsed.state_dir, parsed.snapshot, now_ts)

        if parsed.command == "ack-event":
            journal_file = os.path.join(parsed.state_dir, "consumer_journal.jsonl")
            rsw.append_jsonl(journal_file, {
                "event_id": parsed.event_id,
                "decision": parsed.decision,
                "recorded_at": now_ts,
            })
            return rsw.run_ack(parsed.state_dir, parsed.event_id, parsed.decision, now_ts)

        if parsed.command == "collect-live":
            snap = collect_live_snapshot(parsed.registry, now_ts, parsed.profile_home)
            rsw.atomic_write(parsed.out, snap)
            return 0

        if parsed.command in ("produce-scan", "run-all"):
            return run_producer_scan(
                state_dir=parsed.state_dir,
                snapshot_path=parsed.snapshot,
                registry_path=parsed.registry,
                profile_home=parsed.profile_home,
                now=now_ts,
            )

        parser.print_help()
        return 1
    except Exception as e:
        sys.stderr.write(f"Adapter error: {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
