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
