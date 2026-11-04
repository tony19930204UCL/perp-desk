"""Fixed, read-only owner-metro bindings. No request-controlled paths or PIDs."""
import json
import os
import re
import stat
import time
from owner_activity import _read_bytes, _source_meta, _stamp

_NAMES = ("directive", "stage", "heartbeat", "launch", "adoption", "results", "liquidations", "decision_policy", "frozen_manifest")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_CACHE = {}
SOURCE_AGE_MS = {
    "ledger": 900000, "cron": 900000, "work": 900000,
    "directive": 300000, "heartbeat": 300000,
    "roadmap": 86400000, "journal": 86400000,
    "stage": 300000, "launch": 300000, "adoption": 300000,
    "results": 300000, "liquidations": 300000,
    "decision_policy": 86400000, "frozen_manifest": 86400000,
}


def parts(value):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("invalid binding")
    bits = value.split("/")
    if not 1 <= len(bits) <= 5 or any(
            not _PART.fullmatch(bit) or bit in (".", "..") for bit in bits):
        raise ValueError("invalid binding")
    return tuple(bits)



def _source_stamp(doc):
    """Use the documented source clocks; ts_ms is an integer millisecond epoch."""
    for name in ("updated_at", "at", "ts_iso", "completed_at"):
        stamp = _stamp(doc.get(name))
        if stamp is not None:
            return stamp
    value = doc.get("ts_ms")
    if type(value) is int and 946684800000 <= value < 4102444800000:
        return value
    return None


def _binding_extra(profile, paths):
    managed = profile.get("managed", [])
    nodes = profile.get("nodes", [])
    if (not isinstance(managed, list) or len(managed) > 16 or
            not isinstance(nodes, list) or len(nodes) > 24):
        raise ValueError("oversized binding")
    kinds = {"collector", "compute", "lease", "observer", "coordination"}
    products = {"maker", "collectors", "relay"}
    eligible = []
    for row in managed:
        if not isinstance(row, dict):
            continue
        if (not isinstance(row.get("id"), str) or
                not _PART.fullmatch(row["id"]) or row.get("kind") not in kinds or
                row.get("product") not in products or row.get("source") != "heartbeat" or
                "heartbeat" not in paths or
                not isinstance(row.get("cmd_contains"), str) or
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", row["cmd_contains"])):
            continue
        eligible.append({k: row[k] for k in
                         ("id", "kind", "product", "source", "cmd_contains")})
    paths["managed"] = eligible
    paths["nodes"] = nodes
    return paths


def load(root, now_ms, limit=1048576):
    """Only a fixed trusted local profile; an absent binding is not evidence."""
    document, health = {}, {}
    try:
        raw, _ = _read_bytes(root, ("owner_metro", "bindings.json"), 32768)
        profile = json.loads(raw)
        if (not isinstance(profile, dict) or profile.get("schema_version") != 1
                or not isinstance(profile.get("sources"), dict)):
            raise ValueError("bad profile")
        bindings = profile["sources"]
        if set(bindings) - set(_NAMES):
            raise ValueError("unknown source")
        paths = {key: parts(value) for key, value in bindings.items()}
        if len(set(paths.values())) != len(paths):
            raise ValueError("duplicate source")
        paths = _binding_extra(profile, paths)
    except (OSError, ValueError, TypeError):
        return {}, {"bindings": {"state": "invalid"}}, {}
    for key in _NAMES:
        if key not in paths:
            health[key] = {"state": "unbound"}
            continue
        try:
            signature = _source_meta(root, paths[key])
            if signature[2] > min(limit, 1048576):
                raise ValueError("over limit")
            cached = _CACHE.get((root, key))
            if cached is not None and cached[0] == signature:
                obj = cached[1]
            else:
                raw, _ = _read_bytes(root, paths[key], min(limit, 1048576))
                obj = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(obj, dict):
                    raise ValueError("non-object")
                _CACHE[(root, key)] = (signature, obj)
                if len(_CACHE) > 48:
                    _CACHE.pop(next(iter(_CACHE)))
            document[key] = obj
            at = _source_stamp(obj)
            age = None if at is None or at > now_ms + 5000 else max(0, now_ms - at)
            health[key] = {"state": "stale" if age is not None and age > SOURCE_AGE_MS[key] else (
                           "available" if age is not None else "unverifiable"),
                           "age_ms": age,
                           "completed": False}
        except FileNotFoundError:
            health[key] = {"state": "missing", "completed": False}
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            health[key] = {"state": "invalid", "completed": False}
    return document, health, paths


def processes(launch, adoption, now_ms, proc_root="/proc"):
    """Linux /proc matching; on other systems identity remains unknown."""
    out = []
    rows = launch.get("processes") if isinstance(launch, dict) else None
    adopted = adoption.get("processes") if isinstance(adoption, dict) else None
    if not isinstance(rows, list) or not isinstance(adopted, list):
        return out
    adopted_ids = {(x.get("id"), x.get("pid"), x.get("start_ticks"))
                   for x in adopted if isinstance(x, dict)}
    seen = set()
    for item in rows[:128]:
        if not isinstance(item, dict):
            continue
        pid, ticks, name = item.get("pid"), item.get("start_ticks"), item.get("id")
        if (type(pid) is not int or not 1 <= pid <= 4194304 or type(ticks) is not int
                or ticks <= 0 or not isinstance(name, str)
                or not _PART.fullmatch(name) or (name, pid, ticks) not in adopted_ids
                or (pid, ticks) in seen):
            continue
        seen.add((pid, ticks))
        if item.get('wrapper') is True:
            continue
        state = "unknown"
        elapsed = rss = None
        try:
            # /proc/stat comm may contain spaces and closing parens.
            with open(os.path.join(proc_root, str(pid), "stat"), "rb") as fh:
                statline = fh.read(4096).decode("utf-8")
            fields = statline[statline.rfind(")") + 2:].split()
            actual_ticks = int(fields[19])
            if actual_ticks != ticks:
                state = "mismatch"
            else:
                expected_boot = item.get("boot_id")
                expected_argv = item.get("argv_sha256")
                expected_cwd = item.get("cwd")
                if not isinstance(expected_boot, str) or not re.fullmatch(r"[a-f0-9-]{36}", expected_boot):
                    state = "unverified"
                elif not isinstance(expected_argv, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_argv):
                    state = "unverified"
                elif not isinstance(expected_cwd, str) or not expected_cwd.startswith("/"):
                    state = "unverified"
                else:
                    import hashlib
                    with open(os.path.join(proc_root, "sys/kernel/random/boot_id"), "r") as fh:
                        boot = fh.read().strip()
                    with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as fh:
                        cmd = fh.read(8192)
                    cwd = os.readlink(os.path.join(proc_root, str(pid), "cwd"))
                    if (boot != expected_boot or hashlib.sha256(cmd).hexdigest() != expected_argv
                            or cwd != expected_cwd):
                        state = "mismatch"
                    else:
                        state = "verified"
                        clock_ticks = os.sysconf("SC_CLK_TCK")
                        with open(os.path.join(proc_root, "uptime")) as fh:
                            up = float(fh.read().split()[0])
                        elapsed = max(0, int(up - ticks / clock_ticks))
                        rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError, OverflowError):
            state = "gone"
        out.append({"id": name, "kind": item.get("kind") if item.get("kind") in
                    ("compute", "collector", "lease", "observer", "coordination") else "unknown",
                    "product": item.get("product") if item.get("product") in
                    ("maker", "collectors", "relay") else None,
                    "state": state, "pid": pid if state == "verified" else None,
                    "elapsed_s": elapsed, "rss_bytes": rss})
    return out

