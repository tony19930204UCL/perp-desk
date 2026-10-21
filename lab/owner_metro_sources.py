"""Fixed, read-only owner-metro bindings. No request-controlled paths or PIDs."""
import json
import os
import re
import stat
import time
from owner_activity import _read_bytes

_NAMES = ("directive", "stage", "heartbeat", "launch", "adoption", "results", "liquidations")
_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
_CACHE = {}


def parts(value):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError("invalid binding")
    bits = value.split("/")
    if not 1 <= len(bits) <= 5 or any(
            not _PART.fullmatch(bit) or bit in (".", "..") for bit in bits):
        raise ValueError("invalid binding")
    return tuple(bits)


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
    except (OSError, ValueError, TypeError):
        return {}, {"bindings": {"state": "invalid"}}, {}
    for key in _NAMES:
        if key not in paths:
            health[key] = {"state": "unbound"}
            continue
        try:
            raw, _ = _read_bytes(root, paths[key], min(limit, 1048576))
            obj = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(obj, dict):
                raise ValueError("non-object")
            document[key] = obj
            health[key] = {"state": "available"}
        except FileNotFoundError:
            health[key] = {"state": "missing"}
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            health[key] = {"state": "invalid"}
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
                    "state": state, "pid": pid if state == "verified" else None,
                    "elapsed_s": elapsed, "rss_bytes": rss})
    return out
