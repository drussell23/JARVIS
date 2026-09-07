"""Routing admission is ONE method shared by both GovernedLoopService entry
points.

``submit()`` bound an op to its brain and stamped ``TelemetryContext``
(host telemetry + ``RoutingIntentTelemetry`` incl. the served model's
``schema_capability``), the frozen autonomy tier and the OUROBOROS.md
instruction block inline. ``submit_background()`` — the entry the intake
router PREFERS, so every roadmap goal in a soak — handed the RAW context to
the background pool, whose worker calls ``orchestrator.run`` directly. Those
ops reached the prompt builder with ``ctx.telemetry is None``: no brain, no
served capability, the 2b.1-diff schema structurally unreachable
(``[Schema] op=… capability=? served=- brain=-``, soak 2026-09-07 23:34Z).

These tests bind the REAL ``submit_background`` and ``_admit_routing`` onto a
surface carrying only what admission reads, so the contract is exercised
without booting the service.
"""
from __future__ import annotations

import inspect
import time
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.brain_selector import BrainSelectionResult
from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
from backend.core.ouroboros.governance.op_context import OperationContext


@pytest.fixture(autouse=True)
def _hermetic_served_model(monkeypatch):
    """Admission asks the local lane which model it SERVES (memoised lookup
    against the node). Pin it: a unit test must never negotiate with a live
    ollama, and the served name is what the stamp is asserted to carry."""
    from backend.core.ouroboros.governance import candidate_generator

    async def _served(endpoint):
        return "served-model-under-test"

    monkeypatch.setattr(candidate_generator, "_resolve_served_model", _served)


class _Pool:
    def __init__(self, fault: Exception | None = None) -> None:
        self.submitted: list = []
        self._fault = fault

    async def submit(self, ctx) -> str:
        if self._fault is not None:
            raise self._fault
        self.submitted.append(ctx)
        return str(ctx.op_id)


class _Selector:
    """A brain selector whose served-capability verdict is fixed — the policy
    itself is covered by the served_model_capability suite."""

    def __init__(self, brain: BrainSelectionResult, capability: str) -> None:
        self._brain = brain
        self._cap = capability
        self.daily_spend = 0.0
        self.effective_calls: list = []

    async def select(self, **kw) -> BrainSelectionResult:
        return self._brain

    def effective_schema_capability(self, *, declared: str, served_model):
        self.effective_calls.append((declared, served_model))
        return SimpleNamespace(
            served_model=served_model or "served-model-under-test",
            declared=declared, capability=self._cap,
            changed=(declared != self._cap), reason="test policy",
        )


class _Surface:
    """Binds the production methods under test onto the minimum admission
    surface: the resource snapshot, the brain selector, the FSM registries,
    and the terminal-event sink."""

    submit_background = GovernedLoopService.submit_background
    _admit_routing = GovernedLoopService._admit_routing

    def __init__(self, tmp_path, *, brain: BrainSelectionResult, capability: str,
                 pool: _Pool | None) -> None:
        self._bg_pool = pool
        self._brain_selector = _Selector(brain, capability)
        self._active_ops: set = set()
        self._active_brain_set = None
        self._fsm_contexts: dict = {}
        self._fsm_checkpoint_seq: dict = {}
        self._trust_graduator = None
        self._advanced_autonomy = None
        self._config = SimpleNamespace(project_root=tmp_path)
        snap = SimpleNamespace(
            platform_arch="x86_64", cpu_percent=1.0, ram_available_gb=8.0,
            pressure_for_load=lambda n: SimpleNamespace(name="LOW"),
            sampled_monotonic_ns=time.monotonic_ns(), collector_status="ok",
        )

        async def _snapshot():
            return snap

        async def _noop(**kw):
            return None

        self._stack = SimpleNamespace(
            resource_monitor=SimpleNamespace(snapshot=_snapshot),
            comm=SimpleNamespace(emit_heartbeat=_noop, emit_intent=_noop),
        )
        self.terminal_events: list = []
        self.sync_submits: list = []

    async def _emit_terminal_events(self, *, ctx, result, **kw) -> None:
        self.terminal_events.append((ctx, result))

    async def submit(self, ctx, trigger_source: str = "unknown"):
        self.sync_submits.append(ctx)
        return SimpleNamespace(op_id=ctx.op_id)


