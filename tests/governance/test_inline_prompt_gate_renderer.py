"""InlinePromptGate Slice 4 — listener-based renderer tests.

Covers:
  * Pure formatters (golden output) for pending + each terminal state
  * Phase-boundary projection filter (sentinel + rule_id fallback)
  * Listener fires render on PENDING phase-boundary event
  * Listener does NOT fire render on PENDING per-tool-call event
  * Listener fires dismiss line on terminal transitions
  * Listener handles broken print_cb without raising
  * attach_phase_boundary_renderer returns callable unsub
  * unsub stops further renders
  * End-to-end through the real InlinePromptController singleton
  * Defensive degradation on malformed projection
  * Authority allowlist (no orchestrator-tier imports)
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import uuid
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.inline_permission import (
    InlineDecision,
    InlineGateVerdict,
    RoutePosture,
    UpstreamPolicy,
)
from backend.core.ouroboros.governance.inline_permission_prompt import (
    InlinePromptController,
    InlinePromptRequest,
)
from backend.core.ouroboros.governance.inline_prompt_gate import (
    PhaseInlinePromptRequest,
)
from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
    PENDING_EVENT,
    PHASE_BOUNDARY_HEADER,
    PROMPT_ACTIONS_HINT,
    PROMPT_ID_DISPLAY_CHARS,
    TERMINAL_EVENTS,
    TERMINAL_STATE_VERBS,
    attach_phase_boundary_renderer,
    format_dismiss_line,
    format_phase_boundary_block,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    PHASE_BOUNDARY_RULE_ID,
    PHASE_BOUNDARY_TOOL_SENTINEL,
    bridge_to_controller_request,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _phase_projection(**overrides) -> Dict[str, Any]:
    """A controller projection dict shaped like a phase-boundary
    pending prompt."""
    defaults: Dict[str, Any] = {
        "prompt_id": "ipg-test-render",
        "op_id": "op-render",
        "call_id": "pb-op-render",
        "tool": PHASE_BOUNDARY_TOOL_SENTINEL,
        "target_path": "backend/foo.py (+2 more)",
        "arg_preview": "edit foo.py: rename helper to do_thing",
        "verdict_rule_id": PHASE_BOUNDARY_RULE_ID,
        "verdict_decision": "ask",
        "state": "pending",
        "response": None,
        "reviewer": "",
        "operator_reason": "",
        "created_ts": 0.0,
        "timeout_s": 60.0,
        "expires_ts": 60.0,
    }
    defaults.update(overrides)
    return defaults


def _per_tool_projection(**overrides) -> Dict[str, Any]:
    """A controller projection dict shaped like a per-tool-call
    pending prompt — must NOT trigger the phase-boundary
    renderer."""
    defaults: Dict[str, Any] = {
        "prompt_id": "tool-call-1",
        "op_id": "op-tool",
        "call_id": "call-tool-1",
        "tool": "edit_file",
        "target_path": "backend/foo.py",
        "arg_preview": "edit foo.py: change line 1",
        "verdict_rule_id": "some_real_tool_rule",
        "verdict_decision": "ask",
        "state": "pending",
        "response": None,
        "reviewer": "",
        "operator_reason": "",
        "created_ts": 0.0,
        "timeout_s": 30.0,
        "expires_ts": 30.0,
    }
    defaults.update(overrides)
    return defaults


def _phase_payload(
    event_type: str = PENDING_EVENT, **proj_overrides,
) -> Dict[str, Any]:
    return {
        "event_type": event_type,
        "projection": _phase_projection(**proj_overrides),
    }


def _per_tool_payload(
    event_type: str = PENDING_EVENT, **proj_overrides,
) -> Dict[str, Any]:
    return {
        "event_type": event_type,
        "projection": _per_tool_projection(**proj_overrides),
    }


# ---------------------------------------------------------------------------
# Pure formatter — golden output
# ---------------------------------------------------------------------------


class TestFormatPhaseBoundaryBlock:
    def test_block_contains_header(self):
        block = format_phase_boundary_block(_phase_projection())
        assert PHASE_BOUNDARY_HEADER in block

    def test_block_contains_summary_target_op_rule(self):
        block = format_phase_boundary_block(_phase_projection(
            arg_preview="rename helper",
            target_path="backend/foo.py",
            op_id="op-x",
            verdict_rule_id="phase_boundary_inline_prompt",
        ))
        assert "rename helper" in block
        assert "backend/foo.py" in block
        assert "op-x" in block
        assert "phase_boundary_inline_prompt" in block

    def test_block_contains_actions_hint(self):
        block = format_phase_boundary_block(_phase_projection())
        assert PROMPT_ACTIONS_HINT in block

    def test_block_truncates_long_summary(self):
        long_summary = "x" * 500
        block = format_phase_boundary_block(_phase_projection(
            arg_preview=long_summary,
        ))
        # The rendered line has the (truncated) summary; the block
        # should not be unboundedly long.
        assert len(block) < 1000

    def test_block_truncates_long_prompt_id(self):
        long_id = "ipg-" + ("x" * 100)
        block = format_phase_boundary_block(_phase_projection(
            prompt_id=long_id,
        ))
        # Should not contain the full long_id verbatim.
        assert long_id not in block
        # Should contain the truncated form.
        assert long_id[:PROMPT_ID_DISPLAY_CHARS - 3] in block

    def test_block_renders_timeout_seconds(self):
        block = format_phase_boundary_block(_phase_projection(
            timeout_s=120.5,
        ))
        assert "120.5" in block

    def test_block_handles_missing_fields_gracefully(self):
        block = format_phase_boundary_block({})
        assert PHASE_BOUNDARY_HEADER in block
        assert "(unknown)" in block

    def test_block_handles_garbage_field_types(self):
        block = format_phase_boundary_block({
            "prompt_id": object(),
            "op_id": None,
            "target_path": 42,
            "timeout_s": "not-a-float",
        })
        # Must not raise; produces a degraded render.
        assert PHASE_BOUNDARY_HEADER in block

    def test_block_never_raises(self):
        # Most extreme case: pass garbage types
        garbage_inputs = [
            None, "", 42, [], object(),
        ]
        for g in garbage_inputs:
            try:
                out = format_phase_boundary_block(g)  # type: ignore[arg-type]
                # Should always produce SOMETHING
                assert PHASE_BOUNDARY_HEADER in out
            except Exception:
                pytest.fail(f"format raised on input {g!r}")


# ---------------------------------------------------------------------------
# Pure formatter — dismiss line
# ---------------------------------------------------------------------------


class TestFormatDismissLine:
    @pytest.mark.parametrize(
        "state, expected_verb",
        [
            ("allowed", "allowed"),
            ("denied", "denied"),
            ("expired", "expired"),
            ("paused", "paused"),
        ],
    )
    def test_dismiss_renders_state_verb(
        self, state: str, expected_verb: str,
    ):
        line = format_dismiss_line(_phase_projection(
            state=state, reviewer="repl",
        ))
        assert expected_verb in line
        assert PHASE_BOUNDARY_HEADER in line
        assert "reviewer=repl" in line

    def test_dismiss_unknown_state_renders_unknown(self):
        line = format_dismiss_line(_phase_projection(state=""))
        assert "(unknown)" in line

    def test_dismiss_includes_operator_reason(self):
        line = format_dismiss_line(_phase_projection(
            state="denied", operator_reason="touches credentials",
        ))
        assert "touches credentials" in line

    def test_dismiss_truncates_long_reason(self):
        long_reason = "x" * 200
        line = format_dismiss_line(_phase_projection(
            state="denied", operator_reason=long_reason,
        ))
        assert long_reason not in line
        # Ends with truncation marker
        assert "..." in line

    def test_dismiss_auto_reviewer_when_empty(self):
        line = format_dismiss_line(_phase_projection(
            state="expired", reviewer="",
        ))
        assert "reviewer=auto" in line

    def test_dismiss_never_raises(self):
        for g in [None, "", 42, [], object()]:
            try:
                line = format_dismiss_line(g)  # type: ignore[arg-type]
                assert PHASE_BOUNDARY_HEADER in line
            except Exception:
                pytest.fail(f"dismiss raised on input {g!r}")


# ---------------------------------------------------------------------------
# Phase-boundary projection filter
# ---------------------------------------------------------------------------


class TestPhaseBoundaryFilter:
    def test_phase_boundary_projection_recognized(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            # Manually fire a phase-boundary listener payload.
            for listener in controller._listeners:
                listener(_phase_payload())
            assert any(PHASE_BOUNDARY_HEADER in line for line in captured)
        finally:
            unsub()

    def test_per_tool_projection_filtered_out(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            for listener in controller._listeners:
                listener(_per_tool_payload())
            # No render — filtered.
            assert captured == []
        finally:
            unsub()

    def test_rule_id_sentinel_fallback_recognizes_phase_boundary(self):
        """If a payload has the rule_id sentinel even without the
        tool sentinel (defense-in-depth)."""
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            payload = {
                "event_type": PENDING_EVENT,
                "projection": _phase_projection(
                    tool="some_other_tool",
                    verdict_rule_id=PHASE_BOUNDARY_RULE_ID,
                ),
            }
            for listener in controller._listeners:
                listener(payload)
            assert any(PHASE_BOUNDARY_HEADER in line for line in captured)
        finally:
            unsub()


# ---------------------------------------------------------------------------
# Listener event semantics
# ---------------------------------------------------------------------------


class TestListenerEvents:
    def test_pending_event_renders_block(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            for listener in controller._listeners:
                listener(_phase_payload(event_type=PENDING_EVENT))
            assert len(captured) == 1
            assert PHASE_BOUNDARY_HEADER in captured[0]
        finally:
            unsub()

    def test_unknown_event_no_render(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            for listener in controller._listeners:
                listener(_phase_payload(event_type="unknown_event"))
            assert captured == []
        finally:
            unsub()

    @pytest.mark.parametrize("terminal_event", sorted(TERMINAL_EVENTS))
    def test_terminal_event_renders_dismiss(
        self, terminal_event: str,
    ):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            # Map event_type to projection state.
            event_state_map = {
                "inline_prompt_allowed": "allowed",
                "inline_prompt_denied": "denied",
                "inline_prompt_expired": "expired",
                "inline_prompt_paused": "paused",
            }
            state = event_state_map[terminal_event]
            payload = _phase_payload(
                event_type=terminal_event, state=state, reviewer="repl",
            )
            for listener in controller._listeners:
                listener(payload)
            assert len(captured) == 1
            line = captured[0]
            assert PHASE_BOUNDARY_HEADER in line
            assert TERMINAL_STATE_VERBS[state] in line
        finally:
            unsub()


# ---------------------------------------------------------------------------
# End-to-end through real InlinePromptController
# ---------------------------------------------------------------------------


class TestEndToEndController:
    def _register_phase_boundary_prompt(
        self, controller: InlinePromptController,
        prompt_id: str = "ipg-e2e",
    ) -> None:
        # Need a running event loop for controller.request to schedule
        # the timeout task. Run inside a coroutine.
        async def _do():
            req = PhaseInlinePromptRequest(
                prompt_id=prompt_id,
                op_id="op-e2e",
                phase_at_request="GATE",
                risk_tier="NOTIFY_APPLY",
                change_summary="end-to-end test prompt",
                change_fingerprint="a" * 64,
                target_paths=("backend/foo.py",),
            )
            bridged = bridge_to_controller_request(req)
            controller.request(bridged, timeout_s=30.0)
        asyncio.get_event_loop().run_until_complete(_do())

    @pytest.mark.asyncio
    async def test_pending_phase_boundary_prompt_renders(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            req = PhaseInlinePromptRequest(
                prompt_id="ipg-e2e-pending",
                op_id="op-e2e",
                phase_at_request="GATE",
                risk_tier="NOTIFY_APPLY",
                change_summary="end-to-end test prompt",
                change_fingerprint="a" * 64,
                target_paths=("backend/foo.py",),
            )
            bridged = bridge_to_controller_request(req)
            controller.request(bridged, timeout_s=30.0)
            # Yield once so listener fires.
            await asyncio.sleep(0)
            assert len(captured) == 1
            assert PHASE_BOUNDARY_HEADER in captured[0]
            assert "ipg-e2e-pending" in captured[0]
        finally:
            unsub()
            # Clean up the pending prompt.
            try:
                controller.deny("ipg-e2e-pending", reviewer="cleanup")
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_per_tool_call_prompt_does_not_render(self):
        """Per-tool-call prompts on the same singleton controller
        must NOT trigger the phase-boundary renderer."""
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            tool_req = InlinePromptRequest(
                prompt_id="tool-e2e",
                op_id="op-tool",
                call_id="call-1",
                tool="edit_file",
                arg_fingerprint="x" * 32,
                arg_preview="edit foo.py",
                target_path="backend/foo.py",
                verdict=InlineGateVerdict(
                    decision=InlineDecision.ASK,
                    rule_id="real_rule",
                    reason="real",
                ),
                rationale="model",
                route=RoutePosture.INTERACTIVE,
                upstream_decision=UpstreamPolicy.NO_MATCH,
            )
            controller.request(tool_req, timeout_s=30.0)
            await asyncio.sleep(0)
            assert captured == []
        finally:
            unsub()
            try:
                controller.deny("tool-e2e", reviewer="cleanup")
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_pending_then_terminal_renders_both(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            req = PhaseInlinePromptRequest(
                prompt_id="ipg-pt",
                op_id="op-pt",
                phase_at_request="GATE",
                risk_tier="NOTIFY_APPLY",
                change_summary="pending-then-terminal",
                change_fingerprint="b" * 64,
                target_paths=("backend/bar.py",),
            )
            bridged = bridge_to_controller_request(req)
            controller.request(bridged, timeout_s=30.0)
            controller.allow_once("ipg-pt", reviewer="ide")
            await asyncio.sleep(0)
            # First entry: pending block; second: dismiss line.
            assert len(captured) == 2
            assert "op-confirmation pending" in captured[0]
            assert "allowed" in captured[1]
            assert "reviewer=ide" in captured[1]
        finally:
            unsub()


# ---------------------------------------------------------------------------
# Defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveDegradation:
    def test_broken_print_cb_does_not_raise(self):
        controller = InlinePromptController(default_timeout_s=30.0)

        def _broken(text: str) -> None:
            raise RuntimeError("boom")

        unsub = attach_phase_boundary_renderer(
            _broken, controller=controller,
        )
        try:
            # Listener invocation must not raise even though print_cb
            # is broken — verifies inner safety net.
            for listener in controller._listeners:
                listener(_phase_payload())
            # Pass = no exception escaped.
        finally:
            unsub()

    def test_listener_handles_non_dict_payload(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            for listener in controller._listeners:
                listener("not-a-dict")  # type: ignore[arg-type]
            assert captured == []
        finally:
            unsub()

    def test_listener_handles_non_dict_projection(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            for listener in controller._listeners:
                listener({
                    "event_type": PENDING_EVENT,
                    "projection": "not-a-dict",
                })
            assert captured == []
        finally:
            unsub()


# ---------------------------------------------------------------------------
# attach / unsub semantics
# ---------------------------------------------------------------------------


class TestAttachUnsub:
    def test_attach_returns_callable(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        unsub = attach_phase_boundary_renderer(
            lambda x: None, controller=controller,
        )
        assert callable(unsub)
        unsub()

    def test_unsub_stops_further_renders(self):
        controller = InlinePromptController(default_timeout_s=30.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        # First fire — listener attached.
        for listener in list(controller._listeners):
            listener(_phase_payload())
        first_count = len(captured)
        assert first_count == 1
        # Unsubscribe.
        unsub()
        # Second fire — listener gone.
        for listener in list(controller._listeners):
            listener(_phase_payload())
        assert len(captured) == first_count

    def test_attach_uses_default_controller_when_none(self):
        """No explicit controller → defaults to singleton. Must not
        raise."""
        unsub = attach_phase_boundary_renderer(lambda x: None)
        try:
            assert callable(unsub)
        finally:
            unsub()


# ---------------------------------------------------------------------------
# Authority allowlist
# ---------------------------------------------------------------------------


class TestAuthorityAllowlist:
    def _renderer_source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "inline_prompt_gate_renderer.py"
        )
        return path.read_text()

    def test_imports_in_allowlist(self):
        """Slice 4 hot path allowlist; module-owned registration
        functions (``register_flags`` / ``register_shipped_invariants``)
        are STRUCTURALLY exempt — boot-time discovery only."""
        allowed = {
            "backend.core.ouroboros.governance.inline_permission_prompt",
            "backend.core.ouroboros.governance.inline_prompt_gate_runner",
        }
        tree = ast.parse(self._renderer_source())
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
                            f"Slice 4 imported module outside allowlist: "
                            f"{module!r} at line {lineno}"
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
        tree = ast.parse(self._renderer_source())
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
        tree = ast.parse(self._renderer_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 4 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )
