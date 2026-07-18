"""DW availability folds in the complete_sync (DIRECT_COMPLETION) surface.

_resolve_dw used to give a DW-wide verdict from ONLY the DIRECT_STREAMING (SSE)
surface. SSE is chronically degraded, which is precisely why complete_sync
(DIRECT_COMPLETION) exists — but with SSE transport_degraded and completion
healthy, DW was marked "down" and routing cascaded everything to Claude →
RATE_LIMITED → all_providers_exhausted, 0 ops reached APPLY (bt-2026-07-18-110439).
DW is now "available" if EITHER surface is usable.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.provider_availability import _resolve_dw
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceKind,
    SurfaceVerdict,
)


class _Ledger:
    """Minimal SurfaceHealthLedger double: verdict_for(surface) → record|None."""

    def __init__(self, **verdicts):
        # verdicts keyed by SurfaceKind → SurfaceVerdict (or absent = None)
        self._v = verdicts

    def verdict_for(self, surface):
        v = self._v.get(surface)
        return None if v is None else SimpleNamespace(verdict=v)


def _dw(**kw):
    return _resolve_dw(ledger=_Ledger(**kw))


# ---------------------------------------------------------------------------
# The fix: streaming down + completion healthy → DW available
# ---------------------------------------------------------------------------


def test_streaming_degraded_but_completion_healthy_is_available():
    ok, reason = _dw(**{
        SurfaceKind.DIRECT_STREAMING: SurfaceVerdict.TRANSPORT_DEGRADED,
        SurfaceKind.DIRECT_COMPLETION: SurfaceVerdict.HEALTHY,
    })
    assert ok is True
    assert reason == "direct_completion:healthy"


def test_streaming_auth_failed_but_completion_usable_is_available():
    # Even an entitlement 403 on the SSE surface doesn't down DW if complete_sync
    # is usable (upstream_degraded still counts as usable).
    ok, reason = _dw(**{
        SurfaceKind.DIRECT_STREAMING: SurfaceVerdict.AUTH_FAILED,
        SurfaceKind.DIRECT_COMPLETION: SurfaceVerdict.UPSTREAM_DEGRADED,
    })
    assert ok is True
    assert reason.startswith("direct_completion:")


# ---------------------------------------------------------------------------
# Legacy behavior preserved
# ---------------------------------------------------------------------------


def test_streaming_healthy_is_byte_identical_legacy():
    ok, reason = _dw(**{SurfaceKind.DIRECT_STREAMING: SurfaceVerdict.HEALTHY})
    assert ok is True and reason == "healthy"      # bare verdict value, as before


def test_both_absent_is_legacy_safe_healthy():
    ok, reason = _dw()
    assert ok is True and reason == "unknown"


def test_true_outage_both_surfaces_down_is_unavailable():
    ok, reason = _dw(**{
        SurfaceKind.DIRECT_STREAMING: SurfaceVerdict.TRANSPORT_DEGRADED,
        SurfaceKind.DIRECT_COMPLETION: SurfaceVerdict.TRANSPORT_DEGRADED,
    })
    assert ok is False
    assert reason == "transport_degraded"          # streaming reason preserved


def test_streaming_down_no_completion_record_is_unavailable():
    ok, reason = _dw(**{SurfaceKind.DIRECT_STREAMING: SurfaceVerdict.AUTH_FAILED})
    assert ok is False and reason == "auth_failed"  # forensic label preserved


def test_no_streaming_record_completion_down_reports_completion_reason():
    ok, reason = _dw(**{SurfaceKind.DIRECT_COMPLETION: SurfaceVerdict.INFERENCE_DEGRADED})
    assert ok is False and reason == "inference_degraded"
