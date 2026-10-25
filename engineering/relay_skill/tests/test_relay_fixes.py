"""Offline relay safety controls: no browser or network."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay_fixes import (bubble_match,readback,card_guard,RelayFixError,
                         make_job)


class FixTests(unittest.TestCase):
    def test_fold_exact(self):
        # Kills clipped-bubble confirmation and arbitrary decoration removal.
        p="LONG TASK\nfinal line"
        before=[]
        after=[{"turn_bound":True,"subtree":p,"full":"LONG TASK…顯示較多"}]
        self.assertEqual(readback(p,before,after),"CONFIRMED")
        self.assertEqual(readback(p,after,after),"PENDING")
        self.assertEqual(readback(p,[],after+after),"AMBIGUOUS")
        self.assertFalse(bubble_match(p,{"subtree":p,"turn_bound":False}))
        self.assertFalse(bubble_match(p,{"subtree":p+"x","turn_bound":True}))
        self.assertFalse(bubble_match(p,{"subtree":"LONG TASK",
                                        "turn_bound":True}))

    def test_card_scope(self):
        # Kills basename matches, always/remember and unknown repository.
        j={"repository":"owner/repo","branch":"fix/task",
           "write_allowlist":["engineering/only.py"]}
        good="GitHub 允許一次 owner/repo fix/task engineering/only.py"
        self.assertEqual(len(card_guard(j,good)),64)
        for bad in ("GitHub 允許一次 engineering/other.py",
                    good+" always allow",
                    good+" ghp_"+"z"*22,
                    good+" engineering/other.py",
                    good+" foreign/name/blob/branch/file.py",
                    good.replace("允許一次","允許一次 允許一次")):
            with self.subTest(bad=bad[:25]):
                with self.assertRaises(RelayFixError):
                    card_guard(j,bad)

    def test_explicit_job(self):
        # Kills inferred chat target, expired dispatch and silent overwrite.
        with tempfile.TemporaryDirectory() as d:
            end=(dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat()
            args=(Path(d)/"work","https://chatgpt.com/c/test-id",
                  "owner/repo","fix/task",83,["engineering/only.py"],
                  end,Path(d)/"lock","task","rel-001")
            job=make_job(*args)
            self.assertEqual(json.loads((args[0]/"job.json").read_text()),job)
            with self.assertRaises(RelayFixError):
                make_job(*args)
            with self.assertRaises(RelayFixError):
                make_job(*(list(args[:1])+["https://example.net/"]+list(args[2:])))
