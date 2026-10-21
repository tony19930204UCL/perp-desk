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
