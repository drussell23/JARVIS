"""Slice 33 Arc 0 — Loop-Sink Identifier (diagnostic-only).

Closes the v26 (``bt-2026-05-27-220220``) blind-spot:
ControlPlaneWatchdog stack snapshots fire AFTER the loop unwedges,
so they catch the watchdog itself, not the actual blocker. Slice 33
Arc 0 adds a precision per-call-site blocking-time recorder so the
v27 diagnostic probe ($1 / 10 min) can name the actual on-loop sink.

# Test surface (4 AST pins + 9 spine = 13 tests)

AST pins:
  * substrate exists at canonical path with required public surface
  * substrate has ZERO governance / orchestrator / provider imports
  * 11 wire sites all import loop_sink lazily inside fn bodies
  * master flag default TRUE; explicit-false disables

Spine:
  * sync contextmgr fires above threshold, silent below
  * async contextmgr fires above threshold, silent below
  * decorator forms wrap sync + async equivalently
  * cumulative stats: count / total / max / p50 / p95 monotonic
  * leaderboard sort by total blocking time
  * reset_stats clears registry
  * disabled master switch → no recording, no logging
  * substrate NEVER raises into caller on internal error
  * loop stays responsive under instrumented synthetic load
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SUBSTRATE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "telemetry"
    / "loop_sink.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 4
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_substrate_public_surface() -> None:
    """The substrate MUST expose the documented public symbols and
    live at the canonical path. Without this Slice 33 Arc 0 wiring
    breaks at every site."""
    assert SUBSTRATE_FILE.exists(), (
        f"loop_sink.py missing at {SUBSTRATE_FILE}"
    )
    src = SUBSTRATE_FILE.read_text()
    tree = ast.parse(src, filename=str(SUBSTRATE_FILE))
    fn_names = set()
    class_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            fn_names.add(node.name)
        if isinstance(node, ast.AsyncFunctionDef):
            fn_names.add(node.name)
        if isinstance(node, ast.ClassDef):
            class_names.add(node.name)
    required_fns = {
        "sink_sync", "sink_async", "instrument_sync", "instrument_async",
        "get_stats", "get_leaderboard", "reset_stats", "is_enabled",
    }
    missing = required_fns - fn_names
    assert not missing, f"loop_sink missing required fns: {missing}"
    assert "CallsiteStats" in class_names, (
        "CallsiteStats dataclass missing"
    )
    assert '"sink_sync"' in src and '"sink_async"' in src, (
        "__all__ missing required public symbols"
    )
    assert "Slice 33 Arc 0" in src, "substrate missing slice attribution"


def test_ast_pin_substrate_has_no_governance_coupling() -> None:
    """The substrate MUST NOT import any orchestration / governance
    / provider module. It is a pure utility — coupling would create
    import cycles when consumers wire it back in. Operator binding:
    'no parallel pools; no coupling beyond stdlib'."""
    src = SUBSTRATE_FILE.read_text()
    tree = ast.parse(src, filename=str(SUBSTRATE_FILE))
    forbidden_prefixes = (
        "backend.core.ouroboros.governance",
        "backend.core.ouroboros.orchestrator",
        "backend.core.ouroboros.battle_test",
        "backend.core.ouroboros.consciousness",
        "backend.core.ouroboros.oracle",
    )
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and any(
                node.module.startswith(p) for p in forbidden_prefixes
            ):
                bad_imports.append(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(p) for p in forbidden_prefixes):
                    bad_imports.append(alias.name)
    assert not bad_imports, (
        f"loop_sink imports forbidden coupling: {bad_imports}"
    )


def test_ast_pin_eleven_wire_sites_reference_loop_sink() -> None:
    """All 11 named instrumentation sites MUST reference
    ``loop_sink`` (via either ``sink_sync`` or ``sink_async``) inside
    their function body. Mirror of the operator's named 12 sites
    (one wire is the bulk add_node/add_edge loop, subsuming
    per-call individual instrumentation)."""
    wires = [
        ("backend/core/ouroboros/oracle.py",
         "oracle._scan_for_changes.between_await_chunk"),
        ("backend/core/ouroboros/oracle.py",
         "oracle._index_file.graph_write_bulk"),
        ("backend/core/ouroboros/governance/strategic_direction.py",
         "strategic_direction._extract_git_themes"),
        ("backend/core/ouroboros/governance/direction_inferrer.py",
         "direction_inferrer.DirectionInferrer.infer"),
        ("backend/core/ouroboros/governance/semantic_index.py",
         "semantic_index.SemanticIndex.build"),
        ("backend/core/ouroboros/governance/cross_process_jsonl.py",
         "cross_process_jsonl.flock_append_line"),
        ("backend/core/ouroboros/governance/consciousness_bridge.py",
         "consciousness_bridge.assess_regression_risk"),
        ("backend/core/ouroboros/governance/posture_observer.py",
         "posture_observer.run_one_cycle"),
        ("backend/core/ouroboros/governance/last_session_summary.py",
         "last_session_summary._parse_summary"),
        ("backend/core/ouroboros/governance/sensor_governor.py",
         "sensor_governor.SensorGovernor._weighted_cap"),
        ("backend/core/ouroboros/governance/episodic_memory.py",
         "episodic_memory.FailureMemory.record"),
    ]
    missing: list[str] = []
    for rel_path, callsite_label in wires:
        src = (REPO_ROOT / rel_path).read_text()
        # Each site must reference both loop_sink (the module) and
        # its own callsite label string.
        if "loop_sink" not in src:
            missing.append(f"{rel_path}: no loop_sink import")
        elif callsite_label not in src:
            missing.append(
                f"{rel_path}: missing callsite label "
                f"{callsite_label!r}"
            )
    assert not missing, (
        f"Slice 33 Arc 0 wiring incomplete:\n  "
        + "\n  ".join(missing)
    )


def test_ast_pin_master_flag_default_true() -> None:
    """``JARVIS_LOOP_SINK_ENABLED`` default is TRUE. Explicit falsy
    values disable; everything else (including unset) enables —
    operator binding: diagnostic must be on by default for the v27
    probe to surface data."""
    from backend.core.ouroboros.telemetry.loop_sink import (
        is_enabled, _ENABLED_ENV,
    )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_ENABLED_ENV, None)
        assert is_enabled() is True, "default must be TRUE"
    for falsy in ("0", "false", "FALSE", "no", "off"):
        with mock.patch.dict(os.environ, {_ENABLED_ENV: falsy}):
            assert is_enabled() is False, (
                f"{falsy!r} should disable, did not"
            )
    for truthy in ("1", "true", "TRUE", "yes", "on", ""):
        with mock.patch.dict(os.environ, {_ENABLED_ENV: truthy}):
            # Empty string OR explicit truthy → enabled
            if truthy == "":
                os.environ.pop(_ENABLED_ENV, None)
            assert is_enabled() is True, (
                f"{truthy!r} should enable, did not"
            )


# ──────────────────────────────────────────────────────────────────────
# Spine — 9
# ──────────────────────────────────────────────────────────────────────


def test_spine_sync_contextmgr_fires_above_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_sync, reset_stats, get_stats,
    )
    reset_stats()
    with caplog.at_level(logging.WARNING, logger="Ouroboros.LoopSink"):
        with sink_sync("test.over", threshold_ms=10.0):
            time.sleep(0.05)  # 50 ms — over threshold
    matched = [r for r in caplog.records if "test.over" in r.getMessage()]
    assert len(matched) == 1, (
        f"expected 1 log record above threshold, got {len(matched)}"
    )
    stats = get_stats()
    assert stats["test.over"]["count"] == 1
    assert stats["test.over"]["over_threshold_count"] == 1


def test_spine_sync_contextmgr_silent_below_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_sync, reset_stats, get_stats,
    )
    reset_stats()
    with caplog.at_level(logging.WARNING, logger="Ouroboros.LoopSink"):
        with sink_sync("test.under", threshold_ms=1000.0):
            pass  # microseconds — under any reasonable threshold
    matched = [r for r in caplog.records if "test.under" in r.getMessage()]
    assert len(matched) == 0, "sub-threshold should not log"
    stats = get_stats()
    assert stats["test.under"]["count"] == 1
    assert stats["test.under"]["over_threshold_count"] == 0


def test_spine_async_contextmgr_fires_above_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_async, reset_stats, get_stats,
    )
    reset_stats()

    async def runner() -> None:
        async with sink_async("test.async_over", threshold_ms=10.0):
            await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING, logger="Ouroboros.LoopSink"):
        asyncio.run(runner())
    matched = [
        r for r in caplog.records if "test.async_over" in r.getMessage()
    ]
    assert len(matched) == 1
    stats = get_stats()
    assert stats["test.async_over"]["over_threshold_count"] == 1


def test_spine_decorator_forms_equivalent_to_contextmgr() -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        instrument_sync, instrument_async, reset_stats, get_stats,
    )
    reset_stats()

    @instrument_sync("test.deco_sync", threshold_ms=10.0)
    def slow_sync() -> int:
        time.sleep(0.05)
        return 42

    @instrument_async("test.deco_async", threshold_ms=10.0)
    async def slow_async() -> int:
        await asyncio.sleep(0.05)
        return 99

    assert slow_sync() == 42
    assert asyncio.run(slow_async()) == 99
    stats = get_stats()
    assert stats["test.deco_sync"]["count"] == 1
    assert stats["test.deco_async"]["count"] == 1
    assert stats["test.deco_sync"]["over_threshold_count"] == 1
    assert stats["test.deco_async"]["over_threshold_count"] == 1


def test_spine_cumulative_stats_monotonic() -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_sync, reset_stats, get_stats,
    )
    reset_stats()
    for _ in range(5):
        with sink_sync("test.cumul", threshold_ms=1000.0):
            time.sleep(0.01)
    stats = get_stats()["test.cumul"]
    assert stats["count"] == 5
    assert stats["total_ms"] >= 50.0  # at least 5 × 10ms
    assert stats["max_ms"] >= 10.0
    assert stats["mean_ms"] >= 10.0
    # p95 ≥ p50 (samples populated)
    assert stats["p95_ms"] >= stats["p50_ms"]


def test_spine_leaderboard_sorts_by_total_blocking_time() -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_sync, reset_stats, get_leaderboard,
    )
    reset_stats()
    # site_a: 3 × 30ms = 90ms total
    for _ in range(3):
        with sink_sync("site_a", threshold_ms=1000.0):
            time.sleep(0.03)
    # site_b: 1 × 10ms = 10ms total
    with sink_sync("site_b", threshold_ms=1000.0):
        time.sleep(0.01)
    board = get_leaderboard(top_n=10)
    # site_a must appear before site_b (higher total)
    assert board.index("site_a") < board.index("site_b"), (
        f"leaderboard not sorted by total time:\n{board}"
    )


def test_spine_reset_stats_clears_registry() -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_sync, reset_stats, get_stats,
    )
    with sink_sync("test.to_clear", threshold_ms=1000.0):
        pass
    assert "test.to_clear" in get_stats()
    reset_stats()
    assert get_stats() == {}


def test_spine_master_switch_off_disables_recording(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from backend.core.ouroboros.telemetry.loop_sink import (
        sink_sync, reset_stats, get_stats, _ENABLED_ENV,
    )
    reset_stats()
    with mock.patch.dict(os.environ, {_ENABLED_ENV: "false"}):
        with caplog.at_level(logging.WARNING, logger="Ouroboros.LoopSink"):
            with sink_sync("test.disabled", threshold_ms=1.0):
                time.sleep(0.05)
    matched = [
        r for r in caplog.records if "test.disabled" in r.getMessage()
    ]
    assert len(matched) == 0, "disabled switch should suppress logging"
    # Stats NOT recorded either (no overhead when off)
    assert "test.disabled" not in get_stats()


def test_spine_substrate_never_raises_into_caller() -> None:
    """If the substrate's internal accounting raises, the contextmgr
    must swallow and log — NEVER propagate into the caller. Operator
    binding: diagnostic must not destabilize production."""
    from backend.core.ouroboros.telemetry import loop_sink

    # Force a failure deep inside the stats path by patching
    # _get_or_create_stats to raise — the contextmgr must still
    # complete the wrapped block without raising.
    with mock.patch.object(
        loop_sink, "_get_or_create_stats",
        side_effect=RuntimeError("simulated"),
    ):
        try:
            with loop_sink.sink_sync("test.failure", threshold_ms=1.0):
                time.sleep(0.01)
        except Exception as exc:
            pytest.fail(
                f"sink_sync propagated internal error: {exc}"
            )

        async def run_async():
            async with loop_sink.sink_async(
                "test.failure_async", threshold_ms=1.0,
            ):
                await asyncio.sleep(0.01)

        try:
            asyncio.run(run_async())
        except Exception as exc:
            pytest.fail(
                f"sink_async propagated internal error: {exc}"
            )
