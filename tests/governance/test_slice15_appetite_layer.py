"""Slice 15 T1+T2 — dispatcher value-gate wiring + semantic weight (RED).

T1 (the Run-24 known-inert seam): the value gate must execute on the LIVE
route — proven by driving ``dispatch_pipeline`` ITSELF with a stub registry
(runtime reachability, never a source-slice pin — the T5 lesson's final
form). A cosmetic candidate terminates ``no_op_cosmetic`` before the
VALIDATE runner is ever invoked; a substantive one flows through untouched.

T2 (mandate 3): ONE engine — ``candidate_value_gate`` emits semantic WEIGHT
(count of executable AST statements added/removed/changed; line-grammar
formats: changed residue lines; indeterminate: None) and the Slice-13
verdicts become weight==0 wrappers. Existing verdict pins must stay green.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
)


# ---------------------------------------------------------------------------
# T2 — semantic weight engine
# ---------------------------------------------------------------------------


class TestSemanticWeight:
    def _w(self, root, rel, new):
        from backend.core.ouroboros.governance.candidate_value_gate import (
            file_semantic_weight,
        )
        return file_semantic_weight(root, rel, new)

    def test_cosmetic_change_weighs_zero(self, tmp_path):
        (tmp_path / "m.py").write_text("def f(x):\n    return x + 1\n")
        assert self._w(
            tmp_path, "m.py", "# note\ndef f(x):\n    return x + 1\n",
        ) == 0

    def test_single_constant_change_weighs_one(self, tmp_path):
        (tmp_path / "m.py").write_text("TIMEOUT = 30\n")
        assert self._w(tmp_path, "m.py", "TIMEOUT = 31\n") == 1

    def test_added_function_weighs_its_statements(self, tmp_path):
        (tmp_path / "m.py").write_text("x = 1\n")
        w = self._w(
            tmp_path, "m.py",
            "x = 1\n\ndef f(a):\n    b = a * 2\n    return b\n",
        )
        assert w is not None and w >= 3, (
            "a new function (def + 2 body statements) must weigh more "
            f"than a constant tweak — got {w}"
        )

    def test_syntax_error_weighs_none(self, tmp_path):
        (tmp_path / "m.py").write_text("x = 1\n")
        assert self._w(tmp_path, "m.py", "def broken(:\n") is None

    def test_requirements_weight_counts_residue_changes(self, tmp_path):
        (tmp_path / "requirements.txt").write_text(
            "torch==2.5.1\nnumpy==1.26.0\n",
        )
        assert self._w(
            tmp_path, "requirements.txt",
            "# note\ntorch==2.13.0\nnumpy==1.26.0\n",
        ) == 1

    def test_unknown_format_weighs_none(self, tmp_path):
        (tmp_path / "notes.md").write_text("a\n")
        assert self._w(tmp_path, "notes.md", "b\n") is None

    def test_verdicts_are_weight_wrappers(self):
        """DRY pin: classify_file_change must derive from the weight engine
        — one engine, no second diff evaluation."""
        import inspect
        from backend.core.ouroboros.governance import candidate_value_gate as g
        src = inspect.getsource(g.classify_file_change)
        assert "file_semantic_weight" in src

    def test_candidate_aggregate_sums_and_poisons(self, tmp_path):
        from backend.core.ouroboros.governance.candidate_value_gate import (
            candidate_semantic_weight,
        )
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 1\n")
        total, detail = candidate_semantic_weight(
            tmp_path, [("a.py", "x = 2\n"), ("b.py", "y = 2\n")],
        )
        assert total == 2 and len(detail) == 2
        (tmp_path / "notes.md").write_text("a\n")
        total2, _ = candidate_semantic_weight(
            tmp_path, [("a.py", "x = 2\n"), ("notes.md", "b\n")],
        )
        assert total2 is None, (
            "any indeterminate file poisons the aggregate to None — the "
            "mandate-4 fail-safe consumers route to BACKGROUND on None"
        )


# ---------------------------------------------------------------------------
# T3 — intake signal-value scoring (verifiable state only)
# ---------------------------------------------------------------------------


import json as _json


class TestSignalValueScoring:
    def _score(self, targets, evidence="", root=None):
        from backend.core.ouroboros.governance.signal_value import score_signal
        return score_signal("test", targets, evidence, root)

    def test_resolved_attribution_is_oracle(self, tmp_path):
        from backend.core.ouroboros.governance.signal_value import BAND_ORACLE
        ev = _json.dumps({"attribution": {"status": "resolved"}})
        assert self._score((), ev, tmp_path) == BAND_ORACLE

    def test_executable_python_target(self, tmp_path):
        from backend.core.ouroboros.governance.signal_value import (
            BAND_EXECUTABLE,
        )
        (tmp_path / "m.py").write_text("def f():\n    return 1\n")
        assert self._score(("m.py",), "", tmp_path) == BAND_EXECUTABLE

    def test_requirements_target_is_cosmetic_class(self, tmp_path):
        from backend.core.ouroboros.governance.signal_value import (
            BAND_COSMETIC_CLASS,
        )
        (tmp_path / "requirements.txt").write_text("torch==2.5.1\n")
        assert self._score(
            ("requirements.txt",), "", tmp_path,
        ) == BAND_COSMETIC_CLASS

    def test_docstring_only_module_is_cosmetic_class(self, tmp_path):
        from backend.core.ouroboros.governance.signal_value import (
            BAND_COSMETIC_CLASS,
        )
        (tmp_path / "d.py").write_text('"""Only a docstring."""\n')
        assert self._score(("d.py",), "", tmp_path) == BAND_COSMETIC_CLASS

    def test_no_targets_is_indeterminate(self, tmp_path):
        from backend.core.ouroboros.governance.signal_value import (
            BAND_INDETERMINATE,
        )
        assert self._score((), "", tmp_path) == BAND_INDETERMINATE

    def test_unreadable_target_is_indeterminate(self, tmp_path):
        from backend.core.ouroboros.governance.signal_value import (
            BAND_INDETERMINATE,
        )
        assert self._score(("ghost.py",), "", tmp_path) == BAND_INDETERMINATE

    def test_garbage_evidence_never_raises(self, tmp_path):
        assert self._score((), "{not json", tmp_path) in (0, 1, 2, 3)


# ---------------------------------------------------------------------------
# T4 — adaptive allocation + ceiling
# ---------------------------------------------------------------------------


class TestAdaptiveAllocation:
    def test_restricted_bands_never_scale(self):
        from backend.core.ouroboros.governance.signal_value import (
            adaptive_generation_scale,
        )
        assert adaptive_generation_scale(0) == (1.0, 1.0)
        assert adaptive_generation_scale(1) == (1.0, 1.0)

    def test_scale_is_a_function_of_band(self, monkeypatch):
        from backend.core.ouroboros.governance.signal_value import (
            adaptive_generation_scale,
        )
        monkeypatch.delenv("JARVIS_VALUE_TIME_SCALE_COEFF", raising=False)
        assert adaptive_generation_scale(2) == (1.5, 1.5)
        assert adaptive_generation_scale(3) == (2.0, 2.0)
        monkeypatch.setenv("JARVIS_VALUE_TIME_SCALE_COEFF", "1.0")
        t, _ = adaptive_generation_scale(3)
        assert t == 3.0

    def test_ceiling_only_for_oracle_band(self, monkeypatch):
        from backend.core.ouroboros.governance.signal_value import (
            value_ceiling_breached,
        )
        monkeypatch.delenv("JARVIS_VALUE_CEILING_FILES", raising=False)
        assert value_ceiling_breached(3, 5) is True
        assert value_ceiling_breached(3, 4) is False
        assert value_ceiling_breached(2, 100) is False

    def test_ceiling_floor_halts_for_human(self, tmp_path):
        from backend.core.ouroboros.governance.orchestrator import (
            _value_ceiling_risk_floor,
        )
        from backend.core.ouroboros.governance.risk_engine import RiskTier
        ev = _json.dumps({"attribution": {"status": "resolved"}})
        ctx = SimpleNamespace(
            op_id="op-vc-1",
            signal_source="test_failure",
            target_files=tuple(f"f{i}.py" for i in range(6)),
            intake_evidence_json=ev,
        )
        tier, note = _value_ceiling_risk_floor(ctx, RiskTier.NOTIFY_APPLY)
        assert tier is RiskTier.APPROVAL_REQUIRED
        assert note and "human" in note

    def test_ceiling_floor_never_demotes_or_fires_low(self, tmp_path):
        from backend.core.ouroboros.governance.orchestrator import (
            _value_ceiling_risk_floor,
        )
        from backend.core.ouroboros.governance.risk_engine import RiskTier
        ev = _json.dumps({"attribution": {"status": "resolved"}})
        ctx = SimpleNamespace(
            op_id="op-vc-2", signal_source="test_failure",
            target_files=("a.py", "b.py"), intake_evidence_json=ev,
        )
        tier, note = _value_ceiling_risk_floor(ctx, RiskTier.NOTIFY_APPLY)
        assert tier is RiskTier.NOTIFY_APPLY and note is None
        tier2, _ = _value_ceiling_risk_floor(
            SimpleNamespace(
                op_id="x", signal_source="test_failure",
                target_files=tuple(f"f{i}" for i in range(9)),
                intake_evidence_json=ev,
            ),
            RiskTier.BLOCKED,
        )
        assert tier2 is RiskTier.BLOCKED, "never touches BLOCKED"

    def test_timeout_seams_wired_on_both_paths(self):
        from pathlib import Path as _P
        repo = _P(__file__).resolve().parents[2]
        for rel in (
            "backend/core/ouroboros/governance/orchestrator.py",
            "backend/core/ouroboros/governance/phase_runners/generate_runner.py",
        ):
            src = (repo / rel).read_text()
            i = src.index("_route_timeouts = {")
            window = src[max(0, i - 2000):i + 2000]
            assert "adaptive_generation_scale" in window, (
                f"{rel}: the value-band scale must wire the REAL dispatch "
                "lever (T5 lesson: both paths)"
            )


class TestRouterValueBlock:
    def _ctx(self, tmp_path, *, urgency="normal", source="opportunity_miner",
             targets=(), evidence="", complexity="simple"):
        return SimpleNamespace(
            op_id="op-rt-1",
            signal_urgency=urgency,
            signal_source=source,
            task_complexity=complexity,
            target_files=targets,
            intake_evidence_json=evidence,
            provider_route="",
            provider_route_reason="",
        )

    def _classify(self, ctx):
        from backend.core.ouroboros.governance.urgency_router import (
            UrgencyRouter,
        )
        return UrgencyRouter().classify(ctx)

    def test_cosmetic_signal_clamps_to_background(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
        (tmp_path / "requirements.txt").write_text("torch==2.5.1\n")
        route, reason = self._classify(self._ctx(
            tmp_path, targets=("requirements.txt",),
        ))
        assert route.value == "background"
        assert reason.startswith("value_band=")

    def test_oracle_signal_escalates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
        ev = _json.dumps({"attribution": {"status": "resolved"}})
        route, reason = self._classify(self._ctx(
            tmp_path, source="test_failure", targets=("m.py",), evidence=ev,
        ))
        assert route.value == "standard"
        assert "oracle_escalation" in reason

    def test_oracle_complex_escalates_to_complex(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
        ev = _json.dumps({"attribution": {"status": "resolved"}})
        route, reason = self._classify(self._ctx(
            tmp_path, source="test_failure", targets=("m.py",),
            evidence=ev, complexity="architectural",
        ))
        assert route.value == "complex"

    def test_critical_urgency_falls_through_to_matrix(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
        (tmp_path / "requirements.txt").write_text("torch==2.5.1\n")
        route, reason = self._classify(self._ctx(
            tmp_path, urgency="critical", targets=("requirements.txt",),
        ))
        assert "value_band" not in reason, (
            "IMMEDIATE reflexes are untouched by the value block"
        )

    def test_master_off_is_inert(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_SIGNAL_VALUE_ROUTING_ENABLED", "false")
        monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
        (tmp_path / "requirements.txt").write_text("torch==2.5.1\n")
        _, reason = self._classify(self._ctx(
            tmp_path, targets=("requirements.txt",),
        ))
        assert "value_band" not in reason


# ---------------------------------------------------------------------------
# T1 — RUNTIME reachability on the live dispatcher route
# ---------------------------------------------------------------------------


def _ctx_at_generate(op_id: str):
    """Walk the LEGAL FSM chain to GENERATE (CLASSIFY→ROUTE→GENERATE)."""
    from tests.governance.test_cancel_propagation_slice2 import (
        _make_minimal_op_context,
    )
    return (
        _make_minimal_op_context(op_id)
        .advance(OperationPhase.ROUTE)
        .advance(OperationPhase.GENERATE)
    )


class _FakeComm:
    async def emit_postmortem(self, **kw):
        self.last = kw

    async def emit_heartbeat(self, **kw):
        pass


class _StubOrch:
    """Borrows the REAL gate helper + candidate iterator from Orchestrator
    (unbound functions bind to any instance) — the dispatcher hook then
    exercises production code, not a reimplementation."""

    _cancel_token_registry = None
    _iter_candidate_files = staticmethod(
        Orchestrator.__dict__["_iter_candidate_files"].__func__
        if isinstance(Orchestrator.__dict__.get("_iter_candidate_files"),
                      staticmethod)
        else Orchestrator._iter_candidate_files
    )
    _maybe_complete_cosmetic_candidate = (
        Orchestrator._maybe_complete_cosmetic_candidate
    )

    def __init__(self, tmp_path):
        self._config = OrchestratorConfig(project_root=tmp_path)
        self._stack = SimpleNamespace(comm=_FakeComm())
        self._ledger_records = []

    async def _record_ledger(self, ctx, state, data):
        self._ledger_records.append((ctx.op_id, state, data))


def _stub_orch(tmp_path):
    return _StubOrch(tmp_path)


@pytest.mark.asyncio
async def test_dispatcher_terminates_cosmetic_candidate_at_runtime(tmp_path, monkeypatch):
    """THE Run-24 fix, proven at RUNTIME: dispatch_pipeline itself must
    consult the value gate at the GENERATE→VALIDATE transition and
    terminate an all-cosmetic candidate as no_op_cosmetic WITHOUT ever
    invoking the VALIDATE runner."""
    from backend.core.ouroboros.governance.phase_dispatcher import (
        PhaseContext,
        PhaseRunnerRegistry,
        dispatch_pipeline,
    )
    from backend.core.ouroboros.governance.phase_runner import (
        PhaseResult,
        PhaseRunner,
    )
    from tests.governance.test_cancel_propagation_slice2 import (
        _make_minimal_op_context,
    )

    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    (tmp_path / "m.py").write_text("def f(x):\n    return x + 1\n")
    cosmetic = "# annotation\ndef f(x):\n    return x + 1\n"

    validate_invoked = []

    class _StubGenerate(PhaseRunner):
        phase = OperationPhase.GENERATE

        def __init__(self, pctx):
            self._pctx = pctx

        async def run(self, ctx):
            self._pctx.generation = SimpleNamespace(
                is_noop=False,
                provider_name="stub",
                candidates=[{
                    "file_path": "m.py",
                    "full_content": cosmetic,
                }],
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=OperationPhase.VALIDATE,
                status="ok",
            )

    class _SentinelValidate(PhaseRunner):
        phase = OperationPhase.VALIDATE

        async def run(self, ctx):
            validate_invoked.append(ctx.op_id)
            return PhaseResult(next_ctx=ctx, next_phase=None, status="ok")

    reg = PhaseRunnerRegistry()
    pctx = PhaseContext()
    reg.register(
        OperationPhase.GENERATE,
        lambda orch, serpent, p, ctx: _StubGenerate(p),
    )
    reg.register(
        OperationPhase.VALIDATE,
        lambda orch, serpent, p, ctx: _SentinelValidate(),
    )

    start = _ctx_at_generate("op-slice15-rt-1")
    final = await dispatch_pipeline(
        _stub_orch(tmp_path), None, start,
        registry=reg, initial_context=pctx, max_iterations=6,
    )

    assert validate_invoked == [], (
        "cosmetic candidate must terminate BEFORE the VALIDATE runner — "
        "the Run-24 inert-seam class"
    )
    assert final.terminal_reason_code == "no_op_cosmetic"


@pytest.mark.asyncio
async def test_dispatcher_passes_substantive_candidate_through(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.phase_dispatcher import (
        PhaseContext,
        PhaseRunnerRegistry,
        dispatch_pipeline,
    )
    from backend.core.ouroboros.governance.phase_runner import (
        PhaseResult,
        PhaseRunner,
    )
    from tests.governance.test_cancel_propagation_slice2 import (
        _make_minimal_op_context,
    )

    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    (tmp_path / "m.py").write_text("def f(x):\n    return x + 1\n")

    validate_invoked = []

    class _StubGenerate(PhaseRunner):
        phase = OperationPhase.GENERATE

        def __init__(self, pctx):
            self._pctx = pctx

        async def run(self, ctx):
            self._pctx.generation = SimpleNamespace(
                is_noop=False, provider_name="stub",
                candidates=[{
                    "file_path": "m.py",
                    "full_content": "def f(x):\n    return x + 2\n",
                }],
            )
            return PhaseResult(
                next_ctx=ctx, next_phase=OperationPhase.VALIDATE,
                status="ok",
            )

    class _SentinelValidate(PhaseRunner):
        phase = OperationPhase.VALIDATE

        async def run(self, ctx):
            validate_invoked.append(ctx.op_id)
            return PhaseResult(next_ctx=ctx, next_phase=None, status="ok")

    reg = PhaseRunnerRegistry()
    pctx = PhaseContext()
    reg.register(
        OperationPhase.GENERATE,
        lambda orch, serpent, p, ctx: _StubGenerate(p),
    )
    reg.register(
        OperationPhase.VALIDATE,
        lambda orch, serpent, p, ctx: _SentinelValidate(),
    )

    start = _ctx_at_generate("op-slice15-rt-2")
    await dispatch_pipeline(
        _stub_orch(tmp_path), None, start,
        registry=reg, initial_context=pctx, max_iterations=6,
    )
    assert validate_invoked == ["op-slice15-rt-2"], (
        "a substantive candidate must flow to VALIDATE untouched "
        "(fail-safe forward)"
    )
