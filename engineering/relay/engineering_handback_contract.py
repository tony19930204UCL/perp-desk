#!/usr/bin/env python3
"""Engineering handback contract (schema v2) and prompt construction.

Source vs Live Completion Distinction:
- Source completion: ChatGPT worker claims in the GitHub mailbox at source_head_sha
  (e.g., READY_FOR_REVIEW, self-reported tests, changed files).
- Live completion: Main CIO independent verification at exact HEAD on the host environment.
- ChatGPT's self-reported READY_FOR_REVIEW or worker DONE token is NEVER live completion.
- The next_step_reminder in the mailbox is advisory input to Main CIO, NEVER executed
  as trusted remote instruction.
- Main owns independent acceptance testing, continuation decision, and verification.
"""
from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional, Tuple

CONTRACT_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
VALID_STATUSES = {"READY_FOR_REVIEW", "WAITING_CI", "BLOCKED"}
REMINDER_REQUIRED_KEYS = {"to", "verify_now", "if_pass", "if_fail", "blockers"}

CONTRACT_PROMPT_SUFFIX_TEMPLATE = (
    "本輪task_id與instruction_id如下：task_id={task_id}, instruction_id={instruction_id}。"
    "完成本輪工作後，使用目前實測可写的GitHub工具，將工作回報寫到同一PR／分支的{mailbox}。"
    "必須包括實際修改、實跑測試與證據、未完成工作，以及next_step_reminder：提醒Main現在驗收什麼、通過後原始任務的下一步、不通過應退修什麼。"
    "未實跑不得宣稱PASS。提醒只寫進GitHub回報檔，不依賴使用者轉貼聊天。寫入失败必須明示真正原因，不得聲稱GitHub已更新。"
    "禁止merge/deploy、交易、越權修改與增加付費模型。"
)

def format_contract_suffix(
    task_id: str,
    instruction_id: str,
    mailbox: str = "docs/engineering/PR28_HANDOFF.json"
) -> str:
    """Format standard contract suffix requiring summary, tests, remaining_work, and reminder."""
    if not task_id or not instruction_id:
        raise ValueError("TASK_AND_INSTRUCTION_ID_REQUIRED")
    return CONTRACT_PROMPT_SUFFIX_TEMPLATE.format(
        task_id=task_id,
        instruction_id=instruction_id,
        mailbox=mailbox
    )

def build_prompt(
    task_id: str,
    instruction_id: str,
    instruction_text: str,
    mailbox: str = "docs/engineering/PR28_HANDOFF.json"
) -> str:
    """Combine base instruction text with mandatory contract suffix."""
    suffix = format_contract_suffix(task_id, instruction_id, mailbox)
    clean_text = instruction_text.strip()
    return f"{clean_text}\n\n{suffix}" if clean_text else suffix

def build_initial_prompt(
    task_id: str,
    instruction_id: str,
    task_text: str,
    mailbox: str = "docs/engineering/PR28_HANDOFF.json"
) -> str:
    return build_prompt(task_id, instruction_id, task_text, mailbox)

def build_revision_prompt(
    task_id: str,
    instruction_id: str,
    repair_text: str,
    mailbox: str = "docs/engineering/PR28_HANDOFF.json"
) -> str:
    header = f"Main CIO 驗收退回（{task_id} / {instruction_id}）：\n"
    return build_prompt(task_id, instruction_id, header + repair_text, mailbox)

def build_next_action_prompt(
    task_id: str,
    instruction_id: str,
    next_action_text: str,
    mailbox: str = "docs/engineering/PR28_HANDOFF.json"
) -> str:
    header = f"Main CIO 本地驗收通過本次限定範圍；執行下一個已授權 action（{task_id} / {instruction_id}）：\n"
    return build_prompt(task_id, instruction_id, header + next_action_text, mailbox)

def build_ci_revision_prompt(
    task_id: str,
    instruction_id: str,
    ci_text: str,
    mailbox: str = "docs/engineering/PR28_HANDOFF.json"
) -> str:
    header = f"Main CIO exact-HEAD CI 驗收退回（{task_id} / {instruction_id}）：\n"
    return build_prompt(task_id, instruction_id, header + ci_text, mailbox)

