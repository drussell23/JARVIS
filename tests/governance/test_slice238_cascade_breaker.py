"""Slice 238 — cascade-to-dead-Claude fix (layer 8).

The s237 soak made this the dominant residual: on a STANDARD-route op, when DW
models fail (transient transport hiccup), the sentinel applies
``fallback_tolerance=cascade_to_claude`` and calls ``_call_fallback`` → the Claude
lane → but Claude is economically dead (credit balance too low, HTTP 400) →
``circuit_breaker_tripped:terminal_quota`` → ForegroundCooldown retry cycle.

Investigated root cause (candidate_generator.py:3989): the cascade path reads NO
breaker state — it blindly invokes the configured Claude fallback. The PRIMARY
Claude lane gates on the economic breaker (``get_claude_circuit_breaker()`` /
``_claude_breaker_open``); the cascade bypasses it entirely.

Fix REUSES the same source-of-truth: consult the read-only ``_claude_breaker_open``
predicate (no probe side-effect) before cascading. When the breaker is OPEN
(Claude economically/transport dead), do NOT cascade into a known-dead lane —
route to the EXISTING Slice-180 immortal DW-retry / clean-degrade path (the same
branch the no-fallback case already uses). Breaker CLOSED → byte-identical legacy
cascade (when Claude is funded, the cascade works normally). No hardcoded
"never use Claude" — it reads the live breaker state.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance import candidate_generator as cg


class TestShouldCascadeToClaude:
    def test_breaker_closed_with_fallback_cascades(self):
        # Claude funded (breaker CLOSED) → cascade works normally (legacy)
        assert cg.should_cascade_to_claude(
            has_fallback=True, claude_breaker_open=False, enabled=True,
        ) is True

    def test_breaker_open_with_fallback_suppresses(self):
        # Claude economically dead (breaker OPEN) → do NOT cascade to it
        assert cg.should_cascade_to_claude(
            has_fallback=True, claude_breaker_open=True, enabled=True,
        ) is False

    def test_breaker_open_but_disabled_is_legacy(self):
        # kill switch off → legacy behavior (cascade regardless), for rollback
        assert cg.should_cascade_to_claude(
            has_fallback=True, claude_breaker_open=True, enabled=False,
        ) is True

    def test_no_fallback_never_cascades(self):
        # no Claude fallback configured → never cascade (regardless of breaker)
        assert cg.should_cascade_to_claude(
            has_fallback=False, claude_breaker_open=False, enabled=True,
        ) is False
        assert cg.should_cascade_to_claude(
            has_fallback=False, claude_breaker_open=True, enabled=True,
        ) is False

    def test_pure_no_env_reads_in_source(self):
        # the decision is pure — env/breaker reads happen at the caller, injected
        src = inspect.getsource(cg.should_cascade_to_claude)
        assert "os.environ" not in src
        assert "get_claude_circuit_breaker" not in src


class TestCascadeBreakerConsultFlag:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CASCADE_BREAKER_CONSULT_ENABLED", raising=False)
        assert cg.cascade_breaker_consult_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CASCADE_BREAKER_CONSULT_ENABLED", "false")
        assert cg.cascade_breaker_consult_enabled() is False


class TestDispatchWiresBreakerConsult:
    """Wiring pins (source-level, mirrors the slice-237 style): the sentinel
    dispatch consults the breaker before the cascade, reusing the canonical
    read-only predicate — and routes a suppressed cascade to the existing
    immortal/degrade branch, not a new path."""

    def test_dispatch_consults_breaker_before_cascade(self):
        src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
        assert "should_cascade_to_claude(" in src, "dispatch must consult the cascade decision"
        # reuses the canonical read-only economic-breaker predicate (no probe side-effect)
        assert "_claude_breaker_open" in src
        # the decision is consulted BEFORE the _call_fallback cascade
        decide_idx = src.index("should_cascade_to_claude(")
        fallback_idx = src.rindex("_call_fallback(")
        assert decide_idx < fallback_idx

    def test_suppressed_cascade_reuses_immortal_degrade_branch(self):
        # a breaker-OPEN suppression must route to the EXISTING immortal DW-retry
        # branch (Slice 180), not invent a new degrade path
        src = inspect.getsource(cg.CandidateGenerator._dispatch_via_sentinel)
        assert "immortal" in src.lower()


class TestFallbackIsClaude:
    """The Claude breaker only gates the Claude lane — a non-Claude fallback
    (e.g. Prime) must never be suppressed by it."""

    def _stub(self, provider_name):
        from types import SimpleNamespace
        stub = SimpleNamespace(_fallback=SimpleNamespace(provider_name=provider_name))
        return cg.CandidateGenerator._fallback_is_claude(stub)

    def test_claude_fallback_detected(self):
        assert self._stub("claude-api") is True
        assert self._stub("Claude") is True

    def test_non_claude_fallback_not_detected(self):
        assert self._stub("doubleword-397b") is False
        assert self._stub("j-prime") is False

    def test_none_fallback_not_claude(self):
        from types import SimpleNamespace
        assert cg.CandidateGenerator._fallback_is_claude(
            SimpleNamespace(_fallback=None)
        ) is False


class TestCentralFallbackGuard:
    """The CENTRAL seam: _call_fallback must suppress the dead Claude lane for
    EVERY caller (not just the sentinel cascade — the s237 soak proved
    BadRequestError 400 reached Claude from other _call_fallback callers too)."""

    def test_call_fallback_consults_breaker_centrally(self):
        src = inspect.getsource(cg.CandidateGenerator._call_fallback)
        assert "_claude_breaker_open" in src, "central _call_fallback must consult the breaker"
        assert "_fallback_is_claude" in src, "only suppress when the fallback IS Claude"
        # reuses the existing non-hibernation fallback_skipped sentinel (Slice 19b)
        assert "fallback_skipped:claude_breaker_open" in src
        assert "cascade_breaker_consult_enabled" in src
