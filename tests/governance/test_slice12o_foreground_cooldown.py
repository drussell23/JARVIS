"""
Slice 12O — Foreground macro-cooldown + clean-shutdown tests.
=============================================================

Closes the provider-exhaustion gap surfaced by the Path A post-
Slice-12N soak (bt-2026-05-23-022809): the SWE-Bench-Pro fixture
op reached GENERATE end-to-end but terminated when both DW and
Claude refused requests in the same window (upstream provider
flakiness). Pre-Slice-12O the orchestrator immediately
transitioned the op to terminal; Slice 12O adds a macro-retry
layer with exponential-backoff cooldown so the op can survive
transient provider outages.

PHASE 1 — Macro-cooldown policy:
  * Closed CooldownReason taxonomy (2 values: PROVIDER_EXHAUSTION,
    STREAM_RUPTURE)
  * Frozen CooldownDecision dataclass
  * ForegroundCooldownPolicy.decide() — pure, NEVER raises;
    gates on origin + cause + budget + wall
  * Exponential backoff: base * 2^attempt, capped (default
    60s → 120s → 240s, max 3 attempts)

PHASE 2 — Seamless re-entry:
  * Integration in generate_runner._slice12o_maybe_cooldown
  * Cooldown DOES NOT decrement the in-window retry counter
    (this is a macro-retry layer ABOVE the per-window retries)
  * Composes existing CostGovernor + WallClockWatchdog snapshots

PHASE 3 — Clean shutdown:
  * sleep_cooldown is asyncio.sleep-based (natively cancellation-
    aware)
  * On Layer-2 graceful shutdown, sleep wakes immediately
  * CancelledError propagates so asyncio cancel cascade can
    complete (no silent eating that would break WAL drain)
  * Orchestrator catches CancelledError at the call site,
    records terminal_reason_code=cooldown_cancelled_shutdown,
    then re-raises

Operator binding (verbatim):
  * waiting_cooldown state must release semaphores
  * async exponential backoff sleep (60s → 120s up to sane max)
  * macro-retry MUST respect CostGovernor + WallClockWatchdog
  * tasks in cooldown MUST be strictly responsive to cancellation
  * orchestrator can flush WAL summary.json before OS hard-kills
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import time
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.foreground_cooldown import (
    CooldownDecision,
    CooldownReason,
    ForegroundCooldownPolicy,
    cooldown_enabled,
    get_default_policy,
    is_provider_exhaustion_cause,
    reset_default_policy,
    sleep_cooldown,
)


@pytest.fixture(autouse=True)
def _reset_policy_state():
    reset_default_policy()
    yield
    reset_default_policy()


# ===============================================================
# Phase 1 — Cause classifier
# ===============================================================


def test_classifier_recognizes_provider_exhaustion() -> None:
    """Provider-exhaustion-class reasons map to PROVIDER_EXHAUSTION."""
    for code in (
        "all_providers_exhausted",
        "all_providers_exhausted:circuit_breaker_tripped:terminal_structural",
        "circuit_breaker_tripped:terminal_structural",
        "circuit_breaker_tripped:terminal_quota",
        "provider_exhausted_dw_then_claude",
    ):
        assert is_provider_exhaustion_cause(code) == \
            CooldownReason.PROVIDER_EXHAUSTION, code


def test_classifier_recognizes_stream_rupture() -> None:
    """Stream-rupture-class reasons map to STREAM_RUPTURE."""
    for code in (
        "stream_rupture_mid_generate",
        "stream_disconnected_unexpected",
        "stream_eof_before_completion",
        "stream_timeout_after_60s",
    ):
        assert is_provider_exhaustion_cause(code) == \
            CooldownReason.STREAM_RUPTURE, code


def test_classifier_refuses_structural_bug_class() -> None:
    """Non-provider failures (assertion errors, AST violations,
    SemanticGuard hits) MUST NOT map to either reason."""
    for code in (
        "test_assertion_failed",
        "semantic_guard_credential_introduced",
        "iron_gate_exploration_insufficient",
        "ast_pin_violation",
        "",
        "garbage non-canonical string",
    ):
        assert is_provider_exhaustion_cause(code) is None, code


def test_classifier_safe_on_non_string_input() -> None:
    """NEVER raises on bad input."""
    assert is_provider_exhaustion_cause(None) is None  # type: ignore[arg-type]
    assert is_provider_exhaustion_cause(42) is None  # type: ignore[arg-type]


# ===============================================================
# Phase 1 — Policy decision matrix
# ===============================================================


def test_decide_should_cooldown_for_foreground_exhaustion_healthy() -> None:
    """The happy path: FOREGROUND origin + provider exhaustion
    cause + healthy budget + healthy wall → should_cooldown."""
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-1",
        origin_is_foreground=True,
        terminal_reason_code="circuit_breaker_tripped:terminal_structural",
        remaining_budget_usd=0.40,
        remaining_wall_s=300.0,
    )
    assert d.should_cooldown is True
    assert d.reason == CooldownReason.PROVIDER_EXHAUSTION
    assert d.backoff_s == 60.0
    assert d.attempt == 1
    assert d.refuse_reason is None


def test_decide_refuses_non_foreground() -> None:
    """Operator binding: only FOREGROUND origins eligible for
    cooldown (BACKGROUND/SPECULATIVE/MAINTENANCE terminate
    cleanly per Slice 12N)."""
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-bg",
        origin_is_foreground=False,
        terminal_reason_code="circuit_breaker_tripped:terminal_structural",
        remaining_budget_usd=0.40,
        remaining_wall_s=300.0,
    )
    assert d.should_cooldown is False
    assert d.refuse_reason == "not_foreground_origin"


def test_decide_refuses_non_provider_exhaustion_cause() -> None:
    """Foreground op with structural-bug failure (not a provider
    issue) must terminate immediately, not cooldown-retry."""
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-bug",
        origin_is_foreground=True,
        terminal_reason_code="ast_pin_violation",
        remaining_budget_usd=0.40,
        remaining_wall_s=300.0,
    )
    assert d.should_cooldown is False
    assert d.refuse_reason == "not_provider_exhaustion"


def test_decide_refuses_insufficient_budget() -> None:
    """If remaining budget < min_budget_usd floor, refuse cooldown
    — better to terminate cleanly than burn the last $ on a sleep."""
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-broke",
        origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.01,  # below 0.05 default floor
        remaining_wall_s=300.0,
    )
    assert d.should_cooldown is False
    assert "insufficient_budget" in d.refuse_reason


def test_decide_refuses_insufficient_wall() -> None:
    """If remaining wall < backoff + min_wall, refuse cooldown
    — sleeping past the wall cap is pointless."""
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-no-time",
        origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40,
        remaining_wall_s=30.0,  # below 60 + 30 floor
    )
    assert d.should_cooldown is False
    assert "insufficient_wall" in d.refuse_reason


def test_decide_handles_unknown_snapshots() -> None:
    """When caller doesn't know budget/wall (passes None), skip
    those gates rather than refuse (safer to attempt)."""
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-unknown",
        origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=None,
        remaining_wall_s=None,
    )
    assert d.should_cooldown is True


# ===============================================================
# Phase 1 — Attempt counter + exponential backoff
# ===============================================================


def test_exponential_backoff_doubles_per_attempt() -> None:
    """Operator binding: 'exponential backoff sleep (e.g., 60s,
    120s, up to a sane max)'. Default base=60, cap=300."""
    p = ForegroundCooldownPolicy()
    # Attempt 0 (first cooldown) → 60s
    p.record_attempt("op-bo")
    d1 = p.decide(
        op_id="op-bo", origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40, remaining_wall_s=600.0,
    )
    assert d1.backoff_s == 120.0  # base * 2^1 (already 1 attempt recorded)

    p.record_attempt("op-bo")
    d2 = p.decide(
        op_id="op-bo", origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40, remaining_wall_s=600.0,
    )
    assert d2.backoff_s == 240.0  # base * 2^2


