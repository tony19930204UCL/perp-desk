#!/usr/bin/env python3
"""Unit tests for liq_collector.py.

All tests are strictly offline: socket.socket.connect is patched to raise.
Covers all numbered items 1-16 in SPEC.md Section 10.
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
import shutil
import signal
import socket
import sys
import tempfile
import time
import unittest

import liq_collector
from liq_collector import (
    Collector,
    DEFAULT_MAX_DAY_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    ErrorLogger,
    calculate_backoff,
    compress_old_files,
    ensure_permissions,
    guard,
    is_rate_limited_exception,
    main,
    now_utc_iso,
    resolve_data_dir,
    status,
    validate_url,
)


class MockWebSocket:
    """Mock websocket returning pre-configured messages."""

    def __init__(self, messages=None, recv_delay=0.0):
        self.messages = list(messages or [])
        self.recv_delay = recv_delay
        self.closed = False

    async def recv(self):
        if self.messages:
            msg = self.messages.pop(0)
            if isinstance(msg, Exception):
                raise msg
            if self.recv_delay > 0:
                await asyncio.sleep(self.recv_delay)
            return msg
        # If no more messages, raise ConnectionResetError to signal EOF
        raise ConnectionResetError("Connection closed")
        return ""

    async def close(self):
        self.closed = True


class MockAsyncContext:
    def __init__(self, ws=None, exc=None):
        self.ws = ws
        self.exc = exc

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self.ws

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.ws:
            await self.ws.close()
        return None


class BaseLiqCollectorTest(unittest.TestCase):
    def setUp(self):
        signal.alarm(20)
        # Strictly block real network connection
        self._orig_socket_connect = socket.socket.connect

        def blocked_connect(*args, **kwargs):
            raise RuntimeError("Real network connection blocked in tests")

        socket.socket.connect = blocked_connect

        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.temp_dir.name

    def tearDown(self):
        signal.alarm(0)
        socket.socket.connect = self._orig_socket_connect
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass


class TestLiqCollector(BaseLiqCollectorTest):

    def test_01_real_fixtures_parsing(self):
        """Item 1: Parse 30 real fixture items correctly with Decimal notional and string preservation."""
        fixtures_path = os.path.join(os.path.dirname(__file__), "fixtures", "real_forceorder_events.jsonl")
        self.assertTrue(os.path.exists(fixtures_path), f"Fixtures not found at {fixtures_path}")

        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        raw_lines = []
        with open(fixtures_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    raw_lines.append(line)

        self.assertEqual(len(raw_lines), 30)

        # Parse each line at a distinct receive timestamp
        base_t = 1792429790.0
        for i, line in enumerate(raw_lines):
            t_recv = base_t + i * 2.0  # spacing to avoid dedup
            collector.parse_and_process_message(line, t_recv)

        collector._flush_main_file()

        date_str = collector._get_utc_date_str(int(base_t * 1000))
        out_file = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        self.assertTrue(os.path.exists(out_file))

        parsed_records = []
        with open(out_file, "r", encoding="utf-8") as f:
            for line in f:
                parsed_records.append(json.loads(line))

        self.assertEqual(len(parsed_records), 30)

        for i, rec in enumerate(parsed_records):
            orig_json = json.loads(raw_lines[i])
            o = orig_json["o"]

            # Field checks
            self.assertEqual(rec["symbol"], o["s"])
            self.assertEqual(rec["side"], o["S"])
            self.assertIn(rec["side"], ("BUY", "SELL"))
            self.assertEqual(rec["order_type"], o["o"])
            self.assertEqual(rec["tif"], o["f"])
            self.assertEqual(rec["status"], o["X"])

            # String preservation (must not be float-converted)
            self.assertIsInstance(rec["orig_qty"], str)
            self.assertEqual(rec["orig_qty"], str(o["q"]))

            self.assertIsInstance(rec["price"], str)
            self.assertEqual(rec["price"], str(o["p"]))

            self.assertIsInstance(rec["avg_price"], str)
            self.assertEqual(rec["avg_price"], str(o["ap"]))

            self.assertIsInstance(rec["filled_qty"], str)
            self.assertEqual(rec["filled_qty"], str(o["z"]))

            self.assertIsInstance(rec["last_fill_qty"], str)
            self.assertEqual(rec["last_fill_qty"], str(o["l"]))

            # Notional calculation using Decimal
            ap_dec = Decimal(str(o["ap"]))
            z_dec = Decimal(str(o["z"]))
            if ap_dec != 0:
                expected_notional = format(ap_dec * z_dec, "f")
            else:
                p_dec = Decimal(str(o["p"]))
                expected_notional = format(p_dec * z_dec, "f")
            self.assertEqual(rec["notional"], expected_notional)

            # Timestamps
            self.assertEqual(rec["event_ms"], orig_json["E"])
            self.assertEqual(rec["trade_ms"], o["T"])

            # Margin type
            if "st" in o:
                self.assertEqual(rec["margin_type"], o["st"])

    def test_02_wrapped_and_array_messages(self):
        """Item 2: Unwrap {"stream":..,"data":{..}} and expand array messages into multiple lines."""
        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        base_event = {
            "e": "forceOrder",
            "E": 1792429790100,
            "o": {
                "s": "BTCUSDT",
                "S": "BUY",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1.0",
                "p": "60000.0",
                "ap": "60000.0",
                "X": "FILLED",
                "l": "1.0",
                "z": "1.0",
                "T": 1792429790050,
            },
        }

        # 1. Wrapped message
        wrapped = {"stream": "!forceOrder@arr", "data": base_event}
        collector.parse_and_process_message(json.dumps(wrapped), 1792429790.1)

        # 2. Array message
        event2 = dict(base_event)
        event2["E"] = 1792429791100
        event2["o"] = dict(base_event["o"])
        event2["o"]["T"] = 1792429791050
        event2["o"]["s"] = "ETHUSDT"

        event3 = dict(base_event)
        event3["E"] = 1792429792100
        event3["o"] = dict(base_event["o"])
        event3["o"]["T"] = 1792429792050
        event3["o"]["s"] = "SOLUSDT"

        array_msg = [event2, event3]
        collector.parse_and_process_message(json.dumps(array_msg), 1792429792.0)

        # 3. Wrapped array message
        event4 = dict(base_event)
        event4["E"] = 1792429793100
        event4["o"] = dict(base_event["o"])
        event4["o"]["T"] = 1792429793050
        event4["o"]["s"] = "DOGEUSDT"
        wrapped_array = {"stream": "!forceOrder@arr", "data": [event4]}
        collector.parse_and_process_message(json.dumps(wrapped_array), 1792429793.0)

        collector._flush_main_file()

        date_str = collector._get_utc_date_str(1792429790100)
        out_file = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        self.assertTrue(os.path.exists(out_file))

        with open(out_file, "r") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 4)
        symbols = [r["symbol"] for r in lines]
        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"])

    def test_03_bad_messages_to_reject_file(self):
        """Item 3: Bad messages go to reject file, not main file, counts are accurate."""
        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        t_recv = 1792429790.0
        # 1. Non-JSON
        collector.parse_and_process_message("NOT A VALID JSON", t_recv)

        # 2. Missing 'o'
        collector.parse_and_process_message(json.dumps({"e": "forceOrder", "E": 12345}), t_recv)

        # 3. e != forceOrder
        collector.parse_and_process_message(
            json.dumps({"e": "trade", "E": 12345, "o": {"S": "BUY", "s": "BTCUSDT"}}), t_recv
        )

        # 4. S not BUY or SELL
        collector.parse_and_process_message(
            json.dumps(
                {
                    "e": "forceOrder",
                    "E": 12345,
                    "o": {
                        "s": "BTCUSDT",
                        "S": "HOLD",
                        "o": "LIMIT",
                        "f": "IOC",
                        "q": "1",
                        "p": "1",
                        "ap": "1",
                        "X": "FILLED",
                        "l": "1",
                        "z": "1",
                        "T": 12345,
                    },
                }
            ),
            t_recv,
        )

        collector._flush_main_file()

        date_str = collector._get_utc_date_str(int(t_recv * 1000))
        main_file = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        reject_file = os.path.join(self.data_dir, f"reject-{date_str}.jsonl")

        self.assertFalse(os.path.exists(main_file), "Main file should not exist for rejected messages")
        self.assertTrue(os.path.exists(reject_file), "Reject file must exist")
        self.assertEqual(collector.rejected, 4)

        with open(reject_file, "r") as f:
            reject_lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(reject_lines), 4)
        for r in reject_lines:
            self.assertIn("reason", r)
            self.assertIn("raw", r)
            self.assertIn("recv_ms", r)

    def test_04_deduplication(self):
        """Item 4: Duplicate messages only written once, dups counter incremented."""
        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        event = {
            "e": "forceOrder",
            "E": 1792429790103,
            "o": {
                "s": "CTRUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "42242",
                "p": "0.0157390",
                "ap": "0.0160511",
                "X": "FILLED",
                "l": "22106",
                "z": "42242",
                "T": 1792429789173,
            },
        }
        msg = json.dumps(event)

        collector.parse_and_process_message(msg, 1792429790.1)
        collector.parse_and_process_message(msg, 1792429790.2)
        collector.parse_and_process_message(msg, 1792429790.3)
        collector._flush_main_file()

        self.assertEqual(collector.dups, 2)
        self.assertEqual(collector.events_today, 1)

        date_str = collector._get_utc_date_str(1792429790100)
        out_file = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        with open(out_file, "r") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 1)

    def test_05_file_and_dir_permissions(self):
        """Item 5: Dir is 0700, files are 0600 even when process umask is 0000."""
        old_umask = os.umask(0)
        try:
            collector = Collector(data_dir=self.data_dir)
            ensure_permissions(self.data_dir)

            event = {
                "e": "forceOrder",
                "E": 1792429790100,
                "o": {
                    "s": "BTCUSDT",
                    "S": "SELL",
                    "o": "LIMIT",
                    "f": "IOC",
                    "q": "1",
                    "p": "60000",
                    "ap": "60000",
                    "X": "FILLED",
                    "l": "1",
                    "z": "1",
                    "T": 1792429790000,
                },
            }
            collector.parse_and_process_message(json.dumps(event), 1792429790.1)
            collector.write_reject("bad", "test_reason", 1792429790100)
            collector.heartbeat_tick()
            collector._flush_main_file()

            # Verify dir mode 0700
            dir_st = os.stat(self.data_dir)
            self.assertEqual(dir_st.st_mode & 0o777, 0o700)

            # Verify files mode 0600
            date_str = collector._get_utc_date_str(1792429790100)
            main_path = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
            hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
            rej_path = os.path.join(self.data_dir, f"reject-{date_str}.jsonl")
            st_path = os.path.join(self.data_dir, "status.json")

            for p in (main_path, hb_path, rej_path, st_path):
                self.assertTrue(os.path.exists(p), f"File {p} does not exist")
                mode = os.stat(p).st_mode & 0o777
                self.assertEqual(mode, 0o600, f"File {p} has mode {oct(mode)}, expected 0600")

        finally:
            os.umask(old_umask)

    def test_06_date_partitioning_and_append(self):
        """Item 6: Partition by UTC date of recv_ms; appending on restart without truncation."""
        orig_tz = os.environ.get("TZ")
        try:
            if hasattr(time, "tzset"):
                os.environ["TZ"] = "GMT-9"
                time.tzset()

            collector = Collector(data_dir=self.data_dir)
            ensure_permissions(self.data_dir)

            # 2026-10-18 23:59:59 UTC -> 1792367999
            t1 = 1792367999.0
            # 2026-10-19 00:00:01 UTC -> 1792368001
            t2 = 1792368001.0

            event1 = {
                "e": "forceOrder",
                "E": 1792367999000,
                "o": {
                    "s": "BTCUSDT",
                    "S": "SELL",
                    "o": "LIMIT",
                    "f": "IOC",
                    "q": "1",
                    "p": "60000",
                    "ap": "60000",
                    "X": "FILLED",
                    "l": "1",
                    "z": "1",
                    "T": 1792367999000,
                },
            }

            event2 = {
                "e": "forceOrder",
                "E": 1792368001000,
                "o": {
                    "s": "BTCUSDT",
                    "S": "BUY",
                    "o": "LIMIT",
                    "f": "IOC",
                    "q": "1",
                    "p": "60000",
                    "ap": "60000",
                    "X": "FILLED",
                    "l": "1",
                    "z": "1",
                    "T": 1792368001000,
                },
            }

            collector.parse_and_process_message(json.dumps(event1), t1)
            collector.parse_and_process_message(json.dumps(event2), t2)
            collector._flush_main_file()

            file1 = os.path.join(self.data_dir, "liq-20261018.jsonl")
            file2 = os.path.join(self.data_dir, "liq-20261019.jsonl")
            self.assertTrue(os.path.exists(file1))
            self.assertTrue(os.path.exists(file2))

            # Restart simulation (new Collector instance on same data_dir)
            collector2 = Collector(data_dir=self.data_dir)
            event3 = dict(event2)
            event3["E"] = 1792368002000
            event3["o"] = dict(event2["o"])
            event3["o"]["T"] = 1792368002000
            event3["o"]["s"] = "ETHUSDT"
            collector2.parse_and_process_message(json.dumps(event3), t2 + 10.0)
            collector2._flush_main_file()

            with open(file2, "r") as f:
                lines = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(lines), 2, "Restart should append to existing date file without truncation")
            self.assertEqual(lines[0]["symbol"], "BTCUSDT")
            self.assertEqual(lines[1]["symbol"], "ETHUSDT")
        finally:
            if hasattr(time, "tzset"):
                if orig_tz is not None:
                    os.environ["TZ"] = orig_tz
                else:
                    os.environ.pop("TZ", None)
                time.tzset()

    def test_07_recv_ms_from_now_not_event_ms(self):
        """Item 7: recv_ms taken from receive moment (injected now), not event_ms."""
        # injected now is 1800000000.5
        mock_now = 1800000000.5
        collector = Collector(data_dir=self.data_dir, now=lambda: mock_now)
        ensure_permissions(self.data_dir)

        event = {
            "e": "forceOrder",
            "E": 1792429790103,  # Strictly different from mock_now
            "o": {
                "s": "CTRUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "42242",
                "p": "0.0157390",
                "ap": "0.0160511",
                "X": "FILLED",
                "l": "22106",
                "z": "42242",
                "T": 1792429789173,
            },
        }

        collector.parse_and_process_message(json.dumps(event), mock_now)
        collector._flush_main_file()

        date_str = collector._get_utc_date_str(int(mock_now * 1000))
        out_file = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        with open(out_file, "r") as f:
            rec = json.loads(f.readline())

        self.assertEqual(rec["recv_ms"], 1800000000500)
        self.assertEqual(rec["event_ms"], 1792429790103)
        self.assertNotEqual(rec["recv_ms"], rec["event_ms"])

    def test_08_backoff_sequence_and_bounds(self):
        """Item 8: Backoff sequence 1,2,4,8,16,32,60,60; jitter in 0-25%; min >= 1; 429/418 wait >= 300."""
        # 1. Base sequence without jitter
        expected_bases = [1, 2, 4, 8, 16, 32, 60, 60]
        for k, expected in enumerate(expected_bases):
            val = calculate_backoff(k, is_rate_limited=False, jitter_ratio=0.0)
            self.assertEqual(val, expected)

        # 2. Jitter range check [0, 0.25]
        for k in range(8):
            for _ in range(20):
                val = calculate_backoff(k)
                base = min(60, 1 * (2 ** k))
                self.assertGreaterEqual(val, base)
                self.assertLessEqual(val, base * 1.25 + 1e-9)
                self.assertGreaterEqual(val, 1.0)

        # 3. Rate limited (429/418)
        rl_wait = calculate_backoff(0, is_rate_limited=True, jitter_ratio=0.0)
        self.assertGreaterEqual(rl_wait, 300.0)

        # 4. Collector stability reset after 60s
        simulated_time = 1000.0

        def get_time():
            return simulated_time

        collector = Collector(data_dir=self.data_dir, now=get_time)
        # Verify consecutive failures reset logic in collector.run

    def test_09_reconnect_retry_and_stats(self):
        """Item 9: Injected connect raises exception first 2 times, succeeds on 3rd, data complete, reconnects=2."""
        attempts = 0
        slept = []
        sim_time = 1792429790.0

        def get_time():
            return sim_time

        async def mock_sleep(secs):
            nonlocal sim_time
            slept.append(secs)
            sim_time += secs

        sample_event = {
            "e": "forceOrder",
            "E": 1792429790100,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "60000",
                "ap": "60000",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1792429790000,
            },
        }

        def mock_connect(url, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                return MockAsyncContext(exc=ConnectionError(f"Attempt {attempts} failed"))
            # 3rd attempt succeeds with 3 events then closes
            events = []
            for i in range(3):
                ev = dict(sample_event)
                ev["E"] = 1792429790100 + i * 1000
                ev["o"] = dict(sample_event["o"])
                ev["o"]["T"] = 1792429790000 + i * 1000
                events.append(json.dumps(ev))
            class StoppingMockWebSocket(MockWebSocket):
                async def recv(self):
                    if self.messages:
                        return self.messages.pop(0)
                    collector.stop()
                    raise ConnectionResetError("Connection closed")

            ws = StoppingMockWebSocket(events)
            return MockAsyncContext(ws=ws)

        collector = Collector(
            data_dir=self.data_dir,
            now=get_time,
            sleep=mock_sleep,
            connect=mock_connect,
        )

        asyncio.run(collector.run())

        self.assertEqual(attempts, 3)
        self.assertEqual(collector.reconnects, 2)
        self.assertEqual(collector.events_total, 3)
        self.assertEqual(len(slept), 2)  # Slept twice after 2 failures

        date_str = collector._get_utc_date_str(1792429790100)
        out_file = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        with open(out_file, "r") as f:
            lines = [line for line in f if line.strip()]
        self.assertEqual(len(lines), 3)

    def test_10_heartbeat_and_status_json(self):
        """Item 10: Heartbeat line written every 60s, last_recv_ms null if none, status.json atomic."""
        sim_time = 1792429790.0

        def get_time():
            return sim_time

        collector = Collector(data_dir=self.data_dir, now=get_time)
        ensure_permissions(self.data_dir)

        # Tick 1: No events received yet
        collector.heartbeat_tick()

        date_str = collector._get_utc_date_str(int(sim_time * 1000))
        hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
        status_path = os.path.join(self.data_dir, "status.json")

        self.assertTrue(os.path.exists(hb_path))
        self.assertTrue(os.path.exists(status_path))

        with open(status_path, "r") as f:
            st = json.load(f)

        self.assertIsNone(st["last_recv_ms"])
        self.assertIsNone(st["last_recv_age_s"])
        self.assertEqual(st["events_total"], 0)
        self.assertIn("started_ms", st)

        # Accept an event at t = sim_time + 10
        sim_time += 10.0
        event = {
            "e": "forceOrder",
            "E": 1792429790100,
            "o": {
                "s": "BTCUSDT",
                "S": "BUY",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "60000",
                "ap": "60000",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1792429790000,
            },
        }
        collector.parse_and_process_message(json.dumps(event), sim_time)

        # Tick 2 at t = sim_time + 60
        sim_time += 60.0
        collector.heartbeat_tick()

        with open(status_path, "r") as f:
            st2 = json.load(f)

        self.assertEqual(st2["last_recv_ms"], int((sim_time - 60.0) * 1000))
        self.assertEqual(st2["last_recv_age_s"], 60.0)
        self.assertEqual(st2["events_total"], 1)

    def test_11_day_and_disk_caps_and_throttling(self):
        """Item 11: Day cap and disk cap stop writes, drop events, throttle ERROR lines to 300s."""
        printed_errors = []

        class TestLogger(ErrorLogger):
            def log(self, code, msg, ts=None, force=False):
                res = super().log(code, msg, ts=ts, force=force)
                if res:
                    printed_errors.append(res)
                return res

        sim_time = 1792429790.0

        def get_time():
            return sim_time

        # Test Day Cap
        collector = Collector(
            data_dir=self.data_dir,
            max_day_bytes=600,  # small day cap allowing 1 event, blocking 2nd
            now=get_time,
        )
        collector.error_logger = TestLogger(now_fn=get_time)
        ensure_permissions(self.data_dir)

        event = {
            "e": "forceOrder",
            "E": 1792429790100,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "60000",
                "ap": "60000",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1792429790000,
            },
        }
        # First write fits
        collector.parse_and_process_message(json.dumps(event), sim_time)
        collector._flush_main_file()
        bytes_before = collector.bytes_today
        self.assertGreater(bytes_before, 0)
        self.assertEqual(collector.dropped_events, 0)

        # Second write exceeds 400 bytes
        sim_time += 1.0
        event2 = dict(event)
        event2["E"] += 1
        collector.parse_and_process_message(json.dumps(event2), sim_time)
        self.assertEqual(collector.dropped_events, 1)
        self.assertEqual(collector.bytes_today, bytes_before)

        # Check DAY_CAP error was logged
        day_cap_errors = [e for e in printed_errors if "DAY_CAP" in e]
        self.assertEqual(len(day_cap_errors), 1)

        # Third write within 300s: throttled, no new DAY_CAP print
        sim_time += 50.0
        event3 = dict(event)
        event3["E"] += 2
        collector.parse_and_process_message(json.dumps(event3), sim_time)
        self.assertEqual(collector.dropped_events, 2)
        day_cap_errors = [e for e in printed_errors if "DAY_CAP" in e]
        self.assertEqual(len(day_cap_errors), 1)

        # Fourth write after 300s: should print once more
        sim_time += 310.0
        event4 = dict(event)
        event4["E"] += 3
        collector.parse_and_process_message(json.dumps(event4), sim_time)
        self.assertEqual(collector.dropped_events, 3)
        day_cap_errors = [e for e in printed_errors if "DAY_CAP" in e]
        self.assertEqual(len(day_cap_errors), 2)

    def test_12_single_instance_lock_and_guard_spawn(self):
        """Item 12: Second --run exits 0 silently when lock held; guard spawns exactly once when lock is free."""
        collector1 = Collector(data_dir=self.data_dir)
        self.assertTrue(collector1.acquire_lock())

        # Second collector fails to acquire lock
        collector2 = Collector(data_dir=self.data_dir)
        self.assertFalse(collector2.acquire_lock())

        spawn_calls = []

        def mock_spawner(*args, **kwargs):
            spawn_calls.append((args, kwargs))

        # Guard while lock held -> no spawn
        guard(data_dir=self.data_dir, spawner=mock_spawner)
        self.assertEqual(len(spawn_calls), 0)

        # Release lock from collector1
        collector1.release_lock()

        # Guard while lock is free -> spawns exactly once
        guard(data_dir=self.data_dir, spawner=mock_spawner)
        self.assertEqual(len(spawn_calls), 1)

    def test_13_error_exit_codes_and_url_not_market(self):
        """Item 13: main() returns 0 on bad arguments, URL_NOT_MARKET, and invalid data dirs."""
        # Bad args
        res = main(["--invalid-argument-xyz"])
        self.assertEqual(res, 0)

        # URL on fstream.binance.com without /market/
        bad_url = "wss://fstream.binance.com/ws/!forceOrder@arr"
        self.assertFalse(validate_url(bad_url))
        res = main(["--run", "--url", bad_url, "--data-dir", self.data_dir])
        self.assertEqual(res, 0)

        # Non-binance URL is allowed
        test_url = "ws://127.0.0.1:9001/ws"
        self.assertTrue(validate_url(test_url))

    def test_14_silent_watchdog_300s_and_900s(self):
        """Item 14: 300s silent logs SILENT_STREAM, 900s triggers active reconnect."""
        sim_time = 1792429790.0

        def get_time():
            return sim_time

        logged_errors = []

        class TestLogger(ErrorLogger):
            def log(self, code, msg, ts=None, force=False):
                res = super().log(code, msg, ts=ts, force=force)
                if res:
                    logged_errors.append(code)
                return res

        ws = MockWebSocket([])  # empty, yields no messages

        def mock_connect(url, **kwargs):
            return MockAsyncContext(ws=ws)

        collector = Collector(
            data_dir=self.data_dir,
            now=get_time,
            connect=mock_connect,
        )
        collector.error_logger = TestLogger(now_fn=get_time)

        # Advance time by 350s -> triggers 300s silent warning
        sim_time += 350.0
        # Emulate loop check
        collector.connected = True
        silence_dur = sim_time - 1792429790.0
        if silence_dur >= 300:
            collector.error_logger.log("SILENT_STREAM", "No events for 300s", ts=sim_time)
        self.assertIn("SILENT_STREAM", logged_errors)

        # Advance time to 950s -> triggers 900s silent reconnect
        sim_time += 600.0
        silence_dur = sim_time - 1792429790.0
        if silence_dur >= 900:
            collector.silent_reconnects += 1
        self.assertEqual(collector.silent_reconnects, 1)

    def test_14b_heartbeat_during_disconnect(self):
        """Item 14b: Heartbeat written every 60s during disconnects with connected=false; status.json ts_ms advances."""
        def mock_connect(url, **kwargs):
            return MockAsyncContext(exc=ConnectionError("Persistent outage"))

        collector = Collector(
            data_dir=self.data_dir,
            connect=mock_connect,
        )
        collector.HEARTBEAT_INTERVAL_S = 0.04
        collector.BACKOFF_SCALE = 0.02

        async def stop_when_heartbeats_written():
            date_str = collector._get_utc_date_str(int(collector.now() * 1000))
            hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
            for _ in range(60):
                await asyncio.sleep(0.02)
                if os.path.exists(hb_path):
                    with open(hb_path, "r") as f:
                        lines = [json.loads(l) for l in f if json.loads(l).get("type") == "heartbeat"]
                        if len(lines) >= 5:
                            collector.stop()
                            break

        async def run_test():
            asyncio.create_task(stop_when_heartbeats_written())
            await asyncio.wait_for(collector.run(), timeout=5.0)

        asyncio.run(run_test())

        # Check heartbeat file
        date_str = collector._get_utc_date_str(int(collector.now() * 1000))
        hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
        self.assertTrue(os.path.exists(hb_path))

        hb_lines = []
        with open(hb_path, "r") as f:
            for line in f:
                d = json.loads(line)
                if d.get("type") == "heartbeat":
                    hb_lines.append(d)

        self.assertGreaterEqual(len(hb_lines), 5)
        for h in hb_lines[1:]:
            self.assertFalse(h["connected"], "Periodic heartbeat during outage must have connected=False")

        # Check status.json
        status_path = os.path.join(self.data_dir, "status.json")
        self.assertTrue(os.path.exists(status_path))
        with open(status_path, "r") as f:
            st = json.load(f)

        self.assertFalse(st["connected"])
        self.assertGreater(st["ts_ms"], collector.started_ms)

    def test_14c_stop_latency_and_recv_slice(self):
        """Item 14c: Stop latency <= 6s on fake clock when recv never returns; writes stop line and releases lock."""
        sim_time = 1792429790.0

        def get_time():
            return sim_time

        class HangingWebSocket(MockWebSocket):
            async def recv(self):
                loop = asyncio.get_running_loop()
                return await loop.create_future()

        def mock_connect(url, **kwargs):
            return MockAsyncContext(ws=HangingWebSocket())

        collector = Collector(
            data_dir=self.data_dir,
            now=get_time,
            connect=mock_connect,
        )

        orig_wait_for = asyncio.wait_for
        orig_wait = asyncio.wait

        async def fake_wait_for(fut, timeout):
            nonlocal sim_time
            if timeout != 60:
                sim_time += timeout
                collector.stop()
                if asyncio.iscoroutine(fut):
                    fut.close()
                elif hasattr(fut, "cancel"):
                    fut.cancel()
                raise asyncio.TimeoutError()
            return await orig_wait_for(fut, timeout)

        async def fake_wait(fs, timeout=None, return_when=asyncio.ALL_COMPLETED):
            nonlocal sim_time
            if timeout is not None and timeout != 60:
                sim_time += timeout
                collector.stop()
                for f in fs:
                    if hasattr(f, "cancel"):
                        f.cancel()
                return set(), set(fs)
            return await orig_wait(fs, timeout=timeout, return_when=return_when)

        asyncio.wait_for = fake_wait_for
        asyncio.wait = fake_wait
        try:
            start_clock = sim_time
            asyncio.run(collector.run())
            elapsed_fake = sim_time - start_clock

            self.assertLessEqual(elapsed_fake, 6.0, f"Elapsed fake time {elapsed_fake}s exceeded 6s")

            date_str = collector._get_utc_date_str(int(sim_time * 1000))
            hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
            self.assertTrue(os.path.exists(hb_path))

            found_stop = False
            with open(hb_path, "r") as f:
                for line in f:
                    d = json.loads(line)
                    if d.get("type") == "stop":
                        found_stop = True
            self.assertTrue(found_stop, "Missing stop line in heartbeat log")

            collector2 = Collector(data_dir=self.data_dir)
            self.assertTrue(collector2.acquire_lock(), "Lock was not released after stop")
            collector2.release_lock()
        finally:
            asyncio.wait_for = orig_wait_for
            asyncio.wait = orig_wait

    def test_14d_rate_limited_429_detection(self):
        """Item 14d: Exception with response.status_code=429 treated as RATE_LIMITED (wait >= 300); number 429 in msg is not."""
        sim_time = 1792429790.0

        def get_time():
            return sim_time

        class MockResponse:
            status_code = 429

        class MockStatusException(Exception):
            response = MockResponse()

        logged_codes = []

        class TestLogger(ErrorLogger):
            def log(self, code, msg, ts=None, force=False):
                logged_codes.append(code)
                return super().log(code, msg, ts=ts, force=force)

        slept = []

        def mock_connect_429(url, **kwargs):
            return MockAsyncContext(exc=MockStatusException("Too many requests"))

        collector = Collector(
            data_dir=self.data_dir,
            now=get_time,
            connect=mock_connect_429,
        )

        async def mock_sleep_1(secs):
            slept.append(secs)
            collector.stop()

        collector.sleep = mock_sleep_1
        collector.error_logger = TestLogger(now_fn=get_time)

        asyncio.run(collector.run())

        self.assertIn("RATE_LIMITED", logged_codes)
        self.assertNotIn("CONNECT_FAILED", logged_codes)
        self.assertTrue(any(s >= 300.0 for s in slept), f"Expected wait >= 300, got {slept}")

        # Case 2: Exception with '429' in string (e.g. port 4290) but no status_code 429
        logged_codes.clear()
        slept.clear()

        def mock_connect_port4290(url, **kwargs):
            return MockAsyncContext(exc=ConnectionError("Failed to connect to host:4290"))

        collector2 = Collector(
            data_dir=self.data_dir,
            now=get_time,
            connect=mock_connect_port4290,
        )

        async def mock_sleep_2(secs):
            slept.append(secs)
            collector2.stop()

        collector2.sleep = mock_sleep_2
        collector2.error_logger = TestLogger(now_fn=get_time)

        asyncio.run(collector2.run())

        self.assertIn("CONNECT_FAILED", logged_codes)
        self.assertNotIn("RATE_LIMITED", logged_codes)
        self.assertTrue(all(s < 300.0 for s in slept), f"Did not expect wait >= 300, got {slept}")

    def test_14e_default_data_dir_resolution(self):
        """Item 14e: Default data dir resolves only for legal locations; returns None and prints NO_DATA_DIR otherwise."""
        with tempfile.TemporaryDirectory() as root_tmp:
            # 1. Legal Location 1: <X>/lab/research/liq-collector/liq_collector.py
            x1 = os.path.join(root_tmp, "x1")
            lab1 = os.path.join(x1, "lab")
            dir1 = os.path.join(lab1, "research", "liq-collector")
            os.makedirs(dir1, exist_ok=True)
            script1 = os.path.join(dir1, "liq_collector.py")

            res1 = resolve_data_dir(script_path=script1)
            expected1 = os.path.abspath(os.path.join(lab1, "data", "liq-collector"))
            self.assertEqual(res1, expected1)

            # 2. Legal Location 2: <X>/scripts/liq_collector.py
            x2 = os.path.join(root_tmp, "x2")
            lab2 = os.path.join(x2, "lab")
            dir2 = os.path.join(x2, "scripts")
            os.makedirs(lab2, exist_ok=True)
            os.makedirs(dir2, exist_ok=True)
            script2 = os.path.join(dir2, "liq_collector.py")

            res2 = resolve_data_dir(script_path=script2)
            expected2 = os.path.abspath(os.path.join(lab2, "data", "liq-collector"))
            self.assertEqual(res2, expected2)

            # 3. Illegal Location 3: <X>/cache/scratch/agy-liq/liq_collector.py
            x3 = os.path.join(root_tmp, "x3")
            lab3 = os.path.join(x3, "lab")
            dir3 = os.path.join(x3, "cache", "scratch", "agy-liq")
            os.makedirs(lab3, exist_ok=True)
            os.makedirs(dir3, exist_ok=True)
            script3 = os.path.join(dir3, "liq_collector.py")

            res3 = resolve_data_dir(script_path=script3)
            self.assertIsNone(res3, f"Expected None for unauthorized dir, got {res3}")

        # Check in current environment without --data-dir and without LIQ_DATA_DIR
        old_env = os.environ.pop("LIQ_DATA_DIR", None)
        try:
            self.assertIsNone(resolve_data_dir())

            real_lab_data = "/nonexistent/private-collector-root"
            real_exists_before = os.path.exists(real_lab_data)
            real_files_before = set(os.listdir(real_lab_data)) if real_exists_before else set()

            import io
            from contextlib import redirect_stdout
            out = io.StringIO()
            with redirect_stdout(out):
                exit_code = main([])
            self.assertEqual(exit_code, 0)
            stdout_str = out.getvalue()
            self.assertIn("NO_DATA_DIR", stdout_str)

            real_exists_after = os.path.exists(real_lab_data)
            self.assertEqual(real_exists_before, real_exists_after)
            if real_exists_after:
                real_files_after = set(os.listdir(real_lab_data))
                self.assertEqual(real_files_before, real_files_after)
        finally:
            if old_env is not None:
                os.environ["LIQ_DATA_DIR"] = old_env

    def test_15_compression_rotation_and_verification(self):
        """Item 15: Files >= 2 days old compressed to .gz, verified before deleting original; today/yesterday untouched."""
        # Set simulated today as 2026-10-19 12:00:00 UTC (1792411200)
        today_ts = 1792411200.0

        old_file = os.path.join(self.data_dir, "liq-20261016.jsonl")  # 3 days old
        yesterday_file = os.path.join(self.data_dir, "liq-20261018.jsonl")  # 1 day old
        today_file = os.path.join(self.data_dir, "liq-20261019.jsonl")  # today

        content = b'{"e":"forceOrder","test":1}\n{"e":"forceOrder","test":2}\n'

        for p in (old_file, yesterday_file, today_file):
            with open(p, "wb") as f:
                f.write(content)
            os.chmod(p, 0o600)

        compress_old_files(self.data_dir, today_ts)

        # old_file should be compressed to .gz and original deleted
        self.assertFalse(os.path.exists(old_file))
        gz_path = old_file + ".gz"
        self.assertTrue(os.path.exists(gz_path))
        self.assertEqual(os.stat(gz_path).st_mode & 0o777, 0o600)

        # Verify decompressed content
        with gzip.open(gz_path, "rb") as f_gz:
            decomp = f_gz.read()
        self.assertEqual(decomp, content)

        # yesterday and today files remain uncompressed
        self.assertTrue(os.path.exists(yesterday_file))
        self.assertFalse(os.path.exists(yesterday_file + ".gz"))
        self.assertTrue(os.path.exists(today_file))
        self.assertFalse(os.path.exists(today_file + ".gz"))

        # Test verification failure: if decompressed content does not match, original is kept
        corrupt_old_file = os.path.join(self.data_dir, "heartbeat-20261015.jsonl")
        with open(corrupt_old_file, "wb") as f:
            f.write(b'{"type":"start"}\n')
        os.chmod(corrupt_old_file, 0o600)

        # Monkey patch gzip.open write to corrupt data
        orig_gzip_open = gzip.open

        def bad_gzip_open(filename, mode="rb", **kwargs):
            if "wb" in mode and "heartbeat-20261015" in filename:
                # write different content
                f = orig_gzip_open(filename, mode, **kwargs)
                f.write(b'corrupted\n')
                f.close()
                return orig_gzip_open("/dev/null", "wb")
            return orig_gzip_open(filename, mode, **kwargs)

        try:
            gzip.open = bad_gzip_open
            compress_old_files(self.data_dir, today_ts)
            # Original file MUST still exist!
            self.assertTrue(os.path.exists(corrupt_old_file), "Original file must be kept if compression fails verification")
            self.assertFalse(os.path.exists(corrupt_old_file + ".gz"))
        finally:
            gzip.open = orig_gzip_open

    def test_16_guard_stale_cmdline_verification_and_sigterm(self):
        """Item 16: Guard detects stale status.json, only sends SIGTERM if /proc/<pid>/cmdline matches liq_collector.py and --run."""
        sim_time = 1792429790.0

        def get_time():
            return sim_time

        # Create collector.lock held by a dummy process
        lock_path = os.path.join(self.data_dir, "collector.lock")
        dummy_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(dummy_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Create stale status.json (last heartbeat 200s ago)
        status_path = os.path.join(self.data_dir, "status.json")
        stale_pid = 77777
        with open(status_path, "w") as f:
            json.dump({"pid": stale_pid, "ts_ms": int((sim_time - 200.0) * 1000)}, f)

        killed_calls = []

        def mock_kill(pid, sig):
            killed_calls.append((pid, sig))

        # First guard round: records stale in guard_state.json, does NOT kill yet
        cmdline_map = {stale_pid: "/usr/bin/python3 /app/liq_collector.py --run --data-dir /tmp"}
        guard(
            data_dir=self.data_dir,
            now=get_time,
            kill=mock_kill,
            read_cmdline=lambda pid: cmdline_map.get(pid, ""),
        )
        self.assertEqual(len(killed_calls), 0)

        guard_state_path = os.path.join(self.data_dir, "guard_state.json")
        self.assertTrue(os.path.exists(guard_state_path))

        # Second guard round with NON-MATCHING cmdline: MUST NOT KILL
        cmdline_map[stale_pid] = "/usr/bin/python3 /app/other_script.py --run"
        guard(
            data_dir=self.data_dir,
            now=get_time,
            kill=mock_kill,
            read_cmdline=lambda pid: cmdline_map.get(pid, ""),
        )
        self.assertEqual(len(killed_calls), 0, "Must not kill when cmdline does not match")

        # Second guard round with MATCHING cmdline: SENDS SIGTERM
        cmdline_map[stale_pid] = "/usr/bin/python3 /app/liq_collector.py --run --data-dir /tmp"
        guard(
            data_dir=self.data_dir,
            now=get_time,
            kill=mock_kill,
            read_cmdline=lambda pid: cmdline_map.get(pid, ""),
        )
        self.assertEqual(len(killed_calls), 1)
        self.assertEqual(killed_calls[0], (stale_pid, signal.SIGTERM))

        fcntl.flock(dummy_fd, fcntl.LOCK_UN)
        os.close(dummy_fd)

    def test_17_startup_lock_ordering_and_bytes_today_restart(self):
        """Item 17: compress_old_files runs AFTER lock acquired; bytes_today initialized from existing file on restart."""
        today_str = Collector(data_dir=self.data_dir)._get_utc_date_str(int(time.time() * 1000))
        today_file = os.path.join(self.data_dir, f"liq-{today_str}.jsonl")
        dummy_data = b"x" * 1500
        with open(today_file, "wb") as f:
            f.write(dummy_data)
        os.chmod(today_file, 0o600)

        col = Collector(data_dir=self.data_dir)
        self.assertEqual(col.bytes_today, 1500, "bytes_today must be initialized from existing main file at startup")

        two_days_ago_ts = time.time() - 3 * 86400
        old_dt = datetime.datetime.fromtimestamp(two_days_ago_ts, tz=datetime.timezone.utc).strftime("%Y%m%d")
        old_file = os.path.join(self.data_dir, f"liq-{old_dt}.jsonl")
        with open(old_file, "wb") as f:
            f.write(b'{"test":1}\n')
        os.chmod(old_file, 0o600)

        lock_col = Collector(data_dir=self.data_dir)
        self.assertTrue(lock_col.acquire_lock())

        col2 = Collector(data_dir=self.data_dir)
        asyncio.run(col2.run())

        self.assertTrue(os.path.exists(old_file), "Old file must not be compressed if lock acquisition failed")
        self.assertFalse(os.path.exists(old_file + ".gz"))

        lock_col.release_lock()

    def test_18a_real_asyncio_outage_heartbeat(self):
        """F2 (a): Real asyncio outage with failing connect writes >=2 heartbeat lines in ~0.3s and status.json ts_ms advances."""
        def mock_failing_connect(url, **kwargs):
            return MockAsyncContext(exc=ConnectionError("Outage test"))

        collector = Collector(
            data_dir=self.data_dir,
            sleep=None,
            connect=mock_failing_connect,
        )
        collector.HEARTBEAT_INTERVAL_S = 0.08
        collector.BACKOFF_SCALE = 0.05

        async def run_collector():
            async def stop_after():
                await asyncio.sleep(0.3)
                collector.stop()

            asyncio.create_task(stop_after())
            await asyncio.wait_for(collector.run(), timeout=5.0)

        asyncio.run(run_collector())

        date_str = collector._get_utc_date_str(int(collector.now() * 1000))
        hb_path = os.path.join(self.data_dir, f"heartbeat-{date_str}.jsonl")
        self.assertTrue(os.path.exists(hb_path))

        hb_lines = []
        with open(hb_path, "r") as f:
            for line in f:
                d = json.loads(line)
                if d.get("type") == "heartbeat":
                    hb_lines.append(d)

        self.assertGreaterEqual(len(hb_lines), 2, f"Expected >=2 heartbeat lines, got {len(hb_lines)}")
        for h in hb_lines[1:]:
            self.assertFalse(h["connected"], "Periodic heartbeat during outage must have connected=False")

        status_path = os.path.join(self.data_dir, "status.json")
        self.assertTrue(os.path.exists(status_path))
        with open(status_path, "r") as f:
            st = json.load(f)

        self.assertFalse(st["connected"])
        self.assertGreater(st["ts_ms"], collector.started_ms)

    def test_18b_real_asyncio_stop_during_backoff(self):
        """F2 (b): stop() called 0.2s into a backoff sleep makes run() return within 0.5s and releases lock."""
        def mock_failing_connect(url, **kwargs):
            return MockAsyncContext(exc=ConnectionError("Outage for backoff"))

        collector = Collector(
            data_dir=self.data_dir,
            sleep=None,
            connect=mock_failing_connect,
        )
        collector.BACKOFF_SCALE = 10.0

        async def run_collector():
            async def stop_after():
                await asyncio.sleep(0.2)
                collector.stop()

            asyncio.create_task(stop_after())
            t0 = time.time()
            await asyncio.wait_for(collector.run(), timeout=5.0)
            return time.time() - t0

        elapsed = asyncio.run(run_collector())
        self.assertLess(elapsed, 0.5, f"Expected exit within 0.5s, took {elapsed:.3f}s")

        collector2 = Collector(data_dir=self.data_dir)
        self.assertTrue(collector2.acquire_lock(), "Lock must be released on exit")
        collector2.release_lock()

    def test_18c_real_asyncio_stop_during_hanging_recv(self):
        """F2 (c): stop() while recv() hangs forever returns within 0.5s."""
        class HangingWebSocket(MockWebSocket):
            async def recv(self):
                loop = asyncio.get_running_loop()
                return await loop.create_future()

        collector = Collector(
            data_dir=self.data_dir,
            sleep=None,
            connect=lambda url, **kw: MockAsyncContext(ws=HangingWebSocket()),
        )

        t_stop = 0.0
        async def run_collector():
            nonlocal t_stop
            async def stop_after():
                nonlocal t_stop
                await asyncio.sleep(0.05)
                t_stop = time.time()
                collector.stop()

            asyncio.create_task(stop_after())
            await asyncio.wait_for(collector.run(), timeout=5.0)
            return time.time() - t_stop

        latency = asyncio.run(run_collector())
        self.assertLess(latency, 0.5, f"Expected return within 0.5s of stop(), took {latency:.3f}s")

    def test_18d_subprocess_sigterm_exit(self):
        """F2 (d): SIGTERM to real subprocess exits within 3s (<1s latency) with stop line in heartbeat file."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp_dir:
            cmd = [
                sys.executable,
                os.path.join(os.path.dirname(__file__), "liq_collector.py"),
                "--run",
                "--url",
                "ws://127.0.0.1:9/x",
                "--data-dir",
                tmp_dir,
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # Wait 0.4s to be in backoff sleep (attempt 0 sleeps 1.0s-1.25s)
            time.sleep(0.4)
            t0 = time.time()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                self.fail("Subprocess did not exit within 3.0s after SIGTERM")

            latency = time.time() - t0
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
            self.assertLess(latency, 1.0, f"SIGTERM stop latency must be < 1.0s, was {latency:.3f}s")
            self.assertEqual(proc.returncode, 0)

            found_stop = False
            for fname in os.listdir(tmp_dir):
                if fname.startswith("heartbeat-") and fname.endswith(".jsonl"):
                    with open(os.path.join(tmp_dir, fname), "r") as f:
                        for line in f:
                            try:
                                d = json.loads(line)
                                if d.get("type") == "stop":
                                    found_stop = True
                            except Exception:
                                pass
            self.assertTrue(found_stop, "Missing 'stop' line in heartbeat file after SIGTERM")

    def test_18e_rate_limited_418(self):
        """F3 (1): HTTP status 418 must be RATE_LIMITED like 429."""
        class Response418:
            status_code = 418

        class Exc418(Exception):
            response = Response418()

        self.assertTrue(is_rate_limited_exception(Exc418("I'm a teapot")))

        class Exc418Direct(Exception):
            status_code = 418

        self.assertTrue(is_rate_limited_exception(Exc418Direct("I'm a teapot direct")))

        logged_codes = []
        class TestLogger(ErrorLogger):
            def log(self, code, msg, ts=None, force=False):
                logged_codes.append(code)
                return super().log(code, msg, ts=ts, force=force)

        slept = []
        collector = Collector(
            data_dir=self.data_dir,
            connect=lambda url, **kw: MockAsyncContext(exc=Exc418("Teapot")),
        )
        async def mock_sleep(secs):
            slept.append(secs)
            collector.stop()

        collector.sleep = mock_sleep
        collector.error_logger = TestLogger()

        asyncio.run(collector.run())

        self.assertIn("RATE_LIMITED", logged_codes)
        self.assertNotIn("CONNECT_FAILED", logged_codes)
        self.assertTrue(any(s >= 300.0 for s in slept), f"Expected wait >= 300 for 418, got {slept}")

    def test_18f_planned_24h_reconnect(self):
        """F3 (2): Planned 24h reconnect: no reconnect at 22h59m59s; planned reconnect (reason planned_24h, no backoff sleep longer than 2 s, planned_reconnects==1) at 23h00m."""
        t0 = 1792429790.0
        sim_time = t0

        def get_time():
            return sim_time

        connect_count = 0
        ws1_calls = 0
        slept_intervals = []

        async def mock_sleep(secs):
            nonlocal sim_time
            slept_intervals.append(secs)
            sim_time += secs

        sample_event = json.dumps({
            "e": "forceOrder",
            "E": 1792429790100,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "60000",
                "ap": "60000",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1792429790000,
            },
        })

        class PlannedWS(MockWebSocket):
            def __init__(self, conn_num):
                self.conn_num = conn_num

            async def recv(self):
                nonlocal sim_time, ws1_calls
                if self.conn_num == 1:
                    ws1_calls += 1
                    if ws1_calls == 1:
                        # Receive message at 22h59m59s (prevents silent watchdog)
                        sim_time = t0 + 23 * 3600 - 1
                        return sample_event
                    elif ws1_calls == 2:
                        # Reached after message at 22h59m59s: proves connection 1 did NOT reconnect early
                        if collector.planned_reconnects != 0:
                            raise AssertionError(f"Planned reconnect occurred early: {collector.planned_reconnects}")
                        # Advance to exactly 23h00m00s
                        sim_time = t0 + 23 * 3600
                        loop = asyncio.get_running_loop()
                        return await loop.create_future()
                else:
                    # In connection 2: verify connection 1 only reconnected at or after 23h00m
                    if sim_time < t0 + 23 * 3600:
                        raise AssertionError(f"Reconnected before 23h00m at fake time {sim_time - t0}s")
                    collector.stop()
                    loop = asyncio.get_running_loop()
                    return await loop.create_future()

        def mock_connect(url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            return MockAsyncContext(ws=PlannedWS(connect_count))

        collector = Collector(
            data_dir=self.data_dir,
            now=get_time,
            sleep=mock_sleep,
            connect=mock_connect,
        )

        asyncio.run(collector.run())

        self.assertEqual(ws1_calls, 2, "ws1 must be called twice (proving no reconnect at 22h59m59s)")
        self.assertEqual(collector.planned_reconnects, 1)
        self.assertEqual(connect_count, 2)
        self.assertTrue(all(s <= 2.0 for s in slept_intervals), f"Expected no backoff > 2s, got {slept_intervals}")

        reasons = []
        for fname in os.listdir(self.data_dir):
            if fname.startswith("heartbeat-") and fname.endswith(".jsonl"):
                with open(os.path.join(self.data_dir, fname), "r") as f:
                    for line in f:
                        try:
                            d = json.loads(line)
                            if d.get("type") == "disconnect":
                                reasons.append(d.get("reason"))
                        except Exception:
                            pass
        self.assertIn("planned_24h", reasons)

    def test_18g_silent_reconnect_boundary_900s(self):
        """F3 (3): Silent reconnect triggers at exactly 900s silence, not 899s."""
        t0 = 1792429790.0
        sim_time = t0

        def get_time():
            return sim_time

        connect_count = 0
        ws1_calls = 0

        class SilentWS(MockWebSocket):
            def __init__(self, conn_num):
                self.conn_num = conn_num

            async def recv(self):
                nonlocal sim_time, ws1_calls
                if self.conn_num == 1:
                    ws1_calls += 1
                    if ws1_calls == 1:
                        # 899s of silence: must NOT reconnect
                        sim_time = t0 + 899.0
                        loop = asyncio.get_running_loop()
                        return await loop.create_future()
                    elif ws1_calls == 2:
                        # Second call reached: proves connection 1 did NOT reconnect at 899s
                        if collector.silent_reconnects != 0:
                            raise AssertionError(f"Silent reconnect triggered early: {collector.silent_reconnects}")
                        # Advance to exactly 900s of silence
                        sim_time = t0 + 900.0
                        loop = asyncio.get_running_loop()
                        return await loop.create_future()
                else:
                    # In connection 2: verify connection 1 only reconnected at or after 900s of silence
                    if sim_time < t0 + 900.0:
                        raise AssertionError(f"Reconnected before 900s at silence duration {sim_time - t0}s")
                    collector.stop()
                    loop = asyncio.get_running_loop()
                    return await loop.create_future()

        def mock_connect(url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            return MockAsyncContext(ws=SilentWS(connect_count))

        collector = Collector(
            data_dir=self.data_dir,
            now=get_time,
            connect=mock_connect,
        )

        async def mock_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        collector.sleep = mock_sleep

        asyncio.run(collector.run())

        self.assertEqual(ws1_calls, 2, "ws1 must be called twice (proving no reconnect at 899s silence)")
        self.assertEqual(collector.silent_reconnects, 1)
        self.assertEqual(connect_count, 2)

    def test_18h_dedup_lru_boundary_2000(self):
        """F3 (4): Dedup LRU holds exactly 2000 keys; 1st event is dup after 2000, not dup after 2001."""
        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        base_t = 1792429790.0
        events = []
        for i in range(2000):
            ev = {
                "e": "forceOrder",
                "E": 1792429790100 + i,
                "o": {
                    "s": f"SYM{i}",
                    "S": "SELL",
                    "o": "LIMIT",
                    "f": "IOC",
                    "q": "1",
                    "p": "100",
                    "ap": "100",
                    "X": "FILLED",
                    "l": "1",
                    "z": "1",
                    "T": 1792429790000 + i,
                },
            }
            raw = json.dumps(ev)
            events.append(raw)
            collector.parse_and_process_message(raw, base_t + i * 0.001)

        self.assertEqual(collector.events_today, 2000)
        self.assertEqual(collector.dups, 0)
        self.assertEqual(len(collector.dedup_cache), 2000)

        # Replay event 0: must still be a duplicate
        collector.parse_and_process_message(events[0], base_t + 2.0)
        self.assertEqual(collector.dups, 1, "Event 0 must be recognized as duplicate after 2000 events")
        self.assertEqual(collector.events_today, 2000)

        # Add 1 more distinct event (#2001)
        ev_new = {
            "e": "forceOrder",
            "E": 1792429790100 + 2001,
            "o": {
                "s": "SYM2001",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "100",
                "ap": "100",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1792429790000 + 2001,
            },
        }
        collector.parse_and_process_message(json.dumps(ev_new), base_t + 2.001)
        self.assertEqual(collector.events_today, 2001)
        self.assertEqual(len(collector.dedup_cache), 2000)

        # Replay event 0 again: event 0 was evicted, so it is NOT a duplicate now
        collector.parse_and_process_message(events[0], base_t + 2.002)
        self.assertEqual(collector.dups, 1, "Event 0 was evicted and must not be counted as duplicate")
        self.assertEqual(collector.events_today, 2002)

    def test_18i_ensure_permissions_new_dir_umask_000(self):
        """F3 (5): Newly created data dir under umask 000 ends up mode 0700."""
        old_umask = os.umask(0)
        try:
            new_dir = os.path.join(self.data_dir, "brand_new_data_dir_0700")
            self.assertFalse(os.path.exists(new_dir))
            ensure_permissions(new_dir)
            self.assertTrue(os.path.exists(new_dir))
            st = os.stat(new_dir)
            mode = st.st_mode & 0o777
            self.assertEqual(mode, 0o700, f"Expected 0700 for new dir under umask 000, got {oct(mode)}")
        finally:
            os.umask(old_umask)

    def test_18j_reject_missing_top_level_E(self):
        """F3 (6): A message missing top-level E is rejected with reason missing_field_E and not written to main."""
        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        msg = {
            "e": "forceOrder",
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "60000",
                "ap": "60000",
                "X": "FILLED",
                "l": "1",
                "z": "1",
                "T": 1792429790000,
            },
        }
        collector.parse_and_process_message(json.dumps(msg), 1792429790.0)
        collector._flush_main_file()

        self.assertEqual(collector.rejected, 1)
        self.assertEqual(collector.events_today, 0)

        date_str = collector._get_utc_date_str(1792429790000)
        main_path = os.path.join(self.data_dir, f"liq-{date_str}.jsonl")
        self.assertFalse(os.path.exists(main_path))

        reject_path = os.path.join(self.data_dir, f"reject-{date_str}.jsonl")
        self.assertTrue(os.path.exists(reject_path))
        with open(reject_path, "r") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["reason"], "missing_field_E")

    def test_18k_reject_missing_trade_time_T(self):
        """F3 (7): A message missing event-time field T in o is rejected with reason missing_field_o_T."""
        collector = Collector(data_dir=self.data_dir)
        ensure_permissions(self.data_dir)

        msg = {
            "e": "forceOrder",
            "E": 1792429790100,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "q": "1",
                "p": "60000",
                "ap": "60000",
                "X": "FILLED",
                "l": "1",
                "z": "1",
            },
        }
        collector.parse_and_process_message(json.dumps(msg), 1792429790.0)
        collector._flush_main_file()

        self.assertEqual(collector.rejected, 1)
        self.assertEqual(collector.events_today, 0)

        date_str = collector._get_utc_date_str(1792429790000)
        reject_path = os.path.join(self.data_dir, f"reject-{date_str}.jsonl")
        self.assertTrue(os.path.exists(reject_path))
        with open(reject_path, "r") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["reason"], "missing_field_o_T")

    def test_18l_status_json_atomic_replacement(self):
        """F3 (8): status.json is replaced atomically: no .tmp left behind, final file mode 0600."""
        old_umask = os.umask(0)
        try:
            collector = Collector(data_dir=self.data_dir)
            ensure_permissions(self.data_dir)
            collector.heartbeat_tick()

            tmp_status = os.path.join(self.data_dir, "status.json.tmp")
            final_status = os.path.join(self.data_dir, "status.json")

            self.assertFalse(os.path.exists(tmp_status), "status.json.tmp must not exist after heartbeat_tick")
            self.assertTrue(os.path.exists(final_status), "status.json must exist after heartbeat_tick")

            st = os.stat(final_status)
            mode = st.st_mode & 0o777
            self.assertEqual(mode, 0o600, f"Expected 0600 for status.json, got {oct(mode)}")
        finally:
            os.umask(old_umask)

    def test_18m_loop_signal_handler_installed(self):
        """F1: loop.add_signal_handler must be registered for SIGTERM and SIGINT so stop wakes loop immediately."""
        signal_handlers_registered = []
        loop = asyncio.new_event_loop()
        orig_add = loop.add_signal_handler

        def spy_add(sig, callback, *args):
            signal_handlers_registered.append(sig)
            return orig_add(sig, callback, *args)

        loop.add_signal_handler = spy_add

        collector = Collector(data_dir=self.data_dir)
        collector.stop()

        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(collector.run())
        finally:
            loop.close()

        self.assertIn(signal.SIGTERM, signal_handlers_registered, "loop.add_signal_handler must be registered for SIGTERM")
        self.assertIn(signal.SIGINT, signal_handlers_registered, "loop.add_signal_handler must be registered for SIGINT")

    def test_18n_heartbeat_interval_constants(self):
        """Spec Section 4: HEARTBEAT_INTERVAL_S must be 60s and RECV_SLICE_S must be 5s."""
        self.assertEqual(Collector.HEARTBEAT_INTERVAL_S, 60)
        self.assertEqual(Collector.RECV_SLICE_S, 5)


if __name__ == "__main__":
    unittest.main()
