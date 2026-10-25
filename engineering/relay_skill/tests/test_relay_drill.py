"""Synthetic end-to-end relay sequence; no live browser, GitHub or approval."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from relay_fixes import readback,card_guard,RelayFixError,make_job,atomic
from relay_dispatch_worker import mailbox_matches,may_send


class Drill(unittest.TestCase):
    def test_full_fake_cycle(self):
        # Kills folded-readback false UNKNOWN, duplicate send, and lost REVISE.
        with tempfile.TemporaryDirectory() as base:
            root=Path(base)
            expiry=(dt.datetime.now(dt.timezone.utc)+
                    dt.timedelta(hours=2)).isoformat()
            policy=make_job(root/"job","https://chatgpt.com/c/synth-id",
                "owner/repo","fix/relay",83,["engineering/x.py"],
                expiry,root/"lock","relay-reliability","relay-001")
            text="Full first instruction\nsmall last line"
            before=[]
            after=[{"turn_bound":True,"subtree":text,
                    "full":"Full first instruction…顯示較多"}]
            self.assertEqual(readback(text,before,after),"CONFIRMED")
            self.assertFalse(may_send("CLICK_ACK"))
            self.assertFalse(may_send("SEND_UNKNOWN"))
            self.assertFalse(may_send("CONFIRMED"))
            card="GitHub 允許一次 owner/repo fix/relay engineering/x.py"
            fingerprint=card_guard(policy,card)
            self.assertEqual(len(fingerprint),64)
            state={"instruction_id":"relay-001","phase":"CARD_PENDING",
                   "since":"synthetic-not-wall-clock"}
            atomic(root/"job"/"worker-state.json",state)
            self.assertEqual(json.loads((root/"job"/"worker-state.json")
                             .read_text())["phase"],"CARD_PENDING")
            state["phase"]="CARD_APPROVED"
            atomic(root/"job"/"worker-state.json",state)
            msg={"schema_version":2,"instruction_id":"relay-001"}
            import base64
            encoded=base64.b64encode(json.dumps(msg).encode()).decode()
            self.assertTrue(mailbox_matches(encoded,"relay-001"))
            state["phase"]="HANDBACK"
            state["instruction_id"]="relay-002"
            atomic(root/"job"/"worker-state.json",state)
            self.assertEqual(json.loads((root/"job"/"worker-state.json")
                             .read_text())["instruction_id"],"relay-002")

    def test_untrusted_cards(self):
        # Kills secret, out-of-scope, repeat-card and missing-click-ack.
        job={"repository":"owner/repo","branch":"fix/relay",
             "write_allowlist":["engineering/x.py"]}
        for text in ("GitHub 允許一次 engineering/other.py",
                     "GitHub 允許一次 engineering/x.py PRIVATE KEY",
                     "GitHub 允許一次 engineering/x.py always allow",
                     "GitHub engineering/x.py"):
            with self.subTest(card=text):
                with self.assertRaises(RelayFixError):
                    card_guard(job,text)
        state={"card_click_intent":True,"card_ack":None}
        self.assertNotEqual(state["card_ack"],True)
        self.assertFalse(may_send("SEND_UNKNOWN"))

    def test_unknown_no_resend(self):
        # Kills any retry after durable send intent or unknown click.
        for phase in ("SEND_INTENT","CLICK_ACK","SEND_UNKNOWN","CONFIRMED"):
            self.assertFalse(may_send(phase))
        self.assertTrue(may_send("PRESEND_FAILED"))


if __name__=="__main__":
    unittest.main()
