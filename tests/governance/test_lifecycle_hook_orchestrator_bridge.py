"""Lifecycle Hook Registry Slice 4 — orchestrator bridge tests.

Covers:
  * 5 typed gate helpers — one per LifecycleEvent
  * LifecycleHookGate result shape mirrors DeployGate.preflight
    (passed/failed semantics)
  * BLOCK aggregate → passed=False; WARN/CONTINUE → passed=True
  * Each gate helper builds the correct HookContext per event
  * Master-flag-off short-circuit (via fire_hooks) → passed=True
  * Defensive fail-open: bridge crash → passed=True with sentinel
    detail (broken hook substrate cannot block autonomous loop)
  * Async cancellation propagates per asyncio convention
  * Orchestrator wire-up parses cleanly (smoke test on import +
    AST presence of the gate_pre_apply call site)
  * Authority allowlist (no orchestrator-tier imports in bridge)
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
from typing import Optional, Tuple

import pytest

from backend.core.ouroboros.governance.lifecycle_hook import (
    HookContext,
    HookOutcome,
    HookResult,
    LifecycleEvent,
    make_hook_result,
)
from backend.core.ouroboros.governance.lifecycle_hook_orchestrator_bridge import (
    LIFECYCLE_HOOK_BRIDGE_SCHEMA_VERSION,
    LifecycleHookGate,
    gate_on_operator_action,
    gate_post_apply,
    gate_post_verify,
    gate_pre_apply,
    gate_pre_generate,
)
from backend.core.ouroboros.governance.lifecycle_hook_registry import (
    LifecycleHookRegistry,
    get_default_registry,
    reset_default_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Hook fixtures
# ---------------------------------------------------------------------------


def _continue_hook(ctx: HookContext) -> HookResult:
    return make_hook_result("c", HookOutcome.CONTINUE)


def _block_hook(ctx: HookContext) -> HookResult:
    return make_hook_result("b", HookOutcome.BLOCK, detail="blocked")


def _warn_hook(ctx: HookContext) -> HookResult:
    return make_hook_result("w", HookOutcome.WARN, detail="warning")


def _payload_capturing_hook_factory(captured: list):
    """Returns a hook that records the HookContext it sees, so tests
    verify per-event payload composition."""
    def _h(ctx: HookContext) -> HookResult:
        captured.append({
            "event": ctx.event,
            "op_id": ctx.op_id,
            "phase": ctx.phase,
            "payload": dict(ctx.payload),
        })
        return make_hook_result("p", HookOutcome.CONTINUE)
    return _h


# ---------------------------------------------------------------------------
# LifecycleHookGate shape
# ---------------------------------------------------------------------------


class TestLifecycleHookGateShape:
    def test_passed_false_on_block_aggregate(self):
        g = LifecycleHookGate(
            event=LifecycleEvent.PRE_APPLY,
            passed=False,
            aggregate=HookOutcome.BLOCK,
            blocking_hooks=("b",),
            monotonic_tightening_verdict="passed",
        )
        assert g.passed is False
        assert g.is_tightening is True
        assert g.should_warn is False

    def test_passed_true_on_continue_aggregate(self):
        g = LifecycleHookGate(
            event=LifecycleEvent.PRE_APPLY,
            passed=True,
            aggregate=HookOutcome.CONTINUE,
        )
        assert g.passed is True
        assert g.is_tightening is False
        assert g.should_warn is False

    def test_should_warn_only_on_warn(self):
        g = LifecycleHookGate(
            event=LifecycleEvent.PRE_APPLY,
            passed=True,
            aggregate=HookOutcome.WARN,
            warning_hooks=("w",),
        )
        assert g.should_warn is True
        assert g.passed is True
        assert g.is_tightening is False

    def test_to_dict_round_trip_shape(self):
        g = LifecycleHookGate(
            event=LifecycleEvent.PRE_APPLY,
            passed=False,
            aggregate=HookOutcome.BLOCK,
            total_hooks=3,
            blocking_hooks=("b1", "b2"),
            warning_hooks=("w1",),
            failed_hooks=("f1",),
            detail="test",
            elapsed_ms=12.5,
            monotonic_tightening_verdict="passed",
        )
        d = g.to_dict()
        assert d["event"] == "pre_apply"
        assert d["passed"] is False
        assert d["aggregate"] == "block"
        assert d["blocking_hooks"] == ["b1", "b2"]
        assert d["warning_hooks"] == ["w1"]
        assert d["failed_hooks"] == ["f1"]


# ---------------------------------------------------------------------------
# Per-event gate helpers
# ---------------------------------------------------------------------------


class TestGateHelpers:
    @pytest.mark.asyncio
    async def test_gate_pre_apply_block_returns_passed_false(
        self, monkeypatch,
    ):
        # Master flag default-FALSE through Slices 1-4 — must
        # explicitly enable for fire_hooks to actually run hooks.
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "true")
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _block_hook, name="blocker",
        )
        reset_default_registry_for_tests()
        # Inject our test registry into the singleton slot.
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply(
                "op-x", target_files=("a.py",),
                diff_summary="test diff",
            )
            assert gate.passed is False
            assert gate.aggregate is HookOutcome.BLOCK
            assert "blocker" in gate.blocking_hooks
            assert gate.is_tightening is True
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_gate_pre_apply_continue_returns_passed_true(
        self, monkeypatch,
    ):
        # Need master flag enabled for fire_hooks to actually run.
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "true")
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _continue_hook, name="ok",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply(
                "op-x", target_files=("a.py",),
            )
            assert gate.passed is True
            assert gate.aggregate is HookOutcome.CONTINUE
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_gate_pre_apply_warn_returns_passed_true_should_warn(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "true")
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _warn_hook, name="warner",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply("op-x")
            assert gate.passed is True
            assert gate.aggregate is HookOutcome.WARN
            assert gate.should_warn is True
            assert "warner" in gate.warning_hooks
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_master_flag_off_returns_passed_true(self, monkeypatch):
        """Master flag default-FALSE → fire_hooks short-circuits →
        gate returns passed=True (CONTINUE). Critical backward-compat:
        bridge MUST NOT block the orchestrator when master is off."""
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "false")
        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _block_hook, name="blocker",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            gate = await gate_pre_apply("op-x")
            # Even though a BLOCK hook is registered, master off
            # short-circuits before fire_hooks runs it.
            assert gate.passed is True
            assert gate.aggregate is HookOutcome.CONTINUE
            assert gate.total_hooks == 0
        finally:
            reset_default_registry_for_tests()


# ---------------------------------------------------------------------------
# Per-event payload composition
# ---------------------------------------------------------------------------


class TestPayloadComposition:
    def _setup(self, monkeypatch, event: LifecycleEvent):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "true")
        captured: list = []
        registry = LifecycleHookRegistry()
        registry.register(
            event, _payload_capturing_hook_factory(captured),
            name="capture",
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        return captured

    @pytest.mark.asyncio
    async def test_pre_generate_payload(self, monkeypatch):
        captured = self._setup(monkeypatch, LifecycleEvent.PRE_GENERATE)
        try:
            await gate_pre_generate(
                "op-pg", route="STANDARD", cost_estimate_usd=0.05,
                extra={"model": "claude-sonnet"},
            )
            assert len(captured) == 1
            c = captured[0]
            assert c["event"] is LifecycleEvent.PRE_GENERATE
            assert c["op_id"] == "op-pg"
            assert c["phase"] == "GENERATE"
            assert c["payload"]["route"] == "STANDARD"
            assert c["payload"]["cost_estimate_usd"] == 0.05
            assert c["payload"]["model"] == "claude-sonnet"
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_pre_apply_payload(self, monkeypatch):
        captured = self._setup(monkeypatch, LifecycleEvent.PRE_APPLY)
        try:
            await gate_pre_apply(
                "op-pa",
                target_files=("a.py", "b.py"),
                diff_summary="rename helper",
                risk_tier="NOTIFY_APPLY",
            )
            assert len(captured) == 1
            c = captured[0]
            assert c["event"] is LifecycleEvent.PRE_APPLY
            assert c["op_id"] == "op-pa"
            assert c["phase"] == "APPLY"
            assert c["payload"]["target_files"] == ["a.py", "b.py"]
            assert c["payload"]["diff_summary"] == "rename helper"
            assert c["payload"]["risk_tier"] == "NOTIFY_APPLY"
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_post_apply_payload(self, monkeypatch):
        captured = self._setup(monkeypatch, LifecycleEvent.POST_APPLY)
        try:
            await gate_post_apply(
                "op-pa2",
                applied_files=("a.py",),
                apply_mode="single",
            )
            assert len(captured) == 1
            c = captured[0]
            assert c["event"] is LifecycleEvent.POST_APPLY
            assert c["payload"]["applied_files"] == ["a.py"]
            assert c["payload"]["apply_mode"] == "single"
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_post_verify_payload(self, monkeypatch):
        captured = self._setup(monkeypatch, LifecycleEvent.POST_VERIFY)
        try:
            await gate_post_verify(
                "op-pv", verify_passed=True, duration_s=12.3,
            )
            assert len(captured) == 1
            c = captured[0]
            assert c["event"] is LifecycleEvent.POST_VERIFY
            assert c["payload"]["verify_passed"] is True
            assert c["payload"]["duration_s"] == 12.3
        finally:
            reset_default_registry_for_tests()

    @pytest.mark.asyncio
    async def test_on_operator_action_payload(self, monkeypatch):
        captured = self._setup(
            monkeypatch, LifecycleEvent.ON_OPERATOR_ACTION,
        )
        try:
            await gate_on_operator_action(
                "op-oa", action="cancel", actor="repl-derek",
            )
            assert len(captured) == 1
            c = captured[0]
            assert c["event"] is LifecycleEvent.ON_OPERATOR_ACTION
            assert c["payload"]["action"] == "cancel"
            assert c["payload"]["actor"] == "repl-derek"
        finally:
            reset_default_registry_for_tests()


# ---------------------------------------------------------------------------
# Defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveFailOpen:
    @pytest.mark.asyncio
    async def test_bridge_crash_returns_passed_true(
        self, monkeypatch,
    ):
        """If something inside the bridge pathway crashes (e.g.,
        fire_hooks raises despite its NEVER-raise contract), gate
        MUST return passed=True with sentinel detail. Fail-open
        philosophy: a broken hook substrate cannot stop the
        autonomous loop."""
        from backend.core.ouroboros.governance import (
            lifecycle_hook_orchestrator_bridge as bridge_mod,
        )

        async def _broken_fire_hooks(*args, **kwargs):
            raise RuntimeError("forced bridge crash")

        monkeypatch.setattr(
            bridge_mod, "fire_hooks", _broken_fire_hooks,
        )
        gate = await gate_pre_apply("op-x")
        assert gate.passed is True  # FAIL-OPEN
        assert gate.aggregate is HookOutcome.CONTINUE
        assert "bridge_fail_open" in gate.detail

    @pytest.mark.asyncio
    async def test_async_cancellation_propagates(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LIFECYCLE_HOOKS_ENABLED", "true")

        def _slow_hook(ctx):
            import time as _t
            _t.sleep(2.0)
            return make_hook_result("slow", HookOutcome.CONTINUE)

        registry = LifecycleHookRegistry()
        registry.register(
            LifecycleEvent.PRE_APPLY, _slow_hook, name="slow",
            timeout_s=10.0,
        )
        reset_default_registry_for_tests()
        from backend.core.ouroboros.governance import (
            lifecycle_hook_registry as reg_mod,
        )
        reg_mod._default_registry = registry
        try:
            task = asyncio.create_task(gate_pre_apply("op-x"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            reset_default_registry_for_tests()


# ---------------------------------------------------------------------------
# Orchestrator wire-up smoke
# ---------------------------------------------------------------------------


class TestOrchestratorWireUpSmoke:
    """Verify the orchestrator wire-up parses + the
    gate_pre_apply call site is present + structured correctly.
    Pure AST inspection — no orchestrator boot required."""

    def _orchestrator_source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "orchestrator.py"
        )
        return path.read_text()

    def test_orchestrator_imports_gate_pre_apply(self):
        """The orchestrator's PRE_APPLY wire-up must import
        gate_pre_apply via the bridge."""
        src = self._orchestrator_source()
        assert "from backend.core.ouroboros.governance.lifecycle_hook_orchestrator_bridge import" in src
        assert "gate_pre_apply" in src

    def test_orchestrator_calls_gate_pre_apply(self):
        """The orchestrator must actually invoke gate_pre_apply."""
        src = self._orchestrator_source()
        assert "gate_pre_apply(" in src

    def test_orchestrator_routes_block_to_cancelled(self):
        """When the gate returns passed=False, the orchestrator must
        route to CANCELLED with a terminal_reason_code starting with
        ``lifecycle_hook_blocked``."""
        src = self._orchestrator_source()
        assert "lifecycle_hook_blocked:" in src

    def test_orchestrator_parses_clean(self):
        """Sanity: the orchestrator file with the wire-up must parse
        as valid Python."""
        src = self._orchestrator_source()
        try:
            ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(
                f"orchestrator.py SyntaxError after wire-up: {exc}"
            )


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "lifecycle_hook_orchestrator_bridge.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        allowed = {
            "backend.core.ouroboros.governance.lifecycle_hook",
            "backend.core.ouroboros.governance.lifecycle_hook_executor",
        }
        tree = ast.parse(self._source())
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." in module or (
                    "governance" in module and module
                ):
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    if module not in allowed:
                        raise AssertionError(
                            f"Slice 4 imported module outside "
                            f"allowlist: {module!r} at line {lineno}"
                        )

    def test_no_orchestrator_tier_imports(self):
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for ban in banned_substrings:
                    if ban in module:
                        raise AssertionError(
                            f"Slice 4 imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r}"
                        )

    def test_no_exec_eval_compile_calls(self):
        tree = ast.parse(self._source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 4 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestConstants:
    def test_schema_version(self):
        assert LIFECYCLE_HOOK_BRIDGE_SCHEMA_VERSION == (
            "lifecycle_hook_orchestrator_bridge.1"
        )
