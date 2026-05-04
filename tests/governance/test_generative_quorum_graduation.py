"""Move 6 Slice 5 — Graduation regression tests (CLOSES Move 6).

Coverage:

  * **Master flag default-FALSE** — ``JARVIS_GENERATIVE_QUORUM_
    ENABLED`` deliberately remains operator-controlled
    (cost-correct: K× generation cost ramp is opt-in).
  * **Sub-gate flag default-TRUE** — graduated. Operator may
    revert via explicit env false.
  * **Asymmetric env semantics** — empty/whitespace = unset =
    current default; explicit ``0``/``false``/``no``/``off``
    hot-reverts; truthy variants flip on.
  * **Floor + ceiling cap clamps** — K knob + agreement-
    threshold knob enforce min/max regardless of operator input.
  * **SSE event vocabulary** — ``EVENT_TYPE_QUORUM_OUTCOME``
    string stable; ``publish_quorum_outcome`` is master-flag-
    gated + DISABLED-silenced + best-effort.
  * **shipped_code_invariants** — 5 Move 6 pins are registered
    AND currently hold against shipped code.
  * **FlagRegistry seeds** — 6 Move 6 FlagSpec entries register
    via ``seed_default_registry``.
  * **Operator surfaces** — SSE event publishes carry
    schema_version + structured payload; broker-missing /
    publish-error all return None silently.
  * **Full-revert matrix** — every Slice 5 surface is reachable
    in the disabled state without raising.
  * **Authority invariants** — final pass that no Slice 5
    surface introduces orchestrator-tier dependencies.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# 1. Master flag — DELIBERATELY default-false (operator-controlled)
# ---------------------------------------------------------------------------


class TestMasterFlagDefaultFalse:
    """Slice 5 graduates the OBSERVABILITY surfaces but keeps the
    master flag default-false. Rationale: Quorum incurs K× generation
    cost on every APPROVAL_REQUIRED+ op — this is an explicit operator
    decision, not an autonomous default. Mirrors the
    JARVIS_PLAN_APPROVAL_MODE pattern."""

    def test_master_default_is_true_post_q4_graduation(self):
        # Q4 Priority #1 graduation (2026-05-02): operator
        # authorized after empirical verification of cost gates.
        # Sub-gate / risk-tier filter / COST_GATED_ROUTES still
        # bound K× amplification.
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_enabled,
        )
        os.environ.pop("JARVIS_GENERATIVE_QUORUM_ENABLED", None)
        assert quorum_enabled() is True

    def test_master_explicit_true_flips_on(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_enabled,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_GENERATIVE_QUORUM_ENABLED": "true"},
        ):
            assert quorum_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
    def test_master_truthy_variants(self, v):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_enabled,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_GENERATIVE_QUORUM_ENABLED": v},
        ):
            assert quorum_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_master_falsy_variants(self, v):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_enabled,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_GENERATIVE_QUORUM_ENABLED": v},
        ):
            assert quorum_enabled() is False


# ---------------------------------------------------------------------------
# 2. Sub-gate flag — graduated default-true
# ---------------------------------------------------------------------------


class TestSubGateGraduated:
    def test_sub_gate_default_is_true(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            quorum_gate_enabled,
        )
        os.environ.pop("JARVIS_QUORUM_GATE_ENABLED", None)
        assert quorum_gate_enabled() is True

    def test_sub_gate_explicit_false_hot_reverts(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            quorum_gate_enabled,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_GATE_ENABLED": "false"},
        ):
            assert quorum_gate_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_sub_gate_whitespace_treated_as_unset(self, v):
        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            quorum_gate_enabled,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_GATE_ENABLED": v},
        ):
            assert quorum_gate_enabled() is True


# ---------------------------------------------------------------------------
# 3. Cap-structure clamps — K + agreement-threshold floors+ceilings
# ---------------------------------------------------------------------------


class TestCapStructureClamps:
    def test_k_floor_clamp(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_k,
        )
        # Below floor (1) clamps to floor (2)
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "1"},
        ):
            assert quorum_k() == 2

    def test_k_negative_clamps_to_floor(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_k,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "-99"},
        ):
            assert quorum_k() == 2

    def test_k_ceiling_clamp(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_k,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "10"},
        ):
            assert quorum_k() == 5

    def test_k_default_three(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_k,
        )
        os.environ.pop("JARVIS_QUORUM_K", None)
        assert quorum_k() == 3

    def test_k_garbage_falls_back_to_default(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            quorum_k,
        )
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "not-a-number"},
        ):
            assert quorum_k() == 3

    def test_threshold_floor_clamp(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            agreement_threshold,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_QUORUM_AGREEMENT_THRESHOLD": "1"},
        ):
            assert agreement_threshold() == 2

    def test_threshold_default_two(self):
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            agreement_threshold,
        )
        os.environ.pop(
            "JARVIS_QUORUM_AGREEMENT_THRESHOLD", None,
        )
        assert agreement_threshold() == 2


# ---------------------------------------------------------------------------
# 4. SSE event vocabulary + publisher
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_type_string_stable(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
            EVENT_TYPE_QUORUM_OUTCOME,
        )
        assert (
            EVENT_TYPE_QUORUM_OUTCOME == "generative_quorum_outcome"
        )

    def test_publisher_master_off_returns_none(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
            publish_quorum_outcome,
        )
        os.environ.pop("JARVIS_GENERATIVE_QUORUM_ENABLED", None)
        result = publish_quorum_outcome(
            outcome="consensus", op_id="op-1", detail="x",
            agreement_count=3, distinct_count=1,
            total_rolls=3, failed_count=0,
            elapsed_seconds=0.1,
        )
        assert result is None

    def test_publisher_disabled_outcome_silenced(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
            publish_quorum_outcome,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_GENERATIVE_QUORUM_ENABLED": "true"},
        ):
            result = publish_quorum_outcome(
                outcome="disabled", op_id="op-1", detail="x",
                agreement_count=0, distinct_count=0,
                total_rolls=0, failed_count=0,
                elapsed_seconds=0.0,
            )
            # DISABLED outcomes are silent (zero noise when off)
            assert result is None

    def test_publisher_broker_missing_returns_none(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
            publish_quorum_outcome,
        )
        # Force ide_observability_stream import to fail
        with mock.patch.dict(
            os.environ,
            {"JARVIS_GENERATIVE_QUORUM_ENABLED": "true"},
        ):
            with mock.patch.dict(sys.modules):
                # Ensure clean state
                sys.modules.pop(
                    "backend.core.ouroboros.governance."
                    "ide_observability_stream", None,
                )
                # Sabotage import — raise on next import attempt
                with mock.patch(
                    "builtins.__import__",
                    side_effect=ImportError("simulated"),
                ):
                    result = publish_quorum_outcome(
                        outcome="consensus", op_id="op-1",
                        detail="x", agreement_count=3,
                        distinct_count=1, total_rolls=3,
                        failed_count=0, elapsed_seconds=0.1,
                    )
                    assert result is None

    def test_publisher_runner_fires_on_real_run(self):
        """End-to-end: master + gate on, run K=3 quorum,
        publisher attempts to fire (broker may be uninitialized
        in test env — the publish will return None silently but
        the run completes)."""
        async def gen(*, roll_id, seed):  # noqa: ARG001
            return "x = 1"

        from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
            run_quorum,
        )
        result = asyncio.run(run_quorum(
            gen, k=3, enabled_override=True, op_id="op-test",
        ))
        assert result.verdict.outcome.value == "consensus"


# ---------------------------------------------------------------------------
# 5. shipped_code_invariants — 5 Move 6 pins registered AND hold
# ---------------------------------------------------------------------------


MOVE_6_INVARIANT_NAMES = (
    "generative_quorum_no_authority_imports_primitive",
    "generative_quorum_runner_no_authority_imports",
    "ast_canonical_pure_stdlib",
    "quorum_gate_consumes_cost_gated_routes",
    "quorum_cap_structure_pinned",
)


class TestShippedCodeInvariants:
    def test_all_5_move_6_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            list_shipped_code_invariants,
        )
        invs = list_shipped_code_invariants()
        names = {i.invariant_name for i in invs}
        for name in MOVE_6_INVARIANT_NAMES:
            assert name in names, f"missing pin: {name}"

    @pytest.mark.parametrize("name", MOVE_6_INVARIANT_NAMES)
    def test_each_move_6_pin_holds(self, name):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            list_shipped_code_invariants, validate_invariant,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED": "true"},
        ):
            inv = next(
                i for i in list_shipped_code_invariants()
                if i.invariant_name == name
            )
            violations = validate_invariant(inv)
            assert violations == (), (
                f"pin {name} has {len(violations)} violations: "
                f"{[v.detail for v in violations]}"
            )

    def test_total_invariant_count_at_least_28(self):
        """Move 6 brings total to 28. If this fails because the
        count went UP that's fine — bump the threshold."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            list_shipped_code_invariants,
        )
        invs = list_shipped_code_invariants()
        assert len(invs) >= 28, (
            f"expected ≥28 invariants post Move 6, got {len(invs)}"
        )


