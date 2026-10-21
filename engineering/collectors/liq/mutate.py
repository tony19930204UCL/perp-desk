#!/usr/bin/env python3
"""Mutation testing for liq_collector.py according to SPEC.md Section 11."""

import os
import re
import shutil
import subprocess
import sys
import tempfile

MUTANTS = [
    {
        "name": "m01_chmod_644",
        "desc": "Change file chmod from 0o600 to 0o644",
        "target": "os.chmod(self.current_file_path, 0o600)",
        "replacement": "os.chmod(self.current_file_path, 0o644)",
    },
    {
        "name": "m02_recv_ms_as_event_ms",
        "desc": "Take recv_ms from event_ms instead of now",
        "target": '"recv_iso": recv_iso,\n            "recv_ms": recv_ms,',
        "replacement": '"recv_iso": recv_iso,\n            "recv_ms": event_ms,',
    },
    {
        "name": "m03_notional_float",
        "desc": "Use float for notional calculation instead of Decimal",
        "target": "notional_val = ap_dec * z_dec",
        "replacement": "notional_val = Decimal(str(float(avg_price) * float(filled_qty)))",
    },
    {
        "name": "m04_no_dedup",
        "desc": "Remove deduplication check",
        "target": "if dedup_key in self.dedup_cache:",
        "replacement": "if False and dedup_key in self.dedup_cache:",
    },
    {
        "name": "m05_backoff_max_600",
        "desc": "Change backoff max from 60 to 600",
        "target": "base = min(60, 1 * (2 ** k))",
        "replacement": "base = min(600, 1 * (2 ** k))",
    },
    {
        "name": "m06_day_cap_reversed",
        "desc": "Invert day cap condition (> to <)",
        "target": "if self.bytes_today + rec_len > self.max_day_bytes:",
        "replacement": "if self.bytes_today + rec_len < self.max_day_bytes:",
    },
    {
        "name": "m07_reject_to_main",
        "desc": "Write bad messages to main file instead of reject file",
        "target": 'self.write_reject(raw_text, "invalid_json", recv_ms)',
        "replacement": 'self._write_main_record({"dummy": 1}, recv_ms)',
    },
    {
        "name": "m08_no_market_url_check",
        "desc": "Bypass /market/ check for fstream.binance.com URLs",
        "target": 'if ("fstream.binance.com" in host or host.endswith(".binance.com")) and "/market/" not in path:',
        "replacement": 'if False and ("fstream.binance.com" in host or host.endswith(".binance.com")) and "/market/" not in path:',
    },
    {
        "name": "m09_guard_no_lock_check",
        "desc": "Bypass guard lock check so guard always starts daemon",
        "target": "is_locked = True",
        "replacement": "is_locked = False",
    },
    {
        "name": "m10_guard_no_cmdline_check",
        "desc": "Bypass cmdline check when killing stale daemon",
        "target": 'if "liq_collector.py" in cmdline and "--run" in cmdline:',
        "replacement": "if True:",
    },
    {
        "name": "m11_partition_local_tz",
        "desc": "Partition by local timezone date instead of UTC",
        "target": "dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0, tz=datetime.timezone.utc)",
        "replacement": "dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0)",
    },
    {
        "name": "m12_delete_before_verify_compression",
        "desc": "Delete original file before verification in compress_old_files",
        "target": """            if decomp_lines == orig_lines and decomp_sha256 == orig_sha256:
                os.replace(tmp_gz_path, final_gz_path)
                os.chmod(final_gz_path, 0o600)
                os.remove(orig_path)""",
        "replacement": """            os.remove(orig_path)
            if decomp_lines == orig_lines and decomp_sha256 == orig_sha256:
                os.replace(tmp_gz_path, final_gz_path)
                os.chmod(final_gz_path, 0o600)""",
    },
    {
        "name": "m13_main_exit_1",
        "desc": "Return exit code 1 on main() argument error instead of 0",
        "target": "return 0  # EXIT_ON_ARG_ERROR",
        "replacement": "return 1  # EXIT_ON_ARG_ERROR",
    },
    {
        "name": "m14_no_independent_heartbeat_task",
        "desc": "Remove independent heartbeat task (heartbeat only inside connected loop)",
        "target": "self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())",
        "replacement": "self.heartbeat_task = None  # MUTANT NO HEARTBEAT TASK",
    },
    {
        "name": "m15_recv_slice_70",
        "desc": "Change recv slice timeout from 5s to 70s",
        "target": "    RECV_SLICE_S = 5",
        "replacement": "    RECV_SLICE_S = 70",
    },
    {
        "name": "m16_data_dir_unlimited_upward_search",
        "desc": "Restore unlimited upward search for data dir resolution",
        "target": "return None  # NO_DEFAULT_IN_UNAUTHORIZED_DIR",
        "replacement": 'return os.path.abspath(os.path.join(script_dir, "..", "..", "lab", "data", "liq-collector"))',
    },
    {
        "name": "m17_loop_signal_handler_fallback_only",
        "desc": "Remove loop.add_signal_handler use (fall back to signal.signal only)",
        "target": "loop.add_signal_handler(signal.SIGTERM, self.stop)",
        "replacement": 'raise NotImplementedError("MUTANT_NO_LOOP_SIGNAL")',
    },
    {
        "name": "m18_heartbeat_only_when_connected",
        "desc": "Heartbeat only when connected",
        "target": "                if not self.stop_requested:",
        "replacement": "                if not self.stop_requested and self.is_connected:",
    },
    {
        "name": "m19_status_code_418_dropped",
        "desc": "Change (429, 418) to (429,)",
        "target": "    if status_code in (429, 418):",
        "replacement": "    if status_code in (429,):",
    },
    {
        "name": "m20_planned_reconnect_2h",
        "desc": "Planned reconnect threshold 23*3600 -> 2*3600",
        "target": "                            if self.now() - conn_start_time >= 23 * 3600:",
        "replacement": "                            if self.now() - conn_start_time >= 2 * 3600:",
    },
    {
        "name": "m21_silent_reconnect_90s",
        "desc": "Silent reconnect >= 900 -> >= 90",
        "target": "                            if silence_dur >= 900:",
        "replacement": "                            if silence_dur >= 90:",
    },
    {
        "name": "m22_dedup_cache_size_5",
        "desc": "Dedup cache size 2000 -> 5",
        "target": "        if len(self.dedup_cache) > 2000:",
        "replacement": "        if len(self.dedup_cache) > 5:",
    },
    {
        "name": "m23_makedirs_mode_755",
        "desc": "Makedirs mode 0o700 -> 0o755",
        "target": """        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        os.chmod(data_dir, 0o700)""",
        "replacement": """        os.makedirs(data_dir, mode=0o755, exist_ok=True)
        os.chmod(data_dir, 0o755)""",
    },
    {
        "name": "m24_drop_E_requirement",
        "desc": "Drop the E requirement",
        "target": '        if "E" not in payload:',
        "replacement": '        if False and "E" not in payload:',
    },
    {
        "name": "m25_drop_T_requirement",
        "desc": "Drop the T requirement",
        "target": '        required_o = ("s", "S", "o", "f", "q", "p", "ap", "X", "l", "z", "T")',
        "replacement": '        required_o = ("s", "S", "o", "f", "q", "p", "ap", "X", "l", "z")',
    },
    {
        "name": "m26_status_json_non_atomic",
        "desc": "status.json non-atomic write",
        "target": "            os.replace(tmp_status, final_status)",
        "replacement": "            shutil.copyfile(tmp_status, final_status)",
    },
    {
        "name": "m27_heartbeat_interval_600",
        "desc": "HEARTBEAT_INTERVAL_S 60->600",
        "target": "    HEARTBEAT_INTERVAL_S = 60",
        "replacement": "    HEARTBEAT_INTERVAL_S = 600",
    },
]


