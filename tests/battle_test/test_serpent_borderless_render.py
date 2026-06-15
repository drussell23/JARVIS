"""Sovereign Terminal UI — borderless render + sub-flag regression suite."""
import backend.core.ouroboros.battle_test.presentation_restraint as PR


# --------------------------------------------------------------------------- flags
def test_borderless_flag_default_true(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", raising=False)
    assert PR.borderless_enabled() is True


def test_borderless_flag_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "false")
    assert PR.borderless_enabled() is False


def test_borderless_off_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "false")
    monkeypatch.delenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", raising=False)
    assert PR.borderless_enabled() is False     # master gates the sub-flag


def test_pulse_flag_default_true(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.delenv("JARVIS_TUI_PULSE_ENABLED", raising=False)
    assert PR.pulse_enabled() is True


def test_pulse_off_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "false")
    monkeypatch.delenv("JARVIS_TUI_PULSE_ENABLED", raising=False)
    assert PR.pulse_enabled() is False


# --------------------------------------------------------------------------- render
import io  # noqa: E402

_BOX_CHARS = "┌│└─"


def _flow():
    from rich.console import Console
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    flow = SerpentFlow(session_id="bt-tui-test", cost_cap_usd=2.5, idle_timeout_s=3600.0)
    buf = io.StringIO()
    flow.console = Console(file=buf, force_terminal=False, width=120, color_system=None)
    flow._lens_mode = "all"                  # force every op to render to the viewport
    flow._read_current_posture_token = lambda: "EXPLORE"
    return flow, buf


def test_op_line_borderless_no_box(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, buf = _flow()
    op = "op-test-aaaa"
    flow._active_ops.add(op)
    flow._op_line(op, "applied · doubleword · $0.004")
    out = buf.getvalue()
    assert not any(c in out for c in _BOX_CHARS)
    assert ("⎿" in out) or (">" in out)       # result glyph (utf8 or ascii)


def test_op_line_legacy_box_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "false")
    flow, buf = _flow()
    op = "op-test-bbbb"
    flow._active_ops.add(op)
    flow._op_line(op, "x")
    assert "│" in buf.getvalue()                # legacy border retained


def test_open_block_borderless_action_glyph(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, buf = _flow()
    flow._open_op_block("op-test-cccc", "VoiceCommand")
    out = buf.getvalue()
    assert "┌" not in out
    assert ("⏺" in out) or ("*" in out)        # action glyph
    assert "VoiceCommand" in out


def test_op_blank_borderless_is_plain(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, buf = _flow()
    op = "op-test-dddd"
    flow._active_ops.add(op)
    flow._op_blank(op)
    assert "│" not in buf.getvalue()


def test_close_block_borderless_no_footer(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, buf = _flow()
    op = "op-test-eeee"
    flow._active_ops.add(op)
    flow._close_op_block(op)
    assert not any(c in buf.getvalue() for c in _BOX_CHARS)


# --------------------------------------------------------------------------- grayscale
def test_clean_markup_demotes_secondary_colors():
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    out = SerpentFlow._clean_markup("[cyan]synth[/cyan] [magenta]DW[/magenta] [yellow]m[/yellow]")
    assert "cyan" not in out and "magenta" not in out and "yellow" not in out
    assert "[dim]" in out


def test_clean_markup_preserves_outcome_colors():
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    out = SerpentFlow._clean_markup("[green]ok[/green] [red]bad[/red]")
    assert "[green]" in out and "[red]" in out


def test_clean_markup_strips_phase_emoji():
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    out = SerpentFlow._clean_markup("🔬 sensed")
    assert "🔬" not in out and "sensed" in out


def test_op_line_borderless_strips_emoji_and_color(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, buf = _flow()
    op = "op-emoji"
    flow._active_ops.add(op)
    flow._op_line(op, "[cyan]🔬 sensed[/cyan]  mygoal")
    out = buf.getvalue()
    assert "🔬" not in out and "mygoal" in out


# --------------------------------------------------------------------------- pulse wiring
import asyncio  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402


def test_synth_pulse_drives_existing_spinner(monkeypatch):
    # Leverages the EXISTING _spinner_state mechanism, not a console.status overlay.
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TUI_PULSE_ENABLED", "true")
    flow, _ = _flow()
    mid = {}

    async def go():
        async with flow._synth_pulse("op-p", "doubleword"):
            mid["active"] = flow._spinner_state.active
            mid["msg"] = flow._spinner_state.message

    asyncio.run(go())
    assert mid["active"] is True                         # spinner armed during await
    assert "synthesizing" in mid["msg"]
    assert flow._spinner_state.active is False           # cleared after


def test_synth_pulse_clears_spinner_on_exception(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TUI_PULSE_ENABLED", "true")
    flow, _ = _flow()

    async def go():
        async with flow._synth_pulse("op-p", "doubleword"):
            raise ValueError("boom")

    try:
        asyncio.run(go())
    except ValueError:
        pass
    assert flow._spinner_state.active is False           # cleared despite exception


def test_synth_pulse_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TUI_PULSE_ENABLED", "false")
    flow, _ = _flow()
    ran = {}

    async def go():
        async with flow._synth_pulse("op-p", "doubleword"):
            ran["body"] = True
            ran["active"] = flow._spinner_state.active

    asyncio.run(go())
    assert ran["body"] is True
    assert ran["active"] is False                        # never armed when disabled


def test_start_status_borderless_cleans_message(monkeypatch):
    # The EXISTING execution spinner (validate/verify/tool) shows grayscale-clean
    # messages in borderless mode: no box prefix, no per-phase emoji.
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, _ = _flow()
    flow._start_status("  │  🛡️ immune check │ running tests…")
    msg = flow._spinner_state.message
    assert "│" not in msg and "🛡️" not in msg
    assert "immune check" in msg and "running tests" in msg


# --------------------------------------------------------------------------- lifecycle
def test_full_lifecycle_borderless_clean(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OPBLOCK_BORDERLESS_ENABLED", "true")
    flow, buf = _flow()
    op = "op-life"
    flow._open_op_block(op, "TestFailure")
    flow._op_line(op, "[cyan]🔬 sensed[/cyan]  fix the thing")
    flow._op_line(op, "[magenta]synthesizing[/magenta]  doubleword")
    flow._op_line(op, "[green]applied[/green]  $0.004")
    flow._op_blank(op)
    flow._close_op_block(op)
    out = buf.getvalue()
    assert not any(c in out for c in _BOX_CHARS)          # borderless throughout
    assert ("⏺" in out) or ("*" in out)                   # action glyph
    assert "fix the thing" in out and "TestFailure" in out
    assert "🔬" not in out                                 # emoji demoted
    assert "\n\n" in out                                   # vertical rhythm


def test_off_parity_legacy_boxes_intact(monkeypatch):
    monkeypatch.setenv("JARVIS_PRESENTATION_RESTRAINT_ENABLED", "false")
    flow, buf = _flow()
    op = "op-legacy"
    flow._open_op_block(op, "TestFailure")
    flow._op_line(op, "x")
    flow._close_op_block(op)
    out = buf.getvalue()
    assert "┌" in out and "│" in out and "└" in out       # legacy boxed render intact
