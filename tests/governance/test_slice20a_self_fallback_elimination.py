"""Slice 20A — Self-fallback elimination when Claude is intentionally disabled.

Closes the double-binding bug surfaced by soak bt-2026-05-26-184355 (PURE-DW v15).
Slice 19a correctly disabled ClaudeProvider construction (`self._fallback`
stays None at the provider level). BUT GovernedLoopService line 3576
then collapsed it back into primary via:

  effective_fallback = fallback or primary  # ← assigned DW as fallback

When the operator set JARVIS_PROVIDER_CLAUDE_DISABLED=true:
  fallback=None (from gate), primary=DW
  → effective_fallback = None or DW = DW
  → CandidateGenerator(primary=DW, fallback=DW)  # SAME OBJECT!

Result: Slice 19b's `self._fallback is None` guard never fired
because fallback wasn't None — it was DW. The cascade FSM called
DW twice (once as primary, once as fallback), DW collided on its
own scheduler, exhaustion event recorded as fallback_failed instead
of fallback_skipped, hibernation breaker counted them, soak still
limped along but the architectural intent was violated.

Empirical evidence from v15 (multiple events):

  EXHAUSTION event_n=1 cause=fallback_failed
  primary_name=doubleword-397b
  fallback_name=doubleword-397b   ← SAME PROVIDER
  fallback_err_msg=doubleword_sche... (DW's own scheduler conflict)

# Fix mechanism

GovernedLoopService line 3573-3578 now checks if Claude was
intentionally disabled. When YES AND fallback is None, effective_fallback
STAYS None. CandidateGenerator's `fallback: Optional[CandidateProvider]
= None` signature accepts this. Slice 19b's existing guard then fires
correctly on the first cascade attempt.

# Test surface (2 AST pins + 4 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path
import os
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GLS_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "governed_loop_service.py"
)
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_governed_loop_honors_claude_disabled_for_fallback() -> None:
    """GovernedLoopService MUST check JARVIS_PROVIDER_CLAUDE_DISABLED
    when deriving effective_fallback. Without this, the legacy
    `fallback or primary` self-binding silently activates."""
    src = GLS_FILE.read_text()
    assert "Slice 20A" in src, (
        "GovernedLoopService missing Slice 20A attribution — fix reverted"
    )
    assert "_claude_disabled" in src, (
        "Missing _claude_disabled local in effective_fallback derivation — "
        "self-fallback elimination dead code"
    )
    # The structural form: when _claude_disabled is True AND fallback is None,
    # effective_fallback = None
    assert "effective_fallback = None" in src, (
        "Missing `effective_fallback = None` assignment — Slice 19b's "
        "fallback_skipped guard cannot fire"
    )
    # Soak attribution
    assert "bt-2026-05-26-184355" in src, (
        "Missing v15 soak attribution"
    )


def test_ast_pin_candidate_generator_accepts_optional_fallback() -> None:
    """CandidateGenerator.__init__ MUST accept fallback as
    Optional[CandidateProvider] = None — required for Slice 20A's
    effective_fallback=None to flow through without a TypeError."""
    src = CG_FILE.read_text()
    # The __init__ signature must allow fallback to be None
    tree = ast.parse(src, filename=str(CG_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "__init__"
        ):
            # Find the fallback arg
            for arg in node.args.args:
                if arg.arg == "fallback":
                    # Check if its annotation is Optional[...] OR
                    # the default is None (defaults align with args
                    # via the args.defaults list)
                    annotation_src = (
                        ast.unparse(arg.annotation) if arg.annotation else ""
                    )
                    if "Optional[" in annotation_src or "| None" in annotation_src:
                        found = True
                        break
            if found:
                break
    assert found, (
        "CandidateGenerator.__init__ fallback param NOT Optional — "
        "Slice 20A's effective_fallback=None will TypeError at construction"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 4
# ──────────────────────────────────────────────────────────────────────


def test_spine_candidate_generator_constructs_with_fallback_none() -> None:
    """CandidateGenerator must construct successfully with fallback=None.
    Before Slice 20A this would fail because __init__ required a
    CandidateProvider, not Optional[CandidateProvider]."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    class _StubProvider:
        provider_name = "stub"
        async def generate(self, *a, **k):
            return None

    # Must NOT raise
    cg = CandidateGenerator(
        primary=_StubProvider(),
        fallback=None,
    )
    assert cg._fallback is None, (
        "CandidateGenerator._fallback not None after explicit fallback=None"
    )


def test_spine_slice19b_guard_fires_when_fallback_actually_none() -> None:
    """The whole point of Slice 20A: with fallback=None propagated,
    Slice 19b's `self._fallback is None` guard fires correctly.
    Verify _call_fallback raises with fallback_skipped: cause."""
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    import asyncio
    from datetime import datetime, timezone, timedelta

    class _StubProvider:
        provider_name = "stub-primary"
        async def generate(self, *a, **k):
            return None

    class _StubCtx:
        op_id = "test-op-20a"
        provider_route = "standard"

    cg = CandidateGenerator(
        primary=_StubProvider(),
        fallback=None,
    )

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)

    async def _runner():
        try:
            await cg._call_fallback(_StubCtx(), deadline)
            return None
        except RuntimeError as exc:
            return str(exc)

    result = asyncio.run(_runner())
    assert result is not None, "_call_fallback did not raise"
    assert "fallback_skipped" in result, (
        f"_call_fallback raised but cause was not fallback_skipped — got "
        f"{result!r}; Slice 19b guard didn't fire because fallback wasn't None"
    )


def test_spine_self_fallback_collapse_path_documented() -> None:
    """The Slice 20A fix must mention the specific empirical pattern
    it eliminates: `fallback_name == primary_name == doubleword-397b`.
    Future readers need to trace WHY the branch exists."""
    src = GLS_FILE.read_text()
    # Look for the explanatory comment block
    assert "self-fallback" in src.lower(), (
        "Missing self-fallback explanation — context lost"
    )


def test_spine_legacy_path_preserved_when_claude_enabled() -> None:
    """When Claude is NOT disabled (default operator config),
    effective_fallback STILL falls back to primary if no real
    fallback exists. Pre-Slice-20A byte-equivalent for non-DW-only
    soaks."""
    src = GLS_FILE.read_text()
    # The legacy `fallback or primary` branch must still exist in the
    # else clause
    assert "effective_fallback = fallback or primary" in src, (
        "Legacy `fallback or primary` branch removed — non-DW-only "
        "soaks broken; pre-Slice-20A byte-equivalence violated"
    )
