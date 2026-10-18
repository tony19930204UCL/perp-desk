#!/usr/bin/env python3
"""Exact-job allow-once worker.

Usage: auto_allow.py [--once] <job_dir> <instruction_id> <chat_url> <target_id>
The transport must be a separately trusted adapter returning an envelope:
{"adapter":"trusted_cdp_v1","browser":{"target_id":...,"url":...},
 "result":<original snapshot/evaluate result>}.
This shape alone is not proof of provenance: operators must separately audit
the adapter. The installed legacy bridge does not implement this contract.
"""
import argparse
import json
from pathlib import Path
import sys
import time
from approval_transaction import approve, admission, load_json, transport, AdmissionError, InfraError

def binding(job_dir, instruction, chat, target, expected=None):
    job, identity = admission(job_dir, expected=expected)
    if identity[1:] != (instruction, chat):
        raise AdmissionError("PIN_MISMATCH")
    active = load_json(Path(job_dir) / "active_instruction.json")
    receipt = load_json(Path(job_dir) / ("send_" + instruction + ".json"))
    if active.get("instruction_id") != instruction or receipt.get("instruction_id") != instruction:
        raise AdmissionError("RECEIPT_INSTRUCTION_MISMATCH")
    if receipt.get("task_id") != identity[0]:
        raise AdmissionError("RECEIPT_TASK_MISMATCH")
    if not target or job.get("target_id") != target or receipt.get("target_id") != target:
        raise AdmissionError("TARGET_MISMATCH")
    if active.get("target_id", target) != target:
        raise AdmissionError("ACTIVE_TARGET_MISMATCH")
    if not isinstance(job.get("transport_command"), list) or not job["transport_command"] or not all(isinstance(x, str) and x for x in job["transport_command"]):
        raise AdmissionError("TRANSPORT_UNAVAILABLE")
    return identity

def run(job_dir, instruction, chat, target, once=False):
    try:
        pinned = binding(job_dir, instruction, chat, target)
    except AdmissionError as exc:
        print("BINDING_REJECTED", str(exc), file=sys.stderr)
        return 2

    def send(job, request):
        binding(job_dir, instruction, chat, target, expected=pinned)
        envelope = transport(job, request)
        if not isinstance(envelope, dict) or envelope.get("adapter") != "trusted_cdp_v1":
            raise InfraError("UNATTESTED_TRANSPORT")
        browser = envelope.get("browser")
        if not isinstance(browser, dict) or browser.get("target_id") != target or browser.get("url") != chat:
            raise InfraError("TRANSPORT_IDENTITY_MISMATCH")
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise InfraError("TRANSPORT_RESULT_INVALID")
        if request.get("op") == "snapshot" and result.get("url") != chat:
            raise InfraError("SNAPSHOT_CHAT_MISMATCH")
        return result

    deadline = time.monotonic() + (0 if once else 50)
    while True:
        try:
            binding(job_dir, instruction, chat, target, expected=pinned)
        except AdmissionError as exc:
            print("BINDING_REJECTED", str(exc), file=sys.stderr)
            return 2
        result = approve(job_dir, mode="auto", max_lines=200, send=send)
        status = result.get("status")
        if status == "APPROVED":
            print("ALLOWED_ONCE", result.get("fingerprint", ""))
            return 0
        if status == "NO_PENDING":
            if once:
                return 0
        else:
            print("APPROVAL_" + str(status), result.get("reason", ""), file=sys.stderr)
            return 3
        if time.monotonic() >= deadline:
            return 0
        time.sleep(5)

def main(argv=None):
    parser = argparse.ArgumentParser(usage="auto_allow.py [--once] <job_dir> <instruction_id> <chat_url> <target_id>")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("job_dir")
    parser.add_argument("instruction_id")
    parser.add_argument("chat_url")
    parser.add_argument("target_id")
    args = parser.parse_args(argv)
    return run(Path(args.job_dir).resolve(), args.instruction_id, args.chat_url, args.target_id, args.once)

if __name__ == "__main__":
    sys.exit(main())
