#!/usr/bin/env python3
"""Test single email extraction via GCP J-Prime."""
from __future__ import annotations
import asyncio, os, sys, time, logging
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Load .env except bad ANTHROPIC_API_KEY
for line in (_BACKEND / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k == "ANTHROPIC_API_KEY":
            continue
        os.environ.setdefault(k, v)

os.environ["JARVIS_INVINCIBLE_NODE_IP"] = "34.45.154.209"
os.environ["JARVIS_WORKSPACE_ALLOW_STANDALONE"] = "true"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
for n in ("httpcore", "httpx", "anthropic", "urllib3", "googleapiclient",
          "backend.core.trinity", "CrossRepoExperienceForwarder", "vision",
          "jarvis.shutdown", "GracefulShutdown", "backend.kernel",
          "backend.core.thread_manager", "backend.core.prime_client"):
    logging.getLogger(n).setLevel(logging.WARNING)

logger = logging.getLogger("test_gcp")


async def main():
    from core.prime_router import get_prime_router, notify_gcp_vm_ready

    router = await get_prime_router()
    promoted = await notify_gcp_vm_ready("34.45.154.209", 8000)
    logger.info("GCP promoted: %s", promoted)

    from autonomy.email_triage.extraction import extract_features

    email = {
        "id": "test-001",
        "from": "boss@company.com",
        "subject": "URGENT: Deploy fix before 5pm deadline",
        "snippet": (
            "The production server is down. We need the hotfix deployed "
            "ASAP before the 5pm deadline."
        ),
        "labels": ["INBOX", "UNREAD", "IMPORTANT"],
    }

    logger.info("Extracting features for test email via GCP J-Prime...")
    start = time.monotonic()
    features = await extract_features(email, router, deadline=time.monotonic() + 60.0)
    elapsed = (time.monotonic() - start) * 1000

    print(f"\n{'='*60}")
    print(f"EXTRACTION RESULT ({elapsed:.0f}ms)")
    print(f"{'='*60}")
    print(f"  message_id:     {features.message_id}")
    print(f"  keywords:       {features.keywords}")
    print(f"  sender_freq:    {features.sender_frequency}")
    print(f"  urgency:        {features.urgency_signals}")
    print(f"  confidence:     {features.extraction_confidence}")
    print(f"  source:         {features.extraction_source}")
    print(f"  contract_ver:   {getattr(features, 'extraction_contract_version', 'N/A')}")

    if features.extraction_source == "jprime_v1":
        print(f"\n  ** AI EXTRACTION WORKING via GCP J-Prime! **")
    elif features.extraction_confidence == 0.0:
        print(f"\n  ** Fell back to heuristic-only (AI extraction failed) **")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
