#!/usr/bin/env python3
"""Fail-closed, profile-wide GitHub approval transaction (offline-testable).

Approval never sends, navigates, edits a composer, or changes a connector permission.
All browser actions are scoped to a verified current assistant turn and a single card.
DOM structures here are hypotheses awaiting authenticated live inspection.
"""
from __future__ import annotations

import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import product_dispatch as pd

CARD = "要允許 ChatGPT 使用 GitHub 嗎？"
EXACT_CHAT = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9-]+")
LONG = re.compile(r"[A-Za-z0-9_\-]{32,}")
SNAKE = re.compile(r"[a-z][a-z0-9]{0,23}(?:_[a-z0-9]{1,24}){2,}")
STRICT_NEVER = re.compile(r"(?i)sk_(?:live|test)_|xox[abp]-|AKIA[0-9A-Z]{12,}|-----BEGIN")
STRICT_REGEX = re.compile(
    r"(?i)(?:sk-[a-z0-9]{20,}|ghp_[a-z0-9]{20,}|PRIVATE KEY"
    r"|api[_-]?key\s*[=:]\s*[\"'][a-z0-9]{20,})")
DIFF_SECRET = re.compile(
    r"(?i)(?:sk-[a-z0-9]{20,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}"
    r"|PRIVATE KEY|api[_-]?key\s*[=:]\s*[\"'][a-z0-9]{20,})")


class AdmissionError(Exception):
    pass


class PolicyRejected(Exception):
    def __init__(self, reason, fallback=False):
        super().__init__(reason)
        self.fallback = fallback


