"""ClaudeCircuitBreaker — cross-cutting health gate at the provider boundary.

Pins:
  * 3-state FSM: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.
  * ``record_transport_exhaustion`` increments the counter; trips the
    breaker once it crosses the threshold.
  * Trip is one-shot: once OPEN, additional exhaustions don't
    re-trip until HALF_OPEN reverts.
  * ``record_success`` resets the counter and re-closes a HALF_OPEN
    breaker.
  * ``record_non_transport_failure`` resets the counter without
    closing the breaker (content failures don't excuse infra-level
    sustained outages, but they DO break the consecutive-transport
    streak).
  * ``should_allow_request``:
      - CLOSED: always True
      - OPEN before recovery window: False
      - OPEN after recovery window: transitions to HALF_OPEN, returns True
        once
      - HALF_OPEN after first probe acquired: False (only one probe)
  * Probe failure (HALF_OPEN -> exhaustion) re-OPENs the breaker.
  * Probe success (HALF_OPEN -> success) closes the breaker.
  * ``is_transport_class_exception`` walks __cause__/__context__.
  * Master flag default-true (graduated semantics).

Authority Invariant
-------------------
Tests import only the breaker module + stdlib. No orchestrator /
phase_runners / iron_gate / providers imports.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — State machine basics
# -----------------------------------------------------------------------


def test_initial_state_is_closed():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker()
    assert b.state is CircuitState.CLOSED
    assert b.consecutive_transport_failures == 0
    assert b.tripped_at_monotonic is None


def test_record_exhaustion_below_threshold_stays_closed():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(failure_threshold=3)
    b.record_transport_exhaustion("ConnectTimeout")
    b.record_transport_exhaustion("ReadError")
    assert b.state is CircuitState.CLOSED
    assert b.consecutive_transport_failures == 2


def test_record_exhaustion_at_threshold_trips_open():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(failure_threshold=3)
    for i in range(3):
        b.record_transport_exhaustion(f"err-{i}")
    assert b.state is CircuitState.OPEN
    assert b.tripped_at_monotonic is not None
    assert b.total_trips == 1


def test_record_success_closes_half_open_breaker(monkeypatch):
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(failure_threshold=2, recovery_window_s=0.0)
    b.record_transport_exhaustion("a")
    b.record_transport_exhaustion("b")
    assert b.state is CircuitState.OPEN

    # Recovery window 0.0 — next should_allow_request transitions to HALF_OPEN
    assert b.should_allow_request() is True
    assert b.state is CircuitState.HALF_OPEN

    # Probe success — back to CLOSED
    b.record_success()
    assert b.state is CircuitState.CLOSED
    assert b.consecutive_transport_failures == 0


def test_probe_failure_reopens_breaker(monkeypatch):
    """HALF_OPEN exhaustion must transition back to OPEN with a fresh
    trip clock — NOT continue accumulating consecutive_transport_failures."""
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(failure_threshold=3, recovery_window_s=0.0)
    for _ in range(3):
        b.record_transport_exhaustion("e")
    assert b.state is CircuitState.OPEN
    initial_trips = b.total_trips

    # Recover into HALF_OPEN
    assert b.should_allow_request() is True
    assert b.state is CircuitState.HALF_OPEN

    # Probe fails
    b.record_transport_exhaustion("probe_fail")
    assert b.state is CircuitState.OPEN
    # Total trips incremented for the re-trip
    assert b.total_trips == initial_trips + 1


def test_recovery_window_blocks_probe_until_elapsed(monkeypatch):
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(
        failure_threshold=2, recovery_window_s=900.0,
    )
    b.record_transport_exhaustion("a")
    b.record_transport_exhaustion("b")
    assert b.state is CircuitState.OPEN
    # No probe allowed yet
    assert b.should_allow_request() is False
    assert b.state is CircuitState.OPEN

    # Advance time past the window
    real_t = b._tripped_at_monotonic + 901.0
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.claude_circuit_breaker.time.monotonic",
        lambda: real_t,
    )
    assert b.should_allow_request() is True
    assert b.state is CircuitState.HALF_OPEN


def test_half_open_only_one_probe(monkeypatch):
    """Once HALF_OPEN, only the first should_allow_request returns True;
    subsequent callers see False until the probe resolves."""
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(failure_threshold=2, recovery_window_s=0.0)
    b.record_transport_exhaustion("a")
    b.record_transport_exhaustion("b")
    assert b.should_allow_request() is True  # transition + first probe
    assert b.state is CircuitState.HALF_OPEN
    # Subsequent caller — no second probe
    assert b.should_allow_request() is False
    assert b.state is CircuitState.HALF_OPEN


# -----------------------------------------------------------------------
# § B — Non-transport failure does NOT trip the breaker
# -----------------------------------------------------------------------


def test_non_transport_failure_resets_streak_only():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker, CircuitState,
    )
    b = ClaudeCircuitBreaker(failure_threshold=3)
    b.record_transport_exhaustion("a")
    b.record_transport_exhaustion("b")
    # A content failure breaks the transport streak but does NOT trip
    b.record_non_transport_failure()
    assert b.state is CircuitState.CLOSED
    assert b.consecutive_transport_failures == 0
    # Now we need 3 fresh consecutive transport exhaustions to trip
    for _ in range(2):
        b.record_transport_exhaustion("c")
    assert b.state is CircuitState.CLOSED
    b.record_transport_exhaustion("d")
    assert b.state is CircuitState.OPEN


# -----------------------------------------------------------------------
# § C — Transport-class detection
# -----------------------------------------------------------------------


def test_is_transport_class_direct():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        is_transport_class_exception,
    )
    class ConnectTimeout(Exception):
        pass
    class ReadError(Exception):
        pass
    class SSLWantReadError(Exception):
        pass
    assert is_transport_class_exception(ConnectTimeout()) is True
    assert is_transport_class_exception(ReadError()) is True
    assert is_transport_class_exception(SSLWantReadError()) is True
    assert is_transport_class_exception(RuntimeError()) is False


def test_is_transport_class_walks_chain():
    """Anthropic SDK wraps httpx exceptions as APIConnectionError →
    ConnectError → ConnectTimeout. The breaker must walk the chain."""
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        is_transport_class_exception,
    )
    class ConnectTimeout(Exception):
        pass
    class APIConnectionError(Exception):
        pass

    inner = ConnectTimeout("inner")
    outer = APIConnectionError("outer")
    outer.__cause__ = inner
    # APIConnectionError IS in the transport set — direct hit
    assert is_transport_class_exception(outer) is True

    # Wrap in something not in the set; chain must still resolve
    class WrapperError(Exception):
        pass
    wrapper = WrapperError("not in set")
    wrapper.__cause__ = outer
    assert is_transport_class_exception(wrapper) is True


# -----------------------------------------------------------------------
# § D — Singleton + reset
# -----------------------------------------------------------------------


def test_singleton_returns_same_instance():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        get_claude_circuit_breaker, reset_singleton_for_tests,
    )
    reset_singleton_for_tests()
    a = get_claude_circuit_breaker()
    b = get_claude_circuit_breaker()
    assert a is b


def test_reset_singleton_for_tests():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        get_claude_circuit_breaker, reset_singleton_for_tests,
    )
    reset_singleton_for_tests()
    a = get_claude_circuit_breaker()
    a.record_transport_exhaustion("x")
    reset_singleton_for_tests()
    b = get_claude_circuit_breaker()
    assert a is not b
    assert b.consecutive_transport_failures == 0


# -----------------------------------------------------------------------
# § E — Master flag (graduated default-true)
# -----------------------------------------------------------------------


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CLAUDE_CIRCUIT_BREAKER_ENABLED", raising=False,
    )
    import backend.core.ouroboros.governance.claude_circuit_breaker as m
    importlib.reload(m)
    assert m.is_enabled() is True


def test_master_flag_explicit_falsy_hot_reverts(monkeypatch):
    import backend.core.ouroboros.governance.claude_circuit_breaker as m
    for falsy in ("0", "false", "no", "off"):
        monkeypatch.setenv(
            "JARVIS_CLAUDE_CIRCUIT_BREAKER_ENABLED", falsy,
        )
        importlib.reload(m)
        assert m.is_enabled() is False


def test_threshold_env_override(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CLAUDE_CIRCUIT_BREAKER_THRESHOLD", "5",
    )
    import backend.core.ouroboros.governance.claude_circuit_breaker as m
    importlib.reload(m)
    b = m.ClaudeCircuitBreaker()
    assert b._failure_threshold == 5


def test_recovery_window_env_override(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CLAUDE_CIRCUIT_BREAKER_RECOVERY_S", "60",
    )
    import backend.core.ouroboros.governance.claude_circuit_breaker as m
    importlib.reload(m)
    b = m.ClaudeCircuitBreaker()
    assert b._recovery_window_s == 60.0


# -----------------------------------------------------------------------
# § F — Snapshot for telemetry
# -----------------------------------------------------------------------


def test_snapshot_shape():
    from backend.core.ouroboros.governance.claude_circuit_breaker import (
        ClaudeCircuitBreaker,
    )
    b = ClaudeCircuitBreaker()
    snap = b.snapshot()
    expected_keys = {
        "state",
        "consecutive_transport_failures",
        "tripped_at_monotonic",
        "failure_threshold",
        "recovery_window_s",
        "total_trips",
        "total_successes",
    }
    assert set(snap.keys()) == expected_keys
    assert snap["state"] == "closed"


# -----------------------------------------------------------------------
# § G — Bytes pins on integration sites
# -----------------------------------------------------------------------


def test_provider_records_exhaustion_on_transport_class():
    """providers.py must call record_transport_exhaustion when the
    retry loop exhausts on a transport-class exception."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/providers.py"
    ).read_text()
    assert "record_transport_exhaustion" in src
    assert "is_transport_class_exception" in src


