"""Task #21 regression spine — Dynamic Timeout Coherence.

A″ proved the inversion: inner SWE-Bench eval timeout (default 1800s)
>= outer session wall cap ⇒ the session is killed before
``evaluate_problem``'s ``wait_for`` raises ⇒ NO autoscore verdict
EVER. Structural fix (NOT a config workaround): the harness publishes
its absolute WallClockWatchdog deadline to an env var; the evaluator
clamps ``eval = min(configured, wall_remaining - drain_buffer)`` so
the inner wait ALWAYS ends + emits TERMINAL_TIMEOUT before the outer
bounded-shutdown — removing human config-error from the threat model.

Pins:
  * no deadline env → byte-identical legacy (configured passthrough,
    all precedence paths: explicit / env / default / invalid)
  * far deadline (remaining ≫ configured) → configured (min)
  * near deadline → clamped to remaining − drain_buffer
  * expired/negative budget → _MIN_EVAL_FLOOR_S (never <=0)
  * invalid deadline env → legacy passthrough (fail-open)
  * drain_buffer composes the EXISTING autoscore grace knob
  * AST: evaluator never imports battle_test; _resolve_timeout_s
    composes _apply_wall_coherence; uses min(); reads the named
    env const (no literal); harness publishes it at the arm site
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import evaluator as E
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    _apply_wall_coherence,
    _eval_drain_buffer_s,
    _resolve_timeout_s,
    _MIN_EVAL_FLOOR_S,
    _DEFAULT_TIMEOUT_S,
    _SHUTDOWN_DEADLINE_ENV_VAR,
    _DRAIN_MARGIN_ENV_VAR,
    _DEFAULT_SHUTDOWN_DEADLINE_S,
    _DEFAULT_AUTOSCORE_GRACE_S,
    _DEFAULT_DRAIN_MARGIN_S,
    WALL_DEADLINE_ENV_VAR,
)

_REPO = Path(__file__).resolve().parents[2]
_EVAL_SRC = _REPO / "backend/core/ouroboros/governance/swe_bench_pro/evaluator.py"
_HARNESS_SRC = _REPO / "backend/core/ouroboros/battle_test/harness.py"
_WATCHDOG_SRC = _REPO / "backend/core/ouroboros/battle_test/shutdown_watchdog.py"
_DEADLINE = WALL_DEADLINE_ENV_VAR
_GRACE = "JARVIS_SWE_BENCH_PRO_AUTOSCORE_SHUTDOWN_GRACE_S"
_DRAIN = "JARVIS_SWE_BENCH_PRO_EVAL_DRAIN_BUFFER_S"
_EVT = "JARVIS_SWE_BENCH_PRO_EVAL_TIMEOUT_S"
_SHUT = _SHUTDOWN_DEADLINE_ENV_VAR
_MARGIN = _DRAIN_MARGIN_ENV_VAR


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (_DEADLINE, _GRACE, _DRAIN, _EVT, _SHUT, _MARGIN):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------
# Legacy passthrough (no deadline ⇒ byte-identical pre-#21 behaviour)
# --------------------------------------------------------------------------

def test_legacy_default_passthrough():
    assert _resolve_timeout_s(None) == _DEFAULT_TIMEOUT_S


def test_legacy_explicit_passthrough():
    assert _resolve_timeout_s(123.0) == 123.0


def test_legacy_env_passthrough(monkeypatch):
    monkeypatch.setenv(_EVT, "777")
    assert _resolve_timeout_s(None) == 777.0


def test_legacy_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(_EVT, "not-a-number")
    assert _resolve_timeout_s(None) == _DEFAULT_TIMEOUT_S


# --------------------------------------------------------------------------
# Coherence clamp
# --------------------------------------------------------------------------

def test_far_deadline_returns_configured(monkeypatch):
    monkeypatch.setenv(_DEADLINE, repr(time.monotonic() + 100_000))
    assert _resolve_timeout_s(300.0) == 300.0  # min picks configured


def test_near_deadline_clamps_to_remaining_minus_drain(monkeypatch):
    monkeypatch.setenv(_DEADLINE, repr(time.monotonic() + 200.0))
    drain = _eval_drain_buffer_s()           # 2*30 default = 60
    v = _resolve_timeout_s(1800.0)
    assert 0 < v < 1800.0
    assert abs(v - (200.0 - drain)) < 5.0     # ~140s


def test_expired_budget_returns_floor_never_negative(monkeypatch):
    monkeypatch.setenv(_DEADLINE, repr(time.monotonic() - 50.0))
    v = _resolve_timeout_s(1800.0)
    assert v == _MIN_EVAL_FLOOR_S and v > 0


def test_invalid_deadline_env_is_failopen_passthrough(monkeypatch):
    monkeypatch.setenv(_DEADLINE, "garbage")
    assert _resolve_timeout_s(1800.0) == 1800.0


def test_drain_buffer_is_composed_sum_not_2x_grace(monkeypatch):
    # Task #22: drain = shutdown_deadline + autoscore_grace + margin
    # (NOT the old 2×grace heuristic). Distinct values prove the sum.
    assert _eval_drain_buffer_s() == (
        _DEFAULT_SHUTDOWN_DEADLINE_S
        + _DEFAULT_AUTOSCORE_GRACE_S
        + _DEFAULT_DRAIN_MARGIN_S
    )  # 30 + 30 + 15 = 75 (NOT 2*30=60)
    monkeypatch.setenv(_SHUT, "50")
    monkeypatch.setenv(_GRACE, "40")
    monkeypatch.setenv(_MARGIN, "20")
    assert _eval_drain_buffer_s() == 110.0      # 50+40+20, not 2*40
    monkeypatch.setenv(_DRAIN, "12.5")          # explicit override wins
    assert _eval_drain_buffer_s() == 12.5
    monkeypatch.delenv(_DRAIN, raising=False)
    # invalid components fall back to per-term defaults (never raises)
    monkeypatch.setenv(_SHUT, "nope")
    monkeypatch.setenv(_GRACE, "-5")
    monkeypatch.setenv(_MARGIN, "")
    assert _eval_drain_buffer_s() == (
        _DEFAULT_SHUTDOWN_DEADLINE_S
        + _DEFAULT_AUTOSCORE_GRACE_S
        + _DEFAULT_DRAIN_MARGIN_S
    )


def test_apply_wall_coherence_is_pure_no_deadline(monkeypatch):
    # Direct helper: absent env → unchanged.
    assert _apply_wall_coherence(999.0) == 999.0


# --------------------------------------------------------------------------
# AST / structural pins
# --------------------------------------------------------------------------

def test_ast_pin_evaluator_never_imports_battle_test():
    tree = ast.parse(_EVAL_SRC.read_text())
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
        elif isinstance(node, ast.Import):
            mod = ",".join(a.name for a in node.names)
        if mod and "battle_test" in mod:
            pytest.fail(
                f"evaluator must NOT import battle_test ({mod}) — "
                f"the wall deadline is an env-var seam, not an import"
            )


def test_ast_pin_resolve_composes_coherence_and_min():
    src = _EVAL_SRC.read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_resolve_timeout_s"
    )
    body = ast.unparse(fn)
    assert "_apply_wall_coherence" in body, (
        "_resolve_timeout_s must route through the coherence clamp"
    )
    coh = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_apply_wall_coherence"
    )
    coh_src = ast.unparse(coh)
    assert "min(" in coh_src, "coherence must be min(configured, ...)"
    assert "WALL_DEADLINE_ENV_VAR" in coh_src, (
        "must read the named deadline env const, not a literal"
    )


def test_ast_pin_harness_publishes_deadline_at_arm():
    src = _HARNESS_SRC.read_text()
    assert 'os.environ["OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC"]' in src
    # Published only inside the wall-cap-armed branch (composes _wall_cap).
    arm = src.index("if _wall_cap is not None and _wall_cap > 0:")
    pub = src.index('os.environ["OUROBOROS_BATTLE_WALL_DEADLINE_MONOTONIC"]')
    nxt = src.index("def ", arm)
    assert arm < pub < nxt, (
        "deadline publish must be inside the wall-cap-armed branch"
    )


# --------------------------------------------------------------------------
# Task #22 — teardown-coherence pins
# --------------------------------------------------------------------------

def test_ast_pin_shutdown_deadline_env_parity_no_import():
    """evaluator's shutdown-deadline env string MUST equal the one
    shutdown_watchdog.default_deadline_s reads — proven by parity,
    NOT by importing battle_test (the AST pin below still holds)."""
    wd = _WATCHDOG_SRC.read_text()
    # shutdown_watchdog.default_deadline_s uses this exact literal.
    assert 'os.environ.get("JARVIS_BATTLE_SHUTDOWN_DEADLINE_S"' in wd, (
        "shutdown_watchdog env string changed — update evaluator "
        "_SHUTDOWN_DEADLINE_ENV_VAR parity + this pin together"
    )
    assert _SHUTDOWN_DEADLINE_ENV_VAR == "JARVIS_BATTLE_SHUTDOWN_DEADLINE_S"


def test_ast_pin_drain_composes_three_terms_not_2x():
    tree = ast.parse(_EVAL_SRC.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_eval_drain_buffer_s"
    )
    body = ast.unparse(fn)
    assert "_SHUTDOWN_DEADLINE_ENV_VAR" in body
    assert "_AUTOSCORE_GRACE_ENV_VAR" in body
    assert "_DRAIN_MARGIN_ENV_VAR" in body
    assert "* 2" not in body and "*2" not in body, (
        "the 2×grace heuristic must be gone (Task #22)"
    )


def test_ast_pin_harness_wall_path_composes_deadline_when_autoscore_inflight():
    src = _HARNESS_SRC.read_text()
    # The wall_clock_cap bounded-watchdog arm must consult
    # autoscore_work_in_flight and extend the deadline by grace+margin
    # so os._exit cannot fire before the drain flushes the verdict.
    arm = src.index('_wdg.arm(\n                    reason="wall_clock_cap"')
    region = src[src.rindex("try:", 0, arm) - 200: arm]
    assert "autoscore_work_in_flight" in region, (
        "wall-cap arm must consult autoscore_work_in_flight"
    )
    assert "_arm_deadline" in region and (
        "_grace + _margin" in region or "_grace" in region
    ), "wall-cap arm must extend deadline by grace(+margin) in-flight"


def test_ast_pin_evaluator_still_never_imports_battle_test_after_22():
    tree = ast.parse(_EVAL_SRC.read_text())
    for node in ast.walk(tree):
        mod = None
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
        elif isinstance(node, ast.Import):
            mod = ",".join(a.name for a in node.names)
        if mod and "battle_test" in mod:
            pytest.fail(f"evaluator imported battle_test ({mod}) — #22 "
                        f"must keep the env-string-parity seam")
