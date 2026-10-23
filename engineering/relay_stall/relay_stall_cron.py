#!/usr/bin/env python3
"""relay_stall_cron.py - Periodic cron entrypoint for relay stall observation."""

import argparse
import json
import os
import sys

DEFAULT_STATE_SUBDIR = os.path.join("relay-stall", "state")
DEFAULT_CONFIG_SUBDIR = os.path.join("relay-stall", "config.json")


def resolve_profile_home(script_path: str) -> str:
    """Resolve profile home directory relative to script location."""
    script_dir = os.path.dirname(os.path.abspath(script_path))
    # If placed in <profile_home>/scripts, parent is profile home
    return os.path.abspath(os.path.join(script_dir, ".."))


def load_cron_config(profile_home: str, custom_config: str | None = None) -> dict:
    """Load configuration from profile home or custom path."""
    cfg_file = custom_config or os.path.join(profile_home, DEFAULT_CONFIG_SUBDIR)
    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            sys.stderr.write(f"Warning: failed to parse config {cfg_file}: {e}\n")

    return {
        "state_dir": os.path.join(profile_home, DEFAULT_STATE_SUBDIR),
        "snapshot_path": os.path.join(profile_home, "source-snapshot.json"),
        "profile_home": profile_home,
    }


def run_cron_cycle(config: dict, now: float | None = None) -> int:
    """Execute one fresh producer scan round using configured adapter (no auto-consume)."""
    version_path = config.get("version_path")
    if version_path and os.path.isdir(version_path):
        if version_path not in sys.path:
            sys.path.insert(0, version_path)

    import relay_stall_adapter as rsa

    snapshot_path = config.get("snapshot_path")
    registry_path = config.get("registry_path")
    state_dir = config.get("state_dir")
    profile_home = config.get("profile_home")

    if not state_dir:
        sys.stderr.write("State dir not configured\n")
        return 1

    cmd = ["produce-scan", "--state-dir", state_dir]
    if registry_path:
        cmd.extend(["--registry", registry_path])
        if profile_home:
            cmd.extend(["--profile-home", profile_home])
    elif snapshot_path:
        cmd.extend(["--snapshot", snapshot_path])
    else:
        sys.stderr.write("Neither registry nor snapshot configured\n")
        return 1

    if now is not None:
        cmd.extend(["--now", str(now)])

    return rsa.main(cmd)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for periodic cron runner."""
    args = argv if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(description="Relay stall cron runner.")
    parser.add_argument("--config", default=None, help="Custom path to config.json")
    parser.add_argument("--profile-home", default=None, help="Explicit profile home")
    parser.add_argument("--now", type=float, default=None, help="Explicit epoch timestamp")

    parsed = parser.parse_args(args)
    p_home = parsed.profile_home or resolve_profile_home(__file__)
    cfg = load_cron_config(p_home, parsed.config)
    return run_cron_cycle(cfg, parsed.now)


if __name__ == "__main__":
    sys.exit(main())
