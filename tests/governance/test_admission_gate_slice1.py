"""AdmissionGate Slice 1 — pure-stdlib primitive regression suite.

Pins the substrate Slice 2 will wire into
``CandidateGenerator._call_fallback`` and Slice 3 will graduate.

Strict directives validated:

  * No hardcoding: every threshold has env-knob accessor with
    floor/ceiling clamps. Tests cover defaults + clamps + garbage.
  * NEVER raises: adversarial-input matrix collapses every failure
    mode to a closed enum (FAILED for shape-broken; DISABLED for
    master-off).
  * Pure-stdlib: AST pin asserts NO backend.* imports + NO asyncio
    import + no candidate_generator/providers coupling.
  * Total decision function: no raise statements in body (other
    than dataclass FrozenInstanceError which Python raises on
    attribute writes — not raised by our code).
  * Caller-agnostic: substrate stays free of caller-side imports
    so the dependency direction is one-way.

Covers:

  §A   Schema version + closed taxonomy invariants
  §B   Env-knob defaults + floor/ceiling clamps + garbage handling
  §C   Frozen dataclass to_dict / from_dict round-trips
  §D   Decision matrix — DISABLED branch (master flag off)
  §E   Decision matrix — None ctx → FAILED
  §F   Decision matrix — shape-broken ctx (NaN, negative) → FAILED
  §G   Decision matrix — queue depth at hard cap → SHED_QUEUE_DEEP
  §H   Decision matrix — budget insufficient → SHED_BUDGET_INSUFFICIENT
  §I   Decision matrix — happy path → ADMIT
  §J   Math: required_budget_s = projected_wait × safety_factor + min_viable
  §K   Determinism: same inputs → same record (modulo ts)
  §L   _PROCEED_OUTCOMES set membership invariant
  §M   _SHED_OUTCOMES set membership invariant
  §N   proceeds() / is_shed() helper consistency
  §O   AST authority pins: no asyncio, no candidate_generator,
       no providers, no orchestrator, no backend.* import
  §P   Total contract: garbage env-knob overrides handled
"""
from __future__ import annotations

import ast
import inspect
import json
from typing import Any
from unittest import mock

import pytest

from backend.core.ouroboros.governance.admission_gate import (
    ADMISSION_GATE_SCHEMA_VERSION,
    AdmissionContext,
    AdmissionDecision,
    AdmissionRecord,
    _PROCEED_OUTCOMES,
    _SHED_OUTCOMES,
    admission_gate_enabled,
    budget_safety_factor,
    compute_admission_decision,
    min_viable_call_s,
    queue_depth_hard_cap,
)


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_ADMISSION_GATE_ENABLED",
        "JARVIS_ADMISSION_MIN_VIABLE_CALL_S",
        "JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR",
        "JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _ctx(
    *,
    route: str = "immediate",
    remaining_s: float = 120.0,
    queue_depth: int = 0,
    projected_wait_s: float = 30.0,
    op_id: str = "op-test",
) -> AdmissionContext:
    return AdmissionContext(
        route=route,
        remaining_s=remaining_s,
        queue_depth=queue_depth,
        projected_wait_s=projected_wait_s,
        op_id=op_id,
    )


# ---------------------------------------------------------------------------
# §A — Schema + closed taxonomy
# ---------------------------------------------------------------------------


class TestSchemaAndTaxonomy:
    def test_schema_version_pin(self):
        assert ADMISSION_GATE_SCHEMA_VERSION == "admission_gate.v1"

    def test_decision_has_five_values(self):
        assert len(list(AdmissionDecision)) == 5

    def test_decision_vocabulary_frozen(self):
        # Pin the literal vocabulary against silent additions.
        # Slice 3 promotes this to a shipped_code_invariant pin.
        expected = {
            "admit",
            "shed_budget_insufficient",
            "shed_queue_deep",
            "disabled",
            "failed",
        }
        assert {d.value for d in AdmissionDecision} == expected


