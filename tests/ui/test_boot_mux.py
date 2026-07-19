"""Cinematic Boot Mux — structural TTY isolation spine.

Operator mandate Test A: an injected ``sys.stderr.write`` during early
boot is BUFFERED (routed to boot.log), never reaching the TTY — unless
a fatal exception fires the Dead-Man's Switch, in which case the hidden
buffer flushes to the real stderr so forensics survive.

Plus the structural invariants: mode-flip release (stream objects never
swapped back — late-bound writers survive), isatty transparency (Rich's
tier detection must see the REAL terminal), idempotence, master-off,
and the three wiring pins (ov engage, awakening release, collision-
surface release).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.ui import boot_mux as bm

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_BOOT_MUX_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BOOT_MUX_LOG", str(tmp_path / "boot.log"))
    bm.reset_boot_mux_for_tests()
    real_out, real_err = sys.stdout, sys.stderr
    yield
    sys.stdout, sys.stderr = real_out, real_err
    bm.reset_boot_mux_for_tests()


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:  # noqa: D102
        return True


def _engage_with_fakes(monkeypatch):
    fake_out, fake_err = _FakeTTY(), _FakeTTY()
    monkeypatch.setattr(sys, "stdout", fake_out)
    monkeypatch.setattr(sys, "stderr", fake_err)
    mux = bm.get_boot_mux()
    assert mux.engage() is True
    return mux, fake_out, fake_err


# ---------------------------------------------------------------------------
# Test A — buffered, TTY-silent, dead-man flush on fatal
# ---------------------------------------------------------------------------


def test_early_boot_stderr_is_buffered_not_tty(monkeypatch, tmp_path):
    mux, fake_out, fake_err = _engage_with_fakes(monkeypatch)

    sys.stderr.write("[CrossProcessJSONL] stale_lock_detected age_s=358\n")
    sys.stdout.write("[reaper] reaped 3 stale locks\n")
    print("boot chatter via print()")

    # NOTHING reached the (fake) TTY...
    assert fake_err.getvalue() == ""
    assert fake_out.getvalue() == ""
    # ...everything landed in the hidden buffer + boot.log.
    assert mux.buffered_chars() > 0
    log = (tmp_path / "boot.log").read_text()
    assert "stale_lock_detected" in log
    assert "reaped 3 stale locks" in log
    assert "boot chatter via print()" in log


def test_deadman_flush_surfaces_forensics(monkeypatch):
    mux, _fake_out, fake_err = _engage_with_fakes(monkeypatch)
    sys.stderr.write("FATAL clue: credential vault unreachable\n")
    assert fake_err.getvalue() == ""            # hidden pre-fatal

    mux.release(flush_to_tty=True)              # the Dead-Man's Switch

    dumped = fake_err.getvalue()
    assert "dead-man flush" in dumped
    assert "FATAL clue: credential vault unreachable" in dumped


def test_clean_release_keeps_chatter_hidden(monkeypatch):
    mux, fake_out, fake_err = _engage_with_fakes(monkeypatch)
    sys.stderr.write("routine boot noise\n")
    mux.release()                               # presentation handoff
    assert "routine boot noise" not in fake_err.getvalue()
    # Post-release writes pass straight through to the TTY.
    sys.stdout.write("⏺ crest ignites here")
    assert "⏺ crest ignites here" in fake_out.getvalue()


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


def test_mode_flip_late_bound_writers_survive(monkeypatch):
    """A console that bound sys.stdout WHILE muxed must keep working
    after release — the tee flips mode, the object never changes."""
    mux, fake_out, _fake_err = _engage_with_fakes(monkeypatch)
    bound_stream = sys.stdout                   # what Rich would capture
    mux.release()
    bound_stream.write("late-bound writer output")
    assert "late-bound writer output" in fake_out.getvalue()


def test_isatty_transparency(monkeypatch):
    _mux, _o, _e = _engage_with_fakes(monkeypatch)
    # Rich probes isatty to choose animated-vs-plain — the mux must
    # report the REAL terminal's answer even while capturing.
    assert sys.stdout.isatty() is True
    assert sys.stderr.isatty() is True


def test_engage_idempotent_and_master_off(monkeypatch):
    mux, _o, _e = _engage_with_fakes(monkeypatch)
    assert mux.engage() is True                 # second engage: no-op True
    bm.reset_boot_mux_for_tests()
    monkeypatch.setenv("JARVIS_BOOT_MUX_ENABLED", "0")
    assert bm.get_boot_mux().engage() is False  # master off: never engages


def test_release_idempotent_and_never_raises():
    mux = bm.get_boot_mux()
    mux.release()                               # never engaged — no-op
    mux.release(flush_to_tty=True)


# ---------------------------------------------------------------------------
# Wiring pins — engage at ov entry, release at BOTH presentation moments
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def test_ov_engages_before_bootstrap_import():
    src = _read("backend/core/ouroboros/cli/ov.py")
    engage = src.index("engage_boot_mux()")
    bootstrap = src.index(
        "from scripts.ouroboros_battle_test import main as battle_main"
    )
    assert engage < bootstrap                   # silence precedes the chatter
    assert "_deadman_flush" in src
    assert 'inv.action == "cockpit"' in src[:engage + 500]


def test_awakening_releases_the_mux():
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    body = src[src.index("def _start_awakening_t0"):][:6000]
    assert "release_boot_mux()" in body
    # Handoff happens BEFORE the ceremony console is CONSTRUCTED (the
    # code call, not the docstring mention).
    assert body.index("release_boot_mux()") < body.index(
        "_ov_theme.build_console("
    )


def test_collision_surface_releases_the_mux():
    src = _read("scripts/ouroboros_battle_test.py")
    # Anchor on the PRINTED banner (the comment at the branch head also
    # contains the phrase) — the release call must sit between them.
    idx = src.index("⏺ the organism is already awake")
    region = src[max(0, idx - 2500):idx]
    assert "release_boot_mux" in region
