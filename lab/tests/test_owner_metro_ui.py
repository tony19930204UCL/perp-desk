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
        import glob
        import tempfile
        import threading
        import time
        from dashboard_v2_server import create_server
        from browser_issue3_acceptance import CdpSession, chrome_binary
        try:
            candidates = [os.environ.get("OWNER_METRO_CHROME"),
                          os.environ.get("CHROMIUM_PATH")]
            candidates += sorted(glob.glob(str(Path.home() /
                ".cache/ms-playwright/chromium-*/chrome-linux/chrome")))
            candidates += sorted(glob.glob(str(Path.home() /
                ".cache/ms-playwright/chromium-*/chrome-linux64/chrome")))
            candidates += sorted(glob.glob(str(Path.home() /
                ".cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell")))
            chrome = next((x for x in candidates if x and Path(x).is_file()
                           and os.access(x, os.X_OK)), None) or chrome_binary()
        except RuntimeError:
            self.skipTest("native Chromium missing: operator browser gate NOT_RUN")
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

    def test_node_labels(self):
        js = (LAB / "owner_activity.js").read_text(encoding="utf-8")
        html = (LAB / "owner_activity.html").read_text(encoding="utf-8")
        for marker in ("done:'已核驗'", "active:'運作中'",
                       "waiting:'等待中'", "next:'下一站'",
                       "retest:'失敗重測中'", "user:'待使用者核准'",
                       "failed:'已失敗'", "cron 管理"):
            self.assertIn(marker, js)
        for marker in (".metro-stop.retest", ".metro-stop.user",
                       ".metro-stop.failed", "prefers-reduced-motion"):
            self.assertIn(marker, html)

    def test_readonly_disclosure(self):
        html=(LAB/"owner_activity.html").read_text(encoding="utf-8")
        js=(LAB/"owner_activity.js").read_text(encoding="utf-8")
        for token in ("metroRecent", "metroKpis", "summary",
                      "目前沒有需要你決定的事", "Asia/Taipei",
                      "目前沒有近況", "pending_list"):
            self.assertIn(token, html+js)
        self.assertIn("add(decision,'details')", js)
        self.assertNotIn("method:'POST'", js)
        self.assertNotIn('method:"POST"', js)
        self.assertNotIn("核准按鈕", html+js)
        self.assertNotIn("<form", html)

    def test_safe_poll(self):
        code = (LAB / "owner_activity.js").read_text(encoding="utf-8")
        for value in ("setInterval(poll,3000)", "if(pending)return",
                      "current===cycle", "textContent", "/api/owner-metro",
                      "保留最後已知資料"):
            self.assertIn(value, code)
        for unsafe in ("innerHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, code)