# ---------------------------------------------------------------------------
# §B — Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_master_default_true_post_graduation(self):
        # Graduated 2026-05-02 (Slice 3): default-True.
        assert admission_gate_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
    def test_master_truthy_enables(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", val,
        )
        assert admission_gate_enabled() is True

    @pytest.mark.parametrize(
        "val", ["0", "false", "no", "off", "garbage"],
    )
    def test_master_falsy_disabled(self, monkeypatch, val):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_GATE_ENABLED", val,
        )
        assert admission_gate_enabled() is False

    def test_min_viable_call_default(self):
        assert min_viable_call_s() == 25.0

    def test_min_viable_call_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "0.5",
        )
        assert min_viable_call_s() == 10.0  # floor
        monkeypatch.setenv(
            "JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "9999",
        )
        assert min_viable_call_s() == 60.0  # ceiling

    def test_min_viable_call_garbage(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_MIN_VIABLE_CALL_S", "abc",
        )
        assert min_viable_call_s() == 25.0  # fallback

    def test_budget_safety_factor_default(self):
        assert budget_safety_factor() == 1.2

    def test_budget_safety_factor_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR", "0.1",
        )
        assert budget_safety_factor() == 1.0  # floor
        monkeypatch.setenv(
            "JARVIS_ADMISSION_BUDGET_SAFETY_FACTOR", "99",
        )
        assert budget_safety_factor() == 3.0  # ceiling

    def test_queue_depth_hard_cap_default(self):
        assert queue_depth_hard_cap() == 16

    def test_queue_depth_hard_cap_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP", "0",
        )
        assert queue_depth_hard_cap() == 1  # floor
        monkeypatch.setenv(
            "JARVIS_ADMISSION_QUEUE_DEPTH_HARD_CAP", "999",
        )
        assert queue_depth_hard_cap() == 128  # ceiling


# ---------------------------------------------------------------------------
# §C — Round-trips
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_context_to_dict_shape(self):
        ctx = _ctx(op_id="op-abc-123")
        d = ctx.to_dict()
        assert d["route"] == "immediate"
        assert d["remaining_s"] == 120.0
        assert d["queue_depth"] == 0
        assert d["projected_wait_s"] == 30.0
        assert d["op_id"] == "op-abc-123"
        assert d["schema_version"] == ADMISSION_GATE_SCHEMA_VERSION

    def test_record_round_trip(self):
        rec = AdmissionRecord(
            decision=AdmissionDecision.ADMIT,
            reason="admitted",
            route="standard",
            remaining_s=90.0,
            queue_depth=2,
            projected_wait_s=15.0,
            required_budget_s=43.0,
            op_id="op-rt",
            decided_at_ts=1234.0,
        )
        d = rec.to_dict()
        # Must be JSON-serializable for SSE/observability surfaces.
        assert isinstance(json.dumps(d), str)
        rec2 = AdmissionRecord.from_dict(d)
        assert rec2 is not None
        assert rec2.decision is AdmissionDecision.ADMIT
        assert rec2.reason == "admitted"
        assert rec2.route == "standard"
        assert rec2.remaining_s == 90.0
        assert rec2.queue_depth == 2
        assert rec2.projected_wait_s == 15.0
        assert rec2.required_budget_s == 43.0
        assert rec2.op_id == "op-rt"
        assert rec2.decided_at_ts == 1234.0

    def test_record_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong.v9", "decision": "admit"}
        assert AdmissionRecord.from_dict(d) is None

    def test_record_malformed_returns_none(self):
        assert AdmissionRecord.from_dict("not a dict") is None
        assert AdmissionRecord.from_dict({}) is None
        assert (
            AdmissionRecord.from_dict({
                "schema_version": ADMISSION_GATE_SCHEMA_VERSION,
                "decision": "not_a_real_decision",
            })
            is None
        )


# ---------------------------------------------------------------------------
# §D – §I — Decision matrix
# ---------------------------------------------------------------------------


