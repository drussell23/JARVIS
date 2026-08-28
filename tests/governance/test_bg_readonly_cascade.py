"""Regression spine — BACKGROUND read-only Nervous System Reflex + Option A.

Two structural guarantees this file locks down:

1. **Option A — Venom unlock for read-only BG/SPEC ops.** The
   ``_skip_tools = _route in {"background","speculative"}`` gate in
   providers.py must NOT fire when ``ctx.is_read_only=True``. Without
   this, ``dispatch_subagent`` is structurally unreachable on the
   low-cost routes — which is exactly the route BacklogSensor chooses
   for long-running cartography tasks. Because the policy engine's
   Rule 0d refuses every mutation tool under the read-only contract,
   keeping the tool loop active carries no cost-escalation risk.

2. **Nervous System Reflex (Manifesto §5).** Session 3b
   (bt-2026-04-18-032820) stalled for 5 minutes at
   ``phase=generate streaming=start`` because the provider_topology
   paused DW and the BACKGROUND route has historically had
   "DW only, no Claude fallback". For read-only ops this is
   structurally catastrophic — the op cannot reach the tool loop,
   cannot dispatch subagents, cannot produce its deliverable. The
   reflex: when a read-only BG op hits a DW stall (topology pause
   or ``JARVIS_BG_DW_STALL_BUDGET_S`` exhaustion), cascade to Claude
   instead of the ``background_dw_blocked_by_topology`` raise.

These tests keep the asserting surface small — they don't boot the
full provider stack, they test the routing decisions directly.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. Option A — _skip_tools contract
# ---------------------------------------------------------------------------
#
# We replicate the skip-decision logic without importing providers.py (which
# pulls in the whole provider stack). The *assertion* is the shape of the
# decision: given a route and is_read_only, skip_tools must be False for
# read-only ops on background/speculative routes.
#
# This mirrors providers.py:3471 (PrimeProvider) and providers.py:5385
# (ClaudeProvider) — if either drifts from the canonical rule, a reviewer
# should update this helper or add a second canonical.


def _canonical_skip_tools_decision(route: str, is_read_only: bool) -> bool:
    """Exact semantics of the post-Option A providers.py gate."""
    return route in ("background", "speculative") and not is_read_only


@pytest.mark.parametrize(
    "route,is_read_only,expected",
    [
        ("background", False, True),        # BG mutating → skip (cost guardrail)
        ("speculative", False, True),       # SPEC mutating → skip
        ("background", True, False),        # BG read-only → UNLOCKED (Option A)
        ("speculative", True, False),       # SPEC read-only → UNLOCKED
        ("immediate", False, False),        # IMMEDIATE never skips
        ("standard", False, False),         # STANDARD never skips
        ("complex", False, False),          # COMPLEX never skips
        ("immediate", True, False),         # IMMEDIATE + read-only → also keep
    ],
)
def test_skip_tools_decision_matrix(
    route: str, is_read_only: bool, expected: bool
) -> None:
    assert _canonical_skip_tools_decision(route, is_read_only) == expected


def test_canonical_matches_providers_prime() -> None:
    """Spot-check: the canonical helper must produce the same decision the
    live provider would produce. Imported lazily to avoid booting heavy
    dependencies at module-import time.
    """
    # Read the live providers.py gate by pattern-matching the module text,
    # not by calling into it. This is intentionally structural so a drift
    # in the two gates (PrimeProvider + ClaudeProvider) is loudly visible.
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
    ).read_text()
    # Assert the INVARIANT, not one spelling of it.
    #
    # This used to pin the literal
    #     _skip_tools = _route in ("background", "speculative") and not _is_read_only
    # which stopped existing when the route tuple was extracted into
    # `should_skip_venom_for_route()` — a strict improvement (the route list
    # now lives in one place) that this test reported as a regression, because
    # it was matching the implementation's TEXT rather than its meaning.
    #
    # What actually has to hold: every route-derived skip decision, at every
    # provider site, must be conjoined with `not _is_read_only`. A read-only op
    # may never have the tool loop skipped out from under it — that is what
    # makes `dispatch_subagent` reachable on the low-cost routes, and Rule 0d
    # is what makes it safe. `_skip_tools = True` assignments from other
    # sources (e.g. the repair-context path) are deliberately out of scope.
    # `startswith` after stripping, so prose is excluded: providers.py quotes
    # the old inline form inside a docstring (~line 3298), and a drift
    # detector that trips on its own module's COMMENTARY reports noise instead
    # of drift. Only real assignments are gates.
    route_gates = [
        ln.strip()
        for ln in src.splitlines()
        if ln.strip().startswith("_skip_tools =") and "_route" in ln
    ]
    assert len(route_gates) >= 2, (
        "Expected at least 2 route-derived skip-tools gates "
        f"(PrimeProvider + ClaudeProvider); found {len(route_gates)}: "
        f"{route_gates}"
    )
    for gate in route_gates:
        assert "not _is_read_only" in gate, (
            "a provider's route-derived skip-tools gate no longer honours "
            f"the read-only contract: {gate!r}"
        )


# ---------------------------------------------------------------------------
# 2. Nervous System Reflex — topology skip_and_queue bypass for read-only BG
# ---------------------------------------------------------------------------
#
# When the provider topology blocks DW on BACKGROUND with block_mode=
# skip_and_queue, the ordinary behavior is: raise
# ``background_dw_blocked_by_topology``. For read-only ops that becomes
# a lockup. The fix: cascade to Claude via ``_call_fallback`` instead.


class _FakeContext:
    """Minimal duck-typed OperationContext for _generate_background."""

    def __init__(self, *, op_id: str, is_read_only: bool) -> None:
        self.op_id = op_id
        self.is_read_only = is_read_only
        self.signal_urgency = "low"
        self.signal_source = "backlog"
        self.provider_route = "background"
        self.primary_repo = "jarvis"


def _make_generator_with_paused_topology(
    *, fallback_succeeds: bool = True
) -> Any:
    """Build a CandidateGenerator where the topology pauses DW on BG."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    gen = CandidateGenerator.__new__(CandidateGenerator)
    gen._tier0 = MagicMock()
    gen._tier0.is_available = True
    gen._fallback = MagicMock() if fallback_succeeds else None
    gen._prime = None
    gen._remaining_seconds = lambda _dl: 300.0  # plenty of budget

    fallback_result = MagicMock()
    fallback_result.candidates = ["fake_candidate"]
    fallback_result.generation_duration_s = 2.5
    fallback_result.cost_usd = 0.01

    if fallback_succeeds:
        gen._call_fallback = AsyncMock(return_value=fallback_result)
    return gen