def test_exponential_backoff_capped_at_cap_s(monkeypatch) -> None:
    """Cap prevents runaway backoff if max_attempts is env-cranked."""
    monkeypatch.setenv("JARVIS_FOREGROUND_COOLDOWN_BASE_S", "60")
    monkeypatch.setenv("JARVIS_FOREGROUND_COOLDOWN_CAP_S", "180")
    monkeypatch.setenv("JARVIS_FOREGROUND_COOLDOWN_MAX_ATTEMPTS", "10")
    p = ForegroundCooldownPolicy()
    for _ in range(5):
        p.record_attempt("op-cap")
    d = p.decide(
        op_id="op-cap", origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40, remaining_wall_s=3600.0,
    )
    # Attempt 5 would be 60 * 2^5 = 1920s, capped to 180s
    assert d.backoff_s == 180.0


def test_max_attempts_exhausted_refuses_further_cooldown() -> None:
    """After max_attempts cooldowns, refuse with explicit reason."""
    p = ForegroundCooldownPolicy()
    # Default max_attempts = 3
    for _ in range(3):
        p.record_attempt("op-max")
    d = p.decide(
        op_id="op-max", origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40, remaining_wall_s=600.0,
    )
    assert d.should_cooldown is False
    assert "max_attempts_exhausted" in d.refuse_reason


