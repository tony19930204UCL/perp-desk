"""Cross-jobdir, fail-closed product dispatch authority (stdlib/Linux).

Configure a single absolute dispatch_lock_root for ALL callers on this profile.
No expiry/death automatically clears a send intent. This is not an approver.
"""
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


class DispatchBlocked(RuntimeError):
    def __init__(self, reason, winner=None):
        super().__init__(reason)
        self.winner = winner


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(pid=None):
    pid = pid or os.getpid()
    try:
        raw = Path("/proc/%d/stat" % pid).read_text()
        rest = raw.rsplit(")", 1)[1].strip().split()
        if rest[0] in ("Z", "X"):
            raise DispatchBlocked("OWNER_NOT_LIVE")
        return {"pid": pid, "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "start_ticks": rest[19]}
    except (OSError, IndexError) as exc:
        raise DispatchBlocked("OWNER_IDENTITY_UNKNOWN") from exc


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dispatch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        d = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _scope(job, job_dir, instruction, chat=None):
    root = job.get("dispatch_lock_root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise DispatchBlocked("DISPATCH_LOCK_ROOT_UNCONFIGURED")
    target = Path(root).resolve()
    if target.is_symlink():
        raise DispatchBlocked("DISPATCH_ROOT_SYMLINK")
    repo, pr, branch = job.get("repository"), job.get("pr_number"), job.get("branch")
    if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise DispatchBlocked("DISPATCH_REPO_UNVERIFIED")
    if type(pr) is not int or pr < 1 or not isinstance(branch, str) or not branch:
        raise DispatchBlocked("DISPATCH_PRODUCT_UNVERIFIED")
    url = chat or job.get("dispatch_chat_url") or job.get("chat_url")
    if not isinstance(url, str) or not re.fullmatch(r"https://chatgpt\.com/c/[A-Za-z0-9-]+", url):
        raise DispatchBlocked("DISPATCH_EXPLICIT_CHAT_REQUIRED")
    if not isinstance(instruction, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,140}", instruction):
        raise DispatchBlocked("DISPATCH_INSTRUCTION_INVALID")
    try:
        expires = datetime.datetime.fromisoformat(job["valid_until"])
        if expires.tzinfo is None or datetime.datetime.now(datetime.timezone.utc) >= expires:
            raise DispatchBlocked("DISPATCH_AUTHORIZATION_EXPIRED")
    except (TypeError, ValueError, KeyError) as exc:
        raise DispatchBlocked("DISPATCH_EXPIRY_UNVERIFIED") from exc
    allowlist = job.get("write_allowlist")
    if not isinstance(allowlist, list) or not allowlist:
        raise DispatchBlocked("DISPATCH_WRITE_SCOPE_MISSING")
    scope = {"repo": repo, "pr": pr, "branch": branch, "chat": url,
             "instruction": instruction, "files": sorted(allowlist)}
    product = _sha([repo, pr])
    return target, product, scope, str(Path(job_dir).resolve())


@contextlib.contextmanager
def _record(root, key):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / (key + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(fd, "r+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            path = root / (key + ".json")
            value = json.loads(path.read_text()) if path.exists() else None
            yield path, value
            fcntl.flock(stream, fcntl.LOCK_UN)
    finally:
        pass


def claim(job, job_dir, instruction, *, chat=None, lease=600):
    """One owner across all job directories; never steal after expiry/death."""
    if type(lease) is not int or lease < 1 or lease > 3600:
        raise DispatchBlocked("DISPATCH_LEASE_INVALID")
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    owner = _identity()
    with _record(root, key) as (path, prior):
        if prior:
            if prior["scope"] != scope or prior["job_dir"] != directory:
                raise DispatchBlocked("PRODUCT_ALREADY_CLAIMED", prior.get("owner"))
            if prior.get("phase") in ("SEND_UNKNOWN", "SEND_INTENT", "SENT_CONFIRMED"):
                raise DispatchBlocked("SEND_ALREADY_INTENDED_NO_RETRY", prior.get("owner"))
            if prior.get("owner") != owner:
                raise DispatchBlocked("OWNER_ALREADY_EXISTS", prior.get("owner"))
            if time.time() >= prior["lease_until"]:
                raise DispatchBlocked("OWNER_LEASE_EXPIRED", prior.get("owner"))
            return dict(prior)
        record = {"schema_version": 1, "scope": scope, "scope_sha256": _sha(scope),
                  "job_dir": directory, "owner": owner, "lease_until": time.time() + lease,
                  "phase": "PREPARED", "generation": _sha([scope, directory, owner]),
                  "created": time.time()}
        _write(path, record)
        return record


def check(job, job_dir, instruction, *, phase="prepare", chat=None):
    """Call immediately before draft, send, and permission clicks."""
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (_, prior):
        if prior is None:
            raise DispatchBlocked("DISPATCH_CLAIM_MISSING")
        if prior.get("scope_sha256") != _sha(scope) or prior.get("scope") != scope or prior.get("job_dir") != directory:
            raise DispatchBlocked("DISPATCH_SCOPE_GENERATION_CHANGED", prior.get("owner"))
        if prior.get("owner") != _identity():
            raise DispatchBlocked("DISPATCH_OWNER_PID_START_MISMATCH", prior.get("owner"))
        if time.time() >= prior["lease_until"]:
            raise DispatchBlocked("DISPATCH_LEASE_EXPIRED", prior.get("owner"))
        if phase == "send" and prior["phase"] != "PREPARED":
            raise DispatchBlocked("SEND_ALREADY_INTENDED_NO_RETRY", prior.get("owner"))
        if phase == "approve" and prior["phase"] != "SENT_CONFIRMED":
            raise DispatchBlocked("APPROVAL_REQUIRES_CONFIRMED_SEND", prior.get("owner"))
        return dict(prior)


def phase(job, job_dir, instruction, status, *, chat=None):
    if status not in ("SEND_INTENT", "SEND_UNKNOWN", "SENT_CONFIRMED", "BLOCKED"):
        raise DispatchBlocked("DISPATCH_PHASE_INVALID")
    check(job, job_dir, instruction, phase="send" if status == "SEND_INTENT" else "prepare", chat=chat)
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (path, prior):
        if status == "SEND_INTENT" and prior["phase"] != "PREPARED":
            raise DispatchBlocked("SEND_RETRY_DENIED", prior.get("owner"))
        if status != "SEND_INTENT" and prior["phase"] not in ("SEND_INTENT", "SEND_UNKNOWN", "PREPARED"):
            raise DispatchBlocked("DISPATCH_TERMINAL_ALREADY_RECORDED", prior.get("owner"))
        prior["phase"] = status
        prior["updated"] = time.time()
        _write(path, prior)
        return dict(prior)


def inspect(job, job_dir, instruction, *, chat=None):
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (_, state):
        return state


def adopt_existing(job, job_dir, instruction, identity, *, chat=None):
    """Only an explicitly confirmed still-live original owner; never a new send."""
    actual = _identity(identity.get("pid"))
    if actual != {k: identity.get(k) for k in actual}:
        raise DispatchBlocked("ADOPT_OWNER_IDENTITY_INVALID")
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (path, prior):
        if prior is not None:
            raise DispatchBlocked("EXISTING_PRODUCT_CLAIM", prior.get("owner"))
        record = {"schema_version": 1, "scope": scope, "scope_sha256": _sha(scope),
                  "job_dir": directory, "owner": actual, "lease_until": time.time() + 600,
                  "phase": "ADOPTED_EXTERNAL_NO_SEND", "generation": _sha([scope, directory, actual]),
                  "created": time.time()}
        _write(path, record)
        return record
