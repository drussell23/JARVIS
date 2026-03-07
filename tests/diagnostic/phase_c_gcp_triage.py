#!/usr/bin/env python3
"""Phase C Diagnostic — GCP J-Prime route.

Tests the full triage cycle using GCP J-Prime (golden-image) for extraction
instead of local Prime or cloud Claude.

Run from backend/:
    python3 ../tests/diagnostic/phase_c_gcp_triage.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Ensure backend is on sys.path
_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Load .env but skip ANTHROPIC_API_KEY (bad key in .env)
_dotenv = _BACKEND / ".env"
if _dotenv.exists():
    for line in _dotenv.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "ANTHROPIC_API_KEY":
                continue  # skip bad key
            os.environ.setdefault(k, v)

# Force GCP J-Prime route
GCP_HOST = "34.45.154.209"
GCP_PORT = 8000
os.environ["JARVIS_INVINCIBLE_NODE_IP"] = GCP_HOST
os.environ["JARVIS_WORKSPACE_ALLOW_STANDALONE"] = "true"

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
# Quiet noisy loggers
for name in ("httpcore", "httpx", "anthropic", "urllib3", "googleapiclient",
             "backend.core.trinity", "CrossRepoExperienceForwarder",
             "vision", "jarvis.shutdown", "GracefulShutdown", "backend.kernel",
             "backend.core.thread_manager", "backend.core.prime_client"):
    logging.getLogger(name).setLevel(logging.WARNING)

logger = logging.getLogger("phase_c_gcp")


async def main():
    from autonomy.email_triage.config import get_triage_config
    config = get_triage_config()
    logger.info("Config: enabled=%s, extraction=%s", config.enabled, config.extraction_enabled)

    # 1. Workspace agent
    from neural_mesh.agents.google_workspace_agent import get_google_workspace_agent
    workspace_agent = await get_google_workspace_agent()
    logger.info("Workspace agent resolved: %s", workspace_agent is not None)

    # 2. PrimeRouter — promote GCP endpoint
    from core.prime_router import get_prime_router, notify_gcp_vm_ready
    router = await get_prime_router()
    logger.info("Router initialized: %s", router._initialized)

    promoted = await notify_gcp_vm_ready(GCP_HOST, GCP_PORT)
    logger.info("GCP promoted: %s (router._gcp_promoted=%s)", promoted, router._gcp_promoted)

    # 3. Notifier
    try:
        from agi_os.notification_bridge import notify_user
        notifier = notify_user
    except Exception:
        notifier = None

    # 4. Create runner and warm up
    from autonomy.email_triage.runner import EmailTriageRunner
    runner = EmailTriageRunner(
        config=config,
        workspace_agent=workspace_agent,
        router=router,
        notifier=notifier,
    )
    await runner.warm_up()
    logger.info("Runner warmed up: %s", runner.is_warmed_up)

    # 5. Run one triage cycle
    deadline = time.monotonic() + 90.0
    report = await asyncio.wait_for(runner.run_cycle(deadline=deadline), timeout=90.0)

    # 6. Results
    print("\n" + "=" * 70)
    print("TRIAGE CYCLE WITH GCP J-PRIME")
    print("=" * 70)
    print(f"  Cycle ID:         {report.cycle_id}")
    print(f"  Skipped:          {report.skipped}")
    print(f"  Emails fetched:   {report.emails_fetched}")
    print(f"  Emails processed: {report.emails_processed}")
    print(f"  Tier counts:      {report.tier_counts}")
    print(f"  Notifications:    {report.notifications_sent}")
    print(f"  Errors:           {report.errors}")
    print(f"  Duration:         {report.completed_at - report.started_at:.1f}s")
    print(f"  Snapshot:         {getattr(report, 'snapshot_committed', None)}")

    # Check extraction source on triaged emails
    triaged = runner._triaged_emails
    sources = {}
    for mid, te in triaged.items():
        src = getattr(te.features, "extraction_source", "unknown")
        sources[src] = sources.get(src, 0) + 1
    print(f"  Extraction srcs:  {sources}")

    if report.tier_counts and any(t < 4 for t in report.tier_counts.keys()):
        print("\n  ** Emails scored to tiers < 4 — AI extraction is working! **")
    elif all(t == 4 for t in report.tier_counts.keys()):
        print("\n  ** All Tier 4 — extraction may still be heuristic-only **")

    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
