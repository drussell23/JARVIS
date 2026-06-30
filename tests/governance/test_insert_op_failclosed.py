"""Fail-closed insert-operation verification (phantom-node protection).

The A1-on-32B soak halted on a PHANTOM node: an on-demand L4 insert whose async
operation took ~31s to surface ZONE_RESOURCE_POOL_EXHAUSTED, but the 25s poll cap
expired first and ``_await_insert_operation`` returned an optimistic ``"ok"``. The
FSM went AWAKENING on a node GCE then rolled back, and the zone chain never tried
the next zone.

These tests pin the fail-closed contract:
  1. Terminal-state verification -- ``"ok"`` ONLY on operations.get status=DONE
     with NO error object.
  2. Phantom protection -- a breached safety ceiling (or an unreachable API)
     returns ``"unknown"``, NEVER an optimistic ``"ok"``.
  3. Multi-zonal rollover -- an ``"unknown"`` makes _insert_in_zone reap any late
     phantom (delete_instance) and return ``"stockout"`` so create_instance
     advances to the next zone.
"""
from __future__ import annotations

import json

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gr
from backend.core.ouroboros.governance.gcp_compute_rest import GCPComputeRest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # Tiny cap + interval so the poll loop is fast + deterministic.
    monkeypatch.setenv("JARVIS_INSERT_OP_POLL_CAP_S", "0.05")
    monkeypatch.setenv("JARVIS_INSERT_OP_POLL_INTERVAL_S", "0.001")


def _op(status, *, error=None):
    body = {"status": status}
    if error is not None:
        body["error"] = error
    return (200, json.dumps(body))


_STOCKOUT_ERR = {"errors": [{"code": "ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS"}]}
_QUOTA_ERR = {"errors": [{"code": "QUOTA_EXCEEDED"}]}


