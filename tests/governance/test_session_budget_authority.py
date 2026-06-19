"""Session-Budget Preflight Authority spine.

Pins the load-bearing wallet gate added in response to the Step-1
verification soak (2026-05-21 ``bt-2026-05-21-010600``) where a single
Claude call consumed $0.1281 against a $0.10 session cap because
CostGovernor's per-op cap derivation produced a $1.20 ceiling that
exceeded the session budget.

Covers:
  * Adapter / protocol surface (set / get / reset / env fallback).
  * Preflight gate semantics (refuse / pass / fail-OPEN when no
    authority).
  * Structured ``SessionBudgetPreflightRefused`` shape.
  * CostGovernor session-aware clamp (fail-closed default).
  * AST pin: no governance → battle_test import cycle.
  * Harness registration is wired at construction.
  * Claude and DW providers both fire preflight before dispatch.
  * Master-OFF byte-identical behavior preserved.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.session_budget_authority import (
    SESSION_BUDGET_AUTHORITY_SCHEMA_VERSION,
    SessionBudgetPreflightRefused,
    check_preflight,
    get_session_budget_provider,
    get_session_remaining_usd,
    reset_for_tests,
    set_session_budget_provider,
)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    """Strip every relevant env knob + clear singleton between tests."""
    for k in (
        "JARVIS_S2_SESSION_BUDGET_USD",
        "OUROBOROS_BATTLE_COST_CAP",
        "JARVIS_COST_GOVERNOR_SESSION_CLAMP_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


# ============================================================================
# (1) Adapter / protocol — registration + env fallback
# ============================================================================


class _FakeProvider:
    """Duck-typed satisfies the protocol via `.remaining`."""
    def __init__(self, remaining: float):
        self._r = float(remaining)

    @property
    def remaining(self) -> float:
        return self._r

    def set_remaining(self, v: float) -> None:
        self._r = float(v)


def test_get_remaining_no_provider_no_env_returns_none():
    """Clean process default = None ⇒ preflight fail-OPEN
    (preserves byte-identical pre-PR behavior)."""
    assert get_session_budget_provider() is None
    assert get_session_remaining_usd() is None


def test_get_remaining_with_registered_provider():
    p = _FakeProvider(0.42)
    set_session_budget_provider(p)
    assert get_session_budget_provider() is p
    assert get_session_remaining_usd() == pytest.approx(0.42)


def test_get_remaining_env_fallback_s2_budget(monkeypatch):
    """Tier 1: JARVIS_S2_SESSION_BUDGET_USD."""
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.30")
    assert get_session_remaining_usd() == pytest.approx(0.30)


def test_get_remaining_env_fallback_battle_cost_cap(monkeypatch):
    """Tier 2: OUROBOROS_BATTLE_COST_CAP (when Tier 1 absent)."""
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "0.50")
    assert get_session_remaining_usd() == pytest.approx(0.50)


def test_get_remaining_provider_overrides_env(monkeypatch):
    """Registered provider beats env (operator-defined precedence)."""
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.99")
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "0.88")
    set_session_budget_provider(_FakeProvider(0.10))
    assert get_session_remaining_usd() == pytest.approx(0.10)


def test_get_remaining_provider_raises_falls_back_to_env(
    monkeypatch,
):
    """If provider.remaining raises, fall through to env fallback —
    fail-safe."""

    class _Raises:
        @property
        def remaining(self) -> float:
            raise RuntimeError("synthetic")

    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.25")
    set_session_budget_provider(_Raises())
    # Falls through env-fallback chain after provider read fails
    assert get_session_remaining_usd() == pytest.approx(0.25)


def test_get_remaining_provider_negative_clamped_to_zero():
    """Negative remaining clamped to 0 (no underflow into preflight)."""
    set_session_budget_provider(_FakeProvider(-1.0))
    assert get_session_remaining_usd() == 0.0


def test_get_remaining_garbage_env_falls_through(monkeypatch):
    """Garbage in Tier 1 falls through to Tier 2; garbage in both ⇒ None."""
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "not-a-number")
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "0.40")
    assert get_session_remaining_usd() == pytest.approx(0.40)
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "junk")
    assert get_session_remaining_usd() is None


# ============================================================================
# (2) Preflight gate semantics
# ============================================================================


def test_check_preflight_no_authority_is_noop():
    """No registered provider AND no env ⇒ no-op (fail-OPEN).
    Preserves byte-identical pre-PR behavior in environments without
    a session cap."""
    # Must not raise
    check_preflight(provider_name="claude", estimated_cost_usd=999.0)


def test_check_preflight_under_budget_passes():
    set_session_budget_provider(_FakeProvider(0.20))
    check_preflight(provider_name="claude", estimated_cost_usd=0.05)
    # No raise — passes


def test_check_preflight_over_budget_raises_structured():
    set_session_budget_provider(_FakeProvider(0.10))
    with pytest.raises(SessionBudgetPreflightRefused) as excinfo:
        check_preflight(
            provider_name="claude", estimated_cost_usd=0.50,
        )
    exc = excinfo.value
    assert exc.provider == "claude"
    assert exc.estimated_cost_usd == pytest.approx(0.50)
    assert exc.session_remaining_usd == pytest.approx(0.10)
    assert exc.reason == "session_budget_preflight_refused"


def test_check_preflight_equal_estimate_passes():
    """Boundary: est == remaining ⇒ pass (strict >). Allows the last
    op to fit at the line if no overage is expected."""
    set_session_budget_provider(_FakeProvider(0.10))
    check_preflight(provider_name="dw", estimated_cost_usd=0.10)
    # No raise


def test_check_preflight_bad_estimate_fails_open():
    """A misformed estimate (NaN-like / non-numeric) should fail-OPEN —
    don't block ops over a caller bug. Post-hoc CostTracker.budget_event
    remains the safety net."""
    set_session_budget_provider(_FakeProvider(0.10))
    # Strings / None: bad input
    check_preflight(
        provider_name="claude", estimated_cost_usd=float("nan"),
    )
    # No raise — fail-open


# ============================================================================
# (3) SessionBudgetPreflightRefused exception shape
# ============================================================================


def test_refused_exception_default_reason():
    exc = SessionBudgetPreflightRefused(
        provider="dw",
        estimated_cost_usd=0.50,
        session_remaining_usd=0.10,
    )
    assert exc.reason == "session_budget_preflight_refused"
    assert exc.provider == "dw"
    assert exc.estimated_cost_usd == 0.50
    assert exc.session_remaining_usd == 0.10
    assert "session_budget_preflight_refused" in str(exc)
    assert "$0.5000" in str(exc)


def test_refused_exception_custom_reason():
    exc = SessionBudgetPreflightRefused(
        provider="claude",
        estimated_cost_usd=1.0,
        session_remaining_usd=0.10,
        reason="custom_refusal_token",
    )
    assert exc.reason == "custom_refusal_token"
    assert "custom_refusal_token" in str(exc)


# ============================================================================
# (4) CostGovernor session-aware clamp
# ============================================================================


def test_cost_governor_clamp_to_session_remaining(monkeypatch):
    """Per-op cap derived by CostGovernor MUST be clamped down to
    session_remaining when authority is active. This is the load-
    bearing fix for the $0.10 → $0.1281 overage."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    # Register tight $0.10 session authority
    set_session_budget_provider(_FakeProvider(0.10))
    cfg = CostGovernorConfig()  # defaults
    g = CostGovernor(cfg)
    # Same conditions as bt-2026-05-21-010600: route=immediate
    # complexity=simple → baseline 0.10 × 5.00 × 0.80 × 3.00 = $1.20
    # Without clamp: cap should be $1.20.
    # With clamp: cap must be <= $0.10 (clamped to session_remaining)
    cap, _r, _c = g._derive_cap(
        route="immediate",
        complexity="simple",
        is_read_only=False,
        parallel_factor=1.0,
    )
    assert cap <= 0.10 + 1e-9, (
        f"Clamp failed: cap={cap} exceeds session_remaining=0.10. "
        f"The $0.1281 overage WOULD recur."
    )


