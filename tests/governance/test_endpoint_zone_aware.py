"""Zone-aware endpoint/SERVING discovery (the A1 last-mile fix).

The multi-zonal awaken lands the failover node in ANY fallback zone (a Spot
stockout in us-central1-a -> the node is created in -b or -c). But
``get_node_endpoints`` pinned its instances.get to ``GCP_ZONE`` only, so when the
node landed in -b the L7 SERVING gate queried -a, 404'd, saw "no external IP"
forever, and reaped a FULLY-SERVING 32B node it never dispatched to (proven live:
``curl 34.9.86.150:11434/api/tags`` returned qwen2.5-coder:32b while the driver
saw nothing).

These tests pin the fix: discovery searches the SAME zone_fallback_chain the
teardown already brute-forces (#69779), so a node in any candidate zone is found.
A node present-but-still-booting (200, no natIP yet) is returned as found in THAT
zone -- the scan does not skip past it.
"""
from __future__ import annotations

import json
import re

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gr
from backend.core.ouroboros.governance.gcp_compute_rest import GCPComputeRest

pytestmark = pytest.mark.asyncio

_ZONE_RE = re.compile(r"/zones/([^/]+)/instances/")


def _instance_doc(*, internal="10.128.0.18", external="34.9.86.150"):
    nic = {"networkIP": internal}
    if external is not None:
        nic["accessConfigs"] = [{"natIP": external}]
    return {"status": "RUNNING", "networkInterfaces": [nic]}


class _ZoneRoutingHTTP:
    """Routes instances.get by the zone embedded in the URL. ``present`` maps a
    zone -> instance doc (200); any other zone -> 404. Records queried zones."""

    def __init__(self, present):
        self._present = present
        self.zones_queried = []

    async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        m = _ZONE_RE.search(url)
        z = m.group(1) if m else "?"
        self.zones_queried.append(z)
        if z in self._present:
            return (200, json.dumps(self._present[z]))
        return (404, json.dumps({"error": {"code": 404, "message": "not found"}}))


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    # Deterministic identity + a 3-zone chain (default -a first).
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-central1-a,us-central1-b,us-central1-c")

    async def _tok(self):
        return "ya29.FAKE"

    async def _zone(self):
        return "us-central1-a"

    async def _proj(self):
        return "my-project"

    monkeypatch.setattr(GCPComputeRest, "access_token", _tok)
    monkeypatch.setattr(GCPComputeRest, "zone", _zone)
    monkeypatch.setattr(GCPComputeRest, "project", _proj)


async def test_finds_node_in_default_zone(monkeypatch):
    http = _ZoneRoutingHTTP({"us-central1-a": _instance_doc()})
    monkeypatch.setattr(gr, "_http_request", http)

    internal, external = await GCPComputeRest().get_node_endpoints()

    assert (internal, external) == ("10.128.0.18", "34.9.86.150")
    # Default zone hit first -> no wasted scan of -b/-c.
    assert http.zones_queried == ["us-central1-a"]


async def test_searches_fallback_when_not_in_default(monkeypatch):
    # Node landed in -b (Spot stockout in -a). THE FIX: discovery must find it.
    http = _ZoneRoutingHTTP({"us-central1-b": _instance_doc(external="34.9.86.150")})
    monkeypatch.setattr(gr, "_http_request", http)

    internal, external = await GCPComputeRest().get_node_endpoints()

    assert external == "34.9.86.150"
    assert http.zones_queried == ["us-central1-a", "us-central1-b"]  # -a 404 -> -b hit


async def test_explicit_zone_queries_only_that_zone(monkeypatch):
    http = _ZoneRoutingHTTP({"us-central1-c": _instance_doc(external="34.9.86.200")})
    monkeypatch.setattr(gr, "_http_request", http)

    internal, external = await GCPComputeRest().get_node_endpoints(zone="us-central1-c")

    assert external == "34.9.86.200"
    assert http.zones_queried == ["us-central1-c"]  # no chain search


async def test_returns_none_when_absent_everywhere(monkeypatch):
    http = _ZoneRoutingHTTP({})  # 404 in every zone
    monkeypatch.setattr(gr, "_http_request", http)

    internal, external = await GCPComputeRest().get_node_endpoints()

    assert (internal, external) == (None, None)
    assert http.zones_queried == ["us-central1-a", "us-central1-b", "us-central1-c"]


async def test_present_but_no_external_ip_yet_stops_at_that_zone(monkeypatch):
    # Node is in -b but still booting (no natIP). It IS found there -> return
    # (internal, None); do NOT scan past the zone that holds the node.
    http = _ZoneRoutingHTTP({"us-central1-b": _instance_doc(external=None)})
    monkeypatch.setattr(gr, "_http_request", http)

    internal, external = await GCPComputeRest().get_node_endpoints()

    assert internal == "10.128.0.18"
    assert external is None
    assert http.zones_queried == ["us-central1-a", "us-central1-b"]  # stops at -b


async def test_no_token_fails_closed(monkeypatch):
    async def _no_tok(self):
        return None

    monkeypatch.setattr(GCPComputeRest, "access_token", _no_tok)
    http = _ZoneRoutingHTTP({"us-central1-a": _instance_doc()})
    monkeypatch.setattr(gr, "_http_request", http)

    internal, external = await GCPComputeRest().get_node_endpoints()

    assert (internal, external) == (None, None)
    assert http.zones_queried == []  # never reached the network
