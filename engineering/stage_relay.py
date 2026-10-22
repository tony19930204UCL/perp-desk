#!/usr/bin/env python3
"""Durable, fail-closed stage relay. Not an approver or trading runtime.

Linux stdlib only. All commands/paths must be configured by the operator.
A successful subprocess is NEVER equivalent to accepted research.
"""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_LEASE = 3600
DENIED_NAMES = ("installed_auto_accept.py", "read_file", "agy/")


class RelayError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def raw_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_bytes(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".stage-relay-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(obj))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        parent_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def state_lock(path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def confined(root, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise RelayError("artifact path must be a nonempty relative path")
    root = Path(root).resolve()
    dest = (root / relative).resolve()
    if dest == root or root not in dest.parents:
        raise RelayError("artifact path escapes evidence root: " + relative)
    return dest


def configuration(config_path):
    path = Path(config_path).resolve()
    data = path.read_bytes()
    graph = json.loads(data)
    if graph.get("schema_version") != 1 or graph.get("kind") not in ("stage_relay", "synthetic_stage_relay"):
        raise RelayError("unsupported graph schema/kind")
    if not isinstance(graph.get("evidence_root"), str) or not Path(graph["evidence_root"]).is_absolute():
        raise RelayError("absolute evidence_root required")
    names = [s.get("id") for s in graph.get("stages", [])]
    if len(names) < 1 or any(not isinstance(x, str) or not x for x in names) or len(set(names)) != len(names):
        raise RelayError("stages must have distinct nonempty IDs")
    if not isinstance(graph.get("bootstrap"), dict):
        raise RelayError("external bootstrap acceptance is required")
    return graph, digest(data)


def load_state(state_path, graph_hash):
    path = Path(state_path)
    if not path.is_file():
        raise RelayError("missing state: execute init after verified bootstrap")
    state = raw_json(path)
    if state.get("schema_version") != 1 or state.get("graph_sha256") != graph_hash:
        raise RelayError("config changed since init: explicit operator reconciliation required")
    return state


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def process_identity(pid):
    """Return (identity, status); UNKNOWN is never interpreted as DEAD."""
    if type(pid) is not int or pid <= 1:
        return None, "UNKNOWN"
    try:
        stat = Path("/proc/%d/stat" % pid).read_text(encoding="ascii")
        # comm may contain spaces/parentheses; fields after final ')' start at field 3.
        tail = stat.rsplit(")", 1)[1].strip().split()
        if len(tail) < 20:
            return None, "UNKNOWN"
        state = tail[0]
        ident = {"pid": pid, "start_ticks": tail[19], "boot_id": boot_id()}
        if state in ("Z", "X"):
            return ident, "DEAD"
        return ident, "LIVE"
    except FileNotFoundError:
        return None, "DEAD"
    except (OSError, ValueError, IndexError):
        return None, "UNKNOWN"


def identity_status(owner):
    actual, status = process_identity(owner["pid"])
    if status == "UNKNOWN":
        return "UNKNOWN"
    if actual is not None and any(actual.get(k) != owner.get(k) for k in ("pid", "start_ticks", "boot_id")):
        return "MISMATCH"
    return status


def stage_of(graph, state):
    index = state["stage_index"]
    if index >= len(graph["stages"]):
        return None
    return graph["stages"][index]


def stage_inputs(stage):
    source = stage.get("source_head")
    inp = stage.get("input_sha256")
    if not isinstance(source, str) or not HEX40.fullmatch(source):
        raise RelayError("stage %s has no exact 40-hex source_head" % stage["id"])
    if not isinstance(inp, str) or not HEX64.fullmatch(inp):
        raise RelayError("stage %s has no exact 64-hex input_sha256" % stage["id"])


def validate_argv(argv):
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise RelayError("missing configured nonempty argv executor; no implied product entrypoint")
    if any(any(blocked in token.lower().replace("\\", "/") for blocked in DENIED_NAMES) for token in argv):
        raise RelayError("disallowed installed/denied operation in argv")
    return argv


def bootstrap(graph, gh):
    spec = graph["bootstrap"]
    evidence_root = graph["evidence_root"]
    path = confined(evidence_root, spec.get("acceptance_path"))
    if not path.is_file():
        raise RelayError("missing upstream external acceptance: " + str(path))
    proof = raw_json(path)
    for key in ("stage_id", "source_head", "input_sha256"):
        if proof.get(key) != spec.get(key):
            raise RelayError("bootstrap %s identity mismatch" % key)
    if proof.get("decision") != "ACCEPTED" or not proof.get("reviewer"):
        raise RelayError("bootstrap lacks explicit external acceptance")
    if not HEX40.fullmatch(str(proof["source_head"])) or not HEX64.fullmatch(str(proof["input_sha256"])):
        raise RelayError("bootstrap source/input hash invalid")
    artifact = confined(evidence_root, proof.get("evidence_path"))
    if not artifact.is_file() or digest(artifact.read_bytes()) != proof.get("evidence_sha256"):
        raise RelayError("bootstrap evidence hash invalid/missing")
    return {"acceptance_sha256": digest(path.read_bytes()), "evidence_sha256": proof["evidence_sha256"], "at": now()}


def init(config_path, state_path):
    graph, gh = configuration(config_path)
    with state_lock(state_path):
        if Path(state_path).exists():
            raise RelayError("existing state not overwritten (restart-safe)")
        upstream = bootstrap(graph, gh)
        s = {"schema_version": 1, "kind": graph["kind"], "graph_sha256": gh,
             "bootstrap": upstream, "stage_index": 0, "status": "READY",
             "owner": None, "attempts": {}, "completed": [], "processed_receipts": [],
             "fail_reason": None, "repair_request": None, "previous_accepted_at": None,
             "created_at": now(), "updated_at": now(), "history": []}
        atomic_json(state_path, s)
        return {"action": "INITIALIZED", "stage": graph["stages"][0]["id"], "graph_sha256": gh}


def append_event(s, action, **extra):
    event = {"at": now(), "action": action}
    event.update(extra)
    s["history"].append(event)
    s["updated_at"] = event["at"]


def save(state_path, s):
    atomic_json(state_path, s)


def begin_locked(graph, gh, s, config_path, state_path):
    stage = stage_of(graph, s)
    if stage is None:
        return {"action": "DONE", "completed": len(s["completed"])}
    if s["status"] == "READY":
        try:
            stage_inputs(stage)
            mode = stage.get("mode")
            if mode == "adopt":
                s["status"] = "WAITING_ADOPTION"
                append_event(s, "READY_MISSING_OWNER", stage=stage["id"])
                save(state_path, s)
                return {"action": "READY_MISSING_OWNER", "stage": stage["id"], "reason": "operator must ADOPT known r12 PID/start/lease; never duplicate-dispatch"}
            if mode != "exec":
                raise RelayError("stage %s missing configured executor mode" % stage["id"])
            argv = validate_argv(stage.get("command"))
            lease = stage.get("lease_seconds", 900)
            if type(lease) is not int or not 1 <= lease <= MAX_LEASE:
                raise RelayError("lease_seconds must be in 1..%d" % MAX_LEASE)
            if not stage.get("reports"):
                raise RelayError("stage %s missing report contract" % stage["id"])
        except RelayError as exc:
            s["status"] = "BLOCKED"
            s["fail_reason"] = str(exc)
            append_event(s, "BLOCKED", reason=str(exc))
            save(state_path, s)
            return {"action": "BLOCKED", "stage": stage["id"], "reason": str(exc)}
        token = uuid.uuid4().hex
        attempts = s["attempts"].get(stage["id"], 0) + 1
        log = confined(graph["evidence_root"], "logs/%s-%d.worker.log" % (stage["id"], attempts))
        log.parent.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), "_worker",
                   "--config", str(Path(config_path).resolve()),
                   "--state", str(Path(state_path).resolve()), "--token", token]
        with log.open("xb") as stream:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    close_fds=True, start_new_session=True)
        ident, proc_status = process_identity(proc.pid)
        if ident is None or proc_status != "LIVE":
            s["status"] = "BLOCKED"
            s["fail_reason"] = "UNKNOWN worker PID/start identity; do not retry"
            append_event(s, "BLOCKED", reason=s["fail_reason"])
            save(state_path, s)
            return {"action": "BLOCKED", "reason": s["fail_reason"]}
        started = time.time()
        owner = dict(ident, token=token, role="managed", started_at=started,
                     argv=argv, lease_until=started + lease, worker_log=str(log),
                     product=None)
        s["owner"] = owner
        s["attempts"][stage["id"]] = attempts
        s["status"] = "RUNNING"
        append_event(s, "WORKER_STARTED", stage=stage["id"], pid=proc.pid, owner=ident,
                     argv=argv, accepted_to_worker_seconds=(
                         max(0.0, started - s["previous_accepted_at"]) if s["previous_accepted_at"] is not None else None))
        save(state_path, s)
        return {"action": "WORKER_STARTED", "stage": stage["id"], "pid": proc.pid, "start_ticks": ident["start_ticks"]}
    if s["status"] == "WAITING_ADOPTION":
        return {"action": "READY_MISSING_OWNER", "stage": stage["id"], "reason": "no adopted exact live external owner"}
    if s["status"] == "RUNNING":
        owner = s.get("owner")
        if not owner:
            s["status"] = "BLOCKED"
            s["fail_reason"] = "RUNNING without durable owner"
        else:
            observed = identity_status(owner)
            if observed in ("UNKNOWN", "MISMATCH"):
                s["status"] = "BLOCKED"
                s["fail_reason"] = "unknown/mismatched live process identity: " + observed
            elif time.time() > owner["lease_until"]:
                s["status"] = "BLOCKED"
                s["fail_reason"] = "bounded lease expired; explicit operator reconciliation required"
            elif observed == "DEAD":
                s["status"] = "WAITING_RECEIPT"
                append_event(s, "OWNER_DEAD", stage=stage["id"], reason="no trusted terminal receipt; no retry")
                save(state_path, s)
                return {"action": "WAITING_RECEIPT", "stage": stage["id"], "reason": "process gone; require exact terminal facts"}
            else:
                return {"action": "RUNNING", "stage": stage["id"], "owner": owner}
        append_event(s, "BLOCKED", reason=s["fail_reason"])
        save(state_path, s)
        return {"action": "BLOCKED", "stage": stage["id"], "reason": s["fail_reason"]}
    return {"action": s["status"], "stage": stage["id"], "reason": s.get("fail_reason"), "owner": s.get("owner")}


