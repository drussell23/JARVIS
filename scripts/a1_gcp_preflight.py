#!/usr/bin/env python3
"""a1_gcp_preflight -- $0 GCP credential preflight before any awaken/REST call.

Asserts that GCP credentials, project, and zone are properly configured.
On missing/invalid config: FAILS GRACEFULLY with the EXACT export commands
the operator needs -- no stack trace, no GCP API call, no node created.

Reuses seams from gcp_compute_rest.GCPComputeRest (auth resolution +
verify_compute_scopes) and mirrors the shape of smoke_sa_token_mint.py.

Usage (standalone):
    python3 scripts/a1_gcp_preflight.py

Importable:
    from scripts.a1_gcp_preflight import preflight_gcp_ready
    ok, problems = await preflight_gcp_ready()
"""
from __future__ import annotations

import asyncio
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

_GREEN = "\033[32m"
_RED = "\033[31m"
_RESET = "\033[0m"


async def preflight_gcp_ready(
    *,
    require_zone: bool = True,
    rest=None,
) -> tuple[bool, list[str]]:
    """($0) Assert GCP is ready to awaken a node. Returns (ok, problems).

    Each problem string includes the EXACT ``export ...`` command to fix it.
    Performs NO instances.insert -- only credential/project/zone resolution
    and a $0 token-mint structural check. Never raises (fail-soft -> (False, [...])).

    Parameters
    ----------
    require_zone:
        When True (default), flag a missing/empty GCP_ZONE as a problem.
    rest:
        Optional pre-built GCPComputeRest-like object (for tests). When None
        the real GCPComputeRest is constructed from the environment.
    """
    problems: list[str] = []

    # --- 1. resolve the rest client --------------------------------------
    if rest is None:
        try:
            from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                GCPComputeRest,
            )
            rest = GCPComputeRest()
        except Exception as exc:  # noqa: BLE001
            return (
                False,
                [f"Cannot init GCPComputeRest: {exc} -- ensure google-auth is installed"],
            )

    # --- 2. auth check ---------------------------------------------------
    try:
        auth_mode = rest._auth_mode  # "sa" | "adc" | "metadata"
        if auth_mode == "metadata":
            # Off-GCE with no SA JSON and no ADC -- metadata auth only works
            # when running inside GCE and requires no local credential.
            # For an off-GCE awaken orchestrator this means no credential.
            auth_ok = False
        else:
            scope_result = await rest.verify_compute_scopes()
            # verify_compute_scopes returns (bool, str); accept (bool,) or plain bool
            if isinstance(scope_result, tuple):
                auth_ok = bool(scope_result[0])
            else:
                auth_ok = bool(scope_result)
    except Exception:  # noqa: BLE001
        auth_ok = False

    if not auth_ok:
        problems.append(
            "No valid GCP auth. Fix ONE:\n"
            "    export GOOGLE_APPLICATION_CREDENTIALS=/abs/path/to/sa.json\n"
            "  OR run: gcloud auth application-default login"
        )

    # --- 3. project check ------------------------------------------------
    try:
        proj = await rest.project()
    except Exception:  # noqa: BLE001
        proj = None
    if not proj:
        problems.append(
            "GCP project unresolved -- "
            "export GCP_PROJECT_ID=<your-project>  (e.g. jarvis-473803)"
        )

    # --- 4. zone check ---------------------------------------------------
    if require_zone:
        try:
            z = await rest.zone()
        except Exception:  # noqa: BLE001
            z = None
        if not z:
            problems.append(
                "GCP zone unset -- "
                "export GCP_ZONE=<zone>  (a g2/nvidia-l4 GPU zone, e.g. us-central1-a)"
            )

    ok = len(problems) == 0
    return (ok, problems)


def main() -> None:
    """CLI entry point. Exits 0 on success, 2 on any problem. Never raises."""
    try:
        ok, problems = asyncio.run(preflight_gcp_ready())
    except Exception as exc:  # noqa: BLE001
        print(f"[a1-gcp-preflight] INTERNAL ERROR: {exc}")
        sys.exit(2)

    if ok:
        # Resolve display values for the success banner (best-effort, no error).
        try:
            import asyncio as _a  # noqa: PLC0415

            async def _resolve():
                from backend.core.ouroboros.governance.gcp_compute_rest import (  # noqa: PLC0415
                    GCPComputeRest,
                )
                r = GCPComputeRest()
                p = await r.project()
                z = await r.zone()
                return r._auth_mode, p or "?", z or "?"

            mode, proj, zone = _a.run(_resolve())
        except Exception:  # noqa: BLE001
            mode, proj, zone = "?", "?", "?"

        print(
            f"{_GREEN}[a1-gcp-preflight] OK -- "
            f"project={proj} zone={zone} auth={mode}{_RESET}"
        )
        sys.exit(0)
    else:
        lines = [
            f"{_RED}[a1-gcp-preflight] NOT READY -- "
            "the live 32B awaken cannot proceed.",
            "Run these in THIS terminal, then re-run:",
            "",
        ]
        for problem in problems:
            for line in problem.splitlines():
                lines.append(f"    {line}")
            lines.append("")
        lines.append(f"(All checks are $0 -- no node was created.){_RESET}")
        print("\n".join(lines))
        sys.exit(2)


if __name__ == "__main__":
    main()
