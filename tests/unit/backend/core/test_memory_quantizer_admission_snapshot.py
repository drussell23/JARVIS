import pytest

from backend.core.memory_quantizer import (
    MemoryMetrics,
    MemoryPressure,
    MemoryQuantizer,
    MemoryTier,
)


def _metrics(*, available_gb: float, reserved_gb: float = 0.0) -> MemoryMetrics:
    return MemoryMetrics(
        timestamp=1.0,
        process_memory_gb=1.5,
        system_memory_gb=32.0,
        system_memory_percent=62.0,
        system_memory_available_gb=available_gb,
        tier=MemoryTier.OPTIMAL,
        pressure=MemoryPressure.NORMAL,
        metadata={"reserved_gb": reserved_gb},
    )


def test_build_admission_snapshot_applies_reservations():
    quantizer = MemoryQuantizer(config={})
    snapshot = quantizer.build_admission_snapshot(
        _metrics(available_gb=8.0, reserved_gb=2.5),
        include_reservations=True,
        source="unit_test",
    )

    assert snapshot["raw_available_gb"] == pytest.approx(8.0)
    assert snapshot["reserved_gb"] == pytest.approx(2.5)
    assert snapshot["available_gb"] == pytest.approx(5.5)
    assert snapshot["source"] == "unit_test"


def test_get_admission_snapshot_uses_cached_metrics_without_refresh(monkeypatch):
    quantizer = MemoryQuantizer(config={})
    quantizer.current_metrics = _metrics(available_gb=6.0, reserved_gb=0.5)

    monkeypatch.setattr(
        quantizer,
        "get_current_metrics",
        lambda: pytest.fail("refresh should not run when current_metrics is cached"),
    )

    snapshot = quantizer.get_admission_snapshot(refresh=False, include_reservations=True)
    assert snapshot["available_gb"] == pytest.approx(5.5)
    assert snapshot["source"] == "memory_quantizer_sync"


def test_get_admission_snapshot_can_skip_probe_when_metrics_missing(monkeypatch):
    quantizer = MemoryQuantizer(config={})
    monkeypatch.setattr(
        quantizer,
        "get_current_metrics",
        lambda: pytest.fail("probe should be skipped when allow_probe_if_missing=False"),
    )

    snapshot = quantizer.get_admission_snapshot(
        refresh=False,
        include_reservations=True,
        allow_probe_if_missing=False,
    )
    assert snapshot["available_gb"] is None
    assert snapshot["source"] == "memory_quantizer_sync"


@pytest.mark.asyncio
async def test_get_admission_snapshot_async_refreshes_metrics(monkeypatch):
    quantizer = MemoryQuantizer(config={})
    refreshed = _metrics(available_gb=7.0, reserved_gb=1.0)

    async def _fake_refresh():
        return refreshed

    monkeypatch.setattr(quantizer, "get_current_metrics_async", _fake_refresh)
    snapshot = await quantizer.get_admission_snapshot_async(
        refresh=True,
        include_reservations=True,
    )

    assert snapshot["available_gb"] == pytest.approx(6.0)
    assert snapshot["tier"] == MemoryTier.OPTIMAL.value
    assert snapshot["pressure"] == MemoryPressure.NORMAL.value
    assert snapshot["source"] == "memory_quantizer_async"
