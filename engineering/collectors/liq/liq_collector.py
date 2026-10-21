#!/usr/bin/env python3
"""Binance USD-M forceOrder Public Stream Forward Collector.

Single-file Python 3.14 program conforming to SPEC.md.
All exit codes from main() are 0. Errors output as `ERROR <UTC ISO8601> <CODE> <message>`.
"""

import asyncio
from collections import OrderedDict
import datetime
from decimal import Decimal
import fcntl
import gzip
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse

DEFAULT_URL = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
DEFAULT_MAX_DAY_BYTES = 200 * 1024 * 1024  # 200 MB
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
VERSION = "1"


def now_utc_iso(ts=None):
    if ts is None:
        ts = time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ErrorLogger:
    """Logs error lines to stdout throttled to at most once per code every 300s."""

    def __init__(self, now_fn=None, throttle_seconds=300):
        self.now_fn = now_fn or time.time
        self.throttle_seconds = throttle_seconds
        self._last_printed = {}

    def log(self, code, msg, ts=None, force=False):
        current_time = self.now_fn() if ts is None else ts
        last_time = self._last_printed.get(code, 0)
        if force or (current_time - last_time >= self.throttle_seconds):
            self._last_printed[code] = current_time
            iso_str = now_utc_iso(current_time)
            line = f"ERROR {iso_str} {code} {msg}"
            print(line, flush=True)
            return line
        return None


def resolve_data_dir(cli_dir=None, script_path=None):
    """Resolve data directory according to SPEC section 1 and 6."""
    if cli_dir:
        return os.path.abspath(cli_dir)

    env_dir = os.environ.get("LIQ_DATA_DIR")
    if env_dir:
        return os.path.abspath(env_dir)

    if script_path is None:
        script_path = os.path.abspath(__file__)
    else:
        script_path = os.path.abspath(script_path)

    script_dir = os.path.normpath(os.path.dirname(script_path))

    # Case 1: <X>/lab/research/liq-collector
    if os.path.basename(script_dir) == "liq-collector":
        p_res = os.path.dirname(script_dir)
        if os.path.basename(p_res) == "research":
            p_lab = os.path.dirname(p_res)
            if os.path.basename(p_lab) == "lab":
                x_dir = os.path.dirname(p_lab)
                lab_dir = os.path.join(x_dir, "lab")
                if os.path.isdir(lab_dir):
                    return os.path.abspath(os.path.join(lab_dir, "data", "liq-collector"))

    # Case 2: <X>/scripts
    if os.path.basename(script_dir) == "scripts":
        x_dir = os.path.dirname(script_dir)
        lab_dir = os.path.join(x_dir, "lab")
        if os.path.isdir(lab_dir):
            return os.path.abspath(os.path.join(lab_dir, "data", "liq-collector"))

    return None  # NO_DEFAULT_IN_UNAUTHORIZED_DIR


def validate_url(url):
    """Validate URL. Refuse URLs on fstream.binance.com without '/market/'."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path or ""
        if ("fstream.binance.com" in host or host.endswith(".binance.com")) and "/market/" not in path:
            return False
        return True
    except Exception:
        return False


def calculate_backoff(k, is_rate_limited=False, jitter_ratio=None, backoff_scale=1.0):
    """Calculate backoff with exponential backoff and jitter."""
    base = min(60, 1 * (2 ** k))
    if jitter_ratio is None:
        jitter_ratio = random.uniform(0.0, 0.25)
    wait = base * (1.0 + jitter_ratio) * backoff_scale
    wait = max(1.0 * backoff_scale, wait)
    if is_rate_limited:
        wait = max(300.0, wait)
    return wait


def is_rate_limited_exception(exc):
    """Detect HTTP 429 or 418 via exception.response.status_code with fallback to HTTP 429/418 strings."""
    status_code = None
    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status_code", None)

    if status_code in (429, 418):
        return True
    if status_code is not None:
        return False

    exc_str = str(exc)
    if "HTTP 429" in exc_str or "HTTP 418" in exc_str:
        return True
    return False


def default_connect(url, **kwargs):
    """Default real websocket connection, lazily importing websockets."""
    import websockets

    return websockets.connect(url, **kwargs)


def default_read_cmdline(pid):
    """Read cmdline for a given pid from /proc/<pid>/cmdline."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
            return raw.decode("utf-8", errors="ignore").replace("\x00", " ")
    except Exception:
        return ""


def default_kill(pid, sig):
    """Default kill implementation."""
    os.kill(pid, sig)


def find_python_interpreter():
    """Find a python interpreter that has `websockets` installed."""
    candidates = [
        sys.executable,
        shutil.which("python3"),
        "/usr/bin/python3",
    ]
    for py in candidates:
        if not py or not os.path.exists(py):
            continue
        try:
            res = subprocess.run(
                [py, "-c", "import websockets"],
                capture_output=True,
                timeout=20,
            )
            if res.returncode == 0:
                return py
        except Exception:
            continue
    return None


