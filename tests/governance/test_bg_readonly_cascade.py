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
    # Both gates must read `_is_read_only` and use the same shape.
    assert "_skip_tools = _route in (\"background\", \"speculative\") and not _is_read_only" in src, (
        "providers.py does not contain the expected post-Option A "
        "skip-tools gate — the two provider sites must agree"
    )
    assert src.count("_skip_tools = _route in (\"background\", \"speculative\") and not _is_read_only") >= 2, (
        "Expected at least 2 occurrences of the canonical gate "
        "(PrimeProvider + ClaudeProvider)"
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


@pytest.mark.asyncio
async def test_bg_readonly_cascades_on_topology_skip_and_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core Nervous System Reflex test: topology paused DW + read-only
    op on BG route must NOT raise skip_and_queue; it must cascade to Claude.
    """
    from backend.core.ouroboros.governance import candidate_generator as cg

    # Force topology to block BG with skip_and_queue
    _topology = MagicMock()
    _topology.enabled = True
    _topology.dw_allowed_for_route = lambda route: False
    _topology.reason_for_route = lambda route: "dw_paused_for_test"
    _topology.block_mode_for_route = lambda route: "skip_and_queue"
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

    _topology = MagicMock()
    _topology.enabled = True
    _topology.dw_allowed_for_route = lambda route: False
    _topology.reason_for_route = lambda route: "dw_paused_for_test"
    _topology.block_mode_for_route = lambda route: "skip_and_queue"
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
    # DW-attempt path.
    _topology = MagicMock()
    _topology.enabled = True
    _topology.dw_allowed_for_route = lambda route: True
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