def tick(config_path, state_path):
    graph, gh = configuration(config_path)
    with state_lock(state_path):
        s = load_state(state_path, gh)
        return begin_locked(graph, gh, s, config_path, state_path)


def adopt(config_path, state_path, identity_path, lease):
    graph, gh = configuration(config_path)
    if type(lease) is not int or not 1 <= lease <= MAX_LEASE:
        raise RelayError("bounded lease must be 1..%d seconds" % MAX_LEASE)
    provided = raw_json(identity_path)
    with state_lock(state_path):
        s = load_state(state_path, gh)
        stage = stage_of(graph, s)
        if stage is None or stage.get("mode") != "adopt" or s["status"] not in ("READY", "WAITING_ADOPTION"):
            raise RelayError("ADOPT requires a not-yet-owned adopt-mode stage")
        stage_inputs(stage)
        if graph.get("maker_bridge") and stage["id"] == graph["maker_bridge"].get("historical_stage"):
            import importlib.util
            sibling = Path(__file__).with_name("maker_stage_bridge.py")
            if not sibling.is_file():
                raise RelayError("Maker bridge sibling missing; ADOPT denied")
            module_spec = importlib.util.spec_from_file_location("maker_stage_bridge", sibling)
            maker = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(maker)
            gate = maker.source_gate(graph, gh)
            if gate["action"] != "CAPACITY_SOURCE_ACCEPTED":
                raise RelayError("WAITING_CAPACITY_ACCEPTANCE: " + gate.get("reason", "source not accepted"))
            if provided.get("argv") != graph["maker_bridge"].get("run_argv"):
                raise RelayError("historical adopted argv differs from configured exact Maker --run")
        if not provided.get("operator") or not isinstance(provided.get("argv"), list):
            raise RelayError("ADOPT needs operator and exact argv identity")
        validate_argv(provided["argv"])
        pid = provided.get("pid")
        actual, observed = process_identity(pid)
        if observed != "LIVE" or not actual or any(actual.get(x) != provided.get(x) for x in ("pid", "start_ticks", "boot_id")):
            raise RelayError("external process cannot be uniquely verified LIVE; no dispatch")
        token = uuid.uuid4().hex
        started = time.time()
        owner = dict(actual, token=token, role="external", argv=provided["argv"],
                     operator=provided["operator"], started_at=provided.get("started_at", started),
                     adopted_at=started, lease_until=started + lease, product=dict(actual))
        s["owner"] = owner
        s["status"] = "RUNNING"
        s["attempts"][stage["id"]] = 1
        append_event(s, "ADOPTED", stage=stage["id"], owner=actual, token=token)
        save(state_path, s)
        return {"action": "ADOPTED", "stage": stage["id"], "owner": owner}


