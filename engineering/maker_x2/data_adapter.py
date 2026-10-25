"""Offline historical 1m/funding decoder. Reads local files, never fetches.

CSV/JSON/JSONL/JSON.GZ and Binance 12-column kline arrays supported.
Preserves original UTC timestamps and explicitly rejects disorder/gaps.
"""
import csv
import gzip
import hashlib
import json
from pathlib import Path
from decimal import Decimal

from entry_lifecycle import MINUTE
from signal_features import validate_minutes
from maker_exit_lab import X2Error, number


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _objects(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    extension = path.suffixes[-2] if path.suffix == ".gz" else path.suffix
    with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
        if extension == ".csv":
            yield from csv.DictReader(handle)
        elif extension == ".json":
            # Bounded-memory incremental JSON array decoding: do not
            # json.load a multi-year Binance array into a giant list.
            decoder = json.JSONDecoder()
            buffer = ""
            eof = False
            def need():
                nonlocal buffer, eof
                chunk = handle.read(65536)
                if not chunk:
                    eof = True
                buffer += chunk
            need()
            buffer = buffer.lstrip()
            if not buffer.startswith("["):
                raise X2Error("JSON input must be an array")
            buffer = buffer[1:]
            after_comma = False
            while True:
                buffer = buffer.lstrip()
                if not buffer and not eof:
                    need()
                    continue
                if buffer.startswith("]"):
                    if after_comma:
                        raise X2Error("trailing JSON array comma")
                    buffer = buffer[1:]
                    while not eof:
                        need()
                    if buffer.strip():
                        raise X2Error("trailing data after JSON array")
                    break
                if eof and not buffer:
                    raise X2Error("truncated JSON array")
                try:
                    value, offset = decoder.raw_decode(buffer)
                except json.JSONDecodeError as exc:
                    if eof:
                        raise X2Error("invalid JSON array element") from exc
                    need()
                    continue
                yield value
                after_comma = False
                buffer = buffer[offset:]
                while True:
                    buffer = buffer.lstrip()
                    if not buffer and not eof:
                        need()
                        continue
                    if buffer.startswith(","):
                        after_comma = True
                        buffer = buffer[1:]
                        break
                    if buffer.startswith("]"):
                        break
                    if eof:
                        raise X2Error("JSON array separator expected")
                    need()
        elif extension in (".jsonl", ".ndjson"):
            for line in handle:
                if line.strip():
                    yield json.loads(line)
        else:
            raise X2Error("unsupported local input format")


def _integer(value, label):
    if isinstance(value, bool):
        raise X2Error(label + " is boolean")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise X2Error(label + " must be integer milliseconds") from exc
    if str(value).strip() not in (str(result),):
        raise X2Error(label + " must be exact integer")
    if result < 0:
        raise X2Error(label + " cannot be negative")
    return result


def read_bars(path):
    """Return original closed UTC minutes, source hash and gap inventory."""
    result = []
    previous = None
    gaps = []
    for item in _objects(path):
        if isinstance(item, (list, tuple)):
            if len(item) != 12:
                raise X2Error("Binance kline requires exactly 12 columns")
            ts, op, hi, lo, close, base, end, quote = item[:8]
            if _integer(end, "close time") != _integer(ts, "open time") + MINUTE - 1:
                raise X2Error("incomplete Binance minute")
        elif isinstance(item, dict):
            ts = item.get("open_time", item.get("openTime", item.get("ts")))
            op, hi, lo, close = [item[key] for key in
                                  ("open", "high", "low", "close")]
            base = item.get("base_volume", item.get("volume"))
            quote = item.get("quote_volume", item.get("quoteVolume"))
            if "close_time" in item:
                if _integer(item["close_time"], "close time") != _integer(ts, "open time") + MINUTE - 1:
                    raise X2Error("incomplete close time")
        else:
            raise X2Error("invalid bar input object")
        start = _integer(ts, "open_time")
        if previous is not None:
            if start <= previous:
                raise X2Error("duplicate or backward minute")
            if start != previous + MINUTE:
                gaps.append({"after_open_ms": previous, "before_open_ms": start,
                             "missing_minutes": (start-previous)//MINUTE-1})
        previous = start
        row = {"ts": start, "open": str(number(op, "open", True)),
               "high": str(number(hi, "high", True)),
               "low": str(number(lo, "low", True)),
               "close": str(number(close, "close", True)),
               "base_volume": str(number(base, "base volume")),
               "quote_volume": str(number(quote, "quote volume")),
               "closed": True}
        if Decimal(row["base_volume"]) < 0 or Decimal(row["quote_volume"]) < 0:
            raise X2Error("negative volume")
        result.append(row)
    validate_minutes(result)
    return {"bars": result, "sha256": sha256_file(path),
            "count": len(result), "gaps": gaps,
            "start_open_ms": result[0]["ts"] if result else None,
            "end_open_ms": result[-1]["ts"] if result else None}


def read_funding(path):
    """Original finalized funding timestamp/rate, optional actual mark.

    Missing markPrice remains None; caller must use last COMPLETED bar
    close available at funding timestamp, never a future close.
    """
    result = []
    seen = set()
    previous = None
    for item in _objects(path):
        if not isinstance(item, dict):
            raise X2Error("funding row must be a dictionary")
        stamp = _integer(item.get("funding_time_ms",
                                 item.get("fundingTime")), "funding timestamp")
        rate = str(number(item.get("rate", item.get("fundingRate")), "funding rate"))
        mark = item.get("markPrice", item.get("mark_price"))
        if previous is not None and stamp < previous:
            raise X2Error("backwards funding timestamp")
        if stamp in seen:
            raise X2Error("duplicate funding timestamp")
        seen.add(stamp)
        previous = stamp
        result.append({"ts": stamp, "rate": rate,
                       "mark": None if mark in (None, "") else
                               str(number(mark, "mark", True)),
                       "settlement_id": "funding-" + str(stamp)})
    return {"events": result, "sha256": sha256_file(path),
            "count": len(result)}
