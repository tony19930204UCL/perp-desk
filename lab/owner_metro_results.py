"""Native Maker X2 artifact observer; no producer rewrites or trading calls."""
import hashlib
import json
import re
from owner_activity import _read_bytes, _source_meta
HASH = re.compile(r"^[0-9a-f]{64}$")
IDS = {f"{s}:{f}m:{k}:{t}:{x}" for s in ("ETHUSDT", "BTCUSDT")
       for f in (1, 15) for k in (".25", ".50") for t in (2, 5)
       for x in ("X1", "X2", "X3")}
_CACHE = {}


def read(root, base, attempt, expected=None):
    result = {"state": "unknown", "count": None, "total": 48,
              "metrics": [], "by_exit": {}, "accepted": False}
    if not isinstance(attempt, str) or not re.fullmatch(r"[\w-]{1,64}", attempt):
        return result
    if not isinstance(expected, dict) or not all(HASH.fullmatch(str(expected.get(k)))
            for k in ("input_manifest_sha256", "config_sha256")):
        return result
    source = expected.get("source_sha256")
    if not isinstance(source, dict) or not source or any(
            not isinstance(k, str) or not HASH.fullmatch(str(v))
            for k, v in source.items()):
        return result
    directory = tuple(base)
    if not directory or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", x)
                            or x in (".", "..") for x in directory):
        return result
    # Trusted root is the pre-authorized attempt output, not a request-supplied
    # HTTP path. Files are native cells/00_strategy.json etc, not root/attempt.
    def get(parts):
        signature = _source_meta(root, directory + tuple(parts))
        if signature[2] > 1048576:
            raise ValueError("oversized")
        key = (root, directory, tuple(parts))
        cached = _CACHE.get(key)
        if cached and cached[0] == signature:
            return cached[1], cached[2]
        raw, _ = _read_bytes(root, directory + tuple(parts), 1048576)
        obj = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        digest = hashlib.sha256(raw).hexdigest()
        _CACHE[key] = (signature, obj, digest)
        if len(_CACHE) > 360:
            _CACHE.pop(next(iter(_CACHE)))
        return obj, digest
    try:
        manifest, _ = get(("manifest.json",))
    except FileNotFoundError:
        manifest = None
    except (OSError, ValueError, UnicodeDecodeError):
        return {**result, "state": "invalid"}
    if manifest is not None:
        if (not isinstance(manifest, dict) or
                any(manifest.get(k) != expected[k] for k in (
                    "input_manifest_sha256", "config_sha256", "source_sha256")) or
                not isinstance(manifest.get("artifacts"), dict)):
            return {**result, "state": "old_or_invalid"}
    try:
        summary, digest = get(("results.json",))
        if not isinstance(summary, dict):
            raise ValueError("summary")
        if manifest is not None and manifest["artifacts"].get("results.json") != digest:
            raise ValueError("summary digest")
        rows = summary.get("cells")
        if not isinstance(rows, list) or len(rows) > 48:
            raise ValueError("summary cells")
    except FileNotFoundError:
        rows = None
    except (OSError, ValueError, UnicodeDecodeError):
        return {**result, "state": "invalid"}
    # Native report writes strategy + matched *before* final manifest. A complete
    # JSON pair can be tentatively counted only when the trusted attempt binding
    # pins producer source/input/config; without a summary its metrics remain NA.
    seen, records = set(), []
    for index in range(48):
        row = rows[index] if rows is not None and index < len(rows) else None
        cid = row.get("cell_id") if isinstance(row, dict) else None
        if cid is not None and cid not in IDS:
            return {**result, "state": "invalid"}
        if cid is not None and cid in seen:
            return {**result, "state": "duplicate"}
        if cid is not None:
            seen.add(cid)
        try:
            artifact = []
            for side in ("strategy", "matched"):
                name = ("cells", f"{index:02d}_{side}.json")
                obj, sha = get(name)
                if manifest is not None and manifest["artifacts"].get(
                        "/".join(name)) != sha:
                    raise ValueError("artifact digest")
                if obj is not None and not isinstance(obj, dict):
                    raise ValueError("artifact object")
                artifact.append(obj)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, UnicodeDecodeError):
            return {**result, "state": "invalid", "count": None, "metrics": []}
        strategy, matched = artifact
        if not isinstance(strategy, dict) or not isinstance(strategy.get("episodes"), list):
            return {**result, "state": "invalid", "count": None, "metrics": []}
        native = f"{strategy.get('symbol')}:{strategy.get('framework')}m:{strategy.get('k')}:{strategy.get('ttl')}:{strategy.get('exit')}"
        if native not in IDS or (cid is not None and native != cid) or native in seen and cid is None:
            return {**result, "state": "duplicate" if native in seen else "invalid", "count": None, "metrics": []}
        if cid is None:
            seen.add(native)
        if rows is not None and row is None:
            return {**result, "state": "invalid", "count": None, "metrics": []}
        metrics = row.get("metrics") if isinstance(row, dict) else None
        def number(key):
            value = metrics.get(key) if isinstance(metrics, dict) else None
            if value is None or value == "NOT_APPLICABLE":
                return None
            try:
                from decimal import Decimal
                dec = Decimal(str(value))
                return str(dec) if dec.is_finite() else None
            except (ValueError, TypeError, ArithmeticError):
                return None
        records.append({"cell": native, "exit": strategy.get("exit"),
                        "gross_bp": number("gross_bp"), "net_bp": number("net_bp"),
                        "profit_factor": number("profit_factor"),
                        "conditional_win_rate": number("tp_over_tp_plus_sl"),
                        "risk_denominator": metrics.get("tp_sl_sl_gap_denominator")
                            if isinstance(metrics, dict) else None,
                        "actual_time_share": number("actual_time_close_rate")})
    if rows is not None and manifest is not None and len(rows) != len(records):
        return {**result, "state": "invalid"}
    if not records and rows is None:
        return {**result, "state": "missing"}
    order = sorted(records, key=lambda x: float(x["net_bp"]) if x["net_bp"] is not None else float("-inf"), reverse=True)
    by_exit = {x: sum(r["exit"] == x for r in records) for x in ("X1", "X2", "X3")}
    return {**result, "state": "complete" if len(records) == 48 and manifest is not None
            and rows is not None else "preliminary", "count": len(records),
            "metrics": order, "by_exit": by_exit, "accepted": False}
