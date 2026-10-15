#!/usr/bin/env python3
"""Independent GitHub observer (Role B) for PR28 mailbox.

B only observes exact-head GitHub mailbox without browser/test/verdict access.
Reads PR HEAD -> mailbox at pinned HEAD -> PR recheck and reports changed blob+HEAD.
Never merges, never executes remote instructions, never claims acceptance.
"""
from __future__ import annotations
import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from engineering_handback_contract import (
    validate_handback_payload,
)

def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def atomic_save(path: Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(p)

class GitHubInboxObserver:
    """Read-only GitHub mailbox observer pinned to exact PR commit."""
    
    def __init__(
        self,
        repository: str,
        pr_number: int,
        branch: str,
        mailbox_path: str,
        gh_fn: Callable[[str], Any],
        state_dir: Optional[Path] = None
    ):
        self.repo = repository
        self.pr_number = pr_number
        self.branch = branch
        self.mailbox_path = mailbox_path
        self.gh = gh_fn
        self.state_dir = Path(state_dir) if state_dir else None

    def observe(
        self,
        *,
        expected_task_id: Optional[str] = None,
        expected_instruction_id: Optional[str] = None,
        prior_head: Optional[str] = None,
        prior_blob: Optional[str] = None,
        max_rechecks: int = 3
    ) -> Dict[str, Any]:
        """Observe mailbox at exact commit with PR HEAD recheck."""
        now = stamp()
        
        # 1. Fetch initial PR status
        pr = self.gh(f"repos/{self.repo}/pulls/{self.pr_number}")
        if pr.get('state') != 'open' or pr.get('merged') is True:
            return {
                'observed_at': now,
                'status': 'PR_CLOSED_OR_MERGED',
                'head_sha': pr.get('head', {}).get('sha'),
                'error': 'PR is not open'
            }
        if pr.get('head', {}).get('ref') != self.branch or pr.get('head', {}).get('repo', {}).get('full_name') != self.repo:
            return {
                'observed_at': now,
                'status': 'PR_BOUNDARY_CHANGED',
                'head_sha': pr.get('head', {}).get('sha'),
                'error': 'PR branch or repository boundary changed'
            }
            
        head1 = pr['head']['sha']
        
        # 2. Loop to read mailbox and verify pinned commit hasn't moved
        attempts = 0
        current_head = head1
        mailbox_data = None
        blob_sha = None
        head_moved = False
        
        while attempts < max_rechecks:
            attempts += 1
            # Fetch mailbox contents at current_head
            try:
                content_resp = self.gh(f"repos/{self.repo}/contents/{self.mailbox_path}?ref={current_head}")
                blob_sha = content_resp.get('sha')
                raw_bytes = base64.b64decode(content_resp.get('content', ''))
                try:
                    mailbox_data = json.loads(raw_bytes.decode('utf-8'))
                except Exception as decode_err:
                    return {
                        'observed_at': now,
                        'status': 'MAILBOX_MALFORMED',
                        'head_sha': current_head,
                        'blob_sha': blob_sha,
                        'error': f"JSON decode error: {decode_err}"
                    }
            except Exception as e:
                err_str = str(e)
                if '404' in err_str:
                    return {
                        'observed_at': now,
                        'status': 'MAILBOX_NOT_FOUND',
                        'head_sha': current_head,
                        'blob_sha': None
                    }
                raise
                
            # PR recheck to confirm HEAD did not move during read
            pr_check = self.gh(f"repos/{self.repo}/pulls/{self.pr_number}")
            check_head = pr_check.get('head', {}).get('sha')
            if check_head == current_head:
                # Pinned commit verified
                break
            else:
                # HEAD moved during read; retry pinned read at new HEAD
                head_moved = True
                current_head = check_head
        else:
            return {
                'observed_at': now,
                'status': 'HEAD_MOVED_UNSTABLE',
                'head_sha': current_head,
                'prior_observed_head': head1,
                'error': f"PR HEAD moved repeatedly across {max_rechecks} attempts"
            }

        # 3. Check if unchanged from prior observation
        if blob_sha == prior_blob and current_head == prior_head:
            return {
                'observed_at': now,
                'status': 'MAILBOX_UNCHANGED',
                'head_sha': current_head,
                'blob_sha': blob_sha,
                'payload': mailbox_data
            }

        # 4. Correlate schema and task/instruction identity
        try:
            validated = validate_handback_payload(
                mailbox_data,
                expected_task_id=expected_task_id,
                expected_instruction_id=expected_instruction_id,
                repo=self.repo,
                pr_number=self.pr_number,
                branch=self.branch,
                allow_legacy_v1=False
            )
            report = {
                'observed_at': now,
                'status': 'HANDBACK_OBSERVED',
                'head_sha': current_head,
                'blob_sha': blob_sha,
                'payload': validated,
                'head_moved_reconciled': head_moved,
                'correlation': {
                    'task_id': validated.get('task_id'),
                    'instruction_id': validated.get('instruction_id'),
                    'status': validated.get('status'),
                    'source_head_sha': validated.get('source_head_sha')
                }
            }
        except ValueError as val_err:
            # Missing, malformed, stale or wrong-task mailbox is incomplete handback
            report = {
                'observed_at': now,
                'status': 'INCOMPLETE_HANDBACK',
                'head_sha': current_head,
                'blob_sha': blob_sha,
                'payload': mailbox_data,
                'validation_error': str(val_err)
            }

        # 5. Persist durable state without dropping unconsumed handbacks
        if self.state_dir:
            self._record_observation(report)
            
        return report

    def _record_observation(self, report: Dict[str, Any]) -> None:
        """Durable record preserving unconsumed handback."""
        obs_file = self.state_dir / 'github_inbox_latest.json'
        
        # Check if there is an unconsumed handback
        if obs_file.exists():
            try:
                prior = json.loads(obs_file.read_text())
                if prior.get('status') == 'HANDBACK_OBSERVED' and not prior.get('consumed_by_main'):
                    # Save prior unconsumed handback to backlog/history so it is not dropped
                    unconsumed_log = self.state_dir / 'unconsumed_handbacks.jsonl'
                    with unconsumed_log.open('a') as f:
                        f.write(json.dumps(prior, ensure_ascii=False) + '\n')
            except Exception:
                pass

        atomic_save(obs_file, report)
        events_file = self.state_dir / 'github_inbox_events.jsonl'
        with events_file.open('a') as f:
            f.write(json.dumps({
                'at': report['observed_at'],
                'status': report['status'],
                'head_sha': report.get('head_sha'),
                'blob_sha': report.get('blob_sha')
            }, ensure_ascii=False) + '\n')