def _blocked_topology(
    *,
    block_mode: str = "skip_and_queue",
    reason: str = "dw_paused_for_test",
) -> MagicMock:
    """A topology that blocks DW, speaking the Slice-5a unified contract.

    These tests used to stub only the PRE-Slice-5a trio
    (``dw_allowed_for_route`` / ``reason_for_route`` / ``block_mode_for_route``)
    while production had moved to the unified
    :meth:`ProviderTopology.is_dw_blocked_for_route`. A ``MagicMock``
    auto-creates any attribute it is asked for, so the missing method returned
    a ``Mock`` that unpacked to nothing::

        ValueError: not enough values to unpack (expected 3, got 0)

    — which is worse than an AttributeError, because it fails at the call site
    with a message that says nothing about the real cause: a mock that had
    silently stopped resembling the thing it stands for.

    The fix is not to hand-write the 3-tuple here. That would put a SECOND
    copy of the v1→tuple translation in the test suite, free to drift from the
    one in production exactly as the old stub did. Instead the fixture supplies
    only the PRIMITIVE facts and delegates to the real implementation, so the
    derivation under test is production's own.

    Both generations are stubbed deliberately: the v1 trio and the v2 pair
    (``dw_models_for_route`` / ``fallback_tolerance_for_route``). Which branch
    runs depends on ``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` at call time, and a
    fixture that only satisfies one of them is a test that passes or fails on
    an ambient env var. When Slice 5b deletes the v1 branch, the v2 stubs here
    already carry the same meaning.
    """
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )

    topo = MagicMock()
    topo.enabled = True
    # v1 primitives (sentinel OFF path).
    topo.dw_allowed_for_route = lambda route: False
    topo.reason_for_route = lambda route: reason
    topo.block_mode_for_route = lambda route: block_mode
    # v2 primitives (sentinel ON path) — same meaning: no DW models for this
    # route, and the route queues rather than cascading.
    topo.dw_models_for_route = lambda route: []
    topo.fallback_tolerance_for_route = lambda route: (
        "queue" if block_mode == "skip_and_queue" else "cascade_to_claude"
    )
    # THE POINT: production's own derivation, bound to this fixture's facts.
    topo.is_dw_blocked_for_route = (
        lambda route: ProviderTopology.is_dw_blocked_for_route(topo, route)
    )
    return topo


