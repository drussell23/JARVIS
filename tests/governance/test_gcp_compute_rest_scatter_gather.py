"""Tests for the Asynchronous Scatter-Gather Failover refactor in
gcp_compute_rest.py (GCPComputeRest._race_wave / _sweep_losers / create_instance).

The multi-zone awaken used to roll zone_fallback_chain LINEARLY -- one insert
at a time -- so a real soak probed only 2 of 11 zones in a 15-minute failover
window while 9 sat untried. This refactor consumes the chain W zones at a time
(``JARVIS_FAILOVER_SCATTER_WIDTH``, default 3), races every zone in a wave
CONCURRENTLY via ``asyncio.wait(..., return_when=FIRST_COMPLETED)``, and lets
the FIRST zone to VERIFY as created win the lock -- tearing down every other
wave member (pending-cancel + unconditional best-effort delete) the moment a
winner is decided.

ZERO real GCP / network -- everything is mocked at the ``_http_request``
boundary via a ZONE-AWARE scripted fake (``ZoneScriptedHTTP``). Unlike the
legacy ``FakeHTTP`` in test_gcp_compute_rest.py (a single shared, non-zone-aware
response index -- correct for the OLD strictly-linear one-zone-at-a-time chain,
but ambiguous once multiple zones fire concurrently), this fake routes every
insert / operation-poll / delete call by the ZONE segment of the URL, so
concurrent racers never collide on a shared script.

Covers:
  (a) W=3, zone2 succeeds while zone1/zone3 stockout -> winner=zone2, deletes
      issued for zone1+zone3, result correct.
  (b) W=3, all 3 stockout -> second wave launches with next zones; success there
      wins.
  (c) W=1 -> byte-equivalent legacy linear sequence (zones attempted strictly
      serially, proven via recorded request ordering).
  (d) winner decided while a sibling is still genuinely in-flight -> sibling
      cancelled (proven by wall-clock: the test does NOT wait out the sibling's
      artificial multi-second delay) AND its zone still gets a delete sweep.
  (e) delete sweep tolerates 404 (loser never materialized) without raising.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gr
from backend.core.ouroboros.governance.gcp_compute_rest import GCPComputeRest

pytestmark = pytest.mark.asyncio

_STOCKOUT_ERR = {"errors": [{"code": "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"}]}

_ZONE_RE = re.compile(r"/zones/([^/]+)/")


class ZoneScriptedHTTP:
    """Zone-aware scripted HTTP fake. Every zone gets its OWN script (insert
    HTTP status, terminal operation status/error, an optional artificial
    delay to keep a racer genuinely in-flight, and an optional delete status)
    so concurrent zone racers never share mutable index state. Records every
    call + every DELETE (by zone) for assertions.

    Unscripted zones default to an immediate clean success -- tests only need
    to script the zones whose behavior matters.
    """

    def __init__(self) -> None:
        self.zone_scripts: dict = {}
        self.calls: list = []
        self.deletes: list = []  # zone, in call order
        self.token = json.dumps({"access_token": "ya29.FAKE", "expires_in": 3599})
        self.scopes_text = "https://www.googleapis.com/auth/cloud-platform"
        self.zone_text = "projects/123456789/zones/us-central1-a"
        self.project_text = "my-test-project"

    def script_zone(
        self, zone, *, insert_status=200, op_status="DONE", op_error=None,
        insert_delay=0.0, delete_status=200,
    ) -> None:
        self.zone_scripts[zone] = dict(
            insert_status=insert_status, op_status=op_status, op_error=op_error,
            insert_delay=insert_delay, delete_status=delete_status,
        )

    def _script(self, zone):
        return self.zone_scripts.get(zone) or dict(
            insert_status=200, op_status="DONE", op_error=None,
            insert_delay=0.0, delete_status=200,
        )

    async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        self.calls.append({"url": url, "method": method, "body": body})
        if "metadata.google.internal" in url:
            if url.endswith("/token"):
                return (200, self.token)
            if url.endswith("/scopes"):
                return (200, self.scopes_text)
            if url.endswith("instance/zone"):
                return (200, self.zone_text)
            if url.endswith("project/project-id"):
                return (200, self.project_text)
            return (404, "")

        m = _ZONE_RE.search(url)
        zone = m.group(1) if m else "?"
        script = self._script(zone)

        if method == "POST" and url.endswith("/instances"):
            if script["insert_delay"]:
                await asyncio.sleep(script["insert_delay"])
            if script["insert_status"] >= 300:
                return (script["insert_status"], "insert rejected")
            return (script["insert_status"], json.dumps({"name": "op-insert"}))

        if method == "GET" and "/operations/" in url:
            doc = {"status": script["op_status"]}
            if script["op_error"] is not None:
                doc["error"] = script["op_error"]
            return (200, json.dumps(doc))

        if method == "DELETE":
            self.deletes.append(zone)
            return (script["delete_status"], json.dumps({"name": "op-delete"}))

        return (0, "[unrouted]")


@pytest.fixture
def http(monkeypatch):
    fake = ZoneScriptedHTTP()
    monkeypatch.setattr(gr, "_http_request", fake)
    for var in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_ZONE"):
        monkeypatch.delenv(var, raising=False)
    # Fast, deterministic polling everywhere; Spot-only (no on-demand
    # escalation) so each zone issues exactly ONE insert POST unless a test
    # scripts otherwise.
    monkeypatch.setenv("JARVIS_INSERT_OP_POLL_CAP_S", "0.05")
    monkeypatch.setenv("JARVIS_INSERT_OP_POLL_INTERVAL_S", "0.001")
    monkeypatch.setenv("JARVIS_REAP_CONFIRM_SETTLE_S", "0.001")
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "false")
    return fake


def _insert_posts(http, zones=None):
    """Zones (in call order) that received an instances.insert POST."""
    out = []
    for call in http.calls:
        if call["method"] == "POST" and call["url"].endswith("/instances"):
            m = _ZONE_RE.search(call["url"])
            out.append(m.group(1) if m else "?")
    return out if zones is None else [z for z in out if z in zones]


# ---------------------------------------------------------------------------
# (a) W=3: zone2 wins, zone1+zone3 stockout -> both reaped
# ---------------------------------------------------------------------------

async def test_wave_winner_reaps_every_other_wave_zone(monkeypatch, http):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "3")
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "zone1,zone2,zone3")
    # The metadata-resolved "preferred" zone always leads the chain -- match
    # it to zone1 (already first in the override list) so the chain is
    # exactly [zone1, zone2, zone3], not a 4-zone chain with a duplicate lead.
    http.zone_text = "projects/123456789/zones/zone1"
    http.script_zone("zone1", op_status="DONE", op_error=_STOCKOUT_ERR)
    http.script_zone("zone2", op_status="DONE", op_error=None)  # winner
    http.script_zone("zone3", op_status="DONE", op_error=_STOCKOUT_ERR)

    ok, detail = await GCPComputeRest().create_instance(startup_script="#!/bin/bash\ntrue\n")

    assert ok is True
    assert "created" in detail
    assert "zone2" in detail
    assert set(http.deletes) == {"zone1", "zone3"}
    assert "zone2" not in http.deletes


# ---------------------------------------------------------------------------
# (b) W=3: all 3 stockout -> second wave launches, success there wins
# ---------------------------------------------------------------------------

async def test_all_wave_zones_stockout_advances_to_next_wave(monkeypatch, http):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "3")
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "zone1,zone2,zone3,zone4")
    http.zone_text = "projects/123456789/zones/zone1"
    for z in ("zone1", "zone2", "zone3"):
        http.script_zone(z, op_status="DONE", op_error=_STOCKOUT_ERR)
    http.script_zone("zone4", op_status="DONE", op_error=None)  # 2nd wave winner

    ok, detail = await GCPComputeRest().create_instance(startup_script="#!/bin/bash\ntrue\n")

    assert ok is True
    assert "created" in detail
    assert "zone4" in detail
    # The first wave (zone1-3) fully stocked out with no winner -> no reap
    # needed for any of them (nothing was ever created).
    assert http.deletes == []
    # Every zone in the chain was attempted -- proves the second wave launched.
    posted = _insert_posts(http)
    assert set(posted) == {"zone1", "zone2", "zone3", "zone4"}


# ---------------------------------------------------------------------------
# (c) W=1: byte-equivalent legacy linear sequence -- strictly serial
# ---------------------------------------------------------------------------

async def test_width_one_is_strictly_serial_legacy_sequence(monkeypatch, http):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "1")
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "zone1,zone2,zone3")
    http.zone_text = "projects/123456789/zones/zone1"
    http.script_zone("zone1", op_status="DONE", op_error=_STOCKOUT_ERR)
    http.script_zone("zone2", op_status="DONE", op_error=_STOCKOUT_ERR)
    http.script_zone("zone3", op_status="DONE", op_error=None)  # winner

    ok, detail = await GCPComputeRest().create_instance(startup_script="#!/bin/bash\ntrue\n")

    assert ok is True
    assert "zone3" in detail
    # Exactly ONE zone attempted per wave, in strict chain order -- no
    # concurrent interleaving, matching the pre-refactor linear for-loop.
    assert _insert_posts(http) == ["zone1", "zone2", "zone3"]
    # No wasted reap for a W=1 chain -- each zone is either the eventual
    # winner or fully resolved (stockout) before the next zone is even tried.
    assert http.deletes == []


# ---------------------------------------------------------------------------
# (d) Winner decided while a sibling is still genuinely pending -> cancelled
#     + unconditionally reaped.
# ---------------------------------------------------------------------------

async def test_pending_sibling_cancelled_and_still_reaped(monkeypatch, http):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "2")
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "fast_zone,slow_zone")
    http.zone_text = "projects/123456789/zones/fast_zone"
    http.script_zone("fast_zone", op_status="DONE", op_error=None)  # wins instantly
    # slow_zone's insert POST never returns within the test window unless
    # genuinely cancelled -- a multi-second artificial delay.
    http.script_zone("slow_zone", op_status="DONE", op_error=None, insert_delay=5.0)

    started = time.monotonic()
    ok, detail = await asyncio.wait_for(
        GCPComputeRest().create_instance(startup_script="#!/bin/bash\ntrue\n"),
        timeout=2.0,
    )
    elapsed = time.monotonic() - started

    assert ok is True
    assert "fast_zone" in detail
    # Proves the sibling was CANCELLED, not waited out: total wall time is far
    # below slow_zone's 5s artificial delay.
    assert elapsed < 2.0
    assert "slow_zone" in http.deletes


# ---------------------------------------------------------------------------
# (e) Delete sweep tolerates 404 (loser never materialized) without raising.
# ---------------------------------------------------------------------------

async def test_loser_sweep_tolerates_404_without_raising(monkeypatch, http):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "2")
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "winner_zone,ghost_zone")
    http.zone_text = "projects/123456789/zones/winner_zone"
    http.script_zone("winner_zone", op_status="DONE", op_error=None)
    http.script_zone(
        "ghost_zone", op_status="DONE", op_error=_STOCKOUT_ERR, delete_status=404,
    )

    ok, detail = await GCPComputeRest().create_instance(startup_script="#!/bin/bash\ntrue\n")

    assert ok is True  # the 404 sweep never raises into the awaken path
    assert "winner_zone" in detail
    assert "ghost_zone" in http.deletes


async def test_sweep_losers_directly_tolerates_404(http):
    http.script_zone("ghost_zone", delete_status=404)
    c = GCPComputeRest()
    # NEVER raises, even when every loser is already gone (404).
    await c._sweep_losers("some-node", ["ghost_zone"])
    assert "ghost_zone" in http.deletes


# ---------------------------------------------------------------------------
# JARVIS_FAILOVER_SCATTER_WIDTH parsing
# ---------------------------------------------------------------------------

def test_scatter_width_default_is_three(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_SCATTER_WIDTH", raising=False)
    assert gr._scatter_width() == 3


def test_scatter_width_clamped_to_minimum_one(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "0")
    assert gr._scatter_width() == 1
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "-5")
    assert gr._scatter_width() == 1


def test_scatter_width_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_SCATTER_WIDTH", "not-a-number")
    assert gr._scatter_width() == 3