def test_cost_governor_clamp_disabled_via_env(monkeypatch):
    """Operator emergency override: =false ⇒ legacy unclamped behavior."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    monkeypatch.setenv(
        "JARVIS_COST_GOVERNOR_SESSION_CLAMP_ENABLED", "false",
    )
    set_session_budget_provider(_FakeProvider(0.10))
    cfg = CostGovernorConfig()
    g = CostGovernor(cfg)
    cap, _, _ = g._derive_cap(
        route="immediate", complexity="simple",
        is_read_only=False, parallel_factor=1.0,
    )
    # With clamp disabled, cap reverts to derived multipliers:
    # 0.10 × 5.0 × 0.8 × 3.0 = 1.20 (clamped only to cfg.max_cap_usd)
    assert cap > 0.5, (
        f"Disabled clamp should NOT cap to session_remaining; cap={cap}"
    )


def test_cost_governor_clamp_master_default_true():
    """Verify default fail-closed (master ON unless operator opts out)."""
    from backend.core.ouroboros.governance.cost_governor import (
        _session_clamp_enabled,
    )
    assert _session_clamp_enabled() is True


def test_cost_governor_clamp_no_authority_no_change(monkeypatch):
    """When NO session authority is active AND clamp ON, behavior
    is unchanged (preserves byte-identical pre-PR behavior)."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    # No provider registered, no env knobs
    cfg = CostGovernorConfig()
    g = CostGovernor(cfg)
    cap_with, _, _ = g._derive_cap(
        route="standard", complexity="moderate",
        is_read_only=False, parallel_factor=1.0,
    )
    monkeypatch.setenv(
        "JARVIS_COST_GOVERNOR_SESSION_CLAMP_ENABLED", "false",
    )
    cap_without, _, _ = g._derive_cap(
        route="standard", complexity="moderate",
        is_read_only=False, parallel_factor=1.0,
    )
    assert cap_with == cap_without


