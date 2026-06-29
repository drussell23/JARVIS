#!/usr/bin/env python3
"""smoke_sa_token_mint -- prove the Dynamic IAM Credential Bridge against REAL GCP.

Mints a Compute OAuth token from GOOGLE_APPLICATION_CREDENTIALS via the native
google-auth SDK and resolves the project -- the exact path the Hybrid Execution
Mesh awaken uses. NO node is created; this is a ~1s, $0 reachability proof that
the SA JSON is valid and can authenticate to Compute before we spend on a soak.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=/path/sa.json python3 scripts/smoke_sa_token_mint.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio  # noqa: E402

from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: E402
    GCPComputeRest,
)


async def _run() -> int:
    client = GCPComputeRest()
    mode = client._auth_mode  # sa | adc | metadata
    print(f"[smoke] resolved auth mode = {mode} (off_gce={client._off_gce})")
    if mode == "metadata":
        print("[smoke] FAIL: no SA JSON and no ADC -- nothing to authenticate with.")
        return 2
    ok_scope, detail = await client.verify_compute_scopes()
    token = await client.access_token()
    project = await client.project()
    zone = await client.zone()
    if not token:
        print(f"[smoke] FAIL: token mint returned None ({detail})")
        return 1
    print(f"[smoke] OK: scope_verify={ok_scope} ({detail})")
    print(f"[smoke] token len={len(token)} prefix={token[:14]}...")
    print(f"[smoke] project={project!r} zone={zone!r}")
    print(f"[smoke] image family (env JPRIME_IMAGE_FAMILY)="
          f"{os.environ.get('JPRIME_IMAGE_FAMILY', '<default:jarvis-prime-coder>')!r}")
    if not project or not zone:
        print("[smoke] WARN: project/zone unresolved -- set GCP_PROJECT_ID + GCP_ZONE.")
        return 1
    print("[smoke] PASS -- the adaptive IAM bridge can authenticate to Compute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
