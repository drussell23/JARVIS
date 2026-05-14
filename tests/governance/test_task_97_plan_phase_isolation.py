"""
Task #97 spine — PLAN phase-local sub-budgeting + ``_plan_create`` D2 wiring.

v14-rev14 graduation soak surfaced a structural defect: the SWE op's
PLAN phase consumed 194–337s of the op budget before reaching GENERATE,
ultimately raising ``claude_plan_budget_starved:-45.4s_remaining``.
Root cause: PlanGenerator passed the OUTER op deadline through to
``self._generator.plan(...)``; ``_call_with_backoff`` retried Claude
calls against that shared deadline, draining GENERATE's runway.
Compounded by Task #95 D2 having missed ``_plan_create`` — the 4th
Claude SDK entry point — leaving it on construction-time httpx config.

Per operator binding 2026-05-14 ("strict asynchronous isolation +
sub-phase budgeting"), this PR introduces:

  1. A pure-data ``_compute_plan_phase_budget_s(op_remaining_s)``
     helper that returns ``min(op_remaining × fraction, op_remaining
     - reserve)``.  Decision-table tested.
  2. Three env-tunable knobs (JARVIS_PLAN_PHASE_BUDGET_FRACTION /
     MIN_GENERATE_RESERVE_S / MIN_BUDGET_S), all defaults registered
     in FlagRegistry with Category.TUNING.
  3. Phase-local deadline computation in ``generate_plan``: if the
     computed budget is below ``JARVIS_PLAN_PHASE_MIN_BUDGET_S``,
     skip PLAN entirely (graceful degrade — GENERATE keeps full
     op_remaining); else, call ``self._generator.plan`` with a
     deadline at ``now + plan_budget_s`` and wrap in
     ``asyncio.wait_for(timeout=plan_budget_s + grace)`` for
     autonomous interrupt (composes existing primitive — no new
     bounding mechanism).
  4. D2 helper wiring at ``_plan_create`` (providers.py) — the 4th
     SDK entry point that was missed in Task #95.

This spine pins:

  * Helper math decision-table (fraction × op_remaining; reserve
    bound; floor; invalid env fallbacks).
  * Resolver functions: invalid / negative / out-of-range / typos
    fall back to defaults.
  * AST pins: ``generate_plan`` computes phase-local deadline,
    consults the helper, calls ``self._generator.plan`` with the
    LOCAL deadline (not the op deadline), and wraps in
    ``asyncio.wait_for``.
  * AST pins: ``_plan_create`` in providers.py now passes
    ``timeout=_derive_per_request_httpx_timeout(_attempt_budget_s)``.
  * AST pins: skip path returns ``PlanResult.skipped_result(...)``
    when below floor.
  * FlagRegistry seeds present with correct defaults + categories.

No live network call — fully deterministic via pure-data math + AST
inspection.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_PLAN_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "plan_generator.py"
)
_PROVIDERS_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "flag_registry_seed.py"
)


# ---------------------------------------------------------------------------
# Helper math — decision table
# ---------------------------------------------------------------------------


def _import_helper():
    from backend.core.ouroboros.governance.plan_generator import (
        _compute_plan_phase_budget_s,
    )
    return _compute_plan_phase_budget_s


def _import_resolvers():
    from backend.core.ouroboros.governance.plan_generator import (
        _resolve_plan_phase_fraction,
        _resolve_plan_phase_min_generate_reserve_s,
        _resolve_plan_phase_min_budget_s,
    )
    return (
        _resolve_plan_phase_fraction,
        _resolve_plan_phase_min_generate_reserve_s,
        _resolve_plan_phase_min_budget_s,
    )


@pytest.mark.parametrize("op_remaining,expected_budget", [
    # Generous op budget (300s) at default 30% fraction + 60s reserve:
    #   fraction_bound = 300 × 0.30 = 90
    #   reserve_bound  = 300 - 60   = 240
    #   plan_budget    = min(90, 240) = 90
    (300.0, pytest.approx(90.0, abs=0.01)),
    # Modest op budget (200s):
    #   fraction = 60, reserve = 140 → plan_budget = 60
    (200.0, pytest.approx(60.0, abs=0.01)),
    # Tight op budget (100s):
    #   fraction = 30, reserve = 40 → plan_budget = 30 (fraction wins)
    (100.0, pytest.approx(30.0, abs=0.01)),
    # Very tight op budget (70s) — reserve_bound dominates:
    #   fraction = 21, reserve = 10 → plan_budget = 10 (reserve wins)
    (70.0, pytest.approx(10.0, abs=0.01)),
    # Op budget exactly equal to reserve (60s):
    #   fraction = 18, reserve = 0 → plan_budget = 0 (caller will skip)
    (60.0, 0.0),
    # Op budget below reserve (30s):
    #   fraction = 9, reserve = max(0, -30) = 0 → plan_budget = 0
    (30.0, 0.0),
    # Zero / negative op budget → 0 (clamped at the kernel input)
    (0.0, 0.0),
    (-50.0, 0.0),
    # Large op budget (1200s) at default knobs:
    #   fraction = 360, reserve = 1140 → plan_budget = 360
    (1200.0, pytest.approx(360.0, abs=0.01)),
])
def test_helper_math_decision_table(op_remaining, expected_budget):
    fn = _import_helper()
    assert fn(op_remaining) == expected_budget, (
        f"Math mismatch: op_remaining={op_remaining} → "
        f"expected={expected_budget} got={fn(op_remaining)}"
    )


# ---------------------------------------------------------------------------
# Resolver — invalid env fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_value,expected", [
    # Valid values
    ("0.30", 0.30),
    ("0.50", 0.50),
    ("1.0", 1.0),
    # Edge: 0.0 is REJECTED (would zero out PLAN entirely; out of valid range)
    ("0.0", 0.30),  # falls back to default
    # Out-of-range > 1.0
    ("1.5", 0.30),
    # Negative
    ("-0.1", 0.30),
    # Garbage
    ("abc", 0.30),
    ("", 0.30),
    # Trailing whitespace tolerated by float()
    ("  0.25  ", 0.25),
])
def test_fraction_resolver(env_value, expected, monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_PHASE_BUDGET_FRACTION", env_value)
    fn, _, _ = _import_resolvers()
    assert fn() == pytest.approx(expected, abs=0.01)


def test_fraction_resolver_default_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_PLAN_PHASE_BUDGET_FRACTION", raising=False)
    fn, _, _ = _import_resolvers()
    assert fn() == pytest.approx(0.30, abs=0.01)


@pytest.mark.parametrize("env_value,expected", [
    ("60.0", 60.0),
    ("120.0", 120.0),
    ("0.0", 0.0),  # zero reserve is valid (operator may opt out)
    ("-1.0", 60.0),  # negative → fallback
    ("garbage", 60.0),
])
def test_reserve_resolver(env_value, expected, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S", env_value,
    )
    _, fn, _ = _import_resolvers()
    assert fn() == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize("env_value,expected", [
    ("5.0", 5.0),
    ("0.0", 0.0),
    ("10.0", 10.0),
    ("-1.0", 5.0),
    ("garbage", 5.0),
])
def test_min_budget_resolver(env_value, expected, monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_PHASE_MIN_BUDGET_S", env_value)
    _, _, fn = _import_resolvers()
    assert fn() == pytest.approx(expected, abs=0.01)


def test_helper_respects_env_fraction(monkeypatch):
    """End-to-end: changing the env fraction changes the kernel
    output deterministically (math kernel reads resolver, not a
    cached module-level constant)."""
    fn = _import_helper()
    # Default 0.30
    monkeypatch.delenv("JARVIS_PLAN_PHASE_BUDGET_FRACTION", raising=False)
    assert fn(300.0) == pytest.approx(90.0, abs=0.01)
    # Operator-tuned 0.50
    monkeypatch.setenv("JARVIS_PLAN_PHASE_BUDGET_FRACTION", "0.50")
    assert fn(300.0) == pytest.approx(150.0, abs=0.01)


# ---------------------------------------------------------------------------
# AST pins — generate_plan wires the phase-local deadline
# ---------------------------------------------------------------------------


def test_ast_pin_generate_plan_consults_helper():
    """``generate_plan`` MUST call ``_compute_plan_phase_budget_s`` to
    compute the phase-local budget."""
    src = _PLAN_SRC.read_text(encoding="utf-8")
    assert "_compute_plan_phase_budget_s(_op_remaining_s)" in src, (
        "generate_plan MUST call _compute_plan_phase_budget_s(...) "
        "to compute the phase-local budget"
    )


def test_ast_pin_generate_plan_skips_below_floor():
    """``generate_plan`` MUST skip PLAN early when budget is below the
    floor — graceful degrade preserves GENERATE budget."""
    src = _PLAN_SRC.read_text(encoding="utf-8")
    assert "if _plan_budget_s < _min_plan_budget_s:" in src, (
        "generate_plan MUST gate the PLAN attempt on budget >= floor"
    )
    assert "plan_phase_skipped:insufficient_budget" in src, (
        "Skip path MUST return PlanResult.skipped_result with the "
        "structured plan_phase_skipped:insufficient_budget reason"
    )


def test_ast_pin_generate_plan_uses_local_deadline():
    """``generate_plan`` MUST pass ``_plan_deadline`` (the phase-local
    deadline) to ``self._generator.plan(...)``, NOT the op-level
    ``deadline``.  This is the load-bearing isolation."""
    src = _PLAN_SRC.read_text(encoding="utf-8")
    # Use AST to find the call inside generate_plan
    tree = ast.parse(src)
    gen_plan_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "generate_plan"
        ):
            gen_plan_fn = node
            break
    assert gen_plan_fn is not None
    # Find the call to self._generator.plan(...) inside generate_plan
    found_local_deadline_call = False
    for sub in ast.walk(gen_plan_fn):
        if isinstance(sub, ast.Call):
            # Check for self._generator.plan(prompt, X) call
            if (
                isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "plan"
                and isinstance(sub.func.value, ast.Attribute)
                and sub.func.value.attr == "_generator"
            ):
                # The second positional arg is the deadline
                if len(sub.args) >= 2 and isinstance(sub.args[1], ast.Name):
                    if sub.args[1].id == "_plan_deadline":
                        found_local_deadline_call = True
                        break
    assert found_local_deadline_call, (
        "generate_plan MUST call self._generator.plan(prompt, "
        "_plan_deadline) — passing the PHASE-LOCAL deadline, NOT the "
        "op deadline.  This is the load-bearing isolation pin."
    )


def test_ast_pin_generate_plan_wraps_in_wait_for():
    """The plan() call MUST be wrapped in ``asyncio.wait_for`` for
    autonomous interrupt — composes existing asyncio primitive per
    operator binding."""
    src = _PLAN_SRC.read_text(encoding="utf-8")
    assert "await asyncio.wait_for(" in src, (
        "generate_plan MUST wrap the plan() call in asyncio.wait_for "
        "for autonomous hard-bound interrupt"
    )
    # And the wait_for timeout must include the grace constant
    assert "_plan_budget_s + _wait_for_grace_s" in src, (
        "asyncio.wait_for timeout MUST be _plan_budget_s + "
        "_wait_for_grace_s (soft bound + grace for clean cancel)"
    )


# ---------------------------------------------------------------------------
# AST pins — _plan_create wired with D2 helper (4th SDK entry point)
# ---------------------------------------------------------------------------


def test_ast_pin_plan_create_uses_d2_helper():
    """``_plan_create`` in providers.py MUST now pass ``timeout=
    _derive_per_request_httpx_timeout(_attempt_budget_s)`` — closes
    the Task #95 D2 wiring gap that Task #97 discovered."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    # _plan_create is defined inside the plan() method; look for its
    # body containing the D2 helper call
    assert "_attempt_budget_s = max(" in src, (
        "_plan_create MUST compute _attempt_budget_s from the "
        "deadline before calling messages.create"
    )
    assert "timeout=_derive_per_request_httpx_timeout(_attempt_budget_s)" in src, (
        "_plan_create MUST pass timeout=_derive_per_request_httpx_timeout"
        "(_attempt_budget_s) — composes Task #95 D2 helper"
    )


