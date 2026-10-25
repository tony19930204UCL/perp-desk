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
import secrets
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
    if Path(root).is_symlink():
        raise DispatchBlocked("DISPATCH_ROOT_SYMLINK")
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
    # Reload on disk at each claim/check, never trust a stale caller copy.
    job_file = Path(job_dir).resolve() / "job.json"
    if job_file.is_file():
        newest = json.loads(job_file.read_text(encoding="utf-8"))
        for field in ("repository", "pr_number", "branch", "dispatch_lock_root",
                      "dispatch_chat_url", "chat_url", "valid_until", "write_allowlist"):
            if newest.get(field) != job.get(field):
                raise DispatchBlocked("JOB_SCOPE_CHANGED")
    else:
        raise DispatchBlocked("JOB_FILE_REQUIRED")
    active_file = Path(job_dir).resolve() / "active_instruction.json"
    if active_file.is_file():
        active = json.loads(active_file.read_text(encoding="utf-8"))
        if active.get("instruction_id") != instruction:
            raise DispatchBlocked("ACTIVE_INSTRUCTION_CHANGED")
        if active.get("chat_url") not in (None, url):
            raise DispatchBlocked("ACTIVE_CHAT_CHANGED")
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
            if prior.get("phase") in ("SEND_UNKNOWN", "SEND_INTENT", "SENT_CONFIRMED", "APPROVE_INTENT", "APPROVED"):
                raise DispatchBlocked("SEND_ALREADY_INTENDED_NO_RETRY", prior.get("owner"))
            if prior.get("owner") != owner:
                raise DispatchBlocked("OWNER_ALREADY_EXISTS", prior.get("owner"))
            if time.time() >= prior["lease_until"]:
                raise DispatchBlocked("OWNER_LEASE_EXPIRED", prior.get("owner"))
            return dict(prior)
        record = {"schema_version": 1, "scope": scope, "scope_sha256": _sha(scope),
                  "job_dir": directory, "owner": owner, "lease_until": time.time() + lease,
                  "phase": "PREPARED", "send_ticket": secrets.token_hex(24),
                  "generation": _sha([scope, directory, owner]),
                  "created": time.time()}
        _write(path, record)
        return record


def _actor(prior, ticket=None):
    """Owner, or directly spawned child holding its explicit send ticket."""
    actor = _identity()
    if prior.get("owner") == actor:
        return
    parent = prior.get("owner")
    if not isinstance(ticket, str) or not secrets.compare_digest(
            str(prior.get("send_ticket", "")), ticket):
        raise DispatchBlocked("OWNER_ID_MISMATCH", parent)
    if os.getppid() != parent["pid"] or _identity(parent["pid"]) != parent:
        raise DispatchBlocked("DELEGATE_PARENT_INVALID", parent)


def check(job, job_dir, instruction, *, phase="prepare", chat=None, ticket=None):
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (_, prior):
        if prior is None:
            raise DispatchBlocked("DISPATCH_CLAIM_MISSING")
        if prior.get("scope_sha256") != _sha(scope) or prior.get("scope") != scope or prior.get("job_dir") != directory:
            raise DispatchBlocked("SCOPE_CHANGED", prior.get("owner"))
        _actor(prior, ticket)
        if time.time() >= prior["lease_until"]:
            raise DispatchBlocked("DISPATCH_LEASE_EXPIRED", prior.get("owner"))
        if phase in ("prepare", "send") and prior["phase"] != "PREPARED":
            raise DispatchBlocked("SEND_ALREADY_INTENDED_NO_RETRY", prior.get("owner"))
        if phase == "approve" and prior["phase"] != "SENT_CONFIRMED":
            raise DispatchBlocked("SEND_RECEIPT_REQUIRED", prior.get("owner"))
        return dict(prior)


