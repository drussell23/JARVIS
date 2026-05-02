"""PlanFalsificationDetector Slice 5 — graduation regression spine.

Pins the graduation invariants:

  * Master flag default flipped false -> true
  * Each module owns its register_flags + register_shipped_invariants
  * FlagRegistry discovery seeds all 8 PlanFalsification flags
  * shipped_code_invariants discovery seeds all 4 AST pins; each
    pin passes against the live source
  * SSE event constant + publish helper exist
  * Bridge fires the publish helper on every bridge_to_replan call
    (even silent paths) so observability sees full lifecycle
  * End-to-end: master default-on + bridge default-on causes a
    plan with a missing-file step to drive REPLAN_TRIGGERED with
    the structural feedback block in the orchestrator's reactive
    replan path
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib

import pytest

from backend.core.ouroboros.governance.plan_falsification import (
    FalsificationOutcome,
    plan_falsification_enabled,
)
from backend.core.ouroboros.governance.plan_falsification_detector import (
    detect_falsification,
    filesystem_probe_enabled,
)
from backend.core.ouroboros.governance.plan_falsification_orchestrator_bridge import (
    bridge_enabled,
    bridge_to_replan,
    prompt_inject_enabled,
)
from backend.core.ouroboros.governance.plan_generator import (
    _plan_hypothesis_emit_enabled,
)


# ---------------------------------------------------------------------------
# Master flag default flipped (the headline graduation move)
# ---------------------------------------------------------------------------


class TestGraduatedDefaults:
    def test_master_flag_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", raising=False,
        )
        assert plan_falsification_enabled() is True

    def test_filesystem_probe_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED", raising=False,
        )
        assert filesystem_probe_enabled() is True

    def test_emit_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", raising=False,
        )
        assert _plan_hypothesis_emit_enabled() is True

    def test_bridge_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", raising=False,
        )
        assert bridge_enabled() is True

    def test_inject_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED",
            raising=False,
        )
        assert prompt_inject_enabled() is True


# ---------------------------------------------------------------------------
# Module-owned register_flags + register_shipped_invariants exist
# ---------------------------------------------------------------------------


_MODULES_WITH_FLAGS = (
    "backend.core.ouroboros.governance.plan_falsification",
    "backend.core.ouroboros.governance.plan_falsification_detector",
    "backend.core.ouroboros.governance.plan_generator",
    "backend.core.ouroboros.governance.plan_falsification_orchestrator_bridge",
)

_MODULES_WITH_INVARIANTS = (
    "backend.core.ouroboros.governance.plan_falsification",
    "backend.core.ouroboros.governance.plan_falsification_detector",
    "backend.core.ouroboros.governance.plan_falsification_orchestrator_bridge",
)


class TestModuleOwnedRegistration:
    @pytest.mark.parametrize("modname", _MODULES_WITH_FLAGS)
    def test_register_flags_callable(self, modname):
        mod = importlib.import_module(modname)
        fn = getattr(mod, "register_flags", None)
        assert callable(fn), (
            f"{modname} missing module-owned register_flags"
        )

    @pytest.mark.parametrize("modname", _MODULES_WITH_INVARIANTS)
    def test_register_shipped_invariants_callable(self, modname):
        mod = importlib.import_module(modname)
        fn = getattr(mod, "register_shipped_invariants", None)
        assert callable(fn), (
            f"{modname} missing register_shipped_invariants"
        )


# ---------------------------------------------------------------------------
# FlagRegistry seeding — every flag actually registers
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeding:
    def _empty_registry(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )
        return FlagRegistry()

    def _all_flag_names(self) -> set:
        registry = self._empty_registry()
        for modname in _MODULES_WITH_FLAGS:
            mod = importlib.import_module(modname)
            mod.register_flags(registry)
        return {spec.name for spec in registry.list_all()}

    def test_all_8_flags_seeded(self):
        names = self._all_flag_names()
        expected = {
            # Slice 1 (3)
            "JARVIS_PLAN_FALSIFICATION_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE",
            "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S",
            # Slice 2 (1)
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
            # Slice 3 (1)
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED",
            # Slice 4 (3)
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS",
        }
        missing = expected - names
        assert not missing, f"missing seeds: {sorted(missing)}"

    def test_master_flag_seeded_with_default_true(self):
        registry = self._empty_registry()
        from backend.core.ouroboros.governance import (
            plan_falsification as mod,
        )
        mod.register_flags(registry)
        spec = next(
            s for s in registry.list_all()
            if s.name == "JARVIS_PLAN_FALSIFICATION_ENABLED"
        )
        assert spec.default is True


# ---------------------------------------------------------------------------
# shipped_code_invariants — pins discoverable + pass on live source
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    @pytest.mark.parametrize("modname", _MODULES_WITH_INVARIANTS)
    def test_invariants_returned_as_list(self, modname):
        mod = importlib.import_module(modname)
        invariants = mod.register_shipped_invariants()
        assert isinstance(invariants, list)
        assert len(invariants) >= 1

    def test_total_pin_count_meets_target(self):
        total = 0
        for modname in _MODULES_WITH_INVARIANTS:
            mod = importlib.import_module(modname)
            total += len(mod.register_shipped_invariants())
        # Slice 1: 2 (pure-stdlib + 5-value taxonomies)
        # Slice 2: 1 (authority + async layout)
        # Slice 4: 1 (authority + ASCII-only render)
        # = 4 pins minimum
        assert total >= 4

    def test_each_pin_passes_against_live_source(self):
        """Every pin's validate() returns no violations against
        its own target source. This is the load-bearing test —
        ensures the AST contracts the pin checks for actually
        match what's shipped."""
        import ast as _ast

        repo_root = pathlib.Path(__file__).resolve().parents[2]
        for modname in _MODULES_WITH_INVARIANTS:
            mod = importlib.import_module(modname)
            for inv in mod.register_shipped_invariants():
                target_path = repo_root / inv.target_file
                source = target_path.read_text()
                tree = _ast.parse(source)
                violations = inv.validate(tree, source)
                assert violations == (), (
                    f"{inv.invariant_name!r} flagged violations: "
                    f"{violations}"
                )


