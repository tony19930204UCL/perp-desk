from __future__ import annotations
import json, hashlib
import datetime as dt
from pathlib import Path
from datetime import datetime, timezone
VALID = {'REVISE', 'ACCEPTED_SCOPE', 'BLOCKED'}
STATES = {'DONE', 'ACTIONABLE_NOW', 'EXTERNAL_BLOCKER', 'FUTURE_EVENT'}
MainHandbackError = ValueError

def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)

def folder_for(root, head, blob):
    if len(head) != 40 or any(c not in '0123456789abcdef' for c in head):
        raise ValueError('EXACT_HEAD_REQUIRED')
    if not blob or any(c not in '0123456789abcdef' for c in blob):
        raise ValueError('MAILBOX_BLOB_REQUIRED')
    return Path(root) / 'reviews' / (head + '-cio-' + blob)

def validate_result(result, head, blob):
    if result.get('head_sha') != head or result.get('mailbox_blob') != blob:
        raise ValueError('STALE_OR_MISMATCHED_MAIN_VERDICT')
    if result.get('review_kind') != 'CURRENT_CIO_HANDBACK':
        raise ValueError('CURRENT_CIO_VERDICT_REQUIRED')
    if result.get('status') not in VALID:
        raise ValueError('REVIEW_STATUS_INVALID')
    paths = result.get('evidence_paths')
    if not isinstance(paths, list) or not paths:
        raise ValueError('MAIN_REVIEW_EVIDENCE_MISSING')
    for p in paths:
        if not isinstance(p, str) or not Path(p).is_absolute() or not Path(p).is_file():
            raise ValueError('MAIN_REVIEW_EVIDENCE_MISSING')
        if result.get('evidence_hashes', {}).get(p) != hashlib.sha256(Path(p).read_bytes()).hexdigest():
            raise ValueError('MAIN_REVIEW_EVIDENCE_CHANGED')
    if not result.get('reason'):
        raise ValueError('MAIN_REASON_REQUIRED')
    rows = result.get('remaining_work')
    if not isinstance(rows, list):
        raise ValueError('REMAINING_WORK_STATUS_REQUIRED')
    for row in rows:
        if not isinstance(row, dict) or row.get('status') not in STATES:
            raise ValueError('REMAINING_WORK_STATUS_REQUIRED')
        if row['status'] in {'EXTERNAL_BLOCKER', 'FUTURE_EVENT'}:
            evidence = row.get('evidence_paths')
            if not row.get('release_condition') or not evidence or not all(Path(p).is_absolute() and Path(p).is_file() for p in evidence):
                raise ValueError('REMAINING_WORK_RELEASE_EVIDENCE_REQUIRED')
    if (result['status']=='REVISE' or result['status']=='ACCEPTED_SCOPE' and any(row['status']=='ACTIONABLE_NOW' for row in rows)) and not result.get('next_instruction', '').strip():
        raise ValueError('REVISION_INSTRUCTION_REQUIRED' if result['status']=='REVISE' else 'ACCEPTANCE_NEXT_ACTION_REQUIRED')
    if result['status']=='BLOCKED' and any(row['status']=='ACTIONABLE_NOW' for row in rows):
        raise ValueError('ACTIONABLE_WORK_CANNOT_STOP')
    return result

def record_verdict(root, head, blob, *, status, reason, evidence_paths,
                   remaining_work, next_instruction='', source_review=None):
    folder = folder_for(root, head, blob)
    request = json.loads((folder / 'request.json').read_text())
    if request['head_sha'] != head or request['mailbox_blob'] != blob:
        raise ValueError('REQUEST_MISMATCH')
    result = {'at': stamp(), 'head_sha': head, 'mailbox_blob': blob,
        'review_kind': 'CURRENT_CIO_HANDBACK', 'owner': 'Main CIO',
        'status': status, 'reason': reason, 'evidence_paths': list(evidence_paths),
        'evidence_hashes': {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in evidence_paths},
        'remaining_work': remaining_work, 'next_instruction': next_instruction,
        'source_review': source_review, 'inference_dispatched': False,
        'whole_system_completion_claimed': False}
    validate_result(result, head, blob)
    existing = folder / 'result.json'
    if existing.exists():
        old = json.loads(existing.read_text())
        if old != result:
            raise ValueError('IMMUTABLE_MAIN_VERDICT_EXISTS')
        return old
    atomic(existing, result)
    return result
