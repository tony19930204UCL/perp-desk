"""Static UI/refresh invariants; real browser remains separately required."""
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]


class MetroUI(unittest.TestCase):
    def test_white_and_routes(self):
        html = (LAB / "owner_activity.html").read_text(encoding="utf-8")
        for value in ("#F8FAFC", "#FFFFFF", "#0F172A", "68fr", "32fr",
                      "prefers-reduced-motion", "max-width:430px",
                      "metroKpis", "metroRoutes", "metroSpot", "metroHistory"):
            self.assertIn(value, html)
        self.assertIn("<details", html)
        self.assertNotIn("<iframe", html)

    def test_real_chromium(self):
        import json
        import os
        import tempfile
        import threading
        import time
        from dashboard_v2_server import create_server
        from browser_issue3_acceptance import CdpSession, chrome_binary
        chrome = os.environ.get("OWNER_METRO_CHROME") or chrome_binary()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "owner_metro").mkdir()
            (root / "owner_metro/bindings.json").write_text(json.dumps({
                "schema_version": 1, "sources": {}}))
            server = create_server(scorecard_path=str(root/"a"),
                journal_path=str(root/"b"), ledger_path=str(root/"c"), port=0)
            server.configure_monitor(profile_root=str(root))
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            session = None
            try:
                session = CdpSession(chrome, root / "chrome")
                session.call("Page.enable")
                session.call("Runtime.enable")
                for width in (1280, 390):
                    with self.subTest(width=width):
                        session.call("Emulation.setDeviceMetricsOverride", {
                            "width": width, "height": 850, "deviceScaleFactor": 1,
                            "mobile": width == 390, "screenWidth": width,
                            "screenHeight": 850})
                        session.navigate("http://127.0.0.1:%d/owner_activity.html"
                                         % server.server_port)
                        time.sleep(.4)
                        data = session.evaluate("""(()=>({
                          bg:getComputedStyle(document.body).backgroundColor,
                          grid:getComputedStyle(document.querySelector('#metro .layout')).gridTemplateColumns,
                          count:document.querySelectorAll('#metroKpis .metric').length,
                          hidden:getComputedStyle(document.querySelector('#metrics')).display,
                          closed:!document.querySelector('#legacyAudit').open,
                          scroll:document.documentElement.scrollWidth,
                          client:document.documentElement.clientWidth,
                          anim:getComputedStyle(document.querySelector('.metro-stop.next .dot')).animationName
                            }))()""")
                        self.assertEqual(data["bg"], "rgb(248, 250, 252)")
                        self.assertEqual(data["count"], 4, data)
                        self.assertEqual(data["hidden"], "none")
                        self.assertTrue(data["closed"])
                        self.assertLessEqual(data["scroll"], data["client"], data)
            finally:
                if session is not None:
                    session.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_safe_poll(self):
        code = (LAB / "owner_activity.js").read_text(encoding="utf-8")
        for value in ("setInterval(poll,3000)", "if(pending)return",
                      "current===cycle", "textContent", "/api/owner-metro",
                      "保留最後已知資料"):
            self.assertIn(value, code)
        for unsafe in ("innerHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, code)