# ---------------------------------------------------------------------------
# 6. FlagRegistry seeds — 6 Move 6 FlagSpec entries
# ---------------------------------------------------------------------------


MOVE_6_FLAG_NAMES = (
    "JARVIS_GENERATIVE_QUORUM_ENABLED",
    "JARVIS_QUORUM_GATE_ENABLED",
    "JARVIS_QUORUM_K",
    "JARVIS_QUORUM_AGREEMENT_THRESHOLD",
    "JARVIS_AST_CANONICAL_NORMALIZE_LITERALS",
    "JARVIS_AST_CANONICAL_STRIP_DOCSTRINGS",
)


class TestFlagRegistrySeeds:
    def test_all_6_move_6_flags_seeded(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        names = {spec.name for spec in SEED_SPECS}
        for flag in MOVE_6_FLAG_NAMES:
            assert flag in names, f"missing flag seed: {flag}"

    def test_master_flag_default_true_post_q4_graduation(self):
        """Q4 Priority #1 graduation (2026-05-02): operator
        authorized the flip after empirical verification that
        K× generation cost is bounded by sub-gate + risk-tier
        filter (APPROVAL_REQUIRED+) + COST_GATED_ROUTES exclusion
        of BACKGROUND/SPECULATIVE."""
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        master = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_GENERATIVE_QUORUM_ENABLED"
        )
        assert master.default is True, (
            "master flag default must be True post Q4 P#1 "
            "graduation — flip back to False is the operator's "
            "instant-rollback path"
        )

    def test_sub_gate_default_true_in_seed(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        gate = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_QUORUM_GATE_ENABLED"
        )
        assert gate.default is True

    def test_seed_install_doesnt_raise(self):
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            FlagRegistry,
        )
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            seed_default_registry,
        )
        reg = FlagRegistry()
        count = seed_default_registry(reg)
        assert count >= 6


