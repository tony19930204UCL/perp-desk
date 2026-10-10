"""Contract and invariant tests for F1H paper configurations."""
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import unittest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

V4_CONFIG_SHA256 = "e27b4b4b05ec66a2a88b9d4dd4eb502dd0bc329d87003346da55707ee0a7405c"

F1H_SPECS = {
    "BTCUSDT": {
        "filename": "paper_config_f1h_btcusdt.json",
        "version_id": "F1H-BTCUSDT-PAPER",
        "stop_distance_fraction": "0.0070",
    },
    "SOLUSDT": {
        "filename": "paper_config_f1h_solusdt.json",
        "version_id": "F1H-SOLUSDT-PAPER",
        "stop_distance_fraction": "0.0115",
    },
    "XRPUSDT": {
        "filename": "paper_config_f1h_xrpusdt.json",
        "version_id": "F1H-XRPUSDT-PAPER",
        "stop_distance_fraction": "0.0140",
    },
}


def recursive_diff_paths(dict1, dict2, prefix=()):
    differences = []
    keys = sorted(set(dict1.keys()) | set(dict2.keys()))
    for key in keys:
        path = prefix + (key,)
        if key not in dict1 or key not in dict2:
            differences.append(path)
        elif isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
            differences.extend(recursive_diff_paths(dict1[key], dict2[key], path))
        elif dict1[key] != dict2[key]:
            differences.append(path)
    return differences


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.v4_raw = (LAB / "paper_config_v4.json").read_text(encoding="utf-8")
        self.v4 = json.loads(self.v4_raw)
        self.configs = {}
        self.configs_raw = {}
        for symbol, spec in F1H_SPECS.items():
            path = LAB / spec["filename"]
            raw = path.read_text(encoding="utf-8")
            self.configs_raw[symbol] = raw
            self.configs[symbol] = json.loads(raw)

    def test_configs_load_valid_json(self):
        self.assertEqual(len(self.configs), 3)
        for symbol, config in self.configs.items():
            self.assertIsInstance(config, dict)
            self.assertEqual(config.get("schema_version"), 1)
            self.assertEqual(config.get("mode"), "paper")
            self.assertIs(config.get("approved"), True)

    def test_recursive_diff_against_v4_lists_exactly_three_changed_paths(self):
        expected_paths = {
            ("version_id",),
            ("strategy", "symbol"),
            ("strategy", "stop_distance_fraction"),
        }
        for symbol, config in self.configs.items():
            diffs = recursive_diff_paths(config, self.v4)
            self.assertEqual(set(diffs), expected_paths,
                             f"diff for {symbol} does not match expected 3 paths")
            self.assertEqual(len(diffs), 3,
                             f"diff for {symbol} must contain exactly 3 paths")

    def test_version_ids_are_distinct(self):
        version_ids = [c["version_id"] for c in self.configs.values()]
        self.assertEqual(len(version_ids), len(set(version_ids)),
                         "F1H version_ids must be unique across configs")
        for symbol, spec in F1H_SPECS.items():
            self.assertEqual(self.configs[symbol]["version_id"], spec["version_id"])
            self.assertNotEqual(self.configs[symbol]["version_id"], self.v4["version_id"])

    def test_stop_distance_fractions_equal_specified_values(self):
        for symbol, spec in F1H_SPECS.items():
            fraction_str = self.configs[symbol]["strategy"]["stop_distance_fraction"]
            self.assertEqual(fraction_str, spec["stop_distance_fraction"])
            self.assertEqual(Decimal(fraction_str), Decimal(spec["stop_distance_fraction"]))

    def test_full_dicts_equal_after_removing_three_changed_paths(self):
        changed_paths = [
            ("version_id",),
            ("strategy", "symbol"),
            ("strategy", "stop_distance_fraction"),
        ]
        for symbol, config in self.configs.items():
            c_copy = copy.deepcopy(config)
            v4_copy = copy.deepcopy(self.v4)
            for path in changed_paths:
                target_c = c_copy
                target_v = v4_copy
                for segment in path[:-1]:
                    target_c = target_c[segment]
                    target_v = target_v[segment]
                del target_c[path[-1]]
                del target_v[path[-1]]
            self.assertEqual(c_copy, v4_copy,
                             f"config for {symbol} has differences beyond the 3 allowlisted paths")

    def test_strategy_and_risk_contract_invariants(self):
        for symbol, config in self.configs.items():
            # Interval
            self.assertEqual(config["strategy"]["interval_ms"], self.v4["strategy"]["interval_ms"])
            self.assertEqual(config["strategy"]["interval_ms"], 3600000)
            # Max holding time
            self.assertEqual(config["strategy"]["max_holding_ms"], self.v4["strategy"]["max_holding_ms"])
            self.assertEqual(config["strategy"]["max_holding_ms"], 43200000)
            # Window length
            self.assertEqual(config["research"]["window_ms"], self.v4["research"]["window_ms"])
            self.assertEqual(config["research"]["window_ms"], 1814400000)
            # Target reference & round trips
            self.assertEqual(config["strategy"]["take_profit_reference"],
                             self.v4["strategy"]["take_profit_reference"])
            self.assertEqual(config["research"]["target_complete_round_trips"],
                             self.v4["research"]["target_complete_round_trips"])
            # Sample counts
            self.assertEqual(config["strategy"]["prior_return_samples"],
                             self.v4["strategy"]["prior_return_samples"])
            self.assertEqual(config["indicator_bootstrap"]["bars"],
                             self.v4["indicator_bootstrap"]["bars"])
            # Sigma and volume multiple
            self.assertEqual(config["strategy"]["downside_sigma"],
                             self.v4["strategy"]["downside_sigma"])
            self.assertEqual(config["strategy"]["volume_multiple"],
                             self.v4["strategy"]["volume_multiple"])
            # Cost gate
            self.assertEqual(config["strategy"]["minimum_gross_reward_to_estimated_cost"],
                             self.v4["strategy"]["minimum_gross_reward_to_estimated_cost"])
            # Risk contract values
            self.assertEqual(config["initial_equity_usdt"], self.v4["initial_equity_usdt"])
            self.assertEqual(config["max_loss_per_trade_usdt"], self.v4["max_loss_per_trade_usdt"])
            self.assertEqual(config["max_daily_loss_usdt"], self.v4["max_daily_loss_usdt"])
            self.assertEqual(config["max_effective_exposure_x"], self.v4["max_effective_exposure_x"])
            self.assertEqual(config["max_positions"], self.v4["max_positions"])
            self.assertEqual(config["total_loss_limit_usdt"], self.v4["total_loss_limit_usdt"])
            self.assertEqual(config["stop_required"], self.v4["stop_required"])
            self.assertEqual(config["paper_leverage"], self.v4["paper_leverage"])
            self.assertEqual(config["risk_version"], self.v4["risk_version"])

    def test_paper_config_v4_file_unchanged(self):
        v4_bytes = (LAB / "paper_config_v4.json").read_bytes()
        computed_hash = hashlib.sha256(v4_bytes).hexdigest()
        self.assertEqual(computed_hash, V4_CONFIG_SHA256,
                         "lab/paper_config_v4.json content must remain byte-identical")

    def test_key_order_and_formatting_preserved(self):
        v4_top_keys = list(self.v4.keys())
        v4_strat_keys = list(self.v4["strategy"].keys())
        for symbol, config in self.configs.items():
            self.assertEqual(list(config.keys()), v4_top_keys,
                             f"top-level key order mismatch in {symbol}")
            self.assertEqual(list(config["strategy"].keys()), v4_strat_keys,
                             f"strategy key order mismatch in {symbol}")
            # Traded symbol check: strategy.symbol is replaced, no other key holds traded symbol name
            self.assertEqual(config["strategy"]["symbol"], symbol)


if __name__ == "__main__":
    unittest.main()
