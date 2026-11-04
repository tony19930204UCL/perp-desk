"""Portable regressions for the existing read-only projection and routes."""
import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

LAB = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
import boss_readonly as B
import dashboard_v2_server as S


class BossData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.now = 1793816683501

    def put(self, key, data):
        path = self.root / B.FILES[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) + "\n", encoding="utf-8")

    def flat(self):
        state = dict(schema=1, cash=150.0, position=None, used=[], seq=1)
        self.put("state", state)
        self.put("tactical", dict(mode="PAPER_ONLY", status="OK",
            updated_ms=self.now, cash=150.0, used_order_ids=[],
            open_positions=0, equity=150.0))
        path = self.root / B.FILES["journal"]
        path.write_text(json.dumps(dict(seq=1, events=[], state=state)) + "\n")

    def test_flat_zero(self):
        self.flat()
        t = B.read_boss(self.root, now_ms=self.now)["tactical"]
        self.assertTrue(t["flat_confirmed"])
        self.assertEqual(t["unrealized"], 0)
        self.assertEqual(t["equity"], 150)
        self.assertEqual(t["available_margin"], 150)

    def test_missing_not_zero(self):
        t = B.read_boss(self.root, now_ms=self.now)["tactical"]
        for name in ("equity", "unrealized", "realized", "available_margin"):
            self.assertIsNone(t[name])

    def test_bad_cash_is_unknown(self):
        self.flat()
        path = self.root / B.FILES["journal"]
        row = dict(seq=2, events=[dict(kind="CASH_TRANSFER", amount=100)],
                   state=dict(schema=1, cash=250, position=None, used=[], seq=2))
        with path.open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        self.put("state", row["state"])
        t = B.read_boss(self.root, now_ms=self.now)["tactical"]
        self.assertIsNone(t["realized"])

    def test_provider_rejected(self):
        self.put("prediction", dict(provider="polymarket",
            product="5m_prediction", schema="prediction-paper.v1",
            updated_ms=self.now, rules_verified=True,
            interface_verified=True, bankroll=150, net_pnl=500))
        d = B.read_boss(self.root, now_ms=self.now)
        self.assertEqual(d["engineering"]["sources"]["prediction"]["status"],
                         "REJECTED_PROVIDER")
        self.assertIsNone(d["prediction"]["bankroll"])
        self.assertIsNone(d["prediction"]["net_pnl"])

    def test_weekly_observation(self):
        self.put("weekly", dict(updated="2026-11-04T18:17:57Z",
            gross_usdt_assumed=150, tranches=[dict(
                entry_day=1793664000000, exit_day=1794268800000,
                books={"F2_raw_quint":dict(long_leg_bp=100,
                      short_leg_pnl_bp=-20, funding_bp=2,
                      net_usdt_at_150_gross=.42, missing=0)})]))
        self.put("weekly_state", {"open":[{"entry_day":1793664000000,
            "obs":{"X":{"t":1793750476024}}}]})
        data = B.read_boss(self.root, now_ms=self.now)["weekly"]
        self.assertEqual(data["books"][0]["long_pnl"], .75)
        self.assertNotEqual(data["books"][0]["entry_day"],
                            data["books"][0]["observed_start_ms"])
        self.assertIsNone(data["books"][1]["net"])
        self.assertIsNone(data["settlements"])

    def test_readonly_routes(self):
        with tempfile.TemporaryDirectory() as root:
            server = S.create_server(scorecard_path=root+"/s",
                journal_path=root+"/j", ledger_path=root+"/l", port=0)
            server.configure_monitor(profile_root=root)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 3)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = "http://127.0.0.1:" + str(server.server_port)
            for route, content_type in (
                ("/", "text/html"), ("/boss.js", "javascript"),
                ("/boss.css", "text/css"), ("/engineering", "text/html"),
                ("/api/boss-dashboard", "application/json"),
                ("/api/desk-readonly", "application/json")):
                with self.subTest(route=route):
                    try:
                        with urllib.request.urlopen(base+route, timeout=3) as res:
                            self.assertEqual(res.status, 200)
                            self.assertIn(content_type,
                                          res.headers["Content-Type"])
                            self.assertIn("script-src 'self'",
                                res.headers["Content-Security-Policy"])
                    except urllib.error.HTTPError as exc:
                        if route == "/api/desk-readonly":
                            self.assertEqual(exc.code, 500)
                        else:
                            raise
            for route in ("/boss.js?x=1", "/api/boss-dashboard?x=1",
                          "/lab/data/tactical-sniper/state.json"):
                with self.subTest(rejected=route):
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(base+route, timeout=3)
                    self.assertEqual(ctx.exception.code, 404)
            for headers in ({"Host":"external.test"},
                            {"Origin":"http://external.test"}):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(urllib.request.Request(
                        base+"/api/boss-dashboard", headers=headers), timeout=3)
                self.assertEqual(ctx.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(urllib.request.Request(
                    base+"/api/boss-dashboard", data=b"x"), timeout=3)
            self.assertEqual(ctx.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
