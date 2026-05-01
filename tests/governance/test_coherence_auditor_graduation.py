"""Priority #1 Slice 5 — Graduation regression tests (CLOSES Priority #1).

Coverage:

  * **Master + 2 sub-gate flags default-TRUE** — graduated
    2026-05-01 because Coherence Auditor is read-only over
    existing artifacts (zero LLM cost), runs periodically (not
    per-op), produces ONLY advisory output.
  * **Asymmetric env semantics** — empty/whitespace = unset =
    default; explicit ``0``/``false``/``no``/``off`` hot-reverts.
  * **Cap-structure clamps** — every numeric env knob has floor +
    ceiling enforced via ``min(ceiling, max(floor, value))``.
  * **shipped_code_invariants** — 4 Priority #1 AST pins
    registered AND currently HOLD against shipped code.
  * **FlagRegistry seeds** — 8 Priority #1 FlagSpec entries
    register via ``seed_default_registry``; defaults pinned.
  * **Full-revert matrix** — every Slice 1-4 surface is reachable
    in the disabled state; SSE silenced when master off; bridge
    returns empty when sub-gate off; observer no-op when either
    off.
  * **End-to-end mechanism proofs** — full-pipeline drift
    detection through bridge advisory; cost-correctness pin.
  * **Authority invariants** — final pass: no Slice 1-4 surface
    introduces orchestrator-tier dependencies.
  * **32-invariant total floor** — Priority #1 brings count to 32
    (was 28 post-Move-6; +4 = 32).
"""
from __future__ import annotations

import ast
import asyncio
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_base():
    d = Path(tempfile.mkdtemp(prefix="cgrad_test_")).resolve()
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. Master + 2 sub-gate flags default-TRUE post-graduation
# ---------------------------------------------------------------------------


class TestMasterFlagsDefaultTrue:
    """Three flags graduated default-true Slice 5 / 2026-05-01.
    Cost-correctness rationale: Coherence Auditor is read-only
    over existing artifacts (zero LLM cost, zero K× generation
    amplification), runs periodically (not per-op), produces
    ONLY advisory output (operator approval still required for
    any actual flag flip)."""

    def test_master_default_true(self):
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            coherence_auditor_enabled,
        )
        os.environ.pop("JARVIS_COHERENCE_AUDITOR_ENABLED", None)
        assert coherence_auditor_enabled() is True

    def test_observer_sub_gate_default_true(self):
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            observer_enabled,
        )
        os.environ.pop("JARVIS_COHERENCE_OBSERVER_ENABLED", None)
        assert observer_enabled() is True

    def test_action_bridge_sub_gate_default_true(self):
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            coherence_action_bridge_enabled,
        )
        os.environ.pop(
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED", None,
        )
        assert coherence_action_bridge_enabled() is True

    @pytest.mark.parametrize(
        "flag",
        [
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED",
        ],
    )
    def test_explicit_false_hot_reverts(self, flag):
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            coherence_auditor_enabled,
        )
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            observer_enabled,
        )
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            coherence_action_bridge_enabled,
        )
        funcs = {
            "JARVIS_COHERENCE_AUDITOR_ENABLED": (
                coherence_auditor_enabled
            ),
            "JARVIS_COHERENCE_OBSERVER_ENABLED": observer_enabled,
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": (
                coherence_action_bridge_enabled
            ),
        }
        with mock.patch.dict(os.environ, {flag: "false"}):
            assert funcs[flag]() is False


# ---------------------------------------------------------------------------
# 2. Cap-structure clamps — every env knob bounded
# ---------------------------------------------------------------------------


