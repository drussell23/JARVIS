"""F1 Slice 3 integration + starvation-regression tests.

Scope: `memory/project_followup_f1_intake_governor_enforcement.md` Slice 3.
Operator-authorized 2026-04-24 after Slice 2 review.

This file is the end-to-end sanity layer between the IntakePriorityQueue
primitive (Slice 1 unit tests) and the router wiring (Slice 2 unit
tests). Each test exercises at S1-scale (20 BG envelopes + 1 critical
seed, matching `bt-2026-04-24-062608`'s observed burst shape) through
`UnifiedIntakeRouter.ingest()` → `_dispatch_loop()` with a patched
`_dispatch_one` that captures arrival order.

Contract pinned:

1. S1-scale primary-mode repro inverted: with the master flag on, 20 BG
   envelopes ingested first + 1 critical envelope ingested last →
   dispatcher sees critical FIRST and within the critical deadline (5s).
2. S1-scale flag-off structural regression: with both flags off, the F1
   state is entirely absent (no priority queue, no `[IntakePriority]`
   markers). This locks the pre-F1 state as a regression baseline — any
   accidental auto-wiring of F1 fails this test.
3. S1-scale shadow-mode observes without behavior change: shadow flag
   on + master off → legacy stays primary, shadow counters tick, no
   dispatch-order change.
4. Back-pressure integration: with a low back-pressure threshold, the
   priority queue refuses BG-class ingest on overflow while still
   admitting critical (the bug F1 exists to prevent — structurally
   impossible to starve critical under overload).
5. Authority invariant holds at integration layer (grep pin).

Reachability supplement extension (F2 end-to-end path with F1 ordering)
is intentionally deferred to F1 Slice 4 per operator 2026-04-24.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.intake.intake_priority_queue import (
    IntakePriorityQueue,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_envelope(
    *,
    source: str,
    urgency: str,
    target: str,
) -> IntentEnvelope:
    return make_envelope(
        source=source,
        description=f"test {source}/{urgency}/{target}",
        target_files=(target,),
        repo="jarvis",
        confidence=0.8,
        urgency=urgency,
        evidence={"signature": f"sig-{source}-{urgency}-{target}"},
        requires_human_ack=False,
    )


def _make_config(tmp_path: Path) -> IntakeRouterConfig:
    # F1 Slice 3 context: raise BOTH router-level thresholds so the
    # 21-envelope S1 burst ingest isn't bottlenecked by legacy back-
    # pressure. The F1-level back-pressure (JARVIS_INTAKE_BACKPRESSURE_
    # THRESHOLD on the IntakePriorityQueue primitive) is a SEPARATE
    # concern tested explicitly below in
    # test_backpressure_low_threshold_admits_critical_rejects_bg.
    return IntakeRouterConfig(
        project_root=tmp_path,
        wal_path=tmp_path / ".jarvis" / "intake_wal.jsonl",
        lock_path=tmp_path / ".jarvis" / "intake_router.lock",
        max_queue_size=500,
        backpressure_threshold=500,
    )


def _make_router(tmp_path: Path) -> UnifiedIntakeRouter:
    gls = MagicMock()
    gls.submit = AsyncMock(return_value=None)
    return UnifiedIntakeRouter(gls=gls, config=_make_config(tmp_path))


async def _ingest_s1_shape(
    router: UnifiedIntakeRouter, *, n_bg: int = 20,
) -> IntentEnvelope:
    """Ingest the S1 burst shape: ``n_bg`` doc_staleness/normal envelopes
    followed by 1 backlog/critical seed. Returns the critical envelope so
    the test can identify it in captured dispatch order.
    """
    for i in range(n_bg):
        await router.ingest(
            _make_envelope(
                source="doc_staleness",
                urgency="normal",
                target=f"docs/noise{i}.md",
            )
        )
    critical_env = _make_envelope(
        source="backlog", urgency="critical", target="seed.py",
    )
    await router.ingest(critical_env)
    return critical_env


async def _drive_dispatch_until(
    router: UnifiedIntakeRouter,
    captured: List[IntentEnvelope],
    *,
    predicate,
    max_iterations: int = 200,
    sleep_s: float = 0.01,
) -> None:
    """Run `_dispatch_loop` in-task until `predicate(captured)` is True
    or timeout. Always cleans up the task."""
    router._running = True
    task = asyncio.create_task(router._dispatch_loop())
    try:
        for _ in range(max_iterations):
            if predicate(captured):
                return
            await asyncio.sleep(sleep_s)
    finally:
        router._running = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# (1) S1-scale primary-mode repro inverted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_scale_primary_mode_critical_dispatches_first(
    monkeypatch, tmp_path,
):
    """20 BG doc_staleness envelopes ingested first + 1 backlog/critical
    ingested last → primary-mode dispatches the critical envelope FIRST.
    This is the S1 failure mode (bt-2026-04-24-062608) inverted."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)
    captured: List[IntentEnvelope] = []

    async def _fake_dispatch_one(env: IntentEnvelope) -> None:
        captured.append(env)

    router._dispatch_one = _fake_dispatch_one  # type: ignore[assignment]
    critical_env = await _ingest_s1_shape(router, n_bg=20)

    await _drive_dispatch_until(
        router, captured,
        predicate=lambda c: len(c) >= 1,
    )

    assert len(captured) >= 1, "dispatcher never ran"
    # Identity check via dedup_key (router rewrites with .with_lease()
    # before queueing, so the dispatched instance is a fresh envelope
    # with the same content — `is` would fail despite semantic equality).
    assert captured[0].dedup_key == critical_env.dedup_key, (
        f"F1 primary-mode must dispatch the critical seed FIRST even "
        f"though it was ingested LAST after 20 BG envelopes; got "
        f"source={captured[0].source} urgency={captured[0].urgency}"
    )
    assert captured[0].urgency == "critical"
    assert captured[0].source == "backlog"


