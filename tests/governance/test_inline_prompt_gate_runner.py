"""InlinePromptGate Slice 2 — async producer / controller bridge tests.

Covers:
  * Master-flag short-circuit (DISABLED, no controller call)
  * End-to-end happy paths (allow/deny/pause/expired) using the
    real :class:`InlinePromptController` (singleton substrate)
  * Bridge function field projection (sentinels + truncation +
    multi-path summary)
  * Controller capacity / state-error degradation → DISABLED
  * Async timeout (defense-in-depth) → EXPIRED
  * Async cancellation propagation (the documented exception case)
  * Bridge sentinels are wire-format stable (no rename without
    cascading update)
  * Authority invariant — runner imports allowlist (allowed:
    inline_prompt_gate / inline_permission_prompt /
    inline_permission; banned: orchestrator-tier modules)
  * No exec/eval/compile (mirrors Slice 1 critical safety pin)
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid

import pytest

from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
    RoutePosture,
    UpstreamPolicy,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
)
from backend.core.ouroboros.governance.inline_prompt_gate import (
    PhaseInlinePromptRequest,
    PhaseInlineVerdict,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    DEFAULT_REVIEWER,
    PHASE_BOUNDARY_CALL_ID_PREFIX,
    PHASE_BOUNDARY_RULE_ID,
    PHASE_BOUNDARY_TOOL_SENTINEL,
    bridge_to_controller_request,
    request_phase_inline_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _request(**overrides) -> PhaseInlinePromptRequest:
    defaults: dict = {
        "prompt_id": "ipg-" + uuid.uuid4().hex[:24],
        "op_id": "op-" + uuid.uuid4().hex[:8],
        "phase_at_request": "GATE",
        "risk_tier": "NOTIFY_APPLY",
        "change_summary": "edit foo.py: rename helper",
        "change_fingerprint": "deadbeef" * 8,
        "target_paths": ("backend/foo.py",),
        "rationale": "model proposed a rename",
        "route": "interactive",
    }
    defaults.update(overrides)
    return PhaseInlinePromptRequest(**defaults)


def _fresh_controller() -> InlinePromptController:
    """A fresh controller per test — avoids the module singleton's
    cross-test state."""
    return InlinePromptController(default_timeout_s=10.0)


# ---------------------------------------------------------------------------
# Master flag short-circuit
# ---------------------------------------------------------------------------


class TestMasterFlagShortCircuit:
    @pytest.mark.asyncio
    async def test_disabled_returns_disabled_verdict_no_controller_call(self):
        """When master is off, producer must not touch the controller."""
        controller = _fresh_controller()
        verdict = await request_phase_inline_prompt(
            _request(), controller=controller, enabled=False,
        )
        assert verdict.verdict is PhaseInlineVerdict.DISABLED
        assert controller.pending_count == 0

    @pytest.mark.asyncio
    async def test_disabled_preserves_request_metadata(self):
        controller = _fresh_controller()
        req = _request(prompt_id="ipg-test-x", op_id="op-test-x")
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=False,
        )
        assert verdict.prompt_id == "ipg-test-x"
        assert verdict.op_id == "op-test-x"
        assert verdict.monotonic_tightening_verdict == ""

    @pytest.mark.asyncio
    async def test_enabled_arg_overrides_env(self, monkeypatch):
        """Explicit enabled=True wins over env-off."""
        monkeypatch.setenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", "false",
        )
        controller = _fresh_controller()
        req = _request()

        async def _resolver():
            await asyncio.sleep(0.05)
            controller.allow_once(req.prompt_id, reviewer="test")

        # Run resolver concurrently
        loop_task = asyncio.create_task(_resolver())
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
        )
        await loop_task
        assert verdict.verdict is PhaseInlineVerdict.ALLOW


# ---------------------------------------------------------------------------
# End-to-end happy paths via real controller
# ---------------------------------------------------------------------------


class TestEndToEndHappyPaths:
    @pytest.mark.asyncio
    async def test_allow_yields_allow_verdict(self):
        controller = _fresh_controller()
        req = _request()

        async def _resolver():
            await asyncio.sleep(0.05)
            controller.allow_once(
                req.prompt_id, reviewer="repl_operator",
                reason="looks safe",
            )

        loop_task = asyncio.create_task(_resolver())
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
        )
        await loop_task
        assert verdict.verdict is PhaseInlineVerdict.ALLOW
        assert verdict.allowed is True
        assert verdict.reviewer == "repl_operator"
        assert verdict.operator_reason == "looks safe"
        assert verdict.elapsed_s >= 0.0
        assert verdict.monotonic_tightening_verdict == ""

    @pytest.mark.asyncio
    async def test_deny_yields_deny_verdict_with_tightening_stamp(self):
        controller = _fresh_controller()
        req = _request()

        async def _resolver():
            await asyncio.sleep(0.05)
            controller.deny(
                req.prompt_id, reviewer="repl_operator",
                reason="touches credentials",
            )

        loop_task = asyncio.create_task(_resolver())
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
        )
        await loop_task
        assert verdict.verdict is PhaseInlineVerdict.DENY
        assert verdict.is_tightening is True
        assert verdict.monotonic_tightening_verdict == "passed"
        assert verdict.operator_reason == "touches credentials"

    @pytest.mark.asyncio
    async def test_pause_yields_pause_op_verdict_with_tightening_stamp(self):
        controller = _fresh_controller()
        req = _request()

        async def _resolver():
            await asyncio.sleep(0.05)
            controller.pause_op(
                req.prompt_id, reviewer="repl_operator",
                reason="hold for review",
            )

        loop_task = asyncio.create_task(_resolver())
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
        )
        await loop_task
        assert verdict.verdict is PhaseInlineVerdict.PAUSE_OP
        assert verdict.is_tightening is True
        assert verdict.monotonic_tightening_verdict == "passed"

    @pytest.mark.asyncio
    async def test_controller_internal_timeout_yields_expired(self):
        """Controller's own _run_timeout fires STATE_EXPIRED before
        the producer's defense-in-depth wait_for."""
        controller = InlinePromptController(default_timeout_s=0.05)
        req = _request()
        # No resolver — let the controller timeout.
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
            timeout_s=2.0,  # generous secondary
        )
        assert verdict.verdict is PhaseInlineVerdict.EXPIRED
        assert verdict.is_tightening is False
        assert verdict.monotonic_tightening_verdict == ""

    @pytest.mark.asyncio
    async def test_allow_always_maps_to_allow_verdict(self):
        """The controller's STATE_ALLOWED state covers both
        allow_once and allow_always — both map to PhaseInlineVerdict.ALLOW."""
        controller = _fresh_controller()
        req = _request()

        async def _resolver():
            await asyncio.sleep(0.05)
            controller.allow_always(req.prompt_id, reviewer="repl")

        loop_task = asyncio.create_task(_resolver())
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
        )
        await loop_task
        assert verdict.verdict is PhaseInlineVerdict.ALLOW


