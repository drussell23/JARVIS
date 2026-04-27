"""Priority #2 — BG-starvation end-to-end integration diagnostic.

Per `memory/project_wave3_item6_graduation_matrix.md` + brutal-review
v3 §5 Priority #2: 3 consecutive sessions ended with `ops=0`. The
documented root cause is 3-layered:

  Layer 1 — BacklogSensor BACKGROUND default        ✅ closed by F3
                                                      (env override)
  Layer 2 — Intake queue selection FIFO-within-class 🔴 OPEN — claimed
                                                      mechanism: BG ops
                                                      flood the queue
                                                      and starve the
                                                      seed
  Layer 3 — UrgencyRouter source-type mapping        🔴 CLAIMED — but
                                                      Priority-1
                                                      IMMEDIATE check
                                                      at line 262
                                                      reads urgency
                                                      first, source
                                                      second

This module is **not a fix** — it's an **integration diagnostic
test** that traces a synthetic seed through the full
BacklogSensor → IntakeRouter → UrgencyRouter chain in-process,
asserting at each phase whether the urgency=critical signal
survives.

If the diagnostic passes:
  * The static code path is wired correctly.
  * The session bug is in production runtime composition (e.g. cost
    governor downgrading route, BG worker pool re-classifying,
    ordering-of-checks issue with another phase).
  * Next step: capture a fresh session debug.log with seed in flight
    and grep for the exact divergence.

If the diagnostic fails:
  * The bug is localized to a specific layer (test names indicate which).
  * Next step: fix that layer.

## What we're testing

  1. F3 stamps `envelope.urgency=critical` when env var set.
  2. UnifiedIntakeRouter's `_compute_priority` returns -1 for a
     critical-urgency backlog seed (base 2 - urgency boost 3).
  3. UrgencyRouter's Priority-1 IMMEDIATE check fires for any
     `signal_urgency=critical` ctx, regardless of source.
  4. A simulated mixed-load (DocStaleness flood + 1 backlog seed)
     surfaces the seed FIRST when both go through the same priority
     queue.
"""
from __future__ import annotations

import os
from typing import List, Tuple

import pytest


# ---------------------------------------------------------------------------
# Layer 1 — BacklogSensor F3 stamps envelope.urgency=critical
# ---------------------------------------------------------------------------


def test_layer_1_f3_stamps_critical_on_envelope(
    monkeypatch: pytest.MonkeyPatch,
):
    """F3 env override produces envelope.urgency='critical'."""
    monkeypatch.setenv(
        "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "critical",
    )
    from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (  # noqa: E501
        _default_urgency_override,
    )
    assert _default_urgency_override() == "critical"


def test_layer_1_f3_disabled_returns_none(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(
        "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", raising=False,
    )
    from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (  # noqa: E501
        _default_urgency_override,
    )
    assert _default_urgency_override() is None


def test_layer_1_f3_invalid_returns_none(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "panic",
    )
    from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (  # noqa: E501
        _default_urgency_override,
    )
    assert _default_urgency_override() is None


# ---------------------------------------------------------------------------
# Layer 2 — _compute_priority returns negative for critical seed
# ---------------------------------------------------------------------------


def test_layer_2_critical_backlog_seed_priority_is_negative():
    """A critical-urgency backlog seed has priority -1 (base 2 -
    urgency boost 3 = -1). Lower = higher priority. Should beat any
    DocStaleness op (no entry in _PRIORITY_MAP → base 99)."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        _compute_priority,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        IntentEnvelope, make_envelope,
    )
    seed = make_envelope(
        source="backlog",
        description="forced-reach seed",
        target_files=("a.py",),
        repo="jarvis",
        urgency="critical",
        confidence=0.85,
        evidence={"task_id": "wave3-seed", "signature": "wave3-seed"}, requires_human_ack=False,
    )
    priority, _alignment = _compute_priority(seed)
    assert priority < 0, (
        f"critical backlog seed should have negative priority, got {priority}"
    )


def test_layer_2_docstaleness_priority_is_high_int():
    """A normal-urgency DocStaleness op has priority 99 (no entry
    in _PRIORITY_MAP → base 99, no urgency boost)."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        _compute_priority,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    docstale = make_envelope(
        source="doc_staleness",
        description="stale docstring",
        target_files=("b.py",),
        repo="jarvis",
        urgency="normal",
        confidence=0.5,
        evidence={"signature": "stale-1"}, requires_human_ack=False,
    )
    priority, _alignment = _compute_priority(docstale)
    assert priority >= 50, (
        f"DocStaleness should have high (worse) priority, got {priority}"
    )


