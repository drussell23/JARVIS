#!/usr/bin/env python3
"""CLI entry point for the Ouroboros Battle Test Runner.

Usage::

    python3 scripts/ouroboros_battle_test.py [options]
    python3 scripts/ouroboros_battle_test.py --help
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Ensure the project root is importable regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def main() -> None:
    # ------------------------------------------------------------------
    # Argument parsing
    # ------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        prog="ouroboros_battle_test",
        description="Run the Ouroboros Battle Test: a governed, self-improving AI session.",
    )
    parser.add_argument(
        "--cost-cap",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_COST_CAP", "0.50")),
        metavar="USD",
        help=(
            "Maximum API spend in USD before the session is stopped "
            "(env: OUROBOROS_BATTLE_COST_CAP, default: 0.50)"
        ),
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("OUROBOROS_BATTLE_IDLE_TIMEOUT", "600")),
        metavar="SECONDS",
        help=(
            "Seconds of inactivity before the session is stopped "
            "(env: OUROBOROS_BATTLE_IDLE_TIMEOUT, default: 600)"
        ),
    )
    parser.add_argument(
        "--branch-prefix",
        type=str,
        default=os.environ.get("OUROBOROS_BATTLE_BRANCH_PREFIX", "ouroboros/battle-test"),
        metavar="PREFIX",
        help=(
            "Git branch prefix for the accumulation branch "
            "(env: OUROBOROS_BATTLE_BRANCH_PREFIX, default: ouroboros/battle-test)"
        ),
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=os.environ.get("JARVIS_REPO_PATH", str(_PROJECT_ROOT)),
        metavar="PATH",
        help=(
            "Path to the repository root "
            "(env: JARVIS_REPO_PATH, default: project root)"
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Load .env files (project root and backend/) if keys not already set
    # ------------------------------------------------------------------
    _env_file = _PROJECT_ROOT / ".env"
    _backend_env = _PROJECT_ROOT / "backend" / ".env"
    for env_path in (_env_file, _backend_env):
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                os.environ.setdefault(key, value)

    # ------------------------------------------------------------------
    # Validate environment
    # ------------------------------------------------------------------
    if not os.environ.get("DOUBLEWORD_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Error: neither DOUBLEWORD_API_KEY nor ANTHROPIC_API_KEY is set. "
            "Export at least one before running the battle test.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ------------------------------------------------------------------
    # Governance mode
    # ------------------------------------------------------------------
    os.environ.setdefault("JARVIS_GOVERNANCE_MODE", "governed")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # ------------------------------------------------------------------
    # Build config + harness
    # ------------------------------------------------------------------
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness, HarnessConfig

    config = HarnessConfig(
        repo_path=Path(args.repo_path),
        cost_cap_usd=args.cost_cap,
        idle_timeout_s=args.idle_timeout,
        branch_prefix=args.branch_prefix,
    )

    harness = BattleTestHarness(config)

    # ------------------------------------------------------------------
    # Signal handlers (pre-loop registration for SIGINT / SIGTERM)
    # ------------------------------------------------------------------
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        harness.register_signal_handlers(loop)
    except Exception:
        pass  # Windows or unsupported platform — signals handled inside harness.run()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    try:
        loop.run_until_complete(harness.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
