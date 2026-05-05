"""M10 Slice 4 — Lifecycle (validation + PR) tests
(PRD §32.4.2).

Pins:
  § 1 — Closed taxonomies (5-layer + 5-verdict + 4-stage)
  § 2 — Frozen result containers
  § 3 — Env knobs — clamping + defaults
  § 4 — `validate_only` 4-layer subset (Layers 1-4 + Layer 5 deferred)
  § 5 — Layer 1 short-circuit on FAILED
  § 6 — Layers 3+4 parallel via gather
  § 7 — Layer aggregation rules (any FAILED → FAILED, all DISABLED → DISABLED)
  § 8 — `advance` happy path → AWAITING_APPROVAL
  § 9 — H3 inheritance — push fail preserves branch (PUSH_FAILED)
  § 10 — Worktree fail / commit fail / PR-queue fail → FAILED
  § 11 — Master flag off → DECIDED_SKIP
  § 12 — Bridge defensive wrappers (raise / non-Result return)
  § 13 — Authority floor (no orchestrator/iron_gate imports)
  § 14 — Public exports
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )


def _make_synth(*, code_text="def f(): pass",
                ast_pin_name="my_pin"):
    from backend.core.ouroboros.governance.m10.primitives import (
        ProposalKind,
    )
    from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
        SynthesizedProposal, SynthesisVerdict,
    )
    return SynthesizedProposal(
        proposal_id="m10-test-1",
        kind=ProposalKind.NEW_SENSOR,
        verdict=SynthesisVerdict.SYNTHESIZED,
        code_text=code_text,
        class_name="MyClass",
        module_path="backend/my_class.py",
        ast_pin_name=ast_pin_name,
        consensus_signature="abc" * 16,
        candidate_count=3,
    )


# ---------------------------------------------------------------------------
# Stub bridges + layers
# ---------------------------------------------------------------------------


def _make_passing_layers():
    from backend.core.ouroboros.governance.m10.lifecycle import (
        LayerResult, LayerVerdict, ValidationLayer,
    )

    class _Layers:
        async def run_side_effect_firewall(self, **_):
            return LayerResult(
                layer=ValidationLayer.SIDE_EFFECT_FIREWALL,
                verdict=LayerVerdict.PASSED,
            )

        async def run_protocol_conformance(self, **_):
            return LayerResult(
                layer=ValidationLayer.PROTOCOL_CONFORMANCE,
                verdict=LayerVerdict.PASSED,
            )

        async def run_semantic_guardian(self, **_):
            return LayerResult(
                layer=ValidationLayer.SEMANTIC_GUARDIAN,
                verdict=LayerVerdict.PASSED,
            )

        async def run_security_scanner(self, **_):
            return LayerResult(
                layer=ValidationLayer.SECURITY_SCANNER,
                verdict=LayerVerdict.PASSED,
            )

        async def run_pytest_in_worktree(self, **_):
            return LayerResult(
                layer=ValidationLayer.PYTEST_IN_WORKTREE,
                verdict=LayerVerdict.PASSED,
            )

    return _Layers()


def _make_passing_bridges():
    from backend.core.ouroboros.governance.m10.lifecycle import (
        CommitResult, PRQueueResult, WorktreeResult,
    )

    class _Wt:
        async def create_worktree(
            self, *, proposal_id, branch_name,
        ):
            return WorktreeResult(
                success=True,
                worktree_path=f"/tmp/m10/{proposal_id}",
                branch_name=branch_name,
            )

    class _Co:
        async def write_and_commit(self, **_):
            return CommitResult(
                success=True, commit_hash="abc123",
            )

    class _Pr:
        async def queue_review_pr(
            self, *, proposal_id, branch_name, **_,
        ):
            return PRQueueResult(
                success=True,
                pr_url=f"https://gh/pr/{proposal_id}",
                branch_name=branch_name,
            )

    return _Wt(), _Co(), _Pr()


# ---------------------------------------------------------------------------
# § 1 — Closed taxonomies
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_validation_layer_5_values(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ValidationLayer,
        )
        assert {m.value for m in ValidationLayer} == {
            "side_effect_firewall",
            "protocol_conformance",
            "semantic_guardian",
            "security_scanner",
            "pytest_in_worktree",
        }

    def test_layer_verdict_5_values(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerVerdict,
        )
        assert {m.value for m in LayerVerdict} == {
            "passed", "failed", "skipped",
            "disabled", "provider_error",
        }

    def test_lifecycle_stage_4_values(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LifecycleStage,
        )
        assert {m.value for m in LifecycleStage} == {
            "validation", "commit", "push", "review",
        }


# ---------------------------------------------------------------------------
# § 2 — Frozen result containers
# ---------------------------------------------------------------------------


class TestFrozenContainers:
    def test_layer_result_frozen(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict, ValidationLayer,
        )
        r = LayerResult(
            layer=ValidationLayer.SEMANTIC_GUARDIAN,
            verdict=LayerVerdict.PASSED,
        )
        with pytest.raises(Exception):
            r.verdict = LayerVerdict.FAILED  # type: ignore[misc]

    def test_validation_result_passed_property(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerVerdict, ValidationResult,
        )
        v = ValidationResult(
            proposal_id="x",
            overall_verdict=LayerVerdict.PASSED,
        )
        assert v.passed is True

    def test_lifecycle_result_to_dict(self):
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleResult,
        )
        r = ProposalLifecycleResult(
            proposal_id="x",
            final_phase=M10ProposalPhase.AWAITING_APPROVAL,
        )
        d = r.to_dict()
        assert d["final_phase"] == "awaiting_approval"
        assert d["proposal_id"] == "x"


# ---------------------------------------------------------------------------
# § 3 — Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_pytest_timeout_default_120(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_LIFECYCLE_PYTEST_TIMEOUT_S",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            m10_lifecycle_pytest_timeout_s,
        )
        assert m10_lifecycle_pytest_timeout_s() == 120.0

    def test_layer_timeout_default_30(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_LIFECYCLE_LAYER_TIMEOUT_S",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            m10_lifecycle_layer_timeout_s,
        )
        assert m10_lifecycle_layer_timeout_s() == 30.0


# ---------------------------------------------------------------------------
# § 4 — validate_only
# ---------------------------------------------------------------------------


class TestValidateOnly:
    @pytest.mark.asyncio
    async def test_validate_only_layers_1_through_4(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerVerdict, ProposalLifecycleOrchestrator,
            ValidationLayer,
        )
        orch = ProposalLifecycleOrchestrator()
        result = await orch.validate_only(
            _make_synth(), layers=_make_passing_layers(),
        )
        assert result.passed
        # Layer 5 is DISABLED in validate_only mode
        layer5 = next(
            r for r in result.layer_results
            if r.layer is ValidationLayer.PYTEST_IN_WORKTREE
        )
        assert layer5.verdict is LayerVerdict.DISABLED
        assert "deferred" in layer5.detail.lower()

    @pytest.mark.asyncio
    async def test_validate_only_no_layers_returns_disabled(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerVerdict, ProposalLifecycleOrchestrator,
        )
        orch = ProposalLifecycleOrchestrator()
        result = await orch.validate_only(
            _make_synth(), layers=None,
        )
        assert result.overall_verdict is LayerVerdict.DISABLED


# ---------------------------------------------------------------------------
# § 5 — Layer 1 short-circuit
# ---------------------------------------------------------------------------


class TestLayer1ShortCircuit:
    @pytest.mark.asyncio
    async def test_firewall_failure_short_circuits(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict,
            ProposalLifecycleOrchestrator, ValidationLayer,
        )

        class _FailingFirewall:
            async def run_side_effect_firewall(self, **_):
                return LayerResult(
                    layer=(
                        ValidationLayer.SIDE_EFFECT_FIREWALL
                    ),
                    verdict=LayerVerdict.FAILED,
                    detail="module-level open()",
                )

            async def run_protocol_conformance(self, **_):
                # Should NOT be called (short-circuit)
                raise RuntimeError("layer 2 should be skipped")

            async def run_semantic_guardian(self, **_):
                raise RuntimeError("not reached")

            async def run_security_scanner(self, **_):
                raise RuntimeError("not reached")

            async def run_pytest_in_worktree(self, **_):
                raise RuntimeError("not reached")

        orch = ProposalLifecycleOrchestrator()
        result = await orch.validate_only(
            _make_synth(), layers=_FailingFirewall(),
        )
        assert result.overall_verdict is LayerVerdict.FAILED
        # Only Layer 1 ran
        assert len(result.layer_results) == 1


# ---------------------------------------------------------------------------
# § 6 — Layers 3+4 parallel via gather
# ---------------------------------------------------------------------------


class TestLayers3and4Parallel:
    @pytest.mark.asyncio
    async def test_both_run_when_layers_1_2_pass(
        self, monkeypatch,
    ):
        """Verify gather IS called by checking both Layer 3
        and Layer 4 results appear when 1+2 pass."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict,
            ProposalLifecycleOrchestrator, ValidationLayer,
        )

        sg_called: list = []
        ss_called: list = []

        class _Layers:
            async def run_side_effect_firewall(self, **_):
                return LayerResult(
                    layer=(
                        ValidationLayer.SIDE_EFFECT_FIREWALL
                    ),
                    verdict=LayerVerdict.PASSED,
                )

            async def run_protocol_conformance(self, **_):
                return LayerResult(
                    layer=(
                        ValidationLayer.PROTOCOL_CONFORMANCE
                    ),
                    verdict=LayerVerdict.PASSED,
                )

            async def run_semantic_guardian(self, **_):
                sg_called.append(True)
                await asyncio.sleep(0.01)
                return LayerResult(
                    layer=ValidationLayer.SEMANTIC_GUARDIAN,
                    verdict=LayerVerdict.PASSED,
                )

            async def run_security_scanner(self, **_):
                ss_called.append(True)
                await asyncio.sleep(0.01)
                return LayerResult(
                    layer=ValidationLayer.SECURITY_SCANNER,
                    verdict=LayerVerdict.PASSED,
                )

            async def run_pytest_in_worktree(self, **_):
                return LayerResult(
                    layer=ValidationLayer.PYTEST_IN_WORKTREE,
                    verdict=LayerVerdict.PASSED,
                )

        orch = ProposalLifecycleOrchestrator()
        result = await orch.validate_only(
            _make_synth(), layers=_Layers(),
        )
        assert result.passed
        assert len(sg_called) == 1
        assert len(ss_called) == 1