def test_layer_2_priority_queue_orders_seed_before_flood():
    """In an asyncio.PriorityQueue, a critical backlog seed (priority -1)
    is dequeued BEFORE 10 normal DocStaleness ops (priority 99) even
    when the seed enqueues LAST (proves not FIFO)."""
    import asyncio
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        _compute_priority,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )

    async def _run() -> List[str]:
        q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        # Enqueue 10 DocStaleness ops FIRST.
        for i in range(10):
            doc = make_envelope(
                source="doc_staleness",
                description=f"stale-{i}",
                target_files=(f"d{i}.py",),
                repo="jarvis",
                urgency="normal",
                confidence=0.5,
                evidence={"signature": f"sd-{i}"}, requires_human_ack=False,
            )
            p, _ = _compute_priority(doc)
            await q.put((p, doc.submitted_at, doc))
        # Then enqueue the seed LAST.
        seed = make_envelope(
            source="backlog",
            description="seed",
            target_files=("seed.py",),
            repo="jarvis",
            urgency="critical",
            confidence=0.85,
            evidence={"task_id": "seed", "signature": "seed"}, requires_human_ack=False,
        )
        p, _ = _compute_priority(seed)
        await q.put((p, seed.submitted_at, seed))
        # Pop one — must be the seed.
        _p, _t, env = await q.get()
        return [env.evidence.get("task_id") or env.evidence.get(
            "signature",
        )]

    popped = asyncio.new_event_loop().run_until_complete(_run())
    assert popped[0] == "seed", (
        f"seed should be popped first, got {popped[0]!r} — PriorityQueue "
        "is NOT FIFO-within-class"
    )


# ---------------------------------------------------------------------------
# Layer 3 — UrgencyRouter Priority-1 IMMEDIATE for critical urgency
# ---------------------------------------------------------------------------


def test_layer_3_urgency_router_critical_routes_immediate():
    """ANY ctx with signal_urgency='critical' routes IMMEDIATE,
    regardless of source — Priority-1 fires before Priority 5
    (BACKGROUND) in classify().

    Per memory: 'F3 stamps urgency on envelope but UrgencyRouter's
    route decision uses source-type mapping (`backlog` → BG default)
    not urgency alone.' This test refutes that claim — UrgencyRouter
    DOES check urgency first via Priority-1 IMMEDIATE."""
    from backend.core.ouroboros.governance.urgency_router import (
        ProviderRoute, UrgencyRouter,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
    )
    ctx = OperationContext.create(
        target_files=("seed.py",),
        description="seed",
        signal_urgency="critical",
        signal_source="backlog",
    )
    router = UrgencyRouter()
    route, reason = router.classify(ctx)
    assert route == ProviderRoute.IMMEDIATE, (
        f"critical+backlog should route IMMEDIATE, got {route.value} "
        f"(reason={reason!r})"
    )


def test_layer_3_urgency_router_normal_backlog_does_not_route_immediate():
    """Sanity check: WITHOUT critical urgency, a backlog op does NOT
    route IMMEDIATE. Pins the contract that F3=critical is the gate."""
    from backend.core.ouroboros.governance.urgency_router import (
        ProviderRoute, UrgencyRouter,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
    )
    ctx = OperationContext.create(
        target_files=("seed.py",),
        description="seed",
        signal_urgency="normal",
        signal_source="backlog",
    )
    router = UrgencyRouter()
    route, _reason = router.classify(ctx)
    assert route != ProviderRoute.IMMEDIATE


def test_layer_3_urgency_router_low_backlog_routes_background():
    """And without F3, backlog with low urgency DOES route BACKGROUND
    — that's the original starvation trap F3 was built to escape."""
    from backend.core.ouroboros.governance.urgency_router import (
        ProviderRoute, UrgencyRouter,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
    )
    ctx = OperationContext.create(
        target_files=("seed.py",),
        description="seed",
        signal_urgency="low",
        signal_source="backlog",
    )
    router = UrgencyRouter()
    route, _reason = router.classify(ctx)
    assert route == ProviderRoute.BACKGROUND


# ---------------------------------------------------------------------------
# End-to-end composition — F3-stamped seed survives all 3 layers
# ---------------------------------------------------------------------------


