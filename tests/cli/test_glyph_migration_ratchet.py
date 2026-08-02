"""The glyph migration, ratcheted — so it can only ever finish.

`ui/theme` ships six operator-plane glyphs with an ASCII degradation each, "so
16-color/none terminals keep identical geometry". The attach client began with
**75 hardcoded ⏺ / ⎿ / ⚠ / · and zero calls to `mark()`**, which meant the
degradation never fired there: a non-UTF-8 ``LANG`` is ordinary over ssh, cron
and CI, and on those terminals the whole vocabulary rendered as mojibake.

WHY THIS IS A RATCHET AND NOT A SWEEP
--------------------------------------
Three automated migrations were attempted and all three were wrong, the last
one dangerously:

  1. interpolating ``{_glyph("detail", "-")}`` into an f-string is a
     SyntaxError on 3.11 — the inner quote may not match the host's;
  2. switching to single quotes breaks any single-quoted host
     (``audio.lstrip(' · ')``), which shipped a parse error;
  3. concatenation is correct for every host — but the rewrite glued the ``f``
     prefix onto the FUNCTION NAME: ``f_glyph("action", "*") + " {pid} "``.
     That **parses** — ``f_glyph`` is a valid identifier — so ``ast.parse``
     accepted it. At runtime it is a ``NameError`` with ``{pid}`` rendered
     literally, and it was caught by READING the converted lines, not by any
     checker.

A syntax check proved almost nothing, which is the same lesson as the
recursion bug that took the handoff audit to zero and read as a clean bill of
health. So the remaining sites move in small reviewed batches, and this file
guarantees the count cannot grow while that happens.

Deliberately mirrors `test_semantic_colour_tokens`'s
``test_raw_colour_literals_only_ever_decrease`` rather than inventing a second
ratchet idiom: same shape, same lower-the-ceiling discipline, so a reader who
has met one has met both.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_CLIENT = Path("backend/core/ouroboros/cli/ov.py")

#: The six operator-plane glyphs, read from the design language rather than
#: retyped — a second list would drift the moment a glyph is added.
def _tracked_glyphs() -> str:
    from backend.core.ouroboros.ui.theme import _GLYPHS
    return "".join(pair[0] for pair in _GLYPHS.values() if len(pair[0]) == 1)


#: Lines that EMIT. Prose about the glyphs — docstrings, comments, the notes
#: explaining this very migration — must never count, or the ratchet would
#: punish documenting the work.
_OUTPUT = re.compile(r"(say\(|\.print\(|lines\.append\(|append\(|return )")
_PROSE = ("#", '"""', "'''", "*", ":", "•")

#: Current unmigrated OUTPUT sites. LOWER THIS as batches land; never raise it.
_CEILING = 6


def _unmigrated(path: Path) -> list:
    glyphs = _tracked_glyphs()
    out = []
    for n, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").split("\n"), 1
    ):
        stripped = line.lstrip()
        if stripped.startswith(_PROSE) or '"""' in line:
            continue
        if any(g in line for g in glyphs) and _OUTPUT.search(line):
            out.append((n, stripped[:88]))
    return out


class TestTheCountOnlyFalls:
    def test_no_new_hardcoded_glyph_reaches_an_output_site(self):
        found = _unmigrated(_CLIENT)
        assert len(found) <= _CEILING, (
            f"{len(found)} unmigrated glyph output sites, ceiling {_CEILING}. "
            f"A NEW hardcoded glyph means one more surface that renders as "
            f"mojibake on a non-UTF-8 locale — use `_glyph(name, fallback)`. "
            f"Offenders: {found[:5]}"
        )

    def test_the_ceiling_is_lowered_when_it_should_be(self):
        """A ratchet nobody tightens is a ceiling nobody notices. Mirrors the
        colour ratchet's own guard."""
        found = _unmigrated(_CLIENT)
        assert len(found) >= _CEILING - 3, (
            f"down to {len(found)} from {_CEILING} — lower _CEILING so the "
            f"ratchet keeps holding"
        )

    def test_prose_about_glyphs_is_never_counted(self):
        """The docstrings explaining this migration are full of ⏺ and ⎿.
        Counting them would make documenting the work fail the test that
        exists to finish it."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write('# a comment about ⏺ and ⎿\n'
                     '"""docstring naming ⚠ and ·"""\n'
                     'x = 1\n')
            tmp = Path(fh.name)
        try:
            assert _unmigrated(tmp) == []
        finally:
            tmp.unlink(missing_ok=True)

    def test_an_output_site_IS_counted(self):
        """The detector has to actually detect, or the ceiling is decoration."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as fh:
            fh.write('def f():\n    say("⎿ still hardcoded")\n')
            tmp = Path(fh.name)
        try:
            assert len(_unmigrated(tmp)) == 1
        finally:
            tmp.unlink(missing_ok=True)


