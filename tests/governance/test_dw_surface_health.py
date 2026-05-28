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


# ---------------------------------------------------------------------------
# Task 2 — SurfaceHealthLedger
# ---------------------------------------------------------------------------

from pathlib import Path

from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
)


def test_ledger_records_and_persists(tmp_path: Path):
    p = tmp_path / "dw_surface_health.json"
    led = SurfaceHealthLedger(path=p, autosave=True)
    led.record(
        SurfaceKind.BATCH_STORAGE, SurfaceVerdict.HEALTHY,
        latency_ms=710, diagnostic="", now_unix=1779992906.0,
    )
    assert p.exists()
    led2 = SurfaceHealthLedger(path=p)
    snap = led2.verdict_for(SurfaceKind.BATCH_STORAGE)
    assert snap is not None and snap.verdict == SurfaceVerdict.HEALTHY


def test_ledger_increments_consecutive_failures(tmp_path: Path):
    led = SurfaceHealthLedger(path=tmp_path / "h.json")
    led.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
    led.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.UPSTREAM_DEGRADED)
    rec = led.verdict_for(SurfaceKind.DIRECT_STREAMING)
    assert rec.consecutive_failures == 2
    led.record(SurfaceKind.DIRECT_STREAMING, SurfaceVerdict.HEALTHY)
    assert led.verdict_for(SurfaceKind.DIRECT_STREAMING).consecutive_failures == 0


def test_ledger_corrupt_file_starts_empty(tmp_path: Path):
    p = tmp_path / "h.json"
    p.write_text("{ not json", encoding="utf-8")
    led = SurfaceHealthLedger(path=p)
    led.load()  # must NOT raise
    assert led.verdict_for(SurfaceKind.BATCH_STORAGE) is None


def test_ledger_schema_mismatch_starts_empty(tmp_path: Path):
    import json

    p = tmp_path / "h.json"
    p.write_text(json.dumps({"schema_version": 999, "records": []}))
    led = SurfaceHealthLedger(path=p)
    led.load()  # must NOT raise; wrong schema → start empty
    assert led.snapshot() == {}


def test_ledger_non_list_records_starts_empty(tmp_path: Path):
    import json

    from backend.core.ouroboros.governance.dw_surface_health import (
        LEDGER_SCHEMA_VERSION,
    )

    p = tmp_path / "h.json"
    p.write_text(
        json.dumps({"schema_version": LEDGER_SCHEMA_VERSION, "records": 42})
    )
    led = SurfaceHealthLedger(path=p)
    led.load()  # must NOT raise on a non-list 'records' value
    assert led.snapshot() == {}
