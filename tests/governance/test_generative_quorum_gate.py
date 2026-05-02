"""Move 6 Slice 4 — Risk-tier gate + orchestrator hook tests.

Coverage:

  * **Decision tree ordering** — master → sub-gate → input
    validation → cost-gated route → tier eligibility → ok.
  * **4-tier × 2-route gate matrix** — every combination of
    {SAFE_AUTO, NOTIFY_APPLY, APPROVAL_REQUIRED, BLOCKED} ×
    {STANDARD, BG, SPEC, IMMEDIATE, COMPLEX} produces the
    expected decision.
  * **Cost-gated route refusal** — BG/SPEC always refused at
    APPROVAL_REQUIRED+ tier (structural §26.6 guard via
    ``COST_GATED_ROUTES``).
  * **Master-off + gate-off short-circuits** — zero rolls fired.
  * **ConsensusOutcome → action mapping** — 5 outcomes × 5
    actions; closed taxonomy pin (any new ConsensusOutcome added
    without an action mapping → INVALID).
  * **Enum/string tolerance** — accepts both forms.
  * **invoke_quorum_for_op end-to-end** — gate → runner → action
    mapping; cancellation propagates; fall-through preserves
    backward compat.
  * **Defensive contract** — gate NEVER raises, even on garbage.
  * **Authority invariants** — AST-pinned: imports limited to
    Slice 1+2+3 + cost_contract_assertion; no orchestrator etc;
    MUST reference COST_GATED_ROUTES symbol; no exec/eval/
    compile.
  * **Schema integrity** — frozen dataclass, schema version
    stable, to_dict round-trip shape.
"""
from __future__ import annotations

import ast
import asyncio
import enum
import os
from pathlib import Path
from typing import Any, List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.cost_contract_assertion import (
    COST_GATED_ROUTES,
)
from backend.core.ouroboros.governance.verification.generative_quorum import (
    ConsensusOutcome,
    ConsensusVerdict,
)
from backend.core.ouroboros.governance.verification.generative_quorum_gate import (
    GENERATIVE_QUORUM_GATE_SCHEMA_VERSION,
    QUORUM_ELIGIBLE_TIERS,
    QuorumActionMapping,
    QuorumGateDecision,
    QuorumGateResult,
    RISK_TIER_APPROVAL_REQUIRED,
    RISK_TIER_BLOCKED,
    RISK_TIER_NOTIFY_APPLY,
    RISK_TIER_SAFE_AUTO,
    invoke_quorum_for_op,
    map_consensus_to_action,
    quorum_gate_enabled,
    should_invoke_quorum,
)


# ---------------------------------------------------------------------------
# Fixtures — generators + verdict factories
# ---------------------------------------------------------------------------


def make_static_gen(diff: str):
    async def gen(*, roll_id, seed):  # noqa: ARG001
        return diff
    return gen


def make_verdict(outcome: ConsensusOutcome) -> ConsensusVerdict:
    return ConsensusVerdict(
        outcome=outcome,
        agreement_count=3 if outcome is ConsensusOutcome.CONSENSUS else 2,
        distinct_count=1 if outcome is ConsensusOutcome.CONSENSUS else 2,
        total_rolls=3,
        canonical_signature=(
            "abc" if outcome in (
                ConsensusOutcome.CONSENSUS,
                ConsensusOutcome.MAJORITY_CONSENSUS,
            ) else None
        ),
        accepted_roll_id=(
            "roll-0" if outcome in (
                ConsensusOutcome.CONSENSUS,
                ConsensusOutcome.MAJORITY_CONSENSUS,
            ) else None
        ),
        detail="test",
    )


# ---------------------------------------------------------------------------
# 1. Sub-gate env knob — asymmetric semantics
# ---------------------------------------------------------------------------


class TestQuorumGateEnabledKnob:
    def test_default_is_true_post_graduation(self):
        """Slice 5 graduated 2026-05-01 — sub-gate now defaults
        true. Master flag (``JARVIS_GENERATIVE_QUORUM_ENABLED``)
        remains operator-controlled (default false)."""
        os.environ.pop("JARVIS_QUORUM_GATE_ENABLED", None)
        assert quorum_gate_enabled() is True

    def test_explicit_true(self):
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_GATE_ENABLED": "true"},
        ):
            assert quorum_gate_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy_variants(self, v):
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_GATE_ENABLED": v},
        ):
            assert quorum_gate_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off", "FALSE"])
    def test_falsy_variants(self, v):
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_GATE_ENABLED": v},
        ):
            assert quorum_gate_enabled() is False

    def test_whitespace_treated_as_unset(self):
        # Whitespace = unset = current default = True post Slice 5
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_GATE_ENABLED": "   "},
        ):
            assert quorum_gate_enabled() is True