def test_cost_governor_clamp_respects_min_cap_floor():
    """Even under tight session pressure, cap must not fall below
    cfg.min_cap_usd (operational floor — going lower starves ops)."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor, CostGovernorConfig,
    )
    # Session remaining well below min_cap_usd
    set_session_budget_provider(_FakeProvider(0.001))
    cfg = CostGovernorConfig()
    g = CostGovernor(cfg)
    cap, _, _ = g._derive_cap(
        route="standard", complexity="light",
        is_read_only=False, parallel_factor=1.0,
    )
    assert cap >= cfg.min_cap_usd, (
        f"Cap {cap} fell below cfg.min_cap_usd={cfg.min_cap_usd}"
    )


# ============================================================================
# (5) AST pins — composition discipline
# ============================================================================


_SBA_PATH = Path(
    "backend/core/ouroboros/governance/session_budget_authority.py"
)
_CG_PATH = Path(
    "backend/core/ouroboros/governance/cost_governor.py"
)
_PROVIDERS_PATH = Path(
    "backend/core/ouroboros/governance/providers.py"
)
_DW_PATH = Path(
    "backend/core/ouroboros/governance/doubleword_provider.py"
)
_HARNESS_PATH = Path(
    "backend/core/ouroboros/battle_test/harness.py"
)
_COST_TRACKER_PATH = Path(
    "backend/core/ouroboros/battle_test/cost_tracker.py"
)


def test_ast_pin_no_battle_test_import_in_session_budget_authority():
    """The new module must NOT import from battle_test (would create
    a governance → battle_test cycle)."""
    src = _SBA_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "battle_test" not in alias.name, (
                    f"session_budget_authority must not import battle_test "
                    f"(found: {alias.name})"
                )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "battle_test" not in mod, (
                f"session_budget_authority must not import from "
                f"battle_test (found: {mod})"
            )


def test_ast_pin_cost_tracker_does_not_depend_on_session_budget_authority():
    """The DIRECTION of the dependency must be harness → governance,
    not cost_tracker ↔ governance. cost_tracker.py itself stays free
    of session_budget_authority references — only the harness wires
    them together."""
    src = _COST_TRACKER_PATH.read_text(encoding="utf-8")
    assert "session_budget_authority" not in src, (
        "cost_tracker.py must not import session_budget_authority; "
        "the harness wires them together"
    )


def test_ast_pin_harness_registers_cost_tracker_at_init():
    """harness.py must call set_session_budget_provider(self._cost_tracker)
    AFTER constructing self._cost_tracker."""
    src = _HARNESS_PATH.read_text(encoding="utf-8")
    # The init sequence is on linear order: assignment first, then
    # registration. Confirm via substring + ordering.
    assign_idx = src.find("self._cost_tracker = CostTracker(")
    reg_idx = src.find("set_session_budget_provider(self._cost_tracker)")
    assert assign_idx > 0, "harness assignment to self._cost_tracker missing"
    assert reg_idx > assign_idx, (
        "set_session_budget_provider must appear AFTER the CostTracker "
        "assignment in harness.__init__"
    )


def test_ast_pin_cost_governor_imports_session_budget_authority():
    """CostGovernor's clamp logic must compose session_budget_authority,
    not re-implement remaining-budget reads."""
    src = _CG_PATH.read_text(encoding="utf-8")
    assert "session_budget_authority" in src
    assert "get_session_remaining_usd" in src


def test_ast_pin_claude_provider_calls_preflight():
    """ClaudeProvider.generate() body must invoke check_preflight
    BEFORE any provider dispatch / client construction."""
    src = _PROVIDERS_PATH.read_text(encoding="utf-8")
    assert "session_budget_authority" in src
    # The preflight call site must precede the first
    # _ensure_client / messages.* invocation.
    pre_idx = src.find("check_preflight")
    ensure_idx = src.find("client = self._ensure_client()")
    assert pre_idx > 0 and ensure_idx > 0
    assert pre_idx < ensure_idx, (
        "Claude preflight call must precede _ensure_client invocation"
    )


def test_ast_pin_dw_provider_extends_check_budget():
    """DW _check_budget body must invoke session_budget_authority
    after the daily budget check."""
    src = _DW_PATH.read_text(encoding="utf-8")
    # Find _check_budget block
    cb_idx = src.find("def _check_budget(self)")
    assert cb_idx > 0
    next_def = src.find("def _record_cost", cb_idx)
    body = src[cb_idx:next_def]
    assert "session_budget_authority" in body, (
        "DW _check_budget body must call session_budget_authority"
    )
    assert "check_preflight" in body
    # And the structural ordering: daily check first, preflight second
    daily_idx = body.find("doubleword_budget_exhausted")
    pre_idx = body.find("check_preflight")
    assert 0 < daily_idx < pre_idx, (
        "Daily-budget check must precede preflight call in _check_budget"
    )


def test_ast_pin_no_parallel_cost_ledger_in_session_budget_authority():
    """The new module must NOT define its own cost/budget ledger class
    — it only adapts existing surfaces."""
    src = _SBA_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node.name not in (
                "CostTracker", "CostGovernor", "_CostLedger",
                "BudgetLedger", "SessionLedger",
            ), (
                f"session_budget_authority must not define parallel "
                f"ledger class {node.name!r}"
            )


def test_schema_version_pinned():
    assert SESSION_BUDGET_AUTHORITY_SCHEMA_VERSION == (
        "session_budget_authority.v1"
    )


# ============================================================================
# (6) Provider-side preflight integration (mock at preflight seam)
# ============================================================================


@pytest.mark.asyncio
async def test_claude_preflight_refuses_before_client_construction(
    tmp_path,
):
    """Load-bearing: $0.10 remaining + Claude max_cost_per_op=$0.50
    → SessionBudgetPreflightRefused BEFORE _ensure_client is called."""
    from backend.core.ouroboros.governance.providers import (
        ClaudeProvider,
    )
    from backend.core.ouroboros.governance.op_context import (
        OperationContext,
    )
    import dataclasses
    from datetime import datetime, timedelta, timezone

    provider = ClaudeProvider(
        api_key="test-key",
        repo_root=tmp_path,
        max_cost_per_op=0.50,
        daily_budget=10.0,
    )
    # Spy on _ensure_client — it should NEVER be called
    ensure_calls: list = []
    real_ensure = provider._ensure_client

    def _spy_ensure():
        ensure_calls.append(1)
        return real_ensure()
    provider._ensure_client = _spy_ensure  # type: ignore[assignment]

    set_session_budget_provider(_FakeProvider(0.10))

    ctx = OperationContext.create(
        target_files=("x.py",),
        description="preflight test",
        op_id="op-preflight-refuse",
    )
    ctx = dataclasses.replace(ctx, provider_route="ide")
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=60)

    with pytest.raises(SessionBudgetPreflightRefused) as excinfo:
        await provider.generate(ctx, deadline)
    assert excinfo.value.provider == "claude"
    assert excinfo.value.session_remaining_usd == pytest.approx(0.10)
    assert ensure_calls == [], (
        "_ensure_client must NOT be called when preflight refuses; "
        f"saw {len(ensure_calls)} call(s)"
    )


def test_dw_check_budget_raises_session_refused_when_authority_tight(
    tmp_path,
):
    """DW _check_budget must raise SessionBudgetPreflightRefused
    (NOT DoublewordInfraError) when session authority refuses."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    provider = DoublewordProvider(
        api_key="test-dw-key",
        repo_root=tmp_path,
        max_cost_per_op=0.50,
        daily_budget=10.0,
    )
    set_session_budget_provider(_FakeProvider(0.10))

    with pytest.raises(SessionBudgetPreflightRefused) as excinfo:
        provider._check_budget()
    assert excinfo.value.provider == "doubleword"
    assert excinfo.value.session_remaining_usd == pytest.approx(0.10)