def _allowed_topology(reason: str = "dw_allowed_for_test") -> MagicMock:
    """The permissive counterpart of :func:`_blocked_topology`.

    Same delegation, opposite primitives. Exists so no test in this file
    hand-rolls a v1-only topology again: such a mock passes for as long as
    its code path happens not to reach the unified helper, and fails
    incomprehensibly the moment it does.
    """
    from backend.core.ouroboros.governance.provider_topology import (
        ProviderTopology,
    )

    topo = MagicMock()
    topo.enabled = True
    topo.dw_allowed_for_route = lambda route: True
    topo.reason_for_route = lambda route: reason
    topo.block_mode_for_route = lambda route: "cascade_to_claude"
    topo.dw_models_for_route = lambda route: ["dw-model-for-test"]
    topo.fallback_tolerance_for_route = lambda route: "cascade_to_claude"
    topo.is_dw_blocked_for_route = (
        lambda route: ProviderTopology.is_dw_blocked_for_route(topo, route)
    )
    return topo


@pytest.mark.asyncio
async def test_bg_readonly_cascades_on_topology_skip_and_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core Nervous System Reflex test: topology paused DW + read-only
    op on BG route must NOT raise skip_and_queue; it must cascade to Claude.
    """
    from backend.core.ouroboros.governance import candidate_generator as cg

    # Force topology to block BG with skip_and_queue
    _topology = _blocked_topology()
    monkeypatch.setattr(cg, "get_topology", lambda: _topology, raising=False)

    # The get_topology import inside the method is local — we also need
    # to patch the module export it imports from.
    from backend.core.ouroboros.governance import provider_topology
    monkeypatch.setattr(
        provider_topology, "get_topology", lambda: _topology,
    )

    gen = _make_generator_with_paused_topology()
    ctx = _FakeContext(op_id="op-test-bg-readonly", is_read_only=True)

    # The cascade path is inside _dispatch_by_route. Simulate reaching it.
    from datetime import datetime, timedelta, timezone
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=300)

    # We call _dispatch_by_route which is where the topology check lives.
    result = await gen._generate_dispatch(
        ctx,  # type: ignore[arg-type]
        deadline,
    )
    assert result is not None
    assert len(result.candidates) == 1
    gen._call_fallback.assert_called_once()


@pytest.mark.asyncio
async def test_bg_mutating_still_raises_on_topology_skip_and_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Baseline: without is_read_only, the old behavior is preserved.

    Mutating BG ops must still raise background_dw_blocked_by_topology —
    the Nervous System Reflex is a read-only-scoped exception, not a
    blanket change to the cost guardrail.
    """
    from backend.core.ouroboros.governance import candidate_generator as cg
    from backend.core.ouroboros.governance import provider_topology

    _topology = _blocked_topology()
    monkeypatch.setattr(cg, "get_topology", lambda: _topology, raising=False)
    monkeypatch.setattr(provider_topology, "get_topology", lambda: _topology)

    gen = _make_generator_with_paused_topology()
    ctx = _FakeContext(op_id="op-test-bg-mutating", is_read_only=False)

    from datetime import datetime, timedelta, timezone
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=300)

    with pytest.raises(RuntimeError, match="background_dw_blocked_by_topology"):
        await gen._generate_dispatch(
            ctx,  # type: ignore[arg-type]
            deadline,
        )
    # Fallback must NOT have been called for mutating ops
    gen._call_fallback.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Strict DW stall budget for read-only ops
# ---------------------------------------------------------------------------


