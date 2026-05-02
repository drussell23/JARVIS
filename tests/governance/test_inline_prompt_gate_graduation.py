"""InlinePromptGate Slice 5 — graduation regression spine.

Verifies the full Slices 1-4 stack composes end-to-end after the
master flag flips default-true and dynamic registration discovers
all 4 modules' contributions.

Coverage:
  * Master flag default-true post-graduation
  * Master flag explicit false reverts to disabled
  * HTTP master flag stays default-false (operator-controlled
    cost ramp matches Move 6 pattern)
  * All 8 IPG flags discovered automatically by FlagRegistry
    seed loop
  * All 4 modules' register_shipped_invariants() discovered;
    9 IPG invariants registered
  * All 9 IPG invariants validate clean against current source
  * End-to-end happy path: producer → renderer → REPL allow →
    verdict ALLOW
  * End-to-end deny path stamps Phase C tightening
  * End-to-end pause path stamps Phase C tightening
  * End-to-end timeout path falls through to current behavior
  * Master-flag-off path: producer short-circuits to DISABLED;
    no controller call; no SSE emission; no rendering
  * Reuse contract: same controller singleton serves both
    phase-boundary and per-tool-call prompts; phase-boundary
    listener filters out per-tool-call events
"""
from __future__ import annotations

import asyncio
import uuid
from typing import List

import pytest

from backend.core.ouroboros.governance.flag_registry import FlagRegistry
from backend.core.ouroboros.governance.flag_registry_seed import (
    seed_default_registry,
)
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
    PhaseInlineVerdict,
    inline_prompt_gate_enabled,
)
from backend.core.ouroboros.governance.inline_prompt_gate_http import (
    inline_prompt_gate_http_enabled,
)
from backend.core.ouroboros.governance.inline_prompt_gate_renderer import (
    PHASE_BOUNDARY_HEADER,
    attach_phase_boundary_renderer,
)
from backend.core.ouroboros.governance.inline_prompt_gate_runner import (
    request_phase_inline_prompt,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_all,
)


def _request(**overrides) -> PhaseInlinePromptRequest:
    defaults: dict = {
        "prompt_id": "ipg-grad-" + uuid.uuid4().hex[:16],
        "op_id": "op-grad-" + uuid.uuid4().hex[:8],
        "phase_at_request": "GATE",
        "risk_tier": "NOTIFY_APPLY",
        "change_summary": "graduation test prompt",
        "change_fingerprint": "g" * 64,
        "target_paths": ("backend/foo.py",),
    }
    defaults.update(overrides)
    return PhaseInlinePromptRequest(**defaults)


