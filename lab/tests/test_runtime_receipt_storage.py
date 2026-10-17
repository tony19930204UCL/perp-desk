"""Isolated SQLite tests for receipt storage; no network or runtime services."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from runtime_receipt_storage import ReceiptStorage


def receipt(payload, received="2026-10-17T00:00:00+00:00", source=100):
    return dict(endpoint="/fapi/v1/fundingInfo", params={}, payload=payload,
                received_at=received, source_timestamp_ms=source)


class ReceiptStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("BEGIN")
        self.store = ReceiptStorage(self.db, raw_limit=3)
        self.store.prepare()
        self.db.execute("COMMIT")

    def record(self, value):
        self.db.execute("BEGIN")
        pointer = self.store.record_causal("/fapi/v1/fundingInfo", {}, value)
        self.db.execute("COMMIT")
        return pointer

    def test_identical_payload_dedup_preserves_each_receipt(self):
        a = receipt([{"symbol":"ETHUSDT","fundingIntervalHours":8}], source=100)
        b = receipt(a["payload"], received="2026-10-17T00:00:01+00:00", source=101)
        pa, pb = self.record(a), self.record(b)
        self.assertEqual(pa["payload_sha256"], pb["payload_sha256"])
        self.assertNotEqual(pa["seq"], pb["seq"])
        self.assertEqual(self.store.reconstruct(pa["seq"]), a)
        self.assertEqual(self.store.reconstruct(pb["seq"]), b)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_receipt_payloads").fetchone()[0], 1)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_causal_receipts").fetchone()[0], 2)

    def test_distinct_payloads_are_not_collapsed(self):
        a = self.record(receipt([{"symbol":"ETHUSDT","fundingIntervalHours":8}]))
        b = self.record(receipt([{"symbol":"ETHUSDT","fundingIntervalHours":4}]))
        self.assertNotEqual(a["payload_sha256"], b["payload_sha256"])

    def test_missing_and_corrupt_payload_pointer_fail_closed(self):
        pointer = self.record(receipt([]))
        self.db.execute("PRAGMA foreign_keys=OFF")
        self.db.execute("DELETE FROM runtime_receipt_payloads")
        with self.assertRaisesRegex(ValueError, "missing causal"):
            self.store.reconstruct(pointer["seq"])
        self.db.close()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("BEGIN")
        broken = ReceiptStorage(self.db)
        with self.assertRaisesRegex(ValueError, "missing causal"):
            broken.prepare()
        self.db.execute("ROLLBACK")

    def test_transaction_rollback_is_repeatable(self):
        value = receipt([{"symbol":"ETHUSDT","fundingIntervalHours":8}])
        self.db.execute("BEGIN")
        first = self.store.record_causal("/fapi/v1/fundingInfo", {}, value)
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_causal_receipts").fetchone()[0], 0)
        second = self.record(value)
        self.assertEqual(first["seq"], second["seq"])
        self.assertEqual(self.store.reconstruct(second["seq"]), value)

    def test_deferred_commit_failure_rolls_back_receipt_atomically(self):
        self.db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        self.db.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) "
                        "DEFERRABLE INITIALLY DEFERRED)")
        value = receipt([])
        self.db.execute("BEGIN")
        self.store.record_causal("/fapi/v1/fundingInfo", {}, value)
        self.db.execute("INSERT INTO child VALUES(999)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("COMMIT")
        self.db.execute("ROLLBACK")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_causal_receipts").fetchone()[0], 0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_receipt_payloads").fetchone()[0], 0)

    def test_bounded_noncausal_raw_cannot_remove_causal_evidence(self):
        causal = self.record(receipt([{"symbol":"ETHUSDT","fundingIntervalHours":8}]))
        self.db.execute("BEGIN")
        for n in range(10):
            self.store.record_noncausal_raw("/diagnostic", {"n":n})
        self.db.execute("COMMIT")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_noncausal_raw_receipts").fetchone()[0], 3)
        self.assertEqual(self.store.reconstruct(causal["seq"])["payload"][0]["symbol"],
                         "ETHUSDT")

    def test_prepare_refuses_partial_schema(self):
        other = sqlite3.connect(Path(self.temp.name) / "partial.sqlite3",
                                isolation_level=None)
        self.addCleanup(other.close)
        other.execute("BEGIN")
        other.execute("""CREATE TABLE runtime_receipt_payloads (
    digest TEXT PRIMARY KEY NOT NULL CHECK(typeof(digest)='text' AND length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
    payload_json TEXT NOT NULL CHECK(typeof(payload_json)='text' AND length(payload_json)>0)
)""")
        other.execute("COMMIT")
        other.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "partial"):
            ReceiptStorage(other).prepare()
        other.execute("ROLLBACK")


    def test_corrupt_payload_bytes_refuse_reopen_without_mutation(self):
        pointer = self.record(receipt([{"symbol":"ETHUSDT","fundingIntervalHours":8}]))
        before = self.db.execute(
            "SELECT seq,endpoint,params_json,envelope_json,payload_digest "
            "FROM runtime_causal_receipts").fetchall()
        self.db.execute("PRAGMA ignore_check_constraints=ON")
        self.db.execute("UPDATE runtime_receipt_payloads SET payload_json='[]' "
                        "WHERE digest=?", (pointer["payload_sha256"],))
        self.db.close()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "corrupt causal"):
            ReceiptStorage(self.db).prepare()
        self.db.execute("ROLLBACK")
        after = self.db.execute(
            "SELECT seq,endpoint,params_json,envelope_json,payload_digest "
            "FROM runtime_causal_receipts").fetchall()
        self.assertEqual(after, before)
        self.assertEqual(self.db.execute(
            "SELECT payload_json FROM runtime_receipt_payloads").fetchone()[0], "[]")

    def test_restart_keeps_causal_rows_after_raw_expiry(self):
        causal = self.record(receipt([{"symbol":"ETHUSDT","fundingIntervalHours":8}]))
        self.db.execute("BEGIN")
        for n in range(20):
            self.store.record_noncausal_raw("/fapi/v1/time", {
                "endpoint":"/fapi/v1/time","params":{},"payload":{"serverTime":n},
                "received_at":"2026-10-17T00:00:00+00:00","source_timestamp_ms":n})
        self.db.execute("COMMIT")
        self.db.close()
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("BEGIN")
        reopened = ReceiptStorage(self.db, raw_limit=3)
        reopened.prepare()
        self.db.execute("COMMIT")
        self.assertEqual(reopened.reconstruct(causal["seq"])["payload"][0]["symbol"],
                         "ETHUSDT")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM runtime_noncausal_raw_receipts").fetchone()[0], 3)


@unittest.skipUnless(os.environ.get("STORAGE_SR1_TEN_MINUTE") == "1",
                     "opt-in STORAGE_SR1_TEN_MINUTE=1; Linux /proc/self/io required")
class TenMinuteStorageEvidenceTests(unittest.TestCase):
    DURATION_SECONDS = 600
    TARGET_BYTES_PER_SECOND = 50000

    @staticmethod
    def write_bytes():
        try:
            with open("/proc/self/io", encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("write_bytes:"):
                        return int(line.split(":", 1)[1])
        except OSError as exc:
            raise unittest.SkipTest("Linux /proc/self/io unavailable: " + str(exc))
        raise unittest.SkipTest("Linux write_bytes unavailable")

    def measure(self, writer):
        start = time.monotonic()
        before = self.write_bytes()
        count = 0
        while time.monotonic() - start < self.DURATION_SECONDS:
            writer(count)
            count += 1
            os.sync()
            # One synthetic five-second receipt cadence; elapsed time is real.
            remaining = 5.0 - ((time.monotonic() - start) % 5.0)
            if remaining < 4.9:
                time.sleep(remaining)
        elapsed = time.monotonic() - start
        total = self.write_bytes() - before
        self.assertGreaterEqual(elapsed, self.DURATION_SECONDS)
        rate = total / elapsed
        self.assertLess(rate, self.TARGET_BYTES_PER_SECOND,
                        dict(bytes=total, seconds=elapsed, bytes_per_second=rate))
        return dict(bytes=total, seconds=elapsed, bytes_per_second=rate,
                    operations=count)

    def test_ten_minute_contribution_separated_storage_rate(self):
        from runtime_equity_history import EquityHistoryStore, ExactReceiptStore
        results = {}
        with tempfile.TemporaryDirectory(prefix="sr1-ten-minute-") as directory:
            root = Path(directory)
            def connection(name):
                db = sqlite3.connect(root / (name + ".sqlite3"), isolation_level=None)
                db.execute("PRAGMA foreign_keys=ON")
                return db

            broker_db = connection("broker-hot")
            broker_db.execute("CREATE TABLE hot(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT)")
            def broker_writer(n):
                broker_db.execute("BEGIN")
                broker_db.execute("INSERT OR REPLACE INTO hot VALUES(1,?)",
                                  (json.dumps({"cash":"100","last_ts":n}),))
                broker_db.execute("COMMIT")
            results["broker_hot"] = self.measure(broker_writer)
            broker_db.close()

            history_db = connection("history")
            history_db.execute("BEGIN")
            history = EquityHistoryStore(history_db); history.prepare()
            history.migrate_legacy([]); history_db.execute("COMMIT")
            def history_writer(n):
                history_db.execute("BEGIN")
                history.append({"ts":str(n),"equity_usdt":"100","valuation_stale":False})
                history_db.execute("COMMIT")
            results["history"] = self.measure(history_writer)
            history_db.close()

            reference_db = connection("reference")
            reference_db.execute("BEGIN")
            reference = ExactReceiptStore(reference_db); reference.prepare()
            reference_db.execute("COMMIT")
            fixed_reference = {"endpoint":"/fapi/v1/exchangeInfo","params":{},
                               "payload":{"symbols":[]},"received_at":"synthetic",
                               "source_timestamp_ms":1}
            def reference_writer(n):
                reference_db.execute("BEGIN"); reference.put(fixed_reference)
                reference_db.execute("COMMIT")
            results["reference"] = self.measure(reference_writer)
            reference_db.close()

            funding_db = connection("fundingInfo")
            funding_db.execute("BEGIN")
            funding = ReceiptStorage(funding_db); funding.prepare()
            funding_db.execute("COMMIT")
            payload = [{"symbol":"ETHUSDT","fundingIntervalHours":8}]
            def funding_writer(n):
                value = receipt(payload, received="synthetic:" + str(n), source=n)
                funding_db.execute("BEGIN")
                funding.record_causal("/fapi/v1/fundingInfo", {}, value)
                funding_db.execute("COMMIT")
            results["fundingInfo"] = self.measure(funding_writer)
            payload_rows = funding_db.execute(
                "SELECT COUNT(*) FROM runtime_receipt_payloads").fetchone()[0]
            raw_rows = funding_db.execute(
                "SELECT COUNT(*) FROM runtime_noncausal_raw_receipts").fetchone()[0]
            self.assertEqual(payload_rows, 1)
            self.assertLessEqual(raw_rows, 256)
            funding_db.close()
        print("SR1 TEN_MINUTE " + json.dumps(results, sort_keys=True), flush=True)


if __name__ == "__main__":
    unittest.main()