def _confirmed(directory, instruction, chat):
    for filename in ("send_" + instruction + ".json", "receipt-" + instruction + ".json"):
        p = Path(directory) / filename
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if (data.get("status", data.get("stage")) == "CONFIRMED"
                and data.get("chat_url", data.get("url")) == chat
                and data.get("instruction_id") == instruction
                and (data.get("evidence_kind") == "EXACT_RENDERED_USER_BUBBLE"
                     or any(x.get("stage") == "CONFIRMED" and
                            x.get("evidence") == "EXACT_RENDERED_USER_BUBBLE"
                            for x in data.get("history", [])))
                and (data.get("text_hash") or data.get("prompt_sha256"))):
            return hashlib.sha256(p.read_bytes()).hexdigest()
    return None


def phase(job, job_dir, instruction, status, *, chat=None, ticket=None):
    if status not in ("SEND_INTENT", "SEND_UNKNOWN", "SENT_CONFIRMED", "BLOCKED"):
        raise DispatchBlocked("DISPATCH_PHASE_INVALID")
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (path, prior):
        if not prior or prior.get("scope_sha256") != _sha(scope) or prior.get("job_dir") != directory:
            raise DispatchBlocked("SCOPE_CHANGED", prior.get("owner") if prior else None)
        _actor(prior, ticket)
        if time.time() >= prior["lease_until"]:
            raise DispatchBlocked("DISPATCH_LEASE_EXPIRED", prior.get("owner"))
        transitions = {"SEND_INTENT": ("PREPARED",),
                       "SEND_UNKNOWN": ("SEND_INTENT",),
                       "SENT_CONFIRMED": ("SEND_UNKNOWN",),
                       "BLOCKED": ("PREPARED",)}
        if prior["phase"] not in transitions[status]:
            raise DispatchBlocked("SEND_RETRY_DENIED", prior.get("owner"))
        if status == "SENT_CONFIRMED":
            evidence = _confirmed(directory, instruction, scope["chat"])
            if not evidence:
                raise DispatchBlocked("EXACT_SEND_NOT_CONFIRMED", prior.get("owner"))
            prior["send_receipt_sha256"] = evidence
            prior["approval_grant"] = {"scope_sha256": _sha(scope),
                                       "receipt_sha256": evidence,
                                       "instruction_id": instruction,
                                       "chat": scope["chat"]}
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


def adopt_send(job, job_dir, instruction, *, chat=None):
    """Explicit operator adoption of a previously CONFIRMED instruction send.

    Unrelated adopt_existing remains ADOPTED_EXTERNAL_NO_SEND and cannot approve.
    No message, market launch, or approval occurs here.
    """
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    receipt_sha = _confirmed(directory, instruction, scope["chat"])
    grant = job.get("dispatch_adopt_source")
    expected = {"receipt_sha256": receipt_sha, "repository": scope["repo"],
                "pr_number": scope["pr"], "branch": scope["branch"],
                "chat": scope["chat"], "instruction_id": instruction,
                "scope_sha256": _sha(scope)}
    if not receipt_sha or not isinstance(grant, dict) or grant != expected:
        raise DispatchBlocked("ADOPT_SEND_SOURCE_INVALID")
    with _record(root, key) as (path, prior):
        if prior is not None:
            raise DispatchBlocked("EXISTING_PRODUCT_CLAIM", prior.get("owner"))
        owner = _identity()  # approval actor, NOT the external market owner
        now = time.time()
        record = {"schema_version": 1, "scope": scope, "scope_sha256": _sha(scope),
                  "job_dir": directory, "owner": owner, "lease_until": now + 600,
                  "phase": "SENT_CONFIRMED", "send_receipt_sha256": receipt_sha,
                  "approval_grant": {"scope_sha256": _sha(scope),
                                     "receipt_sha256": receipt_sha,
                                     "instruction_id": instruction, "chat": scope["chat"]},
                  "adopted_send": True, "cards": {}, "created": now,
                  "generation": _sha([scope, directory, owner])}
        _write(path, record)
        return dict(record)