# Each original mutation must fail a relevant original assertion, not the harness.
SELECTORS = [
    "test_05_file_and_dir_permissions",
    "test_07_recv_ms_from_now_not_event_ms",
    "test_01_real_fixtures_parsing",
    "test_04_deduplication",
    "test_08_backoff_sequence_and_bounds",
    "test_11_day_and_disk_caps_and_throttling",
    "test_03_bad_messages_to_reject_file",
    "test_13_error_exit_codes_and_url_not_market",
    "test_12_single_instance_lock_and_guard_spawn",
    "test_16_guard_stale_cmdline_verification_and_sigterm",
    "test_06_date_partitioning_and_append",
    "test_15_compression_rotation_and_verification",
    "test_13_error_exit_codes_and_url_not_market",
    "test_14b_heartbeat_during_disconnect",
    "test_14c_stop_latency_and_recv_slice",
    "test_14e_default_data_dir_resolution",
    "test_18m_loop_signal_handler_installed",
    "test_14b_heartbeat_during_disconnect",
    "test_18e_rate_limited_418",
    "test_18f_planned_24h_reconnect",
    "test_18g_silent_reconnect_boundary_900s",
    "test_18h_dedup_lru_boundary_2000",
    "test_18i_ensure_permissions_new_dir_umask_000",
    "test_18j_reject_missing_top_level_E",
    "test_18k_reject_missing_trade_time_T",
    "test_18l_status_json_atomic_replacement",
    "test_18n_heartbeat_interval_constants",
]


