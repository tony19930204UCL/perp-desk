#!/usr/bin/env python3
"""Instruction carrier (Role A) for ChatGPT engineering channel.

Carrier ONLY delivers and reconciles exact user instruction bubbles.
Sent state is strictly delivery receipt (SENT_CONFIRMED), never work completion.
Zero scanning of assistant DONE tokens, stable body text or generation completion.
"""
from __future__ import annotations
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import product_dispatch as pd
from typing import Any, Callable, Dict, List, Optional, Set

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def normalize_composer(text: str) -> str:
    """Normalize ProseMirror composer whitespace without losing non-newline characters."""
    return re.sub(r'\n{2,}', '\n', text.replace('\r', '')).strip()

def normalize_rendered_bubble(text: str) -> str:
    """Normalize link renderer line breaks immediately before a URL."""
    return re.sub(r'[ \t]*\n[ \t]*(?=https?://)', ' ', normalize_composer(text))

def delivered_prompt(text: str, s: Dict[str, Any], chat_path: str) -> bool:
    """Verify that exact rendered user bubble exists in target chat."""
    if chat_path == '/':  # new-conversation mode: server assigns /c/<id> after first send
        if not re.fullmatch(r'https://chatgpt\.com/c/[A-Za-z0-9-]+', s.get('url') or ''):
            return False
    elif s.get('url') != 'https://chatgpt.com' + chat_path:
        return False
    messages = s.get('user_messages')
    if not isinstance(messages, list):
        return False
    target = normalize_rendered_bubble(text)
    return any(normalize_rendered_bubble(m) == target for m in messages)

def approve_operation(
    op: Dict[str, Any],
    repo: str,
    branch: str,
    mailbox: str,
    allowed: Set[str],
    pr_number: int
) -> bool:
    """Gate tool operations strictly within repository and PR file allowlist."""
    args = op.get('args', {})
    tool = op.get('path', '').rsplit('/', 1)[-1]
    if args.get('repository_full_name') != repo:
        return False
    if tool == 'update_file' or (tool == 'create_file' and args.get('path') == mailbox):
        content = args.get('content', '')
        return (
            args.get('branch') == branch
            and args.get('path') in allowed
            and isinstance(content, str)
            and len(content) <= 180000
            and not re.search(r'(?i)(?:sk-[a-z0-9]{20,}|ghp_[a-z0-9]{20,}|PRIVATE KEY|api[_-]?key\s*[=:]\s*[\"\'][a-z0-9]{20,})', content)
        )
    if tool in ('create_issue_comment', 'add_issue_comment'):
        return (
            args.get('issue_number', args.get('issue')) == pr_number
            and len(args.get('body', '')) <= 20000
        )
    return False

def recover_browser(
    error: Any,
    root: Path,
    browser_recovery_cmd: str,
    chat_path: str,
    run_fn: Callable[[List[str], int], str],
    save_fn: Callable[[str, Any], None],
    event_fn: Callable[..., None]
) -> bool:
    error_str = str(error)
    missing = any(x in error_str for x in ('Dedicated ChatGPT target count=0', 'No MSFT_NetTCPConnection objects', 'Missing CDP listener', 'CmdletizationQuery_NotFound,Get-NetTCPConnection'))
    hung = any(x in error_str for x in ('OperationCanceledException', 'No matching response', 'timed out'))
    if not (missing or hung):
        return False
    marker = root / 'browser_recovery.json'
    if marker.exists():
        previous = json.loads(marker.read_text())
        try:
            previous_at = dt.datetime.fromisoformat(previous['at'])
        except (KeyError, TypeError, ValueError):
            archived = root / ('browser_recovery_legacy_' + str(__import__('time').time_ns()) + '.json')
            marker.replace(archived)
            event_fn('LEGACY_BROWSER_SNAPSHOT_PRESERVED', path=str(archived))
        else:
            if previous_at.tzinfo is None:
                raise RuntimeError('BROWSER_RECOVERY_TIMESTAMP_UNVERIFIED')
            age = (dt.datetime.now(dt.timezone.utc) - previous_at).total_seconds()
            status = previous.get('status')
            if status in ('RECOVERY_INTENT', 'RECOVERY_FAILED') and age < 60:
                raise RuntimeError('BROWSER_RECOVERY_RETRY_BACKOFF')
            if status == 'TARGET_RECOVERED_AWAIT_NEXT_TICK' and hung and age < 600:
                raise RuntimeError('BROWSER_RECOVERY_COOLDOWN')
    save_fn(marker.name, {'at': now(), 'status': 'RECOVERY_INTENT', 'mode': 'restart_hung' if hung else 'open_missing'})
    command = ['powershell.exe', '-NoProfile', '-File', browser_recovery_cmd, '-ChatUrl', 'https://chatgpt.com' + chat_path]
    if hung:
        command.append('-RestartHung')
    try:
        receipt = json.loads(run_fn(command, 45))
    except Exception as exc:
        save_fn(marker.name, {'at': now(), 'status': 'RECOVERY_FAILED', 'error': str(exc)[:800], 'retry_after_seconds': 60})
        event_fn('BROWSER_RECOVERY_FAILED', error=str(exc)[:800])
        raise
    save_fn(marker.name, {'at': now(), 'status': 'TARGET_RECOVERED_AWAIT_NEXT_TICK', 'receipt': receipt})
    event_fn('BROWSER_TARGET_RECOVERED', mode='restart_hung' if hung else 'open_missing')
    return True

