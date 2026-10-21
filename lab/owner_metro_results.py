"""Read-only bounded Maker X2 artifact checks, using shipped report keys."""
import hashlib
import json
import re
from owner_activity import _read_bytes

HASH = re.compile(r"^[0-9a-f]{64}$")
IDS = {f"{symbol}:{frame}m:{k}:{ttl}:{style}"
       for symbol in ("ETHUSDT", "BTCUSDT") for frame in (1, 15)
       for k in (".25", ".50") for ttl in (2, 5) for style in ("X1", "X2", "X3")}


def read(root, base, attempt):
    """Only an explicitly trusted output directory; never scan raw trade streams."""
    state = {"state": "missing", "count": 0, "total": 48, "metrics": []}
    if not isinstance(attempt, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", attempt):
        return state
    try:
        directory = tuple(base)
        if not directory or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", x) or x in (".", "..")
                                for x in directory):
            return state
        def get(name):
            raw, _ = _read_bytes(root, directory + (attempt, name), 1048576)
            return json.loads(raw), hashlib.sha256(raw).hexdigest()
        manifest, _ = get("manifest.json")
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("artifacts"), dict)
                or manifest.get("attempt_id") != attempt
                or not isinstance(manifest.get("source_sha256"), dict)
                or not HASH.fullmatch(str(manifest.get("input_manifest_sha256")))
                or not HASH.fullmatch(str(manifest.get("config_sha256")))):
            return {**state, "state": "invalid"}
        result, digest = get("results.json")
        if digest != manifest["artifacts"].get("results.json"):
            return {**state, "state": "invalid"}
        rows = result.get("cells") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > 48:
            return {**state, "state": "invalid"}
        valid, seen, metrics = 0, set(), []
        for index, cell in enumerate(rows):
            if not isinstance(cell, dict) or cell.get("cell_id") not in IDS:
                continue
            cid = cell["cell_id"]
            if cid in seen:
                return {**state, "state": "duplicate"}
            seen.add(cid)
            m = cell.get("metrics")
            if not isinstance(m, dict) or not all(
                    key in m for key in ("gross_bp", "net_bp", "profit_factor",
                                        "tp_sl_sl_gap_denominator", "tp_over_tp_plus_sl",
                                        "actual_time_close_rate")):
                continue
            for part in ("strategy", "matched"):
                name = f"cells/{index:02d}_{part}.json"
                expected = manifest["artifacts"].get(name)
                if not isinstance(expected, str) or not HASH.fullmatch(expected):
                    break
                raw, _ = _read_bytes(root, directory + (attempt, "cells",
                                      f"{index:02d}_{part}.json"), 1048576)
                if hashlib.sha256(raw).hexdigest() != expected:
                    break
                obj = json.loads(raw)
                if part == "strategy" and (not isinstance(obj, dict)
                        or obj.get("symbol") != cid.split(":")[0]
                        or not isinstance(obj.get("episodes"), list)):
                    break
            else:
                valid += 1
                metrics.append({"cell": cid, "gross_bp": m["gross_bp"],
                                "net_bp": m["net_bp"], "profit_factor": m["profit_factor"],
                                "conditional_win_rate": m["tp_over_tp_plus_sl"],
                                "risk_denominator": m["tp_sl_sl_gap_denominator"],
                                "actual_time_share": m["actual_time_close_rate"],
                                "exit": cid.split(":")[-1]})
        state.update(count=valid, state="preliminary" if valid < 48 else "complete",
                     metrics=metrics[:48], accepted=False)
        # This counts parseable cells, not accepted research or a shared portfolio.
        return state
    except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError):
        return {**state, "state": "invalid"}
