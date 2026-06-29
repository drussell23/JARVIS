#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bake_gpu_golden_image.py -- one-command, hands-off bake of the 32B GPU golden image.

Submits the GPU Packer spec (docs/superpowers/specs/jprime_gpu_golden_image.pkr.hcl)
as a remote Google Cloud Build job over REST -- GCP provisions the builder, runs
``packer build`` (which spins a GPU node, bakes the NVIDIA driver + CUDA + Ollama +
pre-pulls the 32B weights, snapshots the ``jarvis-prime-coder-32b`` image, tears the
node down), then tears the builder down. NO local Packer. NO gcloud. NO GCS upload
(the spec rides inline as base64). Auth is the SAME dynamic ADC REST bridge the
failover provisioner uses (``gcp_compute_rest.access_token`` -> cloud-platform).

The quality tier (``failover_tier.py``) defaults JARVIS_FAILOVER_QUALITY_IMAGE to
``jarvis-prime-coder-32b`` -- so the image this bakes is exactly what the elastic
GPU lane provisions.

Usage:
    # DRY-RUN (default): print the exact Cloud Build resource, submit nothing.
    python3 scripts/bake_gpu_golden_image.py --project jarvis-473803 --dry-run

    # EXECUTE: submit + poll hands-off, streaming the build log to this terminal.
    python3 scripts/bake_gpu_golden_image.py --project jarvis-473803 --execute

Prerequisite (one-time, documented -- NOT a per-bake chore): the Cloud Build
service account needs roles/compute.admin + roles/iam.serviceAccountUser so Packer
can drive Compute. The script prints the exact grant on an auth failure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Repo-root import shim so the script runs from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.cloud_build_baker import (  # noqa: E402
    CloudBuildBaker,
    CrossRepoDependencyError,
    build_status_is_success,
    verify_cross_repo_spec,
)


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Hands-off bake of the 32B GPU golden image via Cloud Build REST.")
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT"),
                   help="GCP project (default $GCP_PROJECT; else resolved from ADC at run).")
    p.add_argument("--zone", default=os.environ.get("GCP_ZONE", "us-central1-b"),
                   help="Build/bake zone (must have the accelerator available).")
    p.add_argument("--model", default=os.environ.get("JARVIS_FAILOVER_QUALITY_MODEL", "qwen2.5-coder:32b"),
                   help="Ollama model to pre-pull into the image.")
    p.add_argument("--image-family", default=os.environ.get("JARVIS_FAILOVER_QUALITY_IMAGE", "jarvis-prime-coder-32b"),
                   help="Published image family (must equal the quality tier's image).")
    p.add_argument("--spec", default=None,
                   help="Path to the Packer .hcl spec (default: the SOVEREIGN jarvis-prime spec "
                        "$JARVIS_PRIME_PATH/infra/packer/jprime_gpu_golden_image.pkr.hcl).")
    p.add_argument("--timeout", type=int, default=int(os.environ.get("JARVIS_BAKE_TIMEOUT_S", "5400")),
                   help="Cloud Build timeout seconds (GPU bake + 32B pull is slow; default 90min).")
    p.add_argument("--poll-interval", type=int, default=15, help="Status poll cadence seconds.")
    p.add_argument("--ephemeral-iam", dest="ephemeral_iam", action="store_true", default=True,
                   help="Create a dedicated least-privilege temp SA, run the build as it, and GUARANTEE teardown (default).")
    p.add_argument("--default-sa", dest="ephemeral_iam", action="store_false",
                   help="Run as the default Cloud Build SA (NOT recommended; needs a broad pre-grant).")
    p.add_argument("--detach", action="store_true", default=False,
                   help="Daemonize: return the terminal immediately; the background process bakes + flares to the WAL.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True, help="Print the build resource, submit nothing (default).")
    g.add_argument("--execute", dest="dry_run", action="store_false", help="Actually submit + poll the build.")
    return p.parse_args(argv)


def _wal_path() -> str:
    return os.environ.get("JARVIS_BAKE_WAL", str(_REPO_ROOT / ".jarvis" / "bake" / "bake_wal.log"))


def _daemonize() -> None:
    """Double-fork into a detached daemon so the terminal returns immediately.
    The grandchild's stdio is redirected to the WAL (build logs land there)."""
    if os.fork() > 0:
        os._exit(0)          # original parent returns the terminal
    os.setsid()
    if os.fork() > 0:
        os._exit(0)          # session leader exits -> fully detached
    wal = Path(_wal_path())
    wal.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(wal), os.O_APPEND | os.O_CREAT | os.O_WRONLY)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)


def _baker(args) -> CloudBuildBaker:
    return CloudBuildBaker(
        spec_path=args.spec, project=args.project, image_family=args.image_family,
        model=args.model, zone=args.zone, timeout_s=args.timeout,
        poll_interval_s=args.poll_interval,
    )


def _print_plan(args, baker: CloudBuildBaker) -> None:
    project = args.project or "<resolved-from-ADC-at-run>"
    print("=" * 72)
    print("GPU GOLDEN-IMAGE BAKE -- DRY RUN (nothing submitted)")
    print("=" * 72)
    print(f"  project      : {project}")
    print(f"  image family : {args.image_family}")
    print(f"  model        : {args.model}")
    print(f"  zone         : {args.zone} (+ multi-zonal fallback on STOCKOUT)")
    print(f"  spec (sovereign jarvis-prime): {baker.resolved_spec_path()}")
    print(f"  timeout      : {args.timeout}s")
    print("-" * 72)
    print("Cloud Build resource that WOULD be POSTed:")
    # build_config needs a project string; use the placeholder for the preview.
    print(json.dumps(baker.build_config(args.project or "PROJECT"), indent=2))
    print("-" * 72)
    print("Re-run with --execute to submit hands-off (ADC auth, no gcloud, no upload).")


async def _execute(args, baker: CloudBuildBaker) -> int:
    if args.ephemeral_iam:
        print("Igniting Zero-Trust bake (dedicated temp SA -> build -> guaranteed teardown) ...", flush=True)
        ok = await baker.bake_with_ephemeral_iam()
    else:
        print("Submitting Cloud Build (default SA) ...", flush=True)
        build_id = await baker.submit()
        if not build_id:
            print("ABORT: submit failed (default SA likely lacks compute rights).", file=sys.stderr)
            return 2
        status = await baker.poll(build_id)
        ok = build_status_is_success(status)
    print(f"\n=== BAKE {'SUCCEEDED (image baked)' if ok else 'did NOT produce an image'} "
          f"-- WAL: {_wal_path()} ===")
    return 0 if ok else 1


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    baker = _baker(args)
    # Cross-Repo Verification Lock: fail FAST if the sovereign jarvis-prime spec
    # is missing/unreadable -- never bake a stale or absent copy.
    try:
        verify_cross_repo_spec(baker.resolved_spec_path())
    except CrossRepoDependencyError as exc:
        print(f"CrossRepoDependencyError: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        _print_plan(args, baker)
        return 0
    if args.detach:
        print(f"Dispatched detached bake -> watch the WAL: {_wal_path()}", flush=True)
        _daemonize()  # terminal returns NOW; the daemon bakes + flares below
    return asyncio.run(_execute(args, baker))


__test_hooks__ = {"daemonize": _daemonize}  # exposed for import without forking


if __name__ == "__main__":
    raise SystemExit(main())
