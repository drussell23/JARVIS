"""The diff overlay, on the surface that reviews diffs.

`capability_handoff` measured `diff_rows` UNSET on `ov`. The daemon owns the
`DiffArchive`, so `/expand d-3` typed at an attached cockpit travelled to the
daemon, opened the diff on the DAEMON's overlay, and mirrored back one line
saying it had opened. The operator was told a diff was on screen and shown
nothing — on the surface they review changes from.

The failures pinned here all read as normal operation:

  * "no such diff" rendered over a diff that exists and is in flight,
  * a repainting overlay asking the daemon for the same diff every frame,
  * a fetch that lands after the operator moved on yanking the overlay back,
  * a truncated patch reviewed as though it were whole.
"""
from __future__ import annotations

import types

import pytest

from backend.core.ouroboros.battle_test import diff_bridge as B
from backend.core.ouroboros.battle_test.diff_archive import (
    ArchivedDiff, get_default_archive,
)

_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


@pytest.fixture
def daemon_archive():
    arch = get_default_archive()
    arch.clear()
    arch.add(op_id="op-1", risk_tier="notify_apply",
             file_paths=("backend/x.py",), diff_text=_DIFF,
             summary="+1 / -1 in 1 file")
    yield arch
    arch.clear()


@pytest.fixture
def wired(daemon_archive):
    """A client cockpit whose archive lives in another process."""
    from backend.core.ouroboros.cli.ov import AttachUI

    sent = []
    client = types.SimpleNamespace(send_input=sent.append,
                                   send_audio=lambda *a: None)
    ui = AttachUI()
    ui.flash = lambda *a, **k: None
    controller = ui.diff_controller(client)
    ref = daemon_archive.list_recent()[0].ref
    return types.SimpleNamespace(ui=ui, client=client, sent=sent,
                                 ctrl=controller, ref=ref,
                                 archive=daemon_archive)


def _serve(ref):
    """The daemon answering a fetch, capturing what it publishes."""
    import backend.core.ouroboros.battle_test.cockpit_attach as CA
    import backend.core.ouroboros.battle_test.serpent_flow as sf

    frames = []
    original = CA.publish_telemetry_global
    CA.publish_telemetry_global = lambda p: (frames.append(p), True)[1]
    try:
        repl = types.SimpleNamespace(_flow=types.SimpleNamespace(
            console=types.SimpleNamespace(print=lambda *a, **k: None)))
        sf.SerpentREPL._serve_diff_fetch(repl, ref)
    finally:
        CA.publish_telemetry_global = original
    return frames[-1] if frames else None


class TestTheCatalog:
    def test_it_carries_metadata_and_never_the_bytes(self, daemon_archive):
        """This rides a 1 Hz frame. Diffs are the one payload on the lane
        with no natural bound — a generated migration is megabytes."""
        rows = B.build_diff_catalog(daemon_archive)
        assert rows and "diff_text" not in rows[0]
        assert rows[0]["ref"] and rows[0]["risk_tier"] == "notify_apply"

    def test_it_is_bounded(self, daemon_archive, monkeypatch):
        for i in range(30):
            daemon_archive.add(op_id=f"op-{i}", risk_tier="safe_auto",
                               file_paths=("a.py",), diff_text=_DIFF,
                               summary="x")
        monkeypatch.setenv("JARVIS_DIFF_CATALOG_ROWS", "5")
        assert len(B.build_diff_catalog(daemon_archive)) == 5

    def test_it_reaches_the_client(self, wired):
        """`_ingest_diff_catalog` existed with no caller in the first cut —
        the wired-but-inert trap, caught by this assertion."""
        wired.ui.on_telemetry({"kind": "heartbeat", "active": True,
                               "diffs": B.build_diff_catalog(wired.archive)})
        assert wired.ui._diff_archive.all_refs() == (wired.ref,)

    def test_it_is_replaced_wholesale_not_merged(self, wired):
        """The archive is a RING: a ref that stops being advertised was
        evicted. Merging would let it live forever in a client that saw it
        once, and `all_refs()` would then lie about what can be opened."""
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        assert wired.ui._diff_archive.all_refs()
        wired.ui.on_telemetry({"kind": "heartbeat", "diffs": []})
        assert wired.ui._diff_archive.all_refs() == ()

    def test_an_older_daemon_sending_none_is_not_an_error(self, wired):
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        wired.ui.on_telemetry({"kind": "heartbeat", "active": True})
        assert wired.ui._diff_archive.all_refs() == (wired.ref,)


class TestPendingIsNotAbsent:
    def test_an_in_flight_diff_is_not_no_such_diff(self, wired):
        """THE remote-specific bug. The controller treats None as "no such
        diff" — correct for a dict, wrong across a socket where "not here
        yet" and "not a thing" are different answers."""
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        assert wired.ctrl.open(wired.ref) is True
        head = wired.ctrl.rows()[0]
        assert "no diff archived" not in head
        assert wired.ref in head

    def test_opening_issues_exactly_one_fetch(self, wired):
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        wired.sent.clear()
        wired.ctrl.open(wired.ref)
        assert wired.sent == [f"/diff-fetch {wired.ref}"]

    def test_repaints_do_not_re_ask_at_the_frame_rate(self, wired):
        """`lookup` runs from a RENDER path."""
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        wired.sent.clear()
        wired.ctrl.open(wired.ref)
        for _ in range(50):
            wired.ui._diff_archive.lookup(wired.ref)
        assert len(wired.sent) == 1

    def test_a_timed_out_fetch_is_retried(self, wired, monkeypatch):
        """A daemon that dropped the request must not leave the overlay
        saying 'rendering…' forever."""
        monkeypatch.setenv("JARVIS_DIFF_FETCH_TIMEOUT_S", "1")
        clock = [1000.0]
        wired.ui._diff_archive._clock = lambda: clock[0]
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        wired.sent.clear()
        wired.ui._diff_archive.lookup(wired.ref)
        assert len(wired.sent) == 1
        clock[0] += 5.0
        wired.ui._diff_archive.lookup(wired.ref)
        assert len(wired.sent) == 2
        assert wired.ui._diff_archive.pending_refs() == (wired.ref,)


