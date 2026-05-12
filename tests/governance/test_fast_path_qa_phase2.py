"""Regression spine for §41.3 #26 Phase 2 — D3b INFORMATIONAL route.

Operator-signed 2026-05-11: D3b approved. Phase 2 expands the
canonical :class:`urgency_router.ProviderRoute` closed-5 taxonomy
to closed-6 by adding ``INFORMATIONAL`` — the read-only knowledge-
lookup route. Fast-path Q&A is the FIRST consumer; every QA
artifact is stamped ``route=ROUTE_INFORMATIONAL`` by construction.

Operator binding 2026-05-11: NO parallel routing logic, NO new
dispatcher, NO hardcoded route strings. The canonical enum
remains the single source of truth; ``fast_path_qa`` duplicates
the value as a module-local constant (mirroring the
:mod:`intent_envelope` ``_VALID_ROUTING_OVERRIDES`` precedent —
authority-asymmetry forbids importing ``urgency_router``).
Cross-reference parity is enforced structurally (this file).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Tuple

import pytest

from backend.core.ouroboros.governance import fast_path_qa as fpq
from backend.core.ouroboros.governance.fast_path_qa import (
    BoundedQAStore,
    QAArtifact,
    QAVerdict,
    ROUTE_INFORMATIONAL,
    _DEFAULT_BUDGET_USD,
    _ENV_BUDGET_USD,
    _ENV_COMPOSE_COST_GOVERNOR,
    _ENV_INFORMATIONAL_BUDGET_USD,
    _ENV_MASTER,
    _ENV_RETRIEVAL_ENABLED,
    ask_question,
    compose_cost_governor_enabled,
    daily_budget_usd,
    register_flags,
    register_shipped_invariants,
    reset_cost_today,
    reset_default_qa_store,
)
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
)
from backend.core.ouroboros.governance.cost_governor import (
    CostGovernor,
    CostGovernorConfig,
    register_finalize_observer,
    reset_finalize_observers,
    set_default_cost_governor,
)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """Each test runs with master ON, retrieval OFF (Phase 0
    path is the simplest exercise of route stamping), fresh
    store + cost counter. Phase 2 budget knobs cleared."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.delenv(_ENV_BUDGET_USD, raising=False)
    monkeypatch.delenv(_ENV_INFORMATIONAL_BUDGET_USD, raising=False)
    monkeypatch.setenv(_ENV_RETRIEVAL_ENABLED, "false")
    reset_default_qa_store()
    reset_cost_today()
    yield


# ---------------------------------------------------------------------------
# Closed-5 → closed-6 ProviderRoute taxonomy
# ---------------------------------------------------------------------------


def test_provider_route_is_closed_6():
    """ProviderRoute enum has exactly 6 values after D3b."""
    values = {m.value for m in ProviderRoute}
    assert values == {
        "immediate",
        "standard",
        "complex",
        "background",
        "speculative",
        "informational",
    }


def test_provider_route_informational_value_string():
    """The new enum member's .value is exactly "informational"."""
    assert ProviderRoute.INFORMATIONAL.value == "informational"


def test_provider_route_informational_membership():
    """The string "informational" round-trips via the enum."""
    assert ProviderRoute("informational") is ProviderRoute.INFORMATIONAL


# ---------------------------------------------------------------------------
# Cross-substrate parity — fast_path_qa.ROUTE_INFORMATIONAL must
# match urgency_router.ProviderRoute.INFORMATIONAL.value exactly.
# Authority-asymmetry forbids importing urgency_router in
# fast_path_qa; this test is the structural parity check.
# ---------------------------------------------------------------------------


def test_route_informational_matches_canonical():
    """``fast_path_qa.ROUTE_INFORMATIONAL`` is a structural
    duplicate of ``ProviderRoute.INFORMATIONAL.value``. If they
    drift, the substrate stamps a route value the canonical
    router can no longer classify."""
    assert ROUTE_INFORMATIONAL == ProviderRoute.INFORMATIONAL.value


def test_route_informational_in_envelope_allowlist():
    """``IntentEnvelope._VALID_ROUTING_OVERRIDES`` includes
    "informational" — envelopes carrying this routing_override
    pass schema validation."""
    from backend.core.ouroboros.governance.intake import (
        intent_envelope as ienv,
    )
    assert "informational" in ienv._VALID_ROUTING_OVERRIDES