def test_provider_records_success_on_call_completion():
    src = pathlib.Path(
        "backend/core/ouroboros/governance/providers.py"
    ).read_text()
    assert ".record_success()" in src


def test_dispatcher_consults_breaker_before_primary():
    """candidate_generator.py must check should_allow_request BEFORE
    calling primary, in _try_primary_then_fallback."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/candidate_generator.py"
    ).read_text()
    fn_idx = src.find("async def _try_primary_then_fallback(")
    next_def = src.find("    async def _call_primary(", fn_idx)
    body = src[fn_idx:next_def]
    assert "should_allow_request()" in body
    breaker_idx = body.find("should_allow_request()")
    primary_call_idx = body.find("await self._call_primary(")
    assert 0 < breaker_idx < primary_call_idx, (
        "breaker check must precede _call_primary"
    )


# -----------------------------------------------------------------------
# § H — Authority invariant
# -----------------------------------------------------------------------


def test_authority_invariant_no_governance_imports():
    """The breaker module imports only stdlib — no governance,
    orchestrator, or provider deps. Bytes-pin source."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/claude_circuit_breaker.py"
    ).read_text()
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.phase_runners",
    )
    for tok in forbidden:
        assert tok not in src, f"breaker forbidden import: {tok}"


def test_test_file_imports_only_breaker_module():
    """The test module imports only the breaker + stdlib. We grep for
    actual ``import`` statements (not docstring mentions) on the test
    file's parsed AST so the forbidden-list documented in this file
    doesn't false-positive against itself."""
    import ast
    src = pathlib.Path(__file__).read_text()
    tree = ast.parse(src)
    governance_imports: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if "backend.core.ouroboros.governance" in mod:
                governance_imports.append(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "backend.core.ouroboros.governance" in alias.name:
                    governance_imports.append(alias.name)
    # Only the breaker module is allowed
    for mod in governance_imports:
        assert "claude_circuit_breaker" in mod, (
            f"forbidden governance import in test: {mod}"
        )
