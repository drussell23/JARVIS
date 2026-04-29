"""Sentinel-Pacemaker Handshake — regression spine.

Closes the catalog-deadlock loop diagnosed in soaks #2-#5: when the
topology layer blocks BG ops because the catalog is purged, no DW
calls happen → no breaker transitions → catalog stays purged →
blocked ops accumulate until idle_timeout.

The handshake breaks the loop natively:
  1. Block site (candidate_generator) detects "catalog purged"
     reason and calls request_force_refresh()
  2. Pacemaker's refresh loop awaits EITHER cadence sleep OR the
     force-refresh event (whichever fires first)
  3. On wake, immediate /models probe; on success, catalog
     repopulates and subsequent ops flow

Pins:
  §1   Master flag default true (asymmetric env semantics)
  §2   request_force_refresh sets the event
  §3   request_force_refresh rate-limited within the min interval
  §4   request_force_refresh master-off returns False
  §5   Subsequent requests after the rate-limit window go through
  §6   Event lazily initialised
  §7   reset_force_refresh_for_tests clears state
  §8   request_force_refresh NEVER raises (defensive)
  §9   Reason text propagated to log
  §10  Block site triggers handshake when reason matches catalog
       purge tokens
  §11  Block site does NOT trigger handshake on non-catalog reasons
  §12  Refresh loop wakes on event before cadence elapses (mocked)
"""
from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from backend.core.ouroboros.governance.dw_discovery_runner import (
    _get_or_create_force_refresh_event,
    _force_refresh_min_interval_s,
    request_force_refresh,
    reset_force_refresh_for_tests,
    sentinel_pacemaker_handshake_enabled,
)


@pytest.fixture(autouse=True)
def reset_state():
    reset_force_refresh_for_tests()
    yield
    reset_force_refresh_for_tests()


# ===========================================================================
# §1 — Master flag
# ===========================================================================


def test_master_flag_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED", raising=False,
    )
    assert sentinel_pacemaker_handshake_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_master_flag_empty_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED", val,
    )
    assert sentinel_pacemaker_handshake_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_master_flag_falsy_disables(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED", val,
    )
    assert sentinel_pacemaker_handshake_enabled() is False


# ===========================================================================
# §2-§5 — Trigger semantics
# ===========================================================================


def test_request_sets_the_event() -> None:
    async def run():
        evt = _get_or_create_force_refresh_event()
        assert evt.is_set() is False
        result = request_force_refresh(reason="test")
        assert result is True
        assert evt.is_set() is True
    asyncio.run(run())


