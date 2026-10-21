"""Isolated synthetic unit tests for Metro safety and accounting semantics."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from owner_metro_sources import parts, processes, load
from owner_metro_model import read_metro


class MetroUnit(unittest.TestCase):
    def test_paths(self):
        for bad in ("../etc/passwd", "/tmp/state", "a//b", "a/../b", "a\\b", "http://x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parts(bad)
        self.assertEqual(parts("owner_metro/run.json"), ("owner_metro", "run.json"))

    def test_process_untrusted(self):
        rows = {"processes": [{"id": "compute", "pid": 999999,
                               "start_ticks": 20, "kind": "compute"}]}
        self.assertEqual(processes(rows, {"processes": []}, 123), [])
        matched = processes(rows, {"processes": [rows["processes"][0]]}, 123)
        self.assertEqual(matched[0]["state"], "gone")
        self.assertIsNone(matched[0]["pid"])

    def test_unbound_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            docs, health, paths = load(temp, 0)
            self.assertEqual(docs, {})
            self.assertEqual(health["bindings"]["state"], "invalid")

    def test_large_ledger_and_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ("commitments", "cron", "lab/journal", "lab/shared", "owner_metro"):
                (root / folder).mkdir(parents=True)
            ledger = {"next": 1203, "items": [
                {"id": "C-%04d" % i, "text": "Synthetic %d" % i,
                 "state": "OPEN", "updated": 1792062000}
                for i in range(1202)]}
            (root / "commitments/ledger.json").write_text(json.dumps(ledger))
            (root / "owner_metro/bindings.json").write_text(json.dumps({
                "schema_version": 1, "sources": {"directive": "owner_metro/directive.json"}}))
            (root / "owner_metro/directive.json").write_text(json.dumps({
                "pending_decisions": 7, "owner": "Synthetic"}))
            model = read_metro(str(root), now_ms=1792065600000)
            self.assertEqual(model["source_totals"]["ledger"], 1202)
            self.assertEqual(model["kpis"]["pending_decisions"], 7)
            self.assertEqual(len(model["routes"]), 3)
            self.assertEqual(model["kpis"]["processes"], 0)
            self.assertIsNone(model["kpis"]["maker_cells"])
            self.assertTrue(all(s["state"] != "done" for r in model["routes"]
                                for s in r["steps"]))
            self.assertEqual(model["source_totals"]["union"], 1202)
            self.assertEqual(model["history_count"], 0)
            ledger["items"][-1]["state"] = "VERIFIED"
            ledger["items"][-2]["state"] = "DROPPED"
            (root / "commitments/ledger.json").write_text(json.dumps(ledger))
            updated = read_metro(str(root), now_ms=1792065600000)
            self.assertEqual(updated["history_count"], 2)
            self.assertEqual(updated["history"][-1]["id"], "C-1201")
            self.assertEqual(updated["source_totals"]["ledger"], 1202)
            self.assertEqual(updated["source_totals"]["union"], 1202)
            (root / "owner_metro/directive.json").write_text("{")
            self.assertIsNone(read_metro(str(root), now_ms=1792065600000)
                              ["kpis"]["pending_decisions"])


if __name__ == "__main__":
    unittest.main()