def run_suite(tmp_dir, selector=None, umask=0o022):
    args = [sys.executable, "-m", "unittest", "-v"]
    if selector:
        args.append("test_liq_collector.TestLiqCollector." + selector)
    else:
        args.append("test_liq_collector")
    def set_mask():
        os.umask(umask)
    try:
        proc = subprocess.run(args, cwd=tmp_dir, capture_output=True,
                              text=True, timeout=45, preexec_fn=set_mask)
        return proc.returncode, (proc.stdout or "") + "\\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    except Exception as exc:
        return None, "HARNESS_ERROR: " + repr(exc)


def control_ok(result):
    code, output = result
    return code == 0 and "OK" in output and "FAILED" not in output


def assertion_kill(result, selector):
    code, output = result
    # -14, timeouts, import failures and unittest ERROR are never kills.
    if code is None or code <= 0 or "ERROR:" in output:
        return False
    match = re.search(r"^FAIL: (\\w+) \\(test_liq_collector\\.TestLiqCollector\\)\\s*$",
                      output, re.MULTILINE)
    return (match is not None and match.group(1) == selector
            and "AssertionError:" in output and "FAILED (failures=" in output)


def run_mutation_tests():
    base_dir = os.path.abspath(os.path.dirname(__file__))
    orig_src = os.path.join(base_dir, "liq_collector.py")
    orig_test = os.path.join(base_dir, "test_liq_collector.py")
    orig_fixtures = os.path.join(base_dir, "fixtures")
    with open(orig_src, encoding="utf-8") as stream:
        src_code = stream.read()
    with tempfile.TemporaryDirectory() as tmp_dir:
        shutil.copy2(orig_src, os.path.join(tmp_dir, "liq_collector.py"))
        shutil.copy2(orig_test, os.path.join(tmp_dir, "test_liq_collector.py"))
        shutil.copytree(orig_fixtures, os.path.join(tmp_dir, "fixtures"))
        for mask in (0o022, 0o002):
            result = run_suite(tmp_dir, umask=mask)
            print(f"CONTROL umask={mask:04o}: {result[0]}", flush=True)
            if not control_ok(result):
                print(result[1][-6000:], flush=True)
                print("BLOCKED: CONTROL must pass before mutants", flush=True)
                return 1

        caught = 0
        for idx, (mutant, selector) in enumerate(zip(MUTANTS, SELECTORS), 1):
            name = mutant["name"]
            target = mutant["target"]
            count = src_code.count(target)
            if count != 1:
                print(f"[{idx:02d}/27] INVALID {name}: target count={count}", flush=True)
                continue
            result = run_suite(tmp_dir, selector)
            if not control_ok(result):
                print(f"[{idx:02d}/27] INVALID {name}: selector control "
                      f"{result[0]} {result[1][-800:]}", flush=True)
                continue
            with open(os.path.join(tmp_dir, "liq_collector.py"), "w",
                      encoding="utf-8") as stream:
                stream.write(src_code.replace(target, mutant["replacement"], 1))
            result = run_suite(tmp_dir, selector)
            if assertion_kill(result, selector):
                caught += 1
                state = "CAUGHT"
            else:
                state = "SURVIVED"
            evidence = result[1][-800:].replace("\\n", " ")
            print(f"[{idx:02d}/27] {state} {name} {selector}: "
                  f"rc={result[0]} {evidence}", flush=True)
            with open(os.path.join(tmp_dir, "liq_collector.py"), "w",
                      encoding="utf-8") as stream:
                stream.write(src_code)
        print(f"mutants {len(MUTANTS)} caught {caught} "
              f"survived {len(MUTANTS) - caught}", flush=True)
        return 0 if caught == len(MUTANTS) else 1


if __name__ == "__main__":
    sys.exit(run_mutation_tests())
