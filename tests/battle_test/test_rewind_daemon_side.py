"""Rewind at the daemon's own terminal, through the same lock.

The Esc-Esc menu was client-only. Not arbitrarily: the cockpit is a
SEPARATE PROCESS and had to ask the daemon to pause over the bridge. At the
daemon's own terminal there is no second process, so the same three calls
are local — which means the Transactional Viewport Lock did not need
reimplementing, only a second entrance.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.rewind_menu import (
    LocalRewindClient,
    RewindController,
    local_rewind_rows,
    render_restore_points,
)

_POINTS = [
    {"sha": "90706b8cd", "subject": "harden the vision floor", "age": "12m"},
    {"sha": "a1b2c3d4e", "subject": "rebuild the fixture", "age": "1h"},
]


def _spy():
    return {"paused": 0, "resumed": 0}


class TestTheLockIsNotReimplemented:
    def test_the_controller_needs_only_three_methods(self):
        """That is why no lock logic had to be copied: nothing about it was
        bridge-specific."""
        import inspect
        src = inspect.getsource(RewindController)
        for call in ("send_autonomy", "send_rewind_request"):
            assert call in src
        assert LocalRewindClient(
            pause=lambda: None, resume=lambda: None, provider=lambda n: [],
        ).send_autonomy("pause") is True

    def test_the_daemon_path_uses_the_SAME_controller(self):
        import inspect
        src = inspect.getsource(local_rewind_rows)
        assert "RewindController(" in src

    def test_the_hold_is_taken_and_released_exactly_once(self):
        s = _spy()
        local_rewind_rows(
            pause=lambda: s.__setitem__("paused", s["paused"] + 1),
            resume=lambda: s.__setitem__("resumed", s["resumed"] + 1),
            provider=lambda n: _POINTS,
        )
        assert s == {"paused": 1, "resumed": 1}

    def test_a_resume_we_did_not_pause_for_is_refused(self):
        """Releasing a hold we never took would drop someone else's lock —
        the upstream refcount is what makes concurrent holders safe, and
        lying to it defeats it."""
        s = _spy()
        c = LocalRewindClient(
            pause=lambda: s.__setitem__("paused", s["paused"] + 1),
            resume=lambda: s.__setitem__("resumed", s["resumed"] + 1),
            provider=lambda n: [],
        )
        assert c.send_autonomy("resume") is True     # no-op, not an error
        assert s["resumed"] == 0


class TestItNeverLeavesTheOrganismPaused:
    def test_an_exploding_provider_still_releases(self):
        """A snapshot that leaves intake paused because the planner raised
        is worse than no rewind: the organism sits idle and nothing says
        why."""
        s = _spy()
        rows = local_rewind_rows(
            pause=lambda: s.__setitem__("paused", s["paused"] + 1),
            resume=lambda: s.__setitem__("resumed", s["resumed"] + 1),
            provider=lambda n: (_ for _ in ()).throw(RuntimeError("git down")),
        )
        assert s["resumed"] == 1
        assert rows and "no restore points" in rows[0] or rows

    def test_an_exploding_renderer_still_releases(self, monkeypatch):
        import backend.core.ouroboros.battle_test.rewind_menu as rm
        s = _spy()
        monkeypatch.setattr(
            rm, "render_restore_points",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("render down")))
        rm.local_rewind_rows(
            pause=lambda: s.__setitem__("paused", s["paused"] + 1),
            resume=lambda: s.__setitem__("resumed", s["resumed"] + 1),
            provider=lambda n: _POINTS,
        )
        assert s["resumed"] == 1

    def test_an_empty_snapshot_releases_too(self):
        s = _spy()
        local_rewind_rows(
            pause=lambda: s.__setitem__("paused", s["paused"] + 1),
            resume=lambda: s.__setitem__("resumed", s["resumed"] + 1),
            provider=lambda n: [],
        )
        assert s["resumed"] == 1


class TestTheSurfaceSuitsTheTerminal:
    def test_rows_not_a_float(self):
        """The daemon REPL is a PromptSession with no palette overlay; the
        Float belongs to the cockpit's FloatContainer. Rows mean the same
        snapshot is usable on a surface that cannot host a menu."""
        rows = render_restore_points(_POINTS, width=90)
        assert all(isinstance(r, str) for r in rows)
        assert any("90706b8cd" in r for r in rows)

    def test_numbering_matches_the_undo_command(self):
        """The reading and the command must not disagree."""
        rows = render_restore_points(_POINTS, width=90)
        assert " 1. " in rows[1] and " 2. " in rows[2]
        assert "/undo" in rows[0]

    def test_nothing_is_auto_executed(self):
        """A rollback should never be one accidental keystroke — the same
        reason the cockpit menu only INSERTS the command."""
        rows = render_restore_points(_POINTS)
        assert "Enter confirms" in rows[0]

    def test_an_empty_list_says_so_rather_than_showing_nothing(self):
        assert "no restore points" in render_restore_points([])[0]

    @pytest.mark.parametrize("junk", [None, "x", [None], [{}], 42])
    def test_junk_degrades(self, junk):
        assert isinstance(render_restore_points(junk), list)  # type: ignore

    def test_rows_respect_the_width(self):
        wide = [{"sha": "a" * 40, "subject": "s" * 200}]
        for w in (40, 80, 120):
            assert all(len(r) <= w for r in render_restore_points(wide, width=w))


class TestItIsREACHABLE:
    def test_the_daemon_offers_a_verb(self):
        import inspect

        # Checked in the SOURCE, not via a guessed class name — this
        # module's REPL class is not `SerpentREPL`, and asserting on a name
        # I assumed would have failed while the wiring was correct.
        from backend.core.ouroboros.battle_test import harness
        src = inspect.getsource(harness)
        assert "def _repl_cmd_rewind" in src
        assert 'cmd.startswith("/rewind")' in src

    def test_the_verb_is_DISCOVERABLE(self):
        """It became findable with no registration, because verb discovery
        reads the chain structurally."""
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            list_verbs, prime_registry,
        )
        prime_registry(force=True)
        assert "rewind" in list_verbs()

    def test_it_reuses_the_undo_planner(self):
        """No second snapshot system."""
        import inspect

        from backend.core.ouroboros.battle_test import harness
        src = inspect.getsource(harness)
        i = src.index("_repl_cmd_rewind")
        assert "UndoPlanner" in src[i:i + 2000]