class TestTheRoundTrip:
    def test_the_bytes_arrive_and_render(self, wired):
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        wired.ctrl.open(wired.ref)
        wired.ui.on_telemetry(_serve(wired.ref))
        assert wired.ui._diff_archive.is_hydrated(wired.ref)
        rows = wired.ctrl.rows()
        assert any("+new" in r for r in rows), rows

    def test_a_missing_ref_is_answered_not_ignored(self, wired):
        """Silence would leave the client re-issuing the fetch at the frame
        rate against a ref that will never arrive."""
        frame = _serve("d-999")
        assert frame["missing"] is True
        wired.ui.on_telemetry(frame)
        assert wired.ui._diff_archive.lookup("d-999") is None
        wired.sent.clear()
        wired.ui._diff_archive.lookup("d-999")
        assert wired.sent == [], "a known-missing ref was requested again"

    def test_a_late_fetch_does_not_yank_the_overlay_back(self, wired):
        """The operator moved on. A payload landing for a ref they are no
        longer looking at must not reopen it."""
        wired.ui.on_telemetry({"kind": "heartbeat",
                               "diffs": B.build_diff_catalog(wired.archive)})
        wired.ctrl.open(wired.ref)
        wired.ctrl.dismiss()
        wired.ui.on_telemetry(_serve(wired.ref))
        assert wired.ctrl.is_active() is False

    def test_an_oversized_diff_is_truncated_out_loud(self, wired, monkeypatch):
        """A diff that simply stops is indistinguishable from one that
        ended, and an operator reviewing a truncated patch as though it were
        whole is the worst thing this surface can produce."""
        monkeypatch.setenv("JARVIS_DIFF_MAX_CHARS", "4000")
        wired.archive.clear()
        wired.archive.add(op_id="big", risk_tier="safe_auto",
                          file_paths=("a.py",),
                          diff_text="+line\n" * 40_000, summary="huge")
        ref = wired.archive.list_recent()[0].ref
        frame = _serve(ref)
        assert frame.get("truncated") is True
        assert "not shown" in frame["diff_text"]
        assert len(frame["diff_text"]) < 5_000


class TestNoSecondOverlay:
    def test_the_client_reuses_the_daemons_controller(self, wired):
        """Not a second overlay: the same renderer, epoch guard, Escape
        arbitration and off-thread render. A regression in the daemon's diff
        surfaces here too rather than in a parallel drawing."""
        from backend.core.ouroboros.battle_test.diff_overlay import (
            DiffOverlayController,
        )
        assert isinstance(wired.ctrl, DiffOverlayController)
        assert isinstance(wired.ctrl._archive, B.RemoteDiffArchive)

    def test_the_controller_is_a_singleton_per_cockpit(self, wired):
        """The verb that OPENS and the hook that DRAWS must be one object, or
        the verb fills a surface nothing renders."""
        assert wired.ui.diff_controller(wired.client) is wired.ctrl

    def test_the_hook_is_handed_to_the_cockpit(self):
        import ast
        import inspect

        from backend.core.ouroboros.cli import ov

        tree = ast.parse(inspect.getsource(ov))
        assert any(
            isinstance(n, ast.keyword) and n.arg == "diff_rows"
            for n in ast.walk(tree)
        ), "ov builds the cockpit without handing it diff_rows"

    def test_only_d_refs_are_intercepted(self):
        """`t-`/`o-`/`n-` refs keep round-tripping and mirroring as markup,
        which already works. Only the diff overlay is drawn client-side."""
        import inspect

        from backend.core.ouroboros.cli import ov

        src = inspect.getsource(ov._route_operator_line)
        assert '"/expand d-"' in src or "'/expand d-'" in src
        assert '"/expand t-"' not in src


class TestSerialisationRoundTrip:
    def test_from_dict_inverts_to_dict(self, daemon_archive):
        """Reconstructing the real dataclass rather than a look-alike is what
        stops the remote overlay from silently rendering a blank where the
        local one shows a field the controller learned to read."""
        entry = daemon_archive.list_recent()[0]
        clone = ArchivedDiff.from_dict(entry.to_dict(include_diff_text=True))
        for field in ("ref", "op_id", "risk_tier", "file_paths", "diff_text",
                      "summary", "apply_outcome", "verify_outcome"):
            assert getattr(clone, field) == getattr(entry, field), field

    def test_it_does_not_invent_a_local_clock(self, daemon_archive):
        """`time.monotonic()` is a per-process origin: a timestamp from the
        daemon means nothing here, and a local one would be a plausible lie."""
        entry = daemon_archive.list_recent()[0]
        clone = ArchivedDiff.from_dict(entry.to_dict(include_diff_text=True))
        assert clone.archived_at == 0.0 and clone.terminal_at == 0.0

    def test_garbage_never_raises(self):
        for bad in (None, "", 42, [], {}, {"op_id": "x"}):
            assert ArchivedDiff.from_dict(bad) is None