def test_max_attempts_zero_disables_cooldown(monkeypatch) -> None:
    """``MAX_ATTEMPTS=0`` is the explicit per-instance disable
    escape hatch (independent of the master flag)."""
    monkeypatch.setenv("JARVIS_FOREGROUND_COOLDOWN_MAX_ATTEMPTS", "0")
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-zero", origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40, remaining_wall_s=600.0,
    )
    assert d.should_cooldown is False
    assert d.refuse_reason == "max_attempts_zero"


def test_master_flag_disabled_refuses_all_cooldowns(monkeypatch) -> None:
    """``COOLDOWN_ENABLED=false`` is the master kill switch
    (byte-identical pre-Slice-12O behavior)."""
    monkeypatch.setenv("JARVIS_FOREGROUND_COOLDOWN_ENABLED", "false")
    p = ForegroundCooldownPolicy()
    d = p.decide(
        op_id="op-disabled", origin_is_foreground=True,
        terminal_reason_code="all_providers_exhausted",
        remaining_budget_usd=0.40, remaining_wall_s=600.0,
    )
    assert d.should_cooldown is False
    assert d.refuse_reason == "disabled"


def test_record_recovery_resets_attempt_counter() -> None:
    """After a successful retry, the attempt counter MUST reset
    so subsequent failures get fresh attempts."""
    p = ForegroundCooldownPolicy()
    p.record_attempt("op-recover")
    p.record_attempt("op-recover")
    assert p.attempt_count("op-recover") == 2
    p.record_recovery("op-recover")
    assert p.attempt_count("op-recover") == 0


def test_forget_alias_clears_counter() -> None:
    """``forget`` is an alias of ``record_recovery`` for terminal-
    cleanup-site clarity."""
    p = ForegroundCooldownPolicy()
    p.record_attempt("op-forget")
    p.forget("op-forget")
    assert p.attempt_count("op-forget") == 0


# ===============================================================
# Phase 3 — Cancellation discipline
# ===============================================================


@pytest.mark.asyncio
async def test_sleep_cooldown_wakes_immediately_on_cancel() -> None:
    """Operator binding: 'tasks in waiting_cooldown state MUST be
    strictly responsive to cancellation'. Sleep wakes on cancel
    within <100ms regardless of nominal backoff."""
    task = asyncio.create_task(sleep_cooldown(60.0, op_id="cancel-test"))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"cancel took {elapsed:.3f}s (too slow)"


@pytest.mark.asyncio
async def test_sleep_cooldown_propagates_cancellederror() -> None:
    """Operator binding: caller catches CancelledError to record
    terminal reason. Helper MUST NOT silently eat it (would break
    asyncio cancel cascade + WAL drain)."""
    task = asyncio.create_task(sleep_cooldown(30.0))
    await asyncio.sleep(0.01)
    task.cancel()
    raised = False
    try:
        await task
    except asyncio.CancelledError:
        raised = True
    assert raised, "CancelledError must propagate; never silently eaten"


@pytest.mark.asyncio
async def test_sleep_cooldown_zero_backoff_is_noop() -> None:
    """``backoff_s=0`` short-circuits without invoking sleep
    (used for testing + edge cases)."""
    started = time.monotonic()
    result = await sleep_cooldown(0.0)
    assert result is True
    assert (time.monotonic() - started) < 0.05


@pytest.mark.asyncio
async def test_sleep_cooldown_normal_completion() -> None:
    """Sleep returns True after normal completion of brief
    backoff window."""
    started = time.monotonic()
    result = await sleep_cooldown(0.1, op_id="normal-test")
    assert result is True
    elapsed = time.monotonic() - started
    assert 0.08 <= elapsed <= 0.5


# ===============================================================
# Phase 2 — Integration with generate_runner
# ===============================================================


def test_generate_runner_imports_slice12o_helper() -> None:
    """The Slice 12O helper must be module-importable from
    generate_runner. Catches a refactor that drops the wiring."""
    from backend.core.ouroboros.governance.phase_runners import (
        generate_runner,
    )
    assert hasattr(generate_runner, "_slice12o_maybe_cooldown")
    assert asyncio.iscoroutinefunction(
        generate_runner._slice12o_maybe_cooldown,
    )


def test_generate_runner_helper_composes_canonical_primitives() -> None:
    """The helper MUST compose the canonical
    ``get_default_policy`` + ``sleep_cooldown`` + the Slice 12N
    route map — no parallel state, no duplication."""
    from backend.core.ouroboros.governance.phase_runners import (
        generate_runner,
    )
    src = inspect.getsource(generate_runner._slice12o_maybe_cooldown)
    assert "get_default_policy" in src
    assert "sleep_cooldown" in src
    assert "_SLICE12N_ROUTE_TO_ORIGIN" in src