def renew(config_path, state_path, token, lease):
    graph, gh = configuration(config_path)
    if type(lease) is not int or not 1 <= lease <= MAX_LEASE:
        raise RelayError("renewal lease out of range")
    with state_lock(state_path):
        s = load_state(state_path, gh)
        owner = s.get("owner")
        if s["status"] != "RUNNING" or not owner or owner["token"] != token:
            raise RelayError("no exact RUNNING owner to renew")
        if identity_status(owner) != "LIVE":
            raise RelayError("cannot renew unverified live identity")
        owner["lease_until"] = time.time() + lease
        append_event(s, "LEASE_RENEWED", stage=stage_of(graph, s)["id"])
        save(state_path, s)
        return {"action": "LEASE_RENEWED", "stage": stage_of(graph, s)["id"], "lease_until": owner["lease_until"]}


def worker(config_path, state_path, token):
    graph, gh = configuration(config_path)
    try:
        with state_lock(state_path):
            s = load_state(state_path, gh)
            stage = stage_of(graph, s)
            owner = s.get("owner")
            if s["status"] != "RUNNING" or not owner or owner["token"] != token or owner["role"] != "managed":
                raise RelayError("worker lease lost before process start")
            if identity_status(owner) != "LIVE" or owner["pid"] != os.getpid():
                raise RelayError("worker PID/start identity mismatch")
            argv = validate_argv(stage.get("command"))
            cwd = stage.get("cwd")
            if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_absolute()):
                raise RelayError("configured cwd must be absolute")
            out = confined(graph["evidence_root"], "logs/%s-%d.product.log" % (stage["id"], s["attempts"][stage["id"]]))
            out.parent.mkdir(parents=True, exist_ok=True)
            with out.open("xb") as stream:
                p = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=subprocess.STDOUT, close_fds=True)
            ident, _ = process_identity(p.pid)
            if ident is None:
                raise RelayError("product start identity UNKNOWN")
            owner["product"] = dict(ident, argv=argv, started_at=time.time(), log=str(out))
            append_event(s, "PRODUCT_STARTED", stage=stage["id"], pid=p.pid, start_ticks=ident["start_ticks"],
                         argv=argv, accepted_to_process_seconds=(
                             max(0.0, owner["product"]["started_at"] - s["previous_accepted_at"])
                             if s["previous_accepted_at"] is not None else None))
            save(state_path, s)
        exitcode = p.wait()
        with state_lock(state_path):
            s = load_state(state_path, gh)
            owner = s.get("owner")
            if not owner or owner.get("token") != token:
                raise RelayError("worker owner replaced; do not record stale completion")
            owner["exit_code"] = exitcode
            owner["ended_at"] = time.time()
            if s["status"] == "RUNNING":
                s["status"] = "WAITING_ACCEPTANCE" if exitcode == 0 else "FAILED"
                if exitcode != 0:
                    s["fail_reason"] = "product exit_code=%d; explicit repair required" % exitcode
                    s["repair_request"] = {"stage": stage_of(graph, s)["id"], "reason": s["fail_reason"],
                                           "executor": stage_of(graph, s).get("repair_command"),
                                           "automatically_sent": False}
            append_event(s, "PRODUCT_EXITED", stage=stage_of(graph, s)["id"], exit_code=exitcode)
            save(state_path, s)
    except Exception as exc:
        with state_lock(state_path):
            try:
                s = load_state(state_path, gh)
                if s.get("owner", {}).get("token") == token and s["status"] == "RUNNING":
                    s["status"] = "BLOCKED"
                    s["fail_reason"] = "worker failure (not retried): %s" % exc
                    append_event(s, "BLOCKED", reason=s["fail_reason"])
                    save(state_path, s)
            except Exception:
                pass
        raise