# ---------------------------------------------------------------------------
# 2. Decision tree ordering — master → sub-gate → input → cost → tier
# ---------------------------------------------------------------------------


class TestDecisionTreeOrdering:
    def test_master_off_short_circuits_before_anything(self):
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="background",  # would also fail cost-gate
            master_override=False,
            gate_override=True,
        )
        # Master off wins — reason MUST be master_disabled, not
        # cost_gated_route or tier_below_threshold
        assert d.should_invoke is False
        assert d.reason == "master_disabled"

    def test_gate_off_after_master_check(self):
        d = should_invoke_quorum(
            risk_tier="safe_auto",  # would fail tier
            current_route="background",  # would fail cost
            master_override=True,
            gate_override=False,
        )
        assert d.reason == "gate_disabled"

    def test_cost_gated_route_refused_before_tier_check(self):
        # APPROVAL_REQUIRED would be eligible but BG route is
        # cost-gated; cost guard wins
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="background",
            master_override=True,
            gate_override=True,
        )
        assert d.reason == "cost_gated_route"

    def test_tier_below_threshold_with_clean_route(self):
        d = should_invoke_quorum(
            risk_tier="safe_auto",
            current_route="standard",
            master_override=True,
            gate_override=True,
        )
        assert d.reason == "tier_below_threshold"

    def test_invalid_input_empty_tier(self):
        d = should_invoke_quorum(
            risk_tier="",
            current_route="standard",
            master_override=True,
            gate_override=True,
        )
        assert d.reason == "invalid_input"

    def test_invalid_input_empty_route(self):
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="",
            master_override=True,
            gate_override=True,
        )
        assert d.reason == "invalid_input"

    def test_invalid_input_none_inputs(self):
        d = should_invoke_quorum(
            risk_tier=None,
            current_route=None,
            master_override=True,
            gate_override=True,
        )
        assert d.reason == "invalid_input"

    def test_ok_path_green(self):
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="standard",
            master_override=True,
            gate_override=True,
        )
        assert d.should_invoke is True
        assert d.reason == "ok"


# ---------------------------------------------------------------------------
# 3. 4-tier × N-route gate matrix
# ---------------------------------------------------------------------------


CLEAN_ROUTES = ["standard", "immediate", "complex"]
GATED_ROUTES = list(COST_GATED_ROUTES)
ALL_TIERS = [
    RISK_TIER_SAFE_AUTO,
    RISK_TIER_NOTIFY_APPLY,
    RISK_TIER_APPROVAL_REQUIRED,
    RISK_TIER_BLOCKED,
]


class TestGateMatrix:
    @pytest.mark.parametrize("tier", ALL_TIERS)
    @pytest.mark.parametrize("route", GATED_ROUTES)
    def test_every_tier_refused_on_gated_route(self, tier, route):
        d = should_invoke_quorum(
            risk_tier=tier,
            current_route=route,
            master_override=True,
            gate_override=True,
        )
        assert d.should_invoke is False
        assert d.reason == "cost_gated_route"

    @pytest.mark.parametrize(
        "tier",
        [RISK_TIER_SAFE_AUTO, RISK_TIER_NOTIFY_APPLY],
    )
    @pytest.mark.parametrize("route", CLEAN_ROUTES)
    def test_below_threshold_tiers_refused_on_clean_route(
        self, tier, route,
    ):
        d = should_invoke_quorum(
            risk_tier=tier,
            current_route=route,
            master_override=True,
            gate_override=True,
        )
        assert d.should_invoke is False
        assert d.reason == "tier_below_threshold"

    @pytest.mark.parametrize(
        "tier",
        [RISK_TIER_APPROVAL_REQUIRED, RISK_TIER_BLOCKED],
    )
    @pytest.mark.parametrize("route", CLEAN_ROUTES)
    def test_eligible_tiers_pass_on_clean_route(self, tier, route):
        d = should_invoke_quorum(
            risk_tier=tier,
            current_route=route,
            master_override=True,
            gate_override=True,
        )
        assert d.should_invoke is True
        assert d.reason == "ok"


# ---------------------------------------------------------------------------
# 4. Eligible-tiers constant pin
# ---------------------------------------------------------------------------