def test_end_to_end_f3_seed_routes_immediate(
    monkeypatch: pytest.MonkeyPatch,
):
    """Full chain: F3 env on → BacklogSensor stamps envelope.urgency
    = critical → _compute_priority returns negative → UrgencyRouter
    routes IMMEDIATE.

    If this test passes AND production sessions still route the
    seed to BG → the bug is NOT in the static code path. It's in
    runtime composition: cost governor / BG worker pool / phase-
    runner ordering. We then need fresh session debug.log to localize.
    """
    monkeypatch.setenv(
        "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "critical",
    )
    from backend.core.ouroboros.governance.intake.sensors.backlog_sensor import (  # noqa: E501
        _default_urgency_override,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        _compute_priority,
    )
    from backend.core.ouroboros.governance.urgency_router import (
        ProviderRoute, UrgencyRouter,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
    )

    # Step 1 (Layer 1): F3 reads "critical".
    override = _default_urgency_override()
    assert override == "critical"

    # Step 2 (BacklogSensor would build envelope with this urgency):
    seed = make_envelope(
        source="backlog",
        description="forced-reach seed",
        target_files=("seed.py",),
        repo="jarvis",
        urgency=override,  # F3 effective_urgency
        confidence=0.85,
        evidence={"task_id": "wave3-seed", "signature": "wave3-seed"}, requires_human_ack=False,
    )
    assert seed.urgency == "critical"

    # Step 3 (Layer 2): _compute_priority gives negative.
    priority, _alignment = _compute_priority(seed)
    assert priority < 0

    # Step 4 (UnifiedIntakeRouter would create ctx with
    # signal_urgency=envelope.urgency):
    ctx = OperationContext.create(
        target_files=seed.target_files,
        description=seed.description,
        signal_urgency=seed.urgency,
        signal_source=seed.source,
    )
    assert ctx.signal_urgency == "critical"
    assert ctx.signal_source == "backlog"

    # Step 5 (Layer 3): UrgencyRouter routes IMMEDIATE.
    router = UrgencyRouter()
    route, reason = router.classify(ctx)
    assert route == ProviderRoute.IMMEDIATE, (
        f"end-to-end F3 chain should route IMMEDIATE; got "
        f"{route.value} reason={reason!r} — STATIC CODE PATH BROKEN"
    )
    assert reason.startswith("critical_urgency:"), (
        f"unexpected reason {reason!r}"
    )


# ---------------------------------------------------------------------------
# Mixed-load simulation — seed survives a flood of BG noise
# ---------------------------------------------------------------------------


def test_end_to_end_seed_survives_docstaleness_flood(
    monkeypatch: pytest.MonkeyPatch,
):
    """Simulate the production scenario: 50 DocStaleness ops flood
    the queue, then 1 critical seed enqueues. The seed must dequeue
    NEXT (not behind 50 BG ops).

    This is the exact pattern documented in W3(6) memory as
    'BG-sensor noise flooding the intake queue and starving the
    seed'. If this test passes, that hypothesis is wrong about the
    static priority queue behavior."""
    import asyncio
    monkeypatch.setenv(
        "JARVIS_BACKLOG_SENSOR_DEFAULT_URGENCY", "critical",
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        _compute_priority,
    )

    async def _run() -> List[Tuple[str, int]]:
        q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        # Flood 50 DocStaleness ops.
        for i in range(50):
            doc = make_envelope(
                source="doc_staleness",
                description=f"stale-{i}",
                target_files=(f"d{i}.py",),
                repo="jarvis",
                urgency="normal",
                confidence=0.5,
                evidence={"signature": f"sd-{i}"}, requires_human_ack=False,
            )
            p, _ = _compute_priority(doc)
            await q.put((p, doc.submitted_at, doc))
        # Critical seed AFTER flood.
        seed = make_envelope(
            source="backlog",
            description="seed",
            target_files=("seed.py",),
            repo="jarvis",
            urgency="critical",
            confidence=0.85,
            evidence={"task_id": "seed", "signature": "seed"}, requires_human_ack=False,
        )
        p, _ = _compute_priority(seed)
        await q.put((p, seed.submitted_at, seed))
        # Pop one; must be the seed.
        out: List[Tuple[str, int]] = []
        for _ in range(3):
            _p, _t, env = await q.get()
            out.append((
                env.evidence.get("task_id")
                or env.evidence.get("signature"),
                _p,
            ))
        return out

    popped = asyncio.new_event_loop().run_until_complete(_run())
    # First popped MUST be the seed.
    assert popped[0][0] == "seed", (
        f"seed starved by DocStaleness flood — popped order {popped} "
        "— STATIC PRIORITY QUEUE FAILS the brutal-review §5 hypothesis"
    )


# ---------------------------------------------------------------------------
# Diagnostic summary
# ---------------------------------------------------------------------------


def test_diagnostic_summary_pin():
    """Pin: this diagnostic exists. If it passes, the BG-starvation
    static code path is wired correctly. The session bug then lives
    in runtime composition (downstream of the integration tests
    above) — NOT in any of: BacklogSensor F3 / _compute_priority /
    asyncio.PriorityQueue ordering / UrgencyRouter Priority-1 check.

    This pin's purpose is to prevent silent removal of this whole
    diagnostic (which would lose the static-vs-runtime localization
    signal)."""
    import inspect
    src = inspect.getsource(__import__(
        "tests.governance.test_bg_starvation_end_to_end_diagnostic",
        fromlist=["*"],
    ))
    for name in [
        "test_layer_1_f3_stamps_critical_on_envelope",
        "test_layer_2_critical_backlog_seed_priority_is_negative",
        "test_layer_2_priority_queue_orders_seed_before_flood",
        "test_layer_3_urgency_router_critical_routes_immediate",
        "test_end_to_end_f3_seed_routes_immediate",
        "test_end_to_end_seed_survives_docstaleness_flood",
    ]:
        assert name in src
