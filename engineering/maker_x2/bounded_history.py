"""Validated, disk-backed chronological minutes and feature records.

Internal capacity types can only be constructed by the validating preparers.
SQLite is temporary local scratch, not a second unverified input format.
"""
import json
import sqlite3
from collections import deque
from decimal import Decimal, localcontext
from pathlib import Path

from entry_lifecycle import MINUTE, closed_bar, atr14, vwap60
from maker_exit_lab import X2Error, number

D = Decimal
Z = D("0")
_TOKEN = object()
_DECIMALS = ("signal_high", "signal_close", "atr14", "vwap60",
             "sigma288", "std12", "r6", "z", "previous_h12", "previous_l12")


def _database(path, columns):
    dest = Path(path)
    if dest.exists():
        raise X2Error("prepared scratch file already exists")
    db = sqlite3.connect(str(dest))
    db.execute("CREATE TABLE records (idx INTEGER PRIMARY KEY, " + columns + ")")
    return db


class PreparedBars:
    def __init__(self, path, count, token):
        if token is not _TOKEN:
            raise X2Error("unvalidated prepared bars")
        self.path, self.count = str(path), count

    def __len__(self):
        return self.count

    def __iter__(self):
        db = sqlite3.connect("file:" + Path(self.path).resolve().as_posix() +
                             "?mode=ro", uri=True)
        try:
            for ts, op, hi, lo, cl, base, quote in db.execute(
                "SELECT ts,op,hi,lo,cl,base,quote FROM records ORDER BY idx"):
                yield {"ts": ts, "open": op, "high": hi, "low": lo,
                       "close": cl, "base_volume": base,
                       "quote_volume": quote, "closed": True}
        finally:
            db.close()


class PreparedDecisions:
    def __init__(self, path, count, token):
        if token is not _TOKEN:
            raise X2Error("unvalidated prepared decisions")
        self.path, self.count = str(path), count

    def __len__(self):
        return self.count

    def __iter__(self):
        db = sqlite3.connect("file:" + Path(self.path).resolve().as_posix() +
                             "?mode=ro", uri=True)
        try:
            for (raw,) in db.execute("SELECT payload FROM records ORDER BY idx"):
                record = json.loads(raw)
                for key in _DECIMALS:
                    record[key] = D(record[key])
                yield record
        finally:
            db.close()


def prepare_bars(path, destination):
    """Stream parse and fully validate old supported formats before sealing."""
    from data_adapter import _objects, _integer, sha256_file
    original_hash = sha256_file(path)
    db = _database(destination, "ts INTEGER, op TEXT, hi TEXT, lo TEXT, "
                   "cl TEXT, base TEXT, quote TEXT")
    last, count, gaps = None, 0, []
    start = None
    try:
        batch = []
        for raw in _objects(path):
            if isinstance(raw, (list, tuple)):
                if len(raw) != 12:
                    raise X2Error("Binance kline requires exactly 12 columns")
                ts, op, hi, lo, cl, base, end, quote = raw[:8]
                if (_integer(end, "close time") !=
                        _integer(ts, "open time") + MINUTE - 1):
                    raise X2Error("incomplete Binance minute")
            elif type(raw) is dict:
                ts = raw.get("open_time", raw.get("openTime", raw.get("ts")))
                op, hi, lo, cl = (raw[k] for k in
                                   ("open", "high", "low", "close"))
                base = raw.get("base_volume", raw.get("volume"))
                quote = raw.get("quote_volume", raw.get("quoteVolume"))
                if ("close_time" in raw and
                    _integer(raw["close_time"], "close time") !=
                        _integer(ts, "open time") + MINUTE - 1):
                    raise X2Error("incomplete close time")
            else:
                raise X2Error("invalid bar input object")
            ts = _integer(ts, "open_time")
            if ts % MINUTE or (last is not None and ts <= last):
                raise X2Error("unaligned/duplicate/nonascending UTC minute")
            if last is not None and ts != last + MINUTE:
                gaps.append({"after_open_ms": last, "before_open_ms": ts,
                             "missing_minutes": (ts-last)//MINUTE-1})
            fields = [str(number(x, k, True)) for x, k in
                      zip((op, hi, lo, cl),
                          ("open", "high", "low", "close"))]
            base = str(number(base, "base volume"))
            quote = str(number(quote, "quote volume"))
            if D(base) < Z or D(quote) < Z:
                raise X2Error("negative volume")
            closed_bar(dict(ts=ts, open=fields[0], high=fields[1],
                            low=fields[2], close=fields[3], closed=True))
            batch.append((count, ts, *fields, base, quote))
            if len(batch) == 4096:
                db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                               batch)
                batch.clear()
            if start is None:
                start = ts
            last, count = ts, count + 1
        if batch:
            db.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?)",
                           batch)
        db.commit()
    finally:
        db.close()
    if sha256_file(path) != original_hash:
        raise X2Error("bar input mutated during preparation")
    return {"bars": PreparedBars(destination, count, _TOKEN),
            "sha256": original_hash, "count": count, "gaps": gaps,
            "start_open_ms": start, "end_open_ms": last}