def check_report(graph, gh, stage, spec, reported):
    expected_path = spec.get("path")
    if reported.get("path") != expected_path:
        raise RelayError("receipt report path mismatch")
    path = confined(graph["evidence_root"], expected_path)
    if not path.is_file():
        raise RelayError("missing required report: " + str(path))
    data = path.read_bytes()
    actual_sha = digest(data)
    if actual_sha != reported.get("sha256"):
        raise RelayError("report hash mismatch: " + expected_path)
    report = json.loads(data)
    for key, expected in (("stage", stage["id"]), ("source_head", stage["source_head"]),
                          ("input_sha256", stage["input_sha256"]), ("graph_sha256", gh)):
        if report.get(key) != expected:
            raise RelayError("report %s mismatch: %s" % (expected_path, key))
    rows = report.get("rows")
    if not isinstance(rows, list) or type(spec.get("rows")) is not int or spec["rows"] < 1:
        raise RelayError("report has no finite expected rows")
    if len(rows) != spec["rows"] or reported.get("rows") != len(rows):
        raise RelayError("report row count mismatch: %s" % expected_path)
    keys = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or not isinstance(row.get("cell_id"), str):
            raise RelayError("report rows require symbol and cell_id")
        keys.append((row["symbol"], row["cell_id"]))
    if len(set(keys)) != len(keys):
        raise RelayError("duplicate symbol/cell rows")
    limits = spec.get("symbol_cells")
    if limits:
        counted = {}
        for symbol, _ in keys:
            counted[symbol] = counted.get(symbol, 0) + 1
        if counted != limits:
            raise RelayError("symbol-cell coverage mismatch: %r" % counted)
    return len(rows), actual_sha


def complete(config_path, state_path, receipt_path, acceptance_path):
    graph, gh = configuration(config_path)
    receipt_file = confined(graph["evidence_root"], receipt_path)
    acceptance_file = confined(graph["evidence_root"], acceptance_path)
    receipt_bytes = receipt_file.read_bytes()
    receipt_sha = digest(receipt_bytes)
    receipt = json.loads(receipt_bytes)
    with state_lock(state_path):
        s = load_state(state_path, gh)
        if receipt_sha in s["processed_receipts"]:
            return {"action": "DUPLICATE_IGNORED", "receipt_sha256": receipt_sha}
        stage = stage_of(graph, s)
        if not stage or s["status"] not in ("RUNNING", "WAITING_RECEIPT", "WAITING_ACCEPTANCE"):
            raise RelayError("no stage accepting this completion; status=" + s["status"])
        stage_inputs(stage)
        owner = s.get("owner")
        if not owner:
            raise RelayError("no uniquely owned process for completion")
        for key, expected in (("stage", stage["id"]), ("source_head", stage["source_head"]),
                              ("input_sha256", stage["input_sha256"]), ("graph_sha256", gh)):
            if receipt.get(key) != expected:
                raise RelayError("terminal receipt %s mismatch" % key)
        observed = receipt.get("owner", {})
        if not isinstance(observed, dict) or any(observed.get(k) != owner.get(k) for k in
                                                   ("pid", "start_ticks", "boot_id", "token")):
            raise RelayError("terminal receipt PID/start/token mismatch")
        if owner["role"] == "managed":
            if s["status"] != "WAITING_ACCEPTANCE" or owner.get("exit_code") != 0:
                raise RelayError("managed child did not have an observed exit 0")
        else:
            if identity_status(owner) != "DEAD":
                raise RelayError("external process is LIVE/UNKNOWN/reused; terminal receipt not trusted")
        outcome = receipt.get("outcome")
        if outcome in ("REFUSED", "UNKNOWN"):
            s["status"] = "BLOCKED"
            s["fail_reason"] = "terminal " + outcome + ": " + str(receipt.get("reason"))
            append_event(s, "SAFETY_STOP", stage=stage["id"], receipt_sha256=receipt_sha)
            s["processed_receipts"].append(receipt_sha)
            save(state_path, s)
            return {"action": "BLOCKED", "reason": s["fail_reason"]}
        if outcome != "SUCCEEDED" or receipt.get("exit_code") != 0:
            s["status"] = "FAILED"
            s["fail_reason"] = "terminal failure: " + str(receipt.get("reason"))
            s["repair_request"] = {"stage": stage["id"], "reason": s["fail_reason"],
                                   "executor": stage.get("repair_command"), "automatically_sent": False}
            append_event(s, "FAILED", stage=stage["id"], receipt_sha256=receipt_sha)
            s["processed_receipts"].append(receipt_sha)
            save(state_path, s)
            return {"action": "FAILED", "reason": s["fail_reason"]}
        acceptance = raw_json(acceptance_file)
        specs = stage.get("reports")
        reported = receipt.get("reports")
        if not isinstance(specs, list) or not specs or not isinstance(reported, list) or len(specs) != len(reported):
            raise RelayError("missing exact required reports")
        validated = [check_report(graph, gh, stage, spec, report) for spec, report in zip(specs, reported)]
        actual_count = sum(x[0] for x in validated)
        if receipt.get("output_count") != actual_count:
            raise RelayError("actual output count != terminal receipt")
        for key, expected in (("stage", stage["id"]), ("source_head", stage["source_head"]),
                              ("input_sha256", stage["input_sha256"]), ("graph_sha256", gh),
                              ("receipt_sha256", receipt_sha)):
            if acceptance.get(key) != expected:
                raise RelayError("external acceptance %s mismatch" % key)
        if acceptance.get("decision") != "ACCEPTED" or not acceptance.get("reviewer"):
            raise RelayError("explicit independent acceptance missing")
        accepted_at = time.time()
        s["completed"].append({"stage": stage["id"], "source_head": stage["source_head"],
                               "input_sha256": stage["input_sha256"], "graph_sha256": gh,
                               "receipt_sha256": receipt_sha, "acceptance_sha256": digest(acceptance_file.read_bytes()),
                               "report_hashes": [x[1] for x in validated], "output_count": actual_count,
                               "owner": owner, "accepted_at": accepted_at})
        s["processed_receipts"].append(receipt_sha)
        s["previous_accepted_at"] = accepted_at
        s["stage_index"] += 1
        s["owner"] = None
        s["status"] = "DONE" if s["stage_index"] == len(graph["stages"]) else "READY"
        append_event(s, "ACCEPTED", stage=stage["id"], output_count=actual_count,
                     receipt_sha256=receipt_sha)
        save(state_path, s)
        next_result = begin_locked(graph, gh, s, config_path, state_path) if s["status"] == "READY" else {"action": "DONE"}
        return {"action": "ACCEPTED", "stage": stage["id"], "output_count": actual_count,
                "next": next_result}