class TestDecisionMatrix:
    def test_disabled_branch(self):
        rec = compute_admission_decision(
            _ctx(),
            enabled=False,
        )
        assert rec.decision is AdmissionDecision.DISABLED
        assert rec.reason == "gate_disabled"
        # Caller treats DISABLED as proceed (preserves
        # pre-Slice-1 behavior).
        assert rec.proceeds() is True
        assert rec.is_shed() is False

    def test_disabled_with_none_ctx_still_safe(self):
        # Disabled path runs even when ctx is None — surfaces a
        # record with empty fields rather than crashing.
        rec = compute_admission_decision(
            None,
            enabled=False,
        )
        assert rec.decision is AdmissionDecision.DISABLED
        assert rec.proceeds() is True

    def test_none_ctx_yields_failed(self):
        rec = compute_admission_decision(
            None,
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.FAILED
        assert rec.reason == "ctx_is_none"
        # FAILED is fail-open — caller treats as proceed.
        assert rec.proceeds() is True
        assert rec.is_shed() is False

    @pytest.mark.parametrize("bad_remaining", [-1.0, float("nan")])
    def test_invalid_remaining_yields_failed(
        self, bad_remaining,
    ):
        rec = compute_admission_decision(
            _ctx(remaining_s=bad_remaining),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.FAILED
        assert "ctx_field" in rec.reason
        assert rec.proceeds() is True

    @pytest.mark.parametrize("bad_wait", [-1.0, float("nan")])
    def test_invalid_projected_wait_yields_failed(
        self, bad_wait,
    ):
        rec = compute_admission_decision(
            _ctx(projected_wait_s=bad_wait),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.FAILED

    def test_negative_queue_depth_yields_failed(self):
        rec = compute_admission_decision(
            _ctx(queue_depth=-1),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.FAILED

    def test_queue_depth_at_hard_cap_sheds(self):
        # depth = 16 (default cap) → SHED_QUEUE_DEEP
        rec = compute_admission_decision(
            _ctx(queue_depth=16, remaining_s=999.0),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.SHED_QUEUE_DEEP
        assert rec.is_shed() is True
        assert rec.proceeds() is False
        assert "queue_depth_at_hard_cap" in rec.reason

    def test_queue_depth_above_hard_cap_sheds(self):
        rec = compute_admission_decision(
            _ctx(queue_depth=20, remaining_s=999.0),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.SHED_QUEUE_DEEP

    def test_queue_depth_just_under_cap_admits(self):
        # depth = 15 (one under default cap of 16) → ADMIT
        rec = compute_admission_decision(
            _ctx(
                queue_depth=15,
                remaining_s=999.0,
                projected_wait_s=10.0,
            ),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.ADMIT

    def test_budget_insufficient_sheds(self):
        # The bt-2026-05-02-234923 reproduction:
        # remaining=120, projected_wait=146 → required_budget =
        # 146*1.2 + 25 = 200.2 > 120 → SHED.
        rec = compute_admission_decision(
            _ctx(remaining_s=120.0, projected_wait_s=146.0),
            enabled=True,
        )
        assert rec.decision is (
            AdmissionDecision.SHED_BUDGET_INSUFFICIENT
        )
        assert "budget_below_required" in rec.reason
        # required = 146*1.2 + 25 = 200.2
        assert rec.required_budget_s == pytest.approx(200.2, rel=0.01)
        assert rec.is_shed() is True
        assert rec.proceeds() is False

    def test_budget_just_sufficient_admits(self):
        # remaining=120, projected_wait=30 → required = 30*1.2+25
        # = 61. 120 > 61 → ADMIT.
        rec = compute_admission_decision(
            _ctx(remaining_s=120.0, projected_wait_s=30.0),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.ADMIT
        assert rec.required_budget_s == pytest.approx(61.0)

    def test_zero_projected_wait_admits(self):
        # remaining=120, projected_wait=0 → required = 0+25 = 25.
        # 120 > 25 → ADMIT.
        rec = compute_admission_decision(
            _ctx(remaining_s=120.0, projected_wait_s=0.0),
            enabled=True,
        )
        assert rec.decision is AdmissionDecision.ADMIT

    def test_remaining_below_min_viable_alone_sheds(self):
        # remaining=20, projected_wait=0 → required = 25.
        # 20 < 25 → SHED.
        rec = compute_admission_decision(
            _ctx(remaining_s=20.0, projected_wait_s=0.0),
            enabled=True,
        )
        assert rec.decision is (
            AdmissionDecision.SHED_BUDGET_INSUFFICIENT
        )


# ---------------------------------------------------------------------------
# §J — Required-budget math is auditable
# ---------------------------------------------------------------------------


class TestBudgetMath:
    def test_required_budget_formula(self):
        rec = compute_admission_decision(
            _ctx(
                remaining_s=999.0,
                projected_wait_s=50.0,
            ),
            enabled=True,
            min_viable_call_s_value=30.0,
            budget_safety_factor_value=1.5,
        )
        # required = 50 * 1.5 + 30 = 105
        assert rec.required_budget_s == 105.0
        assert rec.decision is AdmissionDecision.ADMIT

    def test_required_budget_surfaced_in_shed_record(self):
        # Even when SHED, required_budget_s is in the record so
        # operators can audit the math.
        rec = compute_admission_decision(
            _ctx(remaining_s=10.0, projected_wait_s=100.0),
            enabled=True,
            min_viable_call_s_value=20.0,
            budget_safety_factor_value=1.0,
        )
        # required = 100 * 1.0 + 20 = 120
        assert rec.required_budget_s == 120.0
        assert rec.remaining_s == 10.0
        # And the reason string is auditable.
        assert "100.00" in rec.reason or "100.0" in rec.reason


# ---------------------------------------------------------------------------
# §K — Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_record(self):
        ctx = _ctx(remaining_s=100.0, projected_wait_s=20.0)
        r1 = compute_admission_decision(
            ctx, enabled=True, decided_at_ts=42.0,
        )
        r2 = compute_admission_decision(
            ctx, enabled=True, decided_at_ts=42.0,
        )
        # Records are equal (frozen dataclass equality).
        assert r1 == r2

    def test_decided_at_ts_propagated_to_record(self):
        rec = compute_admission_decision(
            _ctx(),
            enabled=True,
            decided_at_ts=999.5,
        )
        assert rec.decided_at_ts == 999.5

    def test_substrate_does_not_read_clock(self):
        # The function should NOT call time.time() / time.monotonic()
        # internally — decided_at_ts is caller-supplied so the
        # function stays bit-deterministic for testing.
        from backend.core.ouroboros.governance import admission_gate
        src = inspect.getsource(admission_gate)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                # time.time / time.monotonic / datetime.now etc.
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "time"
                    and node.attr in ("time", "monotonic")
                ):
                    pytest.fail(
                        f"forbidden time.{node.attr}() at line "
                        f"{node.lineno} — substrate must stay "
                        "clock-free"
                    )


# ---------------------------------------------------------------------------
# §L – §N — Set membership invariants + helpers
# ---------------------------------------------------------------------------


class TestSetInvariants:
    def test_proceed_outcomes_set(self):
        expected = {
            AdmissionDecision.ADMIT,
            AdmissionDecision.DISABLED,
            AdmissionDecision.FAILED,
        }
        assert _PROCEED_OUTCOMES == expected

    def test_shed_outcomes_set(self):
        expected = {
            AdmissionDecision.SHED_BUDGET_INSUFFICIENT,
            AdmissionDecision.SHED_QUEUE_DEEP,
        }
        assert _SHED_OUTCOMES == expected

    def test_proceed_and_shed_partition_all_outcomes(self):
        # Every AdmissionDecision belongs to EXACTLY ONE set —
        # the partition is total and disjoint. Slice 3 AST-pins
        # this invariant.
        all_outcomes = set(AdmissionDecision)
        assert _PROCEED_OUTCOMES | _SHED_OUTCOMES == all_outcomes
        assert _PROCEED_OUTCOMES & _SHED_OUTCOMES == set()

    def test_proceeds_is_shed_inverse(self):
        for d in AdmissionDecision:
            rec = AdmissionRecord(
                decision=d, reason="x", route="r",
                remaining_s=0.0, queue_depth=0,
                projected_wait_s=0.0, required_budget_s=0.0,
            )
            # Exactly one of proceeds()/is_shed() is True
            # (proceeds includes DISABLED+FAILED+ADMIT;
            # is_shed includes only the two SHED_* outcomes).
            assert rec.proceeds() != rec.is_shed()


# ---------------------------------------------------------------------------
# §O — AST authority pins
# ---------------------------------------------------------------------------


class TestAuthorityPins:
    @staticmethod
    def _module_imports():
        from backend.core.ouroboros.governance import admission_gate
        src = inspect.getsource(admission_gate)
        tree = ast.parse(src)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.append(alias.name)
        return out

    def test_no_asyncio_import(self):
        imports = self._module_imports()
        for imp in imports:
            assert "asyncio" not in imp.split("."), (
                f"forbidden asyncio import: {imp}"
            )

    def test_no_caller_module_imports(self):
        # Substrate must stay caller-agnostic: no
        # candidate_generator / providers / orchestrator imports.
        imports = self._module_imports()
        forbidden = {
            "candidate_generator", "providers",
            "orchestrator", "urgency_router",
            "iron_gate", "risk_tier", "change_engine",
            "gate", "yaml_writer", "policy",
        }
        for imp in imports:
            for f in forbidden:
                assert f not in imp.split("."), (
                    f"forbidden caller-side import: {imp}"
                )

    def test_no_backend_module_imports(self):
        # PURE-stdlib substrate at Slice 1. Slice 3 graduation
        # introduced two registration-contract imports
        # (FlagRegistry + shipped_code_invariants) — both are
        # outbound description channels (substrate publishes
        # metadata, not consumes behavior). Everything else MUST
        # remain stdlib so the substrate stays caller-agnostic.
        ALLOWED_BACKEND_IMPORTS = {
            "backend.core.ouroboros.governance.flag_registry",
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",
        }
        imports = self._module_imports()
        for imp in imports:
            if imp.startswith("backend."):
                assert imp in ALLOWED_BACKEND_IMPORTS, (
                    f"non-stdlib import: {imp}"
                )

    def test_no_exec_eval_compile(self):
        from backend.core.ouroboros.governance import admission_gate
        src = inspect.getsource(admission_gate)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in (
                    "exec", "eval", "compile",
                ):
                    pytest.fail(
                        f"forbidden {func.id} call at line "
                        f"{node.lineno}"
                    )


# ---------------------------------------------------------------------------
# §P — Total contract: garbage env-knob overrides
# ---------------------------------------------------------------------------


class TestTotalContract:
    def test_garbage_min_viable_collapses_to_default(self):
        rec = compute_admission_decision(
            _ctx(remaining_s=999.0, projected_wait_s=10.0),
            enabled=True,
            min_viable_call_s_value="not_a_number",  # type: ignore[arg-type]
        )
        # Should still admit (defaults applied).
        assert rec.decision is AdmissionDecision.ADMIT

    def test_garbage_safety_factor_collapses_to_default(self):
        rec = compute_admission_decision(
            _ctx(remaining_s=999.0, projected_wait_s=10.0),
            enabled=True,
            budget_safety_factor_value=None,
        )
        assert rec.decision is AdmissionDecision.ADMIT

    def test_negative_min_viable_collapses_to_default(self):
        rec = compute_admission_decision(
            _ctx(remaining_s=999.0, projected_wait_s=10.0),
            enabled=True,
            min_viable_call_s_value=-50.0,
        )
        # Negative coerces to 25.0 default.
        assert rec.decision is AdmissionDecision.ADMIT
        # required = 10*1.2 + 25 = 37
        assert rec.required_budget_s == pytest.approx(37.0)

    def test_safety_factor_below_one_collapses_to_default(self):
        rec = compute_admission_decision(
            _ctx(remaining_s=999.0, projected_wait_s=10.0),
            enabled=True,
            budget_safety_factor_value=0.1,
        )
        # Below 1.0 floor → snaps to 1.2 default.
        assert rec.required_budget_s == pytest.approx(37.0)

    def test_garbage_queue_cap_collapses_to_default(self):
        # Garbage cap → coerces to 16 default. depth=15 admits;
        # depth=16 sheds.
        rec_admit = compute_admission_decision(
            _ctx(queue_depth=15, remaining_s=999.0),
            enabled=True,
            queue_depth_hard_cap_value="abc",  # type: ignore[arg-type]
        )
        assert rec_admit.decision is AdmissionDecision.ADMIT
        rec_shed = compute_admission_decision(
            _ctx(queue_depth=16, remaining_s=999.0),
            enabled=True,
            queue_depth_hard_cap_value="abc",  # type: ignore[arg-type]
        )
        assert rec_shed.decision is AdmissionDecision.SHED_QUEUE_DEEP

    def test_function_never_raises_on_adversarial(self):
        # Adversarial matrix — every combination should produce
        # a closed-enum AdmissionDecision, never raise.
        for ctx in [None, _ctx(), _ctx(remaining_s=-1.0)]:
            for enabled in [True, False]:
                for mv in [None, -10, "x", 0]:
                    for sf in [None, -1, "y", 0]:
                        for cap in [None, -5, "z"]:
                            rec = compute_admission_decision(
                                ctx,
                                enabled=enabled,
                                min_viable_call_s_value=mv,  # type: ignore[arg-type]
                                budget_safety_factor_value=sf,  # type: ignore[arg-type]
                                queue_depth_hard_cap_value=cap,  # type: ignore[arg-type]
                            )
                            assert isinstance(
                                rec.decision, AdmissionDecision,
                            )
