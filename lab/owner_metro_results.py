"""Native Maker X2 artifact observer; no producer rewrites or trading calls."""
import hashlib
import json
import re
from owner_activity import _read_bytes, _source_meta
HASH = re.compile(r"^[0-9a-f]{64}$")
GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")
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


def catalog_projection_sha(cat):
    """Compute canonical sha256 of derived catalog identity, excluding expected and projection_sha256."""
    payload = {k: v for k, v in cat.items() if k not in ("expected", "projection_sha256")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def read_catalog(catalog, expected=None):
    """Read a compact projection catalog (< 1MiB) marked DERIVED_VIEW with pinned provenance."""
    result = {"state": "invalid", "count": None, "total": 48,
              "metrics": [], "by_exit": {}, "accepted": False}
    if not isinstance(catalog, dict) or catalog.get("view") != "DERIVED_VIEW":
        return result
    if catalog.get("schema_version") != 1:
        return result
    head = catalog.get("head")
    if not isinstance(head, str) or not GIT_HEAD.fullmatch(head):
        return result
    # Pinned provenance is mandatory in both expected and catalog
    if expected is None or not isinstance(expected, dict):
        return result
    for req in ("head", "manifest_sha256", "results_sha256", "terminal_sha256",
                "config_sha256", "input_manifest_sha256", "source_sha256", "projection_sha256"):
        if req not in expected or req not in catalog:
            return result
    exp_proj = expected["projection_sha256"]
    cat_proj = catalog["projection_sha256"]
    if not isinstance(exp_proj, str) or not HASH.fullmatch(exp_proj):
        return result
    if not isinstance(cat_proj, str) or not HASH.fullmatch(cat_proj):
        return result
    if exp_proj != cat_proj:
        return result

    if not isinstance(expected["head"], str) or not GIT_HEAD.fullmatch(expected["head"]) or expected["head"] != head:
        return result
    for hkey in ("manifest_sha256", "results_sha256", "terminal_sha256", "config_sha256", "input_manifest_sha256"):
        val = expected[hkey]
        if not isinstance(val, str) or not HASH.fullmatch(val) or val != catalog.get(hkey):
            return result
        cat_val = catalog.get(hkey)
        if not isinstance(cat_val, str) or not HASH.fullmatch(cat_val):
            return result
    exp_sources = expected["source_sha256"]
    if not isinstance(exp_sources, dict) or not exp_sources:
        return result
    cat_sources = catalog.get("source_sha256")
    if not isinstance(cat_sources, dict) or cat_sources != exp_sources or any(
            not isinstance(k, str) or not isinstance(v, str) or not HASH.fullmatch(v)
            for k, v in exp_sources.items()):
        return result

    metrics = catalog.get("metrics")
    if not isinstance(metrics, list) or len(metrics) != 48:
        return result
    seen_ids = set()
    for m in metrics:
        if not isinstance(m, dict):
            return result
        cid = m.get("cell")
        if not isinstance(cid, str) or cid not in IDS or cid in seen_ids:
            return result
        seen_ids.add(cid)
    by_exit = catalog.get("by_exit")
    if not isinstance(by_exit, dict) or set(by_exit.keys()) != {"X1", "X2", "X3"}:
        return result
    if any(type(v) is not int or v < 0 for v in by_exit.values()) or sum(by_exit.values()) != 48:
        return result
    actual_by_exit = {x: sum(m.get("exit") == x for m in metrics) for x in ("X1", "X2", "X3")}
    if actual_by_exit != by_exit:
        return result

    calc_sha = catalog_projection_sha(catalog)
    if calc_sha != exp_proj:
        return result

    return {
        "state": "complete",
        "count": len(metrics),
        "total": 48,
        "metrics": metrics,
        "by_exit": by_exit,
        "accepted": False,
        "head": head,
        "classification": catalog.get("classification", "EXPLORATORY_NOT_ACCEPTED"),
        "completed_at": catalog.get("completed_at")
    }


def build_derived_catalog(results_file, manifest_file, terminal_file, cells_dir, expected):
    """Deterministic projection builder from native results.json, manifest.json and terminal.json.
    Performs bounded streaming hashing of all 96 native artifacts (mandatory) and outputs a compact catalog < 1MiB.
    """
    import os
    from decimal import Decimal

    if not isinstance(expected, dict):
        raise ValueError("expected provenance required")
    exp_head = expected.get("head")
    if not isinstance(exp_head, str) or not GIT_HEAD.fullmatch(exp_head):
        raise ValueError("expected head must be 40-character hex")
    for key in ("config_sha256", "input_manifest_sha256"):
        val = expected.get(key)
        if not isinstance(val, str) or not HASH.fullmatch(val):
            raise ValueError(f"expected {key} must be 64-character hex")
    exp_sources = expected.get("source_sha256")
    if not isinstance(exp_sources, dict) or not exp_sources or any(
            not isinstance(k, str) or not isinstance(v, str) or not HASH.fullmatch(v)
            for k, v in exp_sources.items()):
        raise ValueError("expected source_sha256 entries must be 64-character hex")

    def safe_read_file(path, max_bytes):
        if os.path.islink(path):
            raise ValueError(f"symlink rejected: {path}")
        size = os.path.getsize(path)
        if size > max_bytes:
            raise ValueError(f"file oversized: {path}")
        with open(path, "rb") as fd:
            return fd.read(max_bytes)

    def stream_hash(path, max_bytes=10485760):
        if os.path.islink(path):
            raise ValueError(f"symlink rejected: {path}")
        total = 0
        h = hashlib.sha256()
        with open(path, "rb") as fd:
            while chunk := fd.read(65536):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"file exceeds max streaming size: {path}")
                h.update(chunk)
        return h.hexdigest()

    # 1. Terminal verification
    term_bytes = safe_read_file(terminal_file, 65536)
    terminal = json.loads(term_bytes.decode("utf-8"))
    term_sha = hashlib.sha256(term_bytes).hexdigest()

    head = terminal.get("head")
    if not isinstance(head, str) or not GIT_HEAD.fullmatch(head):
        raise ValueError("terminal head must be 40-character hex")
    if head != exp_head:
        raise ValueError("head mismatch")
    if terminal.get("run_rc") != 0 or terminal.get("verify_rc") != 0 or terminal.get("hard_timeout") is True:
        raise ValueError("terminal run or verify failed")

    # 2. Manifest verification
    manifest_bytes = safe_read_file(manifest_file, 1048576)
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    if manifest.get("config_sha256") != expected["config_sha256"]:
        raise ValueError("config_sha256 mismatch")
    if manifest.get("input_manifest_sha256") != expected["input_manifest_sha256"]:
        raise ValueError("input_manifest_sha256 mismatch")
    man_sources = manifest.get("source_sha256")
    if not isinstance(man_sources, dict) or man_sources != exp_sources:
        raise ValueError("source_sha256 mismatch")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("invalid manifest artifacts")

    # 3. Results verification and extraction
    results_bytes = safe_read_file(results_file, 1048576)
    results_sha = hashlib.sha256(results_bytes).hexdigest()

    if "results.json" not in artifacts or artifacts["results.json"] != results_sha:
        raise ValueError("manifest artifacts['results.json'] mismatch with results file")

    results = json.loads(results_bytes.decode("utf-8"))
    cells = results.get("cells")
    if not isinstance(cells, list) or len(cells) != 48:
        raise ValueError("expected 48 cells in results")

    # 4. Mandatory all 96 artifact files verification
    if not cells_dir or not os.path.isdir(cells_dir):
        raise ValueError("cells_dir is mandatory")
    for index in range(48):
        for side in ("strategy", "matched"):
            rel = f"cells/{index:02d}_{side}.json"
            if rel not in artifacts:
                raise ValueError(f"missing manifest artifact entry: {rel}")
            target = os.path.join(cells_dir, f"{index:02d}_{side}.json")
            if not os.path.isfile(target):
                raise ValueError(f"missing mandatory cell artifact file: {target}")
            actual_sha = stream_hash(target)
            if actual_sha != artifacts[rel]:
                raise ValueError(f"artifact hash mismatch: {rel}")

    def number(val):
        if val is None or val == "NOT_APPLICABLE":
            return None
        try:
            dec = Decimal(str(val))
            return str(dec) if dec.is_finite() else None
        except (ValueError, TypeError, ArithmeticError):
            return None

    seen, records = set(), []
    for row in cells:
        cid = row.get("cell_id")
        if not isinstance(cid, str) or cid not in IDS:
            raise ValueError(f"invalid cell_id {cid}")
        if cid in seen:
            raise ValueError(f"duplicate cell_id {cid}")
        seen.add(cid)
        metrics = row.get("metrics", {})
        exit_val = cid.split(":")[-1]
        records.append({
            "cell": cid,
            "exit": exit_val,
            "gross_bp": number(metrics.get("gross_bp")),
            "net_bp": number(metrics.get("net_bp")),
            "profit_factor": number(metrics.get("profit_factor")),
            "conditional_win_rate": number(metrics.get("tp_over_tp_plus_sl")),
            "risk_denominator": metrics.get("tp_sl_sl_gap_denominator"),
            "actual_time_share": number(metrics.get("actual_time_close_rate"))
        })

    order = sorted(records, key=lambda x: float(x["net_bp"]) if x["net_bp"] is not None else float("-inf"), reverse=True)
    by_exit = {x: sum(r["exit"] == x for r in records) for x in ("X1", "X2", "X3")}

    proj_bytes = json.dumps({"count": 48, "total": 48, "by_exit": by_exit, "metrics": order},
                            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    proj_sha = hashlib.sha256(proj_bytes).hexdigest()

    catalog = {
        "schema_version": 1,
        "view": "DERIVED_VIEW",
        "head": head,
        "run_rc": terminal.get("run_rc", 0),
        "verify_rc": terminal.get("verify_rc", 0),
        "status": terminal.get("status", "SHIPPED_VERIFY_PASSED_NEEDS_OPERATOR_REPORT_REVIEW"),
        "classification": "EXPLORATORY_NOT_ACCEPTED",
        "accepted": False,
        "completed_at": terminal.get("at"),
        "elapsed_seconds": terminal.get("elapsed_seconds"),
        "manifest_sha256": manifest_sha,
        "results_sha256": results_sha,
        "terminal_sha256": term_sha,
        "source_sha256": exp_sources,
        "config_sha256": expected["config_sha256"],
        "input_manifest_sha256": expected["input_manifest_sha256"],
        "count": 48,
        "total": 48,
        "by_exit": by_exit,
        "metrics": order,
    }
    catalog["projection_sha256"] = catalog_projection_sha(catalog)
    return catalog