# ---------------------------------------------------------------------------
# SSE event surface
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_constant_defined(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PLAN_FALSIFICATION_VERDICT,
        )
        assert (
            EVENT_TYPE_PLAN_FALSIFICATION_VERDICT
            == "plan_falsification_verdict"
        )

    def test_publish_helper_exists(self):
        from backend.core.ouroboros.governance import (
            ide_observability_stream as mod,
        )
        assert hasattr(mod, "publish_plan_falsification_verdict")
        assert callable(mod.publish_plan_falsification_verdict)

    def test_publish_helper_returns_none_when_stream_disabled(
        self, monkeypatch,
    ):
        # Force stream-disabled state.
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_plan_falsification_verdict,
        )
        out = publish_plan_falsification_verdict(
            op_id="op-1",
            outcome="replan_triggered",
            falsified_step_index=0,
        )
        assert out is None  # stream off → silent return

    def test_publish_helper_never_raises_on_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_plan_falsification_verdict,
        )
        # All defaults - smoke
        publish_plan_falsification_verdict(
            op_id="x", outcome="disabled",
        )
        # With None step_index
        publish_plan_falsification_verdict(
            op_id="x", outcome="failed", falsified_step_index=None,
        )


# ---------------------------------------------------------------------------
# Bridge fires SSE on every call (full lifecycle observability)
# ---------------------------------------------------------------------------