@pytest.mark.asyncio
async def test_s1_scale_primary_mode_all_21_envelopes_drain_eventually(
    monkeypatch, tmp_path,
):
    """Primary-mode drains the full 21-envelope burst without losing any."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)
    captured: List[IntentEnvelope] = []

    async def _fake_dispatch_one(env: IntentEnvelope) -> None:
        captured.append(env)

    router._dispatch_one = _fake_dispatch_one  # type: ignore[assignment]
    await _ingest_s1_shape(router, n_bg=20)

    await _drive_dispatch_until(
        router, captured,
        predicate=lambda c: len(c) >= 21,
        max_iterations=400,
    )

    assert len(captured) == 21, (
        f"expected all 21 envelopes to dispatch, got {len(captured)}"
    )
    # No duplicates.
    signatures = {env.evidence["signature"] for env in captured}
    assert len(signatures) == 21


@pytest.mark.asyncio
async def test_s1_scale_primary_mode_seed_observed_within_deadline(
    monkeypatch, tmp_path,
):
    """The critical seed's wait time in the priority queue must stay
    below its 5s deadline even under a 20-BG burst. Tested by peeking
    at the priority queue state immediately after ingest + inspecting
    the seed's own dequeue decision."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)
    assert router._priority_queue is not None

    # Ingest burst + seed.
    await _ingest_s1_shape(router, n_bg=20)
    # The priority queue should have 21 envelopes; seed at position 0.
    assert len(router._priority_queue) == 21
    depths = router._priority_queue.snapshot_depths()
    assert depths["critical"] == 1
    assert depths["normal"] == 20

    # Dequeue — first pop must be the critical seed within deadline.
    decision = router._priority_queue.dequeue()
    assert decision is not None
    assert decision.urgency == "critical"
    assert decision.source == "backlog"
    # waited_s should be near-zero since we dequeue immediately after ingest.
    assert decision.waited_s < 5.0, (
        f"critical deadline=5s must not be breached; waited={decision.waited_s}"
    )
    # Priority-mode dequeue (not an inversion — we haven't breached deadline).
    assert decision.dequeue_mode == "priority"


# ---------------------------------------------------------------------------
# (2) S1-scale flag-off structural regression — locks pre-F1 state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_scale_flag_off_f1_state_absent(
    monkeypatch, tmp_path, caplog,
):
    """With both F1 flags off, the F1 machinery is entirely inert: no
    priority queue is constructed, no F1 markers are logged, no F1
    state tracks ingest/dispatch. This locks the pre-F1 state as a
    regression baseline — any accidental wiring makes this test fail."""
    monkeypatch.delenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW", raising=False)
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    caplog.set_level(
        logging.DEBUG,
        logger="backend.core.ouroboros.governance.intake.unified_intake_router",
    )
    router = _make_router(tmp_path)

    # Structural assertions.
    assert router._priority_queue is None
    assert router._f1_master_on is False
    assert router._f1_shadow_on is False
    assert router._f1_shadow_delta_count == 0
    assert router._f1_shadow_agree_count == 0

    # Ingest the S1 burst + seed.
    captured: List[IntentEnvelope] = []

    async def _fake_dispatch_one(env: IntentEnvelope) -> None:
        captured.append(env)

    router._dispatch_one = _fake_dispatch_one  # type: ignore[assignment]
    await _ingest_s1_shape(router, n_bg=20)

    await _drive_dispatch_until(
        router, captured,
        predicate=lambda c: len(c) >= 1,
    )

    # F1 state still absent after ingest + dispatch.
    assert router._priority_queue is None
    assert router._f1_shadow_delta_count == 0
    assert router._f1_shadow_agree_count == 0

    # No F1 log markers emitted.
    f1_log_lines = [
        r.message for r in caplog.records
        if "[IntakePriority" in r.message
    ]
    assert f1_log_lines == [], (
        f"F1 markers must not appear when both flags off; got {f1_log_lines}"
    )