# ---------------------------------------------------------------------------
# § 7 — Aggregation rules
# ---------------------------------------------------------------------------


class TestAggregationRules:
    def test_any_failed_overall_failed(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict, ValidationLayer,
            _aggregate_validation_verdict,
        )
        results = [
            LayerResult(
                layer=ValidationLayer.SIDE_EFFECT_FIREWALL,
                verdict=LayerVerdict.PASSED,
            ),
            LayerResult(
                layer=ValidationLayer.SEMANTIC_GUARDIAN,
                verdict=LayerVerdict.FAILED,
            ),
        ]
        assert (
            _aggregate_validation_verdict(results)
            is LayerVerdict.FAILED
        )

    def test_provider_error_overall_failed(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict, ValidationLayer,
            _aggregate_validation_verdict,
        )
        results = [
            LayerResult(
                layer=ValidationLayer.SECURITY_SCANNER,
                verdict=LayerVerdict.PROVIDER_ERROR,
            ),
        ]
        assert (
            _aggregate_validation_verdict(results)
            is LayerVerdict.FAILED
        )

    def test_all_disabled_overall_disabled(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict, ValidationLayer,
            _aggregate_validation_verdict,
        )
        results = [
            LayerResult(
                layer=ValidationLayer.SIDE_EFFECT_FIREWALL,
                verdict=LayerVerdict.DISABLED,
            ),
            LayerResult(
                layer=ValidationLayer.SEMANTIC_GUARDIAN,
                verdict=LayerVerdict.DISABLED,
            ),
        ]
        assert (
            _aggregate_validation_verdict(results)
            is LayerVerdict.DISABLED
        )

    def test_passed_with_skipped_overall_passed(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict, ValidationLayer,
            _aggregate_validation_verdict,
        )
        results = [
            LayerResult(
                layer=ValidationLayer.SIDE_EFFECT_FIREWALL,
                verdict=LayerVerdict.PASSED,
            ),
            LayerResult(
                layer=ValidationLayer.PROTOCOL_CONFORMANCE,
                verdict=LayerVerdict.SKIPPED,
            ),
        ]
        assert (
            _aggregate_validation_verdict(results)
            is LayerVerdict.PASSED
        )

    def test_empty_overall_disabled(self):
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerVerdict, _aggregate_validation_verdict,
        )
        assert (
            _aggregate_validation_verdict([])
            is LayerVerdict.DISABLED
        )


