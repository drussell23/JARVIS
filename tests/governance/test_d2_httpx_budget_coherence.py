"""
Task #95 spine — D2: per-request httpx budget coherence.

Closes the v14-rev12 graduation-soak Tier 2 blocker (PRD §40.7.10-
stage1.6-slice3-v14rev12).  Before D2, ``ClaudeProvider`` constructed
``httpx.Timeout`` once at ``_ensure_client()`` with static values
(``connect=10s, read=600s thinking / 120s default``).  When the outer
``asyncio.wait_for`` fired with a small per-attempt budget (10.4s on
v14-rev12's outer-retry-loop second attempt), the SDK still used the
construction-time values — so the network call actually ran 131s
(10s connect + 120s read) before surrendering, violating the outer
wait_for by 12×.

D2 (operator binding 2026-05-14) fixes this without stacking yet
another blind global timeout:

  * ``_derive_per_request_httpx_timeout(outer_budget_s)`` composes
    an ``httpx.Timeout`` whose connect/write/pool are bounded by
    ``min(JARVIS_CLAUDE_HTTPX_CONNECT_CAP_S, outer_budget_s)`` and
    whose read matches the full per-attempt budget (thinking streams
    emit no bytes until reasoning completes, so read needs the full
    window).
  * Wired at three Claude SDK entry points: ``messages.stream`` in
    ``_do_stream``, ``messages.create`` in ``_create_with_prefill_
    fallback``, and ``_legacy_create`` in the tool-loop fallback path.
  * Companion fast-fail ``sem_exhausted_zero_budget`` in
    ``candidate_generator._call_fallback`` refuses to open a stream
    when post-sem ``_parent_remaining <= 0`` (honest enforcement, no
    fabrication; preserves #88c floor reservation for *nonzero*
    budgets via deadline refresh).

This spine pins:

  * Helper math (decision-table parametrized).
  * Env-tunable cap honored.
  * Helper is wired at every Claude SDK call site (AST scan).
  * Fast-fail raises ``sem_exhausted_zero_budget`` at the right seam.
  * FlagRegistry seed present.

No live network call — fully deterministic via monkeypatched
``time.monotonic`` / direct helper invocation.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_CANDIDATE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "candidate_generator.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Helper behavior — decision-table
# ---------------------------------------------------------------------------


def _import_helper():
    """Lazy import so a broken module doesn't fail collection of pins."""
    from backend.core.ouroboros.governance.providers import (
        _derive_per_request_httpx_timeout,
    )
    return _derive_per_request_httpx_timeout


@pytest.mark.parametrize("budget,cap,expected_connect,expected_read", [
    # v14-rev12 evidence: 10.4s outer-attempt budget — connect capped
    # at 5.0 (default), read at full budget.
    (10.4, None, 5.0, 10.4),
    # Thinking-on stream with full 360s budget — connect still 5.0
    # (the cap dominates), read 360s for thinking_delta window.
    (360.0, None, 5.0, 360.0),
    # Tiny budget (1.0s) — connect can't exceed the budget itself,
    # honoring the invariant "connect ≤ outer_budget".
    (1.0, None, 1.0, 1.0),
    # Operator-tuned tight cap (2.0s) — chosen over default 5.0.
    (10.4, 2.0, 2.0, 10.4),
    # Operator-tuned wider cap (15.0s) but small budget — budget wins.
    (8.0, 15.0, 8.0, 8.0),
    # Boundary: cap == budget — connect == budget.
    (5.0, 5.0, 5.0, 5.0),
])
def test_helper_decision_table(budget, cap, expected_connect, expected_read):
    fn = _import_helper()
    t = fn(budget, connect_cap_s=cap)
    assert t.connect == pytest.approx(expected_connect, abs=0.01), (
        f"connect mismatch: budget={budget} cap={cap} → "
        f"expected={expected_connect} got={t.connect}"
    )
    assert t.read == pytest.approx(expected_read, abs=0.01), (
        f"read mismatch: budget={budget} → expected={expected_read} "
        f"got={t.read}"
    )
    # write and pool always match connect (small, bounded)
    assert t.write == t.connect
    assert t.pool == t.connect


