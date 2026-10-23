#!/usr/bin/env python3
"""
Mutation Testing Runner for Coinglass Heatmap Archiver.
Executes control run first, then runs each mutant in a complete private tree copy.
"""

import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
ORIG_ARCHIVE = os.path.join(WORK_DIR, "heatmap_archive.py")
ORIG_CAPTURE = os.path.join(WORK_DIR, "heatmap_capture.ps1")

MUTATIONS = [
    # 1-14: Existing mutations
    {
        "name": "chmod_0o644",
        "file": "archive",
        "description": "Change file permission from 0o600 to 0o644",
        "find": "0o600",
        "replace": "0o644",
        "expect": ["test_01"],
    },
    {
        "name": "remove_sha256_verification",
        "file": "archive",
        "description": "Bypass PNG sha256 check",
        "find": "if actual_png_sha != png_sha:",
        "replace": "if False and actual_png_sha != png_sha:",
        "expect": ["test_07", "test_17"],
    },
    {
        "name": "exit_1_on_chrome_unreachable",
        "file": "archive",
        "description": "Exit with code 1 instead of 0 on chrome_unreachable",
        "find": """        # Handle specific error status from powershell
        if ps_status in ("chrome_unreachable", "wrong_chrome", "no_coinglass_tab", "powershell_error"):
            cleanup_staging()
            detail = parsed_out.get("error") or ps_status
            sys.stdout.write(f"ERROR heatmap-archive status={ps_status} detail={detail}\\n")
            append_run_log(data_root, {
                "run_id": run_id,
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": ps_status,
                "logged_in": logged_in,
                "login_evidence": login_evidence,
                "login_wait_ms": login_wait_ms,
                "focus_mode": focus_mode,
                "error": detail,
                "combos_total": len(config.get("combos", [])),
                "combos_ok": 0,
                "combos_login_required": 0,
                "png_saved_run": False,
                "png_run_counter": run_counter,
                "bytes_archived": 0,
                "elapsed_seconds": round(time.time() - start_time, 2),
            })
            return 0""",
        "replace": """        # Handle specific error status from powershell
        if ps_status in ("chrome_unreachable", "wrong_chrome", "no_coinglass_tab", "powershell_error"):
            cleanup_staging()
            detail = parsed_out.get("error") or ps_status
            sys.stdout.write(f"ERROR heatmap-archive status={ps_status} detail={detail}\\n")
            append_run_log(data_root, {
                "run_id": run_id,
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": ps_status,
                "logged_in": logged_in,
                "login_evidence": login_evidence,
                "login_wait_ms": login_wait_ms,
                "focus_mode": focus_mode,
                "error": detail,
                "combos_total": len(config.get("combos", [])),
                "combos_ok": 0,
                "combos_login_required": 0,
                "png_saved_run": False,
                "png_run_counter": run_counter,
                "bytes_archived": 0,
                "elapsed_seconds": round(time.time() - start_time, 2),
            })
            return 1""",
        "expect": ["test_05"],
    },
    {
        "name": "remove_failure_runs_jsonl",
        "file": "archive",
        "description": "Skip writing runs.jsonl on powershell error / chrome unreachable",
        "find": """        # Handle specific error status from powershell
        if ps_status in ("chrome_unreachable", "wrong_chrome", "no_coinglass_tab", "powershell_error"):
            cleanup_staging()
            detail = parsed_out.get("error") or ps_status
            sys.stdout.write(f"ERROR heatmap-archive status={ps_status} detail={detail}\\n")
            append_run_log""",
        "replace": """        # Handle specific error status from powershell
        if ps_status in ("chrome_unreachable", "wrong_chrome", "no_coinglass_tab", "powershell_error"):
            cleanup_staging()
            detail = parsed_out.get("error") or ps_status
            sys.stdout.write(f"ERROR heatmap-archive status={ps_status} detail={detail}\\n")
            if False: append_run_log""",
        "expect": ["test_04", "test_05", "test_17"],
    },
    {
        "name": "retention_prune_newest_first",
        "file": "archive",
        "description": "Reverse prune sort order to prune newest first",
        "find": 'groups.sort(key=lambda g: (g["date"], g["prefix"]))',
        "replace": 'groups.sort(key=lambda g: (g["date"], g["prefix"]), reverse=True)',
        "expect": ["test_13"],
    },
    {
        "name": "remove_price_range_check",
        "file": "archive",
        "description": "Bypass price range validation",
        "find": "if price < y_min or price > y_max:",
        "replace": "if False and (price < y_min or price > y_max):",
        "expect": ["test_10", "test_17"],
    },
    {
        "name": "remove_flock",
        "file": "archive",
        "description": "Remove flock locking in archive",
        "find": "fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
        "replace": "pass  # flock bypassed",
        "expect": ["test_15", "test_17", "test_25"],
    },
    {
        "name": "ps1_add_19242",
        "file": "capture",
        "description": "Add banned port 19242 to heatmap_capture.ps1",
        "find": "# 1. Port whitelist check - strictly only 19301",
        "replace": "# 1. Port whitelist check - strictly only 19301; do not use 19242",
        "expect": ["test_18", "test_28"],
    },
    {
        "name": "ps1_remove_bring_to_front",
        "file": "capture",
        "description": "Remove Page.bringToFront from ps1",
        "find": "Invoke-CdpCall -Sess $sess -Method 'Page.bringToFront'",
        "replace": "# Invoke-CdpCall -Sess $sess -Method 'Page.bringToFront'",
        "expect": ["test_19"],
    },
    {
        "name": "ps1_remove_focus_emulation_false",
        "file": "capture",
        "description": "Remove Emulation.setFocusEmulationEnabled false from ps1 finally block",
        "find": "$unfocusPayload = @{ id = 999998; method = 'Emulation.setFocusEmulationEnabled'; params = @{ enabled = $false } } | ConvertTo-Json -Compress",
        "replace": "$unfocusPayload = @{ id = 999998; method = 'Emulation.doNothing'; params = @{} } | ConvertTo-Json -Compress",
        "expect": ["test_19", "test_31"],
    },
    {
        "name": "ps1_add_header_button_click",
        "file": "capture",
        "description": "Add header button click back to ps1",
        "find": "# Login detection per Task 1: async polling function kept in $loginPollJs",
        "replace": "# Login detection per Task 1: async polling function kept in $loginPollJs\n    # document.querySelectorAll('header button') click",
        "expect": ["test_20", "test_29"],
    },
    {
        "name": "archive_remove_warnings_write",
        "file": "archive",
        "description": "Remove warnings write from metadata JSON",
        "find": '"warnings": combo_warnings,',
        "replace": '"warnings": [],',
        "expect": ["test_21", "test_23"],
    },
    {
        "name": "archive_remove_runs_jsonl_warnings",
        "file": "archive",
        "description": "Remove warnings field from runs.jsonl record",
        "find": '"warnings": sorted(list(all_run_warnings)),',
        "replace": '# "warnings" omitted from runs.jsonl',
        "expect": ["test_22", "test_23"],
    },
    {
        "name": "archive_remove_stale_data_warning",
        "file": "archive",
        "description": "Omit stale_data warning from combo warnings",
        "find": 'combo_warnings.append("stale_data")',
        "replace": 'pass  # combo_warnings.append("stale_data")',
        "expect": ["test_23"],
    },
    {
        "name": "login_required_branch_false",
        "file": "archive",
        "description": "Bypass login_required branch with if False",
        "find": 'if r.get("login_required", False):',
        "replace": 'if False and r.get("login_required", False):',
        "expect": ["test_09", "test_24"],
    },
    {
        "name": "login_required_counter_deleted",
        "file": "archive",
        "description": "Delete combos_login_req counter increment",
        "find": "combos_login_req += 1",
        "replace": "pass  # combos_login_req += 1 deleted",
        "expect": ["test_24"],
    },
    {
        "name": "skipped_locked_prefix_warn",
        "file": "archive",
        "description": "Change skipped_locked line prefix from ERROR to WARN",
        "find": 'sys.stdout.write(f"ERROR heatmap-archive status=skipped_locked detail={err_msg}\\n")',
        "replace": 'sys.stdout.write(f"WARN heatmap-archive status=skipped_locked detail={err_msg}\\n")',
        "expect": ["test_15", "test_25"],
    },
    {
        "name": "png_modulo_flipped",
        "file": "archive",
        "description": "Flip PNG cadence modulo check from == 0 to != 0",
        "find": "save_png_run = (png_every_n <= 1) or (run_counter % png_every_n == 0)",
        "replace": "save_png_run = (png_every_n <= 1) or (run_counter % png_every_n != 0)",
        "expect": ["test_32"],
    },
    {
        "name": "png_counter_never_incremented",
        "file": "archive",
        "description": "Do not increment png_run_counter in state.json",
        "find": '"png_run_counter": run_counter + 1,',
        "replace": '"png_run_counter": run_counter,',
        "expect": ["test_17", "test_32"],
    },
    {
        "name": "png_counter_incremented_on_dry_run",
        "file": "archive",
        "description": "Increment png_run_counter on dry run",
        "find": "if args.dry_run:\n            cleanup_staging()",
        "replace": 'if args.dry_run:\n            save_state(data_root, {"png_run_counter": run_counter + 1, "updated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")})\n            cleanup_staging()',
        "expect": ["test_33"],
    },
    {
        "name": "png_forced_when_no_chart_removed",
        "file": "archive",
        "description": "Do not force PNG save when chart_data_ok is False",
        "find": "save_png_combo = save_png_run or (not chart_data_ok)",
        "replace": "save_png_combo = save_png_run",
        "expect": ["test_33"],
    },
    {
        "name": "ps1_add_document_cookie",
        "file": "capture",
        "description": "Add document.cookie to ps1 script",
        "find": "# 1. Port whitelist check - strictly only 19301",
        "replace": "# 1. Port whitelist check - strictly only 19301 document.cookie",
        "expect": ["test_18", "test_26", "test_28"],
    },
    {
        "name": "ps1_add_network_getallcookies",
        "file": "capture",
        "description": "Add Network.getAllCookies to ps1 script",
        "find": "# 1. Port whitelist check - strictly only 19301",
        "replace": "# 1. Port whitelist check - strictly only 19301 Network.getAllCookies",
        "expect": ["test_26", "test_28"],
    },
    {
        "name": "runtime_banned_term_guard_removed",
        "file": "archive",
        "description": "Bypass runtime banned terms guard before powershell invocation",
        "find": "for term in BANNED_PS1_TERMS:\n            if term.lower() in ps_content_lower:",
        "replace": "for term in []:\n            if term.lower() in ps_content_lower:",
        "expect": ["test_28"],
    },
    {
        "name": "ps1_bring_to_front_unconditional",
        "file": "capture",
        "description": "Call Page.bringToFront unconditionally without hidden check",
        "find": "if ($pageVisibility -eq 'hidden') {\n        Invoke-CdpCall -Sess $sess -Method 'Page.bringToFront' | Out-Null",
        "replace": "if ($true) {\n        Invoke-CdpCall -Sess $sess -Method 'Page.bringToFront' | Out-Null",
        "expect": ["test_31"],
    },
    {
        "name": "ps1_set_focus_emulation_true_removed",
        "file": "capture",
        "description": "Set Emulation.setFocusEmulationEnabled to false instead of true",
        "find": """    Invoke-CdpCall -Sess $sess -Method 'Emulation.setFocusEmulationEnabled' -Params @{
        enabled = $true
    }""",
        "replace": """    Invoke-CdpCall -Sess $sess -Method 'Emulation.setFocusEmulationEnabled' -Params @{
        enabled = $false
    }""",
        "expect": ["test_35"],
    },
    {
        "name": "ps1_login_poll_constants_changed",
        "file": "capture",
        "description": "Change login poll arguments from (10000, 500) to (5000, 250)",
        "find": "($loginPollJs)(10000, 500)",
        "replace": "($loginPollJs)(5000, 250)",
        "expect": ["test_20"],
    },
    {
        "name": "ps1_login_poll_user_menu_consecutive_removed",
        "file": "capture",
        "description": "Accept user_menu_button on 1 poll instead of 2 consecutive polls",
        "find": "if (userMenuConsecutive >= 2) {",
        "replace": "if (userMenuConsecutive >= 1) {",
        "expect": ["test_30"],
    },

    # 29-35: Additional mutations (M5)
    {
        "name": "ps1_remove_clear_device_metrics_finally",
        "file": "capture",
        "description": "Remove Emulation.clearDeviceMetricsOverride in the finally block",
        "find": """        try {
            $clearPayload = @{ id = 999999; method = 'Emulation.clearDeviceMetricsOverride'; params = @{} } | ConvertTo-Json -Compress
            $sess.Call($clearPayload, 999999) | Out-Null
        } catch {}""",
        "replace": "        # clearDeviceMetricsOverride removed",
        "expect": ["test_36"],
    },
    {
        "name": "ps1_login_poll_add_click",
        "file": "capture",
        "description": "Make ps1 click something by adding .click() to login poll JS",
        "find": """            if (el.children.length === 0) {
                leaves.push(el);
            }""",
        "replace": """            if (el.children.length === 0) {
                el.click();
                leaves.push(el);
            }""",
        "expect": ["test_29"],
    },
    {
        "name": "ps1_login_poll_rect_right_threshold",
        "file": "capture",
        "description": "Change rect.right >= window.innerWidth - 40 threshold to >= 0 in poll JS",
        "find": "rect.right >= window.innerWidth - 40",
        "replace": "rect.right >= 0",
        "expect": ["test_30"],
    },
    {
        "name": "ps1_login_poll_remove_width_guard",
        "file": "capture",
        "description": "Remove window.innerWidth >= 1000 mobile guard in poll JS",
        "find": "if (window.innerWidth >= 1000) {",
        "replace": "if (true) {",
        "expect": ["test_30"],
    },
    {
        "name": "archive_force_png_off_cadence",
        "file": "archive",
        "description": "Write PNG even when run policy says no (wrapper)",
        "find": "save_png_combo = save_png_run or (not chart_data_ok)",
        "replace": "save_png_combo = True",
        "expect": ["test_32"],
    },
    {
        "name": "archive_drop_focus_mode_metadata",
        "file": "archive",
        "description": "Drop focus_mode from metadata JSON",
        "find": """                "visibility": page_visibility,
                "focus_mode": focus_mode,""",
        "replace": """                "visibility": page_visibility,""",
        "expect": ["test_03", "test_33"],
    },
    {
        "name": "archive_drop_login_evidence_runs_jsonl",
        "file": "archive",
        "description": "Drop login_evidence from runs.jsonl",
        "find": """        run_record = {
            "run_id": run_id,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": overall_status,
            "logged_in": logged_in,
            "login_evidence": login_evidence,""",
        "replace": """        run_record = {
            "run_id": run_id,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": overall_status,
            "logged_in": logged_in,""",
        "expect": ["test_33"],
    },
]


