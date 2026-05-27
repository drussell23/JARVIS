"""Slice 33 Arc 1 — Posture Observer chunked-async signal collection.

Closes the v27 (``bt-2026-05-27-232749``) LoopSink-confirmed sink:
``[LoopSink] callsite=posture_observer.run_one_cycle kind=async
blocked_ms=22560.30`` — the 22.56 second cold-session block dwarfed
every other on-loop event by an order of magnitude.

# Root cause

``SignalCollector.build_bundle()`` ran 12 sync signal collectors
sequentially inside ONE ``asyncio.to_thread`` call. Each collector
performs sync I/O (git subprocess, session-dir iteration, file
reads). The whole bundle = one monolithic worker-thread block;
under GIL contention the asyncio main thread starved.

# Slice 33 Arc 1 fix

New ``SignalCollector.build_bundle_async()`` — same SignalBundle
return shape, but each of the 9 individual signal collectors is
dispatched in its own ``asyncio.to_thread`` call with an explicit
``await asyncio.sleep(0)`` cooperative yield between them. Per-signal
``LoopSink`` instrumentation (``posture.signal.<name>``) attributes
heavy individual collectors so v28 names which signal is hot.

``_collect_with_timeout`` now awaits ``build_bundle_async`` instead
of the monolithic ``to_thread(build_bundle)``. Sync ``build_bundle``
retained for backwards compat (tests + any other caller).

# Slice 33 Arc 1+ radar widening

Two additional ``LoopSink`` wires added to surface the still-unnamed
v27 post-boot sinks (5 of 6 ControlPlaneStarvation events had no
LoopSink correlation):

  * ``intake.UnifiedIntakeRouter.ingest`` — every sensor signal
    flows through this; if it's slow the loop notices.
  * ``oracle.OracleSemanticIndex.initialize_backend`` — ChromaDB
    lazy init is a known bootstrap heavyweight.

# Arc 2 scope refinement (NOT in this PR)

v27 LoopSink data showed ``semantic_index.SemanticIndex.build``
at 691ms — BUT tracing call-sites confirms the ONLY ``.build()``
caller is the daemon thread inside ``build_async()``. The 691ms
happened in a daemon thread, NOT on the asyncio main loop. Arc 2
(spawn-pool offload) targets a non-issue from main-loop starvation
perspective. Deferred pending v28 evidence.

# Test surface (3 AST + 6 spine = 9 tests)
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTURE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "posture_observer.py"
)
INTAKE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "intake"
    / "unified_intake_router.py"
)
ORACLE_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "oracle.py"


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_build_bundle_async_present_with_per_signal_wires() -> None:
    """``SignalCollector.build_bundle_async`` MUST exist as an async
    method AND its body MUST contain per-signal LoopSink wires (the
    ``posture.signal.<name>`` pattern). Without per-signal wires
    we lose attribution for v28."""
    src = POSTURE_FILE.read_text()
    tree = ast.parse(src, filename=str(POSTURE_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "build_bundle_async"
        ):
            body = ast.unparse(node)
            assert "loop_sink" in body or "sink_async" in body, (
                "build_bundle_async missing LoopSink import"
            )
            assert "posture.signal." in body, (
                "build_bundle_async missing per-signal callsite labels"
            )
            assert "asyncio.to_thread" in body or "to_thread" in body, (
                "build_bundle_async not dispatching signals to threads"
            )
            assert "sleep(0)" in body, (
                "build_bundle_async missing cooperative yields"
            )
            found = True
            break
    assert found, (
        "build_bundle_async not found — Slice 33 Arc 1 substrate missing"
    )
    assert "Slice 33 Arc 1" in src, (
        "posture_observer.py missing Slice 33 Arc 1 attribution"
    )


def test_ast_pin_collect_with_timeout_awaits_async_builder() -> None:
    """``_collect_with_timeout`` MUST await ``build_bundle_async``,
    NOT the legacy monolithic ``to_thread(build_bundle)``. Without
    this swap the 22.56 s sink re-opens."""
    src = POSTURE_FILE.read_text()
    tree = ast.parse(src, filename=str(POSTURE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_collect_with_timeout"
        ):
            body = ast.unparse(node)
            assert "build_bundle_async" in body, (
                "_collect_with_timeout doesn't call build_bundle_async — "
                "Slice 33 Arc 1 wiring incomplete; v27 sink re-opens"
            )
            assert "to_thread(self._collector.build_bundle)" not in body, (
                "_collect_with_timeout still uses legacy "
                "to_thread(build_bundle) path"
            )
            return
    pytest.fail("_collect_with_timeout not found in posture_observer.py")


def test_ast_pin_arc1_plus_radar_widening_three_new_wires() -> None:
    """Slice 33 Arc 1+ widens LoopSink coverage by 3 new wires:
    posture per-signal (covered by build_bundle_async pin above),
    intake.UnifiedIntakeRouter.ingest, and
    oracle.OracleSemanticIndex.initialize_backend."""
    intake_src = INTAKE_FILE.read_text()
    assert "intake.UnifiedIntakeRouter.ingest" in intake_src, (
        "intake.ingest LoopSink callsite label missing"
    )
    assert "loop_sink" in intake_src or "sink_async" in intake_src, (
        "intake module missing loop_sink import"
    )

    oracle_src = ORACLE_FILE.read_text()
    assert (
        "oracle.OracleSemanticIndex.initialize_backend" in oracle_src
    ), (
        "ChromaDB initialize_backend LoopSink callsite label missing"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 6
# ──────────────────────────────────────────────────────────────────────


def test_spine_build_bundle_async_parity_with_sync() -> None:
    """``build_bundle_async`` MUST return a SignalBundle with the
    same field shape as the legacy sync ``build_bundle``. Refactor
    must NOT alter what the inferrer sees."""
    from backend.core.ouroboros.governance.posture_observer import (
        SignalCollector,
    )
    c = SignalCollector(Path.cwd())
    sync_bundle = c.build_bundle()
    async_bundle = asyncio.run(c.build_bundle_async())
    sync_d = (
        sync_bundle.__dict__ if hasattr(sync_bundle, "__dict__")
        else dict(getattr(sync_bundle, "_asdict", lambda: {})())
    )
    async_d = (
        async_bundle.__dict__ if hasattr(async_bundle, "__dict__")
        else dict(getattr(async_bundle, "_asdict", lambda: {})())
    )
    assert set(sync_d.keys()) == set(async_d.keys()), (
        f"field shape diverged: sync={sorted(sync_d)} "
        f"async={sorted(async_d)}"
    )
    # All numeric/structural fields must equal (timestamps may differ
    # by microseconds; we compare deterministic fields)
    for field in (
        "feat_ratio", "fix_ratio", "refactor_ratio", "test_docs_ratio",
        "commit_window", "postmortem_window_h", "schema_version",
    ):
        assert sync_d.get(field) == async_d.get(field), (
            f"deterministic field {field} diverged: "
            f"sync={sync_d.get(field)} async={async_d.get(field)}"
        )


def test_spine_build_bundle_async_is_actually_async() -> None:
    from backend.core.ouroboros.governance.posture_observer import (
        SignalCollector,
    )
    assert asyncio.iscoroutinefunction(
        SignalCollector.build_bundle_async,
    ), "build_bundle_async must be async"


def test_spine_main_loop_stays_responsive_during_build_bundle() -> None:
    """The key Slice 33 Arc 1 contract: while ``build_bundle_async``
    is running, the asyncio main loop MUST keep ticking. Heartbeat
    sibling coroutine increments a counter every 10ms; over the
    bundle build the counter MUST grow — proving cooperative yields
    work. This is the structural inverse of the v27 22.56 s wedge."""
    from backend.core.ouroboros.governance.posture_observer import (
        SignalCollector,
    )

    async def run():
        tick_count = 0

        async def heartbeat():
            nonlocal tick_count
            while True:
                tick_count += 1
                await asyncio.sleep(0.01)

        hb = asyncio.create_task(heartbeat())
        try:
            c = SignalCollector(Path.cwd())
            await c.build_bundle_async()
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
        return tick_count

    ticks = asyncio.run(run())
    # 9 signals × cooperative yields → heartbeat should fire many
    # times. Threshold is generous (5+) but proves the loop wasn't
    # wedged. If the loop wedged we'd see 0-1 ticks.
    assert ticks >= 5, (
        f"main loop wedged during build_bundle_async — only {ticks} "
        f"heartbeat tick(s). v27 22.56 s wedge is back."
    )


def test_spine_intake_ingest_wraps_in_sink_async() -> None:
    """``UnifiedIntakeRouter.ingest`` MUST be wrapped in
    ``sink_async(intake.UnifiedIntakeRouter.ingest)``. AST-walk
    verifies the wiring at the function body."""
    src = INTAKE_FILE.read_text()
    tree = ast.parse(src, filename=str(INTAKE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "ingest"
        ):
            body = ast.unparse(node)
            assert "sink_async" in body, (
                "ingest body missing sink_async wrapper"
            )
            assert "intake.UnifiedIntakeRouter.ingest" in body, (
                "ingest body missing callsite label"
            )
            return
    pytest.fail("UnifiedIntakeRouter.ingest not located")


def test_spine_oracle_initialize_backend_wraps_in_sink_async() -> None:
    """``OracleSemanticIndex.initialize_backend`` MUST be wrapped
    in ``sink_async``. ChromaDB lazy init is a known boot heavyweight
    and we need attribution for v28."""
    src = ORACLE_FILE.read_text()
    tree = ast.parse(src, filename=str(ORACLE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "initialize_backend"
        ):
            body = ast.unparse(node)
            assert "sink_async" in body
            assert "oracle.OracleSemanticIndex.initialize_backend" in body
            return
    pytest.fail("OracleSemanticIndex.initialize_backend not located")


def test_spine_per_signal_wires_use_unique_callsite_labels() -> None:
    """Each of the 9 per-signal LoopSink wires inside
    ``build_bundle_async`` MUST use a unique callsite label so the
    v28 leaderboard can attribute heavy collectors precisely."""
    src = POSTURE_FILE.read_text()
    # The 9 signal callsite labels expected (matches the wires we
    # added — must stay synchronized with the body)
    expected = {
        "posture.signal.commit_ratios",
        "posture.signal.postmortem_failure_rate",
        "posture.signal.iron_gate_reject_rate",
        "posture.signal.l2_repair_rate",
        "posture.signal.open_ops_normalized",
        "posture.signal.session_lessons_infra_ratio",
        "posture.signal.time_since_last_graduation_inv",
        "posture.signal.cost_burn_normalized",
        "posture.signal.worktree_orphan_count",
    }
    missing = {label for label in expected if label not in src}
    assert not missing, (
        f"build_bundle_async missing per-signal labels: {sorted(missing)}"
    )