# ---------------------------------------------------------------------------
# Bridge function — controller-shape projection
# ---------------------------------------------------------------------------


class TestBridgeToControllerRequest:
    def test_bridge_uses_phase_boundary_sentinels(self):
        req = _request()
        bridged = bridge_to_controller_request(req)
        assert bridged.tool == PHASE_BOUNDARY_TOOL_SENTINEL
        assert bridged.verdict.rule_id == PHASE_BOUNDARY_RULE_ID
        assert bridged.verdict.decision is InlineDecision.ASK
        assert bridged.call_id.startswith(PHASE_BOUNDARY_CALL_ID_PREFIX)
        assert bridged.upstream_decision is UpstreamPolicy.NO_MATCH

    def test_bridge_preserves_prompt_id_and_op_id(self):
        req = _request(prompt_id="ipg-xyz", op_id="op-xyz")
        bridged = bridge_to_controller_request(req)
        assert bridged.prompt_id == "ipg-xyz"
        assert bridged.op_id == "op-xyz"
        assert "op-xyz" in bridged.call_id

    def test_bridge_uses_change_fingerprint_as_arg_fingerprint(self):
        req = _request(change_fingerprint="a" * 64)
        bridged = bridge_to_controller_request(req)
        assert bridged.arg_fingerprint == "a" * 64

    def test_bridge_truncates_long_summary(self):
        long_summary = "x" * 500
        req = _request(change_summary=long_summary)
        bridged = bridge_to_controller_request(req)
        assert len(bridged.arg_preview) <= 200

    def test_bridge_single_target_path(self):
        req = _request(target_paths=("backend/single.py",))
        bridged = bridge_to_controller_request(req)
        assert bridged.target_path == "backend/single.py"

    def test_bridge_multi_target_path_renders_summary(self):
        req = _request(target_paths=("a.py", "b.py", "c.py"))
        bridged = bridge_to_controller_request(req)
        assert bridged.target_path == "a.py (+2 more)"

    def test_bridge_empty_target_paths_renders_sentinel(self):
        req = _request(target_paths=())
        bridged = bridge_to_controller_request(req)
        assert bridged.target_path == "(no targets)"

    def test_bridge_route_interactive_default(self):
        req = _request(route="interactive")
        bridged = bridge_to_controller_request(req)
        assert bridged.route is RoutePosture.INTERACTIVE

    def test_bridge_route_autonomous(self):
        req = _request(route="autonomous")
        bridged = bridge_to_controller_request(req)
        assert bridged.route is RoutePosture.AUTONOMOUS

    def test_bridge_route_garbage_defaults_to_interactive(self):
        req = _request(route="not-a-route")
        bridged = bridge_to_controller_request(req)
        assert bridged.route is RoutePosture.INTERACTIVE

    def test_bridge_synthesizes_verdict_reason_from_summary(self):
        req = _request(change_summary="rename helper to do_thing")
        bridged = bridge_to_controller_request(req)
        assert "rename helper" in bridged.verdict.reason

    def test_bridge_never_raises_on_garbage_request(self):
        """Defensive — passing a malformed request returns a sentinel
        controller request rather than raising."""

        class _BrokenReq:
            prompt_id = "bad"
            op_id = "bad"
            change_summary = property(lambda self: 1 / 0)  # raises

        bridged = bridge_to_controller_request(
            _BrokenReq(),  # type: ignore[arg-type]
        )
        # Sentinel constructed; structural fields populated.
        assert bridged.tool == PHASE_BOUNDARY_TOOL_SENTINEL
        assert bridged.verdict.rule_id == PHASE_BOUNDARY_RULE_ID


