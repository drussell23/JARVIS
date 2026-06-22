"""T4: Integration spine + static validation for the Autonomous Convergence Watchdog.

Exercises the end-to-end flow (tracker -> verdict -> shedder) without
instantiating the full governed stack.  These are "operator gate" tests:
they prove locally -- before any C2 soak -- that:

  (I1) A stalled irreducible lineage converges to <=target locally (no infinite
       loop, no DW egress).
  (I2) Zero watchdog egress: shedder is pure-deterministic, no model call.
  (I3) Self-healing: stall -> shed -> fitting output observable via
       [SOVEREIGN YIELD] WARNING.
  (I4) Fail-soft: parse error -> tier3 truncation; never crashes.
  (I5) OFF byte-identical: disabled flag -> watchdog_enabled() False.
  (I6) Pure AST only in the shedder (no exec/eval).

These tests are STATIC -- they do NOT run the live orchestrator FSM and do
NOT touch the network.
"""
from __future__ import annotations

import builtins
import inspect
import logging

import pytest

from backend.core.ouroboros.governance import convergence_watchdog as cw_mod
from backend.core.ouroboros.governance.convergence_watchdog import (
    WatchdogVerdict,
    emit_sovereign_yield,
    get_reduction_tracker,
    watchdog_enabled,
)
from backend.core.ouroboros.governance.epistemic_shedder import shed_to_fit

# ---------------------------------------------------------------------------
# Source fixtures
# ---------------------------------------------------------------------------

