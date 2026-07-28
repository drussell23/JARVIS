"""The status line, mounted on the cockpit — and the width it never knew.

`StatusLineBuilder` was fully built and `live_status_line` wired it into the
daemon's PromptSession `bottom_toolbar`. The bipartite cockpit — the surface
`ov` actually mounts, both locally and over attach — got only key hints. So
the operator-load-bearing line (phase, cost, route, warnings) existed and was
invisible on the surface they stare at.

Two things had to be true to mount it, and each has a failure that reads as
normal operation:

  * it must cross a PROCESS boundary (the client's builder is empty by
    construction, and an empty builder renders a perfectly plausible idle
    organism);
  * it must fit a WIDTH (`_format_plain` composes up to ten `·`-joined
    segments and had never been told how wide the terminal is; a fixed-height
    row with `wrap_lines=False` truncates mid-token).
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.battle_test.status_line import (
    StatusSnapshot,
    fit_to_width,
    payload_to_snapshot,
    render_snapshot,
    snapshot_to_payload,
)


@pytest.fixture
def busy() -> StatusSnapshot:
    return StatusSnapshot(
        phase="GENERATE", phase_detail="47s",
        cost_spent_usd=0.04, cost_budget_usd=0.50,
        idle_elapsed_s=12, idle_timeout_s=600,
        primary_op_id="7759-86ab-cdef", extra_op_count=2,
        route="standard", provider="dw",
        liquidity_exhausted=True, liquidity_provider="claude",
        liquidity_reset_s=900,
    )


class TestItCrossesTheProcessBoundary:
    def test_the_snapshot_survives_the_wire_unchanged(self, busy):
        assert payload_to_snapshot(
            json.loads(json.dumps(snapshot_to_payload(busy)))
        ) == busy

    def test_one_renderer_serves_both_sources(self, busy):
        """A local builder and a rehydrated dict must produce the same line.
        Two renderers would be edited months apart."""
        rehydrated = payload_to_snapshot(snapshot_to_payload(busy))
        assert render_snapshot(rehydrated, width=200) == render_snapshot(
            busy, width=200)

    def test_an_unknown_field_from_a_newer_daemon_does_not_crash_the_client(
        self, busy,
    ):
        """THE forward-compat failure. `StatusSnapshot(**payload)` raises on
        the first frame carrying a field this client has never heard of —
        turning an additive daemon change into a client crash."""
        payload = snapshot_to_payload(busy)
        payload["some_future_field"] = "from a newer daemon"
        assert payload_to_snapshot(payload) == busy

    def test_junk_is_None_not_an_empty_snapshot(self):
        """An empty StatusSnapshot renders a plausible IDLE organism. "We
        received nothing" must not look like "nothing is happening"."""
        assert payload_to_snapshot(None) is None
        assert payload_to_snapshot({}) is None
        assert payload_to_snapshot("nonsense") is None
        assert render_snapshot(None) == ""


