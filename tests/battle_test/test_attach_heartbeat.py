"""Regression spine for the Attach Heartbeat — the CC-style live pulse
(`✽ Synthesizing… (4m 9s · ↓ 15.9k tokens · DW-397B)`) in the ov cockpit.

Covers: the pure formatter (pulse animation, elapsed advance, staleness,
token/elapsed formatting, provider chip), the daemon composer's payload
schema + provider labeling, the bridge telemetry roundtrip into the
client's on_telemetry callback, and the AttachUI integration — the live
region above the caret, and that it reaches the callable prompt_toolkit
renders.

The pulse used to live in the bottom toolbar and moved above the caret in
#70118; the tests here were left pointing at the old surface and went red for
two days on a position that was deliberately changed. They now assert per
surface — pulse in the live region, key hints in the toolbar — so a future
move fails on the thing that moved rather than on everything at once.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.battle_test.attach_heartbeat import (
    HEARTBEAT_SCHEMA_VERSION,
    _fmt_elapsed,
    _fmt_tokens,
    build_heartbeat_payload,
    format_heartbeat_line,
    heartbeat_interval_s,
)


def _hb(**kw):
    base = {
        "kind": "heartbeat", "active": True, "verb": "Synthesizing",
        "elapsed_s": 249.0, "tokens_total": 15900,
        "provider_label": "DW-397B",
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------


def test_formatter_renders_cc_shape():
    line = format_heartbeat_line(_hb(), now_mono=100.0, arrival_mono=100.0)
    assert "Synthesizing…" in line
    assert "(4m 9s · ↓ 15.9k tokens · DW-QUARANTINE" not in line
    assert "4m 9s" in line and "↓ 15.9k tokens" in line and "DW-397B" in line


def test_formatter_pulse_is_the_ouroboros_identity_spinner():
    """The pulse wears O+V's glyph in Claude Code's SLOT.

    It used to animate `🐍·····○` → `🐍◯`, eight cells wide, and this pinned
    that consecutive instants differ. The operator's rule is CC's grammar
    with O+V's content, and CC's working line is one cell of glyph then a
    verb — so the frames collapsed to a single `🐍` and the motion moved to
    where CC also puts it: the elapsed seconds and a changing verb.

    What still has to hold is the ONE-AUTHORITY property: this pulse renders
    exactly what the theme says, so every surface shows the same mark.
    """
    from backend.core.ouroboros.ui.theme import ouroboros_frame
    a = format_heartbeat_line(_hb(), now_mono=100.00, arrival_mono=100.0)
    ga = a.split(" Synthesizing")[0]
    assert "🐍" in ga                          # it IS the ouroboros
    assert ga.strip() == ouroboros_frame(100.00, unicode=True)  # ONE authority
    # One cell, because a hairline and a status row are length-sensitive.
    assert len(ga.strip()) == 1


def test_spinner_single_authority_theme_serves_serpent_too():
    """DRY invariant: serpent_flow's REPL spinner aliases the theme's
    canonical frames — one definition animates every surface."""
    from backend.core.ouroboros.ui import theme
    from backend.core.ouroboros.battle_test import serpent_flow as sf
    assert sf._OUROBOROS_FRAMES is theme.OUROBOROS_SPINNER_FRAMES
    assert sf._OUROBOROS_FRAME_INTERVAL_S == theme.OUROBOROS_SPINNER_INTERVAL_S
    assert sf._frame_for_now() in theme.OUROBOROS_SPINNER_FRAMES
    # ASCII degradation still yields ONE cell — the story is the glyph's
    # identity now, not a tail that has to be spelled out.
    ascii_frame = theme.ouroboros_frame(0.0, unicode=False)
    assert len(ascii_frame) == 1 and ascii_frame != "🐍"


def test_formatter_elapsed_advances_client_side():
    line = format_heartbeat_line(_hb(elapsed_s=58.0),
                                 now_mono=105.0, arrival_mono=100.0)
    assert "1m 3s" in line                     # 58 + 5s local advance


def test_formatter_inactive_stale_and_absent_render_empty():
    assert format_heartbeat_line(None) == ""
    assert format_heartbeat_line(_hb(active=False)) == ""
    assert format_heartbeat_line(_hb(), now_mono=200.0,
                                 arrival_mono=100.0) == ""   # stale


def test_formatter_omits_empty_segments():
    line = format_heartbeat_line(
        _hb(tokens_total=0, provider_label="", provider=""),
        now_mono=1.0, arrival_mono=1.0,
    )
    assert "tokens" not in line and "·" not in line.split("(", 1)[1]


def test_formatter_never_raises_on_garbage():
    assert format_heartbeat_line({"active": True, "elapsed_s": "junk"}) == "" \
        or isinstance(format_heartbeat_line(
            {"active": True, "elapsed_s": "junk"}), str)
    assert isinstance(format_heartbeat_line({"active": object()}), str)


def test_helpers_format():
    assert _fmt_elapsed(9) == "9s"
    assert _fmt_elapsed(249) == "4m 9s"
    assert _fmt_elapsed(3900) == "1h 5m"
    assert _fmt_tokens(950) == "950"
    assert _fmt_tokens(15900) == "15.9k"


# ---------------------------------------------------------------------------
# Daemon composer
# ---------------------------------------------------------------------------


def test_composer_none_without_status_builder(monkeypatch):
    from backend.core.ouroboros.battle_test import status_line as sl
    monkeypatch.setattr(sl, "_STATUS_LINE_BUILDER", None, raising=False)
    assert build_heartbeat_payload() is None


def test_composer_schema_and_provider_label(monkeypatch):
    """A registered builder yields a schema-stamped payload with the
    pretty provider chip resolved through serpent_flow's own map."""
    from backend.core.ouroboros.battle_test import status_line as sl

    class _Snap:
        phase, phase_detail = "GENERATE", "47s"
        primary_op_id, route, provider = "op-1", "standard", "doubleword"

    class _B:
        def snapshot(self):
            return _Snap()

    monkeypatch.setattr(sl, "get_status_line_builder", lambda: _B())
    payload = build_heartbeat_payload()
    assert payload is not None
    assert payload["schema_version"] == HEARTBEAT_SCHEMA_VERSION
    assert payload["active"] is True
    assert payload["verb"]                     # phase verb at minimum
    assert payload["provider_label"] == "DW-397B"
    assert payload["elapsed_s"] >= 47.0        # parsed from phase_detail


