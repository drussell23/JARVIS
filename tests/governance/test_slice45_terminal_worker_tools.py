"""Slice 45 — DW terminal-worker tool advertisement.

Root cause closed (v40b bt-2026-05-29-200702): a predicate mismatch between
the prompt layer (``providers._build_tool_section`` suppressed tools for
``should_skip_venom_for_route`` routes) and the exec layer
(``doubleword_provider`` ran the Venom loop for any non-trivial op). A
non-trivial BACKGROUND op therefore ran the tool loop against a prompt that
hid the tool advertisements -> 0 tool calls -> Iron Gate
``exploration_insufficient: 0/1`` -> deadlock.

Phase 1 trace (scripts/trace_qwen_tool_syntax.py) proved Qwen-397B emits a
flawless ``2b.2-tool`` envelope the instant tools are advertised, so the fix
is purely to stop suppressing tools for the terminal-worker BACKGROUND route
(Claude disabled). Env-gated + BACKGROUND-only -> byte-identical legacy when
Claude is enabled or the master flag is off; SPECULATIVE / WIRING_VALIDATION
stay suppressed.

These tests pin:
  1. the policy predicate behavior matrix;
  2. ``_build_tool_section`` advertises iff terminal-worker;
  3. the Slice 12AF legacy suppression is preserved by default;
  4. AST pins: scope did not expand; both call sites consult the policy.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.dw_terminal_worker_policy import (
    MASTER_FLAG,
    TERMINAL_WORKER_ROUTE,
    background_is_terminal_worker,
    claude_is_disabled,
)
from backend.core.ouroboros.governance.providers import (
    _build_lean_codegen_prompt,
    _build_tool_section,
    _should_use_lean_prompt,
)


def _mk_ctx(route: str, complexity: str = "moderate"):
    """Duck-typed minimal OperationContext (matches test_slice12af pattern)."""
    c = MagicMock()
    c.provider_route = route
    c.task_complexity = complexity
    c.cross_repo = False
    c.target_files = ()
    c.repair_context = None
    return c

_REPO = Path(__file__).resolve().parents[2]
_PROVIDERS = _REPO / "backend/core/ouroboros/governance/providers.py"
_DW = _REPO / "backend/core/ouroboros/governance/doubleword_provider.py"
_POLICY = _REPO / "backend/core/ouroboros/governance/dw_terminal_worker_policy.py"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test controls the two flags explicitly; start from a clean slate."""
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.delenv(MASTER_FLAG, raising=False)
    yield


# ── 1. policy predicate matrix ──────────────────────────────────────────


def test_claude_is_disabled_reads_env(monkeypatch):
    assert claude_is_disabled() is False
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert claude_is_disabled() is True
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "0")
    assert claude_is_disabled() is False


def test_legacy_background_not_terminal_worker():
    # No CLAUDE_DISABLED -> Claude active -> background is the cheap pre-pass.
    assert background_is_terminal_worker("background") is False


