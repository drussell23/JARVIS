#!/usr/bin/env python3
"""launch_hybrid_failover_soak -- IGNITE the Hybrid Execution Mesh failover soak.

Local orchestrator (zero-cost execution) bridged to the REAL GCP J-Prime golden
image. Composes the layered-defense env on top of the A1 chaos manifest:

  * JARVIS_FAILOVER_LIFECYCLE_ENABLED=true   -- the FSM is live
  * JARVIS_JPRIME_PRIMACY=false              -- DW primary, J-Prime is the FALLBACK
  * JARVIS_DW_HEARTBEAT_ENABLED=true         -- Layer 1: the heartbeat is armed
  * JARVIS_DW_DEEP_PROBE_ENABLED=true        -- Layer 1: data-plane deep probe
  * JARVIS_FAILOVER_EARLY_PREWARM_ENABLED=true -- heartbeat may awaken pre-outage
  * JARVIS_QUARANTINE_UNMASK_EXHAUSTION_ENABLED=true -- Layer 2: unmasked outage
  * JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED=true    -- any-route reactive awaken
  * JARVIS_FAILOVER_HYBRID_MESH=true         -- resolve the node's EXTERNAL IP

HARD GUARD: refuses to launch without a usable GCP Service Account JSON +
project + zone -- a hybrid soak that cannot awaken is success-theatre, and this
script will not run one. Set:

  GOOGLE_APPLICATION_CREDENTIALS=/path/sa.json
  GCP_PROJECT_ID=<project>
  GCP_ZONE=<zone, e.g. us-central1-a>
  JPRIME_IMAGE_FAMILY=<golden image family, e.g. jarvis-prime-coder>   # optional, has default

Usage:
    python3 scripts/launch_hybrid_failover_soak.py [--max-wall-seconds 1200] [--cost-cap 2.00]
"""
from __future__ import annotations

import os
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
sys.path.insert(0, os.path.join(_REPO, "scripts"))


def _guard_gcp() -> None:
    from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
        _sa_credentials_path, _adc_available,
    )
    sa = _sa_credentials_path()
    has_auth = bool(sa and os.path.isfile(sa)) or _adc_available()
    proj = (os.environ.get("GCP_PROJECT_ID", "") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")).strip()
    zone = (os.environ.get("GCP_ZONE", "") or "").strip()
    missing = []
    if not has_auth:
        missing.append("a GCP credential (GOOGLE_APPLICATION_CREDENTIALS=SA.json OR gcloud ADC)")
    if not proj:
        missing.append("GCP_PROJECT_ID")
    if not zone:
        missing.append("GCP_ZONE")
    if missing:
        print("[hybrid-soak] ABORT -- cannot run a hybrid soak that cannot awaken.")
        print("[hybrid-soak] missing: " + ", ".join(missing))
        print("[hybrid-soak] (run scripts/smoke_sa_token_mint.py first to validate auth.)")
        raise SystemExit(2)
    print(f"[hybrid-soak] auth OK ({'SA' if (sa and os.path.isfile(sa)) else 'ADC'}) "
          f"project={proj} zone={zone}")


def main(argv: list) -> int:
    _guard_gcp()
    from a1_live_fire_chaos_harness import compose_env  # noqa: PLC0415

    env = compose_env()
    for k in ("JARVIS_A1_FIXTURE_MODE", "JARVIS_A1_FIXTURE_TARGET", "JARVIS_A1_FIXTURE_SEED"):
        env.pop(k, None)

    # Sovereign Failover Mesh -- layered defense, hybrid bridge.
    env["JARVIS_FAILOVER_LIFECYCLE_ENABLED"] = "true"
    env["JARVIS_JPRIME_PRIMACY"] = "false"
    env["JARVIS_DW_HEARTBEAT_ENABLED"] = "true"
    env["JARVIS_DW_DEEP_PROBE_ENABLED"] = "true"
    env["JARVIS_FAILOVER_EARLY_PREWARM_ENABLED"] = "true"
    env["JARVIS_QUARANTINE_UNMASK_EXHAUSTION_ENABLED"] = "true"
    env["JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED"] = "true"
    env["JARVIS_FAILOVER_HYBRID_MESH"] = "true"

    # Pass through the operator-exported GCP identity (compose_env may not carry it).
    for k in (
        "GOOGLE_APPLICATION_CREDENTIALS", "GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT",
        "GCP_ZONE", "JPRIME_IMAGE_FAMILY", "JARVIS_FAILOVER_MACHINE_TYPE",
    ):
        if os.environ.get(k):
            env[k] = os.environ[k]

    wall = "1200"
    cost = "2.00"
    if "--max-wall-seconds" in argv:
        wall = argv[argv.index("--max-wall-seconds") + 1]
    if "--cost-cap" in argv:
        cost = argv[argv.index("--cost-cap") + 1]

    print("[hybrid-soak] IGNITE: failover=ON hybrid=ON heartbeat=ON deep_probe=ON "
          f"jprime_primacy=false project={env.get('GCP_PROJECT_ID')} zone={env.get('GCP_ZONE')} "
          f"wall={wall}s cost_cap=${cost}", flush=True)
    cmd = [
        sys.executable, "scripts/ouroboros_battle_test.py",
        "--production-soak", "--headless",
        "--max-wall-seconds", wall, "--cost-cap", cost,
    ]
    # GUARANTEED EPHEMERAL TEARDOWN: an airtight finally that fires
    # delete_instance whether the soak exits cleanly, errors, or is Ctrl-C'd.
    # No orphan billed nodes -- ever. (The node-side Dead-Man's Switch is the
    # second backstop; this is the first.)
    try:
        return subprocess.call(cmd, env=env)
    finally:
        _guaranteed_node_teardown(env)


def _guaranteed_node_teardown(env: dict) -> None:
    """Fail-safe delete of the failover node -- runs on EVERY exit path."""
    try:
        import asyncio  # noqa: PLC0415
        from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
            get_compute_rest,
        )
        for k in ("GCP_PROJECT_ID", "GCP_ZONE", "JARVIS_FAILOVER_USE_ADC",
                  "GOOGLE_APPLICATION_CREDENTIALS"):
            if env.get(k):
                os.environ[k] = env[k]
        node = os.environ.get("JARVIS_FAILOVER_NODE_NAME", "jarvis-prime-failover")

        async def _del():
            ok, detail = await get_compute_rest().delete_instance(node)
            print(f"[hybrid-soak] GUARANTEED TEARDOWN: delete {node} -> ok={ok} {detail}",
                  flush=True)

        asyncio.run(_del())
    except Exception as exc:  # noqa: BLE001 -- teardown must never raise
        print("[hybrid-soak] teardown fail-soft (Dead-Man's Switch backstop): "
              f"{exc!r}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