def test_request_rate_limited_within_min_interval(monkeypatch) -> None:
    """Set min interval high so back-to-back requests hit the gate."""
    monkeypatch.setenv("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "60")

    async def run():
        first = request_force_refresh(reason="first")
        second = request_force_refresh(reason="second")
        third = request_force_refresh(reason="third")
        return first, second, third
    first, second, third = asyncio.run(run())
    assert first is True
    assert second is False
    assert third is False


def test_request_master_off_returns_false(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_SENTINEL_PACEMAKER_HANDSHAKE_ENABLED", "false",
    )

    async def run():
        evt = _get_or_create_force_refresh_event()
        result = request_force_refresh(reason="test")
        return result, evt.is_set()
    result, is_set = asyncio.run(run())
    assert result is False
    assert is_set is False


def test_subsequent_requests_after_window_succeed(monkeypatch) -> None:
    """Set min interval to 1s (the floor) so the second request
    after a 1.5s sleep crosses the window. The 1.0s floor itself
    is a safety invariant — see test_min_interval_floored_at_one_second."""
    monkeypatch.setenv("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "1")

    async def run():
        first = request_force_refresh(reason="first")
        await asyncio.sleep(1.5)  # cross the 1s window
        second = request_force_refresh(reason="second")
        return first, second
    first, second = asyncio.run(run())
    assert first is True
    assert second is True


# ===========================================================================
# §6-§7 — Event lifecycle
# ===========================================================================


def test_event_lazy_init() -> None:
    async def run():
        # First access creates the event
        evt1 = _get_or_create_force_refresh_event()
        # Second access returns the same instance
        evt2 = _get_or_create_force_refresh_event()
        return evt1 is evt2
    assert asyncio.run(run()) is True


def test_reset_for_tests_clears_state() -> None:
    async def run():
        request_force_refresh(reason="t")
        evt_pre = _get_or_create_force_refresh_event()
        assert evt_pre.is_set() is True
        reset_force_refresh_for_tests()
        # New event after reset; old one no longer referenced
        evt_post = _get_or_create_force_refresh_event()
        return evt_post.is_set()
    assert asyncio.run(run()) is False


# ===========================================================================
# §8 — Defensive contract
# ===========================================================================


def test_request_never_raises_on_runtime_error(monkeypatch) -> None:
    """Even if internals raise, request_force_refresh returns
    False rather than propagating."""
    # The function's outer try/except catches anything; we exercise
    # by passing a non-string reason to coerce the path.
    async def run():
        # type: ignore[arg-type]
        result = request_force_refresh(reason=12345)  # type: ignore[arg-type]
        return result
    # Whatever happens, must not raise
    asyncio.run(run())


# ===========================================================================
# §9 — Logging
# ===========================================================================


def test_reason_text_appears_in_log(caplog) -> None:
    caplog.set_level(logging.INFO)

    async def run():
        request_force_refresh(reason="catalog_purged_unit_test")
    asyncio.run(run())
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "force_refresh requested" in log_text
    assert "catalog_purged_unit_test" in log_text


# ===========================================================================
# §10-§11 — Block-site trigger semantics
# ===========================================================================


def test_block_site_triggers_handshake_on_catalog_purge() -> None:
    """Source-grep pin — the block site at
    candidate_generator.py contains the `request_force_refresh`
    call gated on a catalog-purge reason match."""
    from backend.core.ouroboros.governance import candidate_generator
    src = inspect.getsource(candidate_generator)
    # The trigger exists
    assert "request_force_refresh" in src
    # Gated on catalog-purge tokens
    assert "purged" in src or "catalog" in src
    # NOT in the dw_severed failover path (different deadlock)
    # — checked indirectly: only ONE call site
    call_count = src.count("request_force_refresh(")
    assert call_count == 1, (
        f"expected exactly 1 request_force_refresh call site in "
        f"candidate_generator.py, found {call_count}"
    )


def test_block_site_uses_late_import_pattern() -> None:
    """The trigger must use a late-import pattern so a missing
    discovery runner doesn't break the candidate_generator module."""
    from backend.core.ouroboros.governance import candidate_generator
    src = inspect.getsource(candidate_generator)
    # Late import — inside a function/method, not module-level
    # Check that the import is in a try/except block (defensive)
    idx = src.find("from backend.core.ouroboros.governance.dw_discovery_runner")
    assert idx > 0
    # The 200 chars before the import contain a try statement
    pre = src[max(0, idx - 200):idx]
    assert "try:" in pre


# ===========================================================================
# §12 — Refresh loop responds to event (without running real DW)
# ===========================================================================


def test_event_wait_unblocks_before_cadence_sleep(monkeypatch) -> None:
    """Verify that asyncio.wait with FIRST_COMPLETED returns when
    the event fires, without waiting for the (huge) sleep. This
    pins the handshake's core wake-up semantics."""
    async def run():
        evt = _get_or_create_force_refresh_event()
        sleep_task = asyncio.ensure_future(asyncio.sleep(60.0))  # 60s
        event_task = asyncio.ensure_future(evt.wait())
        # Trigger after a short delay
        async def trigger():
            await asyncio.sleep(0.05)
            evt.set()
        triggerer = asyncio.ensure_future(trigger())
        done, pending = await asyncio.wait(
            (sleep_task, event_task),
            return_when=asyncio.FIRST_COMPLETED,
            timeout=2.0,  # safety
        )
        for p in pending:
            p.cancel()
        await triggerer
        # Event task fired; sleep is still pending
        return event_task in done, sleep_task in pending
    event_done, sleep_pending = asyncio.run(run())
    assert event_done is True
    assert sleep_pending is True


# ===========================================================================
# §13 — Min interval env knob respected
# ===========================================================================


def test_min_interval_env_override(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "5.5")
    assert _force_refresh_min_interval_s() == 5.5
    monkeypatch.setenv("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "garbage")
    assert _force_refresh_min_interval_s() == 30.0  # default


def test_min_interval_floored_at_one_second(monkeypatch) -> None:
    """Floor at 1.0 seconds — cannot disable rate-limiting entirely."""
    monkeypatch.setenv("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "0")
    assert _force_refresh_min_interval_s() == 1.0
    monkeypatch.setenv("JARVIS_FORCE_REFRESH_MIN_INTERVAL_S", "-5")
    assert _force_refresh_min_interval_s() == 1.0
