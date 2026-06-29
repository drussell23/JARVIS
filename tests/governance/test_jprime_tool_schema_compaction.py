"""Provider-aware tool-schema compaction -- cognitive armor for the 7B J-Prime.

The full tool section feeds Claude a large block (envelope examples + a
parallel-call essay + voice guidance + the tool list). A 7B model drowns in it
(cognitive load + context overflow). When the failover FSM reports J-Prime is the
active generator (``is_jprime_serving()``), the tool section is dynamically
compacted: verbose optional essays stripped, blank lines collapsed, hard
char-budget truncation. No hardcoded provider check -- the FSM IS the signal.
Gated + fail-soft.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.providers as providers


_FULL = (
    "Use the tool-call JSON envelope.\n\n"
    "### Parallel tool calls (preferred when tools are independent)\n"
    "A long essay about asyncio.gather and batching 3 sequential rounds into 1 "
    "that a 7B model does not need and that overflows its context window...\n"
    "...more verbose guidance...\n\n"
    "### Voice-First Prompt Mode (REQUIRED for this op)\n"
    "Spoken-language preamble guidance the failover node never speaks aloud...\n\n"
    "### Available tools\n\n"
    "- `read_file(path)` -- read a file\n"
    "- `search_code(pattern)` -- regex search\n"
)


def test_drops_verbose_essays_keeps_tools():
    out = providers.compact_tool_section(_FULL)
    assert "Parallel tool calls" not in out      # verbose essay stripped
    assert "Voice-First" not in out              # voice block stripped
    assert "### Available tools" in out          # essential list kept
    assert "read_file(path)" in out and "search_code(pattern)" in out


def test_collapses_excess_blank_lines():
    out = providers.compact_tool_section("a\n\n\n\n\nb")
    assert "\n\n\n" not in out


def test_truncates_to_budget(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_COMPACT_TOOL_MAX_CHARS", "120")
    out = providers.compact_tool_section("### Available tools\n" + ("- x\n" * 500))
    assert len(out) <= 200  # budget + a short truncation marker
    assert "truncated" in out.lower()


def test_failsoft_non_string():
    assert providers.compact_tool_section(None) is None  # type: ignore[arg-type]


def test_empty_passthrough():
    assert providers.compact_tool_section("") == ""


# ---------------------------------------------------------------------------
# Dynamic gate: compact iff the failover FSM says J-Prime is serving.
# ---------------------------------------------------------------------------

def test_compact_gate_off_when_jprime_not_serving(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_SCHEMA_SIMPLIFY_ENABLED", "true")
    import backend.core.ouroboros.governance.failover_lifecycle as fl

    class _Ctrl:
        def is_jprime_serving(self):
            return False
    monkeypatch.setattr(fl, "get_failover_controller", lambda: _Ctrl())
    assert providers._should_compact_for_jprime() is False


def test_compact_gate_on_when_jprime_serving(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_SCHEMA_SIMPLIFY_ENABLED", "true")
    import backend.core.ouroboros.governance.failover_lifecycle as fl

    class _Ctrl:
        def is_jprime_serving(self):
            return True
    monkeypatch.setattr(fl, "get_failover_controller", lambda: _Ctrl())
    assert providers._should_compact_for_jprime() is True


def test_compact_gate_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_VENOM_SCHEMA_SIMPLIFY_ENABLED", "false")
    import backend.core.ouroboros.governance.failover_lifecycle as fl
    monkeypatch.setattr(fl, "get_failover_controller",
                        lambda: type("C", (), {"is_jprime_serving": lambda s: True})())
    assert providers._should_compact_for_jprime() is False  # master gate wins
