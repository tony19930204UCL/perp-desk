"""Transactional receipt storage for PERSIST storage remediation.

Causal receipts are immutable and reconstructible.  Payload bytes are content
addressed so repeated fundingInfo bodies are stored once while every observation
retains its original envelope metadata.  Ordinary noncausal raw observations use
a separate bounded table; pruning that table can never remove causal evidence.
The caller owns BEGIN/COMMIT/ROLLBACK.
"""
import hashlib
import json
import math
import sqlite3

RAW_RECEIPT_LIMIT = 256
_HEX = frozenset("0123456789abcdef")

_PAYLOADS = """CREATE TABLE runtime_receipt_payloads (
    digest TEXT PRIMARY KEY NOT NULL CHECK(typeof(digest)='text' AND length(digest)=64 AND digest NOT GLOB '*[^0-9a-f]*'),
    payload_json TEXT NOT NULL CHECK(typeof(payload_json)='text' AND length(payload_json)>0)
)"""
_CAUSAL = """CREATE TABLE runtime_causal_receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT CHECK(seq>0),
    endpoint TEXT NOT NULL CHECK(typeof(endpoint)='text' AND length(endpoint)>0),
    params_json TEXT NOT NULL CHECK(typeof(params_json)='text' AND length(params_json)>0),
    envelope_json TEXT NOT NULL CHECK(typeof(envelope_json)='text' AND length(envelope_json)>0),
    payload_digest TEXT NOT NULL REFERENCES runtime_receipt_payloads(digest)
)"""
_RAW = """CREATE TABLE runtime_noncausal_raw_receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT CHECK(seq>0),
    endpoint TEXT NOT NULL CHECK(typeof(endpoint)='text' AND length(endpoint)>0),
    receipt_json TEXT NOT NULL CHECK(typeof(receipt_json)='text' AND length(receipt_json)>0)
)"""
_SCHEMAS = {
    "runtime_receipt_payloads": _PAYLOADS,
    "runtime_causal_receipts": _CAUSAL,
    "runtime_noncausal_raw_receipts": _RAW,
}


def _canonical(value):
    active = set()
    def check(item):
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                raise ValueError("cyclic JSON value")
            active.add(id(item))
            try:
                if kind is dict:
                    for key, child in item.items():
                        if type(key) is not str:
                            raise ValueError("JSON object keys must be strings")
                        check(child)
                else:
                    for child in item:
                        check(child)
            finally:
                active.remove(id(item))
        elif kind is float:
            if not math.isfinite(item):
                raise ValueError("nonfinite JSON number")
        elif item is not None and kind not in (str, int, bool):
            raise ValueError("unsupported JSON value")
    check(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      ensure_ascii=True)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_digest(value):
    return type(value) is str and len(value) == 64 and set(value) <= _HEX


