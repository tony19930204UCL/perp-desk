"""Create one immutable stage_relay graph; stdlib only, never execute stages.

--evidence-root names the installed .../stage-relay/C0041 NAMESPACE.
The supervisor's graph.evidence_root is its child .../C0041/evidence.
--out must be .../C0041/graph.json; --output-root is a NEW directory
inside .../C0041/evidence. All other path arguments must be absolute.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
STAGES = ("r4-run", "r4-verify", "r4-reflection")
DENIED_NAMES = ("installed_auto_accept.py", "read_file", "agy/")


class GraphError(ValueError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise GraphError("absolute unambiguous path required")
    return path.resolve()


def confined(root, path):
    path = absolute(path)
    if path == root or root not in path.parents:
        raise GraphError("path outside installed evidence namespace")
    return path


def validate(graph):
    if graph.get("schema_version") != 1 or graph.get("kind") != "stage_relay":
        raise GraphError("invalid controller graph kind")
    root = absolute(graph["evidence_root"])
    if tuple(root.parts[-3:])[-3:] != ("stage-relay", "C0041", "evidence"):
        raise GraphError("invalid graph evidence namespace")
    names = [s["id"] for s in graph["stages"]]
    if names != list(STAGES):
        raise GraphError("missing or duplicated R4 stages")
    if not isinstance(graph.get("bootstrap"), dict):
        raise GraphError("missing external bootstrap")
    for stage, wanted in zip(graph["stages"], (144, 1, 1)):
        command = stage.get("command")
        if (stage.get("mode") != "exec" or
                not isinstance(command, list) or not command or
                any(not isinstance(v, str) or not v for v in command) or
                any(any(d in v.lower().replace("\\", "/") for d in DENIED_NAMES)
                    for v in command)):
            raise GraphError("missing/denied command")
        if (not HEX40.fullmatch(str(stage.get("source_head"))) or
                not HEX64.fullmatch(str(stage.get("input_sha256"))) or
                not Path(stage.get("cwd", "")).is_absolute() or
                type(stage.get("lease_seconds")) is not int or
                not 1 <= stage["lease_seconds"] <= 3600):
            raise GraphError("invalid stage identity/cwd/lease")
        report = stage.get("reports")
        if not isinstance(report, list) or len(report) != 1:
            raise GraphError("exactly one report required")
        spec = report[0]
        if type(spec.get("rows")) is not int or spec["rows"] != wanted:
            raise GraphError("wrong report rows")
        if not isinstance(spec.get("path"), str) or not spec["path"]:
            raise GraphError("missing relative report path")
        if Path(spec["path"]).is_absolute() or ".." in Path(spec["path"]).parts:
            raise GraphError("report escapes evidence")
        if wanted == 144 and spec.get("symbol_cells") != {
                "ETHUSDT": 72, "BTCUSDT": 72}:
            raise GraphError("wrong symbol coverage")
    if "r4_reflection.py" not in graph["stages"][2]["command"]:
        raise GraphError("reflection must use r4_reflection.py")
    return graph


def build(ns, head, inp, config, output, proof, python, out):
    ns = absolute(ns)
    if tuple(ns.parts[-2:]) != ("stage-relay", "C0041"):
        raise GraphError("evidence-root must end stage-relay/C0041")
    evidence = ns / "evidence"
    output = confined(evidence, output)
    if output.exists():
        raise GraphError("run output already exists; do not overwrite")
    out = absolute(out)
    if out != ns / "graph.json":
        raise GraphError("installed graph must be C0041/graph.json")
    if out.exists():
        raise GraphError("existing graph cannot be overwritten")
    inp, config, python = (absolute(v) for v in (inp, config, python))
    if not inp.is_file() or not config.is_file() or not python.is_file():
        raise GraphError("missing input/config/python source")
    if not HEX40.fullmatch(head):
        raise GraphError("source_head must be exact 40 lowercase hex")
    proof = confined(evidence, proof)
    if not proof.is_file():
        raise GraphError("missing bootstrap acceptance")
    data = json.loads(proof.read_text(encoding="utf-8"))
    prior = data.get("stage_id")
    prev_head, prev_inp = data.get("source_head"), data.get("input_sha256")
    if (not prior or not HEX40.fullmatch(str(prev_head)) or
            not HEX64.fullmatch(str(prev_inp)) or
            data.get("decision") != "ACCEPTED" or not data.get("reviewer")):
        raise GraphError("unaccepted upstream bootstrap")
    witness = data.get("evidence_path")
    if not isinstance(witness, str) or Path(witness).is_absolute():
        raise GraphError("invalid upstream evidence path")
    witness_path = confined(evidence, evidence / witness)
    if not witness_path.is_file() or sha(witness_path) != data.get("evidence_sha256"):
        raise GraphError("bootstrap evidence hash mismatch")
    source_sha = sha(inp)
    rel = output.relative_to(evidence).as_posix()
    cwd = Path(__file__).resolve().parent
    common = ["--config", str(config), "--input", str(inp),
              "--output", str(output), "--stage-config", str(out)]
    stages = []
    for name, filename, flag, count in zip(STAGES,
            ("r4_entry.py", "r4_entry.py", "r4_reflection.py"),
            ("--run", "--verify", None), (144, 1, 1)):
        argv = (["prlimit", "--as=5368709120", "--", str(python), "-u", filename]
                if name == "r4-run" else [str(python), "-u", filename])
        if flag:
            argv += [flag] + common
        else:
            argv += common + ["--report",
                str(output / "reports/r4-reflection.json")]
        if name != "r4-run":
            previous = "r4-run" if name == "r4-verify" else "r4-verify"
            argv += ["--receipt", str(evidence / f"inbox/{previous}.receipt.json"),
                     "--acceptance", str(evidence / f"inbox/{previous}.acceptance.json")]
        report_name = ("r4-matrix.json" if name == "r4-run" else
                       "r4-verify.json" if name == "r4-verify" else
                       "r4-reflection.json")
        spec = {"path": f"{rel}/reports/{report_name}", "rows": count}
        if count == 144:
            spec["symbol_cells"] = {"ETHUSDT": 72, "BTCUSDT": 72}
        stages.append({"id": name, "mode": "exec", "source_head": head,
                       "input_sha256": source_sha, "cwd": str(cwd),
                       "lease_seconds": 3600, "command": argv,
                       "reports": [spec]})
    graph = {"schema_version": 1, "kind": "stage_relay",
             "evidence_root": str(evidence),
             "bootstrap": {"acceptance_path": proof.relative_to(evidence).as_posix(),
                           "stage_id": prior, "source_head": prev_head,
                           "input_sha256": prev_inp},
             "stages": stages}
    validate(graph)
    return graph


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("evidence-root", "source-head", "input-json", "config",
                "output-root", "bootstrap-acceptance-json", "python", "out"):
        p.add_argument("--" + arg, required=True)
    args = p.parse_args(argv)
    try:
        graph = build(args.evidence_root, args.source_head, args.input_json,
                      args.config, args.output_root, args.bootstrap_acceptance_json,
                      args.python, args.out)
        data = (json.dumps(graph, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        with open(args.out, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"graph_sha256": hashlib.sha256(data).hexdigest(),
                          "bootstrap_acceptance_sha256": sha(args.bootstrap_acceptance_json),
                          "stage_count": 3}, sort_keys=True))
        return 0
    except (GraphError, OSError, ValueError, KeyError, TypeError) as exc:
        print("R4 GRAPH BLOCKED: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