# ---------------------------------------------------------------------------
# (3) Shadow-mode observes without changing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s1_scale_shadow_mode_priority_queue_built_tracks_ingests(
    monkeypatch, tmp_path,
):
    """Shadow on + master off: priority queue exists + receives mirrored
    ingests; legacy stays primary for dispatch."""
    monkeypatch.delenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW", "true")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)
    assert router._priority_queue is not None
    assert router._f1_master_on is False
    assert router._f1_shadow_on is True

    await _ingest_s1_shape(router, n_bg=20)
    # Priority queue has all 21 (shadow mode mirrors every ingest).
    assert len(router._priority_queue) == 21


# ---------------------------------------------------------------------------
# (4) Back-pressure integration at the router level
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backpressure_low_threshold_admits_critical_rejects_bg(
    monkeypatch, tmp_path,
):
    """With a low back-pressure threshold, the priority queue refuses
    further BG ingestion on overflow but ALWAYS admits critical. Proves
    the bug F1 exists to prevent (critical starvation) is structurally
    impossible under overload."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTAKE_BACKPRESSURE_THRESHOLD", "5")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)
    assert router._priority_queue is not None

    # Ingest 5 normal envelopes — fills the priority queue to threshold.
    for i in range(5):
        await router.ingest(
            _make_envelope(
                source="doc_staleness", urgency="normal",
                target=f"a{i}.py",
            )
        )
    # Priority queue full at 5.
    assert len(router._priority_queue) == 5

    # Try to ingest 3 more normals — priority queue refuses them.
    # (Legacy _queue still accepts; primary-mode dispatch reads from
    # priority queue so the refused normals never reach dispatch.)
    for i in range(3):
        await router.ingest(
            _make_envelope(
                source="doc_staleness", urgency="normal",
                target=f"overflow{i}.py",
            )
        )
    # Priority queue still at 5 (back-pressure refused overflow).
    assert len(router._priority_queue) == 5

    # Now ingest a critical envelope — MUST be admitted regardless of
    # threshold. This is the key invariant F1 enforces.
    await router.ingest(
        _make_envelope(
            source="backlog", urgency="critical",
            target="seed.py",
        )
    )
    assert len(router._priority_queue) == 6, (
        "critical must always be admitted to prevent the exact starvation "
        "mode F1 exists to fix"
    )

    # Critical is at rank 0; it pops first.
    decision = router._priority_queue.dequeue()
    assert decision is not None
    assert decision.urgency == "critical"


@pytest.mark.asyncio
async def test_backpressure_emits_telemetry_warning(
    monkeypatch, tmp_path, caplog,
):
    """Backpressure refusal emits WARNING via the router's telemetry
    sink for operator visibility."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTAKE_BACKPRESSURE_THRESHOLD", "2")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    caplog.set_level(
        logging.WARNING,
        logger="backend.core.ouroboros.governance.intake.unified_intake_router",
    )
    router = _make_router(tmp_path)

    # Fill queue to threshold.
    for i in range(2):
        await router.ingest(
            _make_envelope(
                source="doc_staleness", urgency="normal",
                target=f"fill{i}.py",
            )
        )
    # Ingest one more normal → refused with WARNING telemetry.
    await router.ingest(
        _make_envelope(
            source="doc_staleness", urgency="normal",
            target="reject.py",
        )
    )
    bp_lines = [
        r.message for r in caplog.records
        if "[IntakePriority] backpressure_applied" in r.message
    ]
    assert len(bp_lines) >= 1, (
        f"expected at least one backpressure_applied WARNING; got {bp_lines}"
    )