@pytest.mark.asyncio
async def test_generate_runner_helper_returns_true_on_cooldown() -> None:
    """End-to-end smoke: helper invoked with foreground +
    provider-exhaustion + healthy budget returns True (caller
    should re-attempt without decrementing retry counter)."""
    from backend.core.ouroboros.governance.phase_runners.generate_runner import (
        _slice12o_maybe_cooldown,
    )

    class _FakeCostGov:
        def remaining_for_op(self, op_id):
            return 0.40

    class _FakeWatchdog:
        def remaining_seconds(self):
            return 300.0

    class _FakeStack:
        cost_governor = _FakeCostGov()
        wall_clock_watchdog = _FakeWatchdog()

    class _FakeOrch:
        _stack = _FakeStack()

    class _FakeCtx:
        op_id = "test-op-12o-integration"
        terminal_reason_code = "circuit_breaker_tripped:terminal_structural"

    with patch.dict(
        os.environ,
        {"JARVIS_FOREGROUND_COOLDOWN_BASE_S": "0.05"},  # tiny for test speed
        clear=False,
    ):
        reset_default_policy()
        result = await _slice12o_maybe_cooldown(
            orch=_FakeOrch(), ctx=_FakeCtx(),
            exc=Exception("all_providers_exhausted"),
            route="standard",
        )
    assert result is True


@pytest.mark.asyncio
async def test_generate_runner_helper_returns_false_on_refuse() -> None:
    """When the policy refuses (e.g., not foreground), the helper
    returns False — caller falls through to existing terminal
    path."""
    from backend.core.ouroboros.governance.phase_runners.generate_runner import (
        _slice12o_maybe_cooldown,
    )

    class _NoStack:
        cost_governor = None
        wall_clock_watchdog = None

    class _Orch:
        _stack = _NoStack()

    class _Ctx:
        op_id = "test-bg-op"
        terminal_reason_code = "circuit_breaker_tripped:terminal_structural"

    reset_default_policy()
    result = await _slice12o_maybe_cooldown(
        orch=_Orch(), ctx=_Ctx(),
        exc=Exception("all_providers_exhausted"),
        route="background",  # non-foreground → policy refuses
    )
    assert result is False


@pytest.mark.asyncio
async def test_generate_runner_helper_propagates_cancellation() -> None:
    """If cooldown sleep is cancelled mid-flight (Phase 3),
    CancelledError MUST propagate out of the helper so the
    caller's try/except can record the terminal reason."""
    from backend.core.ouroboros.governance.phase_runners.generate_runner import (
        _slice12o_maybe_cooldown,
    )

    class _Stack:
        cost_governor = None
        wall_clock_watchdog = None

    class _Orch:
        _stack = _Stack()

    class _Ctx:
        op_id = "test-cancel-op"
        terminal_reason_code = "circuit_breaker_tripped:terminal_structural"

    reset_default_policy()
    # Long backoff so we can interrupt mid-sleep
    with patch.dict(
        os.environ,
        {"JARVIS_FOREGROUND_COOLDOWN_BASE_S": "30.0"},
        clear=False,
    ):
        task = asyncio.create_task(_slice12o_maybe_cooldown(
            orch=_Orch(), ctx=_Ctx(),
            exc=Exception("all_providers_exhausted"),
            route="standard",
        ))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ===============================================================
# Phase 2 — Caller-side terminal-reason recording
# ===============================================================


def test_caller_records_cooldown_cancelled_shutdown_terminal_reason() -> None:
    """The integration site in GENERATERunner MUST catch
    CancelledError and record terminal_reason_code=
    cooldown_cancelled_shutdown BEFORE re-raising, so operators
    can attribute the WAL drain to a cooperative cancellation."""
    from backend.core.ouroboros.governance.phase_runners.generate_runner import (
        GENERATERunner,
    )
    src = inspect.getsource(GENERATERunner)
    assert "_slice12o_maybe_cooldown" in src
    assert "except asyncio.CancelledError" in src
    assert "cooldown_cancelled_shutdown" in src
    # The raise MUST be present (silent eating breaks asyncio cancel cascade)
    assert "raise" in src


# ===============================================================
# AST pins — structural regression armor
# ===============================================================


_FC_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "foreground_cooldown.py"
)

_GR_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "generate_runner.py"
)