# ---------------------------------------------------------------------------
# 7. Full-revert matrix — disabled state is fully reachable
# ---------------------------------------------------------------------------


class TestFullRevertMatrix:
    def test_master_off_invoke_returns_fall_through(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            QuorumActionMapping, invoke_quorum_for_op,
        )

        async def gen(*, roll_id, seed):  # noqa: ARG001
            return "x = 1"

        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=gen, k=3,
            master_override=False, gate_override=True,
        ))
        assert result.action is (
            QuorumActionMapping.FALL_THROUGH_SINGLE
        )
        assert result.run_result is None

    def test_gate_off_invoke_returns_fall_through(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            QuorumActionMapping, invoke_quorum_for_op,
        )

        async def gen(*, roll_id, seed):  # noqa: ARG001
            return "x = 1"

        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=gen, k=3,
            master_override=True, gate_override=False,
        ))
        assert result.action is (
            QuorumActionMapping.FALL_THROUGH_SINGLE
        )
        assert result.run_result is None

    def test_both_off_no_rolls_fired(self):
        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            invoke_quorum_for_op,
        )
        call_count = {"n": 0}

        async def counting_gen(*, roll_id, seed):  # noqa: ARG001
            call_count["n"] += 1
            return "x = 1"

        asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=counting_gen, k=3,
            master_override=False, gate_override=False,
        ))
        assert call_count["n"] == 0

    def test_disabled_state_no_sse_emission(self):
        """Master off → publisher returns None silently. No SSE
        traffic when feature is disabled."""
        from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
            publish_quorum_outcome,
        )
        os.environ.pop("JARVIS_GENERATIVE_QUORUM_ENABLED", None)
        for outcome in (
            "consensus", "majority_consensus", "disagreement",
            "disabled", "failed",
        ):
            r = publish_quorum_outcome(
                outcome=outcome, op_id="op-1", detail="x",
                agreement_count=0, distinct_count=0,
                total_rolls=0, failed_count=0,
                elapsed_seconds=0.0,
            )
            assert r is None


