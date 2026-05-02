"""Move 5 Slice 4 — PROBE_ENVIRONMENT executor regression spine.

Coverage tracks:

  * 4th enum value PROBE_ENVIRONMENT pinned + closed-taxonomy
    refresh
  * ConfidenceMonitor.reset_window() — clears rolling deque +
    returns dropped count + master-flag respected
  * execute_probe_environment full decision tree (every
    ConvergenceVerdict outcome → expected ConfidenceCollapseAction):
      - CONVERGED → RETRY_WITH_FEEDBACK + monitor.reset_window()
        + feedback contains canonical_answer
      - DIVERGED → ESCALATE_TO_OPERATOR
      - EXHAUSTED → INCONCLUSIVE + budget reduction
      - DISABLED → RETRY_WITH_FEEDBACK (safe legacy)
      - FAILED → INCONCLUSIVE
  * Master-flag-off short-circuit (no probe runs)
  * Backward-compat: existing 3 ConfidenceCollapseAction values
    unchanged + value strings stable
  * Authority invariants — AST-pinned
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.verification import (
    probe_environment_executor as exec_mod,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (  # noqa: E501
    ConfidenceMonitor,
)
from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    ConvergenceVerdict,
    ProbeOutcome,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
    AmbiguityContext,
)
from backend.core.ouroboros.governance.verification.hypothesis_consumers import (  # noqa: E501
    ConfidenceCollapseAction,
)
from backend.core.ouroboros.governance.verification.probe_environment_executor import (  # noqa: E501
    PROBE_ENVIRONMENT_EXECUTOR_SCHEMA_VERSION,
    execute_probe_environment,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
    ReadonlyEvidenceProber,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _master_flags_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_CONFIDENCE_MONITOR_ENABLED", "true",
    )
    yield


def _ambiguity() -> AmbiguityContext:
    return AmbiguityContext(
        op_id="op-test",
        target_symbol="foo",
        target_file="bar.py",
    )


class _ConvergingBackend:
    def __init__(self, answer: str = "foo is a function") -> None:
        self.calls: List[str] = []
        self._answer = answer

    def execute(self, *, tool_name: str, args: Dict[str, Any]) -> str:
        self.calls.append(tool_name)
        return self._answer


class _DivergingBackend:
    def __init__(self) -> None:
        self.n = 0

    def execute(self, *, tool_name: str, args: Dict[str, Any]) -> str:
        self.n += 1
        return f"distinct-{self.n}"


def _make_monitor() -> ConfidenceMonitor:
    return ConfidenceMonitor(
        provider="dw", model_id="test", window_size=8,
    )


# ---------------------------------------------------------------------------
# 1. ConfidenceCollapseAction — 4th value + backward-compat
# ---------------------------------------------------------------------------


class TestConfidenceCollapseAction4thValue:
    def test_probe_environment_value_added(self):
        assert ConfidenceCollapseAction.PROBE_ENVIRONMENT.value == \
            "probe_environment"

    def test_taxonomy_now_4_values(self):
        # Closed 4-value taxonomy post-Slice-4. Adding more
        # requires explicit graduation work; this catches silent
        # additions.
        expected = {
            "retry_with_feedback",
            "escalate_to_operator",
            "inconclusive",
            "probe_environment",
        }
        assert {
            a.value for a in ConfidenceCollapseAction
        } == expected

    def test_existing_3_values_unchanged(self):
        # Backward-compat invariant — value strings cannot change
        # for the 3 base values.
        assert ConfidenceCollapseAction.RETRY_WITH_FEEDBACK.value \
            == "retry_with_feedback"
        assert ConfidenceCollapseAction.ESCALATE_TO_OPERATOR.value \
            == "escalate_to_operator"
        assert ConfidenceCollapseAction.INCONCLUSIVE.value == \
            "inconclusive"

    def test_action_is_string_serializable(self):
        # All consumers treat ConfidenceCollapseAction as string
        # passthrough — verify via str() coercion.
        for action in ConfidenceCollapseAction:
            assert isinstance(str(action.value), str)


# ---------------------------------------------------------------------------
# 2. ConfidenceMonitor.reset_window
# ---------------------------------------------------------------------------


class TestResetWindow:
    def test_reset_clears_window(self):
        monitor = _make_monitor()
        for _ in range(5):
            monitor.observe(0.5)
        assert monitor.snapshot().observations_count == 5
        dropped = monitor.reset_window()
        assert dropped == 5
        assert monitor.current_margin() is None

    def test_reset_empty_window_returns_zero(self):
        monitor = _make_monitor()
        dropped = monitor.reset_window()
        assert dropped == 0

    def test_reset_followed_by_observe_refills_cleanly(self):
        # Reset clears the rolling window; subsequent observe()
        # can refill cleanly (proves the deque is in a usable
        # state post-reset, not a corrupted one).
        monitor = _make_monitor()
        for _ in range(5):
            monitor.observe(0.5)
        monitor.reset_window()
        # Window empty
        assert monitor.current_margin() is None
        # Refill — should work
        for _ in range(3):
            monitor.observe(0.7)
        assert monitor.current_margin() == pytest.approx(0.7)

    def test_reset_master_off_is_noop(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_MONITOR_ENABLED", "false",
        )
        monitor = _make_monitor()
        # Cannot observe with master off — but if window has
        # entries from before flip, reset returns 0 (no-op).
        dropped = monitor.reset_window()
        assert dropped == 0

    def test_reset_never_raises(self):
        monitor = _make_monitor()
        for _ in range(10):
            monitor.observe(0.5)
        # Multiple resets in a row — never raises
        for _ in range(5):
            monitor.reset_window()


# ---------------------------------------------------------------------------
# 3. execute_probe_environment — full decision tree
# ---------------------------------------------------------------------------


class TestExecuteProbeEnvironment:
    @pytest.mark.asyncio
    async def test_converged_resets_monitor_and_returns_retry(
        self,
    ):
        monitor = _make_monitor()
        for _ in range(5):
            monitor.observe(0.1)  # low margin
        ctx = _ambiguity()
        prober = ReadonlyEvidenceProber(
            backend=_ConvergingBackend("foo is a function"),
        )
        verdict = await execute_probe_environment(
            monitor=monitor,
            ambiguity_context=ctx,
            resolver=prober,
            quorum=2, max_probes=3,
        )
        # Action mapping
        assert verdict.action is \
            ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert verdict.convergence_state == "probe_converged"
        # Confidence elevated
        assert verdict.confidence_posterior == 0.85
        # Feedback contains canonical answer
        assert "foo is a function" in verdict.feedback_text
        # Monitor window cleared
        assert monitor.current_margin() is None

    @pytest.mark.asyncio
    async def test_diverged_returns_escalate(self):
        monitor = _make_monitor()
        for _ in range(5):
            monitor.observe(0.1)
        ctx = _ambiguity()
        prober = ReadonlyEvidenceProber(
            backend=_DivergingBackend(),
        )
        verdict = await execute_probe_environment(
            monitor=monitor,
            ambiguity_context=ctx,
            resolver=prober,
            quorum=2, max_probes=3,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.ESCALATE_TO_OPERATOR
        assert verdict.convergence_state == "probe_diverged"
        assert verdict.confidence_posterior == 0.15
        # On divergence, monitor window is NOT reset
        assert monitor.current_margin() is not None

    @pytest.mark.asyncio
    async def test_exhausted_returns_inconclusive_with_budget_reduction(
        self, monkeypatch,
    ):
        # Force EXHAUSTED by patching run_probe_loop to return it
        async def _fake_runner(*a, **kw):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.EXHAUSTED,
                agreement_count=1,
                distinct_count=2,
                total_answers=2,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="budget exhausted",
            )
        monkeypatch.setattr(
            exec_mod, "run_probe_loop", _fake_runner,
        )
        monitor = _make_monitor()
        ctx = _ambiguity()
        verdict = await execute_probe_environment(
            monitor=monitor, ambiguity_context=ctx,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.INCONCLUSIVE
        assert verdict.convergence_state == "probe_exhausted"
        assert verdict.thinking_budget_reduction_factor == 0.5

    @pytest.mark.asyncio
    async def test_disabled_outcome_returns_retry_safe_default(
        self, monkeypatch,
    ):
        # Bridge is enabled but runner returns DISABLED (e.g.,
        # sub-flag flipped between checks)
        async def _fake_runner(*a, **kw):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.DISABLED,
                agreement_count=0,
                distinct_count=0,
                total_answers=0,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="disabled",
            )
        monkeypatch.setattr(
            exec_mod, "run_probe_loop", _fake_runner,
        )
        monitor = _make_monitor()
        ctx = _ambiguity()
        verdict = await execute_probe_environment(
            monitor=monitor, ambiguity_context=ctx,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert verdict.convergence_state == "probe_disabled"

    @pytest.mark.asyncio
    async def test_failed_outcome_returns_inconclusive(
        self, monkeypatch,
    ):
        async def _fake_runner(*a, **kw):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.FAILED,
                agreement_count=0,
                distinct_count=0,
                total_answers=0,
                canonical_answer=None,
                canonical_fingerprint=None,
                detail="runner crashed",
            )
        monkeypatch.setattr(
            exec_mod, "run_probe_loop", _fake_runner,
        )
        monitor = _make_monitor()
        ctx = _ambiguity()
        verdict = await execute_probe_environment(
            monitor=monitor, ambiguity_context=ctx,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.INCONCLUSIVE
        assert verdict.convergence_state == "probe_failed"

    @pytest.mark.asyncio
    async def test_master_off_short_circuits_zero_cost(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        monitor = _make_monitor()
        ctx = _ambiguity()
        backend = _ConvergingBackend()
        prober = ReadonlyEvidenceProber(backend=backend)
        verdict = await execute_probe_environment(
            monitor=monitor,
            ambiguity_context=ctx,
            resolver=prober,
            quorum=2, max_probes=3,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert verdict.convergence_state == "probe_disabled"
        # Backend NEVER called
        assert backend.calls == []

    @pytest.mark.asyncio
    async def test_runner_exception_caught_defensively(
        self, monkeypatch,
    ):
        # Defense-in-depth: even if runner raises (shouldn't),
        # executor catches and returns INCONCLUSIVE
        async def _boom_runner(*a, **kw):
            raise RuntimeError("runner exploded")
        monkeypatch.setattr(
            exec_mod, "run_probe_loop", _boom_runner,
        )
        monitor = _make_monitor()
        ctx = _ambiguity()
        verdict = await execute_probe_environment(
            monitor=monitor, ambiguity_context=ctx,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.INCONCLUSIVE
        assert verdict.convergence_state == "probe_runner_error"

    @pytest.mark.asyncio
    async def test_monitor_reset_exception_swallowed(
        self, monkeypatch,
    ):
        # Monitor.reset_window() raises — executor swallows
        class _BoomMonitor:
            def reset_window(self):
                raise RuntimeError("reset blew up")

        async def _converged_runner(*a, **kw):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.CONVERGED,
                agreement_count=2,
                distinct_count=1,
                total_answers=2,
                canonical_answer="x",
                canonical_fingerprint="abc",
                detail="converged",
            )
        monkeypatch.setattr(
            exec_mod, "run_probe_loop", _converged_runner,
        )
        ctx = _ambiguity()
        verdict = await execute_probe_environment(
            monitor=_BoomMonitor(), ambiguity_context=ctx,
        )
        # Still returns CONVERGED-mapped result; reset exception
        # didn't propagate
        assert verdict.action is \
            ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert verdict.convergence_state == "probe_converged"

    @pytest.mark.asyncio
    async def test_monitor_without_reset_window_method(
        self, monkeypatch,
    ):
        # Object without reset_window — defensive hasattr check
        async def _converged_runner(*a, **kw):
            return ConvergenceVerdict(
                outcome=ProbeOutcome.CONVERGED,
                agreement_count=2,
                distinct_count=1,
                total_answers=2,
                canonical_answer="x",
                canonical_fingerprint="abc",
                detail="converged",
            )
        monkeypatch.setattr(
            exec_mod, "run_probe_loop", _converged_runner,
        )
        ctx = _ambiguity()
        # Plain object — no reset_window
        verdict = await execute_probe_environment(
            monitor=object(), ambiguity_context=ctx,
        )
        # Still returns CONVERGED mapping
        assert verdict.action is \
            ConfidenceCollapseAction.RETRY_WITH_FEEDBACK


# ---------------------------------------------------------------------------
# 4. Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_pinned(self):
        assert PROBE_ENVIRONMENT_EXECUTOR_SCHEMA_VERSION == \
            "probe_environment_executor.1"


# ---------------------------------------------------------------------------
# 5. End-to-end smoke
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_pipeline_converged(self):
        # Generate context → run probes → CONVERGED → RETRY +
        # monitor reset
        monitor = _make_monitor()
        for _ in range(5):
            monitor.observe(0.05)  # very low margin
        ctx = AmbiguityContext(
            op_id="op-1",
            target_symbol="my_func",
            target_file="src/mod.py",
            claim="my_func is a function",
        )
        prober = ReadonlyEvidenceProber(
            backend=_ConvergingBackend("my_func is defined"),
        )
        verdict = await execute_probe_environment(
            monitor=monitor,
            ambiguity_context=ctx,
            op_id="op-1",
            prior=0.5,
            resolver=prober,
            quorum=2, max_probes=3,
        )
        assert verdict.action is \
            ConfidenceCollapseAction.RETRY_WITH_FEEDBACK
        assert verdict.confidence_posterior == 0.85
        assert "my_func is defined" in verdict.feedback_text
        # Monitor window reset
        assert monitor.current_margin() is None


# ---------------------------------------------------------------------------
# 6. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
)


_FORBIDDEN_MUTATION_TOOL_NAMES = (
    "edit_file",
    "write_file",
    "delete_file",
    "run_tests",
    "bash",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "verification"
                / "probe_environment_executor.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_forbidden_authority_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"executor imports forbidden modules: {offenders}"
        )

    def test_no_mutation_tool_name_references(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.id == fb:
                        offenders.append(node.id)
            elif isinstance(node, ast.Attribute):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.attr == fb:
                        offenders.append(node.attr)
        assert offenders == [], (
            f"executor references mutation tool names: "
            f"{offenders}"
        )

    def test_governance_imports_in_allowlist(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        allowed = (
            "confidence_probe_bridge",
            "confidence_probe_generator",
            "confidence_probe_runner",
            "confidence_monitor",  # for ConfidenceMonitor type
            "hypothesis_consumers",
            # SBT-Probe Escalation Bridge (Slice 2, 2026-05-02) —
            # adds the lazy-import escalation hook on EXHAUSTED.
            "sbt_escalation_runner",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(sub in mod for sub in allowed)
                assert ok, (
                    f"executor imports unexpected governance "
                    f"module: {mod}"
                )

    def test_no_disk_writes(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"executor contains forbidden write token: {tok!r}"
            )

    def test_async_function_present(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        async_def_names = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        }
        assert "execute_probe_environment" in async_def_names

    def test_public_api_exported(self):
        expected = {
            "PROBE_ENVIRONMENT_EXECUTOR_SCHEMA_VERSION",
            "execute_probe_environment",
        }
        assert set(exec_mod.__all__) == expected

    def test_probe_environment_value_pinned(self):
        # Source-token grep: the new enum value must appear in
        # hypothesis_consumers.py. Catches refactor that drops it.
        source = (
            _module_path().parent / "hypothesis_consumers.py"
        ).read_text(encoding="utf-8")
        assert 'PROBE_ENVIRONMENT = "probe_environment"' in source

    def test_reset_window_method_present(self):
        # Source-token grep: ConfidenceMonitor must expose
        # reset_window. Catches refactor that drops it.
        source = (
            _module_path().parent / "confidence_monitor.py"
        ).read_text(encoding="utf-8")
        assert "def reset_window" in source
