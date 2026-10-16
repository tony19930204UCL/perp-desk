"""Issue 67: exact original public depth; no live or account access."""
import copy
import json
import sqlite3
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from paper_market import book_event, mark_event, fresh_source
from paper_runtime_v2 import PaperRuntime, canonical, digest
from sim_broker import SimBroker, InstrumentSettings


def golden():
    return json.loads((LAB / "tests" / "fixtures" / "sol_offtick_original.json").read_text())


def broker_for(tick="0.0100"):
    broker = object.__new__(SimBroker)
    broker.forward_start = 0
    broker.instruments = {"SOLUSDT": InstrumentSettings(
        "SOLUSDT", Decimal("0.0002"), Decimal("0.0005"),
        Decimal("0.01"), Decimal(tick), Decimal("5"), Decimal("80000"),
        Decimal("0.01"), "crypto")}
    return broker


def event_from_original():
    fixture = golden()
    receipt = fixture["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
    parsed = book_event(receipt, "SOLUSDT", 15000)
    return dict(type="book", symbol="SOLUSDT", event_id="synthetic-replay",
                ts=fixture["poll_error"]["at_ms"], source_ts=parsed["ts_ms"],
                bids=parsed["bids"], asks=parsed["asks"])


class SolOfftickRepairTests(unittest.TestCase):
    def test_original_golden_decimal_deeper_ask_and_exact_throw_site(self):
        fixture = golden()
        event = event_from_original()
        self.assertEqual(fixture["exchangeInfo_SOL_reference"]["contract"]["filters"][0]["tickSize"], "0.0100")
        self.assertEqual(event["asks"][2], ["105.5290", "1.00"])
        self.assertEqual(Decimal(event["asks"][2][0]) % Decimal("0.0100"), Decimal("0.0090"))
        with self.assertRaisesRegex(ValueError, r"validator=SimBroker._validate_event side=asks level=2 price=105.5290 tick=0.0100 remainder=0.0090"):
            broker_for()._validate_event(event)

    def test_valid_book_unchanged_and_deeper_bid_rejected(self):
        event = event_from_original()
        event["asks"][2][0] = "105.5300"
        event["asks"].pop(3)
        broker_for()._validate_event(event)
        event["bids"][3][0] = "105.4790"
        with self.assertRaisesRegex(ValueError, r"side=bids level=3 price=105.4790.*remainder=0.0090"):
            broker_for()._validate_event(event)

    def test_changed_filter_not_assumed_to_be_stale(self):
        event = event_from_original()
        with self.assertRaisesRegex(ValueError, "off-tick"):
            broker_for("0.0100")._validate_event(event)
        # A newly observed tick would require independent reference evidence.
        # This unit only tests the arithmetic, not approval of changed filters.
        broker_for("0.0010")._validate_event(event)

    def test_off_grid_mark_is_not_executable_depth(self):
        fixture = golden()
        mark = copy.deepcopy(fixture["receipts"]["/fapi/v1/premiumIndex"]["raw"]["receipt"])
        mark["payload"]["markPrice"] = "105.5290"
        result = mark_event(mark, "SOLUSDT", 15000)
        self.assertEqual(result["mark_price"], "105.5290")
        event = event_from_original()
        with self.assertRaisesRegex(ValueError, "off-tick"):
            broker_for()._validate_event(event)

    def test_original_source_receipt_and_age_boundaries(self):
        fixture = golden()
        receipt = fixture["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        source, observed = fresh_source(receipt, "SOLUSDT", 15000)
        self.assertEqual(source, 1792124551070)
        self.assertEqual(observed, 1792124551153)
        self.assertLessEqual(fixture["poll_error"]["at_ms"] - source, 15000)
        older = copy.deepcopy(receipt)
        older["source_timestamp_ms"] = observed - 15001
        with self.assertRaisesRegex(ValueError, "stale"):
            fresh_source(older, "SOLUSDT", 15000)
        future = copy.deepcopy(receipt)
        future["source_timestamp_ms"] = observed + 5001
        with self.assertRaisesRegex(ValueError, "future"):
            fresh_source(future, "SOLUSDT", 15000)
        event = event_from_original()
        event["source_ts"] = event["ts"] + 1
        with self.assertRaisesRegex(ValueError, "invalid source timestamp"):
            broker_for()._validate_event(event)

    def test_runtime_actual_broker_throw_audit_matches_original_receipt(self):
        fixture = golden()
        depth = fixture["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE audit(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        start = fixture["receipts"]["/fapi/v1/depth"]["raw"]
        db.execute("INSERT INTO audit(payload) VALUES(?)", (canonical(dict(
            type="public_receipt", receipt=depth,
            request_start_ms=start["request_start_ms"],
            request_return_ms=start["request_return_ms"])),))
        events = []
        at = fixture["poll_error"]["at_ms"]
        obj = SimpleNamespace(db=db, state={"reference": None},
            config={"execution_model": {"max_source_age_ms": 15000}},
            clock=lambda: at, monotonic=lambda: 123.0,
            broker=SimpleNamespace(on_event=lambda ev: broker_for()._validate_event(ev)),
            audit=lambda item: events.append(item),
            _timing_record=lambda item: events.append(item))
        event = event_from_original()
        with self.assertRaisesRegex(ValueError, "side=asks level=2"):
            PaperRuntime.emit(obj, event)
        self.assertEqual(events[0]["type"], "broker_offtick_rejection")
        self.assertTrue(events[0]["receipt_audit_match"])
        self.assertEqual(events[0]["source_age_at_dispatch_ms"], at - depth["source_timestamp_ms"])
        self.assertEqual(events[0]["receipt_ms"], 1792124551153)
        self.assertEqual(events[0]["receipt_audit_request_start_ms"], start["request_start_ms"])
        self.assertEqual(events[0]["receipt_audit_request_return_ms"], start["request_return_ms"])
        self.assertEqual(events[0]["original_receipt_sha256"], digest(depth))
        db.close()


if __name__ == "__main__":
    unittest.main()