class TestBridgeSSEPublish:
    @pytest.mark.asyncio
    async def test_bridge_calls_publish_helper(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        # Track publish invocations.
        calls = []

        def _spy(**kwargs):
            calls.append(kwargs)
            return "evt-123"

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_plan_falsification_verdict",
            _spy,
        )
        plan = (
            '{"ordered_changes":[{"file_path":"missing.py",'
            '"change_type":"modify"}]}'
        )
        verdict, _ = await bridge_to_replan(
            plan_json=plan,
            project_root=repo,
            op_id="op-test-1",
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert len(calls) == 1
        assert calls[0]["op_id"] == "op-test-1"
        assert calls[0]["outcome"] == "replan_triggered"
        assert calls[0]["prompt_injected"] is True

    @pytest.mark.asyncio
    async def test_bridge_publishes_silent_paths_too(
        self, tmp_path, monkeypatch,
    ):
        """Even when no falsification, the publish helper still
        fires so operators see full lifecycle."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "auth.py").write_text("x")
        calls = []
        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_plan_falsification_verdict",
            lambda **kw: calls.append(kw),
        )
        plan = (
            '{"ordered_changes":[{"file_path":"auth.py",'
            '"change_type":"modify"}]}'
        )
        await bridge_to_replan(
            plan_json=plan,
            project_root=repo,
            op_id="op-quiet",
        )
        assert len(calls) == 1
        assert calls[0]["outcome"] == "insufficient_evidence"
        assert calls[0]["prompt_injected"] is False

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_break_bridge(
        self, tmp_path, monkeypatch,
    ):
        """A raising publish helper must not cascade into the
        bridge return — observability is best-effort."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def _explode(**_):
            raise RuntimeError("publish blew up")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream."
            "publish_plan_falsification_verdict",
            _explode,
        )
        verdict, text = await bridge_to_replan(
            plan_json='{"ordered_changes":[{"file_path":"missing.py",'
                      '"change_type":"modify"}]}',
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert text


# ---------------------------------------------------------------------------
# End-to-end at graduated defaults: nothing else needed for it to fire
# ---------------------------------------------------------------------------


class TestGraduatedEndToEnd:
    @pytest.mark.asyncio
    async def test_default_env_drives_replan_e2e(
        self, tmp_path, monkeypatch,
    ):
        """At graduated defaults (no env vars set), a plan with a
        missing-file step should drive REPLAN_TRIGGERED with the
        structural feedback block — proving the master + bridge +
        inject defaults compose correctly."""
        for var in (
            "JARVIS_PLAN_FALSIFICATION_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED",
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)

        repo = tmp_path / "repo"
        repo.mkdir()
        plan = (
            '{"schema_version":"plan.1","ordered_changes":['
            '{"file_path":"missing.py","change_type":"modify",'
            '"expected_outcome":"missing.py exports foo()"}'
            ']}'
        )
        verdict, text = await bridge_to_replan(
            plan_json=plan,
            project_root=repo,
            op_id="grad-e2e",
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert text  # prompt injection happened
        assert "missing.py" in text
        assert "missing.py exports foo()" in text

    @pytest.mark.asyncio
    async def test_master_off_overrides_graduation(
        self, tmp_path, monkeypatch,
    ):
        """Operator escape hatch — explicit master=false overrides
        the graduated default."""
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "false",
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        verdict, text = await bridge_to_replan(
            plan_json='{"ordered_changes":[{"file_path":"missing.py",'
                      '"change_type":"modify"}]}',
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.DISABLED
        assert text == ""

    @pytest.mark.asyncio
    async def test_detector_default_on_via_env_unset(
        self, monkeypatch, tmp_path,
    ):
        """Detector itself (Slice 2) returns non-DISABLED outcomes
        with no env vars set."""
        for var in (
            "JARVIS_PLAN_FALSIFICATION_ENABLED",
            "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
        ):
            monkeypatch.delenv(var, raising=False)
        from backend.core.ouroboros.governance.plan_falsification import (
            PlanStepHypothesis,
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        verdict = await detect_falsification(
            (PlanStepHypothesis(
                step_index=0, file_path="missing.py",
                change_type="modify",
            ),),
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED


# ---------------------------------------------------------------------------
# Cancellation still propagates after graduation (regression guard)
# ---------------------------------------------------------------------------


class TestPostGraduationContracts:
    @pytest.mark.asyncio
    async def test_cancellation_still_propagates(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()

        async def _cancel(*_a, **_kw):
            raise asyncio.CancelledError()

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "plan_falsification_orchestrator_bridge.detect_falsification",
            _cancel,
        )
        with pytest.raises(asyncio.CancelledError):
            await bridge_to_replan(
                plan_json='{"ordered_changes":[{"file_path":"x.py"}]}',
                project_root=repo,
            )

    @pytest.mark.asyncio
    async def test_legacy_dynamic_replanner_still_present(self):
        """Slice 4 wire-up keeps the legacy backstop. Verify the
        import still exists in the orchestrator."""
        orch_path = (
            pathlib.Path(__file__).resolve().parents[2]
            / "backend"
            / "core"
            / "ouroboros"
            / "governance"
            / "orchestrator.py"
        )
        src = orch_path.read_text()
        assert "DynamicRePlanner" in src
        assert "DynamicRePlanner.suggest_replan" in src
