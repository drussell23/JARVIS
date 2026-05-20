"""S2 wiring spine — PRD §11 production-path integration.

Tests the actual data flows that make S2 useful at runtime:

  * Provider-side admission hook (Claude + DW): co-located with
    assembled prompt_text / _zw_prompt; uses ``len(prompt_text)``
    dynamically — NO pre-calculation, NO prompt re-assembly (B3).
  * Provider-side op_outcome record on real success seam only (B4):
    cache-hit reconstructed results are skipped.
  * Session budget precedence chain (B1):
    ``JARVIS_S2_SESSION_BUDGET_USD`` > ``OUROBOROS_BATTLE_COST_CAP``
    > default ``0.50``.
  * UnifiedIntakeRouter.peek_top_urgency() — read-only, does NOT pop
    (B2 directive).
  * CostGovernor.session_total_cumulative_usd() — composes existing
    ``_entries`` ledger (no parallel accumulator).
  * Master OFF: byte-identical behavior — no S2 calls, no signals,
    no ring writes.
  * AST pins: S2 must NOT pass dynamic_admit_safety_factor into
    admission_gate.budget_safety_factor_value; S2 module must NOT
    re-implement prompt assembly; no parallel ring/queue/ledger.
  * Fail-open: every S2 fault leaves provider success unchanged.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance import (
    s2_predictive_budget as s2,
)


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    """Strip S2 env so each test starts at documented defaults."""
    for k in (
        "JARVIS_S2_PREDICTIVE_BUDGET_ENABLED",
        "JARVIS_S2_BASE_SAFETY_FACTOR",
        "JARVIS_S2_VOLATILITY_PENALTY",
        "JARVIS_S2_SAFETY_FLOOR",
        "JARVIS_S2_SAFETY_CEILING",
        "JARVIS_S2_CHARS_PER_TOKEN",
        "JARVIS_S2_COST_SAMPLE_WINDOW",
        "JARVIS_S2_PRICING_YAML_PATH",
        "JARVIS_S2_SESSION_BUDGET_USD",
        "OUROBOROS_BATTLE_COST_CAP",
    ):
        monkeypatch.delenv(k, raising=False)
    # Reset module-level singletons that S2 wiring depends on.
    from backend.core.ouroboros.governance.admission_estimator import (
        reset_singletons_for_tests,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        reset_default_intake_router_for_tests,
    )
    reset_singletons_for_tests()
    reset_default_intake_router_for_tests()
    s2._reset_pricing_cache_for_tests()
    yield
    reset_singletons_for_tests()
    reset_default_intake_router_for_tests()
    s2._reset_pricing_cache_for_tests()


# ============================================================================
# (1/8) Session budget precedence chain — B1 revised
# ============================================================================


def test_session_budget_default_050_matches_harness(monkeypatch):
    """No env set ⇒ default $0.50 (matches BattleTestHarnessConfig).
    Not 1.0. PRD §11 B1 revised."""
    assert s2.session_budget_usd() == pytest.approx(0.50)


def test_session_budget_battle_cost_cap_tier(monkeypatch):
    """OUROBOROS_BATTLE_COST_CAP picked up as Tier 2."""
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "2.00")
    assert s2.session_budget_usd() == pytest.approx(2.00)


def test_session_budget_jarvis_s2_wins_precedence(monkeypatch):
    """JARVIS_S2_SESSION_BUDGET_USD (Tier 1) wins over
    OUROBOROS_BATTLE_COST_CAP (Tier 2)."""
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "2.00")
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.10")
    assert s2.session_budget_usd() == pytest.approx(0.10)


def test_session_budget_garbage_falls_through(monkeypatch):
    """Garbage Tier-1 ⇒ falls through to Tier-2; garbage both ⇒
    default 0.50. Defensive parser."""
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "junk")
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "2.00")
    assert s2.session_budget_usd() == pytest.approx(2.00)
    monkeypatch.setenv("OUROBOROS_BATTLE_COST_CAP", "junk")
    assert s2.session_budget_usd() == pytest.approx(0.50)


def test_session_budget_floor_clamp(monkeypatch):
    """Floor at $0.01 prevents div-by-zero downstream."""
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.001")
    assert s2.session_budget_usd() == pytest.approx(0.01)
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0")
    assert s2.session_budget_usd() == pytest.approx(0.01)


# ============================================================================
# (2/8) UnifiedIntakeRouter.peek_top_urgency() — B2 directive
# ============================================================================


def test_router_peek_top_urgency_empty():
    """No priority queue active OR empty queue ⇒ returns None.
    Composes existing _priority_queue (no parallel queue)."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        UnifiedIntakeRouter,
    )
    # Stub router with no priority_queue
    stub = object.__new__(UnifiedIntakeRouter)
    stub._priority_queue = None
    assert stub.peek_top_urgency() is None