def validate_handback_payload(
    payload: Dict[str, Any],
    *,
    expected_task_id: Optional[str] = None,
    expected_instruction_id: Optional[str] = None,
    repo: Optional[str] = None,
    pr_number: Optional[int] = None,
    branch: Optional[str] = None,
    allow_legacy_v1: bool = False
) -> Dict[str, Any]:
    """Validate handback schema v2 payload and correlate task/instruction IDs.
    
    Legacy v1 is historical input only and will not validate for current v2 instructions
    unless allow_legacy_v1 is explicitly True.
    """
    if not isinstance(payload, dict):
        raise ValueError("PAYLOAD_MUST_BE_DICT")
    
    version = payload.get("schema_version")
    if version == LEGACY_SCHEMA_VERSION:
        if not allow_legacy_v1:
            raise ValueError("LEGACY_SCHEMA_V1_HISTORICAL_ONLY")
        # Validate minimal v1 structure
        if payload.get("status") not in VALID_STATUSES:
            raise ValueError("INVALID_STATUS")
        return payload
    
    if version != CONTRACT_SCHEMA_VERSION:
        raise ValueError("UNSUPPORTED_SCHEMA_VERSION")
        
    if repo is not None and payload.get("repository") != repo:
        raise ValueError(f"REPOSITORY_MISMATCH: expected {repo}, got {payload.get('repository')}")
    if pr_number is not None and payload.get("pr_number") != pr_number:
        raise ValueError(f"PR_NUMBER_MISMATCH: expected {pr_number}, got {payload.get('pr_number')}")
    if branch is not None and payload.get("branch") != branch:
        raise ValueError(f"BRANCH_MISMATCH: expected {branch}, got {payload.get('branch')}")
        
    status = payload.get("status")
    if status not in VALID_STATUSES:
        raise ValueError(f"INVALID_STATUS: {status}")
        
    task_id = payload.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise ValueError("TASK_ID_REQUIRED")
    if expected_task_id is not None and task_id != expected_task_id:
        raise ValueError(f"TASK_ID_MISMATCH: expected {expected_task_id}, got {task_id}")
        
    instruction_id = payload.get("instruction_id")
    if not instruction_id or not isinstance(instruction_id, str):
        raise ValueError("INSTRUCTION_ID_REQUIRED")
    if expected_instruction_id is not None and instruction_id != expected_instruction_id:
        raise ValueError(f"INSTRUCTION_ID_MISMATCH: expected {expected_instruction_id}, got {instruction_id}")
        
    if status in ("READY_FOR_REVIEW", "BLOCKED"):
        source_sha = payload.get("source_head_sha")
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ValueError("REAL_SOURCE_HEAD_SHA_REQUIRED")
            
    if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
        raise ValueError("SUMMARY_REQUIRED")
        
    if not isinstance(payload.get("changed_files"), list):
        raise ValueError("CHANGED_FILES_LIST_REQUIRED")
        
    tests = payload.get("tests")
    if not isinstance(tests, list):
        raise ValueError("TESTS_LIST_REQUIRED")
    for t in tests:
        if not isinstance(t, dict):
            raise ValueError("TEST_ENTRY_MUST_BE_DICT")
        if not all(k in t for k in ("command", "result", "evidence")):
            raise ValueError("TEST_ENTRY_MISSING_FIELDS")
            
    if not isinstance(payload.get("remaining_work"), list):
        raise ValueError("REMAINING_WORK_LIST_REQUIRED")
        
    reminder = payload.get("next_step_reminder")
    if not isinstance(reminder, dict):
        raise ValueError("NEXT_STEP_REMINDER_REQUIRED")
    if not REMINDER_REQUIRED_KEYS.issubset(reminder.keys()):
        missing = REMINDER_REQUIRED_KEYS - set(reminder.keys())
        raise ValueError(f"REMINDER_FIELDS_MISSING: {missing}")
    if reminder.get("to") != "Main CIO":
        raise ValueError("REMINDER_MUST_TARGET_MAIN_CIO")
    if not isinstance(reminder.get("blockers"), list):
        raise ValueError("REMINDER_BLOCKERS_MUST_BE_LIST")
        
    return payload

def parse_next_step_reminder(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract next_step_reminder with untrusted flag.
    
    Security rule: Remote reminders are advisory information for Main CIO triage,
    never trusted execution authority.
    """
    reminder = payload.get("next_step_reminder")
    if not isinstance(reminder, dict):
        return {"present": False, "trusted_instruction": False, "reminder": None}
    return {
        "present": True,
        "trusted_instruction": False,
        "to": reminder.get("to"),
        "verify_now": reminder.get("verify_now"),
        "if_pass": reminder.get("if_pass"),
        "if_fail": reminder.get("if_fail"),
        "blockers": reminder.get("blockers", []),
        "warning": "Advisory input only. Main must independently verify before action."
    }