def ensure_permissions(data_dir, error_logger=None, now_fn=None):
    """Ensure data_dir is 0700 and files inside are 0600. Log PERMS_LOOSE if loose."""
    now_fn = now_fn or time.time
    perms_loose = False

    if not os.path.exists(data_dir):
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        os.chmod(data_dir, 0o700)
    else:
        st = os.stat(data_dir)
        if (st.st_mode & 0o077) != 0:
            perms_loose = True
            os.chmod(data_dir, 0o700)

    try:
        for entry in os.listdir(data_dir):
            fpath = os.path.join(data_dir, entry)
            if os.path.isfile(fpath):
                st = os.stat(fpath)
                if (st.st_mode & 0o177) != 0:
                    perms_loose = True
                    os.chmod(fpath, 0o600)
    except Exception:
        pass

    if perms_loose and error_logger:
        error_logger.log("PERMS_LOOSE", "Loose directory or file permissions tightened", ts=now_fn())


def compress_old_files(data_dir, now_ts, error_logger=None):
    """Compress files with UTC date >= 2 days older than today into .gz (0600)."""
    today_dt = datetime.datetime.fromtimestamp(now_ts, tz=datetime.timezone.utc).date()
    if not os.path.exists(data_dir):
        return

    for fname in os.listdir(data_dir):
        # Match liq-YYYYMMDD.jsonl, heartbeat-YYYYMMDD.jsonl, reject-YYYYMMDD.jsonl
        is_liq = fname.startswith("liq-") and fname.endswith(".jsonl")
        is_hb = fname.startswith("heartbeat-") and fname.endswith(".jsonl")
        is_rej = fname.startswith("reject-") and fname.endswith(".jsonl")
        if not (is_liq or is_hb or is_rej):
            continue

        parts = fname[:-6].split("-")
        if len(parts) != 2:
            continue
        date_str = parts[1]
        try:
            file_dt = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        except Exception:
            continue

        if (today_dt - file_dt).days < 2:
            continue

        orig_path = os.path.join(data_dir, fname)
        tmp_gz_path = orig_path + ".gz.tmp"
        final_gz_path = orig_path + ".gz"

        try:
            with open(orig_path, "rb") as f_in:
                orig_bytes = f_in.read()

            orig_lines = orig_bytes.count(b"\n")
            orig_sha256 = hashlib.sha256(orig_bytes).hexdigest()

            # Write gz.tmp
            with gzip.open(tmp_gz_path, "wb") as f_out:
                f_out.write(orig_bytes)
            os.chmod(tmp_gz_path, 0o600)

            # Read back and verify
            with gzip.open(tmp_gz_path, "rb") as f_back:
                decomp_bytes = f_back.read()

            decomp_lines = decomp_bytes.count(b"\n")
            decomp_sha256 = hashlib.sha256(decomp_bytes).hexdigest()

            if decomp_lines == orig_lines and decomp_sha256 == orig_sha256:
                os.replace(tmp_gz_path, final_gz_path)
                os.chmod(final_gz_path, 0o600)
                os.remove(orig_path)
            else:
                if os.path.exists(tmp_gz_path):
                    os.remove(tmp_gz_path)
                if error_logger:
                    error_logger.log("UNEXPECTED", f"Compression verification failed for {fname}", ts=now_ts)
        except Exception as e:
            if os.path.exists(tmp_gz_path):
                try:
                    os.remove(tmp_gz_path)
                except Exception:
                    pass
            if error_logger:
                error_logger.log("UNEXPECTED", f"Compression error for {fname}: {type(e).__name__} {e}", ts=now_ts)


