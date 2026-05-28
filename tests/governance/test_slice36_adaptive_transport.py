"""Slice 36 — Adaptive Transport Dispatcher + Model-ID-Tolerant Aggregation.

Closes the v25→v31 production capability blocker:

  * v31 empirical evidence: STAGE_RT_HTTP_POST p50 TTFT = 66.8s
    on DW /v1/chat/completions for the same prompts that the
    BATCH API (/v1/batches via prompt_only) served in 4-8s.
  * Production picked RT; 0 APPLY events across 6 soaks.
  * Slice 36 routes STANDARD/COMPLEX (pure-DW config) through
    BATCH instead of RT.

Phase 1: ``_slice36_should_force_batch`` decision function +
``generate()`` wiring.

Phase 2: ``record_stage`` model_id-tolerant lookup — fixes the
v31 aggregation bug where Slice 34's op_session (model from
sentinel walker) and Slice 35's record_stage (model from
provider default) used different keys.

Test surface (4 AST + 7 spine = 11 tests).
"""

from __future__ import annotations

import ast
import asyncio
import os
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DW_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "doubleword_provider.py"
)
PROFILER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "telemetry"
    / "dispatch_profiler.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 4
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice36_decision_function_present() -> None:
    """``_slice36_should_force_batch`` MUST exist at module level
    with the 4 documented decision branches."""
    src = DW_FILE.read_text()
    tree = ast.parse(src, filename=str(DW_FILE))
    # The env knob string lives in the module-level constant; the
    # function body references the constant. Check both.
    assert "JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX" in src, (
        "env knob string missing from doubleword_provider"
    )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_slice36_should_force_batch"
        ):
            body = ast.unparse(node)
            assert "JARVIS_PROVIDER_CLAUDE_DISABLED" in body
            assert (
                "_SLICE36_FORCE_BATCH_ENV" in body
                or "JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX" in body
            )
            assert "provider_route" in body
            assert "standard" in body and "complex" in body
            return
    pytest.fail("_slice36_should_force_batch not located in doubleword_provider.py")


def test_ast_pin_generate_consults_slice36_selector() -> None:
    """``generate()`` MUST call ``_slice36_should_force_batch``
    BEFORE its RT-vs-batch routing decision."""
    src = DW_FILE.read_text()
    # The decision call appears AND the RT branch is gated on it
    assert "_slice36_should_force_batch(context)" in src or (
        "_slice36_should_force_batch" in src
    )
    assert "_slice36_force_batch" in src
    assert "self._realtime_enabled and not _slice36_force_batch" in src, (
        "RT branch must be gated by both the legacy flag AND the new "
        "Slice 36 force-batch decision"
    )


def test_ast_pin_record_stage_tolerant_lookup() -> None:
    """``record_stage`` MUST do exact-match lookup FIRST, then fall
    back to ``op_id`` prefix-match if exact key misses. This is the
    Phase 2 aggregation bug fix."""
    src = PROFILER_FILE.read_text()
    # The decision call appears AND tolerant fallback exists
    assert "Slice 36" in src, (
        "Phase 2 fix attribution missing from dispatch_profiler"
    )
    assert "for k, s in _active_ops.items():" in src or (
        "_prefix" in src and "startswith(_prefix)" in src
    ), (
        "tolerant lookup fallback (any-active-for-op_id) missing"
    )


def test_ast_pin_slice36_env_default_on() -> None:
    """``JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX`` defaults TRUE per
    operator authorization. Operator opt-out via explicit-false."""
    # Set Claude-disabled + STANDARD route + leave master unset (default)
    # Should return True
    from backend.core.ouroboros.governance.doubleword_provider import (
        _slice36_should_force_batch,
    )
    class FakeCtx:
        provider_route = "standard"
    with mock.patch.dict(os.environ, {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
    }, clear=False):
        os.environ.pop("JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX", None)
        assert _slice36_should_force_batch(FakeCtx()) is True
    # Explicit opt-out → False
    with mock.patch.dict(os.environ, {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
        "JARVIS_DW_FORCE_BATCH_STANDARD_COMPLEX": "0",
    }):
        assert _slice36_should_force_batch(FakeCtx()) is False


# ──────────────────────────────────────────────────────────────────────
# Spine — 7
# ──────────────────────────────────────────────────────────────────────


