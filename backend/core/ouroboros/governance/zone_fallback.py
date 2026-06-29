"""zone_fallback.py -- Multi-Zonal Fallback for GPU provisioning + baking.

GPU capacity stocks out per-zone. A hardcoded zone is brittle: when GCP returns a
STOCKOUT for one zone the system must autonomously retry the SAME request in the
next zone (and, since regional outages happen, the chain spans multiple regions).

Shared by BOTH the Cloud Build baker AND the orchestrator's instances.insert.
Pure + env-driven -- no hardcoded single zone.

Env
---
JARVIS_GCP_ZONE_FALLBACK   comma-separated ordered zone chain (overrides default)
"""
from __future__ import annotations

import os
from typing import List, Optional

# Cross-region L4-capable chain (mirrors jarvis-prime gcp_vm_manager's us-central1
# array, extended cross-region so a single regional stockout can't strand a bake).
_DEFAULT_ZONES = (
    "us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f",
    "us-west1-b", "us-east4-a", "us-east1-b",
)

# A STOCKOUT (transient capacity) is RETRYABLE in another zone. A QUOTA error is
# NOT -- retrying elsewhere just fails again (regional quota), so we must NOT
# treat it as a stockout (that would mask a real limit needing a quota bump).
_STOCKOUT_SIGNS = (
    "stockout",
    "does not have enough resources",
    "zone_resource_pool_exhausted",
    "resource_exhausted",
    "resource availability",
    "resources available to fulfill",
)


def zone_fallback_chain(preferred: Optional[str] = None) -> List[str]:
    """Ordered zone chain; ``preferred`` (e.g. the metadata zone) is tried first,
    then the rest, de-duplicated. NEVER raises."""
    raw = os.environ.get("JARVIS_GCP_ZONE_FALLBACK", "").strip()
    base = [z.strip() for z in raw.split(",") if z.strip()] if raw else list(_DEFAULT_ZONES)
    ordered: List[str] = []
    if preferred and preferred.strip():
        ordered.append(preferred.strip())
    ordered.extend(base)
    seen = set()
    out = []
    for z in ordered:
        if z and z not in seen:
            seen.add(z)
            out.append(z)
    return out


def is_stockout_error(text: str) -> bool:
    """True iff the error text is a transient capacity STOCKOUT (retryable in
    another zone). Quota/permission errors are NOT stockouts. NEVER raises."""
    t = str(text or "").lower()
    if "quota" in t:  # quota exhaustion is regional + not fixed by another zone
        return False
    return any(s in t for s in _STOCKOUT_SIGNS)


__all__ = ["zone_fallback_chain", "is_stockout_error"]
