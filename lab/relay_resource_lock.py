"""Exclusive, process-shared leased ownership of a named relay resource (POSIX)."""
import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class ResourceBusyError(RuntimeError):
    """A resource is owned or its state cannot safely be interpreted."""


def _paths(resource_id, lock_dir):
    if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 512:
        raise ValueError("invalid resource_id")
    root = Path(lock_dir)
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()
    return root / (name + ".json"), root / (name + ".guard")


@contextmanager
def _guard(path):
    # Guard inode is permanent; never unlink it (avoids split-brain locking).
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path, resource_id):
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("resource_id") != resource_id
                or not isinstance(data.get("owner_id"), str) or not data["owner_id"]
                or type(data.get("pid")) is not int or data["pid"] <= 0
                or type(data.get("lease_until")) not in (int, float)
                or type(data.get("token")) is not str or not data["token"]):
            raise ValueError("invalid ownership record")
        return data
    except (OSError, ValueError, UnicodeError, TypeError) as exc:
        raise ResourceBusyError("corrupt resource lock; manual investigation required") from exc


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _status(data, now, pid_alive):
    if data is None:
        return dict(owner_id=None, pid=None, lease_until=None, stale=False, live=False)
    dead = not pid_alive(data["pid"])
    expired = now >= data["lease_until"]
    return dict(data, stale=dead or expired, live=not (dead or expired))


def _write(path, data):
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def inspect(resource_id, lock_dir, now=None, pid_alive=None):
    path, guard = _paths(resource_id, lock_dir)
    current = time.time() if now is None else now
    with _guard(guard):
        return _status(_read(path, resource_id), current, pid_alive or _alive)


def acquire(resource_id, lock_dir, owner_id, lease_seconds, now=None, pid_alive=None):
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("owner_id required")
    if type(lease_seconds) not in (int, float) or not 0 < lease_seconds < float("inf"):
        raise ValueError("positive finite lease_seconds required")
    path, guard = _paths(resource_id, lock_dir)
    current = time.time() if now is None else now
    with _guard(guard):
        previous = _read(path, resource_id)
        state = _status(previous, current, pid_alive or _alive)
        if state["live"]:
            raise ResourceBusyError("resource %s owned by %s (pid %s)" %
                                    (resource_id, previous["owner_id"], previous["pid"]))
        record = dict(resource_id=resource_id, owner_id=owner_id, pid=os.getpid(),
                      lease_until=current + lease_seconds, token=os.urandom(16).hex(),
                      previous_owner=previous["owner_id"] if previous else None,
                      recovered_from_dead_owner=bool(previous and
                          not (pid_alive or _alive)(previous["pid"])))
        _write(path, record)
        return record


def _verify(path, resource_id, owner_id, token, now, pid_alive):
    data = _read(path, resource_id)
    if (data is None or data["owner_id"] != owner_id or data["token"] != token
            or data["pid"] != os.getpid() or
            not _status(data, now, pid_alive)["live"]):
        raise ResourceBusyError("not the live owner of %s" % resource_id)
    return data


def renew(resource_id, lock_dir, owner_id, token, lease_seconds, now=None, pid_alive=None):
    if type(lease_seconds) not in (int, float) or not 0 < lease_seconds < float("inf"):
        raise ValueError("positive finite lease_seconds required")
    path, guard = _paths(resource_id, lock_dir)
    current = time.time() if now is None else now
    with _guard(guard):
        data = _verify(path, resource_id, owner_id, token, current, pid_alive or _alive)
        data["lease_until"] = max(data["lease_until"], current + lease_seconds)
        _write(path, data)
        return data


def release(resource_id, lock_dir, owner_id, token, now=None, pid_alive=None):
    path, guard = _paths(resource_id, lock_dir)
    current = time.time() if now is None else now
    with _guard(guard):
        _verify(path, resource_id, owner_id, token, current, pid_alive or _alive)
        path.unlink()
        return True
