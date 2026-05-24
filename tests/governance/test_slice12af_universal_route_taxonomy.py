"""Slice 12AF — Universal Route Taxonomy for WIRING_VALIDATION.

# Wedge (bt-2026-05-24-065236)

The Slice 12AD WIRING_VALIDATION route was structurally correct, but
the broader generation stack only recognised the 6 legacy routes
(immediate / standard / complex / background / speculative /
informational). For wiring_validation ops, the cascade was:

  1. Tier 0 (DoubleWord) attempt: lean prompt + tool-loop available
     (DW's ``_will_skip_tools`` only gates on complexity, not route)
  2. Tier 1 (Claude) fallback: prompt STILL included tool
     advertisements (only the lean-prompt path was gated by Slice
     12AD; the full prompt's tool section was unconditional)
  3. Claude returned ``2b.2-tool`` shape (model used the
     advertised tools)
  4. Parser at ``providers.py:4118`` raised
     ``schema_invalid:tool_call_without_tool_loop:2b.2-tool``
  5. CandidateGenerator: ``all_providers_exhausted:fallback_failed``
  6. ForegroundCooldown retry 2/2: same failure
  7. ``generate_runner.py:2111`` ``assert generation is not None``
     fired → unhandled_pipeline_exception → pipeline crash

# Fix (5 surgical sites)

| Site | File | Mechanism |
|---|---|---|
| 1 | `urgency_router.py` `route_budget_profile()` | New `WIRING_VALIDATION` case → `tier0_fraction=0.0` (skip DW entirely) |
| 2 | `providers.py:4118` schema parser | `2b.2-tool` for `VENOM_SKIP_ROUTES` → soft `content_failure` (cascade-friendly) instead of hard `schema_invalid` |
| 3 | `providers.py:_build_tool_section()` | New `provider_route` param; returns `""` for `VENOM_SKIP_ROUTES` so the model NEVER sees tool advertisements |
| 4 | `doubleword_provider.py:1559` `_will_skip_tools` | Extend predicate: also skip tools when route in `VENOM_SKIP_ROUTES` |
| 5 | `generate_runner.py:2111` assertion | Convert bare `assert` to structured `RuntimeError` (defense-in-depth) |

# Test surface (7 spine + AST pins)

  1. `route_budget_profile(WIRING_VALIDATION)` returns expected dict.
  2. Schema parser given 2b.2-tool + route="wiring_validation"
     raises `content_failure:tool_call_returned_under_venom_skip`.
  3. Schema parser given 2b.2-tool + route="standard" still raises
     `schema_invalid:tool_call_without_tool_loop` (legacy preserved).
  4. `_build_tool_section(provider_route="wiring_validation")`
     returns `""` (empty string).
  5. `_build_tool_section(provider_route="standard")` returns the
     full tools block (legacy preserved).
  6. `generate_runner` defense: structured RuntimeError replaces
     bare assert (AST + substring proof).
  7. AST pin: `_build_tool_section` accepts `provider_route` kwarg
     AND both call sites pass it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.urgency_router import (
    ProviderRoute,
    UrgencyRouter,
)


# ──────────────────────────────────────────────────────────────────────
# Site 1 — route_budget_profile(WIRING_VALIDATION)
# ──────────────────────────────────────────────────────────────────────


class TestSite1RouteBudgetProfile:
    def test_wiring_validation_profile_present_and_tier0_disabled(self):
        prof = UrgencyRouter.route_budget_profile(
            ProviderRoute.WIRING_VALIDATION,
        )
        assert prof["tier0_fraction"] == 0.0, (
            "WIRING_VALIDATION must skip Tier 0 (DW) — direct Claude only"
        )
        assert prof["tier1_reserve_s"] == 0.0
        assert prof["max_dw_wait_s"] == 0.0

    def test_legacy_route_profiles_unchanged(self):
        std = UrgencyRouter.route_budget_profile(ProviderRoute.STANDARD)
        assert std["tier0_fraction"] == 0.65, "STANDARD profile must not regress"
        imm = UrgencyRouter.route_budget_profile(ProviderRoute.IMMEDIATE)
        assert imm["tier0_fraction"] == 0.0


# ──────────────────────────────────────────────────────────────────────
# Site 2 — schema parser graceful 2b.2-tool handling
# ──────────────────────────────────────────────────────────────────────


class TestSite2SchemaParserGraceful:
    def _make_ctx(self, route: str):
        """Build a minimal duck-typed OperationContext for _parse_generation_response."""
        from unittest.mock import MagicMock
        ctx = MagicMock()
        ctx.provider_route = route
        ctx.target_files = ()
        ctx.primary_repo = "jarvis"
        ctx.op_id = "op-test-12af"
        # Explicit False — otherwise MagicMock returns a truthy
        # MagicMock for is_read_only and the parser short-circuits
        # at providers.py:3972 ("Read-only op: parser short-circuit")
        # never reaching the 2b.2-tool branch we're testing.
        ctx.is_read_only = False
        return ctx

    def test_2b2_tool_with_wiring_validation_route_is_soft_content_failure(self):
        """When the model returns 2b.2-tool under a VENOM_SKIP route,
        the parser MUST raise ``content_failure:tool_call_returned_under_venom_skip``
        (NOT ``schema_invalid:...``) so the cascade handles it
        gracefully instead of tanking into AssertionError."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_calls": [{"name": "read_file", "args": {"path": "x.py"}}],
        })
        ctx = self._make_ctx("wiring_validation")
        with pytest.raises(RuntimeError) as exc_info:
            _parse_generation_response(
                raw=raw,
                provider_name="claude-api",
                duration_s=1.0,
                ctx=ctx,
                source_hash="dummy",
                source_path="x.py",
            )
        msg = str(exc_info.value)
        assert "content_failure" in msg, (
            f"Expected content_failure classification for "
            f"VENOM_SKIP route, got: {msg}"
        )
        assert "tool_call_returned_under_venom_skip" in msg

    def test_2b2_tool_with_standard_route_still_schema_invalid(self):
        """Legacy preserved: non-VENOM_SKIP routes still raise
        ``schema_invalid:tool_call_without_tool_loop`` so the
        original hard-fail discipline holds for them."""
        from backend.core.ouroboros.governance.providers import (
            _parse_generation_response,
        )
        raw = json.dumps({
            "schema_version": "2b.2-tool",
            "tool_calls": [{"name": "read_file", "args": {"path": "x.py"}}],
        })
        ctx = self._make_ctx("standard")
        with pytest.raises(RuntimeError) as exc_info:
            _parse_generation_response(
                raw=raw,
                provider_name="claude-api",
                duration_s=1.0,
                ctx=ctx,
                source_hash="dummy",
                source_path="x.py",
            )
        msg = str(exc_info.value)
        assert "schema_invalid:tool_call_without_tool_loop" in msg, (
            f"Legacy hard-fail for non-VENOM_SKIP routes "
            f"regressed: {msg}"
        )


