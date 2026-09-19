"""Two cockpit defects from one screenshot: a doubled line, and a mojibake.

**The double.** `_repl_print` published to the attach bridge AND printed to
`sf.console` — but the harness swaps that console for a SPOOLED one that
relays everything printed to it. Both paths reached the cockpit, so every line
arrived twice. `_print_mirrored` is the existing cure, and its own docstring
records the identical defect found in 2026-09 ("Each `⏺ X queued` arrived at
the cockpit as a pair"). It was simply never adopted at the chokepoint that
fans out the most lines.

**The mojibake.** `≡ƒÆ¡` is NOT an encoding fault on the Python side. Measured:
`sys.stdout.encoding` is utf-8, LANG is C.UTF-8, and the emitted bytes are
`f0 9f 92 ad` — correct UTF-8 for `U+1F4AD`. A Windows console then DECODES
them as cp437. Nothing in this process can change that. The guard here is for
the different, real case: a stream that cannot ENCODE at all.
"""
from __future__ import annotations

import io
import sys
import types

import pytest

from backend.core.ouroboros.ui import stream_encoding as SE


# --------------------------------------------------------------------------
# The mojibake, proven to be someone else's layer
# --------------------------------------------------------------------------

def test_the_reported_artifact_is_a_DECODE_fault_not_an_encode_one():
    """💭 as UTF-8, read as cp437, is exactly what the operator saw — which
    means the bytes leaving Python were already correct."""
    assert "\U0001F4AD".encode("utf-8") == b"\xf0\x9f\x92\xad"
    assert b"\xf0\x9f\x92\xad".decode("cp437") == "≡ƒÆ¡"


def test_a_utf8_stream_is_left_completely_alone():
    """Every POSIX host in normal operation. Doing nothing is the correct
    outcome and must be the visible one."""
    s = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    assert SE.stream_can_carry_glyphs(s) is True
    assert SE.ensure_glyph_capable_streams((("out", s),)) == []


# --------------------------------------------------------------------------
# The case the guard IS for
# --------------------------------------------------------------------------

def test_an_ascii_stream_cannot_carry_the_glyphs():
    s = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    assert SE.stream_can_carry_glyphs(s) is False


def test_an_ascii_stream_is_reconfigured():
    """cron, systemd, `docker exec`, LANG unset — one ⏺ would otherwise raise
    UnicodeEncodeError from inside a render."""
    s = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    changed = SE.ensure_glyph_capable_streams((("out", s),))
    assert changed and changed[0][0] == "out"
    assert changed[0][1] == "ascii"
    assert SE.stream_can_carry_glyphs(s) is True


def test_reconfigure_also_softens_errors():
    """A glyph that still cannot be represented should degrade to a
    substitute, never to an exception raised from inside a render."""
    s = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    SE.ensure_glyph_capable_streams((("out", s),))
    assert s.errors == "replace"


def test_a_stream_with_no_encoding_is_treated_as_capable():
    """A test double or in-memory buffer — refusing those would fire the
    guard exactly where it is not needed."""
    assert SE.stream_can_carry_glyphs(io.StringIO()) is True


def test_a_stream_that_refuses_reconfiguration_is_left_working():
    """A guard that breaks stdout to protect stdout has inverted itself."""
    class _NoReconf:
        encoding = "ascii"

    assert SE.ensure_glyph_capable_streams((("out", _NoReconf()),)) == []


@pytest.mark.parametrize("bad", [None, object(), 42])
def test_it_never_raises(bad):
    assert SE.stream_can_carry_glyphs(bad) is True
    assert isinstance(SE.ensure_glyph_capable_streams((("x", bad),)), list)


def test_an_unknown_codec_is_a_refusal_not_a_crash():
    class _Weird:
        encoding = "not-a-real-codec"

    assert SE.stream_can_carry_glyphs(_Weird()) is False


# --------------------------------------------------------------------------
# The doubled line
# --------------------------------------------------------------------------

def test_the_chokepoint_delegates_to_the_seam_that_knows_both_facts():
    import inspect

    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    src = inspect.getsource(BattleTestHarness._repl_print)
    assert "_print_mirrored" in src


def test_it_emits_ONCE_when_the_console_relays():
    """The live defect: publish + print, where the console also relays."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    seen = []
    flow = types.SimpleNamespace(
        _print_mirrored=lambda msg, *a, **k: seen.append(msg),
        console=types.SimpleNamespace(
            print=lambda *a, **k: seen.append("SECOND-PATH"),
            relays_prints=True,
        ),
    )
    h = types.SimpleNamespace(
        _serpent_flow=flow, _cockpit_attach_bridge=None,
    )
    BattleTestHarness._repl_print(h, "[cyan]Autonomous Sentinel armed[/cyan]")
    assert len(seen) == 1, seen
    assert "SECOND-PATH" not in seen


def test_it_still_emits_when_there_is_no_serpent_flow():
    """The legacy path must survive — a harness with no flow still speaks."""
    from backend.core.ouroboros.battle_test.harness import BattleTestHarness

    published = []
    bridge = types.SimpleNamespace(publish_line=published.append)
    h = types.SimpleNamespace(
        _serpent_flow=None, _cockpit_attach_bridge=bridge,
    )
    BattleTestHarness._repl_print(h, "[dim]hello[/dim]")
    assert published and "[dim]" not in published[0]
