"""One answer to "what colour does this mean", and a ratchet on the rest.

`ui/theme.py` owned a hex PALETTE and a ColorTier ladder. `serpent_flow._C`
was a SECOND, independent palette of flat standard-ANSI names. They did not
merely risk drifting: on a truecolor terminal the theme-aware surfaces
rendered hex while every `_C` line rendered plain ANSI, at different
fidelity, in the same session.

And 365 call sites bypassed both with raw `[red]` / `[cyan]` literals — so
"what colour is a failure" was answered independently 365 times, with
nothing able to notice divergence.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from backend.core.ouroboros.ui.semantic_tokens import (
    role_palette,
    sem,
    semantic_for,
    style_for,
)


class TestOneOwner:
    def test_the_cockpit_palette_is_DERIVED_not_declared(self):
        """`_C` was a second palette. It is now a projection — every
        `_C['death']` in serpent_flow resolves through the theme."""
        from backend.core.ouroboros.battle_test.serpent_flow import _C
        assert _C["death"] == style_for("death")
        assert _C["life"] == style_for("life")

    def test_it_resolves_LIVE_not_at_import(self):
        """ColorTier is a property of the terminal. A palette frozen at
        import outlives a resize, a --no-color flip, or a client attaching
        from a different terminal than the daemon booted on."""
        from backend.core.ouroboros.battle_test import serpent_flow
        assert type(serpent_flow._C).__name__ == "_SemanticPalette"

    def test_every_role_maps_to_a_theme_semantic(self):
        for role in role_palette():
            assert semantic_for(role) != "ink", role

    def test_a_theme_change_reaches_the_cockpit(self, monkeypatch):
        """The point of one owner. Repaint the theme, and `_C` follows —
        which a second literal dict could never do."""
        from backend.core.ouroboros.ui import theme
        from backend.core.ouroboros.battle_test.serpent_flow import _C
        table = dict(getattr(theme, "_SEMANTIC_STD", {}))
        table["crit"] = "bold magenta"
        monkeypatch.setattr(theme, "_SEMANTIC_STD", table, raising=False)
        assert "magenta" in _C["death"]


class TestTheVocabularyMatchesClaudeCode:
    def test_green_is_SCARCE(self):
        """CC's load-bearing rule: green means "succeeded" or "added" and
        nothing else. When green is also chrome, a successful outcome
        stops being visible."""
        greens = [r for r, s in role_palette().items() if "green" in s]
        assert set(greens) <= {"life", "code_add"}, greens

    def test_paths_are_cyan_and_underlined(self):
        """CC renders paths as clickable-feeling. Underline is carried
        separately from colour, so a terminal that cannot do colour keeps
        the affordance."""
        style = style_for("file")
        assert "underline" in style

    def test_failure_and_removal_share_one_colour(self):
        assert style_for("death") == style_for("code_del")

    def test_metadata_is_the_quietest_role(self):
        assert style_for("dim") in ("dim", "bright_black", "grey50")


class TestItNeverBreaksRendering:
    @pytest.mark.parametrize("role", ["", None, "not_a_role", 42])
    def test_an_unknown_role_still_returns_a_string(self, role):
        assert isinstance(style_for(role), str)   # type: ignore[arg-type]

    def test_a_dead_theme_yields_NO_second_copy_of_the_literals(
            self, monkeypatch):
        """It returns "" rather than its own fallback table.

        The first draft kept a `_FALLBACK` dict here — which was a second
        palette again, the exact defect this module removes, and it also
        tripped `tests/ui/test_theme_guard.py`'s ban on literal styling in
        `ui/`. One fix removed both: the historical literals live in
        exactly one place (serpent_flow's retained seed) and the caller
        falls through to them."""
        import backend.core.ouroboros.ui.semantic_tokens as st

        def _boom(*a, **k):
            raise RuntimeError("theme unavailable")

        monkeypatch.setattr(st, "semantic_for", _boom)
        assert st.style_for("death") == ""
        src = pathlib.Path(
            "backend/core/ouroboros/ui/semantic_tokens.py").read_text()
        assert "_FALLBACK" not in src, "a second palette grew back"

    def test_the_cockpit_palette_survives_a_dead_theme(self, monkeypatch):
        import backend.core.ouroboros.ui.semantic_tokens as st
        from backend.core.ouroboros.battle_test.serpent_flow import _C
        monkeypatch.setattr(
            st, "style_for", lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError("down")))
        assert _C["death"] == "red"      # the retained fallback literal


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------

#: Raw colour literals remaining, by package. A RATCHET: these numbers may
#: only ever go DOWN. They are not a target and not a budget — they are the
#: measured size of the migration, recorded so that the bleeding stops
#: immediately while the existing sites are converted in batches.
#:
#: Touching 365 render sites in one sweep is exactly where a mistyped token
#: silently produces plain text and no test notices, which is why the fix
#: is "stop new ones now, migrate incrementally" rather than one change.
_RAW_LITERAL_CEILING = {
    "backend/core/ouroboros/battle_test": 244,
    "backend/core/ouroboros/cli": 12,
    "backend/core/ouroboros/ui": 3,
    "backend/core/ouroboros/governance": 106,
}

_RAW = re.compile(
    r"\[/?(?:bright_)?(?:red|green|yellow|blue|magenta|cyan|white|black)\b"
    r"[^\]]*\]")


def _count(root: str) -> int:
    n = 0
    for path in pathlib.Path(root).rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        n += len(_RAW.findall(path.read_text(errors="ignore")))
    return n


@pytest.mark.parametrize("root", sorted(_RAW_LITERAL_CEILING))
def test_raw_colour_literals_only_ever_decrease(root):
    """A call site that says `[red]` stops meaning the right thing the
    moment the palette changes; one that says `sem("death")` does not.

    This fails on a NEW raw literal, so the count cannot grow while the
    migration is in progress. When it drops, lower the ceiling — a ratchet
    that is never tightened is just a comment.
    """
    found = _count(root)
    ceiling = _RAW_LITERAL_CEILING[root]
    assert found <= ceiling, (
        f"{root}: {found} raw colour literals, ceiling {ceiling}. "
        f"Use sem('<role>') from ui.semantic_tokens instead of a literal "
        f"colour — the role keeps meaning the right thing when the palette "
        f"changes."
    )
    assert found >= ceiling - 40, (
        f"{root}: down to {found} from {ceiling} — lower the ceiling in "
        f"_RAW_LITERAL_CEILING so the ratchet keeps holding."
    )
