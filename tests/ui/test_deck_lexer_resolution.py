"""The hunk highlighter resolves a lexer without re-reading the world.

`deck.diff` called `Syntax.guess_lexer(path, code)` once PER LINE. Profiled at
19.4ms a call — 7.7s on a 400-line hunk — with ~88% inside
`find_plugin_lexers()` -> `importlib.metadata.entry_points()`, which re-reads
the `entry_points.txt` of all 1125 installed distributions to reach an answer
that cannot change. A loop-invariant filesystem scan inside a per-line call.

The fix hoists the invariant and keeps the variant, so these tests pin BOTH
halves. The first attempt cached by path alone and was measurably faster — and
silently dropped `html+django`, rendering `{% block %}` as plain text. Speed
that loses a feature is not a fix, so the dialect case below is the important
one.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.ui import deck_grammar as dg


@pytest.fixture(autouse=True)
def _cold_caches():
    """Every test starts on a cold cache — a hit from a neighbour would let a
    broken resolver pass on someone else's answer."""
    dg._sole_lexer_alias.cache_clear()
    dg._guess_with_content.cache_clear()
    dg._filename_candidates.cache_clear()
    yield


class TestTheInvariantIsHoisted:
    def test_a_path_only_one_lexer_claims_ignores_content_entirely(self):
        """Pygments returns a sole match WITHOUT reading the content, so
        caching by path is not an approximation — the content provably cannot
        change the answer."""
        assert dg._filename_candidates("x.py") == 1
        first = dg._lexer_for("x.py", "    return rows[0]")
        assert first == dg._lexer_for("x.py", "<div>{% not python %}</div>")
        assert first == "python"

    def test_the_expensive_scan_happens_once_per_path(self):
        for i in range(200):
            dg._lexer_for("backend/x/ov.py", f"    return rows[{i}]")
        info = dg._sole_lexer_alias.cache_info()
        assert info.misses == 1, "the per-path scan repeated"
        assert info.hits == 199

    def test_repeated_lines_do_not_re_ask_even_when_ambiguous(self):
        """A deck repaints the same hunk every frame, so the ambiguous path
        must memoise too — it just cannot memoise on the path alone."""
        for _ in range(50):
            dg._lexer_for("index.html", "{% block content %}")
        info = dg._guess_with_content.cache_info()
        assert info.misses == 1 and info.hits == 49


class TestTheVariantSurvives:
    """The half a path-only cache destroys."""

    def test_an_ambiguous_filename_still_consults_the_content(self):
        assert dg._filename_candidates("index.html") > 1
        plain = dg._lexer_for("index.html", "<div>")
        django = dg._lexer_for("index.html", "{% block content %}")
        assert plain != django, (
            "the dialect was resolved from the path alone — `{% %}` will "
            "render as plain text"
        )
        assert "django" in django

    def test_a_template_tag_is_still_highlighted_in_a_rendered_hunk(self):
        """The end-to-end statement of the same thing, through `diff`."""
        out = dg.diff(7, "+", "{% block content %}", path="index.html",
                      width=80)
        assert "bright_cyan" in out, "the template tag lost its colour"

    def test_a_python_hunk_is_still_highlighted(self):
        out = dg.diff(7, "+", "    return rows[0]", path="x.py", width=80)
        assert "bright_blue" in out


class TestItDegradesRatherThanGuessing:
    def test_an_unclaimed_path_resolves_to_the_default_sentinel(self):
        assert dg._lexer_for("data.unknownext", "zzz") == "default"
        assert dg._lexer_for("", "zzz") == "default"

    def test_a_whole_name_match_still_resolves(self):
        """`Makefile` has no extension; only filename globbing finds it. An
        extension-only shortcut would silently stop highlighting these."""
        assert dg._lexer_for("Makefile", "all:\n\tgcc") == "make"
        assert dg._lexer_for("Dockerfile", "FROM python") == "docker"

    def test_the_private_pygments_coupling_is_guarded(self, monkeypatch):
        """`_filename_candidates` reads pygments' private helpers. If a future
        pygments reorganises them the optimisation must be lost, never the
        answer: `-1` means "could not tell", which routes to the ordinary
        uncached call.
        """
        import pygments.lexers as pl
        monkeypatch.delattr(pl, "_iter_lexerclasses", raising=False)
        dg._filename_candidates.cache_clear()
        assert dg._filename_candidates("x.py") == -1
        assert dg._lexer_for("x.py", "    return 1") == "python"
        assert dg.diff(1, "+", "    return 1", path="x.py", width=80)

    def test_a_broken_pygments_never_takes_the_deck_down(self, monkeypatch):
        """A deck line is chrome; chrome must not be what fails."""
        import pygments.lexers as pl

        def _boom(*a, **k):
            raise RuntimeError("pygments is unwell")

        monkeypatch.setattr(pl, "_iter_lexerclasses", _boom, raising=False)
        monkeypatch.setattr(pl, "get_lexer_for_filename", _boom, raising=False)
        dg._filename_candidates.cache_clear()
        dg._sole_lexer_alias.cache_clear()
        assert dg._filename_candidates("x.py") == -1
        out = dg.diff(1, "+", "    return 1", path="x.py", width=80)
        # Assert the rendered TEXT, not the markup string: a highlighter splits
        # `return 1` across style tags, so a substring check on the markup
        # tests whether highlighting happened to be off rather than whether the
        # operator can read the line.
        from rich.text import Text
        assert "return 1" in Text.from_markup(out).plain, "the line was lost"