def status(config_path, state_path):
    graph, gh = configuration(config_path)
    with state_lock(state_path):
        s = load_state(state_path, gh)
        return {"action": "STATUS", "stage": (stage_of(graph, s) or {}).get("id"),
                "status": s["status"], "source_head": (stage_of(graph, s) or {}).get("source_head"),
                "graph_sha256": gh, "owner": s["owner"], "reason": s["fail_reason"],
                "completed": s["completed"], "repair_request": s.get("repair_request"),
                "history": s["history"]}


# Fixed profile contract: profile/scripts/stage_relay.py, no arguments, no fallback.
PROFILE_NAMESPACE = ("stage-relay", "C0041")
SAFE_STAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def installed_paths(entrypoint=None):
    """Resolve solely from this installed file, NEVER $HOME or a default profile."""
    candidate = Path(entrypoint or __file__).absolute()
    if candidate.is_symlink():
        raise RelayError("installed entrypoint symlink refused")
    path = candidate.resolve(strict=True)
    if path.name != "stage_relay.py" or path.parent.name != "scripts":
        raise RelayError("NO_ARG_REQUIRES_PROFILE_SCRIPTS_STAGE_RELAY_PY")
    profile = path.parent.parent
    ns = profile.joinpath(*PROFILE_NAMESPACE)
    evidence = ns / "evidence"
    for dest in (ns, evidence):
        if dest.is_symlink() or profile not in dest.resolve().parents:
            raise RelayError("profile namespace escapes installed profile")
    return {"profile": profile, "namespace": ns, "config": ns / "graph.json",
            "state": ns / "state.json", "evidence": evidence}


def consume_inbox(config_path, state_path, evidence_root):
    """Consume only the current stage's fixed inbox pair; no discovery/auto-approval."""
    graph, gh = configuration(config_path)
    with state_lock(state_path):
        s = load_state(state_path, gh)
        stage = stage_of(graph, s)
        if not stage or s["status"] in ("BLOCKED", "FAILED", "DONE"):
            return {"action": "NO_ELIGIBLE_TERMINAL", "status": s["status"]}
        stage_id = stage["id"]
        if not SAFE_STAGE.fullmatch(stage_id):
            raise RelayError("unsafe stage ID for fixed inbox: " + stage_id)
        receipt_rel = "inbox/%s.receipt.json" % stage_id
        acceptance_rel = "inbox/%s.acceptance.json" % stage_id
        receipt_path = confined(evidence_root, receipt_rel)
        acceptance_path = confined(evidence_root, acceptance_rel)
        if receipt_path.is_symlink() or acceptance_path.is_symlink():
            raise RelayError("inbox symlinks refused")
        if not receipt_path.is_file():
            return {"action": "MISSING_TERMINAL_RECEIPT", "stage": stage_id,
                    "expected": str(receipt_path), "reason": "parent has not submitted trusted terminal facts"}
        receipt = raw_json(receipt_path)
        if not isinstance(receipt, dict):
            raise RelayError("terminal inbox receipt must be an object")
        # Reject mismatched current-stage data rather than silently skipping.
        for key, value in (("stage", stage_id), ("source_head", stage["source_head"]),
                           ("input_sha256", stage["input_sha256"]), ("graph_sha256", gh)):
            if receipt.get(key) != value:
                raise RelayError("inbox terminal %s mismatch; operator decision required" % key)
        if receipt.get("outcome") == "SUCCEEDED" and not acceptance_path.is_file():
            return {"action": "MISSING_INDEPENDENT_ACCEPTANCE", "stage": stage_id,
                    "expected": str(acceptance_path),
                    "reason": "exit0 alone never authorizes advancement"}
    # Release outer lock before calling complete (which owns the same lock).
    return complete(config_path, state_path, receipt_rel, acceptance_rel)