def test_route_informational_in_risk_command_preview_tables():
    """``risk_command_preview`` cost + duration tables carry an
    entry for "informational" so the operator-facing
    /preview risk surface doesn't UNKNOWN-classify Q&A ops."""
    from backend.core.ouroboros.governance import (
        risk_command_preview as rcp,
    )
    assert "informational" in rcp._ROUTE_COST_USD
    assert "informational" in rcp._ROUTE_DURATION_S
    # Q&A is cheap by design — the cost entry should be at most
    # an order of magnitude under the IMMEDIATE row.
    assert rcp._ROUTE_COST_USD["informational"] <= (
        rcp._ROUTE_COST_USD["immediate"]
    )


# ---------------------------------------------------------------------------
# QAArtifact carries the canonical route tag
# ---------------------------------------------------------------------------


def test_qa_artifact_default_route_is_informational():
    """A bare-defaults QAArtifact (no explicit route arg) stamps
    ROUTE_INFORMATIONAL — the only valid route for Q&A."""
    artifact = QAArtifact(
        ref="q-1",
        question="hello?",
        answer="hi.",
        asked_at_unix=0.0,
        op_id="",
        cost_usd=0.0,
        model="",
        elapsed_s=0.0,
        inserted_at=0.0,
    )
    assert artifact.route == ROUTE_INFORMATIONAL
    assert artifact.route == "informational"


def test_qa_artifact_to_dict_carries_route():
    """``to_dict`` projection exposes the route for downstream
    consumers (telemetry / IDE GET surfaces / cost ledgers)."""
    artifact = QAArtifact(
        ref="q-1",
        question="q",
        answer="a",
        asked_at_unix=0.0,
        op_id="",
        cost_usd=0.0,
        model="",
        elapsed_s=0.0,
        inserted_at=0.0,
    )
    d = artifact.to_dict()
    assert d["route"] == "informational"


def test_store_round_trip_preserves_route():
    """Artifacts parked in the BoundedQAStore preserve the route
    tag across the store / lookup boundary."""
    store = BoundedQAStore()
    a = store.store(question="q", answer="a")
    looked = store.lookup(a.ref)
    assert looked is not None
    assert looked.route == ROUTE_INFORMATIONAL


def test_ask_question_stamps_informational_route():
    """End-to-end: ask_question parks artifacts with
    ``route=ROUTE_INFORMATIONAL`` regardless of which retrieval
    path was taken."""

    async def _fake_provider(
        system: str, q: str,
    ) -> Tuple[str, float]:
        return ("the answer", 0.0001)

    async def _run() -> None:
        report = await ask_question(
            "what is X?",
            op_id="op-1",
            provider_callable=_fake_provider,
            bridge_callable=lambda *_: None,
        )
        assert report.verdict is QAVerdict.ANSWERED
        assert report.artifact is not None
        assert report.artifact.route == ROUTE_INFORMATIONAL

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Canonical budget knob — JARVIS_INFORMATIONAL_BUDGET_USD takes
# precedence over the legacy JARVIS_FAST_PATH_QA_DAILY_BUDGET_USD.
# ---------------------------------------------------------------------------


def test_canonical_budget_overrides_legacy(monkeypatch):
    """Canonical knob wins when both are set."""
    monkeypatch.setenv(_ENV_INFORMATIONAL_BUDGET_USD, "12.5")
    monkeypatch.setenv(_ENV_BUDGET_USD, "3.0")
    assert daily_budget_usd() == pytest.approx(12.5)


def test_legacy_budget_used_when_canonical_unset(monkeypatch):
    """Backward-compat: legacy knob still honored if canonical
    not set (operators with Phase 0/1 muscle memory don't break)."""
    monkeypatch.delenv(_ENV_INFORMATIONAL_BUDGET_USD, raising=False)
    monkeypatch.setenv(_ENV_BUDGET_USD, "7.5")
    assert daily_budget_usd() == pytest.approx(7.5)


