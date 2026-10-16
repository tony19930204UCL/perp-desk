"""Issue #67: immutable raw SOL receipts and fail-closed throw-site replay.

All dispatch times in this test are simulated. No network, host or account IO.
"""
import copy
import json
import sqlite3
import sys
import unittest
from decimal import Decimal
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from paper_market import book_event, mark_event, fresh_source, receipt_ms
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
    # Live collection uses b['observed_ms'] as the incoming book event ts.
    # poll_error.at_ms is a later observation, NOT the original receipt.
    return dict(type="book", symbol="SOLUSDT", event_id="synthetic-replay",
                ts=parsed["observed_ms"], source_ts=parsed["ts_ms"],
                bids=parsed["bids"], asks=parsed["asks"])


def original_reference():
    """Derive a synthetic runtime envelope from recorded reference fields."""
    record = golden()["exchangeInfo_SOL_reference"]
    return dict(endpoint="/fapi/v1/exchangeInfo",
                received_at=record["received_at"],
                source_timestamp_ms=record["source_timestamp_ms"],
                payload={"symbols": [copy.deepcopy(record["contract"])]})


class SolOfftickRepairTests(unittest.TestCase):
    def replay(self, *, stored="original", with_reference=False, legal=False,
               dispatch_ms=None, event_received_ms=None):
        """Replay actual PaperRuntime.emit with an isolated in-memory audit.

        The injected event observation and each persisted receipt observation
        are independent, not inferred from the same fixture mutation.
        """
        fixture = golden()
        depth_raw = fixture["receipts"]["/fapi/v1/depth"]["raw"]
        original = depth_raw["receipt"]
        dispatch_ms = (fixture["poll_error"]["at_ms"]
                       if dispatch_ms is None else dispatch_ms)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE audit(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
        variants = (("original", "decoy_later")
                    if stored == "original_plus_newer_decoy" else
                    ("original", "decoy_same_ms")
                    if stored == "ambiguous_same_ms" else
                    () if stored == "missing" else (stored,))
        for variant in variants:
            recorded = copy.deepcopy(original)
            delay_ms = 0
            if variant == "changed_source":
                recorded["source_timestamp_ms"] += 1
            elif variant == "changed_body":
                recorded["payload"]["asks"][2][0] = "105.5300"
            elif variant in ("decoy_later", "decoy_same_ms"):
                delay = (timedelta(milliseconds=100) if variant == "decoy_later"
                         else timedelta(microseconds=100))
                received_at = datetime.fromisoformat(
                    original["received_at"].replace("Z", "+00:00")) + delay
                recorded["received_at"] = received_at.isoformat().replace("+00:00", "Z")
                delay_ms = 100 if variant == "decoy_later" else 0
            elif variant != "original":
                raise AssertionError("unsupported replay case")
            db.execute("INSERT INTO audit(payload) VALUES(?)", (canonical(dict(
                type="public_receipt", receipt=recorded,
                request_start_ms=depth_raw["request_start_ms"] + delay_ms,
                request_return_ms=depth_raw["request_return_ms"] + delay_ms)),))
        audits, delivered, timings = [], [], []
        event = event_from_original()
        if event_received_ms is not None:
            event["ts"] = event_received_ms
        if legal:
            event["asks"][2][0] = "105.5300"
            event["asks"].pop(3)

        def validate(ev):
            delivered.append(copy.deepcopy(ev))
            broker_for()._validate_event(ev)

        runtime = SimpleNamespace(
            db=db, state={"reference": original_reference() if with_reference else None},
            config={"execution_model": {"max_source_age_ms": 15000}},
            clock=lambda: dispatch_ms, monotonic=lambda: 123.0,
            broker=SimpleNamespace(on_event=validate),
            audit=lambda item: audits.append(copy.deepcopy(item)),
            _timing_record=lambda item: timings.append(copy.deepcopy(item)))
        error = None
        try:
            PaperRuntime.emit(runtime, event)
        except ValueError as exc:
            error = str(exc)
        finally:
            db.close()
        return dict(audits=audits, delivered=delivered, timings=timings,
                    event=event, error=error)

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
        # Pure arithmetic only, not approval of a changed exchange filter.
        broker_for("0.0010")._validate_event(event)

    def test_off_grid_mark_is_not_executable_depth(self):
        fixture = golden()
        mark = copy.deepcopy(fixture["receipts"]["/fapi/v1/premiumIndex"]["raw"]["receipt"])
        mark["payload"]["markPrice"] = "105.5290"
        result = mark_event(mark, "SOLUSDT", 15000)
        self.assertEqual(result["mark_price"], "105.5290")
        broker_for()._validate_event(dict(type="mark", symbol="SOLUSDT",
            event_id="synthetic-mark", ts=result["observed_ms"],
            source_ts=result["ts_ms"], price=result["mark_price"]))
        with self.assertRaisesRegex(ValueError, "off-tick"):
            broker_for()._validate_event(event_from_original())

    def test_original_source_receipt_and_age_boundaries(self):
        fixture = golden()
        receipt = fixture["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        source, observed = fresh_source(receipt, "SOLUSDT", 15000)
        event = event_from_original()
        self.assertEqual(source, 1792124551070)
        self.assertEqual(observed, 1792124551153)
        self.assertEqual(event["source_ts"], source)
        self.assertEqual(event["ts"], observed)
        self.assertEqual(observed - source, 83)
        self.assertLessEqual(fixture["poll_error"]["at_ms"] - source, 15000)
        older = copy.deepcopy(receipt)
        older["source_timestamp_ms"] = observed - 15001
        with self.assertRaisesRegex(ValueError, "stale"):
            fresh_source(older, "SOLUSDT", 15000)
        future = copy.deepcopy(receipt)
        future["source_timestamp_ms"] = observed + 5001
        with self.assertRaisesRegex(ValueError, "future"):
            fresh_source(future, "SOLUSDT", 15000)
        event["source_ts"] = event["ts"] + 1
        with self.assertRaisesRegex(ValueError, "invalid source timestamp"):
            broker_for()._validate_event(event)

    def test_runtime_actual_broker_throw_audit_matches_original_receipt(self):
        fixture = golden()
        original = fixture["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        raw = fixture["receipts"]["/fapi/v1/depth"]["raw"]
        result = self.replay()
        self.assertIn("side=asks level=2 price=105.5290 tick=0.0100 remainder=0.0090", result["error"])
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(len(result["audits"]), 1)
        rejection = result["audits"][0]
        self.assertEqual(rejection["type"], "broker_offtick_rejection")
        self.assertEqual(rejection["source_ms"], 1792124551070)
        self.assertEqual(rejection["receipt_ms"], 1792124551153)
        self.assertEqual(rejection["dispatch_ms"], 1792124551825)
        self.assertEqual(rejection["dispatch_ms"] - rejection["receipt_ms"], 672)
        self.assertEqual(rejection["source_age_at_dispatch_ms"], 755)
        self.assertTrue(rejection["receipt_audit_match"])
        self.assertEqual(rejection["original_receipt_sha256"], digest(original))
        self.assertEqual(rejection["receipt_audit_request_start_ms"], raw["request_start_ms"])
        self.assertEqual(rejection["receipt_audit_request_return_ms"], raw["request_return_ms"])
        self.assertEqual(result["delivered"][0]["received_ms"], 1792124551153)
        self.assertEqual(result["delivered"][0]["ts"], 1792124551825)
        self.assertEqual(result["event"]["asks"][2], ["105.5290", "1.00"])
        self.assertIsNone(rejection["reference_receipt_ms"])
        self.assertIsNone(rejection["reference_age_ms"])
        self.assertIsNone(rejection["reference_receipt_sha256"])

    def test_missing_or_changed_audit_receipt_cannot_claim_match(self):
        original = golden()["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        for variant in ("missing", "changed_source", "changed_body"):
            with self.subTest(variant=variant):
                result = self.replay(stored=variant)
                self.assertIn("side=asks level=2", result["error"])
                self.assertEqual(len(result["audits"]), 1)
                rejection = result["audits"][0]
                self.assertFalse(rejection["receipt_audit_match"])
                self.assertIsNone(rejection["original_receipt_sha256"])
                self.assertIsNone(rejection["receipt_audit_request_start_ms"])
                self.assertIsNone(rejection["receipt_audit_request_return_ms"])
                self.assertEqual(rejection["source_ms"], original["source_timestamp_ms"])
                self.assertEqual(rejection["receipt_ms"], receipt_ms(original))
                self.assertEqual(rejection["dispatch_ms"], 1792124551825)

    def test_other_valid_receipt_same_source_and_body_never_claims_original(self):
        """The previous correlation incorrectly matched this only-decoy case."""
        original = golden()["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        control = self.replay(stored="original")["audits"][0]
        decoy = self.replay(stored="decoy_later")
        rejection = decoy["audits"][0]
        self.assertEqual(len(decoy["delivered"]), 1)
        self.assertIn("side=asks level=2 price=105.5290", decoy["error"])
        self.assertEqual(rejection["source_ms"], original["source_timestamp_ms"])
        self.assertEqual(rejection["receipt_ms"], receipt_ms(original))
        self.assertEqual(rejection["dispatch_ms"], 1792124551825)
        self.assertEqual(rejection["source_age_at_dispatch_ms"], 755)
        self.assertTrue(control["receipt_audit_match"])
        self.assertFalse(rejection["receipt_audit_match"])
        self.assertIsNone(rejection["original_receipt_sha256"])
        self.assertIsNone(rejection["receipt_audit_request_start_ms"])
        self.assertIsNone(rejection["receipt_audit_request_return_ms"])

    def test_original_receipt_found_below_newer_same_book_decoy(self):
        """Latest unrelated GET must not steal the original audit attribution."""
        original = golden()["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        raw = golden()["receipts"]["/fapi/v1/depth"]["raw"]
        result = self.replay(stored="original_plus_newer_decoy")
        rejection = result["audits"][0]
        self.assertIn("side=asks level=2", result["error"])
        self.assertTrue(rejection["receipt_audit_match"])
        self.assertEqual(rejection["receipt_ms"], receipt_ms(original))
        self.assertEqual(rejection["original_receipt_sha256"], digest(original))
        self.assertEqual(rejection["receipt_audit_request_start_ms"],
                         raw["request_start_ms"])
        self.assertEqual(rejection["receipt_audit_request_return_ms"],
                         raw["request_return_ms"])

    def test_independent_event_observation_must_equal_recorded_receipt(self):
        """Matching body/source cannot override event-provided receipt time."""
        original = golden()["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]
        actual_observed = receipt_ms(original)
        shifted = self.replay(stored="original",
                              event_received_ms=actual_observed + 100)
        self.assertIn("side=asks level=2", shifted["error"])
        self.assertEqual(shifted["delivered"][0]["received_ms"], actual_observed + 100)
        self.assertEqual(shifted["audits"][0]["receipt_ms"], actual_observed + 100)
        self.assertFalse(shifted["audits"][0]["receipt_audit_match"])
        self.assertIsNone(shifted["audits"][0]["original_receipt_sha256"])

    def test_ambiguous_same_millisecond_receipts_fail_closed(self):
        """Event millisecond granularity cannot distinguish two such records."""
        result = self.replay(stored="ambiguous_same_ms")
        rejection = result["audits"][0]
        self.assertIn("side=asks level=2", result["error"])
        self.assertFalse(rejection["receipt_audit_match"])
        self.assertIsNone(rejection["original_receipt_sha256"])
        self.assertIsNone(rejection["receipt_audit_request_start_ms"])
        self.assertIsNone(rejection["receipt_audit_request_return_ms"])

    def test_reference_receipt_age_present_and_absent(self):
        reference = original_reference()
        with_reference = self.replay(with_reference=True)["audits"][0]
        self.assertTrue(with_reference["receipt_audit_match"])
        self.assertEqual(with_reference["reference_receipt_ms"], receipt_ms(reference))
        self.assertEqual(with_reference["reference_age_ms"], 1792124551825 - receipt_ms(reference))
        self.assertEqual(with_reference["reference_receipt_sha256"], digest(reference))
        self.assertEqual(with_reference["reference_hash_kind"],
                         "canonical_runtime_receipt_sha256")
        absent = self.replay(with_reference=False)["audits"][0]
        self.assertIsNone(absent["reference_receipt_ms"])
        self.assertIsNone(absent["reference_age_ms"])
        self.assertIsNone(absent["reference_receipt_sha256"])
        self.assertEqual(absent["original_receipt_sha256"],
                         with_reference["original_receipt_sha256"])

    def test_successful_emit_preserves_original_observation_and_valid_book(self):
        result = self.replay(stored="missing", legal=True)
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["delivered"]), 1)
        self.assertEqual(len(result["audits"]), 1)
        self.assertEqual(result["audits"][0]["type"], "broker_dispatch")
        self.assertEqual(result["audits"][0]["received_ms"], 1792124551153)
        self.assertEqual(result["audits"][0]["source_ms"], 1792124551070)
        self.assertEqual(result["audits"][0]["dispatch_ms"], 1792124551825)
        self.assertEqual(result["delivered"][0]["asks"][2], ["105.5300", "1.00"])
        self.assertEqual(result["delivered"][0]["ts"], 1792124551825)
        self.assertEqual(result["delivered"][0]["received_ms"], 1792124551153)
        self.assertEqual(result["timings"], [])

    def test_future_and_stale_dispatch_fail_before_broker_validation(self):
        source = golden()["receipts"]["/fapi/v1/depth"]["raw"]["receipt"]["source_timestamp_ms"]
        for label, moment, message in (
                ("future", source - 1, "future source at actual broker delivery"),
                ("stale", source + 15001, "stale source at actual broker delivery")):
            with self.subTest(label=label):
                result = self.replay(dispatch_ms=moment)
                self.assertIn(message, result["error"])
                self.assertEqual(result["delivered"], [])
                self.assertEqual(result["audits"], [])
                self.assertEqual(result["timings"], [])


if __name__ == "__main__":
    unittest.main()
