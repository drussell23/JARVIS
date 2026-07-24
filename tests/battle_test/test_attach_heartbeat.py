"""Regression spine for the Attach Heartbeat — the CC-style live pulse
(`✽ Synthesizing… (4m 9s · ↓ 15.9k tokens · DW-397B)`) in the ov cockpit.

Covers: the pure formatter (pulse animation, elapsed advance, staleness,
token/elapsed formatting, provider chip), the daemon composer's payload
schema + provider labeling, the bridge telemetry roundtrip into the
client's on_telemetry callback, and the AttachUI toolbar integration.
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
    """The pulse is O+V's OWN animation — the theme's canonical Ouroboros
    frames (snake → bite → reopen), not a borrowed star. Clock-driven:
    different instants render different frames; the same instant renders
    the SAME frame the daemon's REPL spinner would show."""
    from backend.core.ouroboros.ui.theme import ouroboros_frame
    a = format_heartbeat_line(_hb(), now_mono=100.00, arrival_mono=100.0)
    b = format_heartbeat_line(_hb(), now_mono=100.30, arrival_mono=100.0)
    ga = a.split(" Synthesizing")[0]
    gb = b.split(" Synthesizing")[0]
    assert ga != gb                            # the snake advances
    assert "🐍" in ga                          # it IS the ouroboros
    assert ga.strip() == ouroboros_frame(100.00, unicode=True)  # ONE authority


def test_spinner_single_authority_theme_serves_serpent_too():
    """DRY invariant: serpent_flow's REPL spinner aliases the theme's
    canonical frames — one definition animates every surface."""
    from backend.core.ouroboros.ui import theme
    from backend.core.ouroboros.battle_test import serpent_flow as sf
    assert sf._OUROBOROS_FRAMES is theme.OUROBOROS_SPINNER_FRAMES
    assert sf._OUROBOROS_FRAME_INTERVAL_S == theme.OUROBOROS_SPINNER_INTERVAL_S
    assert sf._frame_for_now() in theme.OUROBOROS_SPINNER_FRAMES
    # ASCII degradation keeps the same story on dumb terminals.
    assert theme.ouroboros_frame(0.0, unicode=False) == "s.....o"


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


def test_attach_ui_toolbar_shows_pulse_then_falls_back():
    from backend.core.ouroboros.cli.ov import AttachUI
    ui = AttachUI()
    idle = ui.toolbar()
    assert "organism live" in idle             # idle fallback
    ui.on_telemetry(_hb())
    live = ui.toolbar()
    assert "Synthesizing…" in live and "DW-397B" in live
    assert "'detach' to leave" in live         # chrome retained
    ui.on_telemetry({"kind": "other", "x": 1})  # non-heartbeat ignored
    assert "Synthesizing…" in ui.toolbar()
    ui._heartbeat_arrived -= 60.0              # goes stale
    assert "organism live" in ui.toolbar()
