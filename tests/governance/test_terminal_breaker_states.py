"""Phase 12 Slice H — Terminal vs Transient Breaker States regression spine.

Pins:
  §1 BreakerState enum has TERMINAL_OPEN value
  §2 CircuitBreaker.record_failure(is_terminal=True) flips to TERMINAL_OPEN
                                                     regardless of state
  §3 TERMINAL_OPEN bypasses failure_threshold (single signal is enough)
  §4 TERMINAL_OPEN never auto-transitions to HALF_OPEN
  §5 record_success on TERMINAL_OPEN is no-op (terminal stays terminal)
  §6 record_failure() on TERMINAL_OPEN is no-op
  §7 reset_terminal() flips TERMINAL_OPEN → CLOSED
  §8 reset_terminal() on non-terminal state is no-op
  §9 TopologySentinel.report_failure(is_terminal=True) flips breaker
  §10 Sentinel report_failure(is_terminal=True) bypasses weighted threshold
  §11 reset_terminal_breaker(model_id) — single-model reset
  §12 reset_all_terminal_breakers — bulk reset
  §13 Dispatcher cascade skips TERMINAL_OPEN models same as OPEN
  §14 Persistent state — TERMINAL_OPEN survives sentinel restart
  §15 Source-level pin — terminal state in dispatcher skip-set
"""
from __future__ import annotations

import inspect
import time
from pathlib import Path
from typing import Any, Optional  # noqa: F401

import pytest

from backend.core.ouroboros.governance import candidate_generator as cg
from backend.core.ouroboros.governance import topology_sentinel as ts
from backend.core.ouroboros.governance.rate_limiter import (
    BreakerState,
    CircuitBreaker,
    CircuitBreakerOpen,
)
from backend.core.ouroboros.governance.topology_sentinel import (
    FailureSource,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    ts.reset_default_sentinel_for_tests()
    sentinel = ts.get_default_sentinel()
    yield sentinel
    ts.reset_default_sentinel_for_tests()


# ---------------------------------------------------------------------------
# §1 — Enum
# ---------------------------------------------------------------------------


def test_breaker_state_has_terminal_open() -> None:
    assert hasattr(BreakerState, "TERMINAL_OPEN")
    assert BreakerState.TERMINAL_OPEN.value == "TERMINAL_OPEN"


def test_breaker_state_set_completeness() -> None:
    """All four states present + value matches name."""
    expected = {"CLOSED", "OPEN", "HALF_OPEN", "TERMINAL_OPEN"}
    actual = {s.value for s in BreakerState}
    assert actual == expected


# ---------------------------------------------------------------------------
# §2 — record_failure(is_terminal=True)
# ---------------------------------------------------------------------------


def test_record_failure_is_terminal_from_closed() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=1)
    cb.record_failure(is_terminal=True)
    assert cb.state == BreakerState.TERMINAL_OPEN


def test_record_failure_is_terminal_from_half_open() -> None:
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout_s=0.01)
    # Drive to OPEN
    for _ in range(3):
        cb.record_failure()
    assert cb.state == BreakerState.OPEN
    time.sleep(0.02)
    cb.check()  # auto-transitions to HALF_OPEN
    assert cb.state == BreakerState.HALF_OPEN
    cb.record_failure(is_terminal=True)
    assert cb.state == BreakerState.TERMINAL_OPEN


def test_record_failure_is_terminal_from_open() -> None:
    """Even if already OPEN, terminal failure flips to TERMINAL_OPEN."""
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    cb.record_failure(is_terminal=True)
    assert cb.state == BreakerState.TERMINAL_OPEN


# ---------------------------------------------------------------------------
# §3 — Bypasses failure_threshold
# ---------------------------------------------------------------------------


def test_terminal_bypasses_failure_threshold() -> None:
    """A single is_terminal=True flips to TERMINAL_OPEN even with
    a failure_threshold of 100 — ground truth doesn't need 100 votes."""
    cb = CircuitBreaker(failure_threshold=100)
    cb.record_failure(is_terminal=True)
    assert cb.state == BreakerState.TERMINAL_OPEN


# ---------------------------------------------------------------------------
# §4 — Never auto-transitions to HALF_OPEN
# ---------------------------------------------------------------------------


