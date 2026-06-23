#!/usr/bin/env python3
"""Sovereign Cloud Ignition — provision the Ouroboros soak on a GCP Spot node.

Thin launcher that REUSES the existing hybrid-cloud provisioner
(``backend/core/gcp_vm_manager.py::GCPVMManager.start_soak_vm``) — it does NOT
reimplement gcloud. It configures a ``VMManagerConfig`` for an
``e2-custom-8-16384`` Spot instance, points it at the Ouroboros startup script
(``deploy/gcp_ouroboros_startup.sh``), and passes the funded DW key + model pin
as instance metadata for the startup script to consume.

Usage (from the repo root, with ADC available — ``gcloud auth application-default
login`` — and a funded ``DOUBLEWORD_API_KEY`` in ``.env``):

    python3 scripts/ignite_sovereign_cloud_node.py \
        --project jarvis-473803 --zone us-central1-a \
        --machine e2-custom-8-16384 --pin openai/gpt-oss-120b

The DW key is read from ``.env`` (never printed — only its sha256[:8] + length).
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_STARTUP_SCRIPT = _REPO_ROOT / "deploy" / "gcp_ouroboros_startup.sh"


def _read_env_var(name: str) -> str:
    """Read *name* from the process env or the repo ``.env`` (never printed)."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    env_path = _REPO_ROOT / ".env"
    if env_path.is_file():
        m = re.search(
            rf'^\s*(?:export\s+)?{re.escape(name)}\s*=\s*["\']?([^"\'\n]+)',
            env_path.read_text(), re.M,
        )
        if m:
            return m.group(1).strip()
    return ""


def _read_dw_key() -> str:
    """Read DOUBLEWORD_API_KEY from the process env or the repo ``.env``."""
    return _read_env_var("DOUBLEWORD_API_KEY")


def _mask(key: str) -> str:
    if not key:
        return "<absent>"
    return f"sha256:{hashlib.sha256(key.encode()).hexdigest()[:8]} len={len(key)}"


async def _ignite(args: argparse.Namespace) -> int:
    dw_key = _read_dw_key()
    if not dw_key:
        print("❌ DOUBLEWORD_API_KEY not found in env or .env — funded key required.")
        return 2
    if not _STARTUP_SCRIPT.is_file():
        print(f"❌ startup script missing: {_STARTUP_SCRIPT}")
        return 2

    # Reuse the existing provisioner — configure, don't reimplement.
    from backend.core.gcp_vm_manager import GCPVMManager, VMManagerConfig

    cfg = VMManagerConfig()
    cfg.enabled = True
    cfg.project_id = args.project
    cfg.zone = args.zone
    cfg.region = args.zone.rsplit("-", 1)[0]
    cfg.machine_type = args.machine
    # ON-DEMAND by default: Spot instances have no guaranteed lifetime and were
    # preempted ~33min into a convergence soak (before the FSM could play out the
    # batch-wait + lane escalation). A deep, long-running convergence FSM needs an
    # UNINTERRUPTED window. ``--spot`` opts back into the cheaper preemptible node.
    cfg.use_spot = bool(getattr(args, "spot", False))
    cfg.use_golden_image = False
    cfg.use_container = False
    cfg.boot_disk_size_gb = args.disk_gb
    cfg.max_vm_lifetime_hours = args.max_hours
    cfg.startup_script_path = str(_STARTUP_SCRIPT)

    print("🐍 Sovereign Cloud Ignition")
    print(f"   project={cfg.project_id} zone={cfg.zone} machine={cfg.machine_type} "
          f"({'SPOT' if cfg.use_spot else 'ON-DEMAND'})")
    print(f"   disk={cfg.boot_disk_size_gb}GB max_lifetime={cfg.max_vm_lifetime_hours}h")
    print(f"   pin={args.pin}   DW key={_mask(dw_key)}")
    print(f"   startup_script={_STARTUP_SCRIPT.name}")
    if args.dry_run:
        print("   --dry-run: config validated, NOT creating the VM.")
        return 0

    md = {
        "jarvis-dw-api-key": dw_key,
        "jarvis-dw-primary-override": args.pin,
    }
    # A1: ship the roadmap HMAC secret so the node can VERIFY the signed
    # roadmap.yaml it hydrates from the GCS Vault (reader REQUIRE_SIGNATURE
    # defaults TRUE). Without it the strategic GOAL never emits — no file-00.
    hmac_secret = _read_env_var("JARVIS_ROADMAP_READER_HMAC_SECRET")
    if hmac_secret:
        md["jarvis-roadmap-hmac-secret"] = hmac_secret
        print(f"   roadmap HMAC via metadata: {_mask(hmac_secret)}")
    else:
        print("   WARNING: no JARVIS_ROADMAP_READER_HMAC_SECRET in env/.env — "
              "signed roadmap will fail verification (no file-00 will emit)")
    if args.crucible:
        # Arm the autonomic graduation cadence (crucible overlay on the node).
        md["jarvis-crucible-mode"] = "true"
        print("   🧬 CRUCIBLE MODE — autonomic graduation cadence armed on boot")
    # JIT GitHub auth: prefer Secret Manager `github-token` (the startup script
    # reads it natively). Optionally pass a token through here as metadata
    # fallback — NEVER printed, only length. A token is REQUIRED for the node to
    # push [SOVEREIGN GRADUATION] PRs; without one the cadence soaks but cannot
    # open PRs (it logs a clear warning).
    gh_tok = os.environ.get("GH_TOKEN", "").strip()
    if gh_tok:
        md["jarvis-gh-token"] = gh_tok
        print(f"   gh_token via metadata: {_mask(gh_tok)}")
    else:
        print("   gh_token: relying on Secret Manager `github-token` "
              "(set GH_TOKEN env to pass via metadata instead)")

    mgr = GCPVMManager(cfg)
    ok, result = await mgr.start_soak_vm(extra_metadata=md)
    if not ok:
        print(f"❌ ignition failed: {result}")
        return 1
    print(f"✅ Spot soak node creating: {result}")
    print("   monitor boot:   gcloud compute instances get-serial-port-output "
          f"{result} --zone {cfg.zone} --project {cfg.project_id}")
    print("   monitor loop:   gcloud compute ssh "
          f"{result} --zone {cfg.zone} --project {cfg.project_id} "
          "--command 'sudo docker logs -f jarvis-sovereign-prod'")
    # Deep lifecycle integration: auto-spawn the Sovereign Telemetry Sentinel as
    # a detached daemon attached to THIS instance -- FSM-aware parsing + the
    # Autopsy-then-kill Good-Citizen protocol, with no operator action.
    if not getattr(args, "no_sentinel", False):
        _spawn_sentinel_daemon(str(result), cfg.zone, cfg.project_id)
    else:
        print("   sentinel: skipped (--no-sentinel)")
    return 0