def approval_once(job, job_dir, instruction, fingerprint, *, chat=None):
    """Atomically claim ONE distinct card delegated from exact sent instruction."""
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise DispatchBlocked("CARD_FINGERPRINT_INVALID")
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (path, prior):
        if not prior or prior.get("scope") != scope or prior.get("job_dir") != directory:
            raise DispatchBlocked("NO_DELEGATED_SEND", prior.get("owner") if prior else None)
        if prior.get("phase") != "SENT_CONFIRMED":
            raise DispatchBlocked("SEND_RECEIPT_REQUIRED", prior.get("owner"))
        current = _confirmed(directory, instruction, scope["chat"])
        grant = {"scope_sha256": _sha(scope), "receipt_sha256": current,
                 "instruction_id": instruction, "chat": scope["chat"]}
        if not current or prior.get("send_receipt_sha256") != current or prior.get("approval_grant") != grant:
            raise DispatchBlocked("SEND_SOURCE_CHANGED", prior.get("owner"))
        if time.time() >= prior["lease_until"]:
            raise DispatchBlocked("DISPATCH_LEASE_EXPIRED", prior.get("owner"))
        cards = prior.setdefault("cards", {})
        if fingerprint in cards:
            raise DispatchBlocked("CARD_ALREADY_ATTEMPTED", cards[fingerprint].get("owner"))
        if any(c["phase"] == "APPROVE_INTENT" for c in cards.values()):
            raise DispatchBlocked("CARD_OUTCOME_UNKNOWN")
        cards[fingerprint] = {"phase": "APPROVE_INTENT", "owner": _identity(),
                              "source_sha256": current, "at": time.time()}
        _write(path, prior)
        return dict(prior)


def reconcile(job, job_dir, instruction, *, receipt, chat=None, fingerprint=None):
    """Positive readback only; never reopen send or card effects."""
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (path, prior):
        if not prior or prior.get("scope") != scope or prior.get("job_dir") != directory:
            raise DispatchBlocked("RECONCILE_SCOPE_INVALID")
        if receipt == "SEND_CONFIRMED":
            if prior["phase"] != "SEND_UNKNOWN":
                raise DispatchBlocked("RECONCILE_PHASE_INVALID")
            evidence = _confirmed(directory, instruction, scope["chat"])
            if not evidence:
                raise DispatchBlocked("EXACT_SEND_NOT_CONFIRMED")
            prior["phase"] = "SENT_CONFIRMED"
            prior["send_receipt_sha256"] = evidence
            prior["approval_grant"] = {"scope_sha256": _sha(scope),
                                       "receipt_sha256": evidence,
                                       "instruction_id": instruction, "chat": scope["chat"]}
        elif receipt == "APPROVED":
            card = prior.get("cards", {}).get(fingerprint)
            if not card or card.get("phase") != "APPROVE_INTENT" or card.get("owner") != _identity():
                raise DispatchBlocked("CARD_RECONCILE_INVALID")
            if _confirmed(directory, instruction, scope["chat"]) != card["source_sha256"]:
                raise DispatchBlocked("SEND_SOURCE_CHANGED")
            card["phase"] = "APPROVED"
            card["readback_at"] = time.time()
        else:
            raise DispatchBlocked("POSITIVE_READBACK_REQUIRED")
        _write(path, prior)
        return dict(prior)


def approve_check(job, job_dir, instruction, fingerprint, *, chat=None):
    """Immediate before-click scope/owner/lease/card/source-generation check."""
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (_, prior):
        if not prior or prior.get("scope") != scope or prior.get("job_dir") != directory:
            raise DispatchBlocked("CARD_SCOPE_CHANGED", prior.get("owner") if prior else None)
        card = prior.get("cards", {}).get(fingerprint)
        if not card or card.get("phase") != "APPROVE_INTENT":
            raise DispatchBlocked("CARD_INTENT_MISMATCH", prior.get("owner"))
        if card.get("owner") != _identity():
            raise DispatchBlocked("CARD_OWNER_MISMATCH", card.get("owner"))
        if time.time() >= prior["lease_until"]:
            raise DispatchBlocked("DISPATCH_LEASE_EXPIRED", prior.get("owner"))
        current = _confirmed(directory, instruction, scope["chat"])
        if (not current or current != card["source_sha256"]
                or current != prior.get("send_receipt_sha256")
                or prior.get("approval_grant") != {
                    "scope_sha256": _sha(scope), "receipt_sha256": current,
                    "instruction_id": instruction, "chat": scope["chat"]}):
            raise DispatchBlocked("SEND_SOURCE_CHANGED", prior.get("owner"))
        return dict(prior)


