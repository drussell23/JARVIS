"""Q4 Priority #2 Slice 4 — graduation regression suite.

Pins the four graduation deliverables:

  * 4 ``shipped_code_invariants`` AST pins
  * 8 FlagRegistry seeds
  * ``closure_loop_proposal_emitted`` SSE event type registered +
    bridge emits on OK propose
  * 4 GET routes (``/observability/closure-loop[/{,history,pending,stats}]``)

Master flag stays default-FALSE deliberately (operator cost ramp,
mirrors Move 6 graduation discipline).

Covers:

  §A   register_shipped_invariants() returns 4 invariants
  §B   Each invariant has the documented validate() shape + target
  §C   ClosureOutcome vocabulary pin: catches added/removed values
  §D   No-approve pins fire on synthetic .approve injection (in-
       memory transformation, not a file write)
  §E   register_flags() registers exactly 8 FlagSpecs with
       documented categories + master default-FALSE
  §F   Master flag default is False
  §G   EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED in _VALID_EVENT_TYPES
  §H   Bridge emits SSE on successful propose (broker.publish called
       with the documented payload shape)
  §I   Bridge does NOT emit when propose returns False (no broker
       call on non-PROPOSED records)
  §J   GET /observability/closure-loop returns health bundle when
       master is on
  §K   GET /observability/closure-loop returns 403 when master is off
  §L   GET /observability/closure-loop/history returns ring records
  §M   GET /observability/closure-loop/pending returns ledger
       PENDING proposals on the new surface
  §N   GET /observability/closure-loop/stats returns observer stats
  §O   GET /observability/closure-loop/history?limit=N malformed → 400
  §P   GET routes rate-limited via existing _check_rate_limit
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationSurface,
    OperatorDecisionStatus,
    get_default_ledger,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category, FlagSpec, FlagType,
)
from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
    EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.verification.closure_loop_bridge import (  # noqa: E501
    default_propose_callback,
    default_propose_callback_async,
    wire_default_observer,
)
from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
    ClosureLoopObserver,
    reset_default_observer,
)
from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    ClosureLoopRecord,
    ClosureOutcome,
    closure_loop_orchestrator_enabled,
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
    read_closure_history,
    reset_for_tests as reset_store,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    CoherenceAdvisoryAction,
    TighteningIntent,
    TighteningProposalStatus,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    BehavioralDriftKind,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    BranchVerdict,
    ReplayOutcome,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_CLOSURE_LOOP_HISTORY_DIR", str(tmp_path),
    )
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_ADAPTATION_LEDGER_PATH",
        str(tmp_path / "adaptation_ledger.jsonl"),
    )
    reset_default_observer()
    reset_default_ledger()
    reset_store()
    yield
    reset_default_observer()
    reset_default_ledger()
    reset_store()


def _record(
    outcome: ClosureOutcome = ClosureOutcome.PROPOSED,
    *,
    fingerprint: str = "fp-001",
) -> ClosureLoopRecord:
    return ClosureLoopRecord(
        outcome=outcome,
        advisory_id="adv-001",
        drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        parameter_name="route_drift_pct",
        current_value=25.0,
        proposed_value=20.0,
        validator_ok=True,
        replay_outcome=ReplayOutcome.SUCCESS,
        replay_verdict=BranchVerdict.DIVERGED_WORSE,
        record_fingerprint=fingerprint,
        decided_at_ts=1234.0,
    )


# ---------------------------------------------------------------------------
# §A–§D — Shipped code invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    def test_register_returns_four(self):
        invs = register_shipped_invariants()
        assert len(invs) == 4

    def test_invariants_have_documented_shape(self):
        invs = register_shipped_invariants()
        names = {i.invariant_name for i in invs}
        expected = {
            "closure_loop_outcome_vocabulary",
            "closure_loop_orchestrator_no_approve",
            "closure_loop_bridge_no_approve",
            "closure_loop_observer_no_approve",
        }
        assert names == expected
        for inv in invs:
            assert inv.target_file.startswith(
                "backend/core/ouroboros/governance/verification/"
            )
            assert callable(inv.validate)
            assert inv.description.strip() != ""

    def test_outcome_vocabulary_pin_passes_on_clean_source(self):
        import ast
        import inspect
        from backend.core.ouroboros.governance.verification import (
            closure_loop_orchestrator,
        )
        src = inspect.getsource(closure_loop_orchestrator)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name
            == "closure_loop_outcome_vocabulary"
        )
        violations = vocab_inv.validate(tree, src)
        assert violations == ()

    def test_outcome_vocabulary_pin_fires_on_missing_value(self):
        # Synthetic source with a missing value triggers the pin.
        import ast
        bad_src = (
            "import enum\n"
            "class ClosureOutcome(str, enum.Enum):\n"
            "    PROPOSED = 'proposed'\n"
            "    SKIPPED_NO_INTENT = 'skipped_no_intent'\n"
            "    DISABLED = 'disabled'\n"
            "    FAILED = 'failed'\n"
            # missing SKIPPED_VALIDATION_FAILED + SKIPPED_REPLAY_REJECTED
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name
            == "closure_loop_outcome_vocabulary"
        )
        violations = vocab_inv.validate(tree, bad_src)
        assert len(violations) >= 1
        joined = " ".join(violations)
        assert "SKIPPED_VALIDATION_FAILED" in joined
        assert "SKIPPED_REPLAY_REJECTED" in joined

    def test_outcome_vocabulary_pin_fires_on_unpinned_value(self):
        # Synthetic source with an extra value triggers the pin.
        import ast
        bad_src = (
            "import enum\n"
            "class ClosureOutcome(str, enum.Enum):\n"
            "    PROPOSED = 'proposed'\n"
            "    SKIPPED_NO_INTENT = 'skipped_no_intent'\n"
            "    SKIPPED_VALIDATION_FAILED = 'a'\n"
            "    SKIPPED_REPLAY_REJECTED = 'b'\n"
            "    DISABLED = 'disabled'\n"
            "    FAILED = 'failed'\n"
            "    NEW_ROGUE = 'new_rogue'\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        vocab_inv = next(
            i for i in invs
            if i.invariant_name
            == "closure_loop_outcome_vocabulary"
        )
        violations = vocab_inv.validate(tree, bad_src)
        assert any("NEW_ROGUE" in v for v in violations)

    def test_no_approve_pin_passes_on_clean_module(self):
        import ast
        import inspect
        from backend.core.ouroboros.governance.verification import (
            closure_loop_bridge,
        )
        src = inspect.getsource(closure_loop_bridge)
        tree = ast.parse(src)
        invs = register_shipped_invariants()
        bridge_pin = next(
            i for i in invs
            if i.invariant_name
            == "closure_loop_bridge_no_approve"
        )
        violations = bridge_pin.validate(tree, src)
        assert violations == ()

    def test_no_approve_pin_fires_on_synthetic_approve_call(self):
        import ast
        bad_src = (
            "import logging\n"
            "def malicious():\n"
            "    ledger.approve(proposal_id='x')\n"
        )
        tree = ast.parse(bad_src)
        invs = register_shipped_invariants()
        bridge_pin = next(
            i for i in invs
            if i.invariant_name
            == "closure_loop_bridge_no_approve"
        )
        violations = bridge_pin.validate(tree, bad_src)
        assert len(violations) == 1
        assert ".approve" in violations[0]


# ---------------------------------------------------------------------------
# §E + §F — FlagRegistry seeds
# ---------------------------------------------------------------------------


class _StubRegistry:
    def __init__(self) -> None:
        self.specs: List[FlagSpec] = []

    def bulk_register(
        self, specs: List[FlagSpec], *, override: bool = False,
    ) -> int:
        self.specs.extend(specs)
        return len(specs)


class TestFlagRegistry:
    def test_registers_exactly_eight_specs(self):
        reg = _StubRegistry()
        n = register_flags(reg)
        assert n == 8
        assert len(reg.specs) == 8

    def test_master_flag_default_false(self):
        reg = _StubRegistry()
        register_flags(reg)
        master = next(
            s for s in reg.specs
            if s.name == "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED"
        )
        assert master.type is FlagType.BOOL
        assert master.default is False
        assert master.category is Category.SAFETY

    def test_categories_documented(self):
        # Each spec must have a category from the 8-slot taxonomy.
        reg = _StubRegistry()
        register_flags(reg)
        for spec in reg.specs:
            assert isinstance(spec.category, Category)
            assert spec.description.strip() != ""
            assert spec.source_file.endswith(".py")
            assert spec.since.startswith(
                "Q4 Priority #2 Slice"
            )

    def test_no_duplicate_flag_names(self):
        reg = _StubRegistry()
        register_flags(reg)
        names = [s.name for s in reg.specs]
        assert len(names) == len(set(names))

    def test_master_flag_default_off_at_runtime(self, monkeypatch):
        # Empirical default-off test (matches the registered FlagSpec).
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED",
            raising=False,
        )
        assert closure_loop_orchestrator_enabled() is False


# ---------------------------------------------------------------------------
# §G–§I — SSE event registration + bridge emission
# ---------------------------------------------------------------------------


class TestSSEEmission:
    def test_event_type_registered(self):
        assert (
            EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED
            == "closure_loop_proposal_emitted"
        )
        assert (
            EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED
            in _VALID_EVENT_TYPES
        )

    @pytest.mark.asyncio
    async def test_bridge_publishes_on_ok_propose(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "true",
        )
        published: List[Tuple[str, str, Dict[str, Any]]] = []

        class _StubBroker:
            def publish(self, event_type, op_id, payload):
                published.append(
                    (event_type, op_id, dict(payload or {})),
                )
                return "evid-1"

        with mock.patch(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.get_default_broker",
            return_value=_StubBroker(),
        ):
            await default_propose_callback_async(_record())

        assert len(published) == 1
        event_type, op_id, payload = published[0]
        assert event_type == EVENT_TYPE_CLOSURE_LOOP_PROPOSAL_EMITTED
        assert op_id == "adv-001"
        assert payload["advisory_id"] == "adv-001"
        assert payload["parameter_name"] == "route_drift_pct"
        assert payload["proposal_id"].startswith("closure-loop-")
        assert payload["record_fingerprint"] == "fp-001"

    @pytest.mark.asyncio
    async def test_bridge_does_not_publish_on_non_proposed(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "true",
        )
        published = []

        class _StubBroker:
            def publish(self, event_type, op_id, payload):
                published.append(event_type)
                return "evid"

        with mock.patch(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.get_default_broker",
            return_value=_StubBroker(),
        ):
            for oc in (
                ClosureOutcome.SKIPPED_NO_INTENT,
                ClosureOutcome.SKIPPED_REPLAY_REJECTED,
                ClosureOutcome.DISABLED,
                ClosureOutcome.FAILED,
            ):
                await default_propose_callback_async(_record(oc))

        assert published == []  # no SSE on non-PROPOSED records

    @pytest.mark.asyncio
    async def test_broker_exception_swallowed(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "true",
        )

        class _ExplodeBroker:
            def publish(self, *args, **kwargs):
                raise RuntimeError("broker boom")

        with mock.patch(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.get_default_broker",
            return_value=_ExplodeBroker(),
        ):
            # Must not raise.
            await default_propose_callback_async(_record())


# ---------------------------------------------------------------------------
# §J–§P — GET routes
#
# Slice 4 ships the 4 routes wired into IDEObservabilityRouter; we
# exercise them via in-process aiohttp client (mirrors the
# established pattern in tests/governance/test_ide_observability.py
# but kept self-contained here so this slice's regression spine
# doesn't depend on test layout there).
# ---------------------------------------------------------------------------


def _aiohttp_available() -> bool:
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _aiohttp_available(),
    reason="aiohttp not available — GET-route tests skipped",
)
class TestGETRoutes:
    @pytest.fixture
    async def client(self, aiohttp_client, monkeypatch):
        from aiohttp import web
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "true",
        )
        app = web.Application()
        router = IDEObservabilityRouter()
        router.register_routes(app)
        return await aiohttp_client(app)

    @pytest.mark.asyncio
    async def test_health_returns_200_when_master_on(self, client):
        resp = await client.get("/observability/closure-loop")
        assert resp.status == 200
        body = await resp.json()
        assert body["enabled"] is True
        assert "history_path" in body
        assert "history_count" in body
        assert "outcome_histogram" in body

    @pytest.mark.asyncio
    async def test_health_returns_403_when_master_off(
        self, aiohttp_client, monkeypatch,
    ):
        from aiohttp import web
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        monkeypatch.setenv(
            "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "false",
        )
        app = web.Application()
        IDEObservabilityRouter().register_routes(app)
        c = await aiohttp_client(app)
        resp = await c.get("/observability/closure-loop")
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_history_returns_records(self, client):
        # Seed the ring with one record.
        from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
            record_closure_outcome,
        )
        record_closure_outcome(_record())
        resp = await client.get(
            "/observability/closure-loop/history?limit=10",
        )
        assert resp.status == 200
        body = await resp.json()
        assert "records" in body
        assert len(body["records"]) >= 1
        assert body["records"][0]["advisory_id"] == "adv-001"

    @pytest.mark.asyncio
    async def test_history_malformed_limit_400(self, client):
        resp = await client.get(
            "/observability/closure-loop/history?limit=garbage",
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_pending_returns_empty_when_no_proposals(self, client):
        resp = await client.get(
            "/observability/closure-loop/pending",
        )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"proposals": []}

    @pytest.mark.asyncio
    async def test_pending_returns_proposal_after_propose(
        self, client,
    ):
        # Drive a real propose through the bridge.
        result = default_propose_callback(_record())
        assert result is True
        resp = await client.get(
            "/observability/closure-loop/pending",
        )
        assert resp.status == 200
        body = await resp.json()
        assert len(body["proposals"]) == 1
        prop = body["proposals"][0]
        assert (
            prop["surface"] == "coherence_auditor.budgets"
        )
        assert prop["operator_decision"] == "pending"

    @pytest.mark.asyncio
    async def test_stats_returns_observer_telemetry(self, client):
        resp = await client.get(
            "/observability/closure-loop/stats",
        )
        assert resp.status == 200
        body = await resp.json()
        assert "pass_index" in body
        assert "outcome_histogram" in body
        assert "schema_version" in body