def reconcile_send(
    key: str,
    s: Dict[str, Any],
    text: Optional[str] = None,
    root: Optional[Path] = None,
    chat_path: Optional[str] = None,
    save_fn: Optional[Callable[[str, Any], None]] = None,
    event_fn: Optional[Callable[..., None]] = None
) -> bool:
    """Reconcile delivery status from exact user message bubbles without replaying actions."""
    if root is None or chat_path is None or save_fn is None:
        return False
    intent = root / ('send_' + key + '.json')
    if not intent.exists():
        return False
    prior = json.loads(intent.read_text())
    if prior.get('status') == 'CONFIRMED':
        return True
    payload = text if text is not None else prior.get('prompt')
    if not isinstance(payload, str):
        return False
    expected = prior.get('text_hash', prior.get('hash'))
    if not expected:
        return False
    if expected != hashlib.sha256(payload.encode()).hexdigest():
        raise RuntimeError('SEND_PAYLOAD_CHANGED')
    if not delivered_prompt(payload, s, chat_path):
        return False
    updated = {
        **prior,
        'status': 'CONFIRMED',
        'confirmed_at': now(),
        'chat_path': chat_path,
        'evidence_kind': 'EXACT_RENDERED_USER_BUBBLE',
        'prior_status': prior.get('status'),
        'normalized_text_hash': hashlib.sha256(normalize_composer(payload).encode()).hexdigest()
    }
    save_fn(intent.name, updated)
    if event_fn:
        event_fn('PROMPT_RECONCILED_NO_REPLAY', key=key, prior_status=prior.get('status'))
    return True

def clear_confirmed_duplicate(
    text: str,
    s: Dict[str, Any],
    root: Path,
    chat_path: str,
    cdp_fn: Callable[[str], Any],
    snapshot_fn: Callable[[], Dict[str, Any]],
    event_fn: Optional[Callable[..., None]] = None
) -> bool:
    """Clear composer residue only if confirmed owned rendered user message exists."""
    if s.get('busy') or not delivered_prompt(text, s, chat_path) or normalize_composer(s.get('composer_text', '')) != normalize_composer(text):
        return False
    raw_hash = hashlib.sha256(text.encode()).hexdigest()
    owned_hashes = {raw_hash, hashlib.sha256(normalize_composer(text).encode()).hexdigest()}
    owned = any(
        json.loads(p.read_text()).get('status') == 'CONFIRMED'
        and (json.loads(p.read_text()).get('text_hash', json.loads(p.read_text()).get('hash')) in owned_hashes
             or json.loads(p.read_text()).get('normalized_text_hash') in owned_hashes)
        for p in root.glob('send_*.json')
    )
    if not owned:
        raise RuntimeError('DUPLICATE_DRAFT_NOT_OWNED')
    cdp_fn('''(()=>{if(location.pathname!==%s)throw Error('Wrong chat');const norm=t=>t.replace(/\\r/g,'').replace(/\\n{2,}/g,'\\n').trim();const text=%s;const users=[...document.querySelectorAll('[data-user-message-bubble] .whitespace-pre-wrap,[data-message-author-role="user"] .whitespace-pre-wrap')];if(!users.some(e=>norm(e.innerText)===norm(text)))throw Error('Delivered user bubble missing');const es=[...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"]')].filter(e=>e.getClientRects().length);if(es.length!==1||norm(es[0].innerText)!==norm(text))throw Error('Foreign draft');es[0].focus();document.execCommand('selectAll',false,null);document.execCommand('delete',false,null);return !es[0].innerText.trim()})()''' % (json.dumps(chat_path), json.dumps(text)))
    after = snapshot_fn()
    if after.get('composer_text', '').strip() or not delivered_prompt(text, after, chat_path):
        raise RuntimeError('DUPLICATE_DRAFT_CLEAR_UNVERIFIED')
    if event_fn:
        event_fn('CONFIRMED_OWNED_DUPLICATE_DRAFT_CLEARED', text_hash=raw_hash)
    return True

