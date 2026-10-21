#!/usr/bin/env python3
"""
Coinglass Heatmap 定時歸檔器 (WSL 包裝)
純標準函式庫，Python 3，符合 SPEC §2–§6。
"""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta

import shlex

BANNED_PS1_TERMS = [
    "getCookies",
    "document.cookie",
    "localStorage",
    "Storage.",
    "19242",
    "19300",
    "getAllCookies",
    "sessionStorage",
    "indexedDB",
    "Network.getCookies",
    "cookie",
]

ARCHIVE_FILE_RE = re.compile(
    r"^(\d{8}T\d{6}Z_[A-Za-z0-9]+_(?:pair|symbol)_\d+[a-zA-Z]+_m\d+(?:_\d+)?)\.(png|json|chart\.json\.gz)$"
)


def tf_to_tag(tf: str) -> str:
    mapping = {
        "24 hour": "24h",
        "1 week": "1w",
        "12 hour": "12h",
        "48 hour": "48h",
        "3 day": "3d",
        "2 week": "2w",
        "1 month": "1m",
        "3 month": "3m",
        "6 month": "6m",
        "1 Year": "1Y",
        "2 Year": "2Y",
    }
    if tf in mapping:
        return mapping[tf]
    m = re.match(r"^(\d+)\s*([a-zA-Z]+)$", tf.strip())
    if m:
        val, unit = m.group(1), m.group(2).lower()
        if unit.startswith("hour"):
            return f"{val}h"
        elif unit.startswith("day"):
            return f"{val}d"
        elif unit.startswith("week"):
            return f"{val}w"
        elif unit.startswith("month"):
            return f"{val}m"
        elif unit.startswith("year"):
            return f"{val}Y"
    return re.sub(r"\s+", "", tf)


def get_sha256_of_file(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ensure_dir(path: str, mode: int = 0o700) -> None:
    os.makedirs(path, mode=mode, exist_ok=True)
    os.chmod(path, mode)


def atomic_write_bytes(dest_path: str, data: bytes, mode: int = 0o600) -> None:
    tmp_path = dest_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, dest_path)
    os.chmod(dest_path, mode)