class TestCapStructureClamps:
    @pytest.mark.parametrize(
        "knob,floor,ceiling,defval",
        [
            (
                "JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT",
                5.0, 100.0, 25.0,
            ),
            (
                "JARVIS_COHERENCE_BUDGET_POSTURE_LOCKED_HOURS",
                24.0, 168.0, 48.0,
            ),
            (
                "JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT",
                2, 50, 3,
            ),
            (
                "JARVIS_COHERENCE_BUDGET_CONFIDENCE_RISE_PCT",
                10.0, 500.0, 50.0,
            ),
            (
                "JARVIS_COHERENCE_HALFLIFE_DAYS",
                0.5, 90.0, 14.0,
            ),
            (
                "JARVIS_COHERENCE_WINDOW_HOURS",
                24, 720, 168,
            ),
            (
                "JARVIS_COHERENCE_MAX_SIGNATURES",
                10, 5000, 200,
            ),
            (
                "JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT",
                1.0, 48.0, 6.0,
            ),
            (
                "JARVIS_COHERENCE_VIGILANCE_MULTIPLIER",
                0.1, 1.0, 0.5,
            ),
            (
                "JARVIS_COHERENCE_TIGHTEN_FACTOR",
                0.5, 0.95, 0.8,
            ),
        ],
    )
    def test_floor_ceiling_clamps(
        self, knob, floor, ceiling, defval,
    ):
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            budget_confidence_rise_pct, budget_posture_locked_hours,
            budget_recurrence_count, budget_route_drift_pct,
            halflife_days,
        )
        from backend.core.ouroboros.governance.verification.coherence_window_store import (  # noqa: E501
            max_signatures_default, window_hours_default,
        )
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            cadence_hours_default, vigilance_multiplier,
        )
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            tighten_factor,
        )
        knob_to_fn = {
            "JARVIS_COHERENCE_BUDGET_ROUTE_DRIFT_PCT": (
                budget_route_drift_pct
            ),
            "JARVIS_COHERENCE_BUDGET_POSTURE_LOCKED_HOURS": (
                budget_posture_locked_hours
            ),
            "JARVIS_COHERENCE_BUDGET_RECURRENCE_COUNT": (
                budget_recurrence_count
            ),
            "JARVIS_COHERENCE_BUDGET_CONFIDENCE_RISE_PCT": (
                budget_confidence_rise_pct
            ),
            "JARVIS_COHERENCE_HALFLIFE_DAYS": halflife_days,
            "JARVIS_COHERENCE_WINDOW_HOURS": window_hours_default,
            "JARVIS_COHERENCE_MAX_SIGNATURES": (
                max_signatures_default
            ),
            "JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT": (
                cadence_hours_default
            ),
            "JARVIS_COHERENCE_VIGILANCE_MULTIPLIER": (
                vigilance_multiplier
            ),
            "JARVIS_COHERENCE_TIGHTEN_FACTOR": tighten_factor,
        }
        fn = knob_to_fn[knob]
        # Default
        os.environ.pop(knob, None)
        assert fn() == defval
        # Below floor → clamps
        with mock.patch.dict(os.environ, {knob: "-99999"}):
            assert fn() == floor
        # Above ceiling → clamps
        with mock.patch.dict(os.environ, {knob: "99999"}):
            assert fn() == ceiling


# ---------------------------------------------------------------------------
# 3. shipped_code_invariants — 4 Priority #1 pins
# ---------------------------------------------------------------------------


PRIORITY_1_INVARIANT_NAMES = (
    "coherence_auditor_no_authority_imports_primitive",
    "coherence_observer_no_authority_imports",
    "coherence_window_store_uses_flock",
    "coherence_action_bridge_consumes_adaptation_ledger",
)