class TestEligibleTiersPin:
    def test_eligible_tiers_is_frozenset(self):
        assert isinstance(QUORUM_ELIGIBLE_TIERS, frozenset)

    def test_eligible_tiers_contains_approval_required(self):
        assert (
            RISK_TIER_APPROVAL_REQUIRED in QUORUM_ELIGIBLE_TIERS
        )

    def test_eligible_tiers_contains_blocked(self):
        assert RISK_TIER_BLOCKED in QUORUM_ELIGIBLE_TIERS

    def test_eligible_tiers_excludes_safe_auto(self):
        assert (
            RISK_TIER_SAFE_AUTO not in QUORUM_ELIGIBLE_TIERS
        )

    def test_eligible_tiers_excludes_notify_apply(self):
        assert (
            RISK_TIER_NOTIFY_APPLY not in QUORUM_ELIGIBLE_TIERS
        )

    def test_eligible_tiers_size(self):
        # If this changes, the action-mapping consequences need
        # revisiting (i.e., what does NOTIFY_APPLY-and-quorum-fired
        # look like?)
        assert len(QUORUM_ELIGIBLE_TIERS) == 2


# ---------------------------------------------------------------------------
# 5. Enum / string tolerance
# ---------------------------------------------------------------------------


class TestEnumStringTolerance:
    def test_string_lowercase(self):
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="standard",
            master_override=True, gate_override=True,
        )
        assert d.should_invoke is True

    def test_string_uppercase(self):
        d = should_invoke_quorum(
            risk_tier="APPROVAL_REQUIRED",
            current_route="STANDARD",
            master_override=True, gate_override=True,
        )
        assert d.should_invoke is True

    def test_string_with_whitespace(self):
        d = should_invoke_quorum(
            risk_tier="  approval_required  ",
            current_route="\tstandard\n",
            master_override=True, gate_override=True,
        )
        assert d.should_invoke is True

    def test_enum_with_name(self):
        class FakeTier(enum.Enum):
            APPROVAL_REQUIRED = enum.auto()
            SAFE_AUTO = enum.auto()

        d = should_invoke_quorum(
            risk_tier=FakeTier.APPROVAL_REQUIRED,
            current_route="standard",
            master_override=True, gate_override=True,
        )
        assert d.should_invoke is True
        assert d.risk_tier == "approval_required"

    def test_enum_with_string_value_via_name(self):
        # _normalize_tier prefers .name (which is the RiskTier
        # convention) — so the enum member name MUST normalize
        # to a canonical lowercase tier string.
        class StrEnum(str, enum.Enum):
            APPROVAL_REQUIRED = "approval_required"

        d = should_invoke_quorum(
            risk_tier=StrEnum.APPROVAL_REQUIRED,
            current_route="standard",
            master_override=True, gate_override=True,
        )
        assert d.should_invoke is True


# ---------------------------------------------------------------------------
# 6. ConsensusOutcome → action mapping
# ---------------------------------------------------------------------------


class TestActionMapping:
    @pytest.mark.parametrize(
        "outcome,expected",
        [
            (
                ConsensusOutcome.CONSENSUS,
                QuorumActionMapping.PROCEED_WITH_CANDIDATE,
            ),
            (
                ConsensusOutcome.MAJORITY_CONSENSUS,
                QuorumActionMapping.PROCEED_NOTIFY_APPLY,
            ),
            (
                ConsensusOutcome.DISAGREEMENT,
                QuorumActionMapping.ESCALATE_BLOCKED,
            ),
            (
                ConsensusOutcome.DISABLED,
                QuorumActionMapping.FALL_THROUGH_SINGLE,
            ),
            (
                ConsensusOutcome.FAILED,
                QuorumActionMapping.FALL_THROUGH_SINGLE,
            ),
        ],
    )
    def test_mapping_pinned(self, outcome, expected):
        verdict = make_verdict(outcome)
        assert map_consensus_to_action(verdict) is expected

    def test_garbage_input_yields_invalid(self):
        assert (
            map_consensus_to_action(None)  # type: ignore[arg-type]
            is QuorumActionMapping.INVALID
        )
        assert (
            map_consensus_to_action("nonsense")  # type: ignore[arg-type]
            is QuorumActionMapping.INVALID
        )

    def test_action_mapping_5_value_closed_taxonomy(self):
        """If a new value is added to ConsensusOutcome without
        updating the mapping, this pin alerts. Currently exhaustive."""
        all_outcomes = list(ConsensusOutcome)
        all_actions = {
            map_consensus_to_action(make_verdict(o))
            for o in all_outcomes
        }
        # All 5 outcomes mapped to non-INVALID actions
        assert QuorumActionMapping.INVALID not in all_actions

    def test_quorum_action_mapping_5_value_closed(self):
        """The mapping enum itself is a closed 5-value taxonomy."""
        assert len(list(QuorumActionMapping)) == 5


