"""\nHeadless CLI — One-shot Ouroboros governance operations via command line.\n\nGap 4: Run Ouroboros without the full supervisor boot.\n  python3 -m backend.core.ouroboros.governance.headless_cli \\
    --goal "fix test failures in backend/core/" \\
    --repo jarvis --json

Returns structured JSON result for CI/CD integration.

Boundary Principle:
  Deterministic: CLI argument parsing, JSON output formatting.
  Agentic: The governance operation itself (CLASSIFY -> COMPLETE).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ouroboros",
        description="Run a one-shot Ouroboros governance operation",
    )
    parser.add_argument(
        "--goal", required=True,
        help="What to fix/improve (e.g., 'fix test failures in backend/core/')",
    )
    parser.add_argument(
        "--repo", default="jarvis",
        help="Target repository (jarvis, jarvis-prime, reactor)",
    )
    parser.add_argument(
        "--target-files", nargs="*", default=[],
        help="Specific files to target (space-separated)",
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output structured JSON result",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify and route only — don't generate or apply",
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0,
        help="Maximum seconds for the operation (default: 300)",
    )
    parser.add_argument(
        "--project-root", default=".",
        help="Project root directory",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose logging",
    )
    return parser.parse_args()


async def run_headless(args: argparse.Namespace) -> Dict[str, Any]:
    """Execute a one-shot governance operation headlessly.

    Constructs a minimal governance stack, creates an IntentEnvelope,
    and runs it through the orchestrator pipeline. Returns structured
    result without requiring the full supervisor boot.
    """
    t0 = time.monotonic()
    project_root = Path(args.project_root).resolve()

    result: Dict[str, Any] = {
        "goal": args.goal,
        "repo": args.repo,
        "target_files": args.target_files,
        "status": "pending",
        "dry_run": args.dry_run,
    }

    try:
        # Construct minimal stack
        from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

        # Create the intent envelope
        target_files = tuple(args.target_files) if args.target_files else ("backend/",)
        envelope = make_envelope(
            source="runtime_health",
            description=args.goal,
            target_files=target_files,
            repo=args.repo,
            confidence=1.0,
            urgency="high",
            evidence={
                "category": "headless_cli",
                "invoked_by": "cli",
                "dry_run": args.dry_run,
            },
            requires_human_ack=False,
        )

        result["envelope_id"] = envelope.idempotency_key
        result["status"] = "envelope_created"

        if args.dry_run:
            # Classify only — extract domain key and entropy
            try:
                from backend.core.ouroboros.governance.entropy_calculator import (
                    extract_domain_key,
                )
                domain = extract_domain_key(target_files, args.goal)
                result["domain_key"] = domain
            except Exception:
                pass

            result["status"] = "dry_run_complete"
            result["duration_s"] = round(time.monotonic() - t0, 2)
            return result

        # Try to route through GLS if available
        try:
            from backend.core.ouroboros.governance.governed_loop_service import (
                GovernedLoopService,
            )
            # Check if GLS singleton exists
            gls = GovernedLoopService.get_instance()
            if gls is not None and gls._router is not None:
                route_result = await asyncio.wait_for(
                    gls._router.ingest(envelope),
                    timeout=args.timeout,
                )
                result["route_result"] = route_result
                result["status"] = "submitted"
            else:
                result["status"] = "gls_not_running"
                result["message"] = (
                    "GovernedLoopService is not running. "
                    "Start the supervisor first: python3 unified_supervisor.py"
                )
        except ImportError:
            result["status"] = "import_error"
        except asyncio.TimeoutError:
            result["status"] = "timeout"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)

    except Exception as exc:
        result["status"] = "fatal"
        result["error"] = str(exc)

    result["duration_s"] = round(time.monotonic() - t0, 2)
    return result


def main() -> None:
    args = parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING)

    result = asyncio.run(run_headless(args))

    if args.json_output:
        print(json.dumps(result, indent=2))
    else:
        status = result.get("status", "unknown")
        print(f"Ouroboros [{status}]: {result.get('goal', '')}")
        if result.get("error"):
            print(f"  Error: {result['error']}")
        if result.get("duration_s"):
            print(f"  Duration: {result['duration_s']}s")
        if result.get("domain_key"):
            print(f"  Domain: {result['domain_key']}")

    sys.exit(0 if result.get("status") not in ("error", "fatal", "timeout") else 1)


if __name__ == "__main__":
    main()
