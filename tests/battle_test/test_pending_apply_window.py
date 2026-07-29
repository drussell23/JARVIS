"""The rejection window, made visible remotely.

A NOTIFY_APPLY op announces `/reject <op> to cancel` and then waits. Locally
that wait is a Rich `Live` panel counting down. Remotely it was
`_notify_apply_plain_fallback` — a silent sleep that polls the cancel flag
and emits nothing, so the operator got the sentence and then five seconds of
nothing to act in.

And that path is not an edge case on a daemon: `should_render` asks whether
THIS process's console is a terminal, and a detached daemon's is not. The
silent path was the only path an attached operator ever saw.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.battle_test import pending_apply as pa


@pytest.fixture(autouse=True)
def _clean():
    pa.reset_for_tests()
    yield
    pa.reset_for_tests()


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _open(delay=5.0, op="7759-86", reason="containment fix"):
    clock = _Clock()
    pa.note_pending(op, delay_s=delay, reason=reason, clock=clock)
    return clock


class TestTheWindowIsVisible:
    def test_an_open_window_renders(self):
        clock = _open()
        rows = pa.render(pa.snapshot(clock=clock), width=88)
        assert rows and "/reject" in rows[0]

    def test_it_names_the_op_the_operator_must_type(self):
        clock = _open(op="7759-86")
        assert "/reject 7759-86" in pa.render(pa.snapshot(clock=clock))[0]

    def test_nothing_pending_renders_NOTHING(self):
        assert pa.snapshot() is None
        assert pa.render(None) == []

    def test_absent_is_distinguishable_from_empty(self):
        """"No apply is pending" and "the daemon never told us" are different
        facts, and a reader that cannot tell them apart draws the first when
        it means the second."""
        assert pa.snapshot() is None


class TestTheCountdownIsHonest:
    def test_it_counts_DOWN_between_heartbeats(self):
        """The mirror of how the pulse advances elapsed UP. Without it the
        number freezes for a second and jumps."""
        clock = _open(delay=5.0)
        snap = pa.snapshot(clock=clock)
        assert "5.0s" in pa.render(snap, age_s=0.0)[0]
        assert "3.6s" in pa.render(snap, age_s=1.4)[0]
        assert "1.8s" in pa.render(snap, age_s=3.2)[0]

    def test_it_ships_REMAINING_not_a_deadline(self):
        """`time.monotonic()` is a per-process origin. Shipping a deadline
        and subtracting the reader's clock yields a countdown wrong by
        however long the two processes have been alive — plausibly wrong,
        which is worse than obviously wrong."""
        clock = _open()
        row = pa.snapshot(clock=clock)["rows"][0]
        assert "remaining_s" in row
        assert "deadline" not in row and "started" not in row

    def test_running_out_says_APPLYING_rather_than_vanishing(self):
        """The operator's last impression should be that the window closed,
        not that the op disappeared."""
        clock = _open(delay=5.0)
        snap = pa.snapshot(clock=clock)
        assert "applying" in pa.render(snap, age_s=6.0)[0]

    def test_the_producer_expires_it_not_the_reader(self):
        """A reader deciding an op had expired would be guessing from a frame
        that is already a second old."""
        clock = _open(delay=1.0)
        clock.now += 2.0
        assert pa.snapshot(clock=clock) is None

    def test_the_snapshot_survives_json(self):
        clock = _open()
        snap = pa.snapshot(clock=clock)
        assert json.loads(json.dumps(snap)) == snap


class TestTheWindowAlwaysCloses:
    def test_clearing_retires_it(self):
        clock = _open()
        pa.clear_pending("7759-86")
        assert pa.snapshot(clock=clock) is None

    def test_a_REJECTED_op_stops_counting_down(self):
        """Rejection returns early from inside the wait loop, so clearing on
        the success path alone would leave a rejected op counting down
        forever on every attached cockpit."""
        import inspect
        from backend.core.ouroboros.battle_test import serpent_flow as sf
        src = inspect.getsource(sf.SerpentFlow._notify_apply_plain_fallback)
        assert "finally:" in src
        finally_block = src[src.index("finally:"):]
        assert "clear_pending" in finally_block

    def test_the_daemon_publishes_from_the_seam_that_WAITS(self):
        import inspect
        from backend.core.ouroboros.battle_test import serpent_flow as sf
        src = inspect.getsource(sf.SerpentFlow._notify_apply_plain_fallback)
        assert "note_pending" in src

    def test_two_windows_sort_by_urgency(self):
        clock = _Clock()
        pa.note_pending("slow", delay_s=9.0, clock=clock)
        pa.note_pending("urgent", delay_s=2.0, clock=clock)
        rows = pa.snapshot(clock=clock)["rows"]
        assert rows[0]["op_id"] == "urgent"


class TestItCrossesTheBridge:
    def test_the_heartbeat_carries_it(self):
        import inspect
        from backend.core.ouroboros.battle_test import attach_heartbeat as hb
        assert '"pending_apply"' in inspect.getsource(hb.build_heartbeat_payload)

    def test_absence_CLEARS_it_unlike_the_roster(self):
        """The roster keeps its last value on a missing key because that
        means an older daemon. Here it means the window closed, and a
        countdown that outlives its op tells the operator they can still
        stop something that already ran."""
        from backend.core.ouroboros.cli.ov import AttachUI
        clock = _open()
        ui = AttachUI()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "pending_apply": pa.snapshot(clock=clock)})
        assert ui._pending_apply_rows()
        ui.on_telemetry({"kind": "heartbeat", "active": True})
        assert ui._pending_apply_rows() == []

    def test_a_stale_frame_retires_the_countdown(self):
        """A dead daemon must not leave a countdown ticking toward an apply
        that will never happen."""
        import time
        from backend.core.ouroboros.cli.ov import AttachUI
        clock = _open()
        ui = AttachUI()
        ui.on_telemetry({"kind": "heartbeat", "active": True,
                         "pending_apply": pa.snapshot(clock=clock)})
        assert ui._pending_apply_rows()
        ui._heartbeat_arrived = time.monotonic() - 10_000
        assert ui._pending_apply_rows() == []

    def test_the_cockpit_is_wired_to_the_CLIENTS_view(self):
        import inspect
        from backend.core.ouroboros.cli.ov import _bipartite_attach_loop
        src = inspect.getsource(_bipartite_attach_loop)
        idx = src.find("pending_rows=")
        assert idx > 0
        assert "_pending_apply_rows" in src[idx:idx + 220]


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: pa.note_pending(None, delay_s=None),
        lambda: pa.note_pending("x", delay_s=-1),
        lambda: pa.clear_pending(None),
        lambda: pa.render("junk"),
        lambda: pa.render({"rows": [{"op_id": None}]}),
        lambda: pa.snapshot(),
    ])
    def test_junk_degrades(self, call):
        call()

    def test_the_master_flag_silences_it(self, monkeypatch):
        clock = _open()
        monkeypatch.setenv("JARVIS_PENDING_APPLY_STRIP_ENABLED", "0")
        assert pa.snapshot(clock=clock) is None
        assert pa.render({"rows": [{"op_id": "x", "remaining_s": 3}]}) == []

    def test_a_narrow_terminal_keeps_the_line_within_bounds(self):
        clock = _open(reason="a very long reason " * 12)
        for width in (30, 60, 120):
            for row in pa.render(pa.snapshot(clock=clock), width=width):
                assert len(row) <= width, (width, row)