def prepare_composer(
    s: Dict[str, Any],
    root: Path,
    chat_path: str,
    reconcile_fn: Callable[..., bool],
    clear_fn: Callable[..., bool],
    snapshot_fn: Callable[[], Dict[str, Any]]
) -> Dict[str, Any]:
    draft = s.get('composer_text', '')
    if s.get('busy') or not draft.strip() or not delivered_prompt(draft, s, chat_path):
        return s
    hashes = {hashlib.sha256(draft.encode()).hexdigest(), hashlib.sha256(normalize_composer(draft).encode()).hexdigest()}
    for p in root.glob('send_*.json'):
        receipt = json.loads(p.read_text())
        expected = receipt.get('text_hash', receipt.get('hash'))
        matches = expected in hashes or receipt.get('normalized_text_hash') in hashes
        if matches and receipt.get('status') in ('SEND_UNKNOWN_NO_RETRY', 'SEND_INTENT', 'COMPOSER_INTENT'):
            payload = next((x for x in (draft, normalize_composer(draft)) if hashlib.sha256(x.encode()).hexdigest() == expected), None)
            if payload is not None:
                reconcile_fn(p.stem[len('send_'):], s, payload)
                receipt = json.loads(p.read_text())
        if matches and receipt.get('status') == 'CONFIRMED':
            clear_fn(draft, s)
            return snapshot_fn()
    return s

