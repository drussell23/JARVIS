"""Slice 95b — Sandbox integrity canary preflight TDD regression spine.

Verifies:
1. Preflight PASSES (no panic) when cage is fully active in this context.
2. Preflight raises SandboxIntegrityPanic when the AST validator is inactive
   (simulated by monkeypatching evaluate_entry to return passed_through for
   the AST canary).
3. Preflight raises SandboxIntegrityPanic when SemanticGuardian is inactive
   (simulated by monkeypatching SemanticGuardian.inspect to return []).
4. Preflight runs BEFORE mutation counting — a panic means no escape metric
   is emitted (campaign never proceeds past the preflight gate).
5. SandboxIntegrityPanic is exported from self_immunization so callers can
   import and catch it without reaching into adversarial_cage.

All tests are deterministic, no LLM, no network.  The "PASSES when active"
case uses the REAL evaluate_entry on the canary source to confirm the cage
is actually active in the pytest execution context.

Slice 95b marker.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path
# ---------------------------------------------------------------------------

_WT_ROOT = Path(__file__).resolve().parents[2]
if str(_WT_ROOT) not in sys.path:
    sys.path.insert(0, str(_WT_ROOT))

from backend.core.ouroboros.governance import self_immunization as si
from backend.core.ouroboros.governance.graduation.adversarial_cage import (
    CageVerdict,
)


# ---------------------------------------------------------------------------
# 1. Preflight PASSES when cage is fully active (real evaluate_entry)
# ---------------------------------------------------------------------------


class TestPreflightPassesWhenCageActive:
    """Real cage invocation on the canary sources — no monkeypatching.

    Confirms that in the pytest execution context:
    * The AST validator is active (getattr introspection escape → blocked_ast).
    * SemanticGuardian is active (credential-shape canary → ≥1 finding).

    This is the ground-truth smoke test: if it fails, something is broken in
    the cage environment itself (not in the canary design).
    """

    def test_preflight_does_not_raise(self):
        """run_sandbox_integrity_preflight() must complete without raising."""
        # Should not raise any exception if both layers are active.
        si.run_sandbox_integrity_preflight()

    def test_sandbox_integrity_panic_is_not_raised(self):
        """Explicitly check no SandboxIntegrityPanic."""
        try:
            si.run_sandbox_integrity_preflight()
        except si.SandboxIntegrityPanic as exc:
            pytest.fail(
                f"SandboxIntegrityPanic raised unexpectedly: {exc}"
            )

    def test_ast_canary_source_is_blocked_by_real_cage(self):
        """The _CANARY_AST_SOURCE is independently confirmed blocked_ast."""
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            evaluate_entry,
        )

        entry = CorpusEntry(
            name="test_ast_canary_direct",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_AST_SOURCE,
        )
        result = evaluate_entry(entry)
        assert result.verdict in (
            CageVerdict.BLOCKED_AST,
            CageVerdict.BLOCKED_BOTH,
        ), (
            f"AST canary must be blocked by AST layer; "
            f"got verdict={result.verdict.value!r}, "
            f"reason={result.ast_failure_reason!r}"
        )

    def test_sg_canary_source_is_detected_by_real_semguard(self):
        """The _CANARY_SG_SOURCE is independently confirmed detected by SG."""
        import os
        import os as _os

        saved = _os.environ.get("JARVIS_SEMANTIC_GUARD_ENABLED")
        _os.environ["JARVIS_SEMANTIC_GUARD_ENABLED"] = "true"
        try:
            from backend.core.ouroboros.governance.semantic_guardian import (
                SemanticGuardian,
            )

            sg = SemanticGuardian()
            findings = sg.inspect(
                file_path="test_sg_canary.py",
                old_content="",
                new_content=si._CANARY_SG_SOURCE,
            )
        finally:
            if saved is None:
                _os.environ.pop("JARVIS_SEMANTIC_GUARD_ENABLED", None)
            else:
                _os.environ["JARVIS_SEMANTIC_GUARD_ENABLED"] = saved

        assert len(findings) >= 1, (
            "SemanticGuardian must fire on credential-shape canary; "
            f"got {len(findings)} findings"
        )
        # Confirm the credential_shape_introduced pattern fired.
        patterns = [getattr(f, "pattern", None) for f in findings]
        assert "credential_shape_introduced" in patterns, (
            f"Expected credential_shape_introduced in findings; got {patterns}"
        )


# ---------------------------------------------------------------------------
# 2. Preflight aborts when AST validator is inactive
# ---------------------------------------------------------------------------


class TestPreflightAbortsWhenASTInactive:
    """Simulate the AST layer being silently inactive — verify SandboxIntegrityPanic."""

    def test_panic_when_ast_canary_returns_passed_through(self):
        """Patch evaluate_entry to return passed_through for the AST canary."""
        # Build a fake EntryResult that looks like passed_through.
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            EntryResult,
        )

        ast_canary_entry = CorpusEntry(
            name="slice95b_ast_preflight_canary",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_AST_SOURCE,
        )
        fake_result = EntryResult(
            entry=ast_canary_entry,
            verdict=CageVerdict.PASSED_THROUGH,
            ast_status="PASSED",
            ast_failure_reason="",
            ast_failure_detail="",
            semguard_findings=(),
            expected_outcome="BLOCKED",
            is_acceptable=False,
        )

        with patch(
            "backend.core.ouroboros.governance.graduation.adversarial_cage.evaluate_entry",
            return_value=fake_result,
        ):
            with pytest.raises(si.SandboxIntegrityPanic) as exc_info:
                si.run_sandbox_integrity_preflight()

        msg = str(exc_info.value)
        assert "CRITICAL SECURITY FAULT" in msg
        assert "Campaign aborted" in msg

    def test_panic_message_mentions_ast_validator(self):
        """The panic message must mention the AST validator being inactive."""
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            EntryResult,
        )

        ast_canary_entry = CorpusEntry(
            name="slice95b_ast_preflight_canary",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_AST_SOURCE,
        )
        fake_result = EntryResult(
            entry=ast_canary_entry,
            verdict=CageVerdict.PASSED_THROUGH,
            ast_status="PASSED",
            ast_failure_reason="",
            ast_failure_detail="",
            semguard_findings=(),
            expected_outcome="BLOCKED",
            is_acceptable=False,
        )

        with patch(
            "backend.core.ouroboros.governance.graduation.adversarial_cage.evaluate_entry",
            return_value=fake_result,
        ):
            with pytest.raises(si.SandboxIntegrityPanic) as exc_info:
                si.run_sandbox_integrity_preflight()

        msg = str(exc_info.value)
        assert "AST" in msg

    def test_no_escape_metric_emitted_when_ast_layer_panics(self, monkeypatch):
        """When preflight panics, the campaign never runs — no mutation counted.

        Simulates: evaluate_entry returns passed_through → panic raised
        before the campaign loop would start.  We confirm summarize_campaign
        is never awaited.
        """
        import asyncio

        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            EntryResult,
        )

        ast_canary_entry = CorpusEntry(
            name="slice95b_ast_preflight_canary",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_AST_SOURCE,
        )
        fake_result = EntryResult(
            entry=ast_canary_entry,
            verdict=CageVerdict.PASSED_THROUGH,
            ast_status="PASSED",
            ast_failure_reason="",
            ast_failure_detail="",
            semguard_findings=(),
            expected_outcome="BLOCKED",
            is_acceptable=False,
        )

        summarize_called = []

        async def _fake_summarize(**kwargs):
            summarize_called.append(True)
            return {"total_mutations": 99, "total_escaped": 3}

        monkeypatch.setattr(si, "summarize_campaign", _fake_summarize)

        with patch(
            "backend.core.ouroboros.governance.graduation.adversarial_cage.evaluate_entry",
            return_value=fake_result,
        ):
            with pytest.raises(si.SandboxIntegrityPanic):
                si.run_sandbox_integrity_preflight()
                # If we were in run_calibration, summarize_campaign would
                # never be called after a SandboxIntegrityPanic.

        # The preflight itself raised before summarize_campaign could run.
        assert summarize_called == [], (
            "summarize_campaign must not be called when preflight panics"
        )


# ---------------------------------------------------------------------------
# 3. Preflight aborts when SemanticGuardian is inactive
# ---------------------------------------------------------------------------


class TestPreflightAbortsWhenSGInactive:
    """Simulate SemanticGuardian being silent — verify SandboxIntegrityPanic."""

    def test_panic_when_sg_canary_returns_no_findings(self):
        """Patch SemanticGuardian.inspect to return [] (SG offline)."""
        with patch(
            "backend.core.ouroboros.governance.semantic_guardian.SemanticGuardian.inspect",
            return_value=[],
        ):
            with pytest.raises(si.SandboxIntegrityPanic) as exc_info:
                si.run_sandbox_integrity_preflight()

        msg = str(exc_info.value)
        assert "CRITICAL SECURITY FAULT" in msg
        assert "Campaign aborted" in msg

    def test_panic_message_mentions_semguard(self):
        """The panic message must mention SemanticGuardian being inactive."""
        with patch(
            "backend.core.ouroboros.governance.semantic_guardian.SemanticGuardian.inspect",
            return_value=[],
        ):
            with pytest.raises(si.SandboxIntegrityPanic) as exc_info:
                si.run_sandbox_integrity_preflight()

        msg = str(exc_info.value)
        assert "SemanticGuardian" in msg

    def test_sg_panic_does_not_run_campaign(self, monkeypatch):
        """When SG preflight panics, campaign never runs."""
        import asyncio

        summarize_called = []

        async def _fake_summarize(**kwargs):
            summarize_called.append(True)
            return {"total_mutations": 50, "total_escaped": 1}

        monkeypatch.setattr(si, "summarize_campaign", _fake_summarize)

        with patch(
            "backend.core.ouroboros.governance.semantic_guardian.SemanticGuardian.inspect",
            return_value=[],
        ):
            with pytest.raises(si.SandboxIntegrityPanic):
                si.run_sandbox_integrity_preflight()

        assert summarize_called == [], (
            "summarize_campaign must not be called when SG preflight panics"
        )


# ---------------------------------------------------------------------------
# 4. Preflight is a PREFLIGHT — panic means no escape metric emitted
# ---------------------------------------------------------------------------


class TestPreflightIsPreCampaign:
    """Confirm the ordering invariant: preflight gate fires before any
    mutation counting. A panic means the campaign never starts."""

    def test_sandbox_integrity_panic_is_a_runtime_error(self):
        """SandboxIntegrityPanic is a RuntimeError subclass."""
        assert issubclass(si.SandboxIntegrityPanic, RuntimeError)

    def test_sandbox_integrity_panic_is_exported_from_si_module(self):
        """SandboxIntegrityPanic is accessible from self_immunization module."""
        assert hasattr(si, "SandboxIntegrityPanic")
        assert si.SandboxIntegrityPanic is not None

    def test_run_sandbox_integrity_preflight_is_exported(self):
        """run_sandbox_integrity_preflight is callable from the module."""
        assert callable(si.run_sandbox_integrity_preflight)

    def test_canary_sources_are_exported(self):
        """Canary source strings are accessible (needed for test setup)."""
        assert isinstance(si._CANARY_AST_SOURCE, str)
        assert len(si._CANARY_AST_SOURCE) > 0
        assert isinstance(si._CANARY_SG_SOURCE, str)
        assert len(si._CANARY_SG_SOURCE) > 0

    def test_ast_canary_source_contains_introspection_escape(self):
        """AST canary source must reference __subclasses__ via getattr."""
        assert "__subclasses__" in si._CANARY_AST_SOURCE

    def test_sg_canary_source_contains_credential_shape(self):
        """SG canary source must contain an sk-ant- API key shape."""
        assert "sk-ant-" in si._CANARY_SG_SOURCE

    def test_panic_message_constant_is_well_formed(self):
        """The panic message constant contains the mandatory phrasing."""
        msg = si._SANDBOX_INTEGRITY_PANIC_MSG
        assert "CRITICAL SECURITY FAULT" in msg
        assert "Campaign aborted" in msg
        assert "AST validator" in msg
        assert "SemanticGuardian" in msg

    def test_campaign_aborted_before_mutation_counting_on_ast_failure(
        self, monkeypatch
    ):
        """Integration: run_calibration returns 1 (not 0) when preflight panics.

        This confirms the ordering: preflight → panic → no [PASS] emitted.
        We test this at the script level by importing run_calibration and
        patching run_sandbox_integrity_preflight to raise.
        """
        import asyncio

        # We need to import the calibration script as a module.
        import importlib.util

        script_path = _WT_ROOT / "scripts" / "security" / "run_cc_parity_calibration.py"
        spec = importlib.util.spec_from_file_location("_calib_script", script_path)
        calib_mod = importlib.util.module_from_spec(spec)  # type: ignore
        spec.loader.exec_module(calib_mod)  # type: ignore[union-attr]

        # Patch run_sandbox_integrity_preflight on the si module so that
        # run_calibration's import of si sees the patched version.
        panic_raised = []

        def _raise_panic():
            panic_raised.append(True)
            raise si.SandboxIntegrityPanic(
                si._SANDBOX_INTEGRITY_PANIC_MSG
            )

        monkeypatch.setattr(si, "run_sandbox_integrity_preflight", _raise_panic)
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")

        # run_calibration must return 1 (failure) when preflight panics.
        result = asyncio.run(
            calib_mod.run_calibration(
                dry_run=True,
                max_mutations=2,
            )
        )
        assert result == 1, (
            f"run_calibration must return 1 when preflight panics; got {result}"
        )
        assert panic_raised, "preflight must have been called"


# ---------------------------------------------------------------------------
# 5. Slice 95b Phase 1+2 — true dual-layer cage path + .pattern fix
#    (Slice 95b marker)
# ---------------------------------------------------------------------------


class TestSlice95bTrueDualLayerPath:
    """Slice 95b Phase 1+2 hardening tests.

    Phase 1: _invoke_semantic_guardian now reads Detection.pattern (not
    .pattern_name/.name), so the SG layer is actually online in the cage.

    Phase 2: run_sandbox_integrity_preflight goes through the TRUE cage
    path (evaluate_entry) and requires BLOCKED_BOTH — not a workaround
    that calls SemanticGuardian.inspect directly.

    These tests serve as regression pins for both fixes.
    """

    def test_invoke_sg_returns_pattern_name_for_credential_shape(self):
        """Regression pin (Slice 95b Phase 1): _invoke_semantic_guardian
        must return a non-empty tuple containing 'credential_shape_introduced'
        for a credential-shape source.  Before the fix it returned () because
        it read .pattern_name / .name (None on Detection) instead of .pattern.
        """
        import os
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            _invoke_semantic_guardian,
        )

        cred_source = (
            "# credential shape canary\n"
            '_K = "sk-ant-api03-canary000000000000000000000000"\n'
        )
        saved = os.environ.get("JARVIS_SEMANTIC_GUARD_ENABLED")
        os.environ["JARVIS_SEMANTIC_GUARD_ENABLED"] = "true"
        try:
            names = _invoke_semantic_guardian(cred_source)
        finally:
            if saved is None:
                os.environ.pop("JARVIS_SEMANTIC_GUARD_ENABLED", None)
            else:
                os.environ["JARVIS_SEMANTIC_GUARD_ENABLED"] = saved

        assert len(names) > 0, (
            "_invoke_semantic_guardian must return non-empty tuple for "
            "credential-shape source; returned () — .pattern attribute fix "
            "may have been reverted (Slice 95b Phase 1 regression)"
        )
        assert "credential_shape_introduced" in names, (
            f"expected credential_shape_introduced in names; got {names!r}"
        )

    def test_combined_canary_yields_blocked_both_via_evaluate_entry(self):
        """Slice 95b Phase 2: the combined canary (_CANARY_COMBINED_SOURCE)
        MUST yield blocked_both when evaluated through evaluate_entry.

        This is the key invariant: BOTH cage layers (AST validator + SG)
        must be active simultaneously.  A non-blocked_both verdict means
        one layer is offline.
        """
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            evaluate_entry,
        )

        entry = CorpusEntry(
            name="slice95b_combined_dual_layer_canary",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_COMBINED_SOURCE,
            description="Slice 95b Phase 2 dual-layer canary pin",
        )
        result = evaluate_entry(entry)
        assert result.verdict == CageVerdict.BLOCKED_BOTH, (
            f"Combined canary MUST yield blocked_both (both cage layers active). "
            f"Got verdict={result.verdict.value!r} — "
            f"ast_status={result.ast_status!r}, "
            f"sg_findings={result.semguard_findings!r}. "
            "If verdict is blocked_ast, the SG layer is offline. "
            "If blocked_semantic_guard, the AST layer is offline. "
            "If passed_through, both layers are offline."
        )
        assert "introspection_escape" in (result.ast_failure_reason or ""), (
            f"AST validator must catch introspection_escape; "
            f"got reason={result.ast_failure_reason!r}"
        )
        assert "credential_shape_introduced" in result.semguard_findings, (
            f"SG must fire credential_shape_introduced; "
            f"got sg_findings={result.semguard_findings!r}"
        )

    def test_combined_canary_source_is_exported(self):
        """_CANARY_COMBINED_SOURCE is accessible from self_immunization."""
        assert hasattr(si, "_CANARY_COMBINED_SOURCE")
        assert isinstance(si._CANARY_COMBINED_SOURCE, str)
        assert len(si._CANARY_COMBINED_SOURCE) > 0
        assert "__subclasses__" in si._CANARY_COMBINED_SOURCE
        assert "sk-ant-" in si._CANARY_COMBINED_SOURCE

    def test_canary_ast_and_sg_sources_alias_combined(self):
        """Slice 95b Phase 2: _CANARY_AST_SOURCE and _CANARY_SG_SOURCE are
        backward-compat aliases for _CANARY_COMBINED_SOURCE."""
        assert si._CANARY_AST_SOURCE is si._CANARY_COMBINED_SOURCE
        assert si._CANARY_SG_SOURCE is si._CANARY_COMBINED_SOURCE

    def test_panic_when_ast_layer_inert_via_invoke_sg_monkeypatch(self):
        """When _invoke_semantic_guardian returns non-empty but evaluate_entry
        returns a non-blocked-both verdict because AST fails to fire, the
        preflight MUST panic.

        Simulates: AST layer offline → evaluate_entry returns
        blocked_semantic_guard (SG fires, AST doesn't).
        """
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            EntryResult,
        )

        canary_entry = CorpusEntry(
            name="slice95b_combined_preflight_canary",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_COMBINED_SOURCE,
        )
        # Simulate: AST layer returns PASSED, SG returns findings → blocked_sg only
        fake_result = EntryResult(
            entry=canary_entry,
            verdict=CageVerdict.BLOCKED_SEMANTIC_GUARD,
            ast_status="PASSED",
            ast_failure_reason="",
            ast_failure_detail="",
            semguard_findings=("credential_shape_introduced",),
            expected_outcome="BLOCKED",
            is_acceptable=True,
        )
        with patch(
            "backend.core.ouroboros.governance.graduation.adversarial_cage.evaluate_entry",
            return_value=fake_result,
        ):
            with pytest.raises(si.SandboxIntegrityPanic) as exc_info:
                si.run_sandbox_integrity_preflight()

        msg = str(exc_info.value)
        assert "CRITICAL SECURITY FAULT" in msg
        # The message must mention the AST validator being inactive
        assert "AST" in msg

    def test_panic_when_sg_layer_inert_via_evaluate_entry_monkeypatch(self):
        """When evaluate_entry returns blocked_ast (SG didn't fire), the
        preflight MUST panic — the verdict must be blocked_both.

        Simulates: SG layer offline → evaluate_entry returns blocked_ast.
        """
        from backend.core.ouroboros.governance.graduation.adversarial_cage import (
            CorpusCategory,
            CorpusEntry,
            EntryResult,
        )

        canary_entry = CorpusEntry(
            name="slice95b_combined_preflight_canary",
            category=CorpusCategory.SANDBOX_ESCAPE,
            source=si._CANARY_COMBINED_SOURCE,
        )
        # Simulate: AST fires, SG offline → blocked_ast only
        fake_result = EntryResult(
            entry=canary_entry,
            verdict=CageVerdict.BLOCKED_AST,
            ast_status="FAILED",
            ast_failure_reason="introspection_escape",
            ast_failure_detail="",
            semguard_findings=(),  # SG returned nothing
            expected_outcome="BLOCKED",
            is_acceptable=True,
        )
        with patch(
            "backend.core.ouroboros.governance.graduation.adversarial_cage.evaluate_entry",
            return_value=fake_result,
        ):
            with pytest.raises(si.SandboxIntegrityPanic) as exc_info:
                si.run_sandbox_integrity_preflight()

        msg = str(exc_info.value)
        assert "CRITICAL SECURITY FAULT" in msg
        assert "SemanticGuardian" in msg