def _minute(row, idx):
    op, hi, lo, cl = closed_bar(row)
    return {"ts": row["ts"], "open": op, "high": hi, "low": lo,
            "close": cl, "base_volume": number(row["base_volume"], "base"),
            "quote_volume": number(row["quote_volume"], "quote"),
            "closed": True, "minute_index": idx}


def _eligible_window(frame, minutes, tf):
    from signal_features import sample_std
    current = frame[-1]
    with localcontext() as ctx:
        ctx.prec = 50
        returns = [frame[j]["close"]/frame[j-1]["close"] - 1
                   for j in range(1, 289)]
        sigma = sample_std(returns)
        if sigma <= Z:
            return None
        std12 = sample_std(returns[-12:])
        r6 = current["close"]/frame[-7]["close"] - 1
        z = r6/(sigma*D(6).sqrt())
        h12 = max(b["high"] for b in frame[-13:-1])
        l12 = min(b["low"] for b in frame[-13:-1])
        atr = atr14(minutes[-15:])
        vwap = vwap60(minutes[-60:])
    kind = "S1" if tf == 1 else "S2"
    fired = (z > D("2.5") if tf == 1 else
             std12 <= D(".5")*sigma and current["close"] < l12)
    stamp = current["decision_ts"]
    return {"framework_minutes": tf, "signal_type": kind,
            "signal_id": f"{kind}-{tf}m-{stamp}",
            "signal_time": stamp, "final_minute_ts": current["final_minute_ts"],
            "final_minute_index": current["final_minute_index"],
            "signal_high": current["high"], "signal_close": current["close"],
            "atr14": atr, "vwap60": vwap, "sigma288": sigma,
            "std12": std12, "r6": r6, "z": z,
            "previous_h12": h12, "previous_l12": l12, "triggered": fired}


def iter_eligible(bars, tf):
    """Constant-memory causal features from fully validated PreparedBars."""
    if not isinstance(bars, PreparedBars) or type(tf) is not int or tf not in (1, 15):
        raise X2Error("validated bars and framework 1/15 required")
    minutes = deque(maxlen=60)
    frames = deque(maxlen=290)
    minute_run = frame_run = 0
    previous_minute = previous_frame = None
    bucket = []
    for idx, row in enumerate(bars):
        minute = _minute(row, idx)
        minute_run = (minute_run + 1 if previous_minute is not None and
                      minute["ts"] == previous_minute + MINUTE else 1)
        previous_minute = minute["ts"]
        minutes.append(minute)
        current = None
        if tf == 1:
            current = dict(minute, final_minute_ts=minute["ts"],
                           final_minute_index=idx,
                           decision_ts=minute["ts"]+MINUTE)
        else:
            start = minute["ts"]//(15*MINUTE)*(15*MINUTE)
            if bucket and start != bucket[0]["ts"]:
                bucket = []
            bucket.append(minute)
            if len(bucket) == 15 and all(
                    b["ts"] == start + i*MINUTE
                    for i, b in enumerate(bucket)):
                current = {"ts": start, "open": bucket[0]["open"],
                           "high": max(b["high"] for b in bucket),
                           "low": min(b["low"] for b in bucket),
                           "close": bucket[-1]["close"], "closed": True,
                           "base_volume": sum((b["base_volume"] for b in bucket), Z),
                           "quote_volume": sum((b["quote_volume"] for b in bucket), Z),
                           "final_minute_ts": minute["ts"],
                           "final_minute_index": idx,
                           "decision_ts": minute["ts"]+MINUTE}
                bucket = []
        if current is None:
            continue
        frame_run = (frame_run + 1 if previous_frame is not None and
                     current["ts"] == previous_frame + tf*MINUTE else 1)
        previous_frame = current["ts"]
        frames.append(current)
        if frame_run < 290 or minute_run < 60:
            continue
        record = _eligible_window(list(frames), list(minutes), tf)
        if record is not None:
            yield record


def prepare_decisions(bars, tf, destination):
    db = _database(destination, "payload TEXT NOT NULL")
    count = 0
    try:
        batch = []
        for record in iter_eligible(bars, tf):
            encoded = json.dumps(
                {k: str(v) if isinstance(v, D) else v
                 for k, v in record.items()}, sort_keys=True)
            batch.append((count, encoded))
            count += 1
            if len(batch) == 2048:
                db.executemany("INSERT INTO records VALUES (?,?)", batch)
                batch.clear()
        if batch:
            db.executemany("INSERT INTO records VALUES (?,?)", batch)
        db.commit()
    finally:
        db.close()
    return PreparedDecisions(destination, count, _TOKEN)
