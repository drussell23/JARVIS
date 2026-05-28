from __future__ import annotations

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceKind,
    SurfaceVerdict,
    SurfaceHealthRecord,
)


def test_surface_kind_closed_taxonomy():
    assert {k.value for k in SurfaceKind} == {
        "batch_storage", "direct_streaming", "auth_sync",
    }


def test_surface_verdict_closed_taxonomy():
    assert {v.value for v in SurfaceVerdict} == {
        "healthy", "transport_degraded", "upstream_degraded",
        "auth_failed", "error_other",
    }


def test_record_roundtrips_through_json():
    rec = SurfaceHealthRecord(
        surface=SurfaceKind.DIRECT_STREAMING,
        verdict=SurfaceVerdict.UPSTREAM_DEGRADED,
        last_probe_unix=1779992906.0,
        latency_ms=712,
        diagnostic="done_before_content",
        consecutive_failures=3,
    )
    restored = SurfaceHealthRecord.from_json_dict(rec.to_json_dict())
    assert restored == rec


def test_from_json_dict_rejects_unknown_surface():
    assert SurfaceHealthRecord.from_json_dict({"surface": "bogus"}) is None