# ---------------------------------------------------------------------------
# 7. invoke_quorum_for_op — end-to-end
# ---------------------------------------------------------------------------


class TestInvokeEndToEnd:
    def test_e2e_consensus_returns_proceed(self):
        gen = make_static_gen("def helper(x): return x * 2")
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=gen, k=3,
            master_override=True, gate_override=True,
        ))
        assert result.action is (
            QuorumActionMapping.PROCEED_WITH_CANDIDATE
        )
        assert result.decision.should_invoke is True
        assert result.decision.reason == "ok"
        assert result.run_result is not None
        assert result.run_result.verdict.total_rolls == 3

    def test_e2e_disagreement_routes_to_escalate(self):
        counter = {"i": 0}
        diffs = [
            "def f(x): return x",
            "def f(x): return -x",
            "class F: pass",
        ]

        async def divergent_gen(*, roll_id, seed):  # noqa: ARG001
            i = counter["i"]
            counter["i"] += 1
            return diffs[i % 3]

        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=divergent_gen, k=3,
            master_override=True, gate_override=True,
        ))
        assert result.action is (
            QuorumActionMapping.ESCALATE_BLOCKED
        )

    def test_e2e_gated_path_no_run_result(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="background",  # cost-gated
            generator=gen, k=3,
            master_override=True, gate_override=True,
        ))
        assert result.action is (
            QuorumActionMapping.FALL_THROUGH_SINGLE
        )
        assert result.run_result is None
        assert result.decision.reason == "cost_gated_route"

    def test_e2e_master_off_no_run_result(self):
        gen = make_static_gen("x = 1")
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
        assert result.decision.reason == "master_disabled"

    def test_e2e_majority_routes_to_notify_apply(self):
        counter = {"i": 0}
        diffs = [
            "def f(x): return x",
            "def f(x): return x",
            "class F: pass",
        ]

        async def maj_gen(*, roll_id, seed):  # noqa: ARG001
            i = counter["i"]
            counter["i"] += 1
            return diffs[i % 3]

        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=maj_gen, k=3,
            master_override=True, gate_override=True,
        ))
        assert result.action is (
            QuorumActionMapping.PROCEED_NOTIFY_APPLY
        )

    def test_e2e_external_cancellation_propagates(self):
        async def slow_gen(*, roll_id, seed):  # noqa: ARG001
            await asyncio.sleep(10.0)
            return "x = 1"

        async def driver():
            task = asyncio.create_task(invoke_quorum_for_op(
                risk_tier="approval_required",
                current_route="standard",
                generator=slow_gen, k=2,
                master_override=True, gate_override=True,
            ))
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(driver())

    def test_e2e_no_rolls_fired_on_gated_path(self):
        """Cost-correctness: when gate refuses, generator is
        NEVER called (zero K× cost when gated)."""
        call_count = {"n": 0}

        async def counting_gen(*, roll_id, seed):  # noqa: ARG001
            call_count["n"] += 1
            return "x = 1"

        asyncio.run(invoke_quorum_for_op(
            risk_tier="safe_auto",  # below threshold
            current_route="standard",
            generator=counting_gen, k=5,
            master_override=True, gate_override=True,
        ))
        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# 8. Defensive contract — gate never raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_should_invoke_with_garbage_inputs(self):
        d = should_invoke_quorum(
            risk_tier=12345,  # type: ignore[arg-type]
            current_route=[1, 2, 3],  # type: ignore[arg-type]
            master_override=True, gate_override=True,
        )
        assert isinstance(d, QuorumGateDecision)

    def test_invoke_with_garbage_generator(self):
        async def garbage_gen(*, roll_id, seed):  # noqa: ARG001
            return None

        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=garbage_gen, k=2,  # type: ignore[arg-type]
            master_override=True, gate_override=True,
        ))
        # Runner produces empty signatures → DISAGREEMENT →
        # ESCALATE_BLOCKED. But never raises.
        assert isinstance(result, QuorumGateResult)


# ---------------------------------------------------------------------------
# 9. Schema integrity
# ---------------------------------------------------------------------------