def supervisor(entrypoint=None):
    """Official script no-arg pre-run: full, unfiltered result EVERY due run."""
    paths = installed_paths(entrypoint)
    cfg, state = paths["config"], paths["state"]
    base = {"profile": str(paths["profile"]), "namespace": str(paths["namespace"]),
            "config": str(cfg), "state_file": str(state),
            "synthetic_acceptance_created": False}
    if not cfg.is_file():
        return dict(base, action="NOT_CONFIGURED", status="NOT_CONFIGURED",
                    needs_model_decision=True, stage=None, owner=None, lease_remaining_seconds=None,
                    reason="missing exact profile stage-relay/C0041/graph.json; operator must configure verified r12 source/input/executor")
    if cfg.is_symlink() or state.is_symlink():
        return dict(base, action="BLOCKED", status="BLOCKED",
                    needs_model_decision=True, stage=None, owner=None, lease_remaining_seconds=None,
                    reason="config/state symlink refused")
    if not state.is_file():
        return dict(base, action="NOT_INITIALIZED", status="NOT_INITIALIZED",
                    needs_model_decision=True, stage=None, owner=None, lease_remaining_seconds=None,
                    reason="missing state.json; operator must verify bootstrap and explicitly init")
    try:
        graph, gh = configuration(cfg)
        if Path(graph["evidence_root"]).resolve() != paths["evidence"].resolve():
            raise RelayError("graph evidence_root must match exact installed profile namespace")
        # No synthetic graph / synthetic acceptance in real supervisor path.
        if graph["kind"] != "stage_relay":
            raise RelayError("installed no-arg supervisor requires real stage_relay graph, not synthetic")
        with state_lock(state):
            current = load_state(state, gh)
            eligible_stage = stage_of(graph, current)
            if eligible_stage and not SAFE_STAGE.fullmatch(eligible_stage["id"]):
                raise RelayError("fixed inbox stage ID unsafe")
        bridge_gate = None
        if graph.get("maker_bridge"):
            import importlib.util
            bridge_path = Path(__file__).with_name("maker_stage_bridge.py")
            if not bridge_path.is_file():
                raise RelayError("maker_stage_bridge.py must be installed beside stage_relay.py")
            module_spec = importlib.util.spec_from_file_location("maker_stage_bridge", bridge_path)
            bridge = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(bridge)
            bridge_gate = bridge.preflight(graph, gh, current)
            if bridge_gate["action"] == "WAITING_CAPACITY_ACCEPTANCE" and current["status"] not in ("FAILED", "BLOCKED", "DONE"):
                owner = current.get("owner")
                return dict(base, action="WAITING_CAPACITY_ACCEPTANCE",
                            status=current["status"], stage=(eligible_stage or {}).get("id"),
                            owner=owner, lease_remaining_seconds=(
                                max(0.0, owner["lease_until"] - time.time()) if owner else None),
                            needs_model_decision=True, capacity=bridge_gate,
                            reason=bridge_gate["reason"])
        try:
            consumed = consume_inbox(cfg, state, paths["evidence"])
        except (RelayError, OSError, ValueError, KeyError, TypeError) as exc:
            consumed = {"action": "INBOX_BLOCKED", "reason": str(exc)}
        # An invalid or ambiguous receipt must NOT cause a launch.
        if consumed["action"] == "INBOX_BLOCKED":
            action = {"action": "INBOX_BLOCKED", "reason": consumed["reason"]}
        else:
            action = tick(cfg, state)
        with state_lock(state):
            current = load_state(state, gh)
            pending = stage_of(graph, current)
            owner = current.get("owner")
            lease = (max(0.0, owner["lease_until"] - time.time())
                     if owner and isinstance(owner.get("lease_until"), (int, float)) else None)
            source = pending.get("source_head") if pending else None
            inp = pending.get("input_sha256") if pending else None
            stage_mode = pending.get("mode") if pending else None
            completed = [{"stage": x["stage"], "source_head": x["source_head"],
                          "report_hashes": x["report_hashes"], "output_count": x["output_count"]}
                         for x in current["completed"]]
        flags = ("NOT_CONFIGURED", "NOT_INITIALIZED", "INBOX_BLOCKED", "BLOCKED", "FAILED",
                 "WAITING_RECEIPT", "WAITING_ACCEPTANCE", "WAITING_ADOPTION", "READY_MISSING_OWNER")
        decision = (action["action"] in flags or consumed["action"] in
                    ("MISSING_INDEPENDENT_ACCEPTANCE", "INBOX_BLOCKED"))
        if current["status"] in ("BLOCKED", "FAILED", "WAITING_RECEIPT", "WAITING_ACCEPTANCE", "WAITING_ADOPTION"):
            decision = True
        reason = (action.get("reason") or consumed.get("reason") or current.get("fail_reason"))
        if current["status"] == "WAITING_ADOPTION" and not reason:
            reason = "r12 external owner missing; operator must ADOPT exact live PID/start/lease, no duplicate dispatch"
        if current["status"] == "READY" and not reason:
            reason = "ready stage requires configured executor or authorized model decision"
        notice = bridge.reflection_notice(graph, current) if graph.get("maker_bridge") else None
        if notice and notice["action"] == "READY_OPERATOR_REFLECTION":
            decision = True
            reason = notice["reason"]
        return dict(base, action=("READY_OPERATOR_REFLECTION" if notice and
                    notice["action"] == "READY_OPERATOR_REFLECTION" else "SUPERVISOR_TICK"),
                    reflection=notice, capacity=bridge_gate, tick=action, inbox=consumed,
                    status=current["status"], stage=pending["id"] if pending else None,
                    stage_mode=stage_mode, source_head=source, input_sha256=inp, graph_sha256=gh,
                    owner=owner, lease_remaining_seconds=lease,
                    completed=completed, needs_model_decision=decision, reason=reason)
    except (RelayError, OSError, ValueError, KeyError, TypeError) as exc:
        return dict(base, action="BLOCKED", status="BLOCKED", stage=None,
                    needs_model_decision=True, owner=None, lease_remaining_seconds=None,
                    reason="fixed installed profile validation: %s" % exc)