# ---------------------------------------------------------------------------
# Master flag flip
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_producer_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_ENABLED", raising=False,
        )
        assert inline_prompt_gate_enabled() is True

    def test_producer_empty_string_is_default_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INLINE_PROMPT_GATE_ENABLED", "")
        assert inline_prompt_gate_enabled() is True

    def test_producer_whitespace_is_default_true(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INLINE_PROMPT_GATE_ENABLED", "   ")
        assert inline_prompt_gate_enabled() is True

    @pytest.mark.parametrize("falsy", ["0", "false", "no", "off", "FALSE"])
    def test_producer_explicit_false_disables(
        self, monkeypatch, falsy: str,
    ):
        monkeypatch.setenv("JARVIS_INLINE_PROMPT_GATE_ENABLED", falsy)
        assert inline_prompt_gate_enabled() is False

    def test_http_default_stays_false_post_graduation(self, monkeypatch):
        """HTTP write surface defers graduation per Move 6
        operator-controlled cost-ramp pattern."""
        monkeypatch.delenv(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED", raising=False,
        )
        assert inline_prompt_gate_http_enabled() is False


# ---------------------------------------------------------------------------
# Dynamic flag discovery
# ---------------------------------------------------------------------------


class TestFlagDiscovery:
    def test_seed_discovers_all_8_ipg_flags(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        ipg_flags = [
            f for f in registry.list_all()
            if "INLINE_PROMPT_GATE" in f.name
        ]
        assert len(ipg_flags) == 8

    def test_producer_4_flags_present(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        names = {f.name for f in registry.list_all()}
        assert "JARVIS_INLINE_PROMPT_GATE_ENABLED" in names
        assert "JARVIS_INLINE_PROMPT_GATE_TIMEOUT_S" in names
        assert (
            "JARVIS_INLINE_PROMPT_GATE_SUMMARY_MAX_CHARS" in names
        )
        assert (
            "JARVIS_INLINE_PROMPT_GATE_FINGERPRINT_HEX_CHARS" in names
        )

    def test_http_4_flags_present(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        names = {f.name for f in registry.list_all()}
        assert "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED" in names
        assert (
            "JARVIS_INLINE_PROMPT_GATE_HTTP_RATE_LIMIT_PER_MIN" in names
        )
        assert (
            "JARVIS_INLINE_PROMPT_GATE_HTTP_MAX_BODY_BYTES" in names
        )
        assert (
            "JARVIS_INLINE_PROMPT_GATE_HTTP_CORS_ORIGINS" in names
        )

    def test_master_flag_default_is_true_in_registry(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec("JARVIS_INLINE_PROMPT_GATE_ENABLED")
        assert spec is not None
        assert spec.default is True

    def test_http_master_flag_default_is_false_in_registry(self):
        registry = FlagRegistry()
        seed_default_registry(registry)
        spec = registry.get_spec(
            "JARVIS_INLINE_PROMPT_GATE_HTTP_ENABLED",
        )
        assert spec is not None
        assert spec.default is False


# ---------------------------------------------------------------------------
# Dynamic AST-pin discovery + clean validation
# ---------------------------------------------------------------------------


class TestInvariantDiscovery:
    def test_all_9_ipg_invariants_discovered(self):
        invs = list_shipped_code_invariants()
        ipg_invs = [
            i for i in invs
            if "inline_prompt_gate" in i.invariant_name
        ]
        assert len(ipg_invs) == 9

    def test_each_module_contributes_at_least_one_invariant(self):
        invs = list_shipped_code_invariants()
        ipg_names = {
            i.invariant_name for i in invs
            if "inline_prompt_gate" in i.invariant_name
        }
        # Slice 1 (3 invariants)
        assert "inline_prompt_gate_pure_stdlib" in ipg_names
        assert "inline_prompt_gate_taxonomy_5_values" in ipg_names
        assert "inline_prompt_gate_state_byte_parity" in ipg_names
        # Slice 2 (2 invariants)
        assert (
            "inline_prompt_gate_runner_sentinels_stable" in ipg_names
        )
        assert (
            "inline_prompt_gate_runner_authority_allowlist" in ipg_names
        )
        # Slice 3 (2 invariants)
        assert (
            "inline_prompt_gate_http_authority_allowlist" in ipg_names
        )
        assert (
            "inline_prompt_gate_http_verdict_vocabulary" in ipg_names
        )
        # Slice 4 (2 invariants)
        assert (
            "inline_prompt_gate_renderer_authority_allowlist" in ipg_names
        )
        assert (
            "inline_prompt_gate_renderer_visual_constants" in ipg_names
        )

    def test_all_ipg_invariants_validate_clean(self):
        violations = validate_all()
        ipg_v = [
            v for v in violations
            if "inline_prompt_gate" in v.invariant_name
        ]
        assert ipg_v == [], (
            f"IPG invariants drifted: {[(v.invariant_name, v.detail) for v in ipg_v]}"
        )


# ---------------------------------------------------------------------------
# End-to-end composition through full stack
# ---------------------------------------------------------------------------


class TestEndToEndComposition:
    @pytest.mark.asyncio
    async def test_e2e_allow_renders_block_resolves_to_allow(self):
        """Producer → controller → renderer fires → operator allow →
        producer returns ALLOW."""
        controller = InlinePromptController(default_timeout_s=10.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            req = _request()

            async def _resolve():
                # Wait for the renderer to fire, then allow.
                await asyncio.sleep(0.05)
                controller.allow_once(
                    req.prompt_id, reviewer="repl_operator",
                )

            resolver_task = asyncio.create_task(_resolve())
            verdict = await request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            )
            await resolver_task

            assert verdict.verdict is PhaseInlineVerdict.ALLOW
            # Renderer fired the pending block.
            assert any(
                PHASE_BOUNDARY_HEADER in line and "pending" in line
                for line in captured
            )
            # Renderer fired the dismiss line.
            assert any(
                PHASE_BOUNDARY_HEADER in line and "allowed" in line
                for line in captured
            )
        finally:
            unsub()

    @pytest.mark.asyncio
    async def test_e2e_deny_stamps_phase_c_tightening(self):
        controller = InlinePromptController(default_timeout_s=10.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            req = _request()

            async def _resolve():
                await asyncio.sleep(0.05)
                controller.deny(
                    req.prompt_id, reviewer="repl",
                    reason="touches credentials",
                )

            resolver_task = asyncio.create_task(_resolve())
            verdict = await request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            )
            await resolver_task

            assert verdict.verdict is PhaseInlineVerdict.DENY
            assert verdict.is_tightening is True
            assert verdict.monotonic_tightening_verdict == "passed"
            assert verdict.operator_reason == "touches credentials"
        finally:
            unsub()

    @pytest.mark.asyncio
    async def test_e2e_pause_stamps_phase_c_tightening(self):
        controller = InlinePromptController(default_timeout_s=10.0)
        unsub = attach_phase_boundary_renderer(
            lambda _: None, controller=controller,
        )
        try:
            req = _request()

            async def _resolve():
                await asyncio.sleep(0.05)
                controller.pause_op(
                    req.prompt_id, reviewer="ide", reason="hold",
                )

            resolver_task = asyncio.create_task(_resolve())
            verdict = await request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            )
            await resolver_task

            assert verdict.verdict is PhaseInlineVerdict.PAUSE_OP
            assert verdict.is_tightening is True
            assert verdict.monotonic_tightening_verdict == "passed"
        finally:
            unsub()

    @pytest.mark.asyncio
    async def test_e2e_timeout_falls_through_to_expired(self):
        """Controller's internal timeout fires EXPIRED — no Phase C
        tightening (operator did not respond; fall through to
        current behavior is the backward-compat path)."""
        controller = InlinePromptController(default_timeout_s=0.05)
        req = _request()
        verdict = await request_phase_inline_prompt(
            req, controller=controller, enabled=True, timeout_s=2.0,
        )
        assert verdict.verdict is PhaseInlineVerdict.EXPIRED
        assert verdict.is_tightening is False
        assert verdict.monotonic_tightening_verdict == ""

    @pytest.mark.asyncio
    async def test_e2e_master_off_disables_full_stack(self, monkeypatch):
        """Master flag off → producer DISABLED, no controller call,
        no SSE, no rendering. The whole stack stays inert."""
        monkeypatch.setenv("JARVIS_INLINE_PROMPT_GATE_ENABLED", "false")
        controller = InlinePromptController(default_timeout_s=10.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            req = _request()
            # Don't pass enabled — let the env-flag drive.
            verdict = await request_phase_inline_prompt(
                req, controller=controller,
            )
            assert verdict.verdict is PhaseInlineVerdict.DISABLED
            assert controller.pending_count == 0
            # Renderer never fired — the listener is attached but
            # no event was published because no prompt was registered.
            assert captured == []
        finally:
            unsub()


# ---------------------------------------------------------------------------
# Reuse contract — singleton coexistence
# ---------------------------------------------------------------------------


class TestReuseContract:
    @pytest.mark.asyncio
    async def test_phase_boundary_and_per_tool_coexist_on_singleton(self):
        """Per-tool-call prompts and phase-boundary prompts share the
        controller singleton without interference. The phase-boundary
        renderer filters out per-tool-call events."""
        controller = InlinePromptController(default_timeout_s=10.0)
        captured: List[str] = []
        unsub = attach_phase_boundary_renderer(
            captured.append, controller=controller,
        )
        try:
            # Register a per-tool-call prompt directly.
            tool_req = InlinePromptRequest(
                prompt_id="tool-coexist",
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
            controller.request(tool_req, timeout_s=10.0)
            await asyncio.sleep(0)
            # Renderer ignored the per-tool prompt.
            assert captured == []

            # Now register a phase-boundary prompt.
            req = _request(prompt_id="ipg-coexist")

            async def _resolve():
                await asyncio.sleep(0.05)
                controller.allow_once(req.prompt_id, reviewer="r")

            resolver_task = asyncio.create_task(_resolve())
            verdict = await request_phase_inline_prompt(
                req, controller=controller, enabled=True,
            )
            await resolver_task
            # Renderer fired only for the phase-boundary prompt.
            assert verdict.verdict is PhaseInlineVerdict.ALLOW
            assert any(
                PHASE_BOUNDARY_HEADER in line and "ipg-coexist" in line
                for line in captured
            )
            # No per-tool prompt rendering.
            assert not any(
                "tool-coexist" in line for line in captured
            )
        finally:
            unsub()
            try:
                controller.deny("tool-coexist", reviewer="cleanup")
            except Exception:
                pass