def _brain(tier: str = "local_prime") -> BrainSelectionResult:
    return BrainSelectionResult(
        brain_id="qwen_coder_32b", model_name="qwen2.5-coder:32b",
        fallback_model="qwen2.5-coder:7b", routing_reason="cai_intent_code_generation",
        task_complexity="heavy_code", estimated_prompt_tokens=1200,
        provider_tier=tier, schema_capability="full_content_only",
    )


def _ctx() -> OperationContext:
    return OperationContext.create(
        description="harden the tracer auth backoff",
        target_files=("backend/core/ouroboros/governance/dw_capacity_probe.py",),
        target_symbols=("_tracer_auth_recheck_s",),
    )


@pytest.mark.asyncio
async def test_a_background_op_reaches_the_pool_admitted(tmp_path):
    pool = _Pool()
    gls = _Surface(tmp_path, brain=_brain(), capability="full_content_and_diff", pool=pool)
    raw = _ctx()
    assert raw.telemetry is None

    op_id = await gls.submit_background(raw, trigger_source="roadmap")

    assert op_id == raw.op_id
    assert len(pool.submitted) == 1
    admitted = pool.submitted[0]
    ri = admitted.telemetry.routing_intent
    assert ri.brain_id == "qwen_coder_32b" and ri.brain_model == "qwen2.5-coder:32b"
    assert ri.schema_capability == "full_content_and_diff", "the SERVED verdict, not the slot's declaration"
    assert ri.served_model == "served-model-under-test"
    assert admitted.telemetry.local_node.arch == "x86_64"
    assert admitted.frozen_autonomy_tier == "governed"
    assert admitted.target_symbols == ("_tracer_auth_recheck_s",), "admission preserves the declared contract"
    assert raw.op_id in gls._fsm_contexts
    assert gls.sync_submits == [], "the pool path never falls back when the pool accepts"


@pytest.mark.asyncio
async def test_a_refused_admission_never_reaches_the_pool(tmp_path):
    pool = _Pool()
    gls = _Surface(tmp_path, brain=_brain(tier="queued"), capability="full_content_only", pool=pool)
    raw = _ctx()

    op_id = await gls.submit_background(raw, trigger_source="roadmap")

    assert op_id == raw.op_id
    assert pool.submitted == [], "a cost-gate-queued op must not run in the background either"
    assert len(gls.terminal_events) == 1
    _, result = gls.terminal_events[0]
    assert result.reason_code == "cost_gate_triggered_queue"


@pytest.mark.asyncio
async def test_the_sync_fallback_admits_exactly_once_from_the_raw_context(tmp_path):
    pool = _Pool(fault=RuntimeError("pool not started"))
    gls = _Surface(tmp_path, brain=_brain(), capability="full_content_and_diff", pool=pool)
    raw = _ctx()

    op_id = await gls.submit_background(raw, trigger_source="roadmap")

    assert op_id == raw.op_id
    assert len(gls.sync_submits) == 1
    assert gls.sync_submits[0].telemetry is None, (
        "submit() performs its own admission; handing it an already-admitted ctx would stamp twice"
    )


def test_routing_admission_is_built_in_exactly_one_place():
    """DRY guard: the routing intent is constructed once, and both entry
    points route through the shared admission."""
    module_src = inspect.getsource(inspect.getmodule(GovernedLoopService))
    assert module_src.count("RoutingIntentTelemetry(") == 1
    assert "_admit_routing(" in inspect.getsource(GovernedLoopService.submit)
    assert "_admit_routing(" in inspect.getsource(GovernedLoopService.submit_background)
    assert "with_telemetry(" in inspect.getsource(GovernedLoopService._admit_routing)
    assert "with_telemetry(" not in inspect.getsource(GovernedLoopService.submit)