class InfraError(Exception):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def profile_root(job_dir):
    """Explicit portable root; never infer a private literal from deployed snapshots."""
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    p = Path(job_dir).resolve()
    for item in [p, *p.parents]:
        if item.name == "scratch" and item.parent.name == "cache":
            return item.parent.parent
    return Path.home() / ".hermes" / "profiles" / "perp-desk"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def admission(job_dir, expected=None, clock=utcnow):
    """Every inspection reloads job, active instruction and positive exact send receipt."""
    root = Path(job_dir)
    j = load_json(root / "job.json")
    a = load_json(root / "active_instruction.json")
    tid = j.get("task_id")
    iid = a.get("instruction_id")
    if not tid or not iid or a.get("task_id") != tid:
        raise AdmissionError("TASK_INSTRUCTION_MISMATCH")
    if expected is not None and (tid, iid, a.get("chat_url")) != expected:
        raise AdmissionError("ACTIVE_IDENTITY_CHANGED")
    chat = a.get("chat_url")
    if not isinstance(chat, str) or not EXACT_CHAT.fullmatch(chat):
        raise AdmissionError("PINNED_CHAT_REQUIRED")
    # A new-mode job can retain its homepage transport URL after dispatch.
    # Only the active instruction and CONFIRMED receipt pin the resulting chat.
    mode = j.get("conversation_mode", "new")
    if mode not in ("new", "reuse"):
        raise AdmissionError("CONVERSATION_MODE_INVALID")
    if mode == "reuse" and j.get("chat_url") != chat:
        raise AdmissionError("JOB_CHAT_MISMATCH")
    if mode == "new" and j.get("chat_url") not in (
            None, "https://chatgpt.com/", "https://chatgpt.com", chat):
        raise AdmissionError("JOB_CHAT_MISMATCH")
    try:
        expires = dt.datetime.fromisoformat(j["valid_until"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AdmissionError("INVALID_EXPIRY") from exc
    if expires.tzinfo is None or expires.utcoffset() is None or clock() >= expires:
        raise AdmissionError("EXPIRED_OR_NAIVE")
    receipt = load_json(root / ("send_" + iid + ".json"))
    if receipt.get("status") != "CONFIRMED" or receipt.get("chat_url") != chat:
        raise AdmissionError("UNCONFIRMED_DISPATCH")
    if receipt.get("chat_path") not in (None, "/", chat.removeprefix("https://chatgpt.com")):
        raise AdmissionError("RECEIPT_CHAT_MISMATCH")
    if not isinstance(j.get("write_allowlist"), list) or not j.get("repository") or not j.get("branch"):
        raise AdmissionError("JOB_SCOPE_INVALID")
    return j, (tid, iid, chat)


def transport(job, request):
    cmd = job.get("transport_command")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise InfraError("TRANSPORT_COMMAND_INVALID")
    try:
        p = subprocess.run(cmd, input=json.dumps(request, ensure_ascii=False),
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InfraError("TRANSPORT_TIMEOUT_OR_EXEC") from exc
    if p.returncode:
        raise InfraError("TRANSPORT_NONZERO")
    if not p.stdout.strip():
        raise InfraError("TRANSPORT_EMPTY")
    try:
        return json.loads(p.stdout)
    except (TypeError, ValueError) as exc:
        raise InfraError("TRANSPORT_JSON_INVALID") from exc


def snapshot(job, send=transport):
    try:
        result = send(job, {"op": "snapshot"})
    except InfraError:
        raise
    except Exception as exc:
        raise InfraError("SNAPSHOT_TRANSPORT_OR_ADAPTER") from exc
    if not isinstance(result, dict) or not isinstance(result.get("url"), str):
        raise InfraError("SNAPSHOT_INVALID")
    return result


# No undocumented global button indices or page-wide allow-once searches.
# Identity is held by DOM object references across evaluate calls; non-unique or
# detached cards fail closed. A portal dialog is accepted ONLY via card aria-controls.
DOM_JS = r"""
(() => {
 "use strict";
 const token = __TOKEN__, action = __ACTION__;
 const cardTitle = "要允許 ChatGPT 使用 GitHub 嗎？";
 const visible = e => !!e && e.isConnected && e.getClientRects().length > 0 &&
     getComputedStyle(e).visibility !== "hidden" && getComputedStyle(e).display !== "none";
 const enabled = e => visible(e) && !e.disabled && e.getAttribute("aria-disabled") !== "true";
 const buttons = e => [...e.querySelectorAll("button")];
 const label = e => (e.innerText || e.textContent || "").trim();
 const unique = a => a.length === 1 ? a[0] : null;
 const activeTurn = () => {
   const users = [...document.querySelectorAll('[data-message-author-role="user"]')];
   const assistants = [...document.querySelectorAll('[data-message-author-role="assistant"]')];
   if (!users.length || !assistants.length) return null;
   const lastUser = users[users.length-1];
   const after = assistants.filter(a => !!(lastUser.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING));
   return after.length ? after[after.length-1] : null;
 };
 const turn = activeTurn();
 const candidates = () => {
   if (!turn) return [];
   const groups = [...turn.querySelectorAll('[data-approval-card], [role="group"]')];
   return groups.filter(g => visible(g) && label(g).includes(cardTitle) &&
     g.getAttribute("data-approval-state") !== "completed" &&
     buttons(g).some(b => enabled(b) && (label(b) === "允許一次" || label(b) === "Allow once")));
 };
 const locate = () => {
   const cards = candidates();
   if (cards.length > 1) throw Error("AMBIGUOUS_PENDING_CARDS");
   return cards.length ? cards[0] : null;
 };
 const tx = globalThis.__boundedApprovalTx;
 if (action === "inspect") {
   const c = locate();
   if (c) {
     const invocation = c.getAttribute("data-invocation-id") || c.getAttribute("data-tool-call-id");
     if (!invocation) throw Error("MISSING_INVOCATION_ID");
     globalThis.__boundedApprovalTx = {token, card:c, turn, invocation, opText:null, clicked:false};
     return {state:"PENDING", invocation};
   }
   if (!turn) return {state:"NO_PENDING"};
   const controls = buttons(turn).filter(b => enabled(b) && label(b) === "先前活動");
   const unknown = buttons(turn).filter(b => label(b) === "先前活動" && !enabled(b));
   if (controls.length > 1 || unknown.length) throw Error("AMBIGUOUS_OR_DISABLED_ACTIVITY");
   if (controls.length === 1 && controls[0].getAttribute("aria-expanded") === "false") {
     globalThis.__boundedApprovalTx = {token, turn, expansion:controls[0], opText:null, clicked:false};
     return {state:"COLLAPSED"};
   }
   if (controls.length && controls[0].getAttribute("aria-expanded") !== "true")
     throw Error("UNKNOWN_ACTIVITY_STATE");
   return {state:"NO_PENDING"};
 }
 if (!tx || tx.token !== token || !turn || tx.turn !== turn)
   throw Error("TRANSACTION_IDENTITY_CHANGED");
 // Readback intentionally bypasses pre-click pending-card lookup and the
 // clicked guard; it NEVER selects or clicks another node after completion.
 if (action === "readback") {
   if (!tx.clicked) throw Error("READBACK_BEFORE_CLICK");
   const c = tx.card;
   const same = !!c && c.isConnected && turn.contains(c) &&
       tx.invocation === (c.getAttribute("data-invocation-id") || c.getAttribute("data-tool-call-id"));
   if (!same) return {same:false, completed:false, invocation:tx.invocation};
   const noOnce = !buttons(c).some(b => enabled(b) &&
       (label(b) === "允許一次" || label(b) === "Allow once"));
   return {same:true, completed:c.getAttribute("data-approval-state") === "completed" && noOnce,
           invocation:tx.invocation};
 }
 if (tx.clicked) throw Error("TRANSACTION_ALREADY_CLICKED");
 if (action === "expand") {
   if (!tx.expansion || !enabled(tx.expansion) ||
       tx.expansion.getAttribute("aria-expanded") !== "false" ||
       label(tx.expansion) !== "先前活動") throw Error("EXPANSION_STATE_CHANGED");
   tx.expansion.click();
   tx.expansion = null;
   return {expanded:true};
 }
 if (action === "bind") {
   const c = locate();
   if (!c) return {state:"NO_PENDING"};
   const invocation = c.getAttribute("data-invocation-id") || c.getAttribute("data-tool-call-id");
   if (!invocation) throw Error("MISSING_INVOCATION_ID");
   tx.card = c; tx.invocation = invocation; tx.opText = null;
   return {state:"PENDING",invocation};
 }
 const c = tx.card;
 if (!c || !visible(c) || !turn.contains(c) || c !== locate() ||
     tx.invocation !== (c.getAttribute("data-invocation-id") || c.getAttribute("data-tool-call-id")) ||
     c.getAttribute("data-approval-state") === "completed")
   throw Error("CARD_REPLACED_OR_CHANGED");
 const dialog = () => {
   const direct = [...c.querySelectorAll('[role="dialog"]')].filter(visible);
   const detailsButton = buttons(c).find(b => label(b) === "查看詳細資訊");
   const id = detailsButton && detailsButton.getAttribute("aria-controls");
   const linked = id ? document.getElementById(id) : null;
   if (linked && linked.getAttribute("role") === "dialog" && visible(linked)) direct.push(linked);
   return unique([...new Set(direct)]);
 };
 if (action === "details") {
   const b = unique(buttons(c).filter(b => enabled(b) && label(b) === "查看詳細資訊"));
   if (!b) throw Error("CARD_DETAILS_NOT_UNIQUE");
   if (!dialog()) b.click();
   const d = dialog();
   if (!d) throw Error("UNOWNED_DETAILS_DIALOG");
   const body = label(d);
   if (tx.opText !== null && tx.opText !== body) throw Error("OPERATION_CHANGED");
   tx.opText = body;
   return {details:body, invocation:tx.invocation};
 }
 if (action === "close") {
   const d = dialog();
   if (!d) throw Error("DIALOG_MISSING");
   const b = unique(buttons(d).filter(b => enabled(b) &&
          (label(b) === "關閉對話框" || label(b) === "關閉對話方塊" || label(b) === "Close dialog")));
   if (!b) throw Error("DIALOG_CLOSE_NOT_UNIQUE");
   b.click();
   return {closed:true};
 }
 if (action === "click") {
   // Final guard in the SAME evaluate, no page-wide fallback, never a second click.
   const d = dialog();
   if (!d || tx.opText === null || tx.opText !== label(d))
     throw Error("OPERATION_GUARD_FAILED");
   const b = unique(buttons(c).filter(b => enabled(b) &&
                   (label(b) === "允許一次" || label(b) === "Allow once")));
   if (!b) throw Error("ALLOW_ONCE_NOT_UNIQUE");
   tx.clicked = true; b.click();
   return {clicked:true, invocation:tx.invocation};
 }
 throw Error("UNKNOWN_APPROVAL_ACTION");
})()
"""


def evaluate(job, token, action, send=transport):
    source = DOM_JS.replace("__TOKEN__", json.dumps(token)).replace("__ACTION__", json.dumps(action))
    try:
        result = send(job, {"op": "evaluate", "expression": source})
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict):
            raise InfraError("EVALUATE_NOT_OBJECT")
        return result
    except InfraError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise InfraError("EVALUATE_BAD_JSON") from exc
    except Exception as exc:
        # Includes Playwright/bridge JS exceptions, not only subprocess failures.
        # Do not leak CDP arguments, card payloads or raw adapter exception text.
        raise InfraError("EVALUATE_TRANSPORT_OR_JS") from exc


def parse_operation(text):
    if not isinstance(text, str) or text.count("call_tool\n") != 1:
        raise PolicyRejected("OPERATION_COUNT")
    raw = text.split("call_tool\n", 1)[1].lstrip()
    try:
        op, end = json.JSONDecoder().raw_decode(raw)
    except ValueError as exc:
        raise PolicyRejected("MALFORMED_OPERATION") from exc
    if not isinstance(op, dict) or not isinstance(op.get("args"), dict):
        raise PolicyRejected("MALFORMED_OPERATION")
    trailing = raw[end:].strip()
    # Only a card-local dialog's exact close-button label is recognized.
    if trailing not in ("", "關閉對話框", "關閉對話方塊", "Close dialog"):
        raise PolicyRejected("OPERATION_TRAILING_DATA")
    return op


def fingerprint(op, invocation):
    a = op["args"]
    content = a.get("content")
    if not isinstance(content, str):
        raise PolicyRejected("CONTENT_NOT_STRING")
    record = [invocation, op.get("path"), a.get("repository_full_name"),
              a.get("branch"), a.get("path"), hashlib.sha256(content.encode()).hexdigest()]
    return hashlib.sha256(json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def scoped(op, job):
    a = op.get("args", {})
    tool = str(op.get("path", "")).rsplit("/", 1)[-1]
    if tool not in ("update_file", "create_file"):
        raise PolicyRejected("TOOL_OUT_OF_SCOPE")
    if a.get("repository_full_name") != job["repository"] or a.get("branch") != job["branch"]:
        raise PolicyRejected("REPOSITORY_OR_BRANCH")
    if a.get("path") not in job["write_allowlist"]:
        raise PolicyRejected("PATH_OUT_OF_SCOPE")
    c = a.get("content")
    if not isinstance(c, str) or len(c) > 180000:
        raise PolicyRejected("INVALID_CONTENT")
    return tool, a, c


def strict_policy(op, job):
    tool, a, c = scoped(op, job)
    for word in LONG.findall(c):
        ok = SNAKE.fullmatch(word) and sum(ch.isdigit() for ch in word) <= len(word) * .1
        if not ok:
            raise PolicyRejected("TOKEN_LIKE", fallback=True)
    if STRICT_NEVER.search(c):
        raise PolicyRejected("TOKEN_LIKE", fallback=True)
    for match in STRICT_REGEX.finditer(c):
        line = c[c.rfind("\n", 0, match.start()) + 1:c.find("\n", match.end()) if "\n" in c[match.end():] else len(c)]
        if "re.compile" not in line and "SECRET" not in line.upper():
            raise PolicyRejected("TOKEN_LIKE", fallback=True)
    return {"tool":tool, "path":a["path"], "changed_lines":None}


def diff_policy(op, job, max_lines=600, fetch_base=None):
    tool, a, c = scoped(op, job)
    if tool != "update_file":
        raise PolicyRejected("DIFF_REQUIRES_UPDATE")
    if not isinstance(max_lines, int) or not 0 < max_lines <= 600:
        raise PolicyRejected("DIFF_LIMIT_INVALID")
    if fetch_base is None:
        command = ["gh", "api", "-H", "Accept: application/vnd.github.raw",
                   "repos/%s/contents/%s?ref=%s" % (job["repository"], a["path"], job["branch"])]
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InfraError("BASE_FETCH_FAILURE") from exc
        if p.returncode:
            raise InfraError("BASE_FETCH_NONZERO")
        base = p.stdout
    else:
        base = fetch_base(job, a["path"])
    if not isinstance(base, str) or not base:
        raise InfraError("BASE_EMPTY")
    lines = [ln for ln in difflib.unified_diff(base.splitlines(), c.splitlines(), n=0, lineterm="")
             if ln[:1] in "+-" and ln[:3] not in ("+++", "---")]
    if not 0 < len(lines) <= max_lines:
        raise PolicyRejected("DIFF_SIZE")
    for ln in (x[1:] for x in lines if x.startswith("+")):
        if DIFF_SECRET.search(ln):
            raise PolicyRejected("ADDED_SECRET")
        for word in LONG.findall(ln):
            if not re.fullmatch(r"[a-z][a-z0-9_]*", word):
                raise PolicyRejected("ADDED_LONG_NONIDENTIFIER")
    return {"tool":tool, "path":a["path"], "changed_lines":len(lines)}


def journal_path(job_dir):
    return profile_root(job_dir) / "cache" / "approval-transaction-ledger.json"


def read_journal(path):
    if not path.exists():
        return {"version": 1, "attempts": {}}
    try:
        data = load_json(path)
    except (OSError, ValueError) as exc:
        raise InfraError("APPROVAL_JOURNAL_UNREADABLE") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("attempts"), dict):
        raise InfraError("APPROVAL_JOURNAL_INVALID")
    return data


def save_journal(path, value):
    """Durable replacement before click. Never store dialog contents."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=path.parent, prefix=".approval-", delete=False) as out:
            temp = Path(out.name)
            json.dump(value, out, sort_keys=True, separators=(",", ":"))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        temp = None
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def identity_key(chat, invocation):
    return hashlib.sha256(json.dumps([chat, invocation],
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def approve(job_dir, mode="auto", max_lines=600, *, send=transport, fetch_base=None,
            clock=utcnow, lock_timeout=8):
    """One serialized inspect -> expand -> details -> policy -> reread -> click -> readback.

    No approval retries even if the click acknowledgement or readback is lost.
    """
    path = Path(job_dir).resolve()
    lock_path = profile_root(path) / "cache" / "approval-transaction.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        end = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    return {"status":"INFRA", "reason":"LOCK_BUSY"}
                time.sleep(.05)
        token = secrets.token_hex(16)
        clicked = False
        try:
            j, ident = admission(path, clock=clock)

            def check():
                newest, _ = admission(path, expected=ident, clock=clock)
                if snapshot(newest, send)["url"] != ident[2]:
                    raise AdmissionError("CHAT_NAVIGATED")
                return newest

            j = check()  # before even inspecting candidate DOM
            state = evaluate(j, token, "inspect", send)
            if state.get("state") == "COLLAPSED":
                j = check()
                evaluate(j, token, "expand", send)
                j = check()
                state = evaluate(j, token, "bind", send)
            if state.get("state") == "NO_PENDING":
                return {"status":"NO_PENDING"}
            if state.get("state") != "PENDING":
                raise InfraError("CARD_STATE_INVALID")
            j = check()
            d1 = evaluate(j, token, "details", send)
            j = check()
            op1 = parse_operation(d1.get("details"))
            fp1 = fingerprint(op1, state["invocation"])
            ledger_file = journal_path(path)
            ledger = read_journal(ledger_file)
            key = identity_key(ident[2], state["invocation"])
            if key in ledger["attempts"]:
                return {"status":"UNKNOWN", "reason":"PRIOR_APPROVAL_ATTEMPT",
                        "fingerprint":fp1}
            if mode == "strict":
                audit = strict_policy(op1, j)
            elif mode == "diff":
                audit = diff_policy(op1, j, max_lines, fetch_base)
            elif mode == "auto":
                try:
                    audit = strict_policy(op1, j)
                except PolicyRejected as exc:
                    if not exc.fallback:
                        raise
                    audit = diff_policy(op1, j, max_lines, fetch_base)
            else:
                raise PolicyRejected("UNSUPPORTED_MODE")
            j = check()
            d2 = evaluate(j, token, "details", send)
            op2 = parse_operation(d2.get("details"))
            if d2.get("invocation") != state["invocation"] or fingerprint(op2, state["invocation"]) != fp1:
                raise PolicyRejected("FINGERPRINT_CHANGED")
            j = check()  # exact identity, expiry, dispatch and URL immediately before click
            # Re-read durable state under the same process-wide lease.
            ledger = read_journal(ledger_file)
            if key in ledger["attempts"]:
                return {"status":"UNKNOWN", "reason":"PRIOR_APPROVAL_ATTEMPT",
                        "fingerprint":fp1}
            ledger["attempts"][key] = {"fingerprint":fp1, "status":"CLICK_INTENT_UNRESOLVED",
                                      "instruction_id":ident[1],
                                      "at":clock().isoformat()}
            save_journal(ledger_file, ledger)
            pd.approval_once(j, path, ident[1], fp1,
                             chat=j.get("dispatch_chat_url"))
            pd.approve_check(j, path, ident[1], fp1,
                             chat=j.get("dispatch_chat_url"))
            clicked = True  # fail closed even when acknowledge is lost; no retry
            ack = evaluate(j, token, "click", send)
            if ack.get("clicked") is not True or ack.get("invocation") != state["invocation"]:
                return {"status":"UNKNOWN", "reason":"CLICK_ACK_UNVERIFIED", "fingerprint":fp1}
            j = check()
            after = evaluate(j, token, "readback", send)
            if not (after.get("same") and after.get("completed") is True and
                    after.get("invocation") == state["invocation"]):
                return {"status":"UNKNOWN", "reason":"SAME_CARD_READBACK_UNVERIFIED",
                        "fingerprint":fp1}
            ledger = read_journal(ledger_file)
            pd.reconcile(j, path, ident[1], receipt="APPROVED",
                         chat=j.get("dispatch_chat_url"))
            ledger["attempts"][key]["status"] = "READBACK_PROVEN"
            save_journal(ledger_file, ledger)
            return {"status":"APPROVED", "fingerprint":fp1, **audit}
        except AdmissionError as exc:
            return {"status":"REJECTED", "reason":str(exc)}
        except PolicyRejected as exc:
            return {"status":"REJECTED", "reason":str(exc)}
        except (InfraError, OSError, RuntimeError, KeyError, TypeError, ValueError) as exc:
            # A post-click transport/DOM failure is NEVER interpreted as success.
            return {"status":"UNKNOWN" if clicked else "INFRA",
                    "reason":type(exc).__name__ if clicked else str(exc)[:120]}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
