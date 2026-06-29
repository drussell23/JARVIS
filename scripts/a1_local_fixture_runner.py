#!/usr/bin/env python3
"""a1_local_fixture_runner -- faithful-env LOCAL Fast-Forward fixture run.

The Universal Parity Bridge: boots the SAME environment the cloud node gets, by
importing the harness's ``compose_env()`` as the single source of truth, under
the deterministic fixture, on macOS -- $0, full logs. Structurally eliminates
local/cloud env drift: any cloud flag change is inherited the instant it lands,
because both paths invoke the identical ``compose_env()``.

OS-agnostic by construction: ``compose_env()`` layers FLAGS on the inherited
(macOS) ``os.environ``, and the OS-agnostic interceptor maps any cloud repo
prefix to the local root under ``JARVIS_LOCAL_MODE`` (logical parity, not a
blind string copy).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="a1_local_fixture_runner")
    ap.add_argument("--fixture-target", default="backend/core/time_utils.py")
    ap.add_argument("--fixture-seed", type=int, default=7)
    ap.add_argument("--max-wall-seconds", type=int, default=300)
    ap.add_argument("--cost-cap", type=float, default=0.05)
    args = ap.parse_args(argv)

    # Stamp fixture + local-mode env into os.environ FIRST so compose_env()
    # (which reads os.environ as its base) inherits them faithfully.
    os.environ["JARVIS_LOCAL_MODE"] = "true"
    os.environ["JARVIS_A1_FIXTURE_MODE"] = "1"
    os.environ["JARVIS_A1_FIXTURE_TARGET"] = args.fixture_target
    os.environ["JARVIS_A1_FIXTURE_SEED"] = str(args.fixture_seed)

    # Universal Parity Bridge — the EXACT same composer the cloud node uses.
    from a1_live_fire_chaos_harness import compose_env
    from a1_deterministic_fixture import (
        remap_cloud_paths_for_local,
        validate_fixture_config,
    )

    env = compose_env()
    env = remap_cloud_paths_for_local(
        env, local_root=str(_REPO_ROOT), enabled=True
    )
    validate_fixture_config(env)  # fail-fast contract (GCS exempt: local)

    soak = str(_SCRIPTS / "ouroboros_battle_test.py")
    cmd = [
        sys.executable, soak, "--production-soak", "--headless",
        "--max-wall-seconds", str(args.max_wall_seconds),
        "--cost-cap", str(args.cost_cap),
    ]
    print(
        "[a1-local] faithful-env fixture soak: %d env vars, target=%s seed=%s"
        % (len(env), args.fixture_target, args.fixture_seed),
        flush=True,
    )
    return subprocess.call(cmd, cwd=str(_REPO_ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