def test_claude_disabled_background_is_terminal_worker(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert background_is_terminal_worker("background") is True


def test_master_flag_off_disables_even_when_claude_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.setenv(MASTER_FLAG, "false")
    assert background_is_terminal_worker("background") is False


def test_scope_is_background_only(monkeypatch):
    # Even with DW terminal, SPECULATIVE / WIRING_VALIDATION / others stay out.
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    for route in ("speculative", "wiring_validation", "standard", "immediate", "complex", ""):
        assert background_is_terminal_worker(route) is False, route


def test_predicate_never_raises_on_odd_input(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert background_is_terminal_worker("BACKGROUND") is False  # case-sensitive by design
    assert background_is_terminal_worker(" background ") is False


# ── 2. _build_tool_section advertises iff terminal-worker ────────────────


def test_tool_section_advertises_for_terminal_worker_background(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    section = _build_tool_section(provider_route="background")
    assert "## Available Tools" in section
    assert "2b.2-tool" in section
    # The exploration tools the Iron Gate counts must be present.
    assert "read_file" in section and "search_code" in section


def test_tool_section_speculative_still_suppressed_when_terminal(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert _build_tool_section(provider_route="speculative") == ""
    assert _build_tool_section(provider_route="wiring_validation") == ""


# ── 3. Slice 12AF legacy suppression preserved by default ───────────────


def test_legacy_background_section_empty_without_claude_disabled():
    # The exact Slice 12AF invariant — unchanged when Claude is enabled.
    assert _build_tool_section(provider_route="background") == ""
    assert _build_tool_section(provider_route="speculative") == ""


def test_master_off_restores_legacy_suppression(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.setenv(MASTER_FLAG, "false")
    assert _build_tool_section(provider_route="background") == ""


def test_standard_route_always_advertises():
    # Regression: non-skip routes are unaffected by this slice.
    section = _build_tool_section(provider_route="standard")
    assert "## Available Tools" in section and "2b.2-tool" in section


# ── 4. AST / structural pins ────────────────────────────────────────────


def test_terminal_worker_route_constant_is_background():
    # Scope guard: an accidental widening to speculative/wiring would
    # change product behavior silently.
    assert TERMINAL_WORKER_ROUTE == "background"


def test_build_tool_section_consults_policy():
    src = _PROVIDERS.read_text(encoding="utf-8")
    assert "background_is_terminal_worker" in src, (
        "_build_tool_section must consult the terminal-worker policy"
    )


def test_doubleword_provider_consults_policy():
    src = _DW.read_text(encoding="utf-8")
    assert "background_is_terminal_worker" in src, (
        "doubleword_provider _will_skip_tools must consult the policy"
    )


# ── 5. END-TO-END production prompt-assembly (the path v40b actually took) ─
#
# The component tests above prove _build_tool_section advertises, but the
# v40b deadlock lived in the PROMPT-SELECTION wiring: _should_use_lean_prompt
# returned False for background -> full prompt -> ``if tools_enabled:`` gate
# (DW call site defaults tools_enabled=False) -> _build_tool_section never
# called. These tests drive the real selection + assembly path so a future
# regression of that wiring is caught.


def test_lean_selected_for_terminal_worker_background(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.delenv("JARVIS_LEAN_PROMPT", raising=False)
    monkeypatch.delenv("JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED", raising=False)
    for comp in ("simple", "moderate", "heavy"):
        assert _should_use_lean_prompt(
            _mk_ctx("background", comp), tools_enabled=True
        ) is True, comp


def test_lean_not_selected_for_background_when_claude_enabled(monkeypatch):
    # Legacy invariant: Claude active -> background still skips the lean
    # tool-first prompt (byte-identical pre-Slice-45).
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.delenv("JARVIS_BG_CASCADE_LEAN_PROMPT_ENABLED", raising=False)
    assert _should_use_lean_prompt(
        _mk_ctx("background", "moderate"), tools_enabled=True
    ) is False


def test_lean_not_selected_for_speculative_terminal(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    assert _should_use_lean_prompt(
        _mk_ctx("speculative", "moderate"), tools_enabled=True
    ) is False


def test_assembled_lean_prompt_advertises_tools_for_terminal_background(monkeypatch):
    """THE end-to-end pin: the real assembled prompt the model receives for a
    Claude-disabled background op must contain the tool advertisements."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.delenv("JARVIS_LEAN_PROMPT", raising=False)
    prompt = _build_lean_codegen_prompt(_mk_ctx("background", "moderate"), preloaded_out=[])
    assert "## Available Tools" in prompt
    assert "2b.2-tool" in prompt
    assert "read_file" in prompt and "search_code" in prompt


def test_assembled_lean_prompt_no_tools_for_background_legacy(monkeypatch):
    """Legacy: Claude enabled -> even if the lean builder is reached
    directly, the tool section stays suppressed for background."""
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    prompt = _build_lean_codegen_prompt(_mk_ctx("background", "moderate"), preloaded_out=[])
    assert "## Available Tools" not in prompt


def test_policy_module_is_a_leaf_no_governance_imports():
    """The policy must stay a dependency-free leaf (env reads only) so both
    providers.py and doubleword_provider.py import it without circular risk."""
    tree = ast.parse(_POLICY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (
                node.module
                if isinstance(node, ast.ImportFrom)
                else node.names[0].name
            )
            assert mod is None or "ouroboros.governance" not in str(mod), (
                f"leaf policy must not import governance modules; found {mod}"
            )