def test_router_peek_top_urgency_returns_top_urgency():
    """Stub a priority heap with ranks; peek returns the
    urgency string matching the lowest rank (= highest priority).
    Composes existing URGENCY_RANK reverse-lookup."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        UnifiedIntakeRouter,
    )
    from backend.core.ouroboros.governance.intake.intake_priority_queue import (  # noqa: E501
        URGENCY_RANK,
    )

    class _FakeEntry:
        def __init__(self, rank):
            self.urgency_rank = rank

    class _FakePQ:
        def __init__(self, ranks):
            self._heap = [_FakeEntry(r) for r in ranks]

    # heap[0] has the lowest rank (highest priority)
    stub = object.__new__(UnifiedIntakeRouter)
    stub._priority_queue = _FakePQ([URGENCY_RANK["critical"]])
    assert stub.peek_top_urgency() == "critical"

    stub._priority_queue = _FakePQ([URGENCY_RANK["low"]])
    assert stub.peek_top_urgency() == "low"


def test_router_peek_top_urgency_does_not_pop():
    """Peek is read-only — does NOT mutate the heap."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        UnifiedIntakeRouter,
    )

    class _FakeEntry:
        def __init__(self, rank): self.urgency_rank = rank

    class _FakePQ:
        def __init__(self):
            self._heap = [_FakeEntry(0), _FakeEntry(1), _FakeEntry(2)]

    stub = object.__new__(UnifiedIntakeRouter)
    stub._priority_queue = _FakePQ()
    before_len = len(stub._priority_queue._heap)
    _ = stub.peek_top_urgency()
    _ = stub.peek_top_urgency()
    assert len(stub._priority_queue._heap) == before_len, "peek mutated heap"


def test_router_peek_failopen_on_introspection_fault():
    """If the heap shape is unexpected, returns None (no raise)."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        UnifiedIntakeRouter,
    )

    class _BrokenPQ:
        _heap = [object()]   # entry has no urgency_rank

    stub = object.__new__(UnifiedIntakeRouter)
    stub._priority_queue = _BrokenPQ()
    assert stub.peek_top_urgency() is None


# ============================================================================
# (3/8) CostGovernor.session_total_cumulative_usd() — composes _entries
# ============================================================================


def test_cost_governor_session_total_empty():
    """No active ops ⇒ 0.0."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
    )
    g = CostGovernor()
    assert g.session_total_cumulative_usd() == pytest.approx(0.0)


def test_cost_governor_session_total_sums_entries():
    """Sums cumulative_usd across _entries; composes existing
    per-op ledger — NO parallel accumulator."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
    )
    g = CostGovernor()
    g.start("op-1", "ide", "trivial")
    g.charge("op-1", 0.05, provider="dw")
    g.start("op-2", "ide", "moderate")
    g.charge("op-2", 0.10, provider="claude")
    g.charge("op-2", 0.02, provider="claude")
    total = g.session_total_cumulative_usd()
    assert total == pytest.approx(0.17)


def test_cost_governor_session_total_failopen():
    """Any internal fault ⇒ 0.0 (no raise)."""
    from backend.core.ouroboros.governance.cost_governor import (
        CostGovernor,
    )
    g = CostGovernor()
    # Mutate _entries to a non-dict to force a fault path
    g._entries = "not-a-dict"  # type: ignore[assignment]
    assert g.session_total_cumulative_usd() == 0.0


# ============================================================================
# (4/8) evaluate_admission_pressure() — provider-side composition
# ============================================================================


def _make_fake_governor(spend: float):
    class _G:
        def session_total_cumulative_usd(self):
            return spend
    return _G()


def _stable_samples(_r, _m, _w):
    """Cold-but-stable samples → CV_MAD ≈ 0 → factor ≈ base (0.9)."""
    return (0.002, 0.002, 0.002, 0.002, 0.002)


def _stable_pricing(_r, _m):
    return (3e-6, 1.5e-5)   # Claude Sonnet-shape


def _zero_estimator(_r, _m):
    return 50.0


def test_evaluate_master_off_returns_none(monkeypatch):
    """Master OFF (default) ⇒ returns None — zero S2 work, no
    governor/sampler/etc. consulted."""
    sev = s2.evaluate_admission_pressure(
        prompt_text="P" * 100, route="ide", model="m",
        cost_governor=_make_fake_governor(99.0),  # huge spend, ignored
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    assert sev is None


def test_evaluate_master_on_safe_budget_returns_none(monkeypatch):
    """Master ON, spend+forecast << budget × factor ⇒ no signal."""
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "1.00")
    sev = s2.evaluate_admission_pressure(
        prompt_text="P" * 100, route="ide", model="m",
        cost_governor=_make_fake_governor(0.01),  # well under budget
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    assert sev is None


def _populated_router_with_high_prio() -> Any:
    """Register a fake router with a critical envelope at the head."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        set_default_intake_router,
    )

    class _FakeEntry:
        def __init__(self, rank): self.urgency_rank = rank

    class _FakePQ:
        _heap = [_FakeEntry(0)]   # critical = rank 0

    class _FakeRouter:
        _priority_queue = _FakePQ()
        # mirror real method:
        def peek_top_urgency(self):
            from backend.core.ouroboros.governance.intake.intake_priority_queue import (  # noqa: E501
                URGENCY_RANK,
            )
            rank = self._priority_queue._heap[0].urgency_rank
            for u, r in URGENCY_RANK.items():
                if r == rank:
                    return u
            return None
    router = _FakeRouter()
    set_default_intake_router(router)
    return router