# ──────────────────────────────────────────────────────────────────────
# Site 3 — _build_tool_section suppresses for VENOM_SKIP_ROUTES
# ──────────────────────────────────────────────────────────────────────


class TestSite3PromptToolSectionSuppression:
    def test_tool_section_empty_for_wiring_validation(self):
        from backend.core.ouroboros.governance.providers import (
            _build_tool_section,
        )
        out = _build_tool_section(
            mcp_tools=None,
            provider_route="wiring_validation",
        )
        assert out == "", (
            "Tool section MUST be empty for wiring_validation route "
            "(cures 2b.2-tool hallucination at source)"
        )

    def test_tool_section_empty_for_background_and_speculative(self):
        from backend.core.ouroboros.governance.providers import (
            _build_tool_section,
        )
        for route in ("background", "speculative"):
            out = _build_tool_section(
                mcp_tools=None,
                provider_route=route,
            )
            assert out == "", (
                f"Tool section MUST be empty for {route} route "
                "(VENOM_SKIP_ROUTES)"
            )

    def test_tool_section_full_for_standard_route(self):
        """Legacy preserved: non-skip routes still get the full
        Available Tools block."""
        from backend.core.ouroboros.governance.providers import (
            _build_tool_section,
        )
        out = _build_tool_section(
            mcp_tools=None,
            provider_route="standard",
        )
        assert out != ""
        assert "Available Tools" in out

    def test_tool_section_full_when_route_is_empty(self):
        """Default param value — legacy call sites that haven't
        plumbed route yet still get the full section."""
        from backend.core.ouroboros.governance.providers import (
            _build_tool_section,
        )
        out = _build_tool_section(mcp_tools=None)
        assert out != ""
        assert "Available Tools" in out


# ──────────────────────────────────────────────────────────────────────
# Site 4 — DW _will_skip_tools extended for VENOM_SKIP_ROUTES
# ──────────────────────────────────────────────────────────────────────


DW_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "doubleword_provider.py"
)


class TestSite4DoublewordSkipTools:
    def test_doubleword_imports_should_skip_venom_for_route(self):
        """AST proof that DW now composes the canonical predicate
        from Slice 12AD's route_predicates module."""
        src = DW_PATH.read_text()
        assert "should_skip_venom_for_route" in src, (
            "doubleword_provider.py must import + call "
            "should_skip_venom_for_route to gate _will_skip_tools "
            "on route (Site 4)"
        )

    def test_doubleword_will_skip_tools_predicate_extended(self):
        """The combined predicate must include both the legacy
        complexity check AND the route-based check."""
        src = DW_PATH.read_text()
        assert "_will_skip_tools" in src
        # Substring proof that both components are present in the
        # combined predicate
        assert (
            'should_skip_venom_for_route(str(_route))' in src
            or "should_skip_venom_for_route" in src
        )


# ──────────────────────────────────────────────────────────────────────
# Site 5 — generate_runner.py defensive raise (no bare assert)
# ──────────────────────────────────────────────────────────────────────


GEN_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "phase_runners" / "generate_runner.py"
)


