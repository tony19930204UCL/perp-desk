"""Offline relay invariants. No web/browser access and no implicit approvals."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

CHAT = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9-]+\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SENSITIVE = re.compile(
    r"(?i)(sk-[a-z0-9]{20,}|gh[pousr]_[a-z0-9]{20,}|github_pat_"
    r"[a-z0-9_]{20,}|PRIVATE KEY|api[_-]?key\s*[=:]|\.env\b)")
FORBIDDEN = re.compile(
    r"(?i)(always allow|always approve|允許所有|永遠允許|remember|"
    r"merge|delete|刪除|force push|workflow|permission change)")
PATH = re.compile(r"(?<![\w./-])([\w.-]+/[\w.-]+/(?:[\w./-]+))")


class RelayFixError(ValueError):
    pass


def normalized(text):
    """Only line ending/line-final whitespace normalisation, no suffix repair."""
    if not isinstance(text, str):
        return ""
    return "\n".join(x.rstrip() for x in
                     text.replace("\r\n", "\n").replace("\r", "\n")
                     .rstrip("\n").split("\n"))


def bubble_match(prompt, bubble):
    """Full DOM message subtree, excluded UI, bound to a user turn."""
    return bool(bubble.get("turn_bound") and bubble.get("subtree") and
                normalized(bubble["subtree"]) == normalized(prompt) and
                len(normalized(prompt)) > 0)


def readback(prompt, before, after):
    """One and only one new exact turn; never confirm from clipped UI."""
    if not isinstance(before, list) or not isinstance(after, list):
        return "AMBIGUOUS"
    old = sum(bubble_match(prompt, b) for b in before)
    new = sum(bubble_match(prompt, b) for b in after)
    if old == 0 and new == 1:
        return "CONFIRMED"
    if old == new:
        return "PENDING"
    return "AMBIGUOUS"


def card_guard(job, text):
    """Explicit operation scope; no basename matching or policy expansion."""
    if not isinstance(text, str) or not text or SENSITIVE.search(text):
        raise RelayFixError("CARD_SECRET_OR_EMPTY")
    if FORBIDDEN.search(text):
        raise RelayFixError("CARD_FORBIDDEN_OPERATION")
    if "GitHub" not in text or text.count("允許一次") != 1:
        raise RelayFixError("CARD_NOT_SINGLE_ALLOW_ONCE")
    repo, branch = job["repository"], job["branch"]
    allowed = job["write_allowlist"]
    if not isinstance(allowed, list) or not allowed:
        raise RelayFixError("CARD_NO_ALLOWLIST")
    named = any(path in text for path in allowed)
    repo_branch = repo in text and branch in text
    if not (named or repo_branch):
        raise RelayFixError("CARD_SCOPE_NOT_NAMED")
    # A name from another repository never authorises the current one.
    refs = re.findall(r"(?<![\w.-])([\w.-]+/[\w.-]+)/(?:blob|tree)/", text)
    if any(r != repo for r in refs):
        raise RelayFixError("CARD_OTHER_REPOSITORY")
    paths = re.findall(r"(?<![\w./-])(?:engineering|docs|lab|automation)/"
                       r"[\w./-]+", text)
    if any(path.rstrip(".,:;") not in allowed for path in paths):
        raise RelayFixError("CARD_PATH_OUT_OF_SCOPE")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
            dir=path.parent, prefix=".relay-", delete=False) as f:
        temp = Path(f.name)
        json.dump(value, f, ensure_ascii=False, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def make_job(directory, chat, repo, branch, pr, paths, expiry,
             lock_root, task_id, instruction):
    if not CHAT.fullmatch(str(chat)) or not REPO.fullmatch(str(repo)):
        raise RelayFixError("EXPLICIT_CHAT_REPO_REQUIRED")
    if type(pr) is not int or pr < 1 or not branch or not task_id or not instruction:
        raise RelayFixError("INVALID_JOB_IDENTITY")
    if not paths or len(set(paths)) != len(paths) or any(
            not p or Path(p).is_absolute() or ".." in Path(p).parts
            for p in paths):
        raise RelayFixError("INVALID_WRITE_SCOPE")
    if not Path(lock_root).is_absolute():
        raise RelayFixError("ABSOLUTE_LOCK_ROOT_REQUIRED")
    try:
        date=dt.datetime.fromisoformat(expiry.replace("Z","+00:00"))
    except ValueError as exc:
        raise RelayFixError("INVALID_EXPIRY") from exc
    if date.tzinfo is None or date <= dt.datetime.now(dt.timezone.utc):
        raise RelayFixError("EXPIRED_OR_NAIVE_JOB")
    target=Path(directory)
    target.mkdir(parents=True,exist_ok=True)
    if (target/"job.json").exists():
        raise RelayFixError("JOB_EXISTS_NO_OVERWRITE")
    value={"repository":repo,"branch":branch,"pr_number":pr,
           "chat_url":chat,"dispatch_chat_url":chat,
           "conversation_mode":"reuse","dispatch_lock_root":str(lock_root),
           "write_allowlist":list(paths),"valid_until":expiry,
           "task_id":task_id,"instruction_id":instruction}
    atomic(target/"job.json",value)
    return value


def main(argv=None):
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="action",required=True)
    p=sub.add_parser("make-job")
    for name in ("job-dir","chat-url","repo","branch","pr","allowlist",
                 "expiry","lock-root","task-id","instruction-id"):
        p.add_argument("--"+name,required=True)
    args=ap.parse_args(argv)
    try:
        result=make_job(args.job_dir,args.chat_url,args.repo,args.branch,
               int(args.pr),json.loads(args.allowlist),args.expiry,
               args.lock_root,args.task_id,args.instruction_id)
        print(json.dumps({"status":"CREATED","job":str(
            Path(args.job_dir)/"job.json"),"instruction_id":result["instruction_id"]}))
        return 0
    except (RelayFixError, ValueError, OSError) as exc:
        print(json.dumps({"status":"BLOCKED","reason":str(exc)}))
        return 3


if __name__=="__main__":
    raise SystemExit(main())
