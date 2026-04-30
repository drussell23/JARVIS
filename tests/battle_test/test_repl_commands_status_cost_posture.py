"""UI Slice 5 — /status, /cost, /posture REPL commands.

Pins the contract that the three new REPL commands:
  * Emit inline output (no Rich Panel, no fixed-region container).
  * Are dispatched both with and without leading slash for parity
    with existing slash commands (``/risk``, ``/budget``, etc.).
  * Consume the preserved ``status_line.py`` data layer (per
    operator directive: "Do not duplicate state-gathering code").
  * Render usable text even when underlying surfaces are absent.
  * Are listed in ``/help`` output so operators can discover them.

Authority Invariant
-------------------
Tests import only from the modules under test + stdlib + Rich
(Console for output capture). No orchestrator / phase_runners /
iron_gate.
"""
from __future__ import annotations

import io
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Bytes pins on dispatch + help
# -----------------------------------------------------------------------


def _serpent_flow_src() -> str:
    return pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()


def test_status_dispatch_accepts_both_forms():
    src = _serpent_flow_src()
    # Both `status` and `/status` should route to _print_status
    assert 'if line in ("status", "/status"):' in src


def test_cost_dispatch_accepts_both_forms():
    src = _serpent_flow_src()
    assert 'if line in ("cost", "/cost"):' in src


def test_posture_dispatch_accepts_both_forms():
    src = _serpent_flow_src()
    assert 'if line in ("posture", "/posture"):' in src


def test_help_lists_the_three_commands():
    src = _serpent_flow_src()
    # The help output must mention all three slash commands
    assert "/status" in src
    assert "/cost" in src
    assert "/posture" in src


def test_print_status_no_rich_panel():
    """The status command must not wrap output in Rich Panel."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def _print_status(self)")
    assert fn_idx > 0
    end_idx = src.find("    def _print_cost(self)", fn_idx)
    assert end_idx > fn_idx
    body = src[fn_idx:end_idx]
    assert "Panel(" not in body, (
        "_print_status must not use Rich Panel — Slice 5 retires the "
        "boxed status surface in favor of inline output"
    )
    # Must use console.print directly (via the ``f = self._flow`` alias)
    assert body.count("f.console.print") >= 4


def test_print_cost_no_rich_panel():
    src = _serpent_flow_src()
    fn_idx = src.find("    def _print_cost(self)")
    assert fn_idx > 0
    end_idx = src.find("    def _print_posture(self)", fn_idx)
    assert end_idx > fn_idx
    body = src[fn_idx:end_idx]
    assert "Panel(" not in body
    # Cost command must consume route_costs from SerpentFlow
    assert "_route_costs" in body


def test_print_posture_consumes_existing_store():
    """Slice 5 must NOT duplicate posture state-gathering — must
    consume the existing ``posture_observer.get_default_store``."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def _print_posture(self)")
    assert fn_idx > 0
    # Bound the search to just this method body — find the next
    # method def to terminate.
    end_idx = src.find("\n    def ", fn_idx + 1)
    if end_idx < 0:
        end_idx = fn_idx + 5000
    body = src[fn_idx:end_idx]
    assert "posture_observer" in body
    assert "get_default_store" in body
    assert "load_current()" in body
    assert "Panel(" not in body


def test_print_status_consumes_status_line_builder():
    """Slice 5 must consume the preserved status_line.py data layer
    (no duplicated state aggregation per operator directive)."""
    src = _serpent_flow_src()
    fn_idx = src.find("    def _print_status(self)")
    end_idx = src.find("    def _print_cost(self)", fn_idx)
    body = src[fn_idx:end_idx]
    assert "get_status_line_builder" in body
    assert "render_plain" in body


# -----------------------------------------------------------------------
# § B — Behavioral: capture rendered output
# -----------------------------------------------------------------------


def _make_flow_and_repl():
    """Construct a SerpentFlow + SerpentREPL pair with a captured
    Console for output assertions."""
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow, SerpentREPL,
    )
    flow = SerpentFlow(
        session_id="bt-slice5-test",
        branch_name="ouroboros/ui/repl-unification",
        cost_cap_usd=2.50,
        idle_timeout_s=3600.0,
    )
    buf = io.StringIO()
    flow.console = Console(
        file=buf, force_terminal=False, width=120, color_system=None,
    )
    repl = SerpentREPL(flow=flow)
    return flow, repl, buf


def test_print_status_emits_inline_no_panel_glyphs():
    flow, repl, buf = _make_flow_and_repl()
    flow._completed = 5
    flow._failed = 1
    flow._cost_total = 0.043
    flow._sensors_active = 16
    repl._print_status()
    out = buf.getvalue()
    # Identity + counters surfaced inline
    assert "Organism Status" in out
    assert "bt-slice5-test" in out
    assert "5" in out  # _completed
    assert "$0.0430" in out
    assert "16" in out  # sensors
    # No Panel border glyphs
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_print_cost_emits_inline_no_panel_glyphs():
    flow, repl, buf = _make_flow_and_repl()
    flow._cost_total = 0.125
    repl._print_cost()
    out = buf.getvalue()
    assert "Cost" in out
    assert "$0.1250" in out
    assert "$2.50" in out
    # Percentage shown
    assert "%" in out
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_print_cost_handles_no_route_data():
    flow, repl, buf = _make_flow_and_repl()
    # No route_costs populated
    repl._print_cost()
    out = buf.getvalue()
    # Must emit a clean "no samples yet" line, not crash
    assert "No route-level cost samples yet" in out


def test_print_posture_handles_missing_store_gracefully():
    """When the PostureStore has no current reading, the command
    must emit a clean 'no reading yet' line — not crash, not empty."""
    flow, repl, buf = _make_flow_and_repl()
    # Force the store to return None by patching at module level
    import backend.core.ouroboros.governance.posture_observer as ps
    original_get = ps.get_default_store

    class _EmptyStore:
        def load_current(self):
            return None

    ps.get_default_store = lambda *a, **kw: _EmptyStore()
    try:
        repl._print_posture()
    finally:
        ps.get_default_store = original_get
    out = buf.getvalue()
    assert "Posture" in out
    assert "no reading yet" in out or "unavailable" in out
    for glyph in ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘"):
        assert glyph not in out


def test_print_posture_handles_store_exception_gracefully():
    """If the store raises, the command must emit a clear unavailable
    line, not propagate the exception."""
    flow, repl, buf = _make_flow_and_repl()
    import backend.core.ouroboros.governance.posture_observer as ps
    original_get = ps.get_default_store

    def _raising_get_default(*a, **kw):
        raise RuntimeError("simulated store failure")

    ps.get_default_store = _raising_get_default
    try:
        repl._print_posture()
    finally:
        ps.get_default_store = original_get
    out = buf.getvalue()
    assert "Posture" in out
    assert "unavailable" in out


# -----------------------------------------------------------------------
# § C — Authority invariant
# -----------------------------------------------------------------------


def test_test_module_no_orchestrator_imports():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "candidate_generator", "providers", "orchestrator",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"