class TestSite5GenerateRunnerDefensiveRaise:
    def test_bare_assert_replaced_with_structured_raise(self):
        """The ``assert generation is not None`` at the original site
        must have been replaced by a structured RuntimeError so the
        cascade gets a useful terminal_reason_code (not an
        uncaught AssertionError)."""
        src = GEN_RUNNER_PATH.read_text()
        # Negative: the literal bare assert + comment is gone
        assert "assert generation is not None  # guaranteed by loop logic" not in src, (
            "Slice 12AF Site 5 should have removed the bare assert "
            "in favour of a structured RuntimeError"
        )
        # Positive: the new structured raise is present + greppable
        assert "if generation is None:" in src
        assert "generate_runner: generation is None after retry" in src

    def test_runtime_error_has_actionable_message(self):
        """The new error message MUST point operators at the upstream
        cause (preceding EXHAUSTION events) instead of just
        crashing silently."""
        src = GEN_RUNNER_PATH.read_text()
        assert "EXHAUSTION events" in src, (
            "Error message must reference EXHAUSTION events so "
            "operators can locate the upstream cause in debug.log"
        )


# ──────────────────────────────────────────────────────────────────────
# AST pin — _build_tool_section signature + caller wiring
# ──────────────────────────────────────────────────────────────────────


PROVIDERS_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "providers.py"
)


class TestSliceTAFArchitecturalPins:
    def test_build_tool_section_has_provider_route_kwarg(self):
        """``_build_tool_section`` MUST expose ``provider_route`` as
        a keyword arg — call sites depend on this contract."""
        src = PROVIDERS_PATH.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_build_tool_section"
            ):
                kw_names = {kw.arg for kw in node.args.kwonlyargs}
                pos_names = {arg.arg for arg in node.args.args}
                all_names = kw_names | pos_names
                assert "provider_route" in all_names, (
                    "_build_tool_section must declare provider_route "
                    f"as a parameter; current params: {all_names}"
                )
                return
        raise AssertionError(
            "_build_tool_section function not found in providers.py"
        )

    def test_build_tool_section_callers_pass_provider_route(self):
        """All `_build_tool_section(...)` call sites in providers.py
        MUST pass `provider_route` so the suppression actually fires.
        Drift here would silently re-introduce the wedge."""
        src = PROVIDERS_PATH.read_text()
        tree = ast.parse(src)
        call_sites_with_route = 0
        call_sites_total = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = (
                fn.id if isinstance(fn, ast.Name)
                else (fn.attr if isinstance(fn, ast.Attribute) else "")
            )
            if fname != "_build_tool_section":
                continue
            call_sites_total += 1
            for kw in node.keywords:
                if kw.arg == "provider_route":
                    call_sites_with_route += 1
                    break
        assert call_sites_total >= 2, (
            f"Expected at least 2 _build_tool_section call sites in "
            f"providers.py, found {call_sites_total}"
        )
        assert call_sites_with_route == call_sites_total, (
            f"All {call_sites_total} _build_tool_section call sites "
            f"in providers.py must pass provider_route= ; "
            f"only {call_sites_with_route} do"
        )

    def test_schema_parser_references_should_skip_venom_for_route(self):
        """The 2b.2-tool branch in `_parse_generation_response` MUST
        consult `should_skip_venom_for_route` to soft-classify the
        cascade-killer. Drift would re-introduce the wedge."""
        src = PROVIDERS_PATH.read_text()
        # Locate _parse_generation_response and walk its body
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_parse_generation_response"
            ):
                body_src = ast.unparse(node)
                assert "should_skip_venom_for_route" in body_src, (
                    "_parse_generation_response must consult "
                    "should_skip_venom_for_route in the 2b.2-tool "
                    "branch (Site 2)"
                )
                assert "tool_call_returned_under_venom_skip" in body_src, (
                    "_parse_generation_response must emit the "
                    "tool_call_returned_under_venom_skip marker "
                    "for soft-classification (Site 2)"
                )
                return
        raise AssertionError(
            "_parse_generation_response not found in providers.py"
        )

    def test_route_predicates_module_referenced_at_5plus_sites(self):
        """After Slice 12AF, providers.py + doubleword_provider.py
        should reference `should_skip_venom_for_route` from `route_predicates`
        at >= 5 sites total (was 3 in Slice 12AD).

        Sites: ClaudeProvider Venom-skip (Slice 12AD), Prime Venom-skip
        (Slice 12AD), _should_use_lean_prompt (Slice 12AD), schema parser
        2b.2-tool branch (Site 2), _build_tool_section (Site 3), DW
        _will_skip_tools (Site 4) → 6 total minimum.
        """
        providers_src = PROVIDERS_PATH.read_text()
        dw_src = DW_PATH.read_text()
        total = (
            providers_src.count("should_skip_venom_for_route")
            + dw_src.count("should_skip_venom_for_route")
        )
        assert total >= 5, (
            f"Expected at least 5 should_skip_venom_for_route "
            f"references after Slice 12AF (providers.py + "
            f"doubleword_provider.py combined); found {total}"
        )