def create_tree_copy(dest_dir: str) -> None:
    """Create a complete copy of the project tree inside dest_dir."""
    shutil.copy2(ORIG_ARCHIVE, dest_dir)
    shutil.copy2(ORIG_CAPTURE, dest_dir)
    shutil.copy2(os.path.join(WORK_DIR, "config.json"), dest_dir)
    shutil.copytree(os.path.join(WORK_DIR, "fixtures"), os.path.join(dest_dir, "fixtures"))
    shutil.copytree(
        os.path.join(WORK_DIR, "tests"),
        os.path.join(dest_dir, "tests"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    os.chmod(os.path.join(dest_dir, "heatmap_archive.py"), 0o755)


def parse_unittest_output(stdout: str, stderr: str):
    """
    Parse unittest discover output.
    Returns (has_load_error, failing_tests).
    """
    combined = (stdout or "") + "\n" + (stderr or "")
    has_load_error = (
        "SyntaxError" in combined
        or "ImportError" in combined
        or "_FailedTest" in combined
    )
    # Extract test names from FAIL and ERROR lines
    matches = re.findall(r"^(?:FAIL|ERROR):\s+([a-zA-Z0-9_]+)", combined, re.MULTILINE)
    failing_tests = list(dict.fromkeys(matches))
    return has_load_error, failing_tests


def run_control():
    """M2: Run control on unmutated tree copy before running mutants."""
    temp_dir = tempfile.mkdtemp(prefix="mut_control_")
    try:
        create_tree_copy(temp_dir)
        env = dict(os.environ)
        env.pop("TEST_ARCHIVE_PY", None)
        env.pop("TEST_CAPTURE_PS1", None)

        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        res = subprocess.run(cmd, cwd=temp_dir, env=env, capture_output=True, text=True)
        out = (res.stdout or "") + "\n" + (res.stderr or "")

        has_load_error, failing_tests = parse_unittest_output(res.stdout, res.stderr)
        if res.returncode != 0 or has_load_error or "OK" not in out:
            print("CONTROL FAILED: " + ", ".join(failing_tests))
            sys.exit(2)

        m_count = re.search(r"Ran (\d+) tests", out)
        n_tests = m_count.group(1) if m_count else "?"
        print(f"CONTROL OK {n_tests} tests")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def execute_mutation(idx: int, m: dict, orig_archive_code: str, orig_capture_code: str):
    """
    M1, M3, M4: Run single mutant in private tree copy.
    Returns (idx, m_name, status, failing_tests, expected).
    """
    m_name = m["name"]
    target_is_archive = (m["file"] == "archive")
    orig_code = orig_archive_code if target_is_archive else orig_capture_code

    # M4: Validate pattern exists and names mutant on failure
    if m["find"] not in orig_code:
        print(f"ABORT: Mutation pattern not found for mutant '{m_name}' in {m['file']}!")
        sys.exit(1)

    # M4: Validate not a no-op
    mut_code = orig_code.replace(m["find"], m["replace"], 1)
    if mut_code == orig_code:
        print(f"ABORT: Mutation '{m_name}' in {m['file']} was a no-op!")
        sys.exit(1)

    temp_dir = tempfile.mkdtemp(prefix=f"mut_{idx}_")
    try:
        create_tree_copy(temp_dir)
        target_path = os.path.join(temp_dir, "heatmap_archive.py" if target_is_archive else "heatmap_capture.ps1")
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(mut_code)
        if target_is_archive:
            os.chmod(target_path, 0o755)

        env = dict(os.environ)
        env.pop("TEST_ARCHIVE_PY", None)
        env.pop("TEST_CAPTURE_PS1", None)

        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        res = subprocess.run(cmd, cwd=temp_dir, env=env, capture_output=True, text=True)

        has_load_error, failing_tests = parse_unittest_output(res.stdout, res.stderr)

        # M3: Mutant counts as CAUGHT only if:
        # (a) no SyntaxError / ImportError / Traceback from loading module
        # (b) at least one FAIL or ERROR line names a test
        # (c) at least one failing test matches an expect substring
        matched = [
            t for t in failing_tests
            if any(exp in t for exp in m["expect"])
        ]

        if has_load_error or not failing_tests or res.returncode == 0:
            status = "SURVIVED"
        elif matched:
            status = "CAUGHT"
        else:
            status = "WRONG_TEST"

        return idx, m_name, status, failing_tests, m["expect"]
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def run_all_mutations():
    with open(ORIG_ARCHIVE, "r", encoding="utf-8") as f:
        orig_archive_code = f.read()

    with open(ORIG_CAPTURE, "r", encoding="utf-8") as f:
        orig_capture_code = f.read()

    # M2: Control run first
    run_control()

    total = len(MUTATIONS)
    print(f"Starting mutation testing with {total} mutants...")

    workers = int(os.environ.get("MUTATION_WORKERS", "4"))
    results = [None] * total

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(execute_mutation, i, m, orig_archive_code, orig_capture_code): i
            for i, m in enumerate(MUTATIONS, 1)
        }
        for future in concurrent.futures.as_completed(future_map):
            idx, m_name, status, failing_tests, expected = future.result()
            results[idx - 1] = (idx, m_name, status, failing_tests, expected)

    caught_count = 0
    survived_list = []

    for idx, m_name, status, failing_tests, expected in results:
        # Print for each mutant: caught/survived, failing test names (max 5), expected names
        fails_display = failing_tests[:5]
        print(f"[{idx}/{total}] {status}: {m_name} (failing: {fails_display}, expected: {expected})")
        if status == "CAUGHT":
            caught_count += 1
        else:
            survived_list.append(m_name)

    print("-" * 60)
    if not survived_list:
        print(f"MUTATION {caught_count}/{total} CAUGHT")
        return 0
    else:
        print(f"SURVIVED: {', '.join(survived_list)}")
        return 1


if __name__ == "__main__":
    exit_code = run_all_mutations()
    sys.exit(exit_code)