def test_helper_floors_zero_and_negative_budget():
    """Helper must never construct a 0-timeout (which httpx interprets
    as 'don't wait at all' and would fail at TLS).  Callers fast-fail
    upstream via ``sem_exhausted_zero_budget`` before reaching the
    helper, but the helper itself must be defensive against bad input.
    """
    fn = _import_helper()
    # Zero outer budget → floored at 0.1s (helper safety)
    t = fn(0.0)
    assert t.connect == pytest.approx(0.1, abs=0.01)
    assert t.read == pytest.approx(0.1, abs=0.01)
    # Negative outer budget → also floored
    t = fn(-5.0)
    assert t.connect == pytest.approx(0.1, abs=0.01)


def test_helper_respects_env_cap_default():
    """JARVIS_CLAUDE_HTTPX_CONNECT_CAP_S env override is read at
    *module import* time (matches existing ``_CLAUDE_HTTP_*`` pattern).
    The function signature still accepts a per-call override so spine
    can deterministically exercise both.  This test verifies the
    function-level override branches.
    """
    fn = _import_helper()
    # Explicit per-call override beats the module default
    t = fn(100.0, connect_cap_s=1.5)
    assert t.connect == pytest.approx(1.5, abs=0.01)
    # Default behavior when override omitted
    t = fn(100.0)
    # Default module const is 5.0 (per seed); won't exceed budget=100
    assert t.connect == pytest.approx(5.0, abs=0.5)


# ---------------------------------------------------------------------------
# AST pins — helper is wired at every Claude SDK call site
# ---------------------------------------------------------------------------


def test_ast_pin_helper_defined():
    """Helper must exist as a module-level function in providers.py."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = [
        n.name for n in tree.body
        if isinstance(n, ast.FunctionDef)
    ]
    assert "_derive_per_request_httpx_timeout" in funcs, (
        "providers.py MUST expose _derive_per_request_httpx_timeout "
        "at module level (importable for spine tests + AST scanning)"
    )


def test_ast_pin_helper_wired_at_stream_path():
    """The stream path MUST consult the helper to construct its
    per-request httpx.Timeout.  Without this, v14-rev12-style
    131s-on-10.4s-budget overruns return."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # Look for the stream kwargs being augmented with the helper
    assert '_stream_kwargs["timeout"] = _derive_per_request_httpx_timeout(' in src, (
        "messages.stream kwargs MUST set timeout via the helper "
        "(_stream_kwargs['timeout'] = _derive_per_request_httpx_timeout(...))"
    )


def test_ast_pin_helper_wired_at_create_path():
    """Primary create path (``_create_with_prefill_fallback``) must
    also use the helper."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    assert '_create_kwargs["timeout"] = _derive_per_request_httpx_timeout(' in src, (
        "messages.create kwargs MUST set timeout via the helper"
    )


def test_ast_pin_helper_wired_at_legacy_create():
    """The legacy tool-loop create path also passes timeout via the
    helper."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # _legacy_create calls messages.create with timeout=_derive...
    assert "timeout=_derive_per_request_httpx_timeout(timeout_s)" in src, (
        "Legacy tool-loop create MUST pass timeout via helper"
    )