def test_bg_readonly_stall_budget_default() -> None:
    """Default is 60s unless JARVIS_BG_DW_STALL_BUDGET_S is overridden."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _BG_READONLY_DW_STALL_BUDGET_S,
    )
    # Default is 60.0; env can tune it. We just check it's a reasonable
    # bound (positive and ≤ the mutating cap of 180s).
    assert 0 < _BG_READONLY_DW_STALL_BUDGET_S <= 180.0


def test_bg_readonly_forces_allow_fallback_independent_of_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structural contract: is_read_only=True on BG must force
    _allow_fallback=True even when JARVIS_BACKGROUND_ALLOW_FALLBACK is unset.

    This is the load-bearing half of the Nervous System Reflex — without
    it, the DW stall timeout path at line ~1900 would still raise without
    cascading.
    """
    monkeypatch.delenv("JARVIS_BACKGROUND_ALLOW_FALLBACK", raising=False)
    monkeypatch.delenv("FORCE_CLAUDE_BACKGROUND", raising=False)

    from backend.core.ouroboros.governance import candidate_generator as cg

    # Inspect the source of _generate_background — the rule must be
    # structurally visible: `_allow_fallback = True` set under is_read_only.
    import inspect
    src = inspect.getsource(cg.CandidateGenerator._generate_background)
    assert "_is_read_only" in src
    assert "_allow_fallback = True" in src
    # The coupling: a read-only branch must appear near the _allow_fallback
    # assignment.
    lines = src.splitlines()
    allow_idx = next(
        i for i, ln in enumerate(lines)
        if "_allow_fallback = True" in ln
    )
    # Look back for the is_read_only guard within the preceding 10 lines.
    window = lines[max(0, allow_idx - 10):allow_idx + 1]
    assert any("_is_read_only" in ln for ln in window), (
        "The _allow_fallback=True assignment must be guarded by an "
        "_is_read_only check — the Nervous System Reflex cannot silently "
        "apply to mutating ops"
    )


# ---------------------------------------------------------------------------
# 4. Budget Math Patch — dynamic _max_cap for read-only BG (Session 6)
# ---------------------------------------------------------------------------
#
# Session 5 proved the graduation signal but died at synthesis-round
# timeout: max_cap=120s, subagents consumed 134.56s, Claude had <46s
# left. Derek's directive: for read-only BG ops the cap must expand to
# base_120s + MAX_PARALLEL_SCOPES*PRIMARY_PROVIDER_TIMEOUT_S + 90s
# synthesis reserve.


def test_readonly_bg_cap_extends_to_full_fanout_budget() -> None:
    """The extended cap must be large enough to accommodate worst-case
    3-parallel subagent wall-clock plus a 90s synthesis reserve.
    """
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    from backend.core.ouroboros.governance.subagent_contracts import (
        MAX_PARALLEL_SCOPES,
        PRIMARY_PROVIDER_TIMEOUT_S,
    )

    # Structural check on the class-level constant the patch introduced.
    assert hasattr(CandidateGenerator, "_BG_READONLY_SYNTHESIS_RESERVE_S")
    reserve = CandidateGenerator._BG_READONLY_SYNTHESIS_RESERVE_S
    assert reserve >= 60.0  # Derek's mandate is 90s; floor at 60s safety.

    # Formula expected by the patch: base + subagent_wallclock + reserve
    base = CandidateGenerator._FALLBACK_MAX_TIMEOUT_S
    expected_cap = (
        base + MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S + reserve
    )

    # The cap must be substantially larger than the mutating-BG cap —
    # that's the whole point of the patch.
    assert expected_cap > base * 3, (
        f"Expected read-only BG cap ({expected_cap:.0f}s) to be at "
        f"least 3x the mutating-BG base cap ({base:.0f}s)"
    )

    # Sanity: the cap is wide enough to cover the Session-5 failure
    # mode. Session 5 timed out at sem_wait_total_s=134.56 with cap=120;
    # the new cap must be comfortably above that.
    assert expected_cap >= 300.0, (
        f"Read-only BG cap must be ≥ 300s to cover Session-5 style "
        f"fan-out patterns, got {expected_cap:.0f}s"
    )


