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
    _ENV_INFORMATIONAL_BUDGET_USD,
    _ENV_MASTER,
    _ENV_RETRIEVAL_ENABLED,
    ask_question,
    daily_budget_usd,
    register_flags,
    register_shipped_invariants,
    reset_cost_today,
    reset_default_qa_store,
)
from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
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
