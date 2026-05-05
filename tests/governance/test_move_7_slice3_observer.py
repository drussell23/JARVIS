"""Move 7 — Cross-op Semantic Budget Slice 3 observer + SSE +
HTTP regression spine (PRD §29.4, 2026-05-05).

Verifies:

  * Async observer one-cycle semantics (read ledger → compute
    budget → emit SSE on transition only)
  * Chatter suppression — same-verdict ticks silent; verdict-
    transition ticks fire SSE
  * Posture-aware cadence resolution (HARDEN tightens; MAINTAIN
    loosens; defaults steady)
  * Cooperative shutdown via stop() event
  * Master-flag-off short-circuits the loop
  * SSE event vocabulary present + publisher behavior
  * HTTP route registered via §33.3 naming-cage register_routes
  * HTTP handler returns 503 when master-off, 200 when on
  * 3 observer AST pins + observability auto-mount AST pin all
    green
  * Authority asymmetry — pure substrate
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Cadence resolution
# ---------------------------------------------------------------------------


def test_base_cadence_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S",
        raising=False,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        base_cadence_s,
    )
    assert base_cadence_s() == 6.0 * 3600.0


def test_base_cadence_clamped(monkeypatch):
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        base_cadence_s,
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S", "0",
    )
    assert base_cadence_s() == 60.0
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S",
        str(8.0 * 24.0 * 3600.0),  # 8 days
    )
    assert base_cadence_s() == 7.0 * 24.0 * 3600.0
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S", "junk",
    )
    assert base_cadence_s() == 6.0 * 3600.0


def test_posture_multiplier_harden_tightens():
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        _posture_multiplier,
    )
    assert _posture_multiplier("HARDEN") < 1.0


def test_posture_multiplier_maintain_loosens():
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        _posture_multiplier,
    )
    assert _posture_multiplier("MAINTAIN") > 1.0


def test_posture_multiplier_default_steady():
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        _posture_multiplier,
    )
    assert _posture_multiplier("EXPLORE") == 1.0
    assert _posture_multiplier("CONSOLIDATE") == 1.0
    assert _posture_multiplier("") == 1.0


def test_resolve_cadence_min_floor():
    """Even at HARDEN with tiny base, cadence floor is 60s."""
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )
    obs = CrossOpSemanticBudgetObserver()
    cadence = obs._resolve_cadence_s(posture="HARDEN")
    assert cadence >= 60.0


# ---------------------------------------------------------------------------
# Observer one-cycle — happy path
# ---------------------------------------------------------------------------


def _seed_ledger(
    target: Path, *, op_id: str, centroid, ts: float,
    monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
        record_op_centroid,
    )
    return record_op_centroid(
        op_id, centroid=centroid, ts_unix=ts, path=target,
    )


def test_observer_run_one_cycle_within_budget(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    _seed_ledger(
        target, op_id="op1", centroid=(1.0, 0.0), ts=1.0,
        monkeypatch=monkeypatch,
    )
    _seed_ledger(
        target, op_id="op2", centroid=(0.99, 0.14), ts=2.0,
        monkeypatch=monkeypatch,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )
    obs = CrossOpSemanticBudgetObserver(ledger_path=target)
    result = asyncio.run(obs.run_one_cycle())
    assert result is not None
    assert result.verdict_value == "within_budget"
    assert result.centroids_seen == 2
    # First-tick emission unconditional (prior is None).
    assert result.sse_emitted is True


def test_observer_chatter_suppression_same_verdict(
    monkeypatch, tmp_path,
):
    """Two consecutive ticks at same verdict → second emits no SSE."""
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    _seed_ledger(
        target, op_id="op1", centroid=(1.0, 0.0), ts=1.0,
        monkeypatch=monkeypatch,
    )
    _seed_ledger(
        target, op_id="op2", centroid=(0.99, 0.14), ts=2.0,
        monkeypatch=monkeypatch,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )
    obs = CrossOpSemanticBudgetObserver(ledger_path=target)
    r1 = asyncio.run(obs.run_one_cycle())
    r2 = asyncio.run(obs.run_one_cycle())
    assert r1.verdict_value == r2.verdict_value
    assert r1.sse_emitted is True
    assert r2.sse_emitted is False  # chatter suppressed


def test_observer_emits_on_verdict_transition(
    monkeypatch, tmp_path,
):
    """Verdict transition WITHIN_BUDGET → EXCEEDED fires SSE."""
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    _seed_ledger(
        target, op_id="op1", centroid=(1.0, 0.0), ts=1.0,
        monkeypatch=monkeypatch,
    )
    _seed_ledger(
        target, op_id="op2", centroid=(0.99, 0.14), ts=2.0,
        monkeypatch=monkeypatch,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )
    obs = CrossOpSemanticBudgetObserver(ledger_path=target)
    r1 = asyncio.run(obs.run_one_cycle())
    assert r1.verdict_value == "within_budget"
    assert r1.sse_emitted is True
    # Now append an orthogonal centroid → next tick exceeds.
    _seed_ledger(
        target, op_id="op3", centroid=(0.0, 1.0), ts=3.0,
        monkeypatch=monkeypatch,
    )
    r2 = asyncio.run(obs.run_one_cycle())
    assert r2.verdict_value == "exceeded"
    assert r2.sse_emitted is True  # transition → emit


def test_observer_insufficient_data(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "centroids.jsonl"
    _seed_ledger(
        target, op_id="op1", centroid=(1.0, 0.0), ts=1.0,
        monkeypatch=monkeypatch,
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )
    obs = CrossOpSemanticBudgetObserver(ledger_path=target)
    result = asyncio.run(obs.run_one_cycle())
    assert result.verdict_value == "insufficient_data"


def test_observer_missing_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    target = tmp_path / "absent.jsonl"
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )
    obs = CrossOpSemanticBudgetObserver(ledger_path=target)
    result = asyncio.run(obs.run_one_cycle())
    assert result.verdict_value == "insufficient_data"


# ---------------------------------------------------------------------------
# Cooperative shutdown
# ---------------------------------------------------------------------------


def test_observer_stop_breaks_loop(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_OBSERVER_CADENCE_S", "60",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observer import (  # noqa: E501
        CrossOpSemanticBudgetObserver,
    )

    async def _run():
        obs = CrossOpSemanticBudgetObserver(
            ledger_path=tmp_path / "absent.jsonl",
        )
        task = asyncio.create_task(obs.run_periodic())
        await asyncio.sleep(0.05)
        await obs.stop()
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            pytest.fail("observer did not stop within 2s")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# SSE event vocabulary + publisher
# ---------------------------------------------------------------------------


def test_sse_event_constant_present():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_SEMANTIC_BUDGET_CHANGED,
    )
    assert (
        EVENT_TYPE_SEMANTIC_BUDGET_CHANGED
        == "semantic_budget_changed"
    )


def test_sse_publisher_master_off_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_semantic_budget_event,
    )
    r = publish_semantic_budget_event(
        verdict="within_budget",
        prev_verdict="",
        integrated_drift=0.1,
        threshold=0.30,
        approaching_band=0.24,
        centroids_seen=2,
        ts_unix=1.0,
    )
    assert r is None


def test_sse_publisher_master_on_does_not_raise(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_semantic_budget_event,
    )
    # No subscribers attached — returns None or event_id; MUST
    # NOT raise.
    publish_semantic_budget_event(
        verdict="exceeded",
        prev_verdict="within_budget",
        integrated_drift=0.5,
        threshold=0.30,
        approaching_band=0.24,
        centroids_seen=3,
        ts_unix=1.0,
    )


# ---------------------------------------------------------------------------
# HTTP observability route — §33.3 naming-cage compliance
# ---------------------------------------------------------------------------


def test_observability_register_routes_smoke():
    pytest.importorskip("aiohttp")
    from aiohttp import web
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observability import (  # noqa: E501
        register_routes,
    )
    app = web.Application()
    register_routes(app)
    paths = [r.resource.canonical for r in app.router.routes()]  # type: ignore[attr-defined]
    assert "/observability/semantic-budget" in paths


def test_observability_handler_master_off_503(monkeypatch):
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observability import (  # noqa: E501
        _SemanticBudgetRoutesHandler,
    )
    handler = _SemanticBudgetRoutesHandler()

    class FakeRequest:
        query = {}
        match_info = {}

    response = asyncio.run(handler.handle_overview(FakeRequest()))
    assert response.status == 503


def test_observability_handler_master_on_200(monkeypatch, tmp_path):
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_BUDGET_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CROSS_OP_SEMANTIC_CENTROIDS_PATH",
        str(tmp_path / "centroids.jsonl"),
    )
    from backend.core.ouroboros.governance.cross_op_semantic_budget_observability import (  # noqa: E501
        _SemanticBudgetRoutesHandler,
    )
    handler = _SemanticBudgetRoutesHandler()

    class FakeRequest:
        query = {}
        match_info = {}

    response = asyncio.run(handler.handle_overview(FakeRequest()))
    assert response.status == 200
    body = json.loads(response.body)
    assert body["sse_event_type"] == "semantic_budget_changed"
    # Cold-start (empty ledger) → INSUFFICIENT_DATA verdict
    assert body["verdict"] in (
        "insufficient_data", "within_budget",
    )


def test_observability_naming_cage_compliance():
    """File name ends `_observability.py` AND exposes module-
    level `register_routes` per §33.3 contract — required for
    §32.11 Slice 3 auto-mount."""
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget_observability as obs,
    )
    assert hasattr(obs, "register_routes")
    import inspect
    sig = inspect.signature(obs.register_routes)
    params = sig.parameters
    assert "app" in params
    # `rate_limit_check` + `cors_headers` MUST be keyword
    # parameters (the §33.3 contract).
    assert (
        params["rate_limit_check"].kind
        in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


_EXPECTED_OBSERVER_PIN_NAMES = {
    "cross_op_semantic_budget_observer_authority_asymmetry",
    "cross_op_semantic_budget_observer_composes_substrate",
    "cross_op_semantic_budget_observer_chatter_suppression",
}


def test_observer_pins_auto_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    missing = _EXPECTED_OBSERVER_PIN_NAMES - registered
    assert not missing, (
        f"missing Slice 3 observer pins: {missing}"
    )


def test_observer_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    relevant = [
        v for v in violations
        if v.invariant_name in _EXPECTED_OBSERVER_PIN_NAMES
    ]
    assert not relevant, (
        "Slice 3 observer pin violations: " + "; ".join(
            f"{v.invariant_name}: {v.detail}"
            for v in relevant
        )
    )


def test_observability_naming_cage_pin_passes():
    """The §32.11 Slice 3 naming-cage pin
    `observability_module_exposes_register_routes_*` enforces
    that every `*_observability.py` exposes module-level
    register_routes. The Move 7 surface MUST satisfy this."""
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget_observability as obs,
    )
    import inspect
    members = dict(inspect.getmembers(obs))
    assert "register_routes" in members
    assert callable(members["register_routes"])


# ---------------------------------------------------------------------------
# Authority asymmetry — file-level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", [
    "backend/core/ouroboros/governance/"
    "cross_op_semantic_budget_observer.py",
    "backend/core/ouroboros/governance/"
    "cross_op_semantic_budget_observability.py",
])
def test_authority_asymmetry(rel_path):
    import ast as _ast
    target = (
        Path(__file__).resolve().parents[2] / rel_path
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator", "iron_gate", "policy", "providers",
        "candidate_generator", "urgency_router",
        "change_engine", "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"{rel_path} MUST NOT import {module!r}"
                    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_observer_public_api():
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget_observer as o,
    )
    expected = (
        "CrossOpSemanticBudgetObserver",
        "ObserverTickResult",
        "base_cadence_s",
        "register_shipped_invariants",
        "CROSS_OP_SEMANTIC_BUDGET_OBSERVER_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(o, name), f"missing public symbol: {name}"


def test_observability_public_api():
    from backend.core.ouroboros.governance import (
        cross_op_semantic_budget_observability as o,
    )
    expected = (
        "register_routes",
        "CROSS_OP_SEMANTIC_BUDGET_OBSERVABILITY_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(o, name), f"missing public symbol: {name}"


# ---------------------------------------------------------------------------
# Slice 5b consolidation — auto-mount via §32.11 Slice 3 registry
# ---------------------------------------------------------------------------


def test_observability_auto_mounts_via_slice_3_registry(
    monkeypatch,
):
    """Verify §32.11 Slice 3 observability_route_registry picks
    up `cross_op_semantic_budget_observability.py` by naming
    convention — first-class proof the §33.3 naming-cage
    inheritance works zero-edit."""
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_AUTODISCOVERY_ENABLED", "true",
    )
    from aiohttp import web
    from backend.core.ouroboros.governance.observability_route_registry import (  # noqa: E501
        discover_and_mount_observability_routes,
        reset_registry_for_tests,
    )
    reset_registry_for_tests()
    try:
        app = web.Application()
        report = discover_and_mount_observability_routes(app)
        mounted_names = {
            r.module_full_name for r in report.mounted
        }
        assert (
            "backend.core.ouroboros.governance."
            "cross_op_semantic_budget_observability"
            in mounted_names
        ), (
            "Slice 3 observability MUST auto-mount via §32.11 "
            "Slice 3 registry — naming-cage zero-edit "
            "inheritance"
        )
    finally:
        reset_registry_for_tests()