class TestTheMigratedSitesDegrade:
    @pytest.fixture
    def ascii_only(self, monkeypatch):
        for key in ("LC_ALL", "LC_CTYPE", "LANG"):
            monkeypatch.delenv(key, raising=False)
        from backend.core.ouroboros.ui import theme
        theme.reset_active_tier_cache()
        yield
        theme.reset_active_tier_cache()

    def test_active_ops_line_degrades(self, ascii_only):
        """Batch 1 — three returns in one function, migrated as a unit."""
        from backend.core.ouroboros.cli.ov import _active_ops_line
        for arg in ([], ["op-1"], ["op-a-b-c"] * 9, None):
            assert "⎿" not in _active_ops_line(arg)  # type: ignore[arg-type]

    def test_active_ops_line_keeps_its_content(self):
        """Migration must not disturb pluralisation or truncation — both were
        carefully commented in the function it touched."""
        from backend.core.ouroboros.cli.ov import _active_ops_line
        assert "active ops: none" in _active_ops_line([])
        many = _active_ops_line([f"op-x-{i}" for i in range(9)])
        assert "9 active ops" in many and "(+5 more)" in many
        assert "1 active op:" in _active_ops_line(["op-a-b-c"])

    def test_the_unreadable_branch_still_says_so(self):
        class _Hostile:
            def __str__(self):
                raise RuntimeError("unstringable")
        from backend.core.ouroboros.cli.ov import _active_ops_line
        assert "unreadable" in _active_ops_line([_Hostile()])

    def test_a_fallback_does_not_stutter_against_its_own_label(self, ascii_only):
        """The trap in swapping a glyph for a WORD.

        `audio`'s ASCII degradation is literally "mic", so
        ``_glyph("audio", "mic") + " mic: "`` renders "🎙 mic:" on a UTF-8
        terminal and **"mic mic:"** on an ASCII one — a stutter the unicode
        form hides completely, and one no syntax or degradation check would
        catch. A glyph whose fallback is a word IS the label; the word goes.
        """
        from backend.core.ouroboros.cli.ov import _glyph
        from backend.core.ouroboros.ui.theme import _GLYPHS
        source = _CLIENT.read_text(encoding="utf-8", errors="replace")
        for name, (uni, ascii_fb) in _GLYPHS.items():
            if not ascii_fb.isalpha():
                continue          # punctuation fallbacks cannot stutter
            bad = f'_glyph("{name}", "{ascii_fb}") + " {ascii_fb}'
            assert bad not in source, (
                f"{name}: the fallback {ascii_fb!r} is followed by the same "
                f"word, so an ASCII terminal reads '{ascii_fb} {ascii_fb}…'")

    def test_glyph_helper_never_raises_on_an_unknown_name(self):
        from backend.core.ouroboros.cli.ov import _glyph
        assert _glyph("no-such-glyph", "FB") == "FB"


class TestTheTrapThatShipped:
    def test_no_f_glued_onto_the_helper_name(self):
        """``f_glyph(...)`` parses and is a NameError at runtime.

        This is the exact artefact the third automated pass produced, and the
        one `ast.parse` waved through. Cheap to assert, and it names the
        failure so the next person does not rediscover it.
        """
        source = _CLIENT.read_text(encoding="utf-8", errors="replace")
        assert "f_glyph(" not in source

    def test_the_helper_is_actually_defined(self):
        source = _CLIENT.read_text(encoding="utf-8", errors="replace")
        assert "def _glyph(" in source

    def test_every_glyph_call_names_a_real_mark(self):
        """A typo'd name silently returns the fallback forever — an ASCII
        glyph on a UTF-8 terminal, which looks like a rendering fault rather
        than a bug."""
        from backend.core.ouroboros.ui.theme import _GLYPHS
        source = _CLIENT.read_text(encoding="utf-8", errors="replace")
        for name in re.findall(r'_glyph\(\s*"([a-z_]+)"', source):
            assert name in _GLYPHS, f"_glyph({name!r}) is not in the design language"