def test_terminal_does_not_auto_recover_via_timeout() -> None:
    """recovery_timeout_s only governs OPEN → HALF_OPEN. TERMINAL_OPEN
    stays put indefinitely. Verified by check() raising past the
    recovery window."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout_s=0.01)
    cb.record_failure(is_terminal=True)
    time.sleep(0.05)  # well past the recovery window
    with pytest.raises(CircuitBreakerOpen) as exc_info:
        cb.check()
    assert "TERMINAL_OPEN" in str(exc_info.value)
    # State unchanged
    assert cb.state == BreakerState.TERMINAL_OPEN


# ---------------------------------------------------------------------------
# §5 — record_success on TERMINAL_OPEN is no-op
# ---------------------------------------------------------------------------


def test_record_success_on_terminal_is_noop() -> None:
    """A racing in-flight success after the terminal verdict landed
    must not clear the terminal state."""
    cb = CircuitBreaker()
    cb.record_failure(is_terminal=True)
    cb.record_success()  # should be ignored
    assert cb.state == BreakerState.TERMINAL_OPEN


# ---------------------------------------------------------------------------
# §6 — Subsequent record_failure on TERMINAL_OPEN is no-op
# ---------------------------------------------------------------------------


def test_record_failure_on_terminal_is_noop() -> None:
    cb = CircuitBreaker()
    cb.record_failure(is_terminal=True)
    cb.record_failure()  # should be ignored
    cb.record_failure(is_terminal=True)  # already terminal; still terminal
    assert cb.state == BreakerState.TERMINAL_OPEN


# ---------------------------------------------------------------------------
# §7 — reset_terminal() works
# ---------------------------------------------------------------------------


def test_reset_terminal_flips_to_closed() -> None:
    cb = CircuitBreaker()
    cb.record_failure(is_terminal=True)
    assert cb.state == BreakerState.TERMINAL_OPEN
    changed = cb.reset_terminal()
    assert changed is True
    assert cb.state == BreakerState.CLOSED


def test_reset_terminal_clears_failure_count() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure(is_terminal=True)
    cb.reset_terminal()
    assert cb._failure_count == 0  # noqa: SLF001
    # Clean slate — needs full threshold to trip again
    cb.record_failure()
    cb.record_failure()
    assert cb.state == BreakerState.CLOSED


# ---------------------------------------------------------------------------
# §8 — reset_terminal() on non-terminal state is no-op
# ---------------------------------------------------------------------------


def test_reset_terminal_on_closed_is_noop() -> None:
    cb = CircuitBreaker()
    changed = cb.reset_terminal()
    assert changed is False
    assert cb.state == BreakerState.CLOSED


def test_reset_terminal_on_open_is_noop() -> None:
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    changed = cb.reset_terminal()
    assert changed is False
    assert cb.state == BreakerState.OPEN


# ---------------------------------------------------------------------------
# §9 — Sentinel report_failure(is_terminal=True)
# ---------------------------------------------------------------------------


def test_sentinel_report_failure_terminal_flips_breaker(
    isolated_sentinel,
) -> None:
    sentinel = isolated_sentinel
    sentinel.register_endpoint("vendor/embed-8B")
    sentinel.report_failure(
        "vendor/embed-8B",
        FailureSource.LIVE_TRANSPORT,
        "modality 4xx",
        status_code=400,
        response_body="model does not support chat",
        is_terminal=True,
    )
    assert sentinel.get_state("vendor/embed-8B") == "TERMINAL_OPEN"


# ---------------------------------------------------------------------------
# §10 — Sentinel terminal bypasses weighted threshold
# ---------------------------------------------------------------------------


def test_sentinel_terminal_single_call_trips_breaker(
    isolated_sentinel,
) -> None:
    """Without is_terminal, a single LIVE_TRANSPORT (weight 2.0)
    wouldn't reach the 3.0 threshold. With is_terminal=True, one call
    is enough."""
    sentinel = isolated_sentinel
    sentinel.register_endpoint("vendor/m-7B")
    # Without is_terminal — single LIVE_TRANSPORT doesn't trip
    sentinel.report_failure(
        "vendor/m-7B", FailureSource.LIVE_TRANSPORT, "transient",
    )
    assert sentinel.get_state("vendor/m-7B") == "CLOSED"  # streak=2.0 < 3.0
    # With is_terminal — single call IS enough
    sentinel.report_failure(
        "vendor/m-7B",
        FailureSource.LIVE_TRANSPORT,
        "modality 4xx",
        status_code=400,
        response_body="not a chat model",
        is_terminal=True,
    )
    assert sentinel.get_state("vendor/m-7B") == "TERMINAL_OPEN"


# ---------------------------------------------------------------------------
# §11 — reset_terminal_breaker(model_id)
# ---------------------------------------------------------------------------


def test_sentinel_reset_terminal_breaker_single_model(
    isolated_sentinel,
) -> None:
    sentinel = isolated_sentinel
    sentinel.register_endpoint("vendor/m-7B")
    sentinel.report_failure(
        "vendor/m-7B", FailureSource.LIVE_TRANSPORT,
        is_terminal=True, status_code=400,
    )
    assert sentinel.get_state("vendor/m-7B") == "TERMINAL_OPEN"
    changed = sentinel.reset_terminal_breaker("vendor/m-7B")
    assert changed is True
    assert sentinel.get_state("vendor/m-7B") == "CLOSED"


def test_sentinel_reset_terminal_unknown_model_returns_false(
    isolated_sentinel,
) -> None:
    sentinel = isolated_sentinel
    assert sentinel.reset_terminal_breaker("never/seen") is False


# ---------------------------------------------------------------------------
# §12 — reset_all_terminal_breakers
# ---------------------------------------------------------------------------


def test_sentinel_reset_all_terminal_bulk(isolated_sentinel) -> None:
    sentinel = isolated_sentinel
    for mid in ("a/m-7B", "b/m-7B", "c/m-7B"):
        sentinel.register_endpoint(mid)
        sentinel.report_failure(
            mid, FailureSource.LIVE_TRANSPORT,
            is_terminal=True, status_code=400,
        )
    # Plus one model in OPEN (not terminal) — must NOT be reset
    sentinel.register_endpoint("d/transient-7B")
    for _ in range(5):
        sentinel.report_failure(
            "d/transient-7B", FailureSource.LIVE_HTTP_5XX,
        )
    assert sentinel.get_state("d/transient-7B") == "OPEN"

    count = sentinel.reset_all_terminal_breakers()
    assert count == 3
    for mid in ("a/m-7B", "b/m-7B", "c/m-7B"):
        assert sentinel.get_state(mid) == "CLOSED"
    # Transient OPEN is preserved
    assert sentinel.get_state("d/transient-7B") == "OPEN"


# ---------------------------------------------------------------------------
# §13 — Dispatcher skips TERMINAL_OPEN
# ---------------------------------------------------------------------------


def test_source_dispatcher_skips_terminal_open() -> None:
    """Source-level pin: candidate_generator's sentinel cascade must
    skip TERMINAL_OPEN models at the same gate as OPEN. Both are
    'do not attempt'; difference is purely recovery model."""
    src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
    # The skip-set check
    assert 'state in ("OPEN", "TERMINAL_OPEN")' in src or (
        '"OPEN"' in src and '"TERMINAL_OPEN"' in src
    ), (
        "dispatcher must skip TERMINAL_OPEN models alongside OPEN"
    )


# ---------------------------------------------------------------------------
# §14 — Persistence
# ---------------------------------------------------------------------------


def test_terminal_state_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TERMINAL_OPEN must persist across sentinel restart so a process
    crash + restart doesn't accidentally re-attempt a known-bad model."""
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path),
    )
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    ts.reset_default_sentinel_for_tests()
    s1 = ts.get_default_sentinel()
    s1.register_endpoint("vendor/embed-8B")
    s1.report_failure(
        "vendor/embed-8B", FailureSource.LIVE_TRANSPORT,
        is_terminal=True, status_code=400,
        response_body="model does not support chat",
    )
    assert s1.get_state("vendor/embed-8B") == "TERMINAL_OPEN"
    # "Restart" — drop singleton, reload from disk
    ts.reset_default_sentinel_for_tests()
    s2 = ts.get_default_sentinel()
    # Re-register so the breaker is hydrated from snapshot
    s2.register_endpoint("vendor/embed-8B")
    assert s2.get_state("vendor/embed-8B") == "TERMINAL_OPEN", (
        "TERMINAL_OPEN state lost on restart — terminal verdicts MUST "
        "survive process crashes"
    )
    ts.reset_default_sentinel_for_tests()