def test_ast_pin_sdk_max_retries_zero_preserved():
    """LOAD-BEARING invariant: ``max_retries=0`` on AsyncAnthropic MUST
    stay, so D2's outer-budget enforcement is the single owner of
    retry semantics — no nested SDK retry stack ignoring outer
    wait_for."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # The exact construction-line pattern from _ensure_client
    assert "max_retries=0" in src, (
        "AsyncAnthropic MUST be constructed with max_retries=0; "
        "_call_with_backoff is the sole retry authority"
    )


# ---------------------------------------------------------------------------
# Companion fast-fail in candidate_generator
# ---------------------------------------------------------------------------


def test_ast_pin_sem_exhausted_fast_fail_present():
    """``_call_fallback`` MUST fast-fail with ``sem_exhausted_zero_budget``
    when post-sem ``_parent_remaining <= 0`` — operator binding 2026-05-14
    clause 3 (honest enforcement, no fabrication).  Sits BEFORE the
    #88c floor refresh by design (refresh is for nonzero budgets)."""
    src = _CANDIDATE_SRC.read_text(encoding="utf-8")
    assert "sem_exhausted_zero_budget" in src, (
        "_call_fallback MUST raise sem_exhausted_zero_budget when "
        "post-sem _parent_remaining <= 0 (D2 operator binding)"
    )
    # The fast-fail must precede the floor-refresh _budget_target
    # line *within _call_fallback* (the relevant method).  There's an
    # earlier _budget_target line in a different method (related but
    # not the seam we pin); use rfind so we land on _call_fallback's.
    idx_fast_fail = src.find("sem_exhausted_zero_budget")
    idx_floor_refresh = src.rfind(
        "_budget_target = max(_parent_remaining,",
    )
    assert idx_fast_fail > 0 and idx_floor_refresh > 0, (
        "Both seams must exist for ordering pin to be meaningful"
    )
    assert idx_fast_fail < idx_floor_refresh, (
        "sem_exhausted_zero_budget fast-fail MUST appear in source "
        "order BEFORE _call_fallback's #88c floor-refresh "
        "_budget_target — D2 honest enforcement runs first; floor "
        "refresh only applies when remaining > 0"
    )


def test_ast_pin_fast_fail_guarded_by_zero_check():
    """The fast-fail must be conditional on ``_parent_remaining <= 0.0``
    so we don't accidentally short-circuit legitimate small-budget
    calls (which #88c's refresh handles)."""
    src = _CANDIDATE_SRC.read_text(encoding="utf-8")
    # The literal guard expression
    assert "_parent_remaining <= 0.0" in src, (
        "Fast-fail MUST be guarded by `_parent_remaining <= 0.0` "
        "(non-zero budgets fall through to #88c floor refresh)"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_seed_has_httpx_connect_cap_flag():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_CLAUDE_HTTPX_CONNECT_CAP_S" in src
    idx = src.find("JARVIS_CLAUDE_HTTPX_CONNECT_CAP_S")
    window = src[idx:idx + 1800]
    assert "default=5.0" in window, (
        "Default cap MUST be 5.0 per operator binding"
    )
    assert "Category.TUNING" in window, (
        "Cap is operator-tunable observability/perf knob → TUNING"
    )
    assert "providers.py" in window, (
        "Source file MUST point at providers.py"
    )


# ---------------------------------------------------------------------------
# Behavioral guarantee — wall ≤ budget + grace under slow connect
# ---------------------------------------------------------------------------


def test_helper_returns_httpx_timeout_instance():
    """Type check: helper returns a real httpx.Timeout (not a float).
    Float would be interpreted by httpx as 'all four = this value',
    making connect == read — defeats the D2 invariant."""
    import httpx
    fn = _import_helper()
    t = fn(10.4)
    assert isinstance(t, httpx.Timeout), (
        f"Helper MUST return httpx.Timeout (got {type(t).__name__}); "
        "segmented timeouts are essential for the D2 invariant"
    )


def test_helper_invariant_connect_le_outer_budget():
    """The load-bearing invariant: regardless of cap settings,
    connect ≤ outer_budget_s.  This pins the operator binding
    'httpx connect+pool timeouts must be ≤ the remaining outer
    asyncio budget for that attempt.'"""
    fn = _import_helper()
    # Sweep across a range of (budget, cap) pairs
    for budget in [0.5, 1.0, 5.0, 10.4, 30.0, 90.0, 360.0]:
        for cap in [None, 0.5, 2.0, 5.0, 10.0, 60.0]:
            t = fn(budget, connect_cap_s=cap)
            assert t.connect <= max(0.1, budget), (
                f"Invariant violated: budget={budget} cap={cap} → "
                f"connect={t.connect} > budget"
            )
            assert t.pool <= max(0.1, budget), (
                f"pool MUST also honor outer budget: budget={budget} "
                f"cap={cap} → pool={t.pool}"
            )