class Collector:
    """Binance forceOrder collector instance with dependency injection."""

    HEARTBEAT_INTERVAL_S = 60
    RECV_SLICE_S = 5
    BACKOFF_SCALE = 1.0

    def __init__(
        self,
        data_dir,
        url=DEFAULT_URL,
        max_day_bytes=DEFAULT_MAX_DAY_BYTES,
        max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
        now=None,
        sleep=None,
        connect=None,
        spawner=None,
        kill=None,
        read_cmdline=None,
    ):
        self.data_dir = os.path.abspath(data_dir)
        self.url = url
        self.max_day_bytes = max_day_bytes
        self.max_total_bytes = max_total_bytes
        self.now = now or time.time
        self.sleep = sleep or asyncio.sleep
        self.connect = connect or default_connect
        self.spawner = spawner or subprocess.Popen
        self.kill = kill or default_kill
        self.read_cmdline = read_cmdline or default_read_cmdline

        self.error_logger = ErrorLogger(now_fn=self.now)
        self.started_ms = int(self.now() * 1000)

        # Connection and state counters
        self.conn_id = 0
        self.connected = False
        self.events_total = 0
        self.events_today = 0
        today_str = self._get_utc_date_str(int(self.now() * 1000))
        today_file = os.path.join(self.data_dir, f"liq-{today_str}.jsonl")
        self.bytes_today = os.path.getsize(today_file) if os.path.exists(today_file) else 0
        self.rejected = 0
        self.rejected_dropped = 0
        self.dups = 0
        self.dropped_events = 0
        self.reconnects = 0
        self.silent_reconnects = 0
        self.planned_reconnects = 0
        self.last_recv_ms = None

        # Deduplication cache per connection (LRU up to 2000 entries)
        self.dedup_cache = OrderedDict()

        # File writing state
        self.current_date = None
        self.current_fd = None
        self.current_file_path = None
        self.events_since_fsync = 0
        self.last_fsync_time = 0
        self.last_heartbeat_time = 0

        self.stop_requested = False
        self._stop_event = asyncio.Event()
        self.heartbeat_task = None
        self.lock_fd = None

    async def _do_sleep(self, seconds):
        if self.stop_requested or seconds <= 0:
            return
        sleep_coro = self.sleep(seconds)
        if not (asyncio.iscoroutine(sleep_coro) or hasattr(sleep_coro, "__await__")):
            return
        sleep_task = asyncio.create_task(sleep_coro)
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, pending = await asyncio.wait(
            [sleep_task, stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except BaseException:
                pass

    async def _heartbeat_loop(self):
        """Independent heartbeat task writing every HEARTBEAT_INTERVAL_S regardless of connection state."""
        try:
            while not self.stop_requested:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.HEARTBEAT_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
                if not self.stop_requested:
                    self.heartbeat_tick()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error_logger.log("UNEXPECTED", f"Heartbeat loop error: {e}", ts=self.now())

    def acquire_lock(self):
        """Acquire exclusive flock on collector.lock."""
        ensure_permissions(self.data_dir, error_logger=self.error_logger, now_fn=self.now)
        lock_path = os.path.join(self.data_dir, "collector.lock")
        try:
            self.lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            os.chmod(lock_path, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self.lock_fd, 0)
            os.write(self.lock_fd, f"{os.getpid()}\n".encode("utf-8"))
            return True
        except (BlockingIOError, OSError):
            if self.lock_fd is not None:
                try:
                    os.close(self.lock_fd)
                except Exception:
                    pass
                self.lock_fd = None
            return False

    def release_lock(self):
        """Release exclusive flock."""
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
            except Exception:
                pass
            self.lock_fd = None

    def get_disk_bytes(self):
        """Calculate total bytes of all files in data_dir."""
        total = 0
        if not os.path.exists(self.data_dir):
            return 0
        try:
            for entry in os.listdir(self.data_dir):
                fpath = os.path.join(self.data_dir, entry)
                if os.path.isfile(fpath):
                    total += os.path.getsize(fpath)
        except Exception:
            pass
        return total

    def check_disk_free(self):
        """Check if disk free space is at least 2 GiB."""
        try:
            usage = shutil.disk_usage(self.data_dir)
            return usage.free >= 2 * 1024 * 1024 * 1024
        except Exception:
            return True

    def _get_utc_date_str(self, ts_ms):
        dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0, tz=datetime.timezone.utc)
        return dt.strftime("%Y%m%d")

    def _ensure_main_file(self, date_str):
        """Ensure current_fd points to liq-YYYYMMDD.jsonl for given date_str."""
        if self.current_date == date_str and self.current_fd is not None:
            return

        # Close old file
        if self.current_fd is not None:
            try:
                os.fsync(self.current_fd)
                os.close(self.current_fd)
            except Exception:
                pass
            self.current_fd = None

        self.current_date = date_str
        self.current_file_path = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")

        file_exists = os.path.exists(self.current_file_path)
        self.current_fd = os.open(self.current_file_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.chmod(self.current_file_path, 0o600)

        # Update bytes_today from existing file size
        self.bytes_today = os.path.getsize(self.current_file_path)
        if not file_exists:
            self.events_today = 0
        self.events_since_fsync = 0
        self.last_fsync_time = self.now()

    def _flush_main_file(self):
        if self.current_fd is not None:
            try:
                os.fsync(self.current_fd)
            except Exception:
                pass
            self.events_since_fsync = 0
            self.last_fsync_time = self.now()

    def write_reject(self, raw_text, reason, recv_ms):
        """Write rejected message to reject-YYYYMMDD.jsonl (max 5MB/day)."""
        date_str = self._get_utc_date_str(recv_ms)
        reject_path = os.path.join(self.data_dir, f"reject-{date_str}.jsonl")

        current_size = os.path.getsize(reject_path) if os.path.exists(reject_path) else 0
        if current_size >= 5 * 1024 * 1024:
            self.rejected_dropped += 1
            return

        record = {
            "raw": raw_text[:2000],
            "reason": str(reason),
            "recv_ms": recv_ms,
        }
        line_bytes = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

        try:
            fd = os.open(reject_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(reject_path, 0o600)
            os.write(fd, line_bytes)
            os.close(fd)
            self.rejected += 1
        except Exception as e:
            self.error_logger.log("WRITE_FAILED", f"Failed to write reject file: {e}", ts=self.now())

        self.error_logger.log("PARSE_REJECT", f"Rejected message: {reason}", ts=self.now())

    def parse_and_process_message(self, raw_text, recv_time):
        """Parse raw incoming message and write valid events or rejects."""
        recv_ms = int(recv_time * 1000)

        try:
            parsed = json.loads(raw_text)
        except Exception:
            self.write_reject(raw_text, "invalid_json", recv_ms)
            return

        # Handle wrapped or array formats
        events_to_process = []
        if isinstance(parsed, list):
            for item in parsed:
                events_to_process.append((item, json.dumps(item, separators=(",", ":"))))
        elif isinstance(parsed, dict):
            if "stream" in parsed and "data" in parsed:
                data = parsed["data"]
                if isinstance(data, list):
                    for item in data:
                        events_to_process.append((item, json.dumps(item, separators=(",", ":"))))
                elif isinstance(data, dict):
                    events_to_process.append((data, json.dumps(data, separators=(",", ":"))))
                else:
                    self.write_reject(raw_text, "invalid_stream_data", recv_ms)
                    return
            else:
                events_to_process.append((parsed, raw_text))
        else:
            self.write_reject(raw_text, "invalid_payload_type", recv_ms)
            return

        for payload, raw_str in events_to_process:
            self._process_single_event(payload, raw_str, recv_ms)

    def _process_single_event(self, payload, raw_str, recv_ms):
        """Validate and record single forceOrder event."""
        if not isinstance(payload, dict):
            self.write_reject(raw_str, "payload_not_dict", recv_ms)
            return

        if payload.get("e") != "forceOrder":
            self.write_reject(raw_str, f"invalid_event_type_{payload.get('e')}", recv_ms)
            return

        o = payload.get("o")
        if not isinstance(o, dict):
            self.write_reject(raw_str, "missing_or_invalid_o", recv_ms)
            return

        side = o.get("S")
        if side not in ("BUY", "SELL"):
            self.write_reject(raw_str, f"invalid_side_{side}", recv_ms)
            return

        # Required fields in o
        required_o = ("s", "S", "o", "f", "q", "p", "ap", "X", "l", "z", "T")
        for field in required_o:
            if field not in o:
                self.write_reject(raw_str, f"missing_field_o_{field}", recv_ms)
                return

        if "E" not in payload:
            self.write_reject(raw_str, "missing_field_E", recv_ms)
            return

        try:
            event_ms = int(payload["E"])
            trade_ms = int(o["T"])
        except Exception:
            self.write_reject(raw_str, "invalid_timestamps", recv_ms)
            return

        symbol = str(o["s"])
        filled_qty = str(o["z"])
        avg_price = str(o["ap"])

        # Deduplication: same millisecond (E), symbol (s), side (S), T, z, ap
        dedup_key = (event_ms, symbol, side, trade_ms, filled_qty, avg_price)
        if dedup_key in self.dedup_cache:
            self.dups += 1
            return
        self.dedup_cache[dedup_key] = True
        if len(self.dedup_cache) > 2000:
            self.dedup_cache.popitem(last=False)

        # Calculate notional using Decimal
        notional_str = None
        try:
            ap_dec = Decimal(str(avg_price))
            z_dec = Decimal(str(filled_qty))
            if ap_dec != 0:
                notional_val = ap_dec * z_dec
                notional_str = format(notional_val, "f")
            else:
                p_dec = Decimal(str(o["p"]))
                notional_val = p_dec * z_dec
                notional_str = format(notional_val, "f")
        except Exception:
            notional_str = None

        margin_type = o.get("st")
        if margin_type is not None:
            try:
                margin_type = int(margin_type)
            except Exception:
                margin_type = None

        recv_iso = now_utc_iso(recv_ms / 1000.0)

        record = {
            "avg_price": avg_price,
            "conn_id": self.conn_id,
            "event_ms": event_ms,
            "filled_qty": filled_qty,
            "last_fill_qty": str(o["l"]),
            "margin_type": margin_type,
            "notional": notional_str,
            "order_type": str(o["o"]),
            "orig_qty": str(o["q"]),
            "price": str(o["p"]),
            "raw": raw_str,
            "recv_iso": recv_iso,
            "recv_ms": recv_ms,
            "side": side,
            "status": str(o["X"]),
            "symbol": symbol,
            "tif": str(o["f"]),
            "trade_ms": trade_ms,
        }

        self._write_main_record(record, recv_ms)

    def _write_main_record(self, record, recv_ms):
        """Check caps and write valid record to main file."""
        line_bytes = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        rec_len = len(line_bytes)

        # Cap checks
        if self.bytes_today + rec_len > self.max_day_bytes:
            self.dropped_events += 1
            self.error_logger.log("DAY_CAP", f"Day cap exceeded ({self.bytes_today} bytes)", ts=self.now())
            return

        current_total = self.get_disk_bytes()
        if current_total + rec_len > self.max_total_bytes:
            self.dropped_events += 1
            self.error_logger.log("DISK_CAP", f"Total disk cap exceeded ({current_total} bytes)", ts=self.now())
            return

        if not self.check_disk_free():
            self.dropped_events += 1
            self.error_logger.log("DISK_CAP", "Disk free space is below 2 GiB", ts=self.now())
            return

        date_str = self._get_utc_date_str(recv_ms)
        self._ensure_main_file(date_str)

        try:
            os.write(self.current_fd, line_bytes)
            self.bytes_today += rec_len
            self.events_today += 1
            self.events_total += 1
            self.last_recv_ms = recv_ms
            self.events_since_fsync += 1

            if self.events_since_fsync >= 100 or (self.now() - self.last_fsync_time >= 5):
                self._flush_main_file()
        except Exception as e:
            self.error_logger.log("WRITE_FAILED", f"Failed to write record: {e}", ts=self.now())

    def heartbeat_tick(self):
        """Write one heartbeat line and update status.json atomically."""
        now_ts = self.now()
        ts_ms = int(now_ts * 1000)
        ts_iso = now_utc_iso(now_ts)

        last_age_s = None
        if self.last_recv_ms is not None:
            last_age_s = round((ts_ms - self.last_recv_ms) / 1000.0, 1)

        total_disk = self.get_disk_bytes()

        hb = {
            "bytes_today": self.bytes_today,
            "conn_id": self.conn_id,
            "connected": self.connected,
            "disk_bytes": total_disk,
            "dropped_events": self.dropped_events,
            "dups": self.dups,
            "events_today": self.events_today,
            "events_total": self.events_total,
            "last_recv_age_s": last_age_s,
            "last_recv_ms": self.last_recv_ms,
            "pid": os.getpid(),
            "planned_reconnects": self.planned_reconnects,
            "reconnects": self.reconnects,
            "rejected": self.rejected,
            "silent_reconnects": self.silent_reconnects,
            "ts_iso": ts_iso,
            "ts_ms": ts_ms,
            "type": "heartbeat",
            "version": VERSION,
        }

        # Write to heartbeat-YYYYMMDD.jsonl
        date_str = self._get_utc_date_str(ts_ms)
        hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
        line_bytes = (json.dumps(hb, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

        try:
            fd = os.open(hb_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(hb_path, 0o600)
            os.write(fd, line_bytes)
            os.close(fd)
        except Exception as e:
            self.error_logger.log("WRITE_FAILED", f"Failed to write heartbeat: {e}", ts=now_ts)

        # Atomic status.json update
        status_data = dict(hb)
        status_data["started_ms"] = self.started_ms
        tmp_status = os.path.join(self.data_dir, "status.json.tmp")
        final_status = os.path.join(self.data_dir, "status.json")
        try:
            with open(tmp_status, "w") as f:
                json.dump(status_data, f, separators=(",", ":"), sort_keys=True)
            os.chmod(tmp_status, 0o600)
            os.replace(tmp_status, final_status)
        except Exception as e:
            self.error_logger.log("WRITE_FAILED", f"Failed to update status.json: {e}", ts=now_ts)

        self.last_heartbeat_time = now_ts

    def write_heartbeat_event(self, event_type, **kwargs):
        """Write special event line (start, stop, connect, disconnect, error) to heartbeat file."""
        now_ts = self.now()
        ts_ms = int(now_ts * 1000)
        ts_iso = now_utc_iso(now_ts)

        record = {
            "ts_iso": ts_iso,
            "ts_ms": ts_ms,
            "type": event_type,
        }
        record.update(kwargs)

        date_str = self._get_utc_date_str(ts_ms)
        hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
        line_bytes = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

        try:
            fd = os.open(hb_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(hb_path, 0o600)
            os.write(fd, line_bytes)
            os.close(fd)
        except Exception:
            pass

    def stop(self, *args):
        """Request graceful shutdown of the collector."""
        self.stop_requested = True
        if hasattr(self, "_stop_event") and self._stop_event is not None:
            self._stop_event.set()

    async def run(self):
        """Run long-running collector until stop_requested."""
        if not validate_url(self.url):
            self.error_logger.log("URL_NOT_MARKET", f"Invalid URL for Binance USD-M forceOrder: {self.url}")
            return

        if not self.acquire_lock():
            # Another instance running, exit silently
            return

        ensure_permissions(self.data_dir, error_logger=self.error_logger, now_fn=self.now)
        compress_old_files(self.data_dir, self.now(), error_logger=self.error_logger)

        loop = asyncio.get_running_loop()
        has_loop_signals = False
        try:
            loop.add_signal_handler(signal.SIGTERM, self.stop)
            loop.add_signal_handler(signal.SIGINT, self.stop)
            has_loop_signals = True
        except (NotImplementedError, RuntimeError):
            def handle_signal(sig, frame):
                self.stop()

            signal.signal(signal.SIGTERM, handle_signal)
            signal.signal(signal.SIGINT, handle_signal)

        self.write_heartbeat_event(
            "start",
            argv=sys.argv,
            data_dir=self.data_dir,
            pid=os.getpid(),
            url=self.url,
            version=VERSION,
        )
        self.heartbeat_tick()

        # Start independent heartbeat task
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        consecutive_failures = 0
        last_daily_compress = self._get_utc_date_str(int(self.now() * 1000))

        try:
            while not self.stop_requested:
                current_day = self._get_utc_date_str(int(self.now() * 1000))
                if current_day != last_daily_compress:
                    compress_old_files(self.data_dir, self.now(), error_logger=self.error_logger)
                    last_daily_compress = current_day

                conn_start_time = self.now()
                self.conn_id += 1
                self.dedup_cache.clear()
                is_rate_limited = False
                disconnect_reason = "unknown"
                was_connected = False

                try:
                    connect_ctx = self.connect(
                        self.url,
                        open_timeout=15,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        max_size=1_048_576,
                    )

                    # Honour stop() during connect handshake
                    connect_task = asyncio.create_task(connect_ctx.__aenter__())
                    stop_task = asyncio.create_task(self._stop_event.wait())
                    done, pending = await asyncio.wait(
                        [connect_task, stop_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done:
                        connect_task.cancel()
                        try:
                            await connect_task
                        except (asyncio.CancelledError, Exception):
                            pass
                        break

                    stop_task.cancel()
                    try:
                        await stop_task
                    except (asyncio.CancelledError, Exception):
                        pass

                    ws = connect_task.result()

                    try:
                        self.connected = True
                        was_connected = True
                        self.write_heartbeat_event("connect", conn_id=self.conn_id, url=self.url)
                        last_event_time = self.now()
                        silent_warned = False

                        while not self.stop_requested:
                            # Stability check: reset backoff after 60s stable connection
                            if consecutive_failures > 0 and (self.now() - conn_start_time >= 60):
                                consecutive_failures = 0

                            # 24-hour planned reconnect (at 23 hours)
                            if self.now() - conn_start_time >= 23 * 3600:
                                self.planned_reconnects += 1
                                disconnect_reason = "planned_24h"
                                break

                            # Silent stream checks
                            silence_dur = self.now() - last_event_time
                            if silence_dur >= 900:
                                self.silent_reconnects += 1
                                self.error_logger.log("SILENT_STREAM", "No events for 900s, reconnecting", ts=self.now())
                                disconnect_reason = "silent_900s"
                                break
                            elif silence_dur >= 300 and not silent_warned:
                                silent_warned = True
                                self.error_logger.log("SILENT_STREAM", "No events received for 300s", ts=self.now())

                            # Recv message: race recv against self._stop_event.wait() with asyncio.wait(FIRST_COMPLETED)
                            # keep RECV_SLICE_S timeout only for watchdog checks
                            recv_task = asyncio.ensure_future(ws.recv())
                            stop_task = asyncio.ensure_future(self._stop_event.wait())
                            done, pending = await asyncio.wait(
                                [recv_task, stop_task],
                                timeout=self.RECV_SLICE_S,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if stop_task in done or self.stop_requested or self._stop_event.is_set():
                                recv_task.cancel()
                                stop_task.cancel()
                                for t in (recv_task, stop_task):
                                    try:
                                        await t
                                    except BaseException:
                                        pass
                                break

                            if recv_task in done:
                                stop_task.cancel()
                                try:
                                    await stop_task
                                except BaseException:
                                    pass
                                msg = recv_task.result()
                                recv_time = self.now()
                                self.parse_and_process_message(msg, recv_time)
                                last_event_time = recv_time
                                silent_warned = False
                            else:
                                # RECV_SLICE_S timeout for watchdog checks: cancel recv_task for this slice
                                recv_task.cancel()
                                stop_task.cancel()
                                for t in (recv_task, stop_task):
                                    try:
                                        await t
                                    except BaseException:
                                        pass
                    finally:
                        try:
                            await connect_ctx.__aexit__(*sys.exc_info())
                        except Exception:
                            pass

                except Exception as exc:
                    disconnect_reason = f"exception_{type(exc).__name__}"
                    if not was_connected:
                        if is_rate_limited_exception(exc):
                            is_rate_limited = True
                            self.error_logger.log("RATE_LIMITED", f"Server rate limited: {exc}", ts=self.now())
                        else:
                            self.error_logger.log("CONNECT_FAILED", f"Connection failed: {exc}", ts=self.now())
                    else:
                        if not self.stop_requested and disconnect_reason not in ("planned_24h", "shutdown"):
                            self.error_logger.log("DISCONNECTED", f"Disconnected unexpectedly: {exc}", ts=self.now())

                finally:
                    dur = round(self.now() - conn_start_time, 2)
                    self.connected = False
                    if self.stop_requested:
                        disconnect_reason = "shutdown"
                    self.write_heartbeat_event(
                        "disconnect",
                        conn_id=self.conn_id,
                        duration_s=dur,
                        reason=disconnect_reason,
                    )
                    if disconnect_reason not in ("planned_24h", "shutdown"):
                        self.reconnects += 1

                if self.stop_requested:
                    break

                if disconnect_reason == "planned_24h":
                    # Reconnect within 2 seconds, no backoff
                    await self._do_sleep(min(1.0, 1.0 * self.BACKOFF_SCALE))
                else:
                    wait_secs = calculate_backoff(
                        consecutive_failures,
                        is_rate_limited=is_rate_limited,
                        backoff_scale=self.BACKOFF_SCALE,
                    )
                    consecutive_failures += 1
                    await self._do_sleep(wait_secs)

        finally:
            if has_loop_signals:
                try:
                    loop.remove_signal_handler(signal.SIGTERM)
                    loop.remove_signal_handler(signal.SIGINT)
                except Exception:
                    pass

            if self.heartbeat_task is not None:
                self.heartbeat_task.cancel()
                try:
                    await self.heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass

            self._flush_main_file()
            if self.current_fd is not None:
                try:
                    os.close(self.current_fd)
                except Exception:
                    pass
                self.current_fd = None

            self.write_heartbeat_event("stop", pid=os.getpid(), reason="shutdown")
            self.heartbeat_tick()
            self.release_lock()


def guard(
    data_dir=None,
    spawner=None,
    kill=None,
    read_cmdline=None,
    now=None,
    error_logger=None,
):
    """Guard routine called every minute by cron."""
    now_fn = now or time.time
    error_log = error_logger or ErrorLogger(now_fn=now_fn)

    target_data_dir = resolve_data_dir(data_dir)
    if not target_data_dir:
        error_log.log("NO_DATA_DIR", "Cannot resolve data directory", ts=now_fn())
        return

    ensure_permissions(target_data_dir, error_logger=error_log, now_fn=now_fn)

    spawner_fn = spawner or subprocess.Popen
    kill_fn = kill or default_kill
    read_cmdline_fn = read_cmdline or default_read_cmdline

    lock_path = os.path.join(target_data_dir, "collector.lock")
    lock_fd = None
    is_locked = False

    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Lock acquired -> daemon is not running
        is_locked = False
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        lock_fd = None
    except (BlockingIOError, OSError):
        # Lock is held -> daemon is running
        is_locked = True
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
            lock_fd = None

    if not is_locked:
        # Check if restarted unexpectedly
        status_path = os.path.join(target_data_dir, "status.json")
        has_abrupt_crash = False
        if os.path.exists(status_path):
            try:
                with open(status_path, "r") as sf:
                    st_data = json.load(sf)
                # Check recent heartbeat file to see if last event was stop
                today_str = datetime.datetime.fromtimestamp(now_fn(), tz=datetime.timezone.utc).strftime("%Y%m%d")
                hb_path = os.path.join(target_data_dir, f"heartbeat-{today_str}.jsonl")
                last_event_type = None
                if os.path.exists(hb_path):
                    with open(hb_path, "r") as hf:
                        for line in hf:
                            try:
                                d = json.loads(line)
                                if d.get("type") in ("start", "stop"):
                                    last_event_type = d.get("type")
                            except Exception:
                                pass
                if last_event_type == "start":
                    has_abrupt_crash = True
            except Exception:
                pass

        if has_abrupt_crash:
            error_log.log("RESTARTED", "Collector restarted unexpectedly after crash", ts=now_fn())

        # Record start_requested in today's heartbeat log
        today_str = datetime.datetime.fromtimestamp(now_fn(), tz=datetime.timezone.utc).strftime("%Y%m%d")
        hb_path = os.path.join(target_data_dir, f"heartbeat-{today_str}.jsonl")
        req_rec = {
            "pid": os.getpid(),
            "ts_iso": now_utc_iso(now_fn()),
            "ts_ms": int(now_fn() * 1000),
            "type": "start_requested",
        }
        line_bytes = (json.dumps(req_rec, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        try:
            fd = os.open(hb_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.chmod(hb_path, 0o600)
            os.write(fd, line_bytes)
            os.close(fd)
        except Exception:
            pass

        # Find python interpreter with websockets
        py = find_python_interpreter()
        if not py:
            error_log.log("NO_WEBSOCKETS", "No python interpreter with websockets found", ts=now_fn())
            return

        # Prepare collector.out with max 5MB rotation
        out_path = os.path.join(target_data_dir, "collector.out")
        if os.path.exists(out_path) and os.path.getsize(out_path) > 5 * 1024 * 1024:
            try:
                os.replace(out_path, out_path + ".1")
            except Exception:
                pass

        out_fd = os.open(out_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.chmod(out_path, 0o600)

        script_path = os.path.abspath(__file__)
        cmd = [py, script_path, "--run", "--data-dir", target_data_dir]
        try:
            spawner_fn(cmd, start_new_session=True, stdout=out_fd, stderr=out_fd)
        finally:
            try:
                os.close(out_fd)
            except Exception:
                pass

    else:
        # Daemon is running, inspect status.json for staleness
        status_path = os.path.join(target_data_dir, "status.json")
        guard_state_path = os.path.join(target_data_dir, "guard_state.json")
        now_ms = int(now_fn() * 1000)

        if not os.path.exists(status_path):
            return

        try:
            with open(status_path, "r") as sf:
                st_data = json.load(sf)
        except Exception:
            return

        ts_ms = st_data.get("ts_ms", 0)
        daemon_pid = st_data.get("pid")
        age_s = (now_ms - ts_ms) / 1000.0

        if age_s > 180:
            error_log.log("STALE_DAEMON", f"Stale daemon detected (last heartbeat {age_s:.1f}s ago)", ts=now_fn())

            prev_stale = False
            if os.path.exists(guard_state_path):
                try:
                    with open(guard_state_path, "r") as gf:
                        g_data = json.load(gf)
                        if g_data.get("stale_pid") == daemon_pid:
                            prev_stale = True
                except Exception:
                    pass

            if prev_stale and daemon_pid:
                # Verify cmdline
                cmdline = read_cmdline_fn(daemon_pid)
                if "liq_collector.py" in cmdline and "--run" in cmdline:
                    try:
                        kill_fn(daemon_pid, signal.SIGTERM)
                    except Exception:
                        pass
                    if os.path.exists(guard_state_path):
                        try:
                            os.remove(guard_state_path)
                        except Exception:
                            pass
            else:
                # Record stale state
                tmp_gs = guard_state_path + ".tmp"
                try:
                    with open(tmp_gs, "w") as gf:
                        json.dump({"detected_ms": now_ms, "stale_pid": daemon_pid}, gf)
                    os.chmod(tmp_gs, 0o600)
                    os.replace(tmp_gs, guard_state_path)
                except Exception:
                    pass
        else:
            if os.path.exists(guard_state_path):
                try:
                    os.remove(guard_state_path)
                except Exception:
                    pass


def status(data_dir=None):
    """Print one-line JSON status summary."""
    target_data_dir = resolve_data_dir(data_dir)
    if not target_data_dir or not os.path.exists(target_data_dir):
        res = {"alive": False, "disk_bytes": 0, "events_today": 0, "last_recv_ms": None, "pid": None}
        print(json.dumps(res, separators=(",", ":"), sort_keys=True))
        return

    # Check lock
    lock_path = os.path.join(target_data_dir, "collector.lock")
    is_alive = False
    lock_fd = None
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Not locked
        is_alive = False
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    except (BlockingIOError, OSError):
        is_alive = True
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass

    status_path = os.path.join(target_data_dir, "status.json")
    status_data = {}
    if os.path.exists(status_path):
        try:
            with open(status_path, "r") as f:
                status_data = json.load(f)
        except Exception:
            pass

    total_disk = 0
    try:
        for entry in os.listdir(target_data_dir):
            fpath = os.path.join(target_data_dir, entry)
            if os.path.isfile(fpath):
                total_disk += os.path.getsize(fpath)
    except Exception:
        pass

    summary = {
        "alive": is_alive,
        "disk_bytes": total_disk,
        "events_today": status_data.get("events_today", 0),
        "last_recv_ms": status_data.get("last_recv_ms"),
        "pid": status_data.get("pid") if is_alive else None,
    }
    print(json.dumps(summary, separators=(",", ":"), sort_keys=True))


def selfcheck(data_dir=None):
    """Perform offline self-check writing a sample event and reading it back."""
    import tempfile

    error_log = ErrorLogger()
    temp_dir = None
    try:
        temp_dir = tempfile.TemporaryDirectory()
        check_dir = temp_dir.name

        sample_event = (
            '{"e":"forceOrder","E":1792429790103,"o":{"s":"CTRUSDT","S":"SELL","o":"LIMIT",'
            '"f":"IOC","q":"42242","p":"0.0157390","ap":"0.0160511","X":"FILLED","l":"22106",'
            '"z":"42242","T":1792429789173,"ps":"CTRUSDT","st":1}}'
        )

        col = Collector(data_dir=check_dir)
        ensure_permissions(check_dir)
        t_recv = 1792429790.103
        col.parse_and_process_message(sample_event, t_recv)
        col._flush_main_file()

        # Check file
        date_str = col._get_utc_date_str(int(t_recv * 1000))
        target_file = os.path.join(check_dir, f"liq-{date_str}.jsonl")
        if not os.path.exists(target_file):
            error_log.log("SELFCHECK_FAILED", "Output file not created")
            return

        st = os.stat(target_file)
        if (st.st_mode & 0o777) != 0o600:
            error_log.log("SELFCHECK_FAILED", f"Bad file mode: {oct(st.st_mode)}")
            return

        with open(target_file, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        if len(lines) != 1:
            error_log.log("SELFCHECK_FAILED", f"Expected 1 line, got {len(lines)}")
            return

        rec = lines[0]
        if rec["symbol"] != "CTRUSDT" or rec["side"] != "SELL" or rec["price"] != "0.0157390":
            error_log.log("SELFCHECK_FAILED", "Record content mismatch")
            return

        print("OK")
    except Exception as e:
        error_log.log("SELFCHECK_FAILED", f"Exception during selfcheck: {e}")
    finally:
        if temp_dir is not None:
            try:
                temp_dir.cleanup()
            except Exception:
                pass


def main(argv=None):
    """Main CLI entrypoint. Always returns exit code 0."""
    if argv is None:
        argv = sys.argv[1:]

    error_log = ErrorLogger()
    try:
        mode = "guard"
        data_dir = None
        url = DEFAULT_URL
        max_day_bytes = DEFAULT_MAX_DAY_BYTES
        max_total_bytes = DEFAULT_MAX_TOTAL_BYTES

        idx = 0
        while idx < len(argv):
            arg = argv[idx]
            if arg == "--run":
                mode = "run"
            elif arg == "--guard":
                mode = "guard"
            elif arg == "--status":
                mode = "status"
            elif arg == "--selfcheck":
                mode = "selfcheck"
            elif arg == "--data-dir":
                idx += 1
                if idx < len(argv):
                    data_dir = argv[idx]
                else:
                    error_log.log("ARG_ERROR", "Missing value for --data-dir")
                    return 0
            elif arg == "--url":
                idx += 1
                if idx < len(argv):
                    url = argv[idx]
                else:
                    error_log.log("ARG_ERROR", "Missing value for --url")
                    return 0
            elif arg == "--max-day-bytes":
                idx += 1
                if idx < len(argv):
                    try:
                        max_day_bytes = int(argv[idx])
                    except ValueError:
                        error_log.log("ARG_ERROR", f"Invalid integer for --max-day-bytes: {argv[idx]}")
                        return 0
                else:
                    error_log.log("ARG_ERROR", "Missing value for --max-day-bytes")
                    return 0
            elif arg == "--max-total-bytes":
                idx += 1
                if idx < len(argv):
                    try:
                        max_total_bytes = int(argv[idx])
                    except ValueError:
                        error_log.log("ARG_ERROR", f"Invalid integer for --max-total-bytes: {argv[idx]}")
                        return 0
                else:
                    error_log.log("ARG_ERROR", "Missing value for --max-total-bytes")
                    return 0
            else:
                error_log.log("ARG_ERROR", f"Unknown argument: {arg}")
                return 0  # EXIT_ON_ARG_ERROR
            idx += 1

        if mode == "selfcheck":
            selfcheck(data_dir=data_dir)
            return 0

        if mode == "status":
            status(data_dir=data_dir)
            return 0

        resolved_dir = resolve_data_dir(data_dir)
        if not resolved_dir:
            error_log.log("NO_DATA_DIR", "Data directory could not be resolved")
            return 0

        if mode == "guard":
            guard(data_dir=resolved_dir)
            return 0

        if mode == "run":
            if not validate_url(url):
                error_log.log("URL_NOT_MARKET", f"Invalid URL for Binance USD-M forceOrder: {url}")
                return 0

            collector = Collector(
                data_dir=resolved_dir,
                url=url,
                max_day_bytes=max_day_bytes,
                max_total_bytes=max_total_bytes,
            )
            asyncio.run(collector.run())
            return 0

    except SystemExit:
        return 0
    except BaseException as e:
        error_log.log("UNEXPECTED", f"{type(e).__name__}: {e}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