def demo_installed_noarg(entrypoint):
    completed = subprocess.run([sys.executable, str(entrypoint)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
    if completed.returncode not in (0, 2):
        raise RelayError("installed noarg subprocess failed: %s %s" %
                         (completed.returncode, completed.stderr[:1000]))
    try:
        result = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RelayError("installed noarg did not return JSON: %s" % exc)
    if result.get("action") != "SUPERVISOR_TICK":
        raise RelayError("demo noarg unexpected supervisor action: %s" % result)
    return result


def demo_task(args):
    graph, gh = configuration(args.config)
    stage = next(x for x in graph["stages"] if x["id"] == args.stage)
    rows = []
    if args.rows == 48:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            for i in range(24):
                rows.append({"symbol": symbol, "cell_id": "%02d" % i, "origin": "SYNTHETIC_SUBPROCESS_ONLY"})
    else:
        rows = [{"symbol": "SYNTH", "cell_id": "%02d" % i,
                 "origin": "SYNTHETIC_SUBPROCESS_ONLY"} for i in range(args.rows)]
    path = confined(graph["evidence_root"], stage["reports"][0]["path"])
    atomic_json(path, {"stage": args.stage, "source_head": stage["source_head"],
                       "input_sha256": stage["input_sha256"], "graph_sha256": gh,
                       "synthetic": True, "rows": rows})
    print(json.dumps({"synthetic": True, "rows": len(rows), "pid": os.getpid()}))


def demo(directory):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile = root / "isolated-profile"
    scripts = profile / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    installed = scripts / "stage_relay.py"
    shutil.copyfile(Path(__file__).resolve(), installed)
    namespace = profile / "stage-relay" / "C0041"
    evidence = namespace / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    graph_path, state_path = namespace / "graph.json", namespace / "state.json"
    if graph_path.exists() or state_path.exists():
        raise RelayError("demo refuses existing graph/state; use new isolated namespace")
    prior = confined(evidence, "bootstrap/r11-synthetic-evidence.json")
    atomic_json(prior, {"kind": "SYNTHETIC_R11_NON_MAKER", "accepted_product": False})
    bootstrap_path = confined(evidence, "bootstrap/r11-acceptance.json")
    head = "d" * 40
    inp = "e" * 64
    atomic_json(bootstrap_path, {"stage_id": "synthetic-r11", "decision": "ACCEPTED",
                                 "reviewer": "synthetic-demo-not-an-approver",
                                 "source_head": head, "input_sha256": inp,
                                 "evidence_path": "bootstrap/r11-synthetic-evidence.json",
                                 "evidence_sha256": digest(prior.read_bytes())})
    ids = [("r12_entrypoint", 1), ("matched_account", 2),
           ("btc_eth_48_cells", 48), ("operator_reflection", 1)]
    stages = []
    for stage_id, count in ids:
        spec = {"path": "reports/" + stage_id + ".json", "rows": count}
        if count == 48:
            spec["symbol_cells"] = {"BTCUSDT": 24, "ETHUSDT": 24}
        stages.append({"id": stage_id, "mode": "exec", "source_head": head, "input_sha256": inp,
                       "lease_seconds": 60, "reports": [spec],
                       "command": [sys.executable, str(installed), "_demo_task",
                                   "--config", str(graph_path), "--stage", stage_id, "--rows", str(count)]})
    atomic_json(graph_path, {"schema_version": 1, "kind": "stage_relay",
                             "evidence_root": str(evidence), "bootstrap": {
                                 "acceptance_path": "bootstrap/r11-acceptance.json",
                                 "stage_id": "synthetic-r11", "source_head": head,
                                 "input_sha256": inp}, "stages": stages})
    init(str(graph_path), str(state_path))
    actions = [demo_installed_noarg(installed)]
    guards = []
    for stage_id, count in ids:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = raw_json(state_path)
            if state["status"] != "RUNNING":
                break
            time.sleep(0.02)
        if state["status"] != "WAITING_ACCEPTANCE":
            raise RelayError("demo subprocess did not exit cleanly: " + state["status"])
        owner = state["owner"]
        path = stages[state["stage_index"]]["reports"][0]["path"]
        report = confined(evidence, path)
        if stage_id == ids[0][0]:
            # Explicit negative guard: rejected before an independent acceptance is accepted.
            bad_receipt = {"stage": stage_id, "source_head": head, "input_sha256": inp,
                           "graph_sha256": state["graph_sha256"],
                           "owner": {k: owner[k] for k in ("pid", "start_ticks", "boot_id", "token")},
                           "exit_code": 0, "outcome": "SUCCEEDED", "output_count": 1,
                           "reports": [{"path": path, "sha256": "0" * 64, "rows": 1}]}
            bad = confined(evidence, "receipts/negative.json")
            atomic_json(bad, bad_receipt)
            bad_accept = confined(evidence, "receipts/negative-acceptance.json")
            atomic_json(bad_accept, {"decision": "ACCEPTED", "reviewer": "synthetic-demo",
                                     "stage": stage_id, "source_head": head, "input_sha256": inp,
                                     "graph_sha256": state["graph_sha256"],
                                     "receipt_sha256": digest(bad.read_bytes())})
            try:
                complete(str(graph_path), str(state_path), "receipts/negative.json",
                         "receipts/negative-acceptance.json")
                raise RelayError("negative guard unexpectedly accepted")
            except RelayError as exc:
                if "report hash mismatch" not in str(exc):
                    raise
                guards.append("hash-mismatch denied; no advance")
        receipt = {"stage": stage_id, "source_head": head, "input_sha256": inp,
                   "graph_sha256": state["graph_sha256"],
                   "owner": {k: owner[k] for k in ("pid", "start_ticks", "boot_id", "token")},
                   "exit_code": 0, "outcome": "SUCCEEDED", "reason": "synthetic demo only",
                   "output_count": count,
                   "reports": [{"path": path, "sha256": digest(report.read_bytes()), "rows": count}]}
        receipt_rel = "inbox/" + stage_id + ".receipt.json"
        acceptance_rel = "inbox/" + stage_id + ".acceptance.json"
        receipt_file = confined(evidence, receipt_rel)
        atomic_json(receipt_file, receipt)
        atomic_json(confined(evidence, acceptance_rel),
                    {"stage": stage_id, "source_head": head, "input_sha256": inp,
                     "graph_sha256": state["graph_sha256"], "receipt_sha256": digest(receipt_file.read_bytes()),
                     "decision": "ACCEPTED", "reviewer": "synthetic-demo-only"})
        actions.append(demo_installed_noarg(installed))
        if actions[-1]["inbox"].get("action") != "ACCEPTED":
            raise RelayError("installed inbox did not consume accepted synthetic stage")
    final = raw_json(state_path)
    if final["status"] != "DONE" or len(final["completed"]) != 4:
        raise RelayError("synthetic demo did not finish four subprocess stages")
    return {"synthetic_only": True, "not_maker_product_evidence": True, "status": "DONE",
            "installed_noarg_chain": True, "installed_entrypoint": str(installed),
            "state": str(state_path), "graph": str(graph_path),
            "guards": guards, "completed": [{"stage": x["stage"], "output_count": x["output_count"],
                                             "pid": x["owner"]["product"]["pid"],
                                             "start_ticks": x["owner"]["product"]["start_ticks"]}
                                            for x in final["completed"]],
            "actions": actions}


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        try:
            answer = supervisor()
        except (RelayError, OSError, ValueError) as exc:
            answer = {"action": "BLOCKED", "status": "BLOCKED", "needs_model_decision": True,
                      "reason": str(exc), "stage": None, "owner": None, "lease_remaining_seconds": None}
        print(json.dumps(answer, sort_keys=True, ensure_ascii=False), flush=True)
        return 2 if answer.get("action") in ("NOT_CONFIGURED", "NOT_INITIALIZED", "BLOCKED") or answer.get("status") in ("BLOCKED", "FAILED") or answer.get("inbox", {}).get("action") == "INBOX_BLOCKED" else 0
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for key in ("init", "tick", "adopt", "renew", "complete", "status", "_worker"):
        p = sub.add_parser(key)
        p.add_argument("--config", required=True)
        p.add_argument("--state", required=True)
        if key == "adopt":
            p.add_argument("--identity", required=True)
            p.add_argument("--lease", required=True, type=int)
        elif key == "renew":
            p.add_argument("--token", required=True)
            p.add_argument("--lease", required=True, type=int)
        elif key == "complete":
            p.add_argument("--receipt", required=True)
            p.add_argument("--acceptance", required=True)
        elif key == "_worker":
            p.add_argument("--token", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--dir", required=True)
    t = sub.add_parser("_demo_task")
    t.add_argument("--config", required=True)
    t.add_argument("--stage", required=True)
    t.add_argument("--rows", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        if args.action == "demo":
            outcome = demo(args.dir)
        elif args.action == "_demo_task":
            demo_task(args)
            return 0
        else:
            cfg, state = args.config, args.state
            if args.action == "init":
                outcome = init(cfg, state)
            elif args.action == "tick":
                outcome = tick(cfg, state)
            elif args.action == "adopt":
                outcome = adopt(cfg, state, args.identity, args.lease)
            elif args.action == "renew":
                outcome = renew(cfg, state, args.token, args.lease)
            elif args.action == "complete":
                outcome = complete(cfg, state, args.receipt, args.acceptance)
            elif args.action == "status":
                outcome = status(cfg, state)
            elif args.action == "_worker":
                worker(cfg, state, args.token)
                return 0
        print(json.dumps(outcome, sort_keys=True, ensure_ascii=False))
        return 0 if outcome.get("action") not in ("BLOCKED", "FAILED") else 2
    except (RelayError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"action": "BLOCKED", "reason": str(exc)}, sort_keys=True), flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main())