def test_dw_check_budget_unchanged_when_no_authority(tmp_path):
    """No session authority registered AND no env ⇒ DW _check_budget
    behaves byte-identically to pre-PR (only daily check fires)."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    provider = DoublewordProvider(
        api_key="test-dw-key",
        repo_root=tmp_path,
        max_cost_per_op=0.50,
        daily_budget=10.0,
    )
    # No provider registered, no env
    # _check_budget should pass cleanly (daily_spend=0 < daily_budget=10)
    provider._check_budget()
    # No raise


def test_dw_complete_sync_inherits_preflight_via_check_budget(tmp_path):
    """The DW heavy lane composes complete_sync() which calls
    _check_budget at line 2529 — verify the preflight propagates
    through that composition."""
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    provider = DoublewordProvider(
        api_key="test-dw-key",
        repo_root=tmp_path,
        max_cost_per_op=0.50,
        daily_budget=10.0,
    )
    set_session_budget_provider(_FakeProvider(0.10))

    # Spy on _get_session — must not be called if preflight refuses
    session_calls: list = []
    provider._get_session = AsyncMock(  # type: ignore[assignment]
        side_effect=lambda *a, **kw: session_calls.append(1),
    )

    import asyncio
    with pytest.raises(SessionBudgetPreflightRefused):
        asyncio.run(provider.complete_sync(
            prompt="x", system_prompt="y", caller_id="test_caller",
            timeout_s=10.0,
        ))
    assert session_calls == [], (
        "complete_sync must refuse via _check_budget BEFORE opening a session"
    )


# ============================================================================
# Sovereign Exec Engine (2026-06-19) — reservation TTL sweep (leak fix)
# ============================================================================
import time as _time
from backend.core.ouroboros.governance import session_budget_authority as _sba
from backend.core.ouroboros.governance.session_budget_authority import (
    acquire_reservation,
    get_reservations_snapshot,
    sweep_stale_reservations,
)


def test_ttl_sweep_releases_stale_reservation(monkeypatch):
    monkeypatch.setenv("JARVIS_SBA_RESERVATION_TTL_S", "600")
    set_session_budget_provider(_FakeProvider(5.0))
    assert acquire_reservation(op_id="op-a", signal_source="test_failure",
                               estimated_total_usd=0.50) is True
    assert len(get_reservations_snapshot()) == 1
    # age the clock past the TTL -> swept
    released = sweep_stale_reservations(now=_time.monotonic() + 700.0)
    assert released == 1
    assert get_reservations_snapshot() == ()


def test_ttl_sweep_keeps_fresh_reservation(monkeypatch):
    monkeypatch.setenv("JARVIS_SBA_RESERVATION_TTL_S", "600")
    set_session_budget_provider(_FakeProvider(5.0))
    acquire_reservation(op_id="op-b", signal_source="test_failure",
                        estimated_total_usd=0.50)
    released = sweep_stale_reservations(now=_time.monotonic() + 1.0)
    assert released == 0
    assert len(get_reservations_snapshot()) == 1


def test_ttl_zero_disables_sweep(monkeypatch):
    monkeypatch.setenv("JARVIS_SBA_RESERVATION_TTL_S", "0")
    set_session_budget_provider(_FakeProvider(5.0))
    acquire_reservation(op_id="op-c", signal_source="test_failure",
                        estimated_total_usd=0.50)
    assert sweep_stale_reservations(now=_time.monotonic() + 9999.0) == 0
    assert len(get_reservations_snapshot()) == 1


def test_lazy_sweep_in_acquire_reclaims_leaked_budget(monkeypatch):
    """The bug: op-A reserves the whole cap, never releases (queued+drained);
    op-B can never acquire. With the lazy TTL sweep, once A is stale, B's
    acquire reclaims it and succeeds."""
    monkeypatch.setenv("JARVIS_SBA_RESERVATION_TTL_S", "600")
    set_session_budget_provider(_FakeProvider(0.50))   # tiny cap, like the soak
    clock = {"t": 1000.0}
    monkeypatch.setattr(_sba.time, "monotonic", lambda: clock["t"])
    # A reserves the ENTIRE $0.50 cap
    assert acquire_reservation(op_id="op-A", signal_source="test_failure",
                               estimated_total_usd=0.50) is True
    # B cannot acquire — no liquidity (this is the live-soak lock)
    assert acquire_reservation(op_id="op-B", signal_source="test_failure",
                               estimated_total_usd=0.50) is False
    # ...time passes; A is now a stale leak (queued+drained, never released)
    clock["t"] = 1000.0 + 700.0
    # B retries — the lazy sweep inside acquire reclaims A, so B succeeds
    assert acquire_reservation(op_id="op-B", signal_source="test_failure",
                               estimated_total_usd=0.50) is True
    snap = {r.op_id for r in get_reservations_snapshot()}
    assert snap == {"op-B"}      # A swept, B holds the reservation
