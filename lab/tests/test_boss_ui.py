"""Chromium DOM acceptance; skips explicitly if Chromium is unavailable."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
import dashboard_v2_server as S

FIXTURE = Path(__file__).parent / "fixtures" / "boss_synthetic.json"


class BossUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chrome = next((p for n in (
            "chromium", "chromium-browser", "google-chrome",
            "google-chrome-stable", "chrome", "msedge"
        ) if (p := shutil.which(n))), None)
        if cls.chrome is None:
            raise unittest.SkipTest("NOT_RUN: Chromium executable unavailable")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.patch = mock.patch.object(S, "read_boss",
            side_effect=lambda root, now_ms: self.fixture)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        root = self.tmp.name
        self.server = S.create_server(scorecard_path=root+"/s",
            journal_path=root+"/j", ledger_path=root+"/l", port=0)
        self.server.configure_monitor(profile_root=root)
        self.thread = threading.Thread(target=self.server.serve_forever,
            daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def rendered(self, width):
        base = "http://127.0.0.1:" + str(self.server.server_port)
        with tempfile.TemporaryDirectory() as profile:
            cmd = [self.chrome, "--headless", "--disable-gpu",
                   "--no-first-run", "--disable-background-networking",
                   "--disable-extensions", "--hide-scrollbars",
                   "--user-data-dir="+profile,
                   "--window-size="+str(width)+",900",
                   "--virtual-time-budget=5000", "--dump-dom", base+"/"]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=25, check=False)
        if proc.returncode:
            self.fail("Chromium failed: " +
                      proc.stderr.decode("utf-8", "replace")[-500:])
        return proc.stdout.decode("utf-8", "replace")

    def check_dom(self, dom):
        for text in ("三戰場", "150U 戰術帳戶",
                     "Binance BTC 5 分鐘預測", "每週雙批次",
                     "152.80 U", "0.00 U（確認空倉）",
                     "BTCUSDT", "ETHUSDT", "SOLUSDT",
                     "0.42 U", "系統工程健康度（點擊展開）"):
            self.assertIn(text, dom)
        for i in range(4):
            self.assertRegex(dom, r'id="book-'+str(i)+r'"')
        self.assertIn("來源時間", dom)
        self.assertIn("尚無資料", dom)
        self.assertRegex(dom, r'<details id="diagnostics"[^>]*>')
        self.assertNotRegex(dom, r'<details id="diagnostics"[^>]* open')
        self.assertLess(dom.index('id="weekly"'), dom.index('id="diagnostics"'))
        self.assertNotIn("unsafe-inline", dom)

    def test_desktop_dom(self):
        self.check_dom(self.rendered(1280))

    def test_mobile_dom(self):
        self.check_dom(self.rendered(390))

    def test_missing_money(self):
        self.fixture["tactical"].update(equity=None,
            available_margin=None, unrealized=None, flat_confirmed=False)
        dom = self.rendered(390)
        self.assertIn('id="equity">尚無資料', dom)
        self.assertIn('id="unrealized">尚無估值', dom)
        self.assertNotIn('id="equity">150.00', dom)


if __name__ == "__main__":
    unittest.main()