def release_revise(job, job_dir, instruction, verdict_path, *, chat=None):
    """Release a confirmed instruction only on a bound, reviewed REVISE verdict."""
    root, key, scope, directory = _scope(job, job_dir, instruction, chat)
    with _record(root, key) as (live, prior):
        if prior is None:
            raise DispatchBlocked("RELEASE_NOTHING")
        try:
            raw = Path(verdict_path).read_bytes()
            verdict = json.loads(raw)
            fields = ("schema_version", "status", "reviewer", "repository",
                      "pr_number", "branch", "released_instruction",
                      "next_instruction", "released_generation", "evidence")
            if (not isinstance(verdict, dict)
                    or any(name not in verdict for name in fields)
                    or type(verdict["schema_version"]) is not int
                    or verdict["schema_version"] != 1
                    or not isinstance(verdict["status"], str)
                    or not isinstance(verdict["reviewer"], str)
                    or not verdict["reviewer"].strip()
                    or type(verdict["pr_number"]) is not int
                    or not isinstance(verdict["evidence"], list)
                    or not verdict["evidence"]
                    or any(not isinstance(item, str) or not item.strip()
                           for item in verdict["evidence"])):
                raise ValueError("bad verdict fields")
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise DispatchBlocked("RELEASE_VERDICT_INVALID") from exc
        if verdict["status"] != "REVISE":
            raise DispatchBlocked("RELEASE_NOT_REVISE")
        old_scope = prior.get("scope", {})
        if (verdict["repository"] != scope["repo"]
                or verdict["pr_number"] != scope["pr"]
                or verdict["branch"] != scope["branch"]
                or any(old_scope.get(name) != scope[name]
                       for name in ("repo", "pr", "branch"))
                or verdict["released_instruction"] != old_scope.get("instruction")
                or verdict["released_generation"] != prior.get("generation")
                or verdict["next_instruction"] != instruction
                or instruction == old_scope.get("instruction")):
            raise DispatchBlocked("RELEASE_INSTR_MISMATCH")
        digest = hashlib.sha256(raw).hexdigest()
        grant = {"verdict_sha256": digest,
                 "released_generation": prior["generation"],
                 "next_instruction": instruction}
        if job.get("dispatch_release_grant") != grant:
            raise DispatchBlocked("RELEASE_GRANT_MISMATCH")
        if (prior.get("phase") != "SENT_CONFIRMED"
                or not prior.get("send_receipt_sha256")
                or _confirmed(prior["job_dir"],old_scope["instruction"],
                              old_scope["chat"]) != prior["send_receipt_sha256"]
                or prior.get("adopted_send")
                or any(card.get("phase") == "APPROVE_INTENT"
                       for card in prior.get("cards", {}).values())):
            raise DispatchBlocked("RELEASE_CLAIM_ACTIVE")
        archive_dir = root / "released"
        archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        archive_dir.chmod(0o700)
        for record_path in archive_dir.glob("*.json"):
            try:
                archived = json.loads(record_path.read_bytes())
            except (OSError, ValueError, UnicodeError) as exc:
                raise DispatchBlocked("RELEASE_ARCHIVE_INVALID") from exc
            if archived.get("verdict_sha256") == digest:
                raise DispatchBlocked("RELEASE_VERDICT_REUSED")
        archive = archive_dir / (key + "-" + prior["generation"] + ".json")
        if archive.exists():
            raise DispatchBlocked("RELEASE_ARCHIVE_EXISTS")
        result = dict(prior, released_at=time.time(), verdict_sha256=digest,
                      verdict=verdict, next_instruction=instruction,
                      releaser=_identity())
        _write(archive, result)
        os.unlink(live)
        dfd = os.open(str(root), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return result