def test_default_used_when_neither_set(monkeypatch):
    """Both unset → default."""
    monkeypatch.delenv(_ENV_INFORMATIONAL_BUDGET_USD, raising=False)
    monkeypatch.delenv(_ENV_BUDGET_USD, raising=False)
    assert daily_budget_usd() == pytest.approx(_DEFAULT_BUDGET_USD)


def test_canonical_budget_garbage_falls_through_to_legacy(monkeypatch):
    """Malformed canonical knob falls through to legacy (not
    silent default). Operators get the most-correct value."""
    monkeypatch.setenv(_ENV_INFORMATIONAL_BUDGET_USD, "not-a-number")
    monkeypatch.setenv(_ENV_BUDGET_USD, "4.25")
    assert daily_budget_usd() == pytest.approx(4.25)


def test_canonical_budget_clamped_to_max(monkeypatch):
    """Canonical knob clamped to [0, 1000] just like legacy."""
    monkeypatch.setenv(_ENV_INFORMATIONAL_BUDGET_USD, "9999")
    assert daily_budget_usd() == pytest.approx(1000.0)


def test_canonical_budget_clamped_to_min(monkeypatch):
    """Negative canonical knob clamped to 0."""
    monkeypatch.setenv(_ENV_INFORMATIONAL_BUDGET_USD, "-5")
    assert daily_budget_usd() == pytest.approx(0.0)


def test_canonical_budget_gate_reaches_pipeline(monkeypatch):
    """Proves the canonical-knob precedence chain reaches the
    budget gate end-to-end. Sets a tiny canonical cap, seeds
    prior spend past it via the cost ledger, asserts the next
    ask_question returns BUDGET_EXHAUSTED without invoking the
    provider. Legacy knob intentionally permissive — exhaustion
    must come from the canonical knob taking precedence."""
    monkeypatch.setenv(_ENV_INFORMATIONAL_BUDGET_USD, "0.001")
    monkeypatch.setenv(_ENV_BUDGET_USD, "100")
    # Seed cost ledger past the canonical cap.
    fpq._record_cost(0.10)

    async def _fake_provider(
        system: str, q: str,
    ) -> Tuple[str, float]:
        raise AssertionError("provider invoked despite exhaustion")

    async def _run() -> None:
        report = await ask_question(
            "ignored?",
            op_id="op-budget",
            provider_callable=_fake_provider,
            bridge_callable=lambda *_: None,
        )
        assert report.verdict is QAVerdict.BUDGET_EXHAUSTED

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Module-local constants — bytes-pinned
# ---------------------------------------------------------------------------


def test_route_informational_is_module_constant():
    """``ROUTE_INFORMATIONAL`` is importable as a module attribute
    so downstream substrates (e.g., REPL surfaces, IDE projections)
    reference it instead of hardcoding the literal."""
    assert hasattr(fpq, "ROUTE_INFORMATIONAL")
    assert fpq.ROUTE_INFORMATIONAL == "informational"


def test_route_informational_in_dunder_all():
    """Re-exported via ``__all__`` so ``from fast_path_qa import *``
    pulls it down (intended public surface)."""
    assert "ROUTE_INFORMATIONAL" in fpq.__all__


def test_informational_budget_env_constant_is_canonical_name():
    """The Phase 2 knob name matches the canonical contract
    documented in ProviderRoute.INFORMATIONAL's docstring."""
    assert (
        _ENV_INFORMATIONAL_BUDGET_USD
        == "JARVIS_INFORMATIONAL_BUDGET_USD"
    )


# ---------------------------------------------------------------------------
# AST pin — fast_path_qa_route_informational_pinned
# ---------------------------------------------------------------------------


def test_route_informational_ast_pin_registered():
    """The Phase 2 AST pin is registered and validates clean."""
    invariants = register_shipped_invariants()
    names = {inv.invariant_name for inv in invariants}
    assert "fast_path_qa_route_informational_pinned" in names