def test_ast_pin_plan_create_inside_plan_method():
    """The fix MUST be localized to ``_plan_create`` inside ``plan(...)``
    — pin defense against accidentally wiring the helper at the wrong
    create call site."""
    src = _PROVIDERS_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Find the plan() async method
    plan_methods = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "plan"
            and any(
                isinstance(a, ast.arg) and a.arg == "deadline"
                for a in node.args.args
            )
        ):
            plan_methods.append(node)
    assert len(plan_methods) >= 1, (
        "providers.py MUST have at least one async plan(self, ..., "
        "deadline) method (ClaudeProvider.plan)"
    )
    # Find _plan_create inside one of them
    found = False
    for plan_fn in plan_methods:
        for sub in ast.walk(plan_fn):
            if (
                isinstance(sub, ast.AsyncFunctionDef)
                and sub.name == "_plan_create"
            ):
                # Look for the helper call inside _plan_create
                for inner in ast.walk(sub):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "_derive_per_request_httpx_timeout"
                    ):
                        found = True
                        break
    assert found, (
        "_derive_per_request_httpx_timeout MUST be called inside "
        "_plan_create (which lives inside the plan(deadline) async "
        "method).  This is the localization pin."
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_seed_fraction_flag_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_PLAN_PHASE_BUDGET_FRACTION" in src
    idx = src.find("JARVIS_PLAN_PHASE_BUDGET_FRACTION")
    window = src[idx:idx + 1500]
    assert "default=0.30" in window
    assert "Category.TUNING" in window
    assert "plan_generator.py" in window


def test_seed_reserve_flag_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S" in src
    idx = src.find("JARVIS_PLAN_PHASE_MIN_GENERATE_RESERVE_S")
    window = src[idx:idx + 1500]
    assert "default=60.0" in window
    assert "Category.TUNING" in window
    assert "plan_generator.py" in window


def test_seed_min_budget_flag_present():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_PLAN_PHASE_MIN_BUDGET_S" in src
    idx = src.find("JARVIS_PLAN_PHASE_MIN_BUDGET_S")
    window = src[idx:idx + 1500]
    assert "default=5.0" in window
    assert "Category.TUNING" in window
    assert "plan_generator.py" in window


# ---------------------------------------------------------------------------
# Load-bearing invariant — phase-local deadline never exceeds op deadline
# ---------------------------------------------------------------------------


def test_invariant_phase_budget_never_exceeds_op_remaining():
    """Sweep: for every (op_remaining, fraction) pair, the computed
    plan budget MUST NOT exceed op_remaining.  This is the "honest
    enforcement" invariant: PLAN cannot promise itself more time than
    the op actually has."""
    fn = _import_helper()
    for op_remaining in [0.0, 1.0, 30.0, 100.0, 500.0, 3600.0]:
        budget = fn(op_remaining)
        assert budget <= op_remaining + 1e-9, (
            f"Invariant violated: op_remaining={op_remaining} → "
            f"plan_budget={budget} (exceeds op_remaining!)"
        )


def test_invariant_generate_reserve_always_honored():
    """When op_remaining > min_generate_reserve, the computed plan
    budget MUST leave at least min_generate_reserve seconds for
    GENERATE."""
    fn = _import_helper()
    _, get_reserve, _ = _import_resolvers()
    reserve = get_reserve()
    for op_remaining in [reserve + 10.0, reserve + 100.0, reserve + 1000.0]:
        plan_budget = fn(op_remaining)
        remaining_for_generate = op_remaining - plan_budget
        assert remaining_for_generate >= reserve - 1e-9, (
            f"Reserve invariant violated: op_remaining={op_remaining} "
            f"plan_budget={plan_budget} → generate_left="
            f"{remaining_for_generate} < reserve={reserve}"
        )
