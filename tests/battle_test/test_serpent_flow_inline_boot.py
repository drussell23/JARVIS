"""SerpentFlow inline boot banner — UI Slice 4 regression.

Pins the contract that the boot banner is now plain inline output
(scrollable with the rest of the event stream), not a Rich ``Panel``
with borders / fixed width / fixed terminal region.

Why these tests exist:
  * The old banner used ``rich.panel.Panel(..., border_style="bold cyan",
    width=min(self.console.width, 72))`` which created a fixed-width
    boxed UI element. Slice 4 retires that in favor of plain
    ``console.print(line)`` calls so the banner scrolls naturally.
  * If a future refactor re-wraps the banner in a Panel (or any
    fixed-region container), these tests fail loudly.

Authority Invariant
-------------------
Tests import only from the module under test + stdlib + Rich (Console
for capture). No orchestrator / phase_runners / iron_gate.
"""
from __future__ import annotations

import io
import pathlib

import pytest


# -----------------------------------------------------------------------
# § A — Bytes pin: the boot banner code path uses no Rich Panel
# -----------------------------------------------------------------------


def _boot_banner_block_src() -> str:
    src = pathlib.Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    fn_idx = src.find("def boot_banner(")
    assert fn_idx > 0, "boot_banner method not found"
    # Walk forward to the next top-level def (or section break)
    # The next method after boot_banner is "def start" or
    # "    # Lifecycle" — find a stable terminator.
    end_idx = src.find("# Lifecycle", fn_idx)
    if end_idx < 0:
        end_idx = src.find("\n    async def ", fn_idx)
    assert end_idx > fn_idx, "could not locate boot_banner end"
    return src[fn_idx:end_idx]


def test_boot_banner_no_rich_panel():
    """The boot_banner method body MUST NOT instantiate a Rich Panel.
    The retirement of the boxed banner is the entire point of UI
    Slice 4 — re-introducing a Panel would re-create the fixed-region
    UI we just removed."""
    body = _boot_banner_block_src()
    assert "Panel(" not in body, (
        "boot_banner must not wrap output in rich.panel.Panel — UI Slice 4"
    )
    assert "border_style" not in body
    assert "width=" not in body or "console.width" not in body, (
        "boot_banner must not clamp width — output must scroll inline"
    )


def test_boot_banner_uses_console_print():
    """The new inline path emits via ``self.console.print(...)`` — at
    least 5 distinct print calls (header, identity block, layer
    header, footer line, log path). Bytes-pinned."""
    body = _boot_banner_block_src()
    print_count = body.count("self.console.print(")
    assert print_count >= 5, (
        f"expected ≥5 console.print calls in inline banner, got {print_count}"
    )


def test_boot_banner_keeps_layer_header_marker():
    """Operator-visible identity is preserved across the refactor.
    The 6-Layer Organism header line MUST remain so the banner
    continues to communicate the same architecture summary to
    operators."""
    body = _boot_banner_block_src()
    assert "6-Layer Organism" in body
    assert "OUROBOROS" in body or "OUROBOROS + VENOM" in body
    assert "Organism alive" in body


# -----------------------------------------------------------------------
# § B — Behavioral test: capture the rendered output
# -----------------------------------------------------------------------


def _render_banner_to_string(layers, n_sensors=0, log_path="") -> str:
    """Construct a SerpentFlow and replace its internal Console with a
    capture buffer, then run boot_banner. Returns the captured text.

    SerpentFlow builds its own Console at __init__ (uses force_terminal
    to survive prompt_toolkit's stdout proxy). We swap it post-init for
    a buffer-backed Console so the test can assert against rendered
    output."""
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import (
        SerpentFlow,
    )
    flow = SerpentFlow(
        session_id="bt-test-001",
        branch_name="ouroboros/ui/repl-unification",
        cost_cap_usd=2.50,
        idle_timeout_s=3600.0,
    )
    buf = io.StringIO()
    flow.console = Console(
        file=buf, force_terminal=False, width=120, color_system=None,
    )
    flow.boot_banner(layers=layers, n_sensors=n_sensors, log_path=log_path)
    return buf.getvalue()


def test_banner_renders_identity_block_inline():
    out = _render_banner_to_string(
        layers=[("🧠", "Cognitive", True, "claude+dw")],
        n_sensors=16,
        log_path=".ouroboros/sessions/bt-test-001/debug.log",
    )
    # Identity surfaced
    assert "OUROBOROS" in out
    assert "bt-test-001" in out
    assert "ouroboros/ui/repl-unification" in out
    assert "$2.50" in out
    assert "3600s" in out
    # Layer block present
    assert "6-Layer Organism" in out
    assert "Cognitive" in out
    # Footer
    assert "Organism alive" in out
    assert "16 sensors" in out
    assert ".ouroboros/sessions/bt-test-001/debug.log" in out


def test_banner_emits_no_box_drawing_panel_borders():
    """Confirms the banner output contains NO Rich Panel border
    characters (╭ ╰ │ ╮ ╯) on the OUTSIDE of the content. The body
    of the layer status uses Unicode separators (── etc.) which is
    fine — those are inline horizontal rules, not a fixed-region
    box. We assert the corner glyphs are absent."""
    out = _render_banner_to_string(
        layers=[("🔧", "Transport", True, "httpx limits")],
    )
    # Rich Panel uses these corner glyphs by default
    panel_corner_glyphs = ("╭", "╰", "╮", "╯", "┌", "└", "┐", "┘")
    for glyph in panel_corner_glyphs:
        assert glyph not in out, (
            f"banner should not contain Panel corner glyph {glyph!r} — "
            "Slice 4 retires the boxed banner"
        )


def test_banner_omits_log_line_when_unset():
    out = _render_banner_to_string(
        layers=[("🧠", "Cognitive", True, "claude+dw")],
        log_path="",
    )
    # The log emoji should not appear when path is blank
    assert "📝" not in out


def test_banner_omits_sensor_line_when_zero():
    out = _render_banner_to_string(
        layers=[("🧠", "Cognitive", True, "claude+dw")],
        n_sensors=0,
    )
    # Sensor count should not appear when zero
    assert " sensors" not in out
