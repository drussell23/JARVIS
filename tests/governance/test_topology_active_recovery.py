"""Slice 3a — TopologySentinel active-recovery regression spine.

The catalog ``GET /models`` IS the lightweight reachability probe the
architectural directive calls for. After a successful fetch,
``apply_health_probe_result(success=True)`` lifts transient blocks
without burning a separate probe. The discovery runner is the only
caller; cadence is the existing ``JARVIS_DW_CATALOG_REFRESH_S`` (30 min
default — operator-tunable, not hardcoded).

Pins:
  §1   topology_active_recovery_enabled flag — default true; case-tolerant
  §2   apply_health_probe_result(success=False) is a no-op
  §3   apply_health_probe_result(success=True) when sentinel master off → 0
  §4   apply_health_probe_result(success=True) when active-recovery off → 0
  §5   TERMINAL_OPEN breakers reset on probe success
  §6   HALF_OPEN breakers transition to CLOSED on probe success
  §7   OPEN breakers are LEFT ALONE (rate_limiter time-based recovery owns it)
  §8   CLOSED breakers are unaffected (no spurious transitions)
  §9   Mixed-state recovery: counts add up (TERMINAL + HALF_OPEN)
  §10  list_blocked_endpoints returns sorted tuple of OPEN/TERMINAL_OPEN
  §11  list_blocked_endpoints returns () when sentinel master off
  §12  list_blocked_endpoints excludes HALF_OPEN + CLOSED
  §13  apply_health_probe_result NEVER raises — defensive on internal errors
  §14  Public API exposed from module
  §15  Source-level pin: function reads the env var
  §16  Propagated-vars contract includes the new flag
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import topology_sentinel as ts


# ---------------------------------------------------------------------------
# Fixture: fresh sentinel + sentinel master flag on
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_sentinel(monkeypatch, tmp_path):
    """Construct a sentinel with master flag on + a tmp state dir."""
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path / "topo"),
    )
    sentinel = ts.TopologySentinel()
    yield sentinel


def _set_breaker_state(sentinel, model_id, state):
    """Force a breaker into a specific state for testing.

    Uses the same pattern force_severed uses — register endpoint then
    mutate state directly. Mirrors the prod failure paths."""
    sentinel.register_endpoint(model_id)
    breaker = sentinel._breakers[model_id]  # noqa: SLF001
    BreakerState = sentinel._BreakerStateCls()
    breaker._state = getattr(BreakerState, state)  # noqa: SLF001
    snap = sentinel._snapshots[model_id]  # noqa: SLF001
    snap.state = state


# ===========================================================================
# §1 — Master flag
# ===========================================================================


def test_active_recovery_enabled_default_true(monkeypatch) -> None:
    monkeypatch.delenv(
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED", raising=False,
    )
    assert ts.topology_active_recovery_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  "])
def test_active_recovery_empty_reads_as_default_true(
    monkeypatch, val,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED", val,
    )
    assert ts.topology_active_recovery_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_active_recovery_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED", val,
    )
    assert ts.topology_active_recovery_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_active_recovery_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED", val,
    )
    assert ts.topology_active_recovery_enabled() is False


# ===========================================================================
# §2-§4 — Disabled paths
# ===========================================================================


def test_probe_failure_is_noop(fresh_sentinel) -> None:
    """A probe FAILURE must not penalise breakers — that would double-
    count failures (model-layer report_failure already covered)."""
    _set_breaker_state(fresh_sentinel, "m1", "TERMINAL_OPEN")
    n = fresh_sentinel.apply_health_probe_result(success=False)
    assert n == 0
    assert fresh_sentinel.get_state("m1") == "TERMINAL_OPEN"


def test_recovery_noop_when_sentinel_master_off(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "false")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path / "topo"),
    )
    sentinel = ts.TopologySentinel()
    _set_breaker_state(sentinel, "m1", "TERMINAL_OPEN")
    n = sentinel.apply_health_probe_result(success=True)
    assert n == 0


def test_recovery_noop_when_active_recovery_off(
    fresh_sentinel, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED", "false",
    )
    _set_breaker_state(fresh_sentinel, "m1", "TERMINAL_OPEN")
    n = fresh_sentinel.apply_health_probe_result(success=True)
    assert n == 0
    assert fresh_sentinel.get_state("m1") == "TERMINAL_OPEN"


# ===========================================================================
# §5-§9 — Recovery semantics
# ===========================================================================


def test_terminal_open_resets_on_probe_success(fresh_sentinel) -> None:
    _set_breaker_state(fresh_sentinel, "m1", "TERMINAL_OPEN")
    assert fresh_sentinel.get_state("m1") == "TERMINAL_OPEN"
    n = fresh_sentinel.apply_health_probe_result(success=True)
    assert n >= 1
    assert fresh_sentinel.get_state("m1") == "CLOSED"


def test_half_open_transitions_to_closed_on_probe_success(
    fresh_sentinel,
) -> None:
    _set_breaker_state(fresh_sentinel, "m1", "HALF_OPEN")
    assert fresh_sentinel.get_state("m1") == "HALF_OPEN"
    n = fresh_sentinel.apply_health_probe_result(success=True)
    assert n >= 1
    assert fresh_sentinel.get_state("m1") == "CLOSED"


def test_open_left_alone_on_probe_success(fresh_sentinel) -> None:
    """OPEN recovery is owned by rate_limiter's time-based 30s
    auto-transition. Forcing OPEN→CLOSED here would race the cooldown
    and could mask a real fault — the directive's "structural repair,
    not bypasses" rule applies."""
    _set_breaker_state(fresh_sentinel, "m1", "OPEN")
    n = fresh_sentinel.apply_health_probe_result(success=True)
    # State preserved — recovery_timeout_s owns this transition
    assert fresh_sentinel.get_state("m1") == "OPEN"
    assert n == 0