# ---------------------------------------------------------------------------
# Defensive degradation paths
# ---------------------------------------------------------------------------


class TestDefensiveDegradation:
    @pytest.mark.asyncio
    async def test_capacity_exhausted_yields_disabled(self):
        controller = InlinePromptController(
            max_pending=1, default_timeout_s=10.0,
        )
        # Fill capacity with a held prompt.
        held = _request(prompt_id="ipg-held")
        held_future = asyncio.ensure_future(
            request_phase_inline_prompt(
                held, controller=controller, enabled=True,
            ),
        )
        # Yield once so the held prompt registers.
        await asyncio.sleep(0)

        # Now register a second — capacity exhausted.
        overflow = _request(prompt_id="ipg-overflow")
        verdict = await request_phase_inline_prompt(
            overflow, controller=controller, enabled=True,
        )
        assert verdict.verdict is PhaseInlineVerdict.DISABLED

        # Clean up the held prompt.
        controller.deny(held.prompt_id, reviewer="cleanup")
        await held_future

    @pytest.mark.asyncio
    async def test_state_collision_yields_disabled(self):
        """If the controller already has a pending prompt with the
        same prompt_id (idempotent retry), the producer must not
        raise — degrade to DISABLED."""
        controller = _fresh_controller()
        req = _request(prompt_id="ipg-collide")
        # Pre-register a held prompt under the same id.
        first_future = asyncio.ensure_future(
            request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            ),
        )
        await asyncio.sleep(0)

        # Second concurrent request with same id → state error.
        second_verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
        )
        assert second_verdict.verdict is PhaseInlineVerdict.DISABLED

        # Clean up the first.
        controller.allow_once(req.prompt_id, reviewer="cleanup")
        await first_future

    @pytest.mark.asyncio
    async def test_secondary_async_timeout_yields_expired(self):
        """If the asyncio wait_for fires before the controller's
        own timeout (defense-in-depth), the producer synthesizes
        an EXPIRED verdict directly."""
        # Controller timeout very generous; secondary tight.
        controller = InlinePromptController(default_timeout_s=10.0)
        req = _request()
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True,
            timeout_s=0.05,  # forces secondary trip
        )
        assert verdict.verdict is PhaseInlineVerdict.EXPIRED
        assert verdict.monotonic_tightening_verdict == ""

    @pytest.mark.asyncio
    async def test_async_cancellation_propagates(self):
        """Caller-initiated cancellation propagates per asyncio
        convention — the orchestrator wire-up (Slice 4) catches."""
        controller = _fresh_controller()
        req = _request()

        producer_task = asyncio.create_task(
            request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            ),
        )
        await asyncio.sleep(0.05)  # let it register
        producer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await producer_task


# ---------------------------------------------------------------------------
# Sentinel constants — wire-format stability
# ---------------------------------------------------------------------------


class TestSentinelConstants:
    def test_tool_sentinel_value_stable(self):
        assert PHASE_BOUNDARY_TOOL_SENTINEL == "phase_boundary"

    def test_rule_id_sentinel_value_stable(self):
        assert PHASE_BOUNDARY_RULE_ID == "phase_boundary_inline_prompt"

    def test_call_id_prefix_value_stable(self):
        assert PHASE_BOUNDARY_CALL_ID_PREFIX == "pb-"

    def test_default_reviewer_value_stable(self):
        assert DEFAULT_REVIEWER == "phase_boundary_producer"

    def test_sentinels_distinguishable_from_real_tool_names(self):
        """The phase-boundary tool sentinel must not collide with
        any real tool name in :mod:`tool_executor`'s built-in
        toolset (read_file, search_code, edit_file, write_file,
        bash, etc.) — assert the sentinel is structurally distinct."""
        real_tools = {
            "read_file", "search_code", "edit_file", "write_file",
            "delete_file", "bash", "web_fetch", "web_search",
            "run_tests", "get_callers", "glob_files", "list_dir",
            "list_symbols", "git_log", "git_diff", "git_blame",
            "ask_human", "apply_patch",
        }
        assert PHASE_BOUNDARY_TOOL_SENTINEL not in real_tools


