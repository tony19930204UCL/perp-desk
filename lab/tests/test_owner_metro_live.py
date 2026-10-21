"""Synthetic HTTP acceptance on the actual dashboard server."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from dashboard_v2_server import create_server


class MetroHTTP(unittest.TestCase):
    def test_loopback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for directory in ("owner_metro", "commitments", "cron", "lab/journal", "lab/shared"):
                (root / directory).mkdir(parents=True)
            (root / "owner_metro/bindings.json").write_text(
                json.dumps({"schema_version": 1, "sources": {
                    "heartbeat": "owner_metro/heartbeat.json"}}))
            (root / "owner_metro/heartbeat.json").write_text(
                json.dumps({"at": "2026-10-21T10:00:00Z", "status": "observed"}))
            server = create_server(scorecard_path=str(root / "a"),
                                   journal_path=str(root / "b"),
                                   ledger_path=str(root / "c"), port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, target, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                try:
                    conn.request(method, target, headers=headers or {})
                    response = conn.getresponse()
                    return response.status, response.read()
                finally:
                    conn.close()
            try:
                self.assertEqual(request("GET", "/api/owner-metro")[0], 404)
                server.configure_monitor(profile_root=str(root))
                code, raw = request("GET", "/api/owner-metro")
                self.assertEqual(code, 200)
                data = json.loads(raw)
                self.assertEqual(data["heartbeat"]["status"], "observed")
                self.assertEqual(data["kpis"]["processes"], 0)
                self.assertEqual(request("HEAD", "/api/owner-metro")[0], 200)
                self.assertEqual(request("POST", "/api/owner-metro")[0], 405)
                self.assertEqual(request("GET", "/api/owner-metro?pid=1")[0], 404)
                self.assertEqual(request("GET", "/api/owner-metro",
                                         {"Origin": "https://attacker.example"})[0], 403)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

class NativeResults(unittest.TestCase):
    def test_partial_and_bad(self):
        import hashlib
        from owner_metro_results import read
        expected = {"input_manifest_sha256": "a"*64,
                    "config_sha256": "b"*64,
                    "source_sha256": {"backtest_entry.py": "c"*64}}
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "output"
            (base / "cells").mkdir(parents=True)
            def write(path, value):
                raw = json.dumps(value, allow_nan=False).encode()
                (base / path).write_bytes(raw)
                return hashlib.sha256(raw).hexdigest()
            self.assertEqual(read(temp, ("output",), "signed", expected)["state"], "missing")
            strategy = {"symbol":"ETHUSDT", "framework":1, "k":".25",
                        "ttl":2, "exit":"X1", "episodes":[]}
            write("cells/00_strategy.json", strategy)
            write("cells/00_matched.json", None)
            observed = read(temp, ("output",), "signed", expected)
            self.assertEqual(observed["count"], 1)
            self.assertEqual(observed["state"], "preliminary")
            self.assertIsNone(observed["metrics"][0]["actual_time_share"])
            metrics = {"gross_bp":"10", "net_bp":"8", "profit_factor":"1.2",
                       "tp_over_tp_plus_sl":"0.5",
                       "tp_sl_sl_gap_denominator":2,
                       "actual_time_close_rate":"NOT_APPLICABLE"}
            digest = write("results.json", {"cells":[{"cell_id":
                "ETHUSDT:1m:.25:2:X1", "metrics":metrics}]})
            manifest = {**expected, "research_status":"EXPLORATORY_UNACCEPTED",
                        "artifacts":{"results.json":digest,
                            "cells/00_strategy.json":hashlib.sha256(
                                (base/"cells/00_strategy.json").read_bytes()).hexdigest(),
                            "cells/00_matched.json":hashlib.sha256(
                                (base/"cells/00_matched.json").read_bytes()).hexdigest()}}
            write("manifest.json", manifest)
            with_result = read(temp, ("output",), "signed", expected)
            self.assertEqual(with_result["count"], 1)
            self.assertEqual(with_result["metrics"][0]["net_bp"], "8")
            self.assertIsNone(with_result["metrics"][0]["actual_time_share"])
            write("results.json", {"cells":[{"cell_id":"ETHUSDT:1m:.25:2:X1",
                  "metrics":metrics}]*2})
            self.assertIn(read(temp, ("output",), "signed", expected)["state"],
                          ("invalid", "duplicate"))
            self.assertEqual(read(temp, ("output",), "old",
                 {**expected, "config_sha256":"d"*64})["state"], "old_or_invalid")