class _ScriptedGET:
    """Returns scripted operations.get responses in order; records calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        i = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[i]


# ---------------------------------------------------------------------------
# _await_insert_operation -- terminal-state verification
# ---------------------------------------------------------------------------

async def test_await_ok_only_on_done_no_error(monkeypatch):
    monkeypatch.setattr(gr, "_http_request", _ScriptedGET([_op("DONE")]))
    v = await GCPComputeRest()._await_insert_operation("p", "z", "op-1", "tok")
    assert v == "ok"


async def test_await_stockout_on_done_with_stockout_error(monkeypatch):
    monkeypatch.setattr(gr, "_http_request", _ScriptedGET([_op("DONE", error=_STOCKOUT_ERR)]))
    v = await GCPComputeRest()._await_insert_operation("p", "z", "op-1", "tok")
    assert v == "stockout"


async def test_await_error_on_done_with_nonstockout_error(monkeypatch):
    monkeypatch.setattr(gr, "_http_request", _ScriptedGET([_op("DONE", error=_QUOTA_ERR)]))
    v = await GCPComputeRest()._await_insert_operation("p", "z", "op-1", "tok")
    assert v == "error"


async def test_await_polls_through_running_until_done(monkeypatch):
    # RUNNING twice, then DONE+no-error -> it must keep polling, not bail.
    http = _ScriptedGET([_op("RUNNING"), _op("RUNNING"), _op("DONE")])
    monkeypatch.setattr(gr, "_http_request", http)
    monkeypatch.setenv("JARVIS_INSERT_OP_POLL_CAP_S", "5.0")  # room to reach DONE
    v = await GCPComputeRest()._await_insert_operation("p", "z", "op-1", "tok")
    assert v == "ok"
    assert http.calls >= 3


async def test_await_unknown_on_ceiling_breach_never_ok(monkeypatch):
    # The operation NEVER reaches DONE within the ceiling -> 'unknown', NOT 'ok'.
    monkeypatch.setattr(gr, "_http_request", _ScriptedGET([_op("RUNNING")]))
    v = await GCPComputeRest()._await_insert_operation("p", "z", "op-1", "tok")
    assert v == "unknown"


async def test_await_unknown_on_persistent_api_failure(monkeypatch):
    # operations.get is unreachable throughout (5xx) -> 'unknown', NOT 'ok'.
    monkeypatch.setattr(gr, "_http_request", _ScriptedGET([(503, "backend error")]))
    v = await GCPComputeRest()._await_insert_operation("p", "z", "op-1", "tok")
    assert v == "unknown"


# ---------------------------------------------------------------------------
# _insert_in_zone -- 'unknown' reaps the phantom + rolls over
# ---------------------------------------------------------------------------

_INSERT_KW = dict(
    zone="us-central1-a",
    project="my-project",
    token="ya29.FAKE",
    headers={"Authorization": "Bearer ya29.FAKE"},
    node="jarvis-prime-failover",
    machine="g2-standard-4",
    family="jarvis-prime-coder-32b",
    startup_script="#!/bin/bash\ntrue\n",
    accelerator_type="nvidia-l4",
    accelerator_count=1,
)


class _PostHTTP:
    """Accepts every POST insert (200 + op name); records POST count."""

    def __init__(self):
        self.posts = 0

    async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        if method == "POST":
            self.posts += 1
        return (200, json.dumps({"name": "op-insert"}))


def _patch_await(monkeypatch, verdicts):
    seq = list(verdicts)
    n = {"i": 0}

    async def _fake(self, project, zone, op_name, token):
        v = seq[min(n["i"], len(seq) - 1)]
        n["i"] += 1
        return v

    monkeypatch.setattr(GCPComputeRest, "_await_insert_operation", _fake)


def _patch_reap(monkeypatch):
    reaped = []

    async def _fake_delete(self, name=None, *, zone=None):
        reaped.append((name, zone))
        return (True, "deleted:200")

    monkeypatch.setattr(GCPComputeRest, "delete_instance", _fake_delete)
    return reaped


async def test_unknown_ondemand_reaps_phantom_and_rolls_over(monkeypatch):
    # On-demand op returns 'unknown' -> reap the phantom + return 'stockout'
    # (so create_instance advances to the next zone). Flag ON so spot escalates.
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    http = _PostHTTP()
    monkeypatch.setattr(gr, "_http_request", http)
    _patch_await(monkeypatch, ["stockout", "unknown"])  # spot stockout -> ondemand unknown
    reaped = _patch_reap(monkeypatch)

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "stockout"          # rolls the chain to the next zone
    assert "unknown" in detail
    assert reaped == [("jarvis-prime-failover", "us-central1-a")]  # phantom reaped in-zone


async def test_unknown_spot_reaps_then_tries_ondemand_same_zone(monkeypatch):
    # Spot op 'unknown' -> reap, then escalate to on-demand SAME zone (which is ok).
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    http = _PostHTTP()
    monkeypatch.setattr(gr, "_http_request", http)
    _patch_await(monkeypatch, ["unknown", "ok"])
    reaped = _patch_reap(monkeypatch)

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "created"
    assert "mode=on-demand" in detail
    assert http.posts == 2                # spot + on-demand
    assert reaped == [("jarvis-prime-failover", "us-central1-a")]  # spot phantom reaped


async def test_missing_op_name_is_fail_closed_not_created(monkeypatch):
    # A 200 insert with NO operation name -> cannot verify -> fail-closed
    # (reap + roll), NEVER an optimistic 'created'.
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "false")

    class _NoName:
        def __init__(self):
            self.posts = 0

        async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
            if method == "POST":
                self.posts += 1
            return (200, json.dumps({}))  # accepted, but no "name"

    monkeypatch.setattr(gr, "_http_request", _NoName())
    reaped = _patch_reap(monkeypatch)

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict != "created"
    assert reaped  # phantom-protection reap fired


# ---------------------------------------------------------------------------
# create_instance -- the chain HUNTS across zones on 'unknown'
# ---------------------------------------------------------------------------

async def test_create_instance_rolls_zones_on_unknown(monkeypatch):
    # Zone 1 op -> unknown (reap + stockout); zone 2 op -> ok (created).
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "false")
    monkeypatch.setenv("JARVIS_GCP_ZONE_FALLBACK", "us-central1-a,us-central1-b")

    # Identity resolution: stub access_token/zone/project so create_instance runs.
    async def _tok(self):
        return "ya29.FAKE"

    async def _zone(self):
        return "us-central1-a"

    async def _proj(self):
        return "my-project"

    monkeypatch.setattr(GCPComputeRest, "access_token", _tok)
    monkeypatch.setattr(GCPComputeRest, "zone", _zone)
    monkeypatch.setattr(GCPComputeRest, "project", _proj)

    http = _PostHTTP()
    monkeypatch.setattr(gr, "_http_request", http)
    _patch_await(monkeypatch, ["unknown", "ok"])  # zone-a unknown, zone-b ok
    reaped = _patch_reap(monkeypatch)

    ok, detail = await GCPComputeRest().create_instance(startup_script="#!/bin/bash\ntrue\n")

    assert ok is True
    assert "created" in detail
    # Reaped the zone-a phantom before rolling to zone-b.
    assert ("jarvis-prime-failover", "us-central1-a") in reaped