def processes_managed(rows, docs, health, now_ms, proc_root="/proc"):
    """Verify current scheduler-owned PID via fresh source and executable argv."""
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows[:16]:
        if not isinstance(row, dict) or row.get("source") != "heartbeat":
            continue
        if (not isinstance(row.get("id"), str) or not _PART.fullmatch(row["id"])
                or row.get("kind") not in
                ("collector", "compute", "lease", "observer", "coordination")
                or row.get("product") not in ("maker", "collectors", "relay")
                or not isinstance(row.get("cmd_contains"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", row["cmd_contains"])):
            continue
        if health.get("heartbeat", {}).get("state") != "available":
            continue
        src = docs.get("heartbeat")
        if not isinstance(src, dict):
            continue
        pid = src.get("pid")
        if type(pid) is not int or not 1 <= pid <= 4194304:
            continue
        state, elapsed, rss, ticks = "unverified", None, None, None
        try:
            with open(os.path.join(proc_root, str(pid), "stat"), "rb") as fd:
                data = fd.read(4096).decode("utf-8")
            fields = data[data.rfind(")") + 2:].split()
            ticks = int(fields[19])
            with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as fd:
                argv = fd.read(8192).split(b"\x00")
            word = row["cmd_contains"].encode("ascii")
            if not any(word in os.path.basename(arg).split(b" ")[0] or word in arg
                       for arg in argv if arg):
                state = "mismatch"
            else:
                hz = os.sysconf("SC_CLK_TCK")
                start = src.get("started_ms")
                if type(start) is int:
                    with open(os.path.join(proc_root, "stat"), "r") as fd:
                        boot = next(int(line.split()[1]) for line in fd
                                    if line.startswith("btime "))
                    if abs((boot + ticks / hz) * 1000 - start) > 10000:
                        state = "mismatch"
                    else:
                        state = "verified"
                elif start is None:
                    state = "verified"
                if state == "verified":
                    with open(os.path.join(proc_root, "uptime")) as fd:
                        uptime = float(fd.read().split()[0])
                    elapsed = max(0, int(uptime - ticks / hz))
                    rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError, StopIteration, OverflowError):
            state = "gone"
        out.append({"id": row["id"], "kind": row["kind"], "product": row["product"],
                    "identity": "managed", "state": state,
                    "pid": pid if state == "verified" else None,
                    "start_ticks": ticks if state == "verified" else None,
                    "elapsed_s": elapsed, "rss_bytes": rss})
    return out