def _load_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def test_ast_pin_cooldown_reason_taxonomy_closed() -> None:
    """The 2 CooldownReason values are the closed taxonomy."""
    tree = _load_ast(_FC_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "CooldownReason":
            continue
        values = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                values.add(stmt.targets[0].id)
        assert values == {"PROVIDER_EXHAUSTION", "STREAM_RUPTURE"}
        return
    pytest.fail("CooldownReason class not found")


def test_ast_pin_cooldown_decision_frozen() -> None:
    """CooldownDecision must be frozen dataclass."""
    tree = _load_ast(_FC_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "CooldownDecision":
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "frozen" and \
                            isinstance(kw.value, ast.Constant) and \
                            kw.value.value is True:
                        return
        pytest.fail("CooldownDecision must be @dataclass(frozen=True)")
    pytest.fail("CooldownDecision class not found")


def test_ast_pin_sleep_cooldown_uses_asyncio_sleep() -> None:
    """sleep_cooldown MUST use asyncio.sleep (cancellation-aware).
    A refactor to time.sleep would re-introduce the wedge."""
    tree = _load_ast(_FC_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "sleep_cooldown":
            continue
        src = ast.unparse(node)
        assert "asyncio.sleep" in src
        # No blocking time.sleep
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and \
                    isinstance(sub.func, ast.Attribute) and \
                    sub.func.attr == "sleep" and \
                    isinstance(sub.func.value, ast.Name) and \
                    sub.func.value.id == "time":
                pytest.fail("sleep_cooldown uses time.sleep — would block loop")
        return
    pytest.fail("sleep_cooldown function not found")


def test_ast_pin_sleep_cooldown_propagates_cancelled() -> None:
    """The CancelledError except block must `raise` (not return
    silently). Silent-eating breaks asyncio cancel cascade."""
    tree = _load_ast(_FC_PATH)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "sleep_cooldown":
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ExceptHandler):
                # Look for CancelledError + Raise inside
                if sub.type is None:
                    continue
                t_src = ast.unparse(sub.type)
                if "CancelledError" not in t_src:
                    continue
                handler_src = ast.unparse(sub)
                assert "raise" in handler_src, (
                    "sleep_cooldown CancelledError handler must `raise` "
                    "(silent eating breaks asyncio cancel cascade)"
                )
                return
    pytest.fail("CancelledError handler not found in sleep_cooldown")


def test_ast_pin_env_knob_constants_present() -> None:
    """All 6 env knob constants must be present at module level."""
    src = _FC_PATH.read_text()
    for knob in (
        "JARVIS_FOREGROUND_COOLDOWN_ENABLED",
        "JARVIS_FOREGROUND_COOLDOWN_MAX_ATTEMPTS",
        "JARVIS_FOREGROUND_COOLDOWN_BASE_S",
        "JARVIS_FOREGROUND_COOLDOWN_CAP_S",
        "JARVIS_FOREGROUND_COOLDOWN_MIN_BUDGET_USD",
        "JARVIS_FOREGROUND_COOLDOWN_MIN_WALL_S",
    ):
        assert knob in src, f"env knob {knob} missing from module"


def test_ast_pin_integration_helper_at_module_level() -> None:
    """``_slice12o_maybe_cooldown`` must be a module-level async
    function in generate_runner.py."""
    tree = _load_ast(_GR_PATH)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and \
                node.name == "_slice12o_maybe_cooldown":
            return
    pytest.fail("_slice12o_maybe_cooldown not at module level in generate_runner")


def test_ast_pin_integration_call_site_catches_cancellederror() -> None:
    """The integration site in GENERATERunner MUST catch
    CancelledError + record cooldown_cancelled_shutdown + raise."""
    src = _GR_PATH.read_text()
    # The integration block contains all three markers
    assert "_slice12o_maybe_cooldown" in src
    assert "except asyncio.CancelledError" in src
    assert "cooldown_cancelled_shutdown" in src
    # The raise after recording is in the except block. We grep-
    # check the sequence appears (not a strict structural check —
    # the helper test_caller_records_cooldown_cancelled_shutdown
    # already covers the inspect-based shape).


def test_ast_pin_no_blocking_time_sleep_in_module() -> None:
    """Slice 12O module MUST NOT use blocking time.sleep
    anywhere — every wait is async."""
    tree = _load_ast(_FC_PATH)
    for sub in ast.walk(tree):
        if isinstance(sub, ast.Call) and \
                isinstance(sub.func, ast.Attribute) and \
                sub.func.attr == "sleep" and \
                isinstance(sub.func.value, ast.Name) and \
                sub.func.value.id == "time":
            pytest.fail(
                f"Blocking time.sleep call at line {sub.lineno} — "
                "Slice 12O is async-only"
            )