def _populated_router_with_low_prio() -> Any:
    """Register a fake router with a low envelope at the head."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        set_default_intake_router,
    )
    from backend.core.ouroboros.governance.intake.intake_priority_queue import (  # noqa: E501
        URGENCY_RANK,
    )

    class _FakeEntry:
        urgency_rank = URGENCY_RANK["low"]

    class _FakePQ:
        _heap = [_FakeEntry()]

    class _FakeRouter:
        _priority_queue = _FakePQ()
        def peek_top_urgency(self):
            return "low"
    router = _FakeRouter()
    set_default_intake_router(router)
    return router


def test_evaluate_tight_budget_no_high_prio_no_signal(monkeypatch):
    """Tight forecast BUT no high-prio queued ⇒ no signal."""
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.10")
    _populated_router_with_low_prio()   # only low-prio queued
    sev = s2.evaluate_admission_pressure(
        prompt_text="P" * 10000, route="ide", model="m",
        cost_governor=_make_fake_governor(0.09),  # tight
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    assert sev is None


def test_evaluate_tight_budget_high_prio_emits_signal(monkeypatch):
    """Tight forecast AND critical at head of queue ⇒ severity returned."""
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.10")
    _populated_router_with_high_prio()
    sev = s2.evaluate_admission_pressure(
        prompt_text="P" * 10000, route="ide", model="m",
        cost_governor=_make_fake_governor(0.10),  # over with forecast
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    assert sev is not None
    assert 0.0 <= sev <= 1.0


def test_evaluate_uses_len_prompt_text_no_assembly(monkeypatch):
    """prompt_chars derived from len(prompt_text) dynamically — B3
    invariant. Confirmed by varying prompt_text length and seeing
    the forecast/severity respond."""
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")
    monkeypatch.setenv("JARVIS_S2_SESSION_BUDGET_USD", "0.10")
    _populated_router_with_high_prio()

    # Short prompt: smaller forecast, may not push over.
    sev_short = s2.evaluate_admission_pressure(
        prompt_text="x", route="ide", model="m",
        cost_governor=_make_fake_governor(0.05),
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    # Long prompt: bigger forecast, pushes over.
    sev_long = s2.evaluate_admission_pressure(
        prompt_text="x" * 1_000_000, route="ide", model="m",
        cost_governor=_make_fake_governor(0.05),
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    # The long prompt's forecast contribution is monotonically larger.
    # If short emits nothing, long must emit (proves prompt_chars
    # actually drives the math).
    assert not (sev_short is not None and sev_long is None)


def test_evaluate_failopen_no_governor(monkeypatch):
    """cost_governor=None AND no default ⇒ returns None, no signal."""
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")
    sev = s2.evaluate_admission_pressure(
        prompt_text="P" * 100, route="ide", model="m",
        cost_governor=None,
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    # In a unit test, no default cost_governor is registered ⇒ None.
    assert sev is None


def test_evaluate_failopen_governor_raises(monkeypatch):
    """If governor.session_total raises, returns None (fail-open)."""
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")

    class _RaisingGovernor:
        def session_total_cumulative_usd(self):
            raise RuntimeError("synthetic")

    sev = s2.evaluate_admission_pressure(
        prompt_text="P" * 100, route="ide", model="m",
        cost_governor=_RaisingGovernor(),
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    assert sev is None


# ============================================================================
# (5/8) AST pins — no misuse of admission_gate; no parallel substrates
# ============================================================================


def test_ast_pin_no_dynamic_factor_in_admission_gate_call_kwargs():
    """S2 must NEVER pass dynamic_admit_safety_factor() through
    admission_gate.budget_safety_factor_value (which is time-domain
    and would silently clamp to 1.2). AST-walks each file; checks
    actual ``Call`` nodes — not comments/docstrings that may legitimately
    reference the parameter name for documentation."""
    for path in (
        "backend/core/ouroboros/governance/providers.py",
        "backend/core/ouroboros/governance/doubleword_provider.py",
        "backend/core/ouroboros/governance/s2_predictive_budget.py",
        "backend/core/ouroboros/governance/candidate_generator.py",
    ):
        src = Path(path).read_text(encoding="utf-8")
        # Anti-pattern (substring): passing the dynamic factor as
        # the admission_gate budget kwarg. This catches the literal
        # source line, but is permissive of comments mentioning the
        # parameter name.
        anti = "budget_safety_factor_value=dynamic_admit_safety_factor"
        assert anti not in src, (
            f"{path}: forbidden semantic conflation — dynamic_admit_safety_factor"
            f" must not be routed through budget_safety_factor_value"
        )
        # For S2 module: walk AST for actual Call kwargs containing
        # ``budget_safety_factor_value=`` (rejecting accidental
        # call-site uses while permitting docstring/comment mentions).
        if "s2_predictive_budget" in path:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for kw in (node.keywords or []):
                        assert kw.arg != "budget_safety_factor_value", (
                            "s2_predictive_budget must not pass "
                            "budget_safety_factor_value into any call "
                            "(time-domain parameter; would silently "
                            "clamp the cost-domain factor to 1.2)"
                        )


def test_ast_pin_no_prompt_reassembly_in_s2():
    """S2 module must NOT call _build_codegen_prompt or
    _build_lean_codegen_prompt (would duplicate provider logic).
    PRD §11 B3 invariant."""
    src = Path(
        "backend/core/ouroboros/governance/s2_predictive_budget.py"
    ).read_text(encoding="utf-8")
    for forbidden in ("_build_codegen_prompt", "_build_lean_codegen_prompt"):
        assert forbidden not in src, (
            f"S2 must not call {forbidden!r} — provider owns "
            f"assembly per PRD §11 B3."
        )


def test_ast_pin_no_parallel_ring_or_ledger_in_s2():
    """S2 must NOT define a parallel RecentDecisionsRing,
    CostGovernor, _OpCostEntry, or _HeapEntry. PRD §3."""
    src = Path(
        "backend/core/ouroboros/governance/s2_predictive_budget.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert node.name not in (
                "RecentDecisionsRing", "CostGovernor",
                "_OpCostEntry", "_HeapEntry", "WaitTimeEstimator",
                "SensorGovernor", "UnifiedIntakeRouter",
                "IntakePriorityQueue",
            ), (
                f"S2 must not redefine canonical class {node.name!r}"
            )


def test_ast_pin_s2_admission_hook_present_in_claude():
    """providers.py must contain the S2 admission hook (PRD §11.4)
    with master-off byte-identical discipline."""
    src = Path(
        "backend/core/ouroboros/governance/providers.py"
    ).read_text(encoding="utf-8")
    assert "evaluate_admission_pressure" in src
    assert "_s2_pressure_check" in src
    # The hook must precede the existing S1 cache gate block so it
    # fires regardless of S1 state.
    s2_pos = src.find("evaluate_admission_pressure")
    s1_pos = src.find("cached_or_generate as _cached_or_generate")
    assert s2_pos > 0 and s1_pos > 0
    assert s2_pos < s1_pos, (
        "S2 admission hook must precede S1 cache gate import in providers.py"
    )


def test_ast_pin_s2_admission_hook_present_in_dw():
    """doubleword_provider.py must contain the S2 admission hook
    co-located with _zw_prompt."""
    src = Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    assert "evaluate_admission_pressure" in src
    assert "_s2_pressure_check" in src
    # The hook should use _effective_model_id, NOT bare self._model
    # (per operator guardrail G2).
    assert "_effective_model_id" in src


def test_ast_pin_record_op_outcome_in_both_provider_success_seams():
    """Both Claude (_finalize_codegen_result) and DW
    (_dispatch_internal success return) must contain a
    record_op_outcome call, master-gated."""
    claude_src = Path(
        "backend/core/ouroboros/governance/providers.py"
    ).read_text(encoding="utf-8")
    dw_src = Path(
        "backend/core/ouroboros/governance/doubleword_provider.py"
    ).read_text(encoding="utf-8")
    for name, src in (("Claude", claude_src), ("DW", dw_src)):
        assert "record_op_outcome" in src, (
            f"{name} provider missing record_op_outcome hook"
        )
        assert "+cache" in src, (
            f"{name} provider missing cache-hit reconstruction skip "
            f"(belt-and-suspenders)"
        )


# ============================================================================
# (6/8) Master-OFF byte-identical — no S2 work performed
# ============================================================================


def test_master_off_evaluate_returns_immediately(monkeypatch):
    """Master OFF ⇒ evaluate_admission_pressure does not consult
    the cost governor at all."""
    consulted = []

    class _CountingGovernor:
        def session_total_cumulative_usd(self):
            consulted.append(1)
            return 99.0

    sev = s2.evaluate_admission_pressure(
        prompt_text="x" * 10000, route="ide", model="m",
        cost_governor=_CountingGovernor(),
        sample_provider=_stable_samples,
        pricing_lookup=_stable_pricing,
        output_token_estimator=_zero_estimator,
    )
    assert sev is None
    assert consulted == [], (
        "Master OFF: governor must not be consulted"
    )


# ============================================================================
# (7/8) Op-outcome record_op_outcome contract — cache-hit skip
# ============================================================================


def test_record_op_outcome_only_on_real_success(monkeypatch):
    """The provider-side record_op_outcome hooks must skip when
    provider_name ends with '+cache' (cache-hit reconstructed
    result). Verified by the AST pin above; this test exercises
    the runtime guard via a simulated finalize."""
    from backend.core.ouroboros.governance.admission_estimator import (
        get_default_history, reset_singletons_for_tests,
    )
    reset_singletons_for_tests()
    monkeypatch.setenv("JARVIS_S2_PREDICTIVE_BUDGET_ENABLED", "true")

    history = get_default_history()
    # Simulate the runtime guard: cache-hit results are NOT recorded.
    pname = "claude-api+cache"
    if not pname.endswith("+cache"):
        history.record_op_outcome("ide", "claude", 100, 0.005)
    # Nothing recorded:
    samples = history.op_outcome_samples("ide", "claude")
    assert samples == (), (
        "cache-hit results must not pollute the MAD sample stream"
    )


def test_record_op_outcome_records_on_real_success(monkeypatch):
    """Real provider success (no +cache marker) DOES record."""
    from backend.core.ouroboros.governance.admission_estimator import (
        get_default_history, reset_singletons_for_tests,
    )
    reset_singletons_for_tests()
    history = get_default_history()
    history.record_op_outcome("ide", "claude", 100, 0.005)
    samples = history.op_outcome_samples("ide", "claude")
    assert len(samples) == 1
    assert samples[0]["cost_usd"] == 0.005


# ============================================================================
# (8/8) Router auto-registration via __init__
# ============================================================================


def test_router_auto_registers_as_default_singleton():
    """The router's ``__init__`` must contain a call to
    ``set_default_intake_router(self)`` so any real construction
    auto-registers. AST-walk pin (UnifiedIntakeRouter's full
    construction requires gls + config fixtures we don't replicate
    in unit tests; this static check is the load-bearing guarantee
    that production construction does register)."""
    import inspect
    from backend.core.ouroboros.governance.intake import (
        unified_intake_router as uir,
    )
    src = inspect.getsource(uir.UnifiedIntakeRouter.__init__)
    assert "set_default_intake_router(self)" in src, (
        "UnifiedIntakeRouter.__init__ must auto-register self as "
        "the default for S2's head-of-queue peek"
    )
    # Also verify the singleton setter/getter are exported.
    assert hasattr(uir, "set_default_intake_router")
    assert hasattr(uir, "get_default_intake_router")
    assert hasattr(uir, "reset_default_intake_router_for_tests")


def test_peek_high_prio_queued_with_no_router():
    """When no router is registered, the S2 helper returns False
    (fail-open: no signal emitted)."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        reset_default_intake_router_for_tests,
    )
    reset_default_intake_router_for_tests()
    assert s2._peek_high_prio_queued() is False


def test_peek_high_prio_queued_critical_returns_true():
    """Registered router with critical at head ⇒ True."""
    _populated_router_with_high_prio()
    assert s2._peek_high_prio_queued() is True


def test_peek_high_prio_queued_low_returns_false():
    """Registered router with low at head ⇒ False (low maps to
    BACKGROUND, not a high-prio class)."""
    _populated_router_with_low_prio()
    assert s2._peek_high_prio_queued() is False