def _spawn_sentinel_daemon(node: str, zone: str, project: str) -> None:
    """Spawn ``sovereign_sentinel.py`` detached, watching ``node``. Fail-soft:
    a spawn failure never fails the ignition (the node is already up)."""
    if os.environ.get("JARVIS_IGNITE_SPAWN_SENTINEL", "true").strip().lower() in (
        "0", "false", "no", "off",
    ):
        print("   sentinel: disabled (JARVIS_IGNITE_SPAWN_SENTINEL=false)")
        return
    try:
        import subprocess
        sentinel = _REPO_ROOT / "scripts" / "sovereign_sentinel.py"
        if not sentinel.exists():
            print("   sentinel: script not found -- skipping daemon spawn")
            return
        logdir = _REPO_ROOT / "autopsy_reports"
        logdir.mkdir(parents=True, exist_ok=True)
        logf = open(logdir / f"sentinel_{node}.log", "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, str(sentinel),
             "--node", node, "--zone", zone, "--project", project],
            stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
        )
        print(f"   🛰️  sentinel daemon spawned (autopsy+auto-kill armed) "
              f"-> autopsy_reports/sentinel_{node}.log")
    except Exception as exc:  # noqa: BLE001 -- never fail ignition on sentinel spawn
        print(f"   sentinel: spawn failed (non-fatal): {exc!r}")


def main() -> int:
    p = argparse.ArgumentParser(description="Provision the Ouroboros soak on a GCP Spot node.")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", "jarvis-473803"))
    p.add_argument("--zone", default=os.environ.get("GCP_ZONE", "us-central1-a"))
    p.add_argument("--machine", default=os.environ.get("GCP_VM_MACHINE_TYPE", "e2-custom-8-16384"))
    p.add_argument("--pin", default=os.environ.get("JARVIS_DW_PRIMARY_OVERRIDE", "openai/gpt-oss-120b"))
    p.add_argument("--disk-gb", type=int, default=int(os.environ.get("GCP_BOOT_DISK_GB", "50")))
    p.add_argument("--max-hours", type=float, default=float(os.environ.get("GCP_MAX_VM_HOURS", "6")))
    p.add_argument("--dry-run", action="store_true", help="validate config, do not create the VM")
    p.add_argument("--crucible", action="store_true",
                   help="arm the autonomic Sovereign Cognitive Graduation Crucible cadence")
    p.add_argument("--no-sentinel", action="store_true",
                   help="do NOT auto-spawn the Sovereign Telemetry Sentinel daemon")
    p.add_argument("--spot", action="store_true",
                   help="use a preemptible SPOT node (cheaper, no guaranteed lifetime); "
                        "default is ON-DEMAND for uninterrupted convergence soaks")
    args = p.parse_args()
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    return asyncio.run(_ignite(args))


if __name__ == "__main__":
    raise SystemExit(main())