# ---------------------------------------------------------------------------
# 8. Authority invariants — final pass on Slice 5 surfaces
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / rel.replace("/", os.sep)
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariantsFinal:
    """Final pass — verify Slice 5 changes did not introduce
    new orchestrator-tier deps."""

    def test_runner_imports_only_allowed_governance(self):
        source = _read(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum_runner.py"
        )
        tree = ast.parse(source)
        allowed = {
            "backend.core.ouroboros.governance.verification.generative_quorum",
            "backend.core.ouroboros.governance.verification.ast_canonical",
            "backend.core.ouroboros.governance.ide_observability_stream",
            # Slice 5b C — lazy bounded-JSONL recorder import.
            # Read-only consumer of QuorumRunResult; never mutates
            # runner state. Authority floor preserved.
            "backend.core.ouroboros.governance.verification.generative_quorum_observer",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    assert node.module in allowed

    def test_gate_must_reference_cost_gated_routes(self):
        source = _read(
            "backend/core/ouroboros/governance/verification/"
            "generative_quorum_gate.py"
        )
        assert "COST_GATED_ROUTES" in source

    def test_ast_canonical_no_governance_imports(self):
        source = _read(
            "backend/core/ouroboros/governance/verification/"
            "ast_canonical.py"
        )
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "governance" not in module
                assert "backend." not in module

    def test_no_exec_eval_compile_anywhere_in_quorum_modules(self):
        for mod in (
            "verification/ast_canonical.py",
            "verification/generative_quorum.py",
            "verification/generative_quorum_runner.py",
            "verification/generative_quorum_gate.py",
        ):
            source = _read(
                f"backend/core/ouroboros/governance/{mod}"
            )
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        assert node.func.id not in (
                            "exec", "eval", "compile",
                        ), (
                            f"{mod} contains forbidden call: "
                            f"{node.func.id}"
                        )


# ---------------------------------------------------------------------------
# 9. End-to-end Move 6 mechanism — verify the kill-vectors hold
# ---------------------------------------------------------------------------


class TestEndToEndMove6Mechanism:
    def test_quine_class_invariance_via_full_path(self):
        """End-to-end: gate fires (override on), runner detects
        Quine-class literal invariance, action mapping returns
        PROCEED_WITH_CANDIDATE. Closes §28.5.2 v9 brutal review's
        Quine-class hallucination bypass vector."""
        counter = {"i": 0}
        diffs = [
            "def helper(x): return x * 2",
            "def helper(x): return x * 3",
            "def helper(x): return x * 5",
        ]

        async def quine_gen(*, roll_id, seed):  # noqa: ARG001
            i = counter["i"]
            counter["i"] += 1
            return diffs[i % 3]

        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            QuorumActionMapping, invoke_quorum_for_op,
        )
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=quine_gen, k=3,
            master_override=True, gate_override=True,
        ))
        # Three rolls converge on same AST signature →
        # PROCEED_WITH_CANDIDATE (the structural-equivalence
        # detection that kills Quine-class via probability)
        assert result.action is (
            QuorumActionMapping.PROCEED_WITH_CANDIDATE
        )

    def test_disagreement_routes_to_escalate(self):
        """End-to-end: three structurally distinct candidates →
        ESCALATE_BLOCKED (existing escalation path; no new
        surface)."""
        counter = {"i": 0}
        diffs = [
            "def f(x): return x + 1",
            "def f(x):\n    if x > 0:\n        return x\n    return 0",
            "class F: pass",
        ]

        async def divergent_gen(*, roll_id, seed):  # noqa: ARG001
            i = counter["i"]
            counter["i"] += 1
            return diffs[i % 3]

        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            QuorumActionMapping, invoke_quorum_for_op,
        )
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=divergent_gen, k=3,
            master_override=True, gate_override=True,
        ))
        assert result.action is (
            QuorumActionMapping.ESCALATE_BLOCKED
        )

    def test_cost_contract_preserved_bg_route(self):
        """End-to-end: BG route + master+gate on → cost_gated_route
        refusal, NO rolls fired. Cost contract structurally
        preserved (PRD §26.6 inheritance)."""
        call_count = {"n": 0}

        async def counting_gen(*, roll_id, seed):  # noqa: ARG001
            call_count["n"] += 1
            return "x = 1"

        from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
            QuorumActionMapping, invoke_quorum_for_op,
        )
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="background",
            generator=counting_gen, k=3,
            master_override=True, gate_override=True,
        ))
        assert call_count["n"] == 0
        assert result.action is (
            QuorumActionMapping.FALL_THROUGH_SINGLE
        )
        assert result.decision.reason == "cost_gated_route"
