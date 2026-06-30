"""Fix A regression: on-demand-on-stockout fallback (gated) in _insert_in_zone.

When ``JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT`` is ON, a SPOT stockout in a zone
falls through to an on-demand insert in the SAME zone before giving up (L4 Spot
is scarce; on-demand has capacity in a quota'd region). Default OFF -> byte
identical to today's immediate-stockout-return Spot-only behavior.

Covers BOTH stockout seams:
  - op-stockout: HTTP 200 -> insert op resolves "stockout" via _await_insert_operation.
  - sync-stockout: HTTP 4xx whose body is_stockout_error(text).

Seams mocked: module ``_http_request`` (returns (status, text)) +
``_await_insert_operation`` (returns "ok"/"stockout"/"error"). is_stockout_error
is driven via real bodies for the sync path.
"""
from __future__ import annotations

import json

import pytest

import backend.core.ouroboros.governance.gcp_compute_rest as gr
from backend.core.ouroboros.governance.gcp_compute_rest import GCPComputeRest

pytestmark = pytest.mark.asyncio

_OK_INSERT = (200, json.dumps({"name": "op-insert"}))
# A synchronous stockout rejection body (matched by is_stockout_error).
_SYNC_STOCKOUT = (
    503,
    json.dumps({"error": {"errors": [{"reason": "ZONE_RESOURCE_POOL_EXHAUSTED"}]}}),
)
_INSERT_KW = dict(
    zone="us-central1-a",
    project="my-project",
    token="ya29.FAKE",
    headers={"Authorization": "Bearer ya29.FAKE"},
    node="jarvis-prime-failover",
    machine="g2-standard-4",
    family="jarvis-prime-coder",
    startup_script="#!/bin/bash\ntrue\n",
    accelerator_type="nvidia-l4",
    accelerator_count=1,
)


class _RecordingHTTP:
    """Returns scripted (status, text) per POST call; records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def __call__(self, url, *, method="GET", headers=None, body=None, timeout_s=10.0):
        self.calls.append({"url": url, "method": method, "body": body})
        idx = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[idx]

    @property
    def insert_attempts(self) -> int:
        return sum(1 for c in self.calls if c["method"] == "POST")


def _patch_await(monkeypatch, verdicts):
    """Patch _await_insert_operation to pop scripted verdicts in order."""
    seq = list(verdicts)
    calls = {"n": 0}

    async def _fake(self, project, zone, op_name, token):
        v = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return v

    monkeypatch.setattr(GCPComputeRest, "_await_insert_operation", _fake)
    return calls


# --------------------------------------------------------------------------
# op-stockout path (HTTP 200 -> op resolves "stockout")
# --------------------------------------------------------------------------

async def test_spot_stockout_falls_through_to_ondemand_when_enabled(monkeypatch):
    """Flag ON: Spot op-stockout -> on-demand insert created. TWO inserts."""
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    http = _RecordingHTTP([_OK_INSERT, _OK_INSERT])
    monkeypatch.setattr(gr, "_http_request", http)
    # spot op -> stockout; on-demand op -> ok.
    _patch_await(monkeypatch, ["stockout", "ok"])

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "created"
    assert "mode=on-demand" in detail
    assert http.insert_attempts == 2


async def test_spot_stockout_returns_stockout_when_disabled(monkeypatch):
    """Flag OFF: Spot op-stockout -> immediate stockout. ONE insert (byte-identical)."""
    monkeypatch.delenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", raising=False)
    http = _RecordingHTTP([_OK_INSERT, _OK_INSERT])
    monkeypatch.setattr(gr, "_http_request", http)
    _patch_await(monkeypatch, ["stockout", "ok"])

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "stockout"
    assert "op_stockout" in detail
    assert http.insert_attempts == 1


async def test_ondemand_also_stockout_gives_up(monkeypatch):
    """Flag ON: both Spot + on-demand op-stockout -> give up on the zone (not created)."""
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    http = _RecordingHTTP([_OK_INSERT, _OK_INSERT])
    monkeypatch.setattr(gr, "_http_request", http)
    # spot -> stockout, on-demand -> stockout (returns immediately, spot is False).
    _patch_await(monkeypatch, ["stockout", "stockout"])

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "stockout"  # on-demand pass returned stockout
    assert verdict != "created"
    assert http.insert_attempts == 2


# --------------------------------------------------------------------------
# sync-stockout path (HTTP 4xx whose body is_stockout_error)
# --------------------------------------------------------------------------

async def test_sync_stockout_falls_through_to_ondemand_when_enabled(monkeypatch):
    """Flag ON: Spot sync-stockout -> on-demand insert created. TWO inserts."""
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    http = _RecordingHTTP([_SYNC_STOCKOUT, _OK_INSERT])
    monkeypatch.setattr(gr, "_http_request", http)
    _patch_await(monkeypatch, ["ok"])  # only the on-demand 200 reaches _await

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "created"
    assert "mode=on-demand" in detail
    assert http.insert_attempts == 2


async def test_sync_stockout_returns_stockout_when_disabled(monkeypatch):
    """Flag OFF: Spot sync-stockout -> immediate stockout. ONE insert (byte-identical)."""
    monkeypatch.delenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", raising=False)
    http = _RecordingHTTP([_SYNC_STOCKOUT, _OK_INSERT])
    monkeypatch.setattr(gr, "_http_request", http)

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    assert verdict == "stockout"
    assert "sync_stockout" in detail
    assert http.insert_attempts == 1


async def test_sync_stockout_ondemand_also_stockout_gives_up(monkeypatch):
    """Flag ON: both Spot + on-demand sync-stockout -> give up on the zone."""
    monkeypatch.setenv("JARVIS_FAILOVER_ONDEMAND_ON_STOCKOUT", "true")
    http = _RecordingHTTP([_SYNC_STOCKOUT, _SYNC_STOCKOUT])
    monkeypatch.setattr(gr, "_http_request", http)

    verdict, detail = await GCPComputeRest()._insert_in_zone(**_INSERT_KW)

    # On-demand also sync-stockout: spot is False -> returns ("stockout", sync_stockout).
    assert verdict == "stockout"
    assert verdict != "created"
    assert http.insert_attempts == 2