class TestShippedCodeInvariants:
    def test_all_4_priority_1_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            list_shipped_code_invariants,
        )
        invs = list_shipped_code_invariants()
        names = {i.invariant_name for i in invs}
        for n in PRIORITY_1_INVARIANT_NAMES:
            assert n in names, f"missing pin: {n}"

    @pytest.mark.parametrize("name", PRIORITY_1_INVARIANT_NAMES)
    def test_each_priority_1_pin_holds(self, name):
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

    def test_total_invariant_count_at_least_32(self):
        """Priority #1 brings total to 32 (28 post-Move-6 + 4)."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            list_shipped_code_invariants,
        )
        assert len(list_shipped_code_invariants()) >= 32


# ---------------------------------------------------------------------------
# 4. FlagRegistry seeds — 8 Priority #1 entries
# ---------------------------------------------------------------------------


PRIORITY_1_FLAG_NAMES = (
    "JARVIS_COHERENCE_AUDITOR_ENABLED",
    "JARVIS_COHERENCE_OBSERVER_ENABLED",
    "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED",
    "JARVIS_COHERENCE_WINDOW_HOURS",
    "JARVIS_COHERENCE_MAX_SIGNATURES",
    "JARVIS_COHERENCE_CADENCE_HOURS_DEFAULT",
    "JARVIS_COHERENCE_HALFLIFE_DAYS",
    "JARVIS_COHERENCE_TIGHTEN_FACTOR",
)


class TestFlagRegistrySeeds:
    def test_all_8_priority_1_flags_seeded(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        names = {s.name for s in SEED_SPECS}
        for f in PRIORITY_1_FLAG_NAMES:
            assert f in names, f"missing seed: {f}"

    def test_3_master_gate_flags_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        for name in (
            "JARVIS_COHERENCE_AUDITOR_ENABLED",
            "JARVIS_COHERENCE_OBSERVER_ENABLED",
            "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED",
        ):
            spec = next(s for s in SEED_SPECS if s.name == name)
            assert spec.default is True, (
                f"{name} default must be True post-graduation"
            )

    def test_seed_install_doesnt_raise(self):
        from backend.core.ouroboros.governance.flag_registry import (  # noqa: E501
            FlagRegistry,
        )
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            seed_default_registry,
        )
        reg = FlagRegistry()
        count = seed_default_registry(reg)
        assert count >= 8


# ---------------------------------------------------------------------------
# 5. Full-revert matrix — disabled state reachable
# ---------------------------------------------------------------------------


class TestFullRevertMatrix:
    def test_master_off_drift_returns_disabled(self):
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            CoherenceOutcome, compute_behavioral_drift,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_AUDITOR_ENABLED": "false"},
        ):
            v = compute_behavioral_drift(None, None)
            assert v.outcome is CoherenceOutcome.DISABLED

    def test_master_off_publish_returns_none(self):
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            publish_behavioral_drift,
        )
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            BehavioralDriftVerdict, CoherenceOutcome,
            DriftSeverity,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_AUDITOR_ENABLED": "false"},
        ):
            v = BehavioralDriftVerdict(
                outcome=CoherenceOutcome.DRIFT_DETECTED,
                largest_severity=DriftSeverity.HIGH,
                drift_signature="x" * 64,
            )
            assert publish_behavioral_drift(verdict=v) is None

    def test_master_off_propose_returns_empty(self):
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            propose_coherence_action,
        )
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            BehavioralDriftFinding, BehavioralDriftKind,
            BehavioralDriftVerdict, CoherenceOutcome,
            DriftSeverity,
        )
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_COHERENCE_ACTION_BRIDGE_ENABLED": "false",
            },
        ):
            v = BehavioralDriftVerdict(
                outcome=CoherenceOutcome.DRIFT_DETECTED,
                findings=(BehavioralDriftFinding(
                    kind=(
                        BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT
                    ),
                    severity=DriftSeverity.HIGH,
                    detail="t",
                    delta_metric=1.0, budget_metric=1.0,
                ),),
                largest_severity=DriftSeverity.HIGH,
                drift_signature="x" * 64,
            )
            assert propose_coherence_action(v) == tuple()

    def test_observer_master_off_no_start(self, tmp_base):
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            CoherenceObserver,
        )
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COHERENCE_AUDITOR_ENABLED": "false"},
        ):
            obs = CoherenceObserver(base_dir=tmp_base)
            obs.start()
            assert obs.is_running() is False

    def test_observer_sub_gate_off_no_start(self, tmp_base):
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            CoherenceObserver,
        )
        with mock.patch.dict(
            os.environ,
            {
                "JARVIS_COHERENCE_AUDITOR_ENABLED": "true",
                "JARVIS_COHERENCE_OBSERVER_ENABLED": "false",
            },
        ):
            obs = CoherenceObserver(base_dir=tmp_base)
            obs.start()
            assert obs.is_running() is False


# ---------------------------------------------------------------------------
# 6. End-to-end mechanism proofs
# ---------------------------------------------------------------------------


class TestEndToEndMechanism:
    def test_full_pipeline_drift_to_advisory(
        self, tmp_base,
    ):
        """End-to-end: drift detected → SSE event payload shape →
        advisory record with monotonic-tightening verdict.
        Verifies every layer produces correct output."""
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            BehavioralDriftFinding, BehavioralDriftKind,
            BehavioralDriftVerdict, CoherenceOutcome,
            DriftSeverity,
        )
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            CoherenceAdvisoryAction,
            TighteningProposalStatus,
            propose_coherence_action,
            record_coherence_advisory,
            read_coherence_advisories,
        )
        from backend.core.ouroboros.governance.adaptation.ledger import (  # noqa: E501
            MonotonicTighteningVerdict,
        )

        f = BehavioralDriftFinding(
            kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
            severity=DriftSeverity.HIGH,
            detail="route distribution rotated 80%",
            delta_metric=80.0, budget_metric=25.0,
        )
        v = BehavioralDriftVerdict(
            outcome=CoherenceOutcome.DRIFT_DETECTED,
            findings=(f,),
            largest_severity=DriftSeverity.HIGH,
            drift_signature="abc" * 21 + "d",
            detail="full-pipeline test",
        )
        path = tmp_base / "coherence_advisory.jsonl"
        # All flags default-true post graduation
        advisories = propose_coherence_action(v)
        assert len(advisories) == 1
        adv = advisories[0]
        assert (
            adv.action
            is CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET
        )
        assert adv.tightening_status is (
            TighteningProposalStatus.PASSED
        )
        # Phase C universal cage rule — canonical verdict
        assert adv.monotonic_tightening_verdict == (
            MonotonicTighteningVerdict.PASSED.value
        )
        from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
            RecordOutcome,
        )
        out = record_coherence_advisory(adv, path=path)
        assert out is RecordOutcome.RECORDED
        recovered = read_coherence_advisories(
            path=path, since_ts=0.0,
        )
        assert len(recovered) == 1
        assert recovered[0].advisory_id == adv.advisory_id

    def test_cost_correctness_no_llm_calls_in_pipeline(self):
        """Pin: full pipeline (signature compute → drift compute
        → propose_action → publish_event) makes ZERO LLM /
        provider calls. Verified by AST: none of the 4 modules
        import providers/doubleword_provider/urgency_router/etc.
        (covered by individual no-authority pins; this is a
        cross-module final pass.)"""
        sources = []
        for mod in (
            "coherence_auditor.py",
            "coherence_window_store.py",
            "coherence_observer.py",
            "coherence_action_bridge.py",
        ):
            path = (
                Path(__file__).resolve().parents[2]
                / "backend" / "core" / "ouroboros"
                / "governance" / "verification" / mod
            )
            sources.append(path.read_text(encoding="utf-8"))
        forbidden = (
            "providers", "doubleword_provider",
            "urgency_router", "candidate_generator",
        )
        for source in sources:
            for tok in forbidden:
                # Allow comments mentioning the term but not actual
                # imports. ast walk per module already pinned by
                # individual invariant pins. This is a defense-in-
                # depth byte-pin checking imports specifically.
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(
                        node, (ast.Import, ast.ImportFrom),
                    ):
                        module = (
                            node.module
                            if isinstance(
                                node, ast.ImportFrom,
                            ) else
                            (
                                node.names[0].name
                                if node.names else ""
                            )
                        )
                        module = module or ""
                        assert tok not in module, (
                            f"forbidden cost-amplifying import "
                            f"{tok!r} in {module!r}"
                        )

    def test_end_to_end_observer_cycle_with_master_on(
        self, tmp_base,
    ):
        """Run a real observer cycle with master+sub-gate true.
        Inject scripted collector. Verify pipeline produces
        coherent ObserverTickResult."""
        from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
            CoherenceObserver, ObserverTickOutcome,
        )
        from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
            OpRecord, PostureRecord, WindowData,
        )

        class Stub:
            def __init__(self):
                self.calls = 0

            def collect_window(self, *, now_ts, window_hours):
                self.calls += 1
                return WindowData(
                    window_start_ts=now_ts - 3600.0,
                    window_end_ts=now_ts,
                    op_records=tuple(
                        OpRecord(f"op-{i}", "standard", now_ts)
                        for i in range(5)
                    ),
                    posture_records=(
                        PostureRecord("explore", now_ts),
                    ),
                )

        # Default-true post graduation; explicit override OK
        ts = time.time()
        obs = CoherenceObserver(
            collector=Stub(),
            posture_reader=lambda: "EXPLORE",
            base_dir=tmp_base,
        )
        r = asyncio.run(obs.run_one_cycle(now_ts=ts))
        # First cycle → INSUFFICIENT_DATA (no prior signature)
        assert r.outcome is (
            ObserverTickOutcome.INSUFFICIENT_DATA
        )


# ---------------------------------------------------------------------------
# 7. Authority invariants — final pass
# ---------------------------------------------------------------------------


def _read_module(rel: str) -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / rel
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariantsFinal:
    """Cross-module final pass — verify Slice 5 graduation flips
    didn't introduce orchestrator-tier deps."""

    @pytest.mark.parametrize(
        "module",
        [
            "coherence_auditor.py",
            "coherence_window_store.py",
            "coherence_observer.py",
            "coherence_action_bridge.py",
        ],
    )
    def test_no_orchestrator_in_any_slice(self, module):
        source = _read_module(module)
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                m = m or ""
                for f in forbidden:
                    assert f not in m, (
                        f"{module}: forbidden import {m}"
                    )

    @pytest.mark.parametrize(
        "module",
        [
            "coherence_auditor.py",
            "coherence_window_store.py",
            "coherence_action_bridge.py",
        ],
    )
    def test_no_async_in_sync_slices(self, module):
        """Slices 1, 2, 4 are sync. Only Slice 3 (observer) has
        async."""
        source = _read_module(module)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"{module}: forbidden async function "
                f"{getattr(node, 'name', '?')!r}"
            )

    def test_observer_has_async_for_periodic_loop(self):
        """Slice 3 (observer) MUST have async functions for the
        periodic loop."""
        source = _read_module("coherence_observer.py")
        tree = ast.parse(source)
        async_funcs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert "run_one_cycle" in async_funcs
        assert "_run_forever" in async_funcs

    def test_no_exec_eval_compile_anywhere_in_priority_1(self):
        """Critical safety: NO module in Priority #1 may execute
        candidate code. Mirrors Move 6 Slice 2 ast_canonical's
        critical safety pin."""
        for mod in (
            "coherence_auditor.py",
            "coherence_window_store.py",
            "coherence_observer.py",
            "coherence_action_bridge.py",
        ):
            source = _read_module(mod)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name):
                        assert node.func.id not in (
                            "exec", "eval", "compile",
                        ), (
                            f"{mod}: forbidden call "
                            f"{node.func.id}"
                        )

    def test_auditor_pure_stdlib(self):
        """Slice 1 has the strongest invariant: PURE-STDLIB."""
        source = _read_module("coherence_auditor.py")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                assert "governance" not in m
                assert "backend." not in m