def test_interval_env_knob(monkeypatch):
    monkeypatch.setenv("JARVIS_ATTACH_HEARTBEAT_S", "2.5")
    assert heartbeat_interval_s() == 2.5
    monkeypatch.setenv("JARVIS_ATTACH_HEARTBEAT_S", "0")
    assert heartbeat_interval_s() == 0.0


# ---------------------------------------------------------------------------
# Bridge roundtrip + AttachUI toolbar
# ---------------------------------------------------------------------------


@pytest.fixture()
def sock():
    import shutil
    import tempfile
    from pathlib import Path
    d = tempfile.mkdtemp(prefix="cahb")
    try:
        yield Path(d) / "a.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def test_telemetry_roundtrip_lands_in_on_telemetry(sock):
    import backend.core.ouroboros.battle_test.cockpit_attach as ca
    b = ca.CockpitAttachBridge(path=sock)
    assert await b.start() is True
    try:
        got = []
        c = ca.CockpitAttachClient(path=sock, on_telemetry=got.append)
        assert await c.connect() is True
        b.publish_telemetry(_hb())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if got:
                break
        assert got and got[0]["kind"] == "heartbeat"
        assert got[0]["provider_label"] == "DW-397B"
        c.close()
    finally:
        await b.stop()


def test_attach_ui_pulse_shows_then_falls_back():
    """The pulse lives ABOVE the caret, in the live region — not the toolbar.

    It moved there in #70118 on the same operator mandate that later moved the
    bipartite cockpit's row (#70228): reading order runs downward, so status
    belongs above the line you are typing. This test asserted the old position
    and went red at that commit rather than at the one that broke something —
    which is the failure mode a position assertion exists to prevent, so it now
    pins the surface each piece actually owns.
    """
    from backend.core.ouroboros.cli.ov import AttachUI
    ui = AttachUI()
    assert "organism live" in ui._live_region()   # idle fallback
    ui.on_telemetry(_hb())
    live = ui._live_region()
    assert "Synthesizing…" in live and "DW-397B" in live
    ui.on_telemetry({"kind": "other", "x": 1})    # non-heartbeat ignored
    assert "Synthesizing…" in ui._live_region()
    ui._heartbeat_arrived -= 60.0                 # goes stale
    assert "organism live" in ui._live_region()


def test_the_pulse_reaches_the_rendered_prompt():
    """`_live_region` composing correctly is not the same as it being DRAWN.

    Nothing proved the block reached `prompt()` — the callable wired into
    `PromptSession(message=...)` at ov.py:1578, which is what prompt_toolkit
    actually renders. Without this the region could be composed for no one and
    every other test here would still pass.
    """
    from backend.core.ouroboros.cli.ov import AttachUI
    ui = AttachUI()
    ui.on_telemetry(_hb())
    rendered = ui.prompt()
    assert "Synthesizing…" in rendered
    # Above the caret, not below it: the whole point of the move.
    assert rendered.index("Synthesizing…") < rendered.rindex("›")


def test_the_toolbar_keeps_the_key_hints():
    """What genuinely does belong under the input: hints about the line you
    are on. The pulse left; the chrome stayed."""
    from backend.core.ouroboros.cli.ov import AttachUI
    ui = AttachUI()
    ui.on_telemetry(_hb())
    toolbar = str(ui.toolbar())
    assert "'detach' to leave" in toolbar
    assert "Synthesizing…" not in toolbar