def test_closed_unaffected_on_probe_success(fresh_sentinel) -> None:
    _set_breaker_state(fresh_sentinel, "m1", "CLOSED")
    n = fresh_sentinel.apply_health_probe_result(success=True)
    assert fresh_sentinel.get_state("m1") == "CLOSED"
    assert n == 0


def test_mixed_state_recovery_counts_correctly(fresh_sentinel) -> None:
    _set_breaker_state(fresh_sentinel, "term1", "TERMINAL_OPEN")
    _set_breaker_state(fresh_sentinel, "term2", "TERMINAL_OPEN")
    _set_breaker_state(fresh_sentinel, "half1", "HALF_OPEN")
    _set_breaker_state(fresh_sentinel, "open1", "OPEN")
    _set_breaker_state(fresh_sentinel, "closed1", "CLOSED")
    n = fresh_sentinel.apply_health_probe_result(success=True)
    # 2 terminal resets + 1 half_open transition = 3
    assert n == 3
    assert fresh_sentinel.get_state("term1") == "CLOSED"
    assert fresh_sentinel.get_state("term2") == "CLOSED"
    assert fresh_sentinel.get_state("half1") == "CLOSED"
    assert fresh_sentinel.get_state("open1") == "OPEN"  # untouched
    assert fresh_sentinel.get_state("closed1") == "CLOSED"  # untouched


# ===========================================================================
# §10-§12 — list_blocked_endpoints contract
# ===========================================================================


def test_list_blocked_endpoints_returns_sorted_tuple(fresh_sentinel) -> None:
    _set_breaker_state(fresh_sentinel, "z_open", "OPEN")
    _set_breaker_state(fresh_sentinel, "a_terminal", "TERMINAL_OPEN")
    _set_breaker_state(fresh_sentinel, "m_closed", "CLOSED")
    _set_breaker_state(fresh_sentinel, "k_half", "HALF_OPEN")
    blocked = fresh_sentinel.list_blocked_endpoints()
    # Sorted, only OPEN/TERMINAL_OPEN
    assert blocked == ("a_terminal", "z_open")


def test_list_blocked_endpoints_returns_empty_when_master_off(
    monkeypatch, tmp_path,
) -> None:
    monkeypatch.setenv("JARVIS_TOPOLOGY_SENTINEL_ENABLED", "false")
    monkeypatch.setenv(
        "JARVIS_TOPOLOGY_SENTINEL_STATE_DIR", str(tmp_path / "topo"),
    )
    sentinel = ts.TopologySentinel()
    _set_breaker_state(sentinel, "m1", "OPEN")
    # Master flag off → list_blocked returns () (legacy yaml authoritative)
    assert sentinel.list_blocked_endpoints() == ()


def test_list_blocked_endpoints_excludes_half_open_and_closed(
    fresh_sentinel,
) -> None:
    _set_breaker_state(fresh_sentinel, "open1", "OPEN")
    _set_breaker_state(fresh_sentinel, "half1", "HALF_OPEN")
    _set_breaker_state(fresh_sentinel, "closed1", "CLOSED")
    blocked = fresh_sentinel.list_blocked_endpoints()
    assert "open1" in blocked
    assert "half1" not in blocked
    assert "closed1" not in blocked


# ===========================================================================
# §13 — Defensive — never raises
# ===========================================================================


def test_apply_health_probe_result_never_raises(fresh_sentinel) -> None:
    """Even if internal state is corrupt, the method returns 0."""
    # Empty sentinel — no breakers registered
    n = fresh_sentinel.apply_health_probe_result(success=True)
    assert n == 0


def test_list_blocked_endpoints_never_raises(fresh_sentinel) -> None:
    blocked = fresh_sentinel.list_blocked_endpoints()
    assert isinstance(blocked, tuple)


# ===========================================================================
# §14-§16 — Surface contracts
# ===========================================================================


def test_public_api_exposed() -> None:
    assert hasattr(ts, "topology_active_recovery_enabled")
    assert callable(ts.topology_active_recovery_enabled)


def test_source_reads_env_var() -> None:
    import inspect
    src = inspect.getsource(ts.topology_active_recovery_enabled)
    assert "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED" in src


def test_propagated_vars_includes_active_recovery_flag() -> None:
    """The harness env-propagation contract must include the new flag
    so subprocess soaks see operator-set values."""
    assert (
        "JARVIS_TOPOLOGY_ACTIVE_RECOVERY_ENABLED"
        in ts.sentinel_propagated_vars()
    )