class ReceiptStorage:
    """Strict storage facade; never owns a transaction."""

    def __init__(self, connection, *, raw_limit=RAW_RECEIPT_LIMIT):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("sqlite3.Connection required")
        if type(raw_limit) is not int or raw_limit < 0:
            raise ValueError("raw_limit must be a nonnegative integer")
        self.db = connection
        self.raw_limit = raw_limit

    def _writer(self):
        if not self.db.in_transaction:
            raise RuntimeError("caller must BEGIN before receipt storage writes")

    def prepare(self):
        self._writer()
        existing = {name: (kind, sql) for name, kind, sql in self.db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)",
            tuple(_SCHEMAS))}
        if existing and set(existing) != set(_SCHEMAS):
            raise ValueError("partial runtime receipt storage schema")
        for name, value in existing.items():
            if value != ("table", _SCHEMAS[name]):
                raise ValueError("incompatible runtime receipt storage schema: " + name)
        if not existing:
            self.db.execute("PRAGMA foreign_keys=ON")
            for sql in _SCHEMAS.values():
                self.db.execute(sql)
        unexpected = self.db.execute(
            "SELECT name FROM sqlite_master WHERE tbl_name IN (?,?,?) "
            "AND type IN ('trigger','index') AND name NOT LIKE 'sqlite_autoindex_%'",
            tuple(_SCHEMAS)).fetchall()
        if unexpected:
            raise ValueError("unexpected runtime receipt storage object")
        for digest, payload_json in self.db.execute(
                "SELECT digest,payload_json FROM runtime_receipt_payloads"):
            if not _valid_digest(digest) or _digest(payload_json) != digest:
                raise ValueError("corrupt receipt payload archive")
            if _canonical(json.loads(payload_json)) != payload_json:
                raise ValueError("noncanonical receipt payload archive")
        for seq, endpoint, params_json, envelope_json, payload_digest in self.db.execute(
                "SELECT seq,endpoint,params_json,envelope_json,payload_digest "
                "FROM runtime_causal_receipts"):
            if (type(seq) is not int or seq <= 0 or type(endpoint) is not str
                    or not endpoint or not _valid_digest(payload_digest)):
                raise ValueError("invalid causal receipt row")
            params = json.loads(params_json)
            envelope = json.loads(envelope_json)
            if _canonical(params) != params_json or _canonical(envelope) != envelope_json:
                raise ValueError("noncanonical causal receipt metadata")
            if "payload" in envelope:
                raise ValueError("causal envelope must not duplicate payload")
            if self.db.execute(
                    "SELECT 1 FROM runtime_receipt_payloads WHERE digest=?",
                    (payload_digest,)).fetchone() is None:
                raise ValueError("missing causal receipt payload pointer")

    def _put_payload(self, payload):
        text = _canonical(payload)
        digest = _digest(text)
        row = self.db.execute(
            "SELECT payload_json FROM runtime_receipt_payloads WHERE digest=?",
            (digest,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO runtime_receipt_payloads(digest,payload_json) VALUES(?,?)",
                (digest, text))
        elif row[0] != text or _digest(row[0]) != digest:
            raise ValueError("receipt payload digest collision or corruption")
        return digest

    def record_causal(self, endpoint, params, receipt):
        """Append every causal observation while deduplicating only payload bytes."""
        self._writer()
        if type(endpoint) is not str or not endpoint or type(params) is not dict:
            raise ValueError("endpoint and params required")
        if type(receipt) is not dict or "payload" not in receipt:
            raise ValueError("complete receipt with payload required")
        envelope = dict(receipt)
        payload = envelope.pop("payload")
        payload_digest = self._put_payload(payload)
        cursor = self.db.execute(
            "INSERT INTO runtime_causal_receipts(endpoint,params_json,envelope_json,payload_digest) "
            "VALUES(?,?,?,?)",
            (endpoint, _canonical(params), _canonical(envelope), payload_digest))
        return dict(seq=cursor.lastrowid, payload_sha256=payload_digest)

    def reconstruct(self, seq):
        if type(seq) is not int or seq <= 0:
            raise ValueError("positive causal receipt sequence required")
        row = self.db.execute(
            "SELECT endpoint,params_json,envelope_json,payload_digest "
            "FROM runtime_causal_receipts WHERE seq=?", (seq,)).fetchone()
        if row is None:
            raise KeyError("unknown causal receipt")
        endpoint, params_json, envelope_json, payload_digest = row
        payload_row = self.db.execute(
            "SELECT payload_json FROM runtime_receipt_payloads WHERE digest=?",
            (payload_digest,)).fetchone()
        if payload_row is None:
            raise ValueError("missing causal receipt payload pointer")
        payload_json = payload_row[0]
        if not _valid_digest(payload_digest) or _digest(payload_json) != payload_digest:
            raise ValueError("corrupt causal receipt payload pointer")
        receipt = json.loads(envelope_json)
        receipt["payload"] = json.loads(payload_json)
        if receipt.get("endpoint") != endpoint or receipt.get("params", {}) != json.loads(params_json):
            raise ValueError("causal receipt endpoint/params identity mismatch")
        return receipt

    def record_noncausal_raw(self, endpoint, receipt):
        """Bounded raw diagnostics; never referenced by causal accounting."""
        self._writer()
        if type(endpoint) is not str or not endpoint:
            raise ValueError("endpoint required")
        text = _canonical(receipt)
        cursor = self.db.execute(
            "INSERT INTO runtime_noncausal_raw_receipts(endpoint,receipt_json) VALUES(?,?)",
            (endpoint, text))
        if self.raw_limit == 0:
            self.db.execute("DELETE FROM runtime_noncausal_raw_receipts")
        else:
            self.db.execute(
                "DELETE FROM runtime_noncausal_raw_receipts WHERE seq < "
                "(SELECT COALESCE(MAX(seq),0)-?+1 FROM runtime_noncausal_raw_receipts)",
                (self.raw_limit,))
        return cursor.lastrowid