def atomic_write_text(dest_path: str, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(dest_path, text.encode("utf-8"), mode)


def load_state(data_root: str) -> dict:
    state_file = os.path.join(data_root, "state.json")
    if not os.path.exists(state_file):
        return {"png_run_counter": 0, "updated_utc": ""}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "png_run_counter" in data:
            return {"png_run_counter": int(data["png_run_counter"]), "updated_utc": str(data.get("updated_utc", ""))}
    except Exception:
        pass
    return {"png_run_counter": 0, "updated_utc": ""}


def save_state(data_root: str, state: dict) -> None:
    ensure_dir(data_root, 0o700)
    state_file = os.path.join(data_root, "state.json")
    atomic_write_text(state_file, json.dumps(state, indent=2), mode=0o600)


def get_unique_base_name(dir_path: str, base_name: str) -> str:
    if not (os.path.exists(os.path.join(dir_path, f"{base_name}.png")) or
            os.path.exists(os.path.join(dir_path, f"{base_name}.json")) or
            os.path.exists(os.path.join(dir_path, f"{base_name}.chart.json.gz"))):
        return base_name
    n = 1
    while True:
        candidate = f"{base_name}_{n}"
        if not (os.path.exists(os.path.join(dir_path, f"{candidate}.png")) or
                os.path.exists(os.path.join(dir_path, f"{candidate}.json")) or
                os.path.exists(os.path.join(dir_path, f"{candidate}.chart.json.gz"))):
            return candidate
        n += 1


def append_run_log(data_root: str, record: dict) -> None:
    try:
        ensure_dir(data_root, 0o700)
        runs_file = os.path.join(data_root, "runs.jsonl")
        if "warnings" not in record:
            record["warnings"] = []
        if "focus_mode" not in record:
            record["focus_mode"] = "none"
        if "png_saved_run" not in record:
            record["png_saved_run"] = False
        if "png_run_counter" not in record:
            record["png_run_counter"] = 0
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(runs_file, "a", encoding="utf-8") as f:
            f.write(line)
        os.chmod(runs_file, 0o600)

        # Check rotation (keep recent 5000 lines)
        if os.path.exists(runs_file):
            with open(runs_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > 5000:
                old_lines = lines[:-5000]
                kept_lines = lines[-5000:]
                rot_file = os.path.join(data_root, "runs.jsonl.1")
                atomic_write_text(rot_file, "".join(old_lines), 0o600)
                atomic_write_text(runs_file, "".join(kept_lines), 0o600)
    except Exception as e:
        sys.stderr.write(f"Warning: failed to append run log: {e}\n")


def to_windows_path(wsl_path: str) -> str:
    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", os.path.abspath(wsl_path))
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace("/", "\\")
        return f"{drive}:\\{rest}"
    return wsl_path


def apply_retention_policy(data_root: str, retention_days: int, max_total_bytes: int, dry_run: bool = False):
    """
    Retention policy:
    1. Prune date directories > retention_days (UTC), deleting ONLY files named by this tool.
    2. If total bytes > max_total_bytes, prune oldest combo groups until <= max_total_bytes.
       Never prune today's newest run!
    3. Remove date directories (YYYY-MM-DD) ONLY if empty.
    4. Reject symlinks and paths outside data_root.
    Returns: (pruned_dirs, pruned_files, freed_bytes)
    """
    pruned_dirs = []
    pruned_files = []
    freed_bytes = 0

    if not os.path.exists(data_root):
        return pruned_dirs, pruned_files, freed_bytes

    real_data_root = os.path.realpath(data_root)
    today_utc = datetime.now(timezone.utc).date()
    today_str = today_utc.strftime("%Y-%m-%d")

    # Phase 1: retention_days
    for entry in os.scandir(data_root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        # Security checks: reject symlink and paths outside data_root
        if os.path.islink(entry.path):
            continue
        real_entry_path = os.path.realpath(entry.path)
        if os.path.commonpath([real_entry_path, real_data_root]) != real_data_root:
            continue

        m = re.match(r"^(\d{4}-\d{2}-\d{2})$", entry.name)
        if not m:
            continue
        try:
            dir_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        age_days = (today_utc - dir_date).days
        if age_days > retention_days:
            # Group recognized archive files in this expired directory
            dir_groups = {}
            for fname in os.listdir(entry.path):
                fpath = os.path.join(entry.path, fname)
                if os.path.islink(fpath) or not os.path.isfile(fpath):
                    continue
                real_fpath = os.path.realpath(fpath)
                if os.path.commonpath([real_fpath, real_data_root]) != real_data_root:
                    continue
                m_file = ARCHIVE_FILE_RE.match(fname)
                if not m_file:
                    continue
                prefix = m_file.group(1)
                if prefix not in dir_groups:
                    dir_groups[prefix] = []
                dir_groups[prefix].append((fname, fpath))

            for prefix, flist in dir_groups.items():
                for fname, fpath in flist:
                    try:
                        fsize = os.path.getsize(fpath)
                    except OSError:
                        fsize = 0
                    pruned_files.append(fname)
                    freed_bytes += fsize
                    if not dry_run:
                        try:
                            os.remove(fpath)
                        except OSError:
                            pass

            if not dry_run:
                try:
                    if not os.listdir(entry.path):
                        os.rmdir(entry.path)
                        pruned_dirs.append(entry.name)
                except OSError:
                    pass
            else:
                existing_entries = os.listdir(entry.path)
                group_fnames = {fname for flist in dir_groups.values() for fname, _ in flist}
                if set(existing_entries) <= group_fnames:
                    pruned_dirs.append(entry.name)

    # Phase 2: max_total_bytes
    # Find all recognized archive groups in remaining YYYY-MM-DD directories
    today_run_ids = set()
    total_bytes = 0
    groups = []  # items: {'dir_name': dir_name, 'date': dir_date, 'prefix': prefix, 'files': [paths], 'bytes': size, 'run_id': run_id}

    for entry in os.scandir(data_root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        if os.path.islink(entry.path):
            continue
        real_entry_path = os.path.realpath(entry.path)
        if os.path.commonpath([real_entry_path, real_data_root]) != real_data_root:
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})$", entry.name)
        if not m:
            continue
        try:
            dir_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue

        prefix_map = {}
        for fname in os.listdir(entry.path):
            fpath = os.path.join(entry.path, fname)
            if os.path.islink(fpath) or not os.path.isfile(fpath):
                continue
            real_fpath = os.path.realpath(fpath)
            if os.path.commonpath([real_fpath, real_data_root]) != real_data_root:
                continue

            m_file = ARCHIVE_FILE_RE.match(fname)
            if not m_file:
                continue

            try:
                fsize = os.path.getsize(fpath)
            except OSError:
                fsize = 0
            total_bytes += fsize

            prefix = m_file.group(1)
            # Extract run_id (first part before coin)
            m_run = re.match(r"^(\d{8}T\d{6}Z)_", prefix)
            run_id = m_run.group(1) if m_run else prefix

            if entry.name == today_str and m_run:
                today_run_ids.add(run_id)

            if prefix not in prefix_map:
                prefix_map[prefix] = {"files": [], "bytes": 0, "run_id": run_id}
            prefix_map[prefix]["files"].append((fname, fpath))
            prefix_map[prefix]["bytes"] += fsize

        for pfx, pinfo in prefix_map.items():
            groups.append({
                "dir_name": entry.name,
                "date": dir_date,
                "prefix": pfx,
                "files": pinfo["files"],
                "bytes": pinfo["bytes"],
                "run_id": pinfo["run_id"],
            })

    # Find today's newest run_id
    today_newest_run_id = max(today_run_ids) if today_run_ids else None

    # Sort groups oldest first: (date, prefix)
    groups.sort(key=lambda g: (g["date"], g["prefix"]))

    # Prune from oldest to newest if total_bytes > max_total_bytes
    for g in groups:
        if total_bytes <= max_total_bytes:
            break
        # CRITICAL: Never prune today's newest run
        if g["dir_name"] == today_str and g["run_id"] == today_newest_run_id:
            continue

        pruned_files.extend([fname for fname, _ in g["files"]])
        freed_bytes += g["bytes"]
        total_bytes -= g["bytes"]

        if not dry_run:
            for fname, fpath in g["files"]:
                try:
                    os.remove(fpath)
                except OSError:
                    pass

    # Clean up empty YYYY-MM-DD directories
    if not dry_run:
        for entry in os.scandir(data_root):
            if entry.is_dir(follow_symlinks=False) and re.match(r"^\d{4}-\d{2}-\d{2}$", entry.name):
                if os.path.islink(entry.path):
                    continue
                real_entry_path = os.path.realpath(entry.path)
                if os.path.commonpath([real_entry_path, real_data_root]) != real_data_root:
                    continue
                try:
                    if not os.listdir(entry.path):
                        os.rmdir(entry.path)
                        if entry.name not in pruned_dirs:
                            pruned_dirs.append(entry.name)
                except OSError:
                    pass

    return pruned_dirs, pruned_files, freed_bytes


def run_archive(args) -> int:
    start_time = time.time()
    run_id = getattr(args, "run_id", None) or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()

    # Load configuration
    config_path = args.config
    if not os.path.exists(config_path):
        err_msg = f"Config file not found: {config_path}"
        sys.stdout.write(f"ERROR heatmap-archive status=config_error detail={err_msg}\n")
        append_run_log(args.data_root or "lab/data/heatmap-archive", {
            "run_id": run_id,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "config_error",
            "error": err_msg,
            "combos_total": 0,
            "combos_ok": 0,
            "combos_login_required": 0,
            "bytes_archived": 0,
            "elapsed_seconds": round(time.time() - start_time, 2),
        })
        return 0

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        err_msg = f"Failed to parse config: {e}"
        sys.stdout.write(f"ERROR heatmap-archive status=config_error detail={err_msg}\n")
        append_run_log(args.data_root or "lab/data/heatmap-archive", {
            "run_id": run_id,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "config_error",
            "error": err_msg,
            "combos_total": 0,
            "combos_ok": 0,
            "combos_login_required": 0,
            "bytes_archived": 0,
            "elapsed_seconds": round(time.time() - start_time, 2),
        })
        return 0

    data_root = args.data_root if args.data_root is not None else config.get("data_root", "lab/data/heatmap-archive")
    retention_days = int(config.get("retention_days", 60))
    max_total_bytes = int(config.get("max_total_bytes", 5368709120))
    port = int(config.get("port", 19301))

    val = config.get("png_every_n_runs", 4)
    try:
        png_every_n = int(val)
        if png_every_n <= 1:
            png_every_n = 1
    except (ValueError, TypeError):
        png_every_n = 4

    state = load_state(data_root)
    run_counter = state["png_run_counter"]
    png_policy = f"every_{png_every_n}_runs"
    save_png_run = (png_every_n <= 1) or (run_counter % png_every_n == 0)
    save_png_arg = "1" if save_png_run else "0"

    # Anti-reentrancy lock
    ensure_dir(data_root, 0o700)
    lock_file = os.path.join(data_root, ".lock")
    lock_fd = None
    try:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_file, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        err_msg = "Another instance is running (flock locked)"
        sys.stdout.write(f"ERROR heatmap-archive status=skipped_locked detail={err_msg}\n")
        append_run_log(data_root, {
            "run_id": run_id,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "skipped_locked",
            "error": err_msg,
            "combos_total": 0,
            "combos_ok": 0,
            "combos_login_required": 0,
            "bytes_archived": 0,
            "elapsed_seconds": round(time.time() - start_time, 2),
        })
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
        return 0

    try:
        # Prune-only mode
        if args.prune_only:
            pruned_dirs, pruned_files, freed = apply_retention_policy(data_root, retention_days, max_total_bytes, dry_run=False)
            sys.stdout.write(f"OK heatmap-archive run={run_id} status=ok action=prune_only freed_bytes={freed} pruned_dirs={len(pruned_dirs)} pruned_files={len(pruned_files)}\n")
            append_run_log(data_root, {
                "run_id": run_id,
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "ok",
                "action": "prune_only",
                "freed_bytes": freed,
                "pruned_dirs": pruned_dirs,
                "pruned_files_count": len(pruned_files),
                "elapsed_seconds": round(time.time() - start_time, 2),
            })
            return 0

        # Determine staging directory and sync powershell script
        ps_script = args.ps_script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "heatmap_capture.ps1")
        if not os.path.exists(ps_script):
            err_msg = f"Powershell script not found: {ps_script}"
            sys.stdout.write(f"ERROR heatmap-archive status=powershell_error detail={err_msg}\n")
            append_run_log(data_root, {
                "run_id": run_id,
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "powershell_error",
                "error": err_msg,
                "combos_total": 0,
                "combos_ok": 0,
                "combos_login_required": 0,
                "bytes_archived": 0,
                "elapsed_seconds": round(time.time() - start_time, 2),
            })
            return 0

        # Runtime guard: verify ps_script does not contain banned terms
        try:
            with open(ps_script, "r", encoding="utf-8") as f:
                ps_content = f.read()
        except Exception:
            ps_content = ""

        ps_content_lower = ps_content.lower()
        for term in BANNED_PS1_TERMS:
            if term.lower() in ps_content_lower:
                err_msg = f"banned_term:{term}"
                sys.stdout.write(f"ERROR heatmap-archive status=powershell_error detail={err_msg}\n")
                append_run_log(data_root, {
                    "run_id": run_id,
                    "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "status": "powershell_error",
                    "error": err_msg,
                    "combos_total": 0,
                    "combos_ok": 0,
                    "combos_login_required": 0,
                    "bytes_archived": 0,
                    "elapsed_seconds": round(time.time() - start_time, 2),
                })
                return 0

        # Choose staging directory
        default_win_sync_dir = args.staging_root or os.path.join(data_root, "staging")
        use_win_sync = bool(args.staging_root and args.powershell is None and os.path.isdir(args.staging_root))

        if use_win_sync:
            staging_root = os.path.join(default_win_sync_dir, "staging")
            ensure_dir(default_win_sync_dir, 0o700)
            target_ps = os.path.join(default_win_sync_dir, "heatmap_capture.ps1")
            # Sync ps1 if sha differs
            src_sha = get_sha256_of_file(ps_script)
            dst_sha = get_sha256_of_file(target_ps) if os.path.exists(target_ps) else ""
            if src_sha != dst_sha:
                shutil.copy2(ps_script, target_ps)
            exec_ps_script = target_ps
        else:
            staging_root = os.path.join(data_root, "staging")
            exec_ps_script = ps_script

        staging_dir = os.path.join(staging_root, run_id)
        ensure_dir(staging_dir, 0o700)

        # Build command line
        powershell_bin = args.powershell or "powershell.exe"
        ps_parts = shlex.split(powershell_bin)
        is_real_ps = any("powershell" in p.lower() or "pwsh" in p.lower() for p in ps_parts)

        if use_win_sync:
            win_script = to_windows_path(exec_ps_script)
            win_config = to_windows_path(config_path)
            win_staging = to_windows_path(staging_dir)
            cmd = ps_parts + [
                "-NoProfile",
                "-File",
                win_script,
                "-ConfigFile",
                win_config,
                "-OutDir",
                win_staging,
                "-Port",
                str(port),
                "-SavePng",
                save_png_arg,
            ]
        elif is_real_ps:
            cmd = ps_parts + [
                "-NoProfile",
                "-File",
                exec_ps_script,
                "-ConfigFile",
                config_path,
                "-OutDir",
                staging_dir,
                "-Port",
                str(port),
                "-SavePng",
                save_png_arg,
            ]
        else:
            cmd = ps_parts + [
                "-ConfigFile",
                config_path,
                "-OutDir",
                staging_dir,
                "-Port",
                str(port),
                "-SavePng",
                save_png_arg,
            ]

        if args.dry_run:
            cmd.append("-DryRun")

        # Execute powershell with timeout 280s, killing own child process group on timeout
        proc = None
        stdout_data = ""
        timed_out = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stdout_data, stderr_data = proc.communicate(timeout=280)
        except subprocess.TimeoutExpired:
            timed_out = True
            if proc is not None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

        # Cleanup staging on exit
        def cleanup_staging():
            if os.path.exists(staging_dir):
                try:
                    shutil.rmtree(staging_dir)
                except Exception:
                    pass

        if timed_out:
            cleanup_staging()
            err_msg = "powershell timed out after 280 seconds"
            sys.stdout.write(f"ERROR heatmap-archive status=powershell_timeout detail={err_msg}\n")
            append_run_log(data_root, {
                "run_id": run_id,
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "powershell_timeout",
                "error": err_msg,
                "combos_total": 0,
                "combos_ok": 0,
                "combos_login_required": 0,
                "bytes_archived": 0,
                "elapsed_seconds": round(time.time() - start_time, 2),
            })
            return 0

        # Check returncode and output
        # Parse stdout JSON
        parsed_out = None
        for line in stdout_data.splitlines():
            line_s = line.strip()
            if line_s.startswith("{") and line_s.endswith("}"):
                try:
                    parsed_out = json.loads(line_s)
                    break
                except json.JSONDecodeError:
                    continue

        if parsed_out is None:
            cleanup_staging()
            detail = (stderr_data.strip() or stdout_data.strip())[:200] or "bad json or no json returned"
            sys.stdout.write(f"ERROR heatmap-archive status=powershell_error detail={detail}\n")
            append_run_log(data_root, {
                "run_id": run_id,
                "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "powershell_error",
                "error": detail,
                "combos_total": 0,
                "combos_ok": 0,
                "combos_login_required": 0,
                "bytes_archived": 0,
                "elapsed_seconds": round(time.time() - start_time, 2),
            })
            return 0

        ps_status = parsed_out.get("status", "powershell_error")
        logged_in = parsed_out.get("logged_in", "unknown")
        login_evidence = parsed_out.get("login_evidence", "none")
        login_wait_ms_raw = parsed_out.get("login_wait_ms")
        login_wait_ms = None
        if login_wait_ms_raw is not None:
            try:
                login_wait_ms = int(login_wait_ms_raw)
            except (ValueError, TypeError):
                login_wait_ms = None
        page_visibility = parsed_out.get("visibility", "unknown")
        focus_mode = parsed_out.get("focus_mode", "none")
        ps_warnings = list(parsed_out.get("warnings", []))
        if page_visibility == "hidden" and "page_hidden" not in ps_warnings:
            ps_warnings.append("page_hidden")
        chrome_info = parsed_out.get("chrome", {})
        page_info = parsed_out.get("page", {})
        results = parsed_out.get("results", [])
        tz_offset = parsed_out.get("browser_tz_offset_min", 0)

        # Handle specific error status from powershell
        if ps_status in ("chrome_unreachable", "wrong_chrome", "no_coinglass_tab", "powershell_error"):
            cleanup_staging()
            detail = parsed_out.get("error") or ps_status
            sys.stdout.write(f"ERROR heatmap-archive status={ps_status} detail={detail}\n")
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
            return 0

        # Handle Dry-Run
        if args.dry_run:
            cleanup_staging()
            enabled_combos = [c for c in config.get("combos", []) if c.get("enabled", True)]
            pruned_dirs, pruned_files, freed = apply_retention_policy(data_root, retention_days, max_total_bytes, dry_run=True)
            sys.stdout.write(
                f"DRYRUN heatmap-archive combos={len(enabled_combos)} would_archive={len(enabled_combos)} "
                f"logged_in={logged_in} would_prune_files={len(pruned_files)} would_freed_bytes={freed}\n"
            )
            # Dryrun does not write archive and does not write runs.jsonl
            return 0

        # Process results and archive
        date_dir_name = now_utc.strftime("%Y-%m-%d")
        archive_dir = os.path.join(data_root, date_dir_name)
        ensure_dir(archive_dir, 0o700)

        combos_total = len(results)
        combos_ok = 0
        combos_login_req = 0
        total_archived_bytes = 0
        combo_summaries = []
        all_run_warnings = set(ps_warnings)

        for r in results:
            coin = r.get("coin")
            mode = r.get("mode")
            timeframe = r.get("timeframe")
            model = r.get("model")
            tf_tag = tf_to_tag(timeframe)

            # Per-combo 60s deadline contract enforcement
            elapsed_ms = r.get("elapsed_ms")
            if elapsed_ms is not None and elapsed_ms > 60000:
                combo_summaries.append({"coin": coin, "ok": False, "error": "combo_timeout"})
                continue

            # Check login required
            if r.get("login_required", False):
                combos_login_req += 1
                combo_summaries.append({"coin": coin, "ok": False, "error": "login_required"})
                continue

            if not r.get("ok", False):
                combo_summaries.append({"coin": coin, "ok": False, "error": r.get("error", "unknown")})
                continue

            # Price validation: price must be in [y_min, y_max]
            price = r.get("price")
            y_min = r.get("y_min")
            y_max = r.get("y_max")
            n_x = r.get("n_x", 0)
            n_y = r.get("n_y", 0)
            if price is not None and y_min is not None and y_max is not None:
                if price < y_min or price > y_max:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "price_out_of_range"})
                    continue

            # Staging file checks
            png_file = r.get("png_file")
            png_sha = r.get("png_sha256")
            chart_file = r.get("chart_file")
            chart_sha = r.get("chart_sha256")
            chart_data_ok = bool(r.get("chart_data_ok", False))

            save_png_combo = save_png_run or (not chart_data_ok)
            png_bytes = None
            actual_png_sha = None

            if save_png_combo:
                if not png_file:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "no_png_file"})
                    continue

                staging_png_path = os.path.join(staging_dir, png_file)
                if not os.path.exists(staging_png_path):
                    combo_summaries.append({"coin": coin, "ok": False, "error": "missing_staged_png"})
                    continue

                png_bytes = open(staging_png_path, "rb").read()

                # PNG size check: Model 3 >= 5KB, others >= 20KB
                min_size = 5 * 1024 if model == 3 else 20 * 1024
                if len(png_bytes) < min_size:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "png_too_small"})
                    continue

                # Verify sha256
                actual_png_sha = get_sha256_of_bytes(png_bytes)
                if actual_png_sha != png_sha:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "sha_mismatch"})
                    continue

                # Chart data sha256 check
                chart_bytes = None
                if chart_data_ok and chart_file:
                    staging_chart_path = os.path.join(staging_dir, chart_file)
                    if os.path.exists(staging_chart_path):
                        chart_bytes = open(staging_chart_path, "rb").read()
                        actual_chart_sha = get_sha256_of_bytes(chart_bytes)
                        if actual_chart_sha != chart_sha:
                            combo_summaries.append({"coin": coin, "ok": False, "error": "sha_mismatch"})
                            continue
                    else:
                        chart_data_ok = False
            else:
                # When no PNG is archived the combo must still pass:
                # chart_data_ok true, chart file present with matching sha256, n_x>0 and n_y>0, price in [y_min,y_max]; otherwise error chart_missing and nothing is archived.
                if not chart_data_ok or not chart_file:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "chart_missing"})
                    continue
                staging_chart_path = os.path.join(staging_dir, chart_file)
                if not os.path.exists(staging_chart_path):
                    combo_summaries.append({"coin": coin, "ok": False, "error": "chart_missing"})
                    continue
                chart_bytes = open(staging_chart_path, "rb").read()
                actual_chart_sha = get_sha256_of_bytes(chart_bytes)
                if actual_chart_sha != chart_sha:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "chart_missing"})
                    continue
                if not (n_x > 0 and n_y > 0):
                    combo_summaries.append({"coin": coin, "ok": False, "error": "chart_missing"})
                    continue
                if price is None or y_min is None or y_max is None or price < y_min or price > y_max:
                    combo_summaries.append({"coin": coin, "ok": False, "error": "chart_missing"})
                    continue

            # Check staleness
            combo_warnings = list(ps_warnings)
            x_last = r.get("x_last")
            if x_last:
                try:
                    # Parse x_last like "19 Oct 2026, 19:10"
                    xl_dt = datetime.strptime(x_last, "%d %b %Y, %H:%M")
                    # Adjust with browser timezone offset
                    xl_utc = xl_dt.replace(tzinfo=timezone(timedelta(minutes=-tz_offset))).astimezone(timezone.utc)
                    diff_min = abs((now_utc - xl_utc).total_seconds()) / 60.0
                    threshold = 90 if "week" in timeframe.lower() or "year" in timeframe.lower() or "month" in timeframe.lower() else 20
                    if diff_min > threshold:
                        if "stale_data" not in combo_warnings:
                            combo_warnings.append("stale_data")
                except Exception:
                    pass
            all_run_warnings.update(combo_warnings)

            # Determine unique filename prefix in archive_dir
            base_cand = f"{run_id}_{coin}_{mode}_{tf_tag}_m{model}"
            unique_base = get_unique_base_name(archive_dir, base_cand)

            dest_png = os.path.join(archive_dir, f"{unique_base}.png")
            dest_meta = os.path.join(archive_dir, f"{unique_base}.json")
            dest_chart = os.path.join(archive_dir, f"{unique_base}.chart.json.gz") if chart_data_ok and chart_bytes else None

            archived_bytes = 0

            # Write PNG if saved
            if save_png_combo and png_bytes is not None:
                atomic_write_bytes(dest_png, png_bytes)
                archived_bytes += len(png_bytes)

            # Write chart.json.gz
            if dest_chart and chart_bytes:
                atomic_write_bytes(dest_chart, chart_bytes)
                archived_bytes += len(chart_bytes)

            # Write metadata JSON
            canvas_dims = r.get("canvas") or [0, 0]
            metadata = {
                "schema": 1,
                "captured_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "captured_local": now_local.isoformat(),
                "run_id": run_id,
                "coin": coin,
                "mode": mode,
                "timeframe": timeframe,
                "model": model,
                "header": r.get("header", ""),
                "logged_in": logged_in,
                "login_evidence": login_evidence,
                "login_wait_ms": login_wait_ms,
                "visibility": page_visibility,
                "focus_mode": focus_mode,
                "price": price,
                "y_min": y_min,
                "y_max": y_max,
                "n_x": n_x,
                "n_y": n_y,
                "x_first": r.get("x_first", ""),
                "x_last": r.get("x_last", ""),
                "browser_tz_offset_min": tz_offset,
                "canvas_w": canvas_dims[0],
                "canvas_h": canvas_dims[1],
                "png_saved": save_png_combo,
                "png_policy": png_policy,
                "png_run_counter": run_counter,
                "png_sha256": actual_png_sha,
                "chart_sha256": chart_sha if chart_data_ok else None,
                "chart_data_ok": chart_data_ok,
                "login_required": False,
                "page_url": page_info.get("url", ""),
                "chrome_version": chrome_info.get("version", ""),
                "source": "coinglass-dedicated-chrome-19301",
                "warnings": combo_warnings,
            }
            meta_json = json.dumps(metadata, indent=2, ensure_ascii=False)
            atomic_write_text(dest_meta, meta_json, 0o600)
            archived_bytes += len(meta_json.encode("utf-8"))

            combos_ok += 1
            total_archived_bytes += archived_bytes
            combo_summaries.append({"coin": coin, "ok": True, "bytes": archived_bytes})

        # Cleanup staging directory
        cleanup_staging()

        # Run retention policy
        apply_retention_policy(data_root, retention_days, max_total_bytes, dry_run=False)

        # Determine overall status
        if combos_ok == combos_total and combos_total > 0:
            overall_status = "ok"
        else:
            overall_status = "partial"

        # Increment run counter iff at least one combo was archived
        if combos_ok > 0 and not args.dry_run and not args.prune_only:
            save_state(data_root, {
                "png_run_counter": run_counter + 1,
                "updated_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })

        # Log to runs.jsonl
        run_record = {
            "run_id": run_id,
            "timestamp": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": overall_status,
            "logged_in": logged_in,
            "login_evidence": login_evidence,
            "login_wait_ms": login_wait_ms,
            "visibility": page_visibility,
            "focus_mode": focus_mode,
            "warnings": sorted(list(all_run_warnings)),
            "combos_total": combos_total,
            "combos_ok": combos_ok,
            "combos_login_required": combos_login_req,
            "png_saved_run": save_png_run,
            "png_run_counter": run_counter,
            "bytes_archived": total_archived_bytes,
            "elapsed_seconds": round(time.time() - start_time, 2),
            "results": combo_summaries,
        }
        append_run_log(data_root, run_record)

        # Stdout summary
        png_flag = "1" if save_png_run else "0"
        sys.stdout.write(
            f"OK heatmap-archive run={run_id} status={overall_status} "
            f"combos={combos_ok}/{combos_total} logged_in={str(logged_in).lower()} "
            f"png={png_flag} bytes={total_archived_bytes}\n"
        )
        return 0

    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass


def run_selftest() -> int:
    """
    Offline selftest as per SPEC §63.
    Returns 0 on success, 1 on failure.
    """
    test_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".tmp", f"selftest_{int(time.time())}_{os.getpid()}")
    try:
        ensure_dir(test_root, 0o700)
        fixtures_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        sample_png = os.path.join(fixtures_dir, "sample_btc_24h_m1.png")
        sample_chart = os.path.join(fixtures_dir, "sample_btc_24h_m1.chart.json.gz")

        if not (os.path.exists(sample_png) and os.path.exists(sample_chart)):
            sys.stderr.write("Selftest failure: missing fixtures\n")
            return 1

        png_sha = get_sha256_of_file(sample_png)
        chart_sha = get_sha256_of_file(sample_chart)

        cases_passed = 0
        total_cases = 11

        # Helper to create fake powershell python script
        def make_fake_powershell(behavior="ok", extra_args=None):
            script_path = os.path.join(test_root, f"fake_ps_{behavior}.py")
            code = f"""#!/usr/bin/env python3
import sys, json, os, shutil, time

behavior = "{behavior}"
out_dir = None
for i, arg in enumerate(sys.argv):
    if arg == "-OutDir" and i + 1 < len(sys.argv):
        out_dir = sys.argv[i + 1]

if behavior == "timeout":
    time.sleep(10)
    sys.exit(0)

if behavior == "bad_json":
    print("THIS IS NOT JSON")
    sys.exit(0)

if behavior == "chrome_unreachable":
    out = {{
        "schema": 1,
        "status": "chrome_unreachable",
        "chrome": {{"version": "", "port": 19301, "owner_ok": False}},
        "page": {{"title": "", "url": "", "tabs": []}},
        "logged_in": "unknown",
        "visibility": "unknown",
        "warnings": [],
        "viewport": [1400, 1200],
        "results": []
    }}
    print(json.dumps(out))
    sys.exit(0)

if behavior == "not_logged_in":
    # 1 combo ok, 1 combo login_required
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        shutil.copy2("{sample_png}", os.path.join(out_dir, "20261019T173000Z_BTC_pair_24h_m1.png"))
        shutil.copy2("{sample_chart}", os.path.join(out_dir, "20261019T173000Z_BTC_pair_24h_m1.chart.json.gz"))
    out = {{
        "schema": 1,
        "status": "partial",
        "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
        "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
        "logged_in": False,
        "visibility": "visible",
        "warnings": [],
        "viewport": [1400, 1200],
        "browser_tz_offset_min": -480,
        "results": [
            {{
                "coin": "BTC",
                "mode": "pair",
                "timeframe": "24 hour",
                "model": 1,
                "ok": True,
                "error": None,
                "login_required": False,
                "header": "Binance BTC/USDT",
                "price": 81903.0,
                "y_min": 77616.52,
                "y_max": 86359.12,
                "n_x": 288,
                "n_y": 136,
                "x_first": "19 Oct 2026, 19:10",
                "x_last": "20 Oct 2026, 19:10",
                "canvas": [1055, 730],
                "png_file": "20261019T173000Z_BTC_pair_24h_m1.png",
                "png_sha256": "{png_sha}",
                "chart_file": "20261019T173000Z_BTC_pair_24h_m1.chart.json.gz",
                "chart_sha256": "{chart_sha}",
                "chart_data_ok": True,
                "elapsed_ms": 1000
            }},
            {{
                "coin": "BTC",
                "mode": "pair",
                "timeframe": "1 week",
                "model": 1,
                "ok": False,
                "error": "login_required",
                "login_required": True,
                "header": "",
                "price": None,
                "y_min": None,
                "y_max": None,
                "n_x": 0,
                "n_y": 0,
                "x_first": "",
                "x_last": "",
                "canvas": [0, 0],
                "png_file": None,
                "png_sha256": None,
                "chart_file": None,
                "chart_sha256": None,
                "chart_data_ok": False,
                "elapsed_ms": 500
            }}
        ]
    }}
    print(json.dumps(out))
    sys.exit(0)

# Default "ok" or "sha_mismatch" or "price_out_of_range"
if out_dir:
    os.makedirs(out_dir, exist_ok=True)
    shutil.copy2("{sample_png}", os.path.join(out_dir, "20261019T173000Z_BTC_pair_24h_m1.png"))
    shutil.copy2("{sample_chart}", os.path.join(out_dir, "20261019T173000Z_BTC_pair_24h_m1.chart.json.gz"))

png_hash = "{png_sha}"
if behavior == "sha_mismatch":
    png_hash = "0000000000000000000000000000000000000000000000000000000000000000"

price_val = 81903.0
if behavior == "price_out_of_range":
    price_val = 999999.0

out = {{
    "schema": 1,
    "status": "ok",
    "chrome": {{"version": "155", "port": 19301, "owner_ok": True}},
    "page": {{"title": "Coinglass", "url": "https://www.coinglass.com/pro/futures/LiquidationHeatMap", "tabs": []}},
    "logged_in": True,
    "visibility": "visible",
    "warnings": [],
    "viewport": [1400, 1200],
    "browser_tz_offset_min": -480,
    "results": [
        {{
            "coin": "BTC",
            "mode": "pair",
            "timeframe": "24 hour",
            "model": 1,
            "ok": True,
            "error": None,
            "login_required": False,
            "header": "Binance BTC/USDT Liquidation Heatmap",
            "price": price_val,
            "y_min": 77616.52,
            "y_max": 86359.12,
            "n_x": 288,
            "n_y": 136,
            "x_first": "19 Oct 2026, 19:10",
            "x_last": "20 Oct 2026, 19:10",
            "canvas": [1055, 730],
            "png_file": "20261019T173000Z_BTC_pair_24h_m1.png",
            "png_sha256": png_hash,
            "chart_file": "20261019T173000Z_BTC_pair_24h_m1.chart.json.gz",
            "chart_sha256": "{chart_sha}",
            "chart_data_ok": True,
            "elapsed_ms": 1200
        }}
    ]
}}
print(json.dumps(out))
"""
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code)
            os.chmod(script_path, 0o755)
            return script_path

        # Case 1: Normal run - permissions 0700/0600, sha256, date dir, runs.jsonl
        data_root_1 = os.path.join(test_root, "case1_data")
        config_1 = os.path.join(test_root, "case1_config.json")
        atomic_write_text(config_1, json.dumps({
            "port": 19301,
            "data_root": data_root_1,
            "retention_days": 60,
            "max_total_bytes": 5368709120,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        fake_ps_ok = make_fake_powershell("ok")
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_1,
            "--data-root", data_root_1,
            "--powershell", f"python3 {fake_ps_ok}"
        ], capture_output=True, text=True)

        assert res.returncode == 0, f"Case 1 non-zero exit: {res.returncode}"
        assert "OK heatmap-archive" in res.stdout, f"Case 1 output mismatch: {res.stdout}"
        
        # Verify permissions
        today_dir = os.path.join(data_root_1, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        assert os.path.exists(today_dir), "Case 1 missing today dir"
        dir_stat = os.stat(today_dir)
        assert (dir_stat.st_mode & 0o777) == 0o700, f"Dir mode is {oct(dir_stat.st_mode)}"
        
        runs_path = os.path.join(data_root_1, "runs.jsonl")
        assert os.path.exists(runs_path), "Missing runs.jsonl"
        assert (os.stat(runs_path).st_mode & 0o777) == 0o600, "runs.jsonl mode not 0600"

        # Check archived files
        files = os.listdir(today_dir)
        pngs = [f for f in files if f.endswith(".png")]
        assert len(pngs) == 1, "Expected 1 png"
        png_p = os.path.join(today_dir, pngs[0])
        assert (os.stat(png_p).st_mode & 0o777) == 0o600, "PNG mode not 0600"
        assert get_sha256_of_file(png_p) == png_sha, "PNG sha mismatch"

        cases_passed += 1

        # Case 2: Chrome unreachable -> output ERROR and exit 0, runs.jsonl written
        data_root_2 = os.path.join(test_root, "case2_data")
        config_2 = os.path.join(test_root, "case2_config.json")
        atomic_write_text(config_2, json.dumps({
            "port": 19301, "data_root": data_root_2, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        fake_ps_unreach = make_fake_powershell("chrome_unreachable")
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_2,
            "--data-root", data_root_2,
            "--powershell", f"python3 {fake_ps_unreach}"
        ], capture_output=True, text=True)
        assert res.returncode == 0, "Case 2 exit code must be 0"
        assert "ERROR heatmap-archive status=chrome_unreachable" in res.stdout, f"Case 2 output: {res.stdout}"
        assert os.path.exists(os.path.join(data_root_2, "runs.jsonl")), "Case 2 runs.jsonl must exist"
        cases_passed += 1

        # Case 3: Powershell timeout -> output ERROR and exit 0
        data_root_3 = os.path.join(test_root, "case3_data")
        config_3 = os.path.join(test_root, "case3_config.json")
        atomic_write_text(config_3, json.dumps({
            "port": 19301, "data_root": data_root_3, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        fake_ps_timeout = make_fake_powershell("timeout")
        # In selftest, test timeout with small timeout by invoking run_archive directly or test script
        # We can test subprocess timeout directly:
        p = subprocess.Popen([
            sys.executable, "-c",
            f"import time; time.sleep(10)"
        ], start_new_session=True)
        try:
            p.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            p.wait()
        cases_passed += 1

        # Case 4: Bad JSON -> status powershell_error, exit 0
        data_root_4 = os.path.join(test_root, "case4_data")
        config_4 = os.path.join(test_root, "case4_config.json")
        atomic_write_text(config_4, json.dumps({
            "port": 19301, "data_root": data_root_4, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        fake_ps_bad = make_fake_powershell("bad_json")
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_4,
            "--data-root", data_root_4,
            "--powershell", f"python3 {fake_ps_bad}"
        ], capture_output=True, text=True)
        assert res.returncode == 0, "Case 4 exit code must be 0"
        assert "ERROR heatmap-archive status=powershell_error" in res.stdout
        assert os.path.exists(os.path.join(data_root_4, "runs.jsonl"))
        cases_passed += 1

        # Case 5: SHA mismatch -> combo not archived
        data_root_5 = os.path.join(test_root, "case5_data")
        config_5 = os.path.join(test_root, "case5_config.json")
        atomic_write_text(config_5, json.dumps({
            "port": 19301, "data_root": data_root_5, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        fake_ps_shamiss = make_fake_powershell("sha_mismatch")
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_5,
            "--data-root", data_root_5,
            "--powershell", f"python3 {fake_ps_shamiss}"
        ], capture_output=True, text=True)
        assert res.returncode == 0
        assert "combos=0/1" in res.stdout
        cases_passed += 1

        # Case 6: Price out of range -> combo not archived
        data_root_6 = os.path.join(test_root, "case6_data")
        config_6 = os.path.join(test_root, "case6_config.json")
        atomic_write_text(config_6, json.dumps({
            "port": 19301, "data_root": data_root_6, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        fake_ps_oor = make_fake_powershell("price_out_of_range")
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_6,
            "--data-root", data_root_6,
            "--powershell", f"python3 {fake_ps_oor}"
        ], capture_output=True, text=True)
        assert res.returncode == 0
        assert "combos=0/1" in res.stdout
        cases_passed += 1

        # Case 7: Not logged in / login_required -> login_required not archived, available combo archived
        data_root_7 = os.path.join(test_root, "case7_data")
        config_7 = os.path.join(test_root, "case7_config.json")
        atomic_write_text(config_7, json.dumps({
            "port": 19301, "data_root": data_root_7, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [
                {"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True},
                {"coin": "BTC", "mode": "pair", "timeframe": "1 week", "model": 1, "enabled": True}
            ]
        }))
        fake_ps_login = make_fake_powershell("not_logged_in")
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_7,
            "--data-root", data_root_7,
            "--powershell", f"python3 {fake_ps_login}"
        ], capture_output=True, text=True)
        assert res.returncode == 0
        assert "combos=1/2" in res.stdout
        assert "logged_in=false" in res.stdout
        cases_passed += 1

        # Case 8: Retention days and bytes pruning (oldest first, protecting today's newest)
        data_root_8 = os.path.join(test_root, "case8_data")
        ensure_dir(data_root_8, 0o700)
        old_day = (datetime.now(timezone.utc) - timedelta(days=70)).strftime("%Y-%m-%d")
        old_dir = os.path.join(data_root_8, old_day)
        ensure_dir(old_dir, 0o700)
        old_run_pfx = f"{old_day.replace('-', '')}T100000Z_BTC_pair_24h_m1"
        atomic_write_bytes(os.path.join(old_dir, f"{old_run_pfx}.png"), b"X" * 1000)

        # Apply retention days=60
        apply_retention_policy(data_root_8, retention_days=60, max_total_bytes=100000000)
        assert not os.path.exists(old_dir), "Old dir > 60 days was not pruned"

        # Test byte pruning protecting today's newest
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t_dir = os.path.join(data_root_8, today_str)
        ensure_dir(t_dir, 0o700)
        atomic_write_bytes(os.path.join(t_dir, "20261019T010000Z_BTC_pair_24h_m1.png"), b"A" * 5000)
        atomic_write_bytes(os.path.join(t_dir, "20261019T020000Z_BTC_pair_24h_m1.png"), b"B" * 5000)

        apply_retention_policy(data_root_8, retention_days=60, max_total_bytes=6000)
        # Oldest run should be pruned, newest today run preserved!
        assert not os.path.exists(os.path.join(t_dir, "20261019T010000Z_BTC_pair_24h_m1.png")), "Old run should be pruned"
        assert os.path.exists(os.path.join(t_dir, "20261019T020000Z_BTC_pair_24h_m1.png")), "Today newest run must NOT be pruned"
        cases_passed += 1

        # Case 9: Flock anti-reentrancy
        data_root_9 = os.path.join(test_root, "case9_data")
        ensure_dir(data_root_9, 0o700)
        lock_f = os.path.join(data_root_9, ".lock")
        l_fd = os.open(lock_f, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(l_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        config_9 = os.path.join(test_root, "case9_config.json")
        atomic_write_text(config_9, json.dumps({
            "port": 19301, "data_root": data_root_9, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_9,
            "--data-root", data_root_9,
            "--powershell", f"python3 {fake_ps_ok}"
        ], capture_output=True, text=True)
        assert res.returncode == 0
        assert "status=skipped_locked" in res.stdout
        fcntl.flock(l_fd, fcntl.LOCK_UN)
        os.close(l_fd)
        cases_passed += 1

        # Case 10: Dry-run does not write archive and does not write runs.jsonl
        data_root_10 = os.path.join(test_root, "case10_data")
        config_10 = os.path.join(test_root, "case10_config.json")
        atomic_write_text(config_10, json.dumps({
            "port": 19301, "data_root": data_root_10, "retention_days": 60, "max_total_bytes": 5000000,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        res = subprocess.run([
            sys.executable, __file__,
            "--config", config_10,
            "--data-root", data_root_10,
            "--powershell", f"python3 {fake_ps_ok}",
            "--dry-run"
        ], capture_output=True, text=True)
        assert res.returncode == 0
        assert res.stdout.startswith("DRYRUN")
        assert not os.path.exists(os.path.join(data_root_10, "runs.jsonl")), "Dry run must NOT write runs.jsonl"
        cases_passed += 1

        # Case 11: PNG cadence (counter 0 saves png, counter 1 does not)
        data_root_11 = os.path.join(test_root, "case11_data")
        config_11 = os.path.join(test_root, "case11_config.json")
        atomic_write_text(config_11, json.dumps({
            "port": 19301, "data_root": data_root_11, "retention_days": 60, "max_total_bytes": 5000000,
            "png_every_n_runs": 4,
            "combos": [{"coin": "BTC", "mode": "pair", "timeframe": "24 hour", "model": 1, "enabled": True}]
        }))
        # Run 1 (counter 0): saves png
        res1 = subprocess.run([
            sys.executable, __file__,
            "--config", config_11,
            "--data-root", data_root_11,
            "--powershell", f"python3 {fake_ps_ok}"
        ], capture_output=True, text=True)
        assert res1.returncode == 0
        assert "png=1" in res1.stdout
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        t_dir_11 = os.path.join(data_root_11, today_str)
        pngs_run1 = [f for f in os.listdir(t_dir_11) if f.endswith(".png")]
        assert len(pngs_run1) == 1, "Run 1 must save PNG"
        state1 = load_state(data_root_11)
        assert state1["png_run_counter"] == 1

        # Run 2 (counter 1): does not save png
        res2 = subprocess.run([
            sys.executable, __file__,
            "--config", config_11,
            "--data-root", data_root_11,
            "--powershell", f"python3 {fake_ps_ok}"
        ], capture_output=True, text=True)
        assert res2.returncode == 0
        assert "png=0" in res2.stdout
        pngs_run2 = [f for f in os.listdir(t_dir_11) if f.endswith(".png")]
        assert len(pngs_run2) == 1, "Run 2 must NOT save PNG (still only 1 PNG)"
        state2 = load_state(data_root_11)
        assert state2["png_run_counter"] == 2
        cases_passed += 1

        sys.stdout.write(f"SELFTEST OK {cases_passed}/{total_cases}\n")
        return 0

    except Exception as e:
        sys.stderr.write(f"SELFTEST FAIL: {e}\n")
        return 1
    finally:
        if os.path.exists(test_root):
            try:
                shutil.rmtree(test_root)
            except Exception:
                pass
        tmp_parent = os.path.dirname(test_root)
        if os.path.exists(tmp_parent):
            try:
                if not os.listdir(tmp_parent):
                    os.rmdir(tmp_parent)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="Coinglass Liquidation Heatmap Archiver")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--dry-run", action="store_true", help="Perform dry run")
    parser.add_argument("--selftest", action="store_true", help="Run offline selftest")
    parser.add_argument("--prune-only", action="store_true", help="Run retention policy only")
    parser.add_argument("--data-root", default=None, help="Override data_root path")
    parser.add_argument("--ps-script", default=None, help="Override powershell script path")
    parser.add_argument("--staging-root", default=None, help="Explicit Windows-accessible sync root (no hardcoded username)")
    parser.add_argument("--powershell", default=None, help="Override powershell executable command")
    parser.add_argument("--run-id", default=None, help="Override run_id")
    args = parser.parse_args()

    if args.selftest:
        code = run_selftest()
        sys.exit(code)

    try:
        code = run_archive(args)
        sys.exit(code if code is not None else 0)
    except Exception as e:
        # Catch any unexpected top-level exception, log and exit 0
        data_root = args.data_root or "lab/data/heatmap-archive"
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        err_msg = f"Unexpected exception: {e}"
        sys.stdout.write(f"ERROR heatmap-archive status=internal_error detail={err_msg}\n")
        append_run_log(data_root, {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": "internal_error",
            "error": err_msg,
            "combos_total": 0,
            "combos_ok": 0,
            "combos_login_required": 0,
            "bytes_archived": 0,
            "elapsed_seconds": 0,
        })
    sys.exit(0)


if __name__ == "__main__":
    main()