@pytest.mark.asyncio
async def test_readonly_bg_fallback_applies_extended_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When is_read_only=True + BG route, _call_fallback must observe
    the extended cap, not the 120s mutating baseline.
    """
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    from backend.core.ouroboros.governance.subagent_contracts import (
        MAX_PARALLEL_SCOPES,
        PRIMARY_PROVIDER_TIMEOUT_S,
    )

    # Build a hollow generator with just enough to exercise _call_fallback.
    gen = CandidateGenerator.__new__(CandidateGenerator)
    gen._fallback = MagicMock()
    gen._remaining_seconds = lambda _dl: 500.0
    gen._fallback_concurrency = 3

    # Intercept max_cap by patching the log line — it's the most stable
    # capture point. We inspect the call args of the acquire log.
    captured_caps: list = []

    original_info = None
    import logging

    class CapCapture(logging.Handler):
        def emit(self, record):
            msg = record.getMessage()
            if "Fallback sem acquire" in msg and "max_cap=" in msg:
                # Extract max_cap=...s from the message.
                import re
                m = re.search(r"max_cap=([\d.]+)s", msg)
                if m:
                    captured_caps.append(float(m.group(1)))

    handler = CapCapture()
    logging.getLogger(
        "backend.core.ouroboros.governance.candidate_generator"
    ).addHandler(handler)
    try:
        # Minimal asyncio sem so the sem async with block doesn't crash.
        gen._fallback_sem = asyncio.Semaphore(3)

        # Return control fast once we're past the log — raise to exit.
        async def _boom(ctx, dl):
            raise RuntimeError("test_done")

        monkeypatch.setattr(gen, "_FALLBACK_MIN_GUARANTEED_S", 10.0, raising=False)

        # Patch the inner fallback call to exit after max_cap is logged.
        async def _exit_after_log(*a, **kw):
            raise RuntimeError("test_done")
        # _call_fallback does a lot more than just the sem+log — we only
        # need the acquire log to fire. The method raises naturally when
        # `_fallback.generate` is a MagicMock with no side_effect, so we
        # just need to run it to the log point.

        from datetime import datetime, timedelta, timezone
        dl = datetime.now(tz=timezone.utc) + timedelta(seconds=500)

        ctx = _FakeContext(op_id="op-test-cap", is_read_only=True)

        try:
            await gen._call_fallback(ctx, dl)  # type: ignore[arg-type]
        except Exception:
            pass  # We're intentionally not completing the call.

        assert captured_caps, "Fallback sem acquire log must have fired"
        observed_cap = captured_caps[0]
        expected = (
            CandidateGenerator._FALLBACK_MAX_TIMEOUT_S
            + MAX_PARALLEL_SCOPES * PRIMARY_PROVIDER_TIMEOUT_S
            + CandidateGenerator._BG_READONLY_SYNTHESIS_RESERVE_S
        )
        assert observed_cap == pytest.approx(expected, abs=0.5), (
            f"Expected extended cap {expected:.1f}s, observed "
            f"{observed_cap:.1f}s — the read-only BG branch didn't fire"
        )
    finally:
        logging.getLogger(
            "backend.core.ouroboros.governance.candidate_generator"
        ).removeHandler(handler)


def test_mutating_bg_cap_unchanged_after_patch() -> None:
    """Mutating BG ops must still get the 120s cap — the extended cap
    is scoped strictly to is_read_only=True.
    """
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    # Structural invariant: the mutating baseline is still _FALLBACK_MAX_TIMEOUT_S
    # and the read-only extension is additive on top of it.
    assert CandidateGenerator._FALLBACK_MAX_TIMEOUT_S == pytest.approx(
        120.0, abs=0.1
    )


@pytest.mark.asyncio
async def test_bg_readonly_uses_tight_stall_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When read-only, the DW cap used is _BG_READONLY_DW_STALL_BUDGET_S,
    not the 150s/180s mutating cap.
    """
    from backend.core.ouroboros.governance import candidate_generator as cg
    from backend.core.ouroboros.governance import provider_topology

    # Topology lets BG through (no skip_and_queue) so we exercise the
    # DW-attempt path. Built through the same helper as the blocking
    # fixtures: this one passes today only because the code path under test
    # happens not to reach `is_dw_blocked_for_route`, which makes a bare
    # v1-only MagicMock a landmine rather than a passing test.
    _topology = _allowed_topology()
    monkeypatch.setattr(provider_topology, "get_topology", lambda: _topology)

    captured = {}

    async def _fake_generate(ctx, dl):
        await asyncio.sleep(0)
        captured["called"] = True
        return None  # empty → _dw_error="background_dw_empty_result"

    gen = _make_generator_with_paused_topology()
    gen._tier0._realtime_enabled = True
    gen._tier0.generate = _fake_generate
    gen._tier0.is_available = True

    ctx = _FakeContext(op_id="op-test-tight-cap", is_read_only=True)

    from datetime import datetime, timedelta, timezone
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=600)

    # Patch asyncio.wait_for to capture the timeout used.
    captured_timeout = {}
    real_wait_for = asyncio.wait_for

    async def spy_wait_for(awaitable, timeout):
        captured_timeout["timeout"] = timeout
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)

    # We expect the function to attempt DW, get empty, then cascade.
    result = await gen._generate_background(ctx, deadline)  # type: ignore[arg-type]

    assert captured.get("called") is True
    assert "timeout" in captured_timeout
    # The cap must be tight — strictly less than the mutating cap of 150s.
    assert captured_timeout["timeout"] <= cg._BG_READONLY_DW_STALL_BUDGET_S + 0.01
    assert captured_timeout["timeout"] < 150.0
    # And the cascade to Claude must have fired.
    gen._call_fallback.assert_called_once()
    assert result is not None
