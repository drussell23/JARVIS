"""
Tests for Proactive Proxy Detector with Sub-100ms Fast-Fail.
"""

import pytest
import asyncio
import time
from backend.core.connection.proactive_proxy_detector import (
    ProactiveProxyDetector,
    ProxyDetectorConfig,
    ProxyStatus,
)


@pytest.mark.asyncio
async def test_detection_completes_in_under_100ms():
    """Proxy detection should complete in under 100ms."""
    detector = ProactiveProxyDetector()

    start = time.perf_counter()
    status, _ = await detector.detect()
    elapsed_ms = (time.perf_counter() - start) * 1000

    # Should complete quickly even if proxy is down
    assert elapsed_ms < 100, f"Detection took {elapsed_ms:.1f}ms, expected <100ms"


@pytest.mark.asyncio
async def test_cached_status_is_fast():
    """Cached status should return instantly."""
    detector = ProactiveProxyDetector()

    # First detection
    await detector.detect()

    # Second detection should be cached
    start = time.perf_counter()
    status, msg = await detector.detect()
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 5, f"Cached detection took {elapsed_ms:.1f}ms, expected <5ms"
    assert msg == "Cached status"


@pytest.mark.asyncio
async def test_force_bypasses_cache():
    """force=True should bypass cache."""
    detector = ProactiveProxyDetector()

    # First detection
    await detector.detect()

    # Forced detection should not say "Cached status"
    status, msg = await detector.detect(force=True)
    assert msg != "Cached status"


@pytest.mark.asyncio
async def test_invalidate_cache_works():
    """invalidate_cache should clear cached status."""
    detector = ProactiveProxyDetector()

    # First detection
    await detector.detect()

    # Invalidate
    detector.invalidate_cache()

    # Should not be cached
    status, msg = await detector.detect()
    assert msg != "Cached status"


@pytest.mark.asyncio
async def test_returns_valid_status():
    """Should return a valid ProxyStatus enum value."""
    detector = ProactiveProxyDetector()

    status, msg = await detector.detect()

    assert isinstance(status, ProxyStatus)
    assert status in (ProxyStatus.AVAILABLE, ProxyStatus.UNAVAILABLE, ProxyStatus.UNKNOWN)


@pytest.mark.asyncio
async def test_config_from_environment():
    """Config should load from environment."""
    import os

    # Set environment variable
    original = os.environ.get('CLOUD_SQL_PROXY_PORT')
    os.environ['CLOUD_SQL_PROXY_PORT'] = '5555'

    try:
        config = ProxyDetectorConfig()
        assert config.proxy_port == 5555
    finally:
        if original:
            os.environ['CLOUD_SQL_PROXY_PORT'] = original
        else:
            del os.environ['CLOUD_SQL_PROXY_PORT']


@pytest.mark.asyncio
async def test_concurrent_detection_is_safe():
    """Multiple concurrent detections should be safe."""
    detector = ProactiveProxyDetector()

    # Launch 10 concurrent detections
    results = await asyncio.gather(*[
        detector.detect()
        for _ in range(10)
    ])

    # All should return valid results
    for status, msg in results:
        assert isinstance(status, ProxyStatus)
        assert isinstance(msg, str)
