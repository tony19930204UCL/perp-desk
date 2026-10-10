"""Fail-closed, single-send relay operator entry point (no browser dependency)."""
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path

from lab import relay_resource_lock


class RelayError(RuntimeError):
    pass


def _ledger_path(job_id, ledger_dir):
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", job_id):
        raise ValueError("invalid job_id")
    return Path(ledger_dir) / (job_id + ".json")


def _read(path):
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("status"), str):
            raise ValueError("invalid ledger")
        return record
    except (OSError, ValueError, UnicodeError) as exc:
        raise RelayError("ledger corrupt; do not resend") from exc


def _save(path, record, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # Preserve even incomplete intent: never make this job sendable again.
            raise
    else:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _messages(transport):
    messages = transport.user_messages()
    if not isinstance(messages, (list, tuple)):
        raise RelayError("invalid user_messages response")
    return messages


def _exact_message(messages, prompt):
    return any(type(message) is str and message == prompt for message in messages)


def observe_prerequisites(config, transport):
    """Transport supplies observe() -> snapshot; unknown fields fail closed."""
    snapshot = transport.observe()
    if not isinstance(snapshot, dict):
        raise RelayError("invalid observation")
    required = ("route_ok", "composer_empty", "busy", "auth_required",
                "inline_approval_present")
    if any(type(snapshot.get(key)) is not bool for key in required):
        raise RelayError("incomplete observation")
    owner = relay_resource_lock.inspect(config["resource_id"], config["lock_dir"])
    return {**{key: snapshot[key] for key in required},
            "resource_owner": owner["owner_id"] if owner["live"] else None}


def _refusal(observation):
    for key, reason in (("route_ok", "wrong_route"),
                        ("composer_empty", "draft_present"),
                        ("busy", "busy"),
                        ("auth_required", "auth_required"),
                        ("inline_approval_present", "inline_approval_present")):
        if observation[key] != (key in ("route_ok", "composer_empty")):
            return reason
    return None


def dispatch_once(job_id, prompt, config, transport, ledger_dir):
    """Write durable intent before the sole send; never retry an uncertain send."""
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("nonempty prompt required")
    path = _ledger_path(job_id, ledger_dir)
    if _read(path) is not None:
        return "REFUSED_DUPLICATE"
    resource = config["resource_id"]
    lock_dir = config["lock_dir"]
    owner = config["owner_id"]
    try:
        lease = relay_resource_lock.acquire(resource, lock_dir, owner,
                                            config.get("lease_seconds", 120))
    except relay_resource_lock.ResourceBusyError:
        return "REFUSED_RESOURCE_LOCK"
    try:
        if _read(path) is not None:
            return "REFUSED_DUPLICATE"
        observation = observe_prerequisites(config, transport)
        reason = _refusal(observation)
        now = time.time()
        record = dict(job_id=job_id, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                      prompt=prompt, dispatch_time=now, status="REFUSED_PRE_SEND" if reason else "UNCERTAIN",
                      intent_recorded=not bool(reason), refusal_reason=reason)
        try:
            _save(path, record, exclusive=True)
        except FileExistsError:
            return "REFUSED_DUPLICATE"
        if reason:
            return "REFUSED_PRE_SEND"
        try:
            transport.send(prompt)
            confirmed = _exact_message(_messages(transport), prompt)
        except Exception:
            confirmed = False
        record["status"] = "SENT" if confirmed else "UNCERTAIN"
        _save(path, record)
        return record["status"]
    finally:
        relay_resource_lock.release(resource, lock_dir, owner, lease["token"])


def reconcile(job_id, transport, ledger_dir):
    """Read-only with respect to transport; ledger may advance on exact evidence."""
    path = _ledger_path(job_id, ledger_dir)
    record = _read(path)
    if record is None:
        raise RelayError("unknown job")
    if record["status"] != "UNCERTAIN":
        return record["status"]
    prompt = record.get("prompt")
    if (type(prompt) is str
            and hashlib.sha256(prompt.encode()).hexdigest() == record.get("prompt_sha256")
            and _exact_message(_messages(transport), prompt)):
        record["status"] = "SENT_CONFIRMED"
        _save(path, record)
    return record["status"]


def _epoch(value):
    if type(value) in (float, int):
        return float(value)
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("timezone required")
        return dt.timestamp()
    raise ValueError("invalid timestamp")


def accept_reply(reply, expected_job_id, expected_nonce, dispatch_time):
    """Require matching correlation fields and a timestamp at/after dispatch."""
    if not isinstance(reply, dict):
        return False
    if (not isinstance(expected_job_id, str) or not expected_job_id
            or not isinstance(expected_nonce, str) or not expected_nonce
            or reply.get("job_id") != expected_job_id
            or reply.get("nonce") != expected_nonce):
        return False
    try:
        return _epoch(reply.get("timestamp")) >= _epoch(dispatch_time)
    except (TypeError, ValueError, OverflowError):
        return False