# ---------------------------------------------------------------------------
# Authority invariant — runner imports allowlist
# ---------------------------------------------------------------------------


class TestRunnerAuthorityInvariant:
    def _runner_source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "inline_prompt_gate_runner.py"
        )
        return path.read_text()

    def test_imports_are_in_allowlist(self):
        """Slice 2 hot path may import:
          * Slice 1 (inline_prompt_gate)
          * Controller substrate (inline_permission_prompt)
          * Verdict shapes (inline_permission)
        Module-owned registration functions (``register_flags`` /
        ``register_shipped_invariants``) are STRUCTURALLY exempt —
        boot-time discovery only. Same exemption as Priority #6
        closure (commit 441cdc7bd2)."""
        allowed = {
            "backend.core.ouroboros.governance.inline_permission",
            "backend.core.ouroboros.governance.inline_permission_prompt",
            "backend.core.ouroboros.governance.inline_prompt_gate",
        }
        tree = ast.parse(self._runner_source())
        registration_funcs = {"register_flags", "register_shipped_invariants"}
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
                            f"Slice 2 imported module outside allowlist: "
                            f"{module!r} at line {lineno}"
                        )

    def test_no_orchestrator_tier_imports(self):
        """Defense-in-depth: explicit ban on orchestrator + policy
        modules, even if they were mistakenly added to the allowlist."""
        banned_substrings = (
            "orchestrator", "phase_runner", "iron_gate",
            "change_engine", "candidate_generator",
            ".providers", "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "semantic_guardian",
            "semantic_firewall", "risk_engine",
        )
        tree = ast.parse(self._runner_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for ban in banned_substrings:
                    if ban in module:
                        raise AssertionError(
                            f"Slice 2 imported BANNED orchestrator-tier "
                            f"substring {ban!r} via {module!r} at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )

    def test_no_exec_eval_compile_calls(self):
        """Mirrors Slice 1 critical safety pin."""
        tree = ast.parse(self._runner_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 2 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


# ---------------------------------------------------------------------------
# Concurrent-prompt sanity
# ---------------------------------------------------------------------------


class TestConcurrentPrompts:
    @pytest.mark.asyncio
    async def test_concurrent_prompts_on_different_op_ids(self):
        """Multiple in-flight phase-boundary prompts on distinct
        op_ids resolve independently."""
        controller = _fresh_controller()
        reqs = [
            _request(prompt_id=f"ipg-c{i}", op_id=f"op-c{i}")
            for i in range(3)
        ]

        async def _resolver():
            await asyncio.sleep(0.05)
            for r in reqs:
                # mix of allow / deny / pause
                if r.prompt_id == "ipg-c0":
                    controller.allow_once(r.prompt_id, reviewer="r")
                elif r.prompt_id == "ipg-c1":
                    controller.deny(r.prompt_id, reviewer="r")
                else:
                    controller.pause_op(r.prompt_id, reviewer="r")

        resolver_task = asyncio.create_task(_resolver())
        results = await asyncio.gather(*[
            request_phase_inline_prompt(
                r, controller=controller, enabled=True,
            )
            for r in reqs
        ])
        await resolver_task
        assert results[0].verdict is PhaseInlineVerdict.ALLOW
        assert results[1].verdict is PhaseInlineVerdict.DENY
        assert results[2].verdict is PhaseInlineVerdict.PAUSE_OP


# ---------------------------------------------------------------------------
# SSE bridge integration — controller listener fires through to broker
# ---------------------------------------------------------------------------


class TestSSEBridgeIntegration:
    @pytest.mark.asyncio
    async def test_controller_listener_fires_on_phase_boundary_prompt(self):
        """Reuse contract: any listener registered via
        ``controller.on_transition(...)`` fires for phase-boundary
        prompts identically to per-tool-call prompts. This is the
        mechanism by which the existing
        ``inline_permission_observability.attach_controller_to_broker``
        publishes ``inline_prompt_*`` SSE events for our prompts
        with ZERO new wiring."""
        controller = _fresh_controller()
        events: list = []

        def _listener(payload: dict) -> None:
            events.append(payload.get("event_type"))

        unsub = controller.on_transition(_listener)
        try:
            req = _request()

            async def _resolver():
                await asyncio.sleep(0.05)
                controller.allow_once(req.prompt_id, reviewer="r")

            resolver_task = asyncio.create_task(_resolver())
            await request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            )
            await resolver_task

            assert "inline_prompt_pending" in events
            assert "inline_prompt_allowed" in events
        finally:
            unsub()