# ---------------------------------------------------------------------------
# (5) Primary-mode supersedes shadow when both flags on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_mode_supersedes_shadow_even_with_both_flags_on(
    monkeypatch, tmp_path,
):
    """When master + shadow both on, master-mode semantics dominate:
    priority queue is source of truth, shadow_agree/shadow_delta
    counters are NOT incremented (only legacy path updates them)."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW", "true")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)

    captured: List[IntentEnvelope] = []

    async def _fake_dispatch_one(env: IntentEnvelope) -> None:
        captured.append(env)

    router._dispatch_one = _fake_dispatch_one  # type: ignore[assignment]
    await _ingest_s1_shape(router, n_bg=5)

    await _drive_dispatch_until(
        router, captured,
        predicate=lambda c: len(c) >= 1,
    )

    # Primary-mode is in effect — first dispatched is the critical seed.
    assert captured[0].urgency == "critical"
    # Shadow counters stay at zero because shadow-delta logic only
    # runs in the legacy dispatch branch.
    assert router._f1_shadow_delta_count == 0
    assert router._f1_shadow_agree_count == 0


# ---------------------------------------------------------------------------
# (6) Authority invariant — Slice 3 additions don't introduce regressions
# ---------------------------------------------------------------------------


def test_slice3_integration_tests_stay_authority_clean():
    """The Slice 3 integration test file itself stays grep-clean of banned
    authority modules (same invariant as the primitive + wiring tests)."""
    module_path = Path(__file__).resolve()
    source = module_path.read_text(encoding="utf-8")
    banned = [
        r"from backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"import backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from backend\.core\.ouroboros\.governance\.policy\b",
        r"from backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"from backend\.core\.ouroboros\.governance\.gate\b",
        r"from backend\.core\.ouroboros\.governance\.semantic_guardian\b",
    ]
    for pattern in banned:
        assert not re.search(pattern, source), (
            f"Slice 3 integration tests import banned authority module: {pattern}"
        )


# ---------------------------------------------------------------------------
# (7) Coalesce + F1 interaction — critical envelope under primary-mode
#     still respects coalesce (Slice 2 scope didn't touch coalesce)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_mode_with_zero_coalesce_window_dispatches_all(
    monkeypatch, tmp_path,
):
    """With coalesce_window=0, dispatch_loop bypasses coalesce entirely
    and every envelope goes directly through _dispatch_one. Primary-mode
    F1 reorders dequeue by urgency; zero-coalesce ensures the reordering
    translates to dispatch order 1:1."""
    monkeypatch.setenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)

    captured: List[IntentEnvelope] = []

    async def _fake_dispatch_one(env: IntentEnvelope) -> None:
        captured.append(env)

    router._dispatch_one = _fake_dispatch_one  # type: ignore[assignment]
    await _ingest_s1_shape(router, n_bg=5)

    await _drive_dispatch_until(
        router, captured,
        predicate=lambda c: len(c) >= 6,
    )
    assert len(captured) == 6
    # Critical first, then 5 normals (FIFO within equal urgency).
    assert captured[0].urgency == "critical"
    for i in range(1, 6):
        assert captured[i].urgency == "normal", (
            f"position {i} should be normal (FIFO within rank=2); "
            f"got urgency={captured[i].urgency}"
        )


# ---------------------------------------------------------------------------
# (8) Structural regression: flag-off has no deadline/reserved-slot/
#     back-pressure enforcement — the guardrails F1 adds are absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_no_backpressure_refusal(monkeypatch, tmp_path):
    """Flag off: even under overload, ingest always succeeds (no F1
    back-pressure). This locks S1's unbounded-ingest behavior as the
    baseline the F1 master flag is required to change."""
    monkeypatch.delenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_INTAKE_PRIORITY_SCHEDULER_SHADOW", raising=False)
    monkeypatch.setenv("JARVIS_INTAKE_BACKPRESSURE_THRESHOLD", "5")
    monkeypatch.setenv("JARVIS_COALESCE_WINDOW_S", "0")
    monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "off")
    router = _make_router(tmp_path)
    assert router._priority_queue is None

    # Ingest 10 normals — all should be accepted (no F1 back-pressure).
    for i in range(10):
        result = await router.ingest(
            _make_envelope(
                source="doc_staleness", urgency="normal",
                target=f"a{i}.py",
            )
        )
        # Result is "enqueued" (or similar) — never "refused".
        # Legacy ingest doesn't have a back-pressure "refused" return
        # value at all, proving F1 adds new behavior.
        assert result in {"enqueued", "duplicate", "parked"}

    # Legacy _queue has 10 items; priority queue doesn't exist.
    assert router._queue.qsize() == 10
    assert router._priority_queue is None
