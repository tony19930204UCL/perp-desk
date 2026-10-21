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

    def test_safe_poll(self):
        code = (LAB / "owner_activity.js").read_text(encoding="utf-8")
        for value in ("setInterval(poll,3000)", "if(pending)return",
                      "current===cycle", "textContent", "/api/owner-metro",
                      "保留最後已知資料"):
            self.assertIn(value, code)
        for unsafe in ("innerHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, code)