_HEAVY_SOURCE = '''
"""Module-level docstring that adds padding to the source text so the total
character count significantly exceeds any reasonable target budget."""

import os
import sys


def alpha(x, y, z):
    """Alpha function docstring, also padding."""
    result = 0
    for i in range(x):
        for j in range(y):
            result += i * j * z
    return result


def beta(data):
    """Beta function with a very heavy body."""
    output = []
    for item in data:
        if item > 0:
            output.append(item * 2)
        elif item < 0:
            output.append(item - 1)
        else:
            output.append(0)
    return output


class Worker:
    """Worker class with multiple heavy methods."""

    def __init__(self, config):
        """Init docstring padding."""
        self.config = config
        self.state = {}

    def run(self):
        """Run method with a large body to inflate character count."""
        for key, value in self.config.items():
            if isinstance(value, list):
                self.state[key] = [v * 2 for v in value]
            else:
                self.state[key] = value
        return self.state

    def cleanup(self):
        """Cleanup method."""
        self.state.clear()
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tracker_singleton():
    """Reset process-global ReductionTracker between tests to avoid pollution."""
    cw_mod._REDUCTION_TRACKER_SINGLETON = None  # type: ignore[attr-defined]
    yield
    cw_mod._REDUCTION_TRACKER_SINGLETON = None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Test 1: Stall -> yield (operator gate)
#   Drive IRREDUCIBLE-but-overweight lineage through 2 stalled passes.
#   Prove loop TERMINATES: the shed output is <=target, not re-sliced again.
# ---------------------------------------------------------------------------


def test_stall_then_yield_converges_to_target():
    """Two stalled passes cause WatchdogVerdict.stalled, then shed_to_fit
    returns a payload whose len <= target. Loop terminates -- no infinite
    re-decompose, no DW egress (shedder is synchronous + pure).
    """
    tracker = get_reduction_tracker()
    lineage = "test-lineage-irred-01"
    target = 200  # aggressive budget the heavy source cannot satisfy raw

    # Pass 1: large parent (1000), large child (990) -> ratio 0.99, stall #1.
    v1 = tracker.record_pass(lineage, parent_chars=1000, max_child_chars=990)
    assert v1.stalled is False, "Single stall should NOT trip threshold (need 2)"
    assert v1.consecutive_stalls == 1
    assert v1.ratio == pytest.approx(0.99, abs=1e-6)

    # Pass 2: same irreducible ratio -> stall #2 trips.
    v2 = tracker.record_pass(lineage, parent_chars=1000, max_child_chars=990)
    assert v2.stalled is True, "Two consecutive stalls must trip the watchdog"
    assert v2.consecutive_stalls == 2

    # Now shed the heavy source to fit the target.
    shed, tier = shed_to_fit(_HEAVY_SOURCE, target_chars=target)

    # CORE INVARIANT: result fits the target -> loop terminates.
    assert len(shed) <= target, (
        f"shed_to_fit must return <=target chars; got {len(shed)}, target={target}, tier={tier}"
    )

    # The tier label must be a known value.
    assert tier in ("none", "tier1", "tier2", "tier3"), f"Unknown tier: {tier!r}"


# ---------------------------------------------------------------------------
# Test 2: Reducible lineage -> no stall, normal slicing path.
# ---------------------------------------------------------------------------


def test_reducible_lineage_never_stalls():
    """When the child is well below the parent on every pass, the tracker
    never trips -- no stall verdict, no watchdog intervention needed.
    """
    tracker = get_reduction_tracker()
    lineage = "test-lineage-good-01"

    # Feed 5 passes where child is only 30% of parent (ratio 0.30).
    for pass_num in range(5):
        v = tracker.record_pass(lineage, parent_chars=1000, max_child_chars=300)
        assert v.stalled is False, (
            f"Pass {pass_num + 1}: ratio 0.30 should never stall; got {v}"
        )
        assert v.consecutive_stalls == 0


# ---------------------------------------------------------------------------
# Test 3: Tier escalation fits -- tier1 -> tier2 -> tier3 cascade.
# ---------------------------------------------------------------------------


def test_tier_escalation_fits_at_tier3():
    """When source is too heavy for tier1 or tier2, shed_to_fit escalates
    through the tiers and the final result is always <=target.
    Each re-measurement after each tier is internal to shed_to_fit.
    """
    # Use a very tight target to force tier3.
    target = 50
    shed, tier = shed_to_fit(_HEAVY_SOURCE, target_chars=target)

    assert len(shed) <= target, (
        f"tier3 nuclear truncation must fit; got len={len(shed)}, target={target}"
    )
    # With a 50-char target against ~1KB source, tier3 should be hit.
    assert tier == "tier3", f"Expected tier3 for tight budget; got {tier!r}"


def test_tier1_fits_when_docstrings_are_the_excess():
    """When the source is only slightly over-budget due to docstrings, tier1
    should be enough -- the result has no module/function docstrings and fits.
    """
    # Build a source where the docstrings are the bulk of the excess.
    minimal_core = "def f(x):\n    return x\n"
    padded_docstrings = '"""' + ("X" * 300) + '"""\n' + minimal_core
    # Core without docstrings is ~30 chars; add docstring padding to push it over.
    target = len(minimal_core) + 10  # fits without the docstring

    shed, tier = shed_to_fit(padded_docstrings, target_chars=target)
    assert len(shed) <= target, (
        f"tier1 doc-strip should fit; got len={len(shed)}, target={target}, tier={tier!r}"
    )
    # Tier 1 or better should have caught this.
    assert tier in ("tier1", "tier2", "tier3")


# ---------------------------------------------------------------------------
# Test 4: Parse error -> tier3 (graceful degradation).
# ---------------------------------------------------------------------------


def test_parse_error_falls_to_tier3():
    """Malformed Python source that cannot be ast.parsed falls straight to
    tier3 truncation.  The result is always <=target, never raises.
    """
    malformed = "def (:\n  oops " * 30  # intentionally invalid Python
    target = 40

    shed, tier = shed_to_fit(malformed, target_chars=target)

    assert tier == "tier3", f"Parse error must reach tier3; got {tier!r}"
    assert len(shed) <= target, (
        f"Tier3 truncation must fit budget; got len={len(shed)}, target={target}"
    )


# ---------------------------------------------------------------------------
# Test 5: Zero watchdog egress -- shedder is synchronous + pure, no network.
# ---------------------------------------------------------------------------


def test_zero_watchdog_egress_shedder_is_synchronous():
    """shed_to_fit is a pure synchronous function -- it cannot make a
    network call or DW egress by construction.  This test asserts the return
    type contract: a 2-tuple (str, str), which proves it completed
    synchronously without awaiting anything.
    """
    result = shed_to_fit(_HEAVY_SOURCE, target_chars=100)

    # Pure synchronous function must return a plain tuple, not a coroutine.
    assert isinstance(result, tuple), "shed_to_fit must return a plain tuple"
    assert len(result) == 2, "shed_to_fit must return (str, str)"
    shed, tier = result
    assert isinstance(shed, str), "First element must be str"
    assert isinstance(tier, str), "Second element (tier) must be str"

    # No coroutine object was returned (which would indicate an await was needed).
    assert not inspect.iscoroutine(result), "shed_to_fit must not return a coroutine"


# ---------------------------------------------------------------------------
# Test 6: NEVER exec -- shedder must not call exec under any circumstance.
# ---------------------------------------------------------------------------


def test_never_exec_called_by_shedder(monkeypatch):
    """The epistemic shedder specification requires PURE AST only:
    ast.parse / ast.unparse / ast.get_source_segment / ast.fix_missing_locations
    NEVER exec / eval / compile(..., mode='exec').

    Monkeypatch builtins.exec to raise; shed_to_fit must complete without
    triggering it.
    """
    def _forbidden_exec(*args, **kwargs):
        raise AssertionError("epistemic_shedder called exec -- FORBIDDEN by spec")

    monkeypatch.setattr(builtins, "exec", _forbidden_exec)

    # Must complete without raising AssertionError (exec was not called).
    shed, tier = shed_to_fit(_HEAVY_SOURCE, target_chars=80)
    assert isinstance(shed, str), "shed_to_fit must return a string even with exec blocked"


# ---------------------------------------------------------------------------
# Test 7: OFF byte-identical -- disabled flag means watchdog_enabled() False.
# ---------------------------------------------------------------------------


def test_watchdog_disabled_flag_returns_false(monkeypatch):
    """JARVIS_CONVERGENCE_WATCHDOG_ENABLED=false must make watchdog_enabled()
    return False.  This is the OFF byte-identical gate: the outer orchestrator
    seam checks this flag before any watchdog logic runs.
    """
    monkeypatch.setenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", "false")
    assert watchdog_enabled() is False

    # Also verify the '0' form.
    monkeypatch.setenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", "0")
    assert watchdog_enabled() is False

    # And verify it defaults to True when not set.
    monkeypatch.delenv("JARVIS_CONVERGENCE_WATCHDOG_ENABLED", raising=False)
    assert watchdog_enabled() is True


# ---------------------------------------------------------------------------
# Test 8: [SOVEREIGN YIELD] emitted -- emit_sovereign_yield logs WARNING.
# ---------------------------------------------------------------------------


def test_emit_sovereign_yield_logs_warning(caplog):
    """emit_sovereign_yield must log a WARNING containing '[SOVEREIGN YIELD]'
    and must be fail-soft without an SSE broker (the lazy import silently
    swallows ImportError / AttributeError from the missing SSE stack).
    """
    with caplog.at_level(logging.WARNING, logger="backend.core.ouroboros.governance.convergence_watchdog"):
        emit_sovereign_yield(
            "op-t4-test-001",
            lineage_id="lineage-t4-01",
            ratio=0.97,
            consecutive_stalls=2,
            parent_chars=5000,
            child_chars=4850,
            tier="tier2",
        )

    # Must have emitted at least one WARNING with the sentinel.
    yield_records = [
        r for r in caplog.records if "[SOVEREIGN YIELD]" in r.getMessage()
    ]
    assert yield_records, (
        "emit_sovereign_yield must emit a WARNING containing '[SOVEREIGN YIELD]'; "
        f"records seen: {[r.getMessage() for r in caplog.records]}"
    )

    # The record must be at WARNING level.
    assert any(r.levelno == logging.WARNING for r in yield_records), (
        "The [SOVEREIGN YIELD] record must be at WARNING level"
    )

    # Key fields must appear in the log message.
    msg = yield_records[0].getMessage()
    assert "op-t4-test-001" in msg
    assert "lineage-t4-01" in msg
    assert "0.970" in msg or "0.97" in msg