def send(
    text: str,
    key: str,
    s: Dict[str, Any],
    *,
    root: Path,
    chat_path: str,
    cdp_fn: Callable[[str], Any],
    snapshot_fn: Callable[[], Dict[str, Any]],
    save_fn: Callable[[str, Any], None],
    event_fn: Optional[Callable[..., None]] = None,
    valid_until: Optional[str] = None,
    dispatch_job: Optional[Dict[str, Any]] = None
) -> None:
    """Deliver instruction to chat and verify rendered user message bubble.
    
    Delivery receipt ONLY: CONFIRMED in send_{key}.json. Never marks task completion.
    Expired send authorization strictly blocks send action without extending authority.
    """
    if valid_until is not None:
        deadline = dt.datetime.fromisoformat(valid_until)
        if dt.datetime.now(dt.timezone.utc) >= deadline:
            raise RuntimeError('AUTHORIZATION_EXPIRED')

    if dispatch_job is None:
        raise RuntimeError("DISPATCH_JOB_REQUIRED")
    pd.check(dispatch_job, root, key, phase="prepare",
             chat=dispatch_job.get("dispatch_chat_url"))
    s = prepare_composer(
        s, root, chat_path,
        reconcile_fn=lambda k, snap, t: reconcile_send(k, snap, t, root=root, chat_path=chat_path, save_fn=save_fn, event_fn=event_fn),
        clear_fn=lambda draft, snap: clear_confirmed_duplicate(draft, snap, root=root, chat_path=chat_path, cdp_fn=cdp_fn, snapshot_fn=snapshot_fn, event_fn=event_fn),
        snapshot_fn=snapshot_fn
    )
    intent = root / ('send_' + key + '.json')
    need_fill = True
    if reconcile_send(key, s, text, root=root, chat_path=chat_path, save_fn=save_fn, event_fn=event_fn):
        return
    if s.get('busy'):
        return
    if intent.exists():
        prior = json.loads(intent.read_text())
        if prior.get('status') not in ('COMPOSER_INTENT', 'SEND_INTENT'):
            return
        if prior.get('text_hash') and prior['text_hash'] != hashlib.sha256(text.encode()).hexdigest():
            raise RuntimeError('SEND_PAYLOAD_CHANGED')
        pending = cdp_fn('(()=>{if(location.pathname!==' + json.dumps(chat_path) + ')throw Error("Wrong chat");const e=[...document.querySelectorAll(\'#prompt-textarea,[contenteditable="true"][role="textbox"]\')].filter(e=>e.getClientRects().length);return e.length===1?e[0].innerText:null})()')
        if not isinstance(pending, str):
            raise RuntimeError('SEND_INTENT_RECONCILIATION_REQUIRED')
        if normalize_composer(pending) == normalize_composer(text):
            need_fill = False
        elif pending.strip() or prior.get('status') != 'COMPOSER_INTENT':
            raise RuntimeError('SEND_INTENT_RECONCILIATION_REQUIRED')
    else:
        save_fn(intent.name, {'at': now(), 'status': 'COMPOSER_INTENT', 'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'prompt': text})

    if need_fill:
        pd.check(dispatch_job, root, key, phase="prepare",
                 chat=dispatch_job.get("dispatch_chat_url"))
        js = '''(()=>{if(location.pathname!==%s)throw Error('Wrong chat');const candidates=[...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"]')].filter(e=>e.getClientRects().length);if(candidates.length!==1)throw Error('Composer missing or ambiguous');const e=candidates[0];if(e.innerText.trim())throw Error('Composer occupied');e.focus();document.execCommand('insertText',false,%s);const norm=t=>t.replace(/\\r/g,'').replace(/\\n{2,}/g,'\\n').trim();if(norm(e.innerText)!==norm(%s))throw Error('Composer mismatch');return true})()''' % (json.dumps(chat_path), json.dumps(text), json.dumps(text))
        cdp_fn(js)

    pd.phase(dispatch_job, root, key, 'SEND_INTENT')
    save_fn(intent.name, {'at': now(), 'status': 'SEND_INTENT', 'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'prompt': text})
    save_fn(intent.name, {'at': now(), 'status': 'SEND_UNKNOWN_NO_RETRY', 'text_hash': hashlib.sha256(text.encode()).hexdigest(), 'prompt': text})

    pd.phase(dispatch_job, root, key, 'SEND_UNKNOWN')
    cdp_fn('(()=>{const b=[...document.querySelectorAll("button[data-testid=send-button],button[aria-label=傳送提示詞],button[aria-label=傳送訊息],button[aria-label=傳送]")].filter(e=>e.getClientRects().length);if(b.length!==1||b[0].disabled)throw Error("Send button unavailable");b[0].click();return true})()')
    after = snapshot_fn()
    if chat_path == '/':  # URL changes asynchronously after first message
        import time
        for _ in range(30):
            if delivered_prompt(text, after, chat_path) and not after.get('composer_text', '').strip():
                break
            time.sleep(1); after = snapshot_fn()
    confirmed = delivered_prompt(text, after, chat_path) and not after.get('composer_text', '').strip()
    save_fn(intent.name, {
        'at': now(),
        'status': 'CONFIRMED' if confirmed else 'SEND_UNKNOWN_NO_RETRY',
        'text_hash': hashlib.sha256(text.encode()).hexdigest(),
        'prompt': text,
        'chat_path': chat_path,
        'chat_url': after.get('url') if confirmed else None,
        'instruction_id': key,
        'evidence_kind': 'EXACT_RENDERED_USER_BUBBLE' if confirmed else 'UNVERIFIED'
    })
    if confirmed:
        pd.phase(dispatch_job, root, key, 'SENT_CONFIRMED')
    if event_fn:
        event_fn('PROMPT_READBACK', key=key, status=json.loads(intent.read_text())['status'])

def permission(
    s: Dict[str, Any],
    *,
    cdp_fn: Callable[[str], Any],
    snapshot_fn: Callable[[], Dict[str, Any]],
    repo: str,
    branch: str,
    mailbox: str,
    allowed: Set[str],
    pr_number: int,
    save_fn: Callable[[str, Any], None],
    event_fn: Callable[..., None],
    valid_until: Optional[str] = None,
    dispatch_job: Optional[Dict[str, Any]] = None,
    dispatch_dir: Optional[Path] = None,
    instruction_id: Optional[str] = None
) -> bool:
    """Validate and approve GitHub tool usage within strictly scoped permissions."""
    if '要允許 ChatGPT 使用 GitHub 嗎？' not in s.get('text', ''):
        return False
    if valid_until is not None:
        deadline = dt.datetime.fromisoformat(valid_until)
        if dt.datetime.now(dt.timezone.utc) >= deadline:
            raise RuntimeError('AUTHORIZATION_EXPIRED')

    cdp_fn('(()=>{const b=[...document.querySelectorAll("button")].filter(e=>e.innerText==="查看詳細資訊");if(b.length!==1)throw Error("Ambiguous details");b[0].click();return true})()')
    details = cdp_fn('JSON.stringify([...document.querySelectorAll("[role=dialog]")].map(e=>e.innerText))')
    ops = []
    for text in details:
        if 'call_tool\n' in text:
            try:
                ops.append(json.JSONDecoder().raw_decode(text.split('call_tool\n', 1)[1].lstrip())[0])
            except ValueError:
                pass
    if len(ops) != 1 or not approve_operation(ops[0], repo, branch, mailbox, allowed, pr_number):
        save_fn('permission_scope_failure_latest.json', {
            'at': now(),
            'dialog_count': len(details),
            'parsed_operation_count': len(ops),
            'call_tool_visible': any('call_tool' in text for text in details),
            'operations': [{
                'tool': op.get('path', '').rsplit('/', 1)[-1],
                'repository': op.get('args', {}).get('repository_full_name'),
                'branch': op.get('args', {}).get('branch'),
                'file': op.get('args', {}).get('path'),
                'approved': approve_operation(op, repo, branch, mailbox, allowed, pr_number)
            } for op in ops]
        })
        cdp_fn('(()=>{const b=[...document.querySelectorAll("button")].find(e=>e.innerText==="關閉對話框");if(b)b.click();return true})()')
        raise RuntimeError('PERMISSION_SCOPE_BLOCKED')

    op = ops[0]
    if dispatch_job is None or dispatch_dir is None or instruction_id is None:
        raise RuntimeError("DISPATCH_DELEGATION_REQUIRED")
    fingerprint = hashlib.sha256(json.dumps(op, sort_keys=True).encode()).hexdigest()
    pd.approval_once(dispatch_job, dispatch_dir, instruction_id, fingerprint,
                     chat=dispatch_job.get("dispatch_chat_url"))
    event_fn('APPROVAL_INTENT', operation=op['path'].rsplit('/', 1)[-1], file=op['args'].get('path'), content_hash=hashlib.sha256(op['args'].get('content', '').encode()).hexdigest())
    cdp_fn('(()=>{const b=[...document.querySelectorAll("button")].find(e=>e.innerText==="關閉對話框");if(b)b.click();return true})()')
    pd.approve_check(dispatch_job, dispatch_dir, instruction_id, fingerprint,
                     chat=dispatch_job.get("dispatch_chat_url"))
    cdp_fn('(()=>{const b=[...document.querySelectorAll("button")].filter(e=>e.innerText.startsWith("允許一次"));if(b.length!==1)throw Error("Ambiguous approval");b[0].click();return true})()')
    after = snapshot_fn()
    event_fn('APPROVAL_READBACK', prompt_gone='要允許 ChatGPT 使用 GitHub 嗎？' not in after.get('text', ''))
    return True

class InstructionCarrier:
    """Encapsulation for Role A instruction carrier transport."""

    def __init__(
        self,
        root: Path,
        chat_path: str,
        repo: str,
        branch: str,
        mailbox: str,
        allowed: Set[str],
        pr_number: int,
        cdp_fn: Callable[[str], Any],
        snapshot_fn: Callable[[], Dict[str, Any]],
        save_fn: Callable[[str, Any], None],
        event_fn: Callable[..., None],
        valid_until: Optional[str] = None,
        dispatch_job: Optional[Dict[str, Any]] = None
    ):
        self.root = Path(root)
        self.chat_path = chat_path
        self.repo = repo
        self.branch = branch
        self.mailbox = mailbox
        self.allowed = allowed
        self.pr_number = pr_number
        self.cdp = cdp_fn
        self.snapshot = snapshot_fn
        self.save = save_fn
        self.event = event_fn
        self.valid_until = valid_until
        self.dispatch_job = dispatch_job

    def send_instruction(self, text: str, key: str, snapshot: Dict[str, Any]) -> None:
        send(
            text, key, snapshot,
            root=self.root,
            chat_path=self.chat_path,
            cdp_fn=self.cdp,
            snapshot_fn=self.snapshot,
            save_fn=self.save,
            event_fn=self.event,
            valid_until=self.valid_until,
            dispatch_job=self.dispatch_job
        )

    def reconcile_pending(self, snapshot: Dict[str, Any]) -> None:
        for pending in self.root.glob('send_*.json'):
            prior = json.loads(pending.read_text())
            if prior.get('status') != 'CONFIRMED' and isinstance(prior.get('prompt'), str):
                reconcile_send(
                    pending.stem[len('send_'):],
                    snapshot,
                    root=self.root,
                    chat_path=self.chat_path,
                    save_fn=self.save,
                    event_fn=self.event
                )