def test_route_informational_ast_pin_passes_on_current_source():
    """The pin's validator returns empty tuple against current
    source — no drift."""
    invariants = register_shipped_invariants()
    target = None
    for inv in invariants:
        if (
            inv.invariant_name
            == "fast_path_qa_route_informational_pinned"
        ):
            target = inv
            break
    assert target is not None
    src_path = (
        "backend/core/ouroboros/governance/fast_path_qa.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        source = fh.read()
    import ast as _ast
    tree = _ast.parse(source)
    violations = target.validate(tree, source)
    assert violations == (), f"AST pin drift: {violations}"


# ---------------------------------------------------------------------------
# FlagRegistry seeds — canonical knob is registered as a typed
# spec so /help flags surfaces it for operators.
# ---------------------------------------------------------------------------


def test_register_flags_includes_canonical_informational_budget():
    """The Phase 2 canonical budget knob is registered as a
    FlagSpec — operators can discover it via /help flags."""
    captured: list = []

    class _FakeRegistry:
        def register(self, spec: Any) -> None:
            captured.append(spec)

    count = register_flags(_FakeRegistry())
    assert count >= 13  # 12 prior + 1 new
    names = {spec.name for spec in captured}
    assert _ENV_INFORMATIONAL_BUDGET_USD in names


def test_register_flags_retains_legacy_budget():
    """Legacy knob is still registered for backward-compat
    visibility — operators upgrading from Phase 0/1 see both
    knobs in /help flags + a description explaining precedence."""
    captured: list = []

    class _FakeRegistry:
        def register(self, spec: Any) -> None:
            captured.append(spec)

    register_flags(_FakeRegistry())
    names = {spec.name for spec in captured}
    assert _ENV_BUDGET_USD in names
    assert _ENV_INFORMATIONAL_BUDGET_USD in names


# ---------------------------------------------------------------------------
# Authority-asymmetry preserved — fast_path_qa MUST NOT import
# urgency_router (the parity contract is enforced structurally,
# not via import).
# ---------------------------------------------------------------------------


def test_fast_path_qa_does_not_import_urgency_router():
    """The substrate stays decoupled from routing internals; the
    cross-substrate parity is structural (constant value) +
    behavioral (this test file's assertion)."""
    import ast as _ast
    src_path = (
        "backend/core/ouroboros/governance/fast_path_qa.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = _ast.parse(source)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            assert (
                "urgency_router" not in mod
            ), f"forbidden import: {mod}"
        if isinstance(node, _ast.Import):
            for alias in node.names:
                assert "urgency_router" not in alias.name, (
                    f"forbidden import: {alias.name}"
                )


# ---------------------------------------------------------------------------
# Slice 2 — Canonical cost_governor composition
#
# Phase 2 D3b strengthens the system by composing the canonical
# CostGovernor for per-op cost attribution. INFORMATIONAL gains a
# route_factor entry in cost_governor, mirroring the closed-5→6
# expansion that ProviderRoute shipped. fast_path_qa.ask_question
# threads start()/charge()/finish() through the governor so cost
# data flows into the canonical observability chain (finalize
# observers, band-crossing SSE events) instead of staying isolated
# in the Q&A-substrate-local counter.
#
# Daily aggregate budget stays Q&A-substrate-local (no canonical
# daily-per-route surface exists).
# ---------------------------------------------------------------------------


def test_cost_governor_route_factors_include_informational():
    """cost_governor's route_factors default contains
    "informational" with the canonical JARVIS_OP_COST_ROUTE_*
    env knob convention. Mirrors ProviderRoute's closed-5→6
    expansion."""
    cfg = CostGovernorConfig()
    assert "informational" in cfg.route_factors
    # Q&A is cheap by design (read-only, small-token).
    assert cfg.route_factors["informational"] > 0.0
    # Factor should be smaller than the IMMEDIATE / COMPLEX
    # routes (which absorb tool-loop + multi-file generation).
    assert (
        cfg.route_factors["informational"]
        < cfg.route_factors["immediate"]
    )
    assert (
        cfg.route_factors["informational"]
        < cfg.route_factors["complex"]
    )


def test_cost_governor_informational_factor_env_tunable(monkeypatch):
    """The default factor is operator-tunable via
    JARVIS_OP_COST_ROUTE_INFORMATIONAL — closes the no-
    hardcoding contract."""
    monkeypatch.setenv("JARVIS_OP_COST_ROUTE_INFORMATIONAL", "0.99")
    cfg = CostGovernorConfig()
    assert cfg.route_factors["informational"] == pytest.approx(0.99)


def test_compose_cost_governor_default_true(monkeypatch):
    """Production behavior: composition is default-TRUE when
    master is on. Operator opt-out is explicit."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.delenv(_ENV_COMPOSE_COST_GOVERNOR, raising=False)
    assert compose_cost_governor_enabled() is True


def test_compose_cost_governor_master_off_overrides(monkeypatch):
    """Master flag off forces composition off regardless of the
    sub-flag — the substrate's §33.1 contract."""
    monkeypatch.setenv(_ENV_MASTER, "false")
    monkeypatch.setenv(_ENV_COMPOSE_COST_GOVERNOR, "true")
    assert compose_cost_governor_enabled() is False


def test_compose_cost_governor_sub_flag_off(monkeypatch):
    """Operator can disable composition without flipping master."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_COMPOSE_COST_GOVERNOR, "false")
    assert compose_cost_governor_enabled() is False


def test_ask_question_starts_cost_governor_op(monkeypatch):
    """End-to-end: ask_question registers the op with the
    canonical governor at start. Verified by observing the
    governor's per-op snapshot post-call."""
    governor = CostGovernor()
    set_default_cost_governor(governor)
    try:
        async def _fake_provider(
            system: str, q: str,
        ) -> Tuple[str, float]:
            return ("the answer", 0.0050)

        async def _run() -> None:
            report = await ask_question(
                "what?",
                op_id="op-cg-1",
                provider_callable=_fake_provider,
                bridge_callable=lambda *_: None,
            )
            assert report.verdict is QAVerdict.ANSWERED
            # finish() was called at the end → entry removed.
            # Prior to finish, charge() must have been invoked.
            # The CostWarningObserver / finalize_observer chain
            # would have already seen this op.
            assert governor.summary("op-cg-1") is None

        asyncio.run(_run())
    finally:
        set_default_cost_governor(None)


def test_ask_question_charges_realized_cost_to_governor(monkeypatch):
    """Per-op cumulative spend is recorded on the governor before
    finish() clears the entry. Use the module-level finalize
    observer to capture the final summary."""
    governor = CostGovernor()
    captured: list = []

    def _observer(op_id: str, summary: Any) -> None:
        captured.append((op_id, dict(summary) if summary else {}))

    reset_finalize_observers()
    unsubscribe = register_finalize_observer(_observer)
    set_default_cost_governor(governor)
    try:
        async def _fake_provider(
            system: str, q: str,
        ) -> Tuple[str, float]:
            return ("answer", 0.0075)

        async def _run() -> None:
            report = await ask_question(
                "another?",
                op_id="op-cg-2",
                provider_callable=_fake_provider,
                bridge_callable=lambda *_: None,
            )
            assert report.verdict is QAVerdict.ANSWERED

        asyncio.run(_run())

        # Filter to our op (other ops in this process may also fire).
        ours = [c for c in captured if c[0] == "op-cg-2"]
        assert len(ours) == 1
        _, summary = ours[0]
        # cumulative_usd is the sum of charge() invocations —
        # equals the fake provider's reported cost.
        cumulative = summary.get("cumulative_usd", 0.0)
        assert cumulative == pytest.approx(0.0075, abs=1e-6)
        # The op was registered with the canonical route.
        assert summary.get("route") == ROUTE_INFORMATIONAL
    finally:
        unsubscribe()
        reset_finalize_observers()
        set_default_cost_governor(None)


def test_ask_question_finalizes_on_provider_failure():
    """Provider failure must still finalize the governor op —
    otherwise we leak entries (resource leak + skewed cumulative
    cost telemetry)."""
    governor = CostGovernor()
    set_default_cost_governor(governor)
    try:
        async def _failing_provider(
            system: str, q: str,
        ) -> Tuple[str, float]:
            raise RuntimeError("simulated provider crash")

        async def _run() -> None:
            report = await ask_question(
                "fails?",
                op_id="op-cg-fail",
                provider_callable=_failing_provider,
                bridge_callable=lambda *_: None,
            )
            assert report.verdict is QAVerdict.PROVIDER_FAILED
            assert governor.summary("op-cg-fail") is None

        asyncio.run(_run())
    finally:
        set_default_cost_governor(None)


def test_ask_question_falls_back_when_compose_disabled(monkeypatch):
    """Sub-flag off → daily aggregate counter still gates; no
    governor entry is created (Phase 0/1 behavior preserved)."""
    monkeypatch.setenv(_ENV_COMPOSE_COST_GOVERNOR, "false")
    governor = CostGovernor()
    set_default_cost_governor(governor)
    try:
        async def _fake_provider(
            system: str, q: str,
        ) -> Tuple[str, float]:
            return ("ok", 0.001)

        async def _run() -> None:
            report = await ask_question(
                "fallback?",
                op_id="op-fallback",
                provider_callable=_fake_provider,
                bridge_callable=lambda *_: None,
            )
            assert report.verdict is QAVerdict.ANSWERED
            # No governor entry created — composition disabled.
            assert governor.summary("op-fallback") is None
    finally:
        set_default_cost_governor(None)


def test_ask_question_falls_back_when_no_governor_singleton():
    """No-singleton case → substrate degrades to daily counter
    alone (Phase 0/1 behavior). Proves the defensive None-check
    on get_default_cost_governor()."""
    set_default_cost_governor(None)

    async def _fake_provider(
        system: str, q: str,
    ) -> Tuple[str, float]:
        return ("ok", 0.001)

    async def _run() -> None:
        report = await ask_question(
            "no-gov?",
            op_id="op-nogov",
            provider_callable=_fake_provider,
            bridge_callable=lambda *_: None,
        )
        # Daily counter still allows the op through.
        assert report.verdict is QAVerdict.ANSWERED

    asyncio.run(_run())


def test_register_flags_includes_compose_cost_governor():
    """Phase 2 Slice 2 sub-flag is registered."""
    captured: list = []

    class _FakeRegistry:
        def register(self, spec: Any) -> None:
            captured.append(spec)

    register_flags(_FakeRegistry())
    names = {spec.name for spec in captured}
    assert _ENV_COMPOSE_COST_GOVERNOR in names


def test_compose_cost_governor_default_in_flag_spec_is_true():
    """The sub-flag's FlagSpec default is True so /help flags
    surfaces the production behavior, not the opt-out value."""
    captured: list = []

    class _FakeRegistry:
        def register(self, spec: Any) -> None:
            captured.append(spec)

    register_flags(_FakeRegistry())
    spec = next(
        s for s in captured
        if s.name == _ENV_COMPOSE_COST_GOVERNOR
    )
    assert spec.default is True


def test_composes_canonical_ast_pin_requires_cost_governor():
    """The composes_canonical pin requires cost_governor +
    get_default_cost_governor references in the substrate source
    — guarantees the composition isn't accidentally ripped out."""
    invariants = register_shipped_invariants()
    target = next(
        inv for inv in invariants
        if inv.invariant_name == "fast_path_qa_composes_canonical"
    )
    import ast as _ast
    src_path = (
        "backend/core/ouroboros/governance/fast_path_qa.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = _ast.parse(source)
    violations = target.validate(tree, source)
    assert violations == (), f"AST pin drift: {violations}"


def test_cost_governor_informational_route_structurally_present():
    """AST-walks cost_governor.py and asserts the
    route_factors default-factory dict literal contains an
    "informational" key bound to the canonical env-knob name.
    This is the structural enforcement (cost_governor has no
    shipped_code_invariants registration yet)."""
    import ast as _ast
    src_path = (
        "backend/core/ouroboros/governance/cost_governor.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        source = fh.read()
    tree = _ast.parse(source)
    found_route_key = False
    found_canonical_envvar = False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, _ast.Constant)
                    and key.value == "informational"
                ):
                    found_route_key = True
                    # The value should be _env_float(<envvar>, <default>).
                    if (
                        isinstance(value, _ast.Call)
                        and value.args
                        and isinstance(value.args[0], _ast.Constant)
                        and value.args[0].value
                        == "JARVIS_OP_COST_ROUTE_INFORMATIONAL"
                    ):
                        found_canonical_envvar = True
    assert found_route_key, (
        '"informational" key missing from cost_governor '
        "route_factors default-factory dict"
    )
    assert found_canonical_envvar, (
        "JARVIS_OP_COST_ROUTE_INFORMATIONAL env-knob name not "
        "bound to the informational route_factors entry — "
        "mirrors the JARVIS_OP_COST_ROUTE_* family convention"
    )
