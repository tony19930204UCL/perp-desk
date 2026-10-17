"""Isolated SQLite tests for receipt storage; no network or runtime services."""
import json
import sqlite3
import tempfile
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


if __name__ == "__main__":
    unittest.main()