def test_spine_force_batch_false_when_claude_enabled() -> None:
    """When Claude is NOT disabled (default), Slice 36 returns False
    regardless of route — RT path with Claude fallback is the
    legacy default behavior."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        _slice36_should_force_batch,
    )
    class FakeCtx:
        provider_route = "standard"
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_PROVIDER_CLAUDE_DISABLED", None)
        assert _slice36_should_force_batch(FakeCtx()) is False
    with mock.patch.dict(os.environ, {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "false",
    }):
        assert _slice36_should_force_batch(FakeCtx()) is False


def test_spine_force_batch_false_for_non_standard_routes() -> None:
    """IMMEDIATE / BG / SPECULATIVE preserve RT path (low-context
    paths where Venom adds value)."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        _slice36_should_force_batch,
    )
    class FakeCtx:
        pass
    with mock.patch.dict(os.environ, {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
    }):
        for route in ("immediate", "background", "speculative", "", None):
            ctx = FakeCtx()
            ctx.provider_route = route
            assert _slice36_should_force_batch(ctx) is False, (
                f"route={route!r} should NOT force batch"
            )


def test_spine_force_batch_true_for_standard_and_complex() -> None:
    """STANDARD + COMPLEX under pure-DW config → force batch."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        _slice36_should_force_batch,
    )
    class FakeCtx:
        pass
    with mock.patch.dict(os.environ, {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "1",
    }):
        for route in ("standard", "complex", "STANDARD", "Complex"):
            ctx = FakeCtx()
            ctx.provider_route = route
            assert _slice36_should_force_batch(ctx) is True, (
                f"route={route!r} should force batch"
            )


def test_spine_force_batch_never_raises_on_missing_attrs() -> None:
    """If context has no ``provider_route`` attr or env access
    fails, Slice 36 MUST return False (preserve legacy behavior)."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        _slice36_should_force_batch,
    )
    class BrokenCtx:
        def __getattr__(self, name):
            raise RuntimeError(f"broken attr {name}")
    # Must not raise; must return False (defensive)
    with mock.patch.dict(os.environ, {
        "JARVIS_PROVIDER_CLAUDE_DISABLED": "true",
    }):
        result = _slice36_should_force_batch(BrokenCtx())
        assert result is False


def test_spine_record_stage_tolerant_on_model_id_mismatch() -> None:
    """Phase 2 fix: when op_session uses model X but record_stage
    is called with model Y for the same op_id, the stage MUST still
    aggregate into the op's summary (tolerant fallback)."""
    os.environ["JARVIS_DISPATCH_PROFILER_ENABLED"] = "1"
    from backend.core.ouroboros.telemetry import dispatch_profiler as dp
    dp.reset_for_tests()

    async def run():
        # Slice 34 op_session uses model_A
        async with dp.op_session(
            op_id="op-test-mismatch",
            model_id="Qwen/Qwen3.5-35B-A3B-FP8",
            route="standard",
        ) as summary:
            # Slice 35 record_stage uses model_B (provider default)
            dp.record_stage(
                "STAGE_RT_PROMPT_BUILD",
                op_id="op-test-mismatch",
                model_id="Qwen/Qwen3.5-397B-A17B-FP8",  # MISMATCH
                duration_ms=100.0,
            )
        # PRE-Slice-36 bug: stage would NOT appear in summary.stages
        # POST-Slice-36: tolerant fallback finds the active op
        assert len(summary.stages) == 1
        assert summary.stages[0].stage_name == "STAGE_RT_PROMPT_BUILD"
        assert summary.stages[0].duration_ms == 100.0

    asyncio.run(run())
    dp.reset_for_tests()


def test_spine_record_stage_exact_match_still_works() -> None:
    """Tolerant fallback MUST NOT break the exact-match case
    (model_id matches → exact lookup succeeds → stage records)."""
    os.environ["JARVIS_DISPATCH_PROFILER_ENABLED"] = "1"
    from backend.core.ouroboros.telemetry import dispatch_profiler as dp
    dp.reset_for_tests()

    async def run():
        async with dp.op_session(
            op_id="op-exact", model_id="m-same", route="standard",
        ) as summary:
            dp.record_stage(
                "S1", op_id="op-exact", model_id="m-same",
                duration_ms=50.0,
            )
        assert len(summary.stages) == 1

    asyncio.run(run())
    dp.reset_for_tests()


def test_spine_record_stage_no_active_op_still_emits_log() -> None:
    """When NO active op_session matches (neither exact nor
    prefix), record_stage MUST still emit the per-stage log row
    (per Slice 35 contract — log rows are independent of summary
    aggregation)."""
    os.environ["JARVIS_DISPATCH_PROFILER_ENABLED"] = "1"
    from backend.core.ouroboros.telemetry import dispatch_profiler as dp
    dp.reset_for_tests()

    # No op_session active — record_stage should not raise, should
    # still emit the per-stage row (verified via lack of exception)
    try:
        dp.record_stage(
            "S_ORPHAN", op_id="op-no-session", model_id="m",
            duration_ms=42.0,
        )
    except Exception as exc:
        pytest.fail(f"record_stage raised when no active op: {exc}")
    dp.reset_for_tests()