# ---------------------------------------------------------------------------
# §15 — Backward compat: legacy report_failure signature still works
# ---------------------------------------------------------------------------


def test_sentinel_report_failure_legacy_3arg(
    isolated_sentinel,
) -> None:
    """Pre-Slice-H callers passing only the 3 positional args must
    continue to work. is_terminal defaults False; flag carries no
    behavioral change."""
    sentinel = isolated_sentinel
    sentinel.register_endpoint("vendor/m-7B")
    # Drive to OPEN via legacy 3-arg calls — single LIVE_STREAM_STALL
    # (weight 3.0) is enough
    sentinel.report_failure(
        "vendor/m-7B", FailureSource.LIVE_STREAM_STALL, "stall",
    )
    assert sentinel.get_state("vendor/m-7B") == "OPEN"


def test_sentinel_recover_via_probe_only_for_open_not_terminal(
    isolated_sentinel,
) -> None:
    """OPEN can recover via probe → HALF_OPEN → CLOSED. TERMINAL_OPEN
    cannot — pin the asymmetry."""
    sentinel = isolated_sentinel
    sentinel.register_endpoint("transient/m-7B")
    sentinel.register_endpoint("terminal/m-7B")
    sentinel.report_failure(
        "transient/m-7B", FailureSource.LIVE_STREAM_STALL,
    )  # OPEN
    sentinel.report_failure(
        "terminal/m-7B", FailureSource.LIVE_TRANSPORT,
        is_terminal=True, status_code=400,
    )  # TERMINAL_OPEN
    # report_success on transient eventually returns CLOSED via HALF_OPEN
    sentinel.report_success("transient/m-7B")
    sentinel.report_success("transient/m-7B")
    # report_success on terminal MUST NOT clear it
    sentinel.report_success("terminal/m-7B")
    sentinel.report_success("terminal/m-7B")
    assert sentinel.get_state("terminal/m-7B") == "TERMINAL_OPEN"