class TestItFitsTheTerminal:
    def test_the_headline_is_the_last_thing_standing(self, busy):
        """At every width down to almost nothing, the phase survives."""
        for width in (200, 140, 110, 80, 50, 28):
            line = render_snapshot(busy, width=width)
            assert "Phase:" in line, f"phase lost at {width} cols: {line!r}"

    def test_a_warning_outlives_the_cost(self, busy):
        """Priority is by MEANING. A dry provider runway may be the reason
        the organism is doing nothing; the budget is a standing question."""
        line = render_snapshot(busy, width=50)
        assert "dry" in line and "Cost:" not in line

    def test_the_hint_loses_to_the_headline(self, busy):
        """The first draft guessed prefixes for the hotkey legend, guessed
        wrong, and at 120 columns shed `Phase:` while keeping
        `enter to submit`."""
        line = render_snapshot(busy, width=120)
        assert "Phase:" in line
        assert "reverse-search" not in line

    def test_an_unrecognised_segment_is_shed_before_the_essentials(self):
        """A token some future slice appends is decoration until someone
        says otherwise. The alternative is that it silently outranks the
        phase."""
        line = "Phase: GENERATE · Cost: $1.00 / $2.00 · brand new token here"
        out = fit_to_width(line, 46)
        assert "Phase:" in out and "brand new token" not in out

    def test_it_sheds_whole_segments_never_a_slice(self, busy):
        """`Cost: $0.04 / $0.5` is a number the operator will misread, and
        `Phase: GENERAT` is a phase that does not exist."""
        for width in range(30, 200, 7):
            line = render_snapshot(busy, width=width)
            if "Cost:" in line:
                assert "$0.50" in line, f"cost clipped at {width}: {line!r}"

    def test_everything_fits_or_is_marked(self, busy):
        for width in range(20, 210, 3):
            line = render_snapshot(busy, width=width)
            assert len(line) <= width, f"{width}: {len(line)} — {line!r}"

    def test_a_narrow_terminal_compacts_without_being_told(self):
        """Compact was env-only. An operator on an 80-column terminal should
        not have to know a flag exists."""
        snap = StatusSnapshot(phase="GENERATE", cost_spent_usd=0.1,
                              cost_budget_usd=0.5, primary_op_id="abc-1234")
        assert len(render_snapshot(snap, width=60)) <= 60

    def test_an_explicit_choice_still_wins(self, busy, monkeypatch):
        """An operator who asked for compact means it at every width."""
        monkeypatch.setenv("JARVIS_UI_STATUS_LINE_COMPACT", "1")
        assert "Op:" not in render_snapshot(busy, width=400)

    def test_no_width_means_no_shedding(self, busy):
        """Callers that cannot know the terminal get today's behaviour."""
        assert render_snapshot(busy, width=None) == render_snapshot(
            busy, width=0)


class TestTheMount:
    def test_the_cockpit_accepts_a_status_provider(self):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_bipartite_application,
        )
        import inspect
        sig = inspect.signature(build_bipartite_application)
        assert "status_rows" in sig.parameters

    def test_the_row_collapses_when_there_is_nothing_to_say(self):
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_dynamic_rows,
        )
        assert build_dynamic_rows(lambda: []).filter() is False

    def test_the_attach_client_is_wired_to_the_DAEMONS_snapshot(self):
        """The bug this exists to avoid, pinned at the seam: the provider
        must read the AttachUI, never this process's own builder."""
        import inspect
        from backend.core.ouroboros.cli.ov import _bipartite_attach_loop
        src = inspect.getsource(_bipartite_attach_loop)
        idx = src.find("status_rows=")
        assert idx > 0
        wiring = src[idx:idx + 200]
        assert "_status_rows" in wiring
        assert "get_status_line_builder" not in wiring

    def test_the_daemon_cockpit_uses_its_LOCAL_builder(self):
        import inspect
        from backend.core.ouroboros.battle_test import serpent_flow as sf
        src = inspect.getsource(sf._local_status_rows)
        assert "get_status_line_builder" in src
        assert "render_snapshot" in src

    def test_a_stale_frame_retires_the_line(self):
        """A dead daemon must not leave its last phase showing under an idle
        pulse. Same window as the pulse and the roster — one definition of
        lost contact, not three."""
        import time
        from backend.core.ouroboros.cli.ov import AttachUI
        ui = AttachUI()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "status": snapshot_to_payload(StatusSnapshot(
                             phase="GENERATE", cost_budget_usd=0.5))})
        assert ui._status_rows()
        ui._heartbeat_arrived = time.monotonic() - 10_000
        assert ui._status_rows() == []

    def test_the_heartbeat_carries_it(self):
        import inspect
        from backend.core.ouroboros.battle_test import attach_heartbeat as hb
        assert '"status"' in inspect.getsource(hb.build_heartbeat_payload)


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: snapshot_to_payload(None),
        lambda: payload_to_snapshot(object()),
        lambda: fit_to_width(None, 40),
        lambda: fit_to_width("a · b", None),
        lambda: render_snapshot(None, width=-5),
    ])
    def test_junk_degrades(self, call):
        call()

    def test_the_master_flag_silences_it(self, busy, monkeypatch):
        monkeypatch.setenv("JARVIS_UI_STATUS_LINE_ENABLED", "0")
        assert render_snapshot(busy, width=200) == ""