class TestSchema:
    def test_decision_is_frozen(self):
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="standard",
            master_override=True, gate_override=True,
        )
        with pytest.raises((AttributeError, Exception)):
            d.reason = "hax"  # type: ignore[misc]

    def test_decision_to_dict_round_trip_shape(self):
        d = should_invoke_quorum(
            risk_tier="approval_required",
            current_route="standard",
            master_override=True, gate_override=True,
        )
        out = d.to_dict()
        assert out["should_invoke"] is True
        assert out["reason"] == "ok"
        assert out["risk_tier"] == "approval_required"
        assert out["current_route"] == "standard"
        assert (
            out["schema_version"]
            == GENERATIVE_QUORUM_GATE_SCHEMA_VERSION
        )

    def test_result_to_dict_with_run_result(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="standard",
            generator=gen, k=2,
            master_override=True, gate_override=True,
        ))
        out = result.to_dict()
        assert "decision" in out
        assert "action" in out
        assert "run_result" in out
        assert out["run_result"] is not None

    def test_result_to_dict_without_run_result(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(invoke_quorum_for_op(
            risk_tier="approval_required",
            current_route="background",
            generator=gen, k=2,
            master_override=True, gate_override=True,
        ))
        out = result.to_dict()
        assert out["run_result"] is None

    def test_schema_version_stable(self):
        assert (
            GENERATIVE_QUORUM_GATE_SCHEMA_VERSION
            == "generative_quorum_gate.1"
        )


# ---------------------------------------------------------------------------
# 10. Authority invariants — AST-pinned import discipline
# ---------------------------------------------------------------------------


def _gate_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "verification"
        / "generative_quorum_gate.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def gate_source(self) -> str:
        return _gate_source()

    def test_no_orchestrator_imports(self, gate_source):
        forbidden_modules = [
            "orchestrator",
            "iron_gate",
            "policy",
            "change_engine",
            "candidate_generator",
            "providers",
            "doubleword_provider",
            "urgency_router",
            "auto_action_router",
            "subagent_scheduler",
            "tool_executor",
            "phase_runners",
            "semantic_guardian",
            "semantic_firewall",
            "risk_engine",
        ]
        tree = ast.parse(gate_source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                module = module or ""
                for f in forbidden_modules:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: {module}"
                    )

    def test_governance_imports_in_allowlist(self, gate_source):
        """Slice 4 may import ONLY:
          * Slice 1 (generative_quorum)
          * Slice 3 (generative_quorum_runner) — transitively
            pulls Slice 2 (ast_canonical)
          * cost_contract_assertion (for COST_GATED_ROUTES)"""
        tree = ast.parse(gate_source)
        allowed = {
            "backend.core.ouroboros.governance.cost_contract_assertion",
            "backend.core.ouroboros.governance.verification.ast_canonical",
            "backend.core.ouroboros.governance.verification.generative_quorum",
            "backend.core.ouroboros.governance.verification.generative_quorum_runner",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    assert node.module in allowed, (
                        f"governance import outside allowlist: "
                        f"{node.module}"
                    )

    def test_must_reference_cost_gated_routes(self, gate_source):
        """STRUCTURAL cost-contract guard: gate MUST reference the
        COST_GATED_ROUTES symbol from cost_contract_assertion. Any
        refactor that drops this reference is caught structurally
        BEFORE shipping. This is the load-bearing pin from the
        Move 6 scope: 'gate consumes COST_GATED_ROUTES from
        cost_contract_assertion — refactor that bypasses cost
        guard caught structurally.'"""
        assert "COST_GATED_ROUTES" in gate_source, (
            "gate dropped its reference to COST_GATED_ROUTES — "
            "the structural §26.6 cost-contract guard is gone"
        )

    def test_no_mutation_tools(self, gate_source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "os.remove", "os.unlink",
            "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in gate_source, (
                f"gate contains forbidden mutation token: {f!r}"
            )

    def test_no_exec_eval_compile(self, gate_source):
        tree = ast.parse(gate_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    ), (
                        f"gate contains forbidden call: "
                        f"{node.func.id}"
                    )

    def test_invoke_for_op_is_async(self, gate_source):
        tree = ast.parse(gate_source)
        async_funcs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert "invoke_quorum_for_op" in async_funcs

    def test_public_api_exported(self, gate_source):
        for name in (
            "should_invoke_quorum", "invoke_quorum_for_op",
            "map_consensus_to_action", "QuorumGateDecision",
            "QuorumGateResult", "QuorumActionMapping",
            "QUORUM_ELIGIBLE_TIERS",
            "GENERATIVE_QUORUM_GATE_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in gate_source

    def test_eligible_tiers_string_constants_match_module(
        self, gate_source,
    ):
        # The 4 RISK_TIER_* string constants must remain
        # lowercase — orchestrator may rely on this canonical
        # form when comparing.
        assert RISK_TIER_SAFE_AUTO == "safe_auto"
        assert RISK_TIER_NOTIFY_APPLY == "notify_apply"
        assert RISK_TIER_APPROVAL_REQUIRED == "approval_required"
        assert RISK_TIER_BLOCKED == "blocked"