# ---------------------------------------------------------------------------
# § 8 — advance happy path
# ---------------------------------------------------------------------------


class TestAdvanceHappyPath:
    @pytest.mark.asyncio
    async def test_full_pipeline_to_awaiting_approval(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleOrchestrator,
        )
        wt, co, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt,
            commit_bridge=co,
            pr_bridge=pr,
        )
        assert (
            result.final_phase
            is M10ProposalPhase.AWAITING_APPROVAL
        )
        assert result.validation_result.passed
        assert result.worktree_result.success
        assert result.commit_result.success
        assert result.pr_result.success
        assert "m10-test-1" in result.pr_result.pr_url

    @pytest.mark.asyncio
    async def test_pr_branch_namespace(self, monkeypatch):
        """All M10 proposal branches MUST namespace under
        ``ouroboros/m10/`` so OrangePR cleanup paths don't
        conflict."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleOrchestrator,
        )
        wt, co, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt, commit_bridge=co,
            pr_bridge=pr,
        )
        assert (
            result.pr_result.branch_name
            == "ouroboros/m10/m10-test-1"
        )


# ---------------------------------------------------------------------------
# § 9 — H3 push-fail preserves branch
# ---------------------------------------------------------------------------


class TestPushFailPreservesBranch:
    @pytest.mark.asyncio
    async def test_push_fail_to_push_failed_phase(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            PRQueueResult,
            ProposalLifecycleOrchestrator,
        )

        class _PrPushFail:
            async def queue_review_pr(self, **_):
                return PRQueueResult(
                    success=False, push_failed=True,
                    error="git push exit 128",
                )

        wt, co, _ = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt, commit_bridge=co,
            pr_bridge=_PrPushFail(),
        )
        assert (
            result.final_phase is M10ProposalPhase.PUSH_FAILED
        )
        assert "preserved" in result.failure_reason.lower()


# ---------------------------------------------------------------------------
# § 10 — Worktree / commit / PR-queue failures
# ---------------------------------------------------------------------------


class TestStageFailures:
    @pytest.mark.asyncio
    async def test_worktree_fail_to_failed(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleOrchestrator, WorktreeResult,
        )

        class _WtFail:
            async def create_worktree(self, **_):
                return WorktreeResult(
                    success=False,
                    error="branch_already_exists",
                )

        _, co, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=_WtFail(),
            commit_bridge=co, pr_bridge=pr,
        )
        assert result.final_phase is M10ProposalPhase.FAILED
        assert (
            "worktree_create_failed"
            in result.failure_reason
        )

    @pytest.mark.asyncio
    async def test_commit_fail_to_failed(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            CommitResult, ProposalLifecycleOrchestrator,
        )

        class _CoFail:
            async def write_and_commit(self, **_):
                return CommitResult(
                    success=False,
                    error="hook_failed",
                )

        wt, _, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt, commit_bridge=_CoFail(),
            pr_bridge=pr,
        )
        assert result.final_phase is M10ProposalPhase.FAILED
        assert (
            "commit_failed" in result.failure_reason
        )

    @pytest.mark.asyncio
    async def test_pr_queue_fail_no_push_to_failed(
        self, monkeypatch,
    ):
        """PR queue failure WITHOUT push_failed flag is
        FAILED (not PUSH_FAILED)."""
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            PRQueueResult,
            ProposalLifecycleOrchestrator,
        )

        class _PrFail:
            async def queue_review_pr(self, **_):
                return PRQueueResult(
                    success=False,
                    push_failed=False,
                    error="gh api unreachable",
                )

        wt, co, _ = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt, commit_bridge=co,
            pr_bridge=_PrFail(),
        )
        assert result.final_phase is M10ProposalPhase.FAILED
        assert (
            "pr_queue_failed" in result.failure_reason
        )

    @pytest.mark.asyncio
    async def test_pytest_fail_to_failed(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            LayerResult, LayerVerdict,
            ProposalLifecycleOrchestrator, ValidationLayer,
        )

        class _PytestFailLayers:
            async def run_side_effect_firewall(self, **_):
                return LayerResult(
                    layer=(
                        ValidationLayer.SIDE_EFFECT_FIREWALL
                    ),
                    verdict=LayerVerdict.PASSED,
                )

            async def run_protocol_conformance(self, **_):
                return LayerResult(
                    layer=(
                        ValidationLayer.PROTOCOL_CONFORMANCE
                    ),
                    verdict=LayerVerdict.PASSED,
                )

            async def run_semantic_guardian(self, **_):
                return LayerResult(
                    layer=ValidationLayer.SEMANTIC_GUARDIAN,
                    verdict=LayerVerdict.PASSED,
                )

            async def run_security_scanner(self, **_):
                return LayerResult(
                    layer=ValidationLayer.SECURITY_SCANNER,
                    verdict=LayerVerdict.PASSED,
                )

            async def run_pytest_in_worktree(self, **_):
                return LayerResult(
                    layer=ValidationLayer.PYTEST_IN_WORKTREE,
                    verdict=LayerVerdict.FAILED,
                    detail="3 of 5 tests failed",
                )

        wt, co, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_PytestFailLayers(),
            worktree_bridge=wt, commit_bridge=co,
            pr_bridge=pr,
        )
        assert result.final_phase is M10ProposalPhase.FAILED
        assert "pytest_failed" in result.failure_reason


# ---------------------------------------------------------------------------
# § 11 — Master flag off
# ---------------------------------------------------------------------------


class TestMasterFlagOff:
    @pytest.mark.asyncio
    async def test_master_off_returns_decided_skip(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.m10.primitives import (
            M10ProposalPhase,
        )
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleOrchestrator,
        )
        wt, co, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt, commit_bridge=co,
            pr_bridge=pr,
        )
        assert (
            result.final_phase
            is M10ProposalPhase.DECIDED_SKIP
        )
        assert result.failure_reason == "master_flag_off"


# ---------------------------------------------------------------------------
# § 12 — Bridge defensive wrappers
# ---------------------------------------------------------------------------


class TestBridgeSafety:
    @pytest.mark.asyncio
    async def test_worktree_bridge_raises_caught(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleOrchestrator,
        )

        class _WtRaise:
            async def create_worktree(self, **_):
                raise RuntimeError("synthetic")

        _, co, pr = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=_WtRaise(),
            commit_bridge=co, pr_bridge=pr,
        )
        # MUST NOT raise; failure surfaces in result
        assert result.worktree_result.success is False
        assert (
            "RuntimeError" in result.worktree_result.error
        )

    @pytest.mark.asyncio
    async def test_pr_bridge_returns_wrong_type_caught(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.lifecycle import (
            ProposalLifecycleOrchestrator,
        )

        class _PrWrongType:
            async def queue_review_pr(self, **_):
                return "not a PRQueueResult"

        wt, co, _ = _make_passing_bridges()
        orch = ProposalLifecycleOrchestrator()
        result = await orch.advance(
            _make_synth(),
            layers=_make_passing_layers(),
            worktree_bridge=wt, commit_bridge=co,
            pr_bridge=_PrWrongType(),
        )
        assert result.pr_result.success is False
        assert (
            "non-PRQueueResult" in result.pr_result.error
        )


# ---------------------------------------------------------------------------
# § 13 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
        "from backend.core.ouroboros.governance.graduation_orchestrator",
        # Bridge implementations are caller-injected — module
        # MUST NOT import the real WorktreeManager /
        # AutoCommitter / OrangePRReviewer
        "from backend.core.ouroboros.governance.worktree_manager",
        "from backend.core.ouroboros.governance.auto_committer",
        "from backend.core.ouroboros.governance.orange_pr_reviewer",
    )

    def test_lifecycle_module_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "m10" / "lifecycle.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"lifecycle.py must NOT import {forbidden} — "
                f"all real-world deps caller-injected via "
                f"Protocol"
            )


# ---------------------------------------------------------------------------
# § 14 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance.m10 import (
            lifecycle,
        )
        expected = sorted([
            "CommitBridgeProtocol",
            "CommitResult",
            "LayerResult",
            "LayerVerdict",
            "LifecycleStage",
            "M10_LIFECYCLE_SCHEMA_VERSION",
            "OrangePRBridgeProtocol",
            "PRQueueResult",
            "ProposalLifecycleOrchestrator",
            "ProposalLifecycleResult",
            "ValidationLayer",
            "ValidationLayersProtocol",
            "ValidationResult",
            "WorktreeBridgeProtocol",
            "WorktreeResult",
            "get_default_lifecycle",
            "m10_lifecycle_layer_timeout_s",
            "m10_lifecycle_pytest_timeout_s",
            "reset_default_lifecycle_for_tests",
        ])
        assert sorted(lifecycle.__all__) == expected
