"""Comprehensive regression tests for owner metro truth fixes.
All test names <= 25 chars.
TemporaryDirectory uses dir=.test-tmp.
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from owner_metro_model import read_metro, _evaluate_prohibitions
from owner_metro_nodes import derive
from owner_metro_results import build_derived_catalog, read_catalog, IDS

TEST_TMP = Path(__file__).resolve().parents[2] / ".test-tmp"
TEST_TMP.mkdir(parents=True, exist_ok=True)


def canonical_hash(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class OwnerMetroTruthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(TEST_TMP))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for d in ("commitments", "owner_metro", "cron", "lab/journal", "lab/shared", "proc"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.proc = self.root / "proc"
        self.now_ms = 1792672200000

    def write_ledger(self, items):
        (self.root / "commitments/ledger.json").write_text(
            json.dumps({"next": len(items) + 1, "items": items})
        )

    def write_bindings(self, sources=None, nodes=None):
        payload = {"schema_version": 1, "sources": sources or {}}
        if nodes is not None:
            payload["nodes"] = nodes
        (self.root / "owner_metro/bindings.json").write_text(json.dumps(payload))

    def test_old_node_dropped(self):
        """Old bound node with state DROPPED must NOT be rendered failed."""
        ledger_items = [
            {"id": "C-101", "state": "VERIFIED"},
            {"id": "C-102", "state": "VERIFIED"},
            {"id": "C-103", "state": "DROPPED", "reason": "superseded by replacement"},
        ]
        self.write_ledger(ledger_items)
        nodes = [
            {
                "route": "relay",
                "step": 2,
                "items": ["C-101", "C-102", "C-103"],
                "gate": "C-103",
            }
        ]
        overrides, warnings = derive(
            str(self.root), ledger_items, nodes, self.now_ms, proc_root=str(self.proc)
        )
        self.assertNotIn(("relay", 2), overrides, "DROPPED node should not override as failed")

    def test_policy_absent(self):
        """Negative test: rule id absent from manifest stays pending."""
        c201 = {
            "id": "C-201",
            "text": "Task absent from manifest",
            "state": "OPEN",
            "blocked_by": "user:Should we proceed?",
            "created": 1792250000.0,
            "updated": 1792250000.0,
        }
        self.write_ledger([c201])
        frozen_manifest = {"schema_version": 1, "items": []}
        man_raw = json.dumps(frozen_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_sha = hashlib.sha256(man_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_raw)

        decision_policy = {
            "schema_version": 1,
            "manifest_sha256": man_sha,
            "rules": [
                {
                    "id": "C-201",
                    "classification": "prohibition",
                    "pin_sha256": canonical_hash(c201),
                    "note": "Rule for absent item",
                }
            ],
        }
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(decision_policy))
        self.write_bindings(
            sources={
                "frozen_manifest": "owner_metro/frozen_manifest.json",
                "decision_policy": "owner_metro/decision_policy.json",
                "directive": "owner_metro/directive.json",
            }
        )
        (self.root / "owner_metro/directive.json").write_text(
            json.dumps({"updated_at": 1792625361, "pending_decisions": 99})
        )
        metro = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        pending_ids = [p["id"] for p in metro["kpis"]["pending_list"]]
        self.assertIn("C-201", pending_ids)
        self.assertEqual(metro["kpis"]["pending_decisions"], 1)

    def test_policy_tamper(self):
        """Negative test: tampered manifest original payload or tampered ledger text stays pending."""
        c202_ledger = {
            "id": "C-202",
            "text": "Altered ledger text",
            "state": "OPEN",
            "blocked_by": "user:Question?",
            "created": 1792250000.0,
            "updated": 1792250000.0,
        }
        self.write_ledger([c202_ledger])
        c202_orig = {"id": "C-202", "text": "Original manifest text", "blocked_by": "user:Question?"}
        frozen_manifest = {
            "schema_version": 1,
            "items": [
                {
                    "id": "C-202",
                    "original_payload": c202_orig,
                    "original_payload_sha256": canonical_hash(c202_orig),
                }
            ],
        }
        man_raw = json.dumps(frozen_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_sha = hashlib.sha256(man_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_raw)

        decision_policy = {
            "schema_version": 1,
            "manifest_sha256": man_sha,
            "rules": [
                {
                    "id": "C-202",
                    "classification": "prohibition",
                    "pin_sha256": canonical_hash(c202_orig),
                    "note": "Tampered item",
                }
            ],
        }
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(decision_policy))
        self.write_bindings(
            sources={
                "frozen_manifest": "owner_metro/frozen_manifest.json",
                "decision_policy": "owner_metro/decision_policy.json",
                "directive": "owner_metro/directive.json",
            }
        )
        (self.root / "owner_metro/directive.json").write_text(
            json.dumps({"updated_at": 1792625361, "pending_decisions": 99})
        )
        metro = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        pending_ids = [p["id"] for p in metro["kpis"]["pending_list"]]
        self.assertIn("C-202", pending_ids)
        self.assertEqual(metro["kpis"]["pending_decisions"], 1)

    def test_policy_valid(self):
        """Valid typed prohibition excluded, history retained, unrelated gate retained, DROPPED excluded."""
        c203 = {
            "id": "C-203",
            "text": "Valid prohibition task",
            "state": "OPEN",
            "blocked_by": "user:Do not run paper?",
            "created": 1792250000.0,
            "updated": 1792250000.0,
        }
        c204 = {
            "id": "C-204",
            "text": "Unrelated gate in same manifest",
            "state": "OPEN",
            "blocked_by": "user:Confirm test rollout?",
            "created": 1792250000.0,
            "updated": 1792250000.0,
        }
        c205 = {
            "id": "C-205",
            "text": "Dropped task",
            "state": "DROPPED",
            "blocked_by": "user:Old question?",
            "created": 1792250000.0,
            "updated": 1792250000.0,
        }
        self.write_ledger([c203, c204, c205])
        frozen_manifest = {
            "schema_version": 1,
            "items": [
                {
                    "id": "C-203",
                    "original_payload": dict(c203),
                    "original_payload_sha256": canonical_hash(c203),
                },
                {
                    "id": "C-204",
                    "original_payload": dict(c204),
                    "original_payload_sha256": canonical_hash(c204),
                },
            ],
        }
        man_raw = json.dumps(frozen_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_sha = hashlib.sha256(man_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_raw)

        decision_policy = {
            "schema_version": 1,
            "manifest_sha256": man_sha,
            "rules": [
                {
                    "id": "C-203",
                    "classification": "prohibition",
                    "pin_sha256": canonical_hash(c203),
                    "note": "Prohibition policy retained",
                }
            ],
        }
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(decision_policy))
        self.write_bindings(
            sources={
                "frozen_manifest": "owner_metro/frozen_manifest.json",
                "decision_policy": "owner_metro/decision_policy.json",
                "directive": "owner_metro/directive.json",
            }
        )
        (self.root / "owner_metro/directive.json").write_text(
            json.dumps({"updated_at": 1792625361, "pending_decisions": 99})
        )
        metro = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        pending_ids = [p["id"] for p in metro["kpis"]["pending_list"]]
        self.assertEqual(pending_ids, ["C-204"])
        self.assertEqual(metro["kpis"]["pending_decisions"], 1)
        self.assertEqual(metro["kpis"]["pending_source"], "ledger")
        history_ids = [h["id"] for h in metro["history"]]
        self.assertIn("C-203", history_ids)
        self.assertIn("C-205", history_ids)

    def test_policy_wrong_id(self):
        """Manifest member original_payload.id != rule/member id stays pending."""
        row = {"id": "C-902", "text": "task", "blocked_by": "user:q", "state": "OPEN"}
        original = {**row, "id": "C-unrelated"}
        manifest = {"schema_version": 1, "items": [{"id": row["id"], "original_payload": original, "original_payload_sha256": canonical_hash(original)}]}
        docs = {"frozen_manifest": manifest, "decision_policy": {"schema_version": 1, "manifest_sha256": canonical_hash(manifest), "rules": [{"id": row["id"], "classification": "prohibition", "pin_sha256": canonical_hash(original)}]}}
        self.assertEqual(_evaluate_prohibitions(docs, {}, [row]), {})

    def test_policy_null_items(self):
        """frozen_manifest items=None should not raise TypeError and return {}."""
        docs = {"frozen_manifest": {"schema_version": 1, "items": None},
                "decision_policy": {"schema_version": 1, "manifest_sha256": "x", "rules": []}}
        self.assertEqual(_evaluate_prohibitions(docs, {}, []), {})

    def test_pending_canonical(self):
        """Fresh directive with pending 99 and old active owners must be driven by canonical ledger."""
        self.write_ledger([{"id": "C-901", "state": "OPEN", "text": "new", "blocked_by": "user:real question"}])
        (self.root / "owner_metro/directive.json").write_text(
            json.dumps({"updated_at": 1792767000, "pending_decisions": 99,
                        "active_owners": [{"item": "C-old", "product": "maker", "actor": "old"}]})
        )
        self.write_bindings(sources={"directive": "owner_metro/directive.json"})
        o = read_metro(str(self.root), now_ms=1792767000000, proc_root=str(self.proc))
        self.assertEqual(o["kpis"]["pending_decisions"], len(o["kpis"]["pending_list"]))
        self.assertEqual(o["kpis"]["pending_source"], "ledger")
        self.assertFalse(any(x.get("item") == "C-old" for x in o["directive"]["owners"]))

    def _setup_raw_artifacts(self, head_40):
        raw_dir = self.root / "raw_results"
        cells_dir = raw_dir / "cells"
        cells_dir.mkdir(parents=True, exist_ok=True)
        config_sha = "b" * 64
        input_sha = "c" * 64
        source_sha = {"backtest_entry.py": "d" * 64}
        expected = {
            "head": head_40,
            "config_sha256": config_sha,
            "input_manifest_sha256": input_sha,
            "source_sha256": source_sha,
        }
        artifacts = {}
        cell_rows = []
        for index, cid in enumerate(sorted(IDS)):
            exit_val = cid.split(":")[-1]
            for side in ("strategy", "matched"):
                rel = f"cells/{index:02d}_{side}.json"
                content = json.dumps({"cell": cid, "side": side, "index": index}).encode("utf-8")
                (raw_dir / rel).write_bytes(content)
                artifacts[rel] = hashlib.sha256(content).hexdigest()
            cell_rows.append({
                "cell_id": cid,
                "metrics": {
                    "gross_bp": "-1.5",
                    "net_bp": str(index),
                    "profit_factor": "0.8",
                    "tp_over_tp_plus_sl": "0.5",
                    "tp_sl_sl_gap_denominator": 100,
                    "actual_time_close_rate": "NOT_APPLICABLE",
                }
            })
        results_data = {"cell_count": 48, "cells": cell_rows}
        results_bytes = json.dumps(results_data).encode("utf-8")
        results_sha = hashlib.sha256(results_bytes).hexdigest()
        (raw_dir / "results.json").write_bytes(results_bytes)
        artifacts["results.json"] = results_sha
        manifest_data = {
            "config_sha256": config_sha,
            "input_manifest_sha256": input_sha,
            "source_sha256": source_sha,
            "artifacts": artifacts,
        }
        (raw_dir / "manifest.json").write_text(json.dumps(manifest_data))
        terminal_data = {
            "head": head_40,
            "run_rc": 0,
            "verify_rc": 0,
            "hard_timeout": False,
            "at": 1792672132.93,
            "elapsed_seconds": 7755.98,
            "status": "SHIPPED_VERIFY_PASSED_NEEDS_OPERATOR_REPORT_REVIEW",
        }
        (raw_dir / "terminal.json").write_text(json.dumps(terminal_data))
        return raw_dir, cells_dir, expected

    def test_catalog_build(self):
        """Test build_derived_catalog requires 40-char head, rejects 64-char head, verifies 96 files."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)

        # 1. 64-char head rejected
        bad_expected = dict(expected, head="a" * 64)
        with self.assertRaises(ValueError):
            build_derived_catalog(
                str(raw_dir / "results.json"),
                str(raw_dir / "manifest.json"),
                str(raw_dir / "terminal.json"),
                str(cells_dir),
                bad_expected,
            )

        # 2. Missing cells_dir rejected
        with self.assertRaises(ValueError):
            build_derived_catalog(
                str(raw_dir / "results.json"),
                str(raw_dir / "manifest.json"),
                str(raw_dir / "terminal.json"),
                None,
                expected,
            )

        # 3. Successful build
        catalog = build_derived_catalog(
            str(raw_dir / "results.json"),
            str(raw_dir / "manifest.json"),
            str(raw_dir / "terminal.json"),
            str(cells_dir),
            expected,
        )
        self.assertEqual(catalog["view"], "DERIVED_VIEW")
        self.assertEqual(catalog["head"], head_40)
        self.assertEqual(catalog["count"], 48)
        self.assertEqual(catalog["total"], 48)
        self.assertFalse(catalog["accepted"])
        self.assertEqual(catalog["by_exit"], {"X1": 16, "X2": 16, "X3": 16})
        self.assertLess(len(json.dumps(catalog).encode("utf-8")), 1048576)

    def test_catalog_read(self):
        """read_catalog requires expected provenance with 40-char head and verified sources."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        catalog = build_derived_catalog(
            str(raw_dir / "results.json"),
            str(raw_dir / "manifest.json"),
            str(raw_dir / "terminal.json"),
            str(cells_dir),
            expected,
        )
        # Without expected: returns invalid
        self.assertEqual(read_catalog(catalog)["state"], "invalid")

        # With complete matching expected: returns complete
        cat_expected = {
            "head": head_40,
            "manifest_sha256": catalog["manifest_sha256"],
            "results_sha256": catalog["results_sha256"],
            "terminal_sha256": catalog["terminal_sha256"],
            "config_sha256": catalog["config_sha256"],
            "input_manifest_sha256": catalog["input_manifest_sha256"],
            "source_sha256": catalog["source_sha256"],
            "projection_sha256": catalog["projection_sha256"],
        }
        res = read_catalog(catalog, cat_expected)
        self.assertEqual(res["state"], "complete")
        self.assertEqual(res["count"], 48)
        self.assertFalse(res["accepted"])

    def test_catalog_mismatch(self):
        """read_catalog fails closed on head, hash or source mismatch."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        catalog = build_derived_catalog(
            str(raw_dir / "results.json"),
            str(raw_dir / "manifest.json"),
            str(raw_dir / "terminal.json"),
            str(cells_dir),
            expected,
        )
        cat_expected = {
            "head": head_40,
            "manifest_sha256": catalog["manifest_sha256"],
            "results_sha256": catalog["results_sha256"],
            "terminal_sha256": catalog["terminal_sha256"],
            "config_sha256": catalog["config_sha256"],
            "input_manifest_sha256": catalog["input_manifest_sha256"],
            "source_sha256": catalog["source_sha256"],
            "projection_sha256": catalog["projection_sha256"],
        }
        # Head mismatch
        self.assertEqual(read_catalog(catalog, dict(cat_expected, head="0" * 40))["state"], "invalid")
        # Manifest hash mismatch
        self.assertEqual(read_catalog(catalog, dict(cat_expected, manifest_sha256="0" * 64))["state"], "invalid")
        # Results hash mismatch
        self.assertEqual(read_catalog(catalog, dict(cat_expected, results_sha256="0" * 64))["state"], "invalid")
        # Terminal hash mismatch
        self.assertEqual(read_catalog(catalog, dict(cat_expected, terminal_sha256="0" * 64))["state"], "invalid")
        # Source mismatch
        self.assertEqual(read_catalog(catalog, dict(cat_expected, source_sha256={"other.py": "0" * 64}))["state"], "invalid")

    def test_catalog_str_exit(self):
        """read_catalog with string in by_exit (e.g. '16') returns invalid without TypeError."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        catalog = build_derived_catalog(
            str(raw_dir / "results.json"),
            str(raw_dir / "manifest.json"),
            str(raw_dir / "terminal.json"),
            str(cells_dir),
            expected,
        )
        cat_pin = {
            "head": head_40,
            "manifest_sha256": catalog["manifest_sha256"],
            "results_sha256": catalog["results_sha256"],
            "terminal_sha256": catalog["terminal_sha256"],
            "config_sha256": catalog["config_sha256"],
            "input_manifest_sha256": catalog["input_manifest_sha256"],
            "source_sha256": catalog["source_sha256"],
            "projection_sha256": catalog["projection_sha256"],
        }
        control_res = read_catalog(catalog, cat_pin)
        self.assertEqual(control_res["state"], "complete")
        self.assertEqual(control_res["count"], 48)

        bad_catalog = dict(catalog, by_exit={"X1": "16", "X2": 16, "X3": 16})
        from owner_metro_results import catalog_projection_sha
        recomputed_sha = catalog_projection_sha(bad_catalog)
        bad_catalog["projection_sha256"] = recomputed_sha
        bad_pin = dict(cat_pin, projection_sha256=recomputed_sha)
        self.assertEqual(read_catalog(bad_catalog, bad_pin)["state"], "invalid")
        self.assertIsNone(read_catalog(bad_catalog, bad_pin)["count"])

    def test_catalog_tamper(self):
        """read_catalog with tampered metrics value returns invalid."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        catalog = build_derived_catalog(
            str(raw_dir / "results.json"),
            str(raw_dir / "manifest.json"),
            str(raw_dir / "terminal.json"),
            str(cells_dir),
            expected,
        )
        cat_expected = {
            "head": head_40,
            "manifest_sha256": catalog["manifest_sha256"],
            "results_sha256": catalog["results_sha256"],
            "terminal_sha256": catalog["terminal_sha256"],
            "config_sha256": catalog["config_sha256"],
            "input_manifest_sha256": catalog["input_manifest_sha256"],
            "source_sha256": catalog["source_sha256"],
            "projection_sha256": catalog["projection_sha256"],
        }
        tampered_metrics = [dict(m) for m in catalog["metrics"]]
        tampered_metrics[0]["net_bp"] = "999999"
        bad_catalog = dict(catalog, metrics=tampered_metrics)
        self.assertEqual(read_catalog(bad_catalog, cat_expected)["state"], "invalid")

    def test_count_cap(self):
        """Pending limit caps pending_decisions to visible pending_list length."""
        items = [{"id": f"C-{i}", "state": "OPEN", "text": "new", "blocked_by": "user:question"} for i in range(100, 169)]
        self.write_ledger(items)
        self.write_bindings(sources={"directive": "owner_metro/directive.json"})
        (self.root / "owner_metro/directive.json").write_text(json.dumps({"updated_at": self.now_ms / 1000, "pending_decisions": 69}))
        k = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))["kpis"]
        self.assertEqual(k["pending_decisions"], len(k["pending_list"]))
        self.assertEqual(k["pending_decisions"], 64)
        self.assertEqual(k["pending_total"], 69)
        self.assertTrue(k["pending_truncated"])

    def test_missing_directive(self):
        """Valid ledger with missing directive sets canonical ledger count."""
        self.write_ledger([{"id": "C-901", "state": "OPEN", "text": "new", "blocked_by": "user:question"}])
        self.write_bindings(sources={"directive": "owner_metro/directive.json"})
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m["sources"]["directive"]["state"], "missing")
        self.assertEqual(m["kpis"]["pending_decisions"], 1)
        self.assertEqual(m["kpis"]["pending_source"], "ledger")

    def test_bad_directive(self):
        """Valid ledger with malformed directive sets canonical ledger count."""
        self.write_ledger([{"id": "C-901", "state": "OPEN", "text": "new", "blocked_by": "user:question"}])
        self.write_bindings(sources={"directive": "owner_metro/directive.json"})
        (self.root / "owner_metro/directive.json").write_text("{")
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m["sources"]["directive"]["state"], "invalid")
        self.assertEqual(m["kpis"]["pending_decisions"], 1)
        self.assertEqual(m["kpis"]["pending_source"], "ledger")

    def test_bad_both_na(self):
        """Missing ledger and missing directive both results in pending NA."""
        self.write_bindings(sources={"directive": "owner_metro/directive.json"})
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertIsNone(m["kpis"]["pending_decisions"])
        self.assertIsNone(m["kpis"]["pending_source"])

    def test_canonical_owners(self):
        """Dispatched ledger items appear in canonical_owners."""
        self.write_ledger([
            {"id": "C-301", "state": "DISPATCHED", "handle": {"actor": "bot1", "product": "maker", "kind": "pid"}},
            {"id": "C-302", "state": "OPEN", "blocked_by": "user:q"},
        ])
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        can = m["canonical_owners"]
        self.assertEqual(len(can), 1)
        self.assertEqual(can[0]["item"], "C-301")
        self.assertEqual(can[0]["actor"], "bot1")

    def test_pin_required(self):
        """Stripping projection_sha256 from catalog and pin returns None count."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        c = dict(cat); c.pop("projection_sha256")
        p = dict(pin); p.pop("projection_sha256")
        self.assertIsNone(read_catalog(c, p)["count"])

    def test_comp_tamper(self):
        """Tampered completed_at in catalog returns None count."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        c = dict(cat); c["completed_at"] += 1000
        self.assertIsNone(read_catalog(c, pin)["count"])

    def test_class_tamper(self):
        """Tampered classification in catalog returns None count."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        c = dict(cat); c["classification"] = "PASS"
        self.assertIsNone(read_catalog(c, pin)["count"])

    def test_dup_cells(self):
        """Duplicate cell in catalog metrics returns None count."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        c = dict(cat); c["metrics"] = [c["metrics"][0]] * 48
        self.assertIsNone(read_catalog(c, pin)["count"])

    def test_hash_format(self):
        """Non-hex hash format in expected/catalog returns None count."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        c = dict(cat); c["manifest_sha256"] = "not-a-hash"
        p = dict(pin); p["manifest_sha256"] = "not-a-hash"
        self.assertIsNone(read_catalog(c, p)["count"])

    def test_missing_prov(self):
        """Missing mandatory provenance field from expected pin returns None count."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        for key in ("source_sha256", "config_sha256", "input_manifest_sha256", "terminal_sha256", "projection_sha256"):
            p = dict(pin); del p[key]
            with self.subTest(key=key):
                self.assertIsNone(read_catalog(cat, p)["count"])

    def test_missing_raw(self):
        """Missing mandatory raw cell file raises ValueError."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        (cells_dir / "00_strategy.json").unlink()
        with self.assertRaises(ValueError):
            build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)

    def test_missing_entry(self):
        """Missing manifest entry raises ValueError."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        p = raw_dir / "manifest.json"
        m = json.loads(p.read_text())
        del m["artifacts"]["cells/00_strategy.json"]
        p.write_text(json.dumps(m))
        with self.assertRaises(ValueError):
            build_derived_catalog(raw_dir / "results.json", p, raw_dir / "terminal.json", cells_dir, expected)

    def test_results_tamper(self):
        """results.json whitespace added preserves valid JSON but triggers digest mismatch."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        orig = (raw_dir / "results.json").read_bytes()
        (raw_dir / "results.json").write_bytes(orig + b" ")
        with self.assertRaises(ValueError):
            build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)

    def test_raw_bytes_tamper(self):
        """Raw cell artifact byte mismatch with manifest raises ValueError."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        target = cells_dir / "00_strategy.json"
        target.write_bytes(target.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)

    def test_snapshot_absent(self):
        """Snapshot with missing results catalog file yields NA and missing state."""
        self.write_bindings(sources={"results": "owner_metro/nonexistent.json"})
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertIsNone(m["kpis"]["maker_cells"])
        self.assertEqual(m["sources"]["results"]["state"], "missing")
        self.assertFalse(m["sources"]["results"]["completed"])
        self.assertIsNone(m["clocks"]["historical_completed_at"])

    def test_policy_bad_text(self):
        """Prohibition with altered ledger text stays pending with valid control."""
        c203 = {"id": "C-203", "text": "Original manifest text", "state": "OPEN", "blocked_by": "user:Q?", "created": 1792250000.0, "updated": 1792250000.0}
        self.write_ledger([c203])
        manifest = {"schema_version": 1, "items": [{"id": "C-203", "original_payload": c203, "original_payload_sha256": canonical_hash(c203)}]}
        man_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_sha = hashlib.sha256(man_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_raw)
        policy = {"schema_version": 1, "manifest_sha256": man_sha, "rules": [{"id": "C-203", "classification": "prohibition", "pin_sha256": canonical_hash(c203)}]}
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(policy))
        self.write_bindings(sources={"frozen_manifest": "owner_metro/frozen_manifest.json", "decision_policy": "owner_metro/decision_policy.json"})
        m0 = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m0["kpis"]["pending_decisions"], 0)
        self.assertIn("C-203", [h["id"] for h in m0["history"]])

        c203_altered = dict(c203, text="Altered text in ledger")
        self.write_ledger([c203_altered])
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m["kpis"]["pending_decisions"], 1)
        self.assertEqual(m["kpis"]["pending_list"][0]["id"], "C-203")

    def test_policy_bad_orig(self):
        """Manifest original_payload text altered stays pending with valid control."""
        c203 = {"id": "C-203", "text": "Original text", "state": "OPEN", "blocked_by": "user:Q?", "created": 1792250000.0, "updated": 1792250000.0}
        self.write_ledger([c203])
        manifest = {"schema_version": 1, "items": [{"id": "C-203", "original_payload": c203, "original_payload_sha256": canonical_hash(c203)}]}
        man_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_sha = hashlib.sha256(man_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_raw)
        policy = {"schema_version": 1, "manifest_sha256": man_sha, "rules": [{"id": "C-203", "classification": "prohibition", "pin_sha256": canonical_hash(c203)}]}
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(policy))
        self.write_bindings(sources={"frozen_manifest": "owner_metro/frozen_manifest.json", "decision_policy": "owner_metro/decision_policy.json"})
        m0 = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m0["kpis"]["pending_decisions"], 0)

        c203_altered_orig = dict(c203, text="Altered in manifest")
        manifest2 = {"schema_version": 1, "items": [{"id": "C-203", "original_payload": c203_altered_orig, "original_payload_sha256": canonical_hash(c203_altered_orig)}]}
        man2_raw = json.dumps(manifest2, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man2_sha = hashlib.sha256(man2_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man2_raw)
        policy2 = {"schema_version": 1, "manifest_sha256": man2_sha, "rules": [{"id": "C-203", "classification": "prohibition", "pin_sha256": canonical_hash(c203_altered_orig)}]}
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(policy2))
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m["kpis"]["pending_decisions"], 1)
        self.assertEqual(m["kpis"]["pending_list"][0]["id"], "C-203")

    def test_policy_bad_hash(self):
        """Manifest original_payload_sha256 mismatch stays pending with valid control."""
        c203 = {"id": "C-203", "text": "Original text", "state": "OPEN", "blocked_by": "user:Q?", "created": 1792250000.0, "updated": 1792250000.0}
        self.write_ledger([c203])
        manifest = {"schema_version": 1, "items": [{"id": "C-203", "original_payload": c203, "original_payload_sha256": canonical_hash(c203)}]}
        man_raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_sha = hashlib.sha256(man_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_raw)
        policy = {"schema_version": 1, "manifest_sha256": man_sha, "rules": [{"id": "C-203", "classification": "prohibition", "pin_sha256": canonical_hash(c203)}]}
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(policy))
        self.write_bindings(sources={"frozen_manifest": "owner_metro/frozen_manifest.json", "decision_policy": "owner_metro/decision_policy.json"})
        m0 = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m0["kpis"]["pending_decisions"], 0)
        self.assertIn("C-203", [h["id"] for h in m0["history"]])

        manifest_bad = {"schema_version": 1, "items": [{"id": "C-203", "original_payload": c203, "original_payload_sha256": "0" * 64}]}
        man_bad_raw = json.dumps(manifest_bad, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        man_bad_sha = hashlib.sha256(man_bad_raw).hexdigest()
        (self.root / "owner_metro/frozen_manifest.json").write_bytes(man_bad_raw)
        policy_bad = {"schema_version": 1, "manifest_sha256": man_bad_sha, "rules": [{"id": "C-203", "classification": "prohibition", "pin_sha256": canonical_hash(c203)}]}
        (self.root / "owner_metro/decision_policy.json").write_text(json.dumps(policy_bad))
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m["kpis"]["pending_decisions"], 1)
        self.assertEqual(m["kpis"]["pending_list"][0]["id"], "C-203")

    def test_clocks_separate(self):
        """Observed clock advances while historical and directive remain separate."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        cat["expected"] = pin
        (self.root / "owner_metro/results.json").write_text(json.dumps(cat))
        (self.root / "owner_metro/directive.json").write_text(json.dumps({"updated_at": 1792625000, "pending_decisions": 0}))
        self.write_bindings(sources={"results": "owner_metro/results.json", "directive": "owner_metro/directive.json"})
        m1 = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        c1 = m1["clocks"]
        self.assertEqual(c1["observed_ms"], self.now_ms)
        self.assertEqual(c1["historical_completed_at"], 1792672132.93)
        self.assertEqual(c1["directive_updated_at"], 1792625000)
        self.assertIsNone(c1["progress_at"])
        m2 = read_metro(str(self.root), now_ms=self.now_ms + 5000, proc_root=str(self.proc))
        c2 = m2["clocks"]
        self.assertEqual(c2["observed_ms"], self.now_ms + 5000)
        self.assertEqual(c2["historical_completed_at"], 1792672132.93)
        self.assertEqual(c2["directive_updated_at"], 1792625000)
        self.assertIsNone(c2["progress_at"])
        cat_bad = dict(cat, classification="PASS")
        (self.root / "owner_metro/results.json").write_text(json.dumps(cat_bad))
        m3 = read_metro(str(self.root), now_ms=self.now_ms + 10000, proc_root=str(self.proc))
        self.assertIsNone(m3["clocks"]["historical_completed_at"])

    def test_owners_dispatched(self):
        """canonical_owners includes DISPATCHED with ref and excludes OPEN/DROPPED."""
        ledger_items = [
            {"id": "C-401", "state": "DISPATCHED", "handle": {"kind": "pid", "ref": "proc_1", "actor": "bot1", "product": "maker"}},
            {"id": "C-402", "state": "OPEN", "blocked_by": "user:q"},
            {"id": "C-403", "state": "DROPPED", "handle": {"kind": "cron", "ref": "cron_old"}},
        ]
        self.write_ledger(ledger_items)
        (self.root / "owner_metro/directive.json").write_text(json.dumps({
            "updated_at": 1792625000,
            "active_owners": [
                {"item": "C-401", "actor": "bot1", "product": "maker"},
                {"item": "C-old", "actor": "old_actor", "product": "maker"},
                {"item": "C-403", "actor": "bot3", "product": "maker"},
            ]
        }))
        self.write_bindings(sources={"directive": "owner_metro/directive.json"})
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        can = m["canonical_owners"]
        self.assertEqual(len(can), 1)
        self.assertEqual(can[0]["item"], "C-401")
        self.assertEqual(can[0]["ref"], "proc_1")
        self.assertEqual(can[0]["kind"], "pid")
        self.assertNotIn("C-402", [c["item"] for c in can])
        self.assertNotIn("C-403", [c["item"] for c in can])
        dir_owners = m["directive"]["owners"]
        self.assertEqual(len(dir_owners), 1)
        self.assertEqual(dir_owners[0]["item"], "C-401")

    def test_oversize_raw(self):
        """Raw cell file > 1MiB (up to 10MiB) streams successfully."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        big_content = json.dumps({"cell": sorted(IDS)[0], "side": "strategy", "pad": "x" * 1200000}).encode("utf-8")
        target = cells_dir / "00_strategy.json"
        target.write_bytes(big_content)
        manifest_p = raw_dir / "manifest.json"
        manifest_data = json.loads(manifest_p.read_text())
        manifest_data["artifacts"]["cells/00_strategy.json"] = hashlib.sha256(big_content).hexdigest()
        manifest_p.write_text(json.dumps(manifest_data))
        cat = build_derived_catalog(raw_dir / "results.json", manifest_p, raw_dir / "terminal.json", cells_dir, expected)
        self.assertEqual(cat["count"], 48)

    def test_oversize_exceed(self):
        """Raw cell file exceeding 10MiB streaming limit raises ValueError."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        target = cells_dir / "00_strategy.json"
        target.write_bytes(b"x" * (10485760 + 1024))
        with self.assertRaises(ValueError):
            build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)

    def test_snapshot_catalog(self):
        """read_metro snapshot with derived catalog and pin reflects available, tampers fail to invalid."""
        head_40 = "1" * 40
        raw_dir, cells_dir, expected = self._setup_raw_artifacts(head_40)
        cat = build_derived_catalog(raw_dir / "results.json", raw_dir / "manifest.json", raw_dir / "terminal.json", cells_dir, expected)
        pin = {k: cat[k] for k in ("head", "manifest_sha256", "results_sha256", "terminal_sha256", "source_sha256", "config_sha256", "input_manifest_sha256", "projection_sha256")}
        cat["expected"] = pin
        (self.root / "owner_metro/results.json").write_text(json.dumps(cat))
        self.write_bindings(sources={"results": "owner_metro/results.json"})
        m = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertEqual(m["kpis"]["maker_cells"], 48)
        self.assertEqual(m["sources"]["results"]["state"], "available")
        self.assertTrue(m["sources"]["results"]["completed"])
        cat_bad = dict(cat); cat_bad["classification"] = "PASS"
        (self.root / "owner_metro/results.json").write_text(json.dumps(cat_bad))
        m2 = read_metro(str(self.root), now_ms=self.now_ms, proc_root=str(self.proc))
        self.assertIsNone(m2["kpis"]["maker_cells"])
        self.assertEqual(m2["sources"]["results"]["state"], "invalid")
        self.assertFalse(m2["sources"]["results"]["completed"])


if __name__ == "__main__":
    unittest.main()
