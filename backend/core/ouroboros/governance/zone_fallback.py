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

# Cross-Region L4 Capacity Matrix -- grouped by region, region-ordered. When an
# ENTIRE region is ZONE_RESOURCE_POOL_EXHAUSTED the hunt falls to the next region,
# so provisioning adapts to GLOBAL L4 capacity, not local scarcity. The zones are
# the nvidia-l4-capable zones per region (US-first; extend via env for EU/APAC).
# Env JARVIS_GCP_ZONE_FALLBACK overrides the whole matrix (comma-separated).
_L4_REGION_MATRIX = (
    ("us-central1", ("us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f")),
    ("us-east1", ("us-east1-c", "us-east1-d")),
    ("us-east4", ("us-east4-a", "us-east4-c")),
    ("us-west1", ("us-west1-a", "us-west1-b")),
    ("us-west4", ("us-west4-a",)),
    ("us-south1", ("us-south1-a",)),
)
_DEFAULT_ZONES = tuple(z for _region, _zones in _L4_REGION_MATRIX for z in _zones)


def region_of(zone: str) -> str:
    """The region a zone belongs to (``us-east4-a`` -> ``us-east4``). NEVER raises."""
    z = str(zone or "").strip()
    return z.rsplit("-", 1)[0] if "-" in z else z

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
    """Ordered cross-region zone chain. ``preferred`` (e.g. the metadata zone) leads,
    then the REST OF ITS REGION (exhaust the local region first), then every other
    region in matrix order -- so a whole-region stockout falls to a fallback region.
    Env ``JARVIS_GCP_ZONE_FALLBACK`` overrides the matrix (honored verbatim, still
    preferred-led + region-front-loaded). De-duplicated. NEVER raises."""
    raw = os.environ.get("JARVIS_GCP_ZONE_FALLBACK", "").strip()
    base = [z.strip() for z in raw.split(",") if z.strip()] if raw else list(_DEFAULT_ZONES)
    ordered: List[str] = []
    pref = preferred.strip() if (preferred and preferred.strip()) else ""
    if pref:
        ordered.append(pref)
        # Front-load the rest of the preferred zone's region before other regions.
        pref_region = region_of(pref)
        ordered.extend(z for z in base if region_of(z) == pref_region)
    ordered.extend(base)
    seen = set()
    out: List[str] = []
    for z in ordered:
        if z and z not in seen:
            seen.add(z)
            out.append(z)
    return out


def regions_in_chain(preferred: Optional[str] = None) -> List[str]:
    """The ordered, unique regions the fallback chain will hunt (for cross-region
    logging + 'all regions exhausted' detection). NEVER raises."""
    seen = set()
    out: List[str] = []
    for z in zone_fallback_chain(preferred):
        r = region_of(z)
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def is_stockout_error(text: str) -> bool:
    """True iff the error text is a transient capacity STOCKOUT (retryable in
    another zone). Quota/permission errors are NOT stockouts. NEVER raises."""
    t = str(text or "").lower()
    if "quota" in t:  # quota exhaustion is regional + not fixed by another zone
        return False
    return any(s in t for s in _STOCKOUT_SIGNS)


__all__ = [
    "zone_fallback_chain", "is_stockout_error", "region_of", "regions_in_chain",
]
