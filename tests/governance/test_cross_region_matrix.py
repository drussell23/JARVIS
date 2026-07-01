"""Cross-region L4 capacity matrix (beat a whole-region stockout).

A single-region zone chain strands the awaken when that region is fully
ZONE_RESOURCE_POOL_EXHAUSTED. The fallback chain must span regions -- preferred
region first, then fall to other L4-capable regions -- so the hunt adapts to
GLOBAL capacity, not local scarcity. Env-driven; no hardcoded single region.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.zone_fallback as zf


def test_default_chain_spans_multiple_regions(monkeypatch):
    monkeypatch.delenv("JARVIS_GCP_ZONE_FALLBACK", raising=False)
    chain = zf.zone_fallback_chain("us-central1-a")
    regions = {zf.region_of(z) for z in chain}
    assert "us-central1" in regions
    # Must fall out of us-central1 into other L4 regions.
    assert len(regions) >= 3, regions
    assert any(r != "us-central1" for r in regions)


def test_preferred_region_ordered_first(monkeypatch):
    monkeypatch.delenv("JARVIS_GCP_ZONE_FALLBACK", raising=False)
    chain = zf.zone_fallback_chain("us-west1-b")
    # The preferred zone leads; its region's zones precede other regions.
    assert chain[0] == "us-west1-b"
    first_other = next(i for i, z in enumerate(chain) if zf.region_of(z) != "us-west1")
    west_idxs = [i for i, z in enumerate(chain) if zf.region_of(z) == "us-west1"]
    assert max(west_idxs) < first_other  # all us-west1 zones before any other region


def test_region_of():
    assert zf.region_of("us-east4-a") == "us-east4"
    assert zf.region_of("us-central1-f") == "us-central1"
    assert zf.region_of("") == ""


def test_regions_in_chain_ordered_unique(monkeypatch):
    monkeypatch.delenv("JARVIS_GCP_ZONE_FALLBACK", raising=False)
    regions = zf.regions_in_chain()
    assert regions[0] == zf.region_of(zf.zone_fallback_chain()[0])
    assert len(regions) == len(set(regions))  # unique, order-preserved


def test_env_override_still_honored(monkeypatch):
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "europe-west4-a,europe-west1-b")
    chain = zf.zone_fallback_chain()
    assert chain[:2] == ["europe-west4-a", "europe-west1-b"]


def test_dedup_preserves_order(monkeypatch):
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-east1-c,us-east1-c,us-west1-a")
    chain = zf.zone_fallback_chain("us-east1-c")
    assert chain == ["us-east1-c", "us-west1-a"]


# ---------------------------------------------------------------------------
# Fast-Fail: create_instance emits HARDWARE_CAPACITY_EXHAUSTED on global stockout
# ---------------------------------------------------------------------------

import logging  # noqa: E402
import pytest  # noqa: E402
import backend.core.ouroboros.governance.gcp_compute_rest as gr  # noqa: E402
from backend.core.ouroboros.governance.gcp_compute_rest import GCPComputeRest  # noqa: E402

pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_create_instance_emits_capacity_exhausted_on_global_stockout(monkeypatch, caplog):
    monkeypatch.delenv("JARVIS_GCP_ZONE_FALLBACK", raising=False)

    async def _tok(self):
        return "ya29.FAKE"

    async def _zone(self):
        return "us-central1-a"

    async def _proj(self):
        return "p"

    async def _all_stockout(self, **kw):
        return ("stockout", "zone={}:op_stockout".format(kw.get("zone")))

    monkeypatch.setattr(GCPComputeRest, "access_token", _tok)
    monkeypatch.setattr(GCPComputeRest, "zone", _zone)
    monkeypatch.setattr(GCPComputeRest, "project", _proj)
    monkeypatch.setattr(GCPComputeRest, "_insert_in_zone", _all_stockout)

    with caplog.at_level(logging.ERROR):
        ok, detail = await GCPComputeRest().create_instance(startup_script="x")

    assert ok is False
    assert "all_zones_stockout:regions=" in detail          # regions recorded
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "HARDWARE_CAPACITY_EXHAUSTED" in blob            # the fast-fail marker
