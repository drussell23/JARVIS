"""The `/` palette — browsable, complete, and never inventive.

Three defects this pins, all found by probing the shipped completer rather
than by reading it:

1. ``/`` yielded 8 of 60 verbs. ``fuzzy_match``'s ``max_results`` default is 8
   because it was written for "did you mean", and reusing it for a palette
   silently truncated the menu to the first eight alphabetically. The operator
   saw /anticipate../bus and concluded the palette was broken.

2. ``/deck`` yielded ``/bus, /cost, /m10``. No verb starts with "/deck" — it
   is client-handled and absent from the dispatch registry — so the fuzzy
   fallback answered a valid verb with edit-distance noise. A palette that
   invents plausible wrong answers is worse than one that says nothing.

3. Client verbs were missing entirely, which is what caused (2).
"""
from __future__ import annotations

import contextlib
import io
from typing import List, Tuple

import pytest


def _completions(text: str) -> List[Tuple[str, str]]:
    from prompt_toolkit.document import Document

    from backend.core.ouroboros.cli.ov import _build_slash_completer
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        c = _build_slash_completer()
        assert c is not None, "no completer was built"
        return [
            (o.text, o.display_meta_text)
            for o in c.get_completions(Document(text), None)
        ]


# --------------------------------------------------------------------------
# 1. browsable
# --------------------------------------------------------------------------

def test_a_bare_slash_offers_the_whole_palette() -> None:
    out = _completions("/")
    assert len(out) > 50, (
        f"only {len(out)} verbs offered — the 'did you mean' cap of 8 is "
        f"truncating a browsable menu"
    )


def test_no_entry_shows_maintainer_prose_as_help() -> None:
    """SUPERSEDES an earlier assertion that every verb must carry meta.

    That test was written while descriptions were mined from module
    docstrings, and it passed by accepting things like "/autobiography REPL —
    Wave 1 #8". Filling the column was the wrong goal: a blank reads as
    "nobody has written this yet", whereas confident noise reads as a
    description that happens to be wrong, and the operator acts on it.

    The invariant that actually matters is that nothing SHOWN is junk."""
    bad = []
    for verb, meta in _completions("/"):
        m = meta.strip()
        if not m:
            continue                      # honest blank — awaiting authored help
        low = m.lower()
        if "never raises" in low or m.startswith("§") or "slice " in low:
            bad.append((verb, m))
        if low.rstrip(".").endswith((" repl", " dispatcher", " by", " for")):
            bad.append((verb, m))
    assert bad == [], f"maintainer prose shown as help: {bad[:4]}"


def test_authored_verbs_do_carry_help() -> None:
    """The verbs that HAVE been written for an operator must show it."""
    described = {t: m for t, m in _completions("/") if m.strip()}
    for verb in ("/molt", "/moltbook", "/deck", "/wake"):
        assert described.get(verb), f"{verb} has authored help but shows none"


def test_prefix_narrows_the_palette() -> None:
    out = [t for t, _m in _completions("/mol")]
    assert set(out) == {"/molt", "/moltbook"}


# --------------------------------------------------------------------------
# 2. client verbs are in the same palette
# --------------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["/deck", "/wake", "/detach"])
def test_client_handled_verbs_are_offered(verb: str) -> None:
    """They never reach the daemon, so a dispatch-only registry omits them —
    and their absence is what sent /deck into the fuzzy fallback."""
    assert any(t == verb for t, _m in _completions(verb)), (
        f"{verb} is handled by the cockpit but missing from its own palette"
    )


def test_deck_resolves_to_itself_not_to_noise() -> None:
    out = [t for t, _m in _completions("/deck")]
    assert out == ["/deck"], (
        f"/deck offered {out} — the fuzzy fallback is answering a valid verb "
        f"with edit-distance guesses"
    )


def test_the_palette_and_the_router_share_one_table() -> None:
    """DRY, asserted: a verb the router dispatches must be offerable, so the
    menu cannot drift from what the CLI actually accepts."""
    from backend.core.ouroboros.cli.ov import AUDIO_VERBS, client_verbs

    offered = {t.lstrip("/") for t, _m in _completions("/")}
    for verb in AUDIO_VERBS:
        if " " in verb:          # multi-word forms are typed, not completed
            continue
        assert verb in offered, f"router accepts {verb!r}; palette omits it"
    assert "deck" in client_verbs()


# --------------------------------------------------------------------------
# 3. it never invents
# --------------------------------------------------------------------------

def test_a_genuine_typo_still_gets_suggestions() -> None:
    """Fuzzy is right for a typo of a REAL verb — that behaviour is kept."""
    out = [t for t, _m in _completions("/moltbok")]
    assert any("molt" in t for t in out), "typo recovery was lost"


def test_prose_never_triggers_the_palette() -> None:
    """Operators type goals and sentences; a menu must not interleave."""
    assert _completions("fix the failing test") == []


def test_the_local_repl_default_is_unchanged() -> None:
    """build_completer's default cap must stay 8 — SerpentFlow's palette is a
    suggestion list and was not part of this change."""
    import inspect

    from backend.core.ouroboros.battle_test.repl_completion import (
        build_completer,
    )
    sig = inspect.signature(build_completer)
    assert sig.parameters["max_results"].default == 8


def test_completer_is_threaded() -> None:
    """Priming the registry walks packages; on the event loop that would
    freeze the very keystroke that opened the menu."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import _build_slash_completer
        c = _build_slash_completer()
    assert type(c).__name__ == "ThreadedCompleter"


# --------------------------------------------------------------------------
# 4. the palette must be wired to the surface the operator ACTUALLY types into
# --------------------------------------------------------------------------

def _cockpit_app():
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        build_bipartite_application,
    )
    from backend.core.ouroboros.cli.ov import _build_slash_completer
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        mux = BipartiteLayout(width=100, height=20, title="t")
        return build_bipartite_application(
            mux, on_accept=lambda _t: None,
            completer=_build_slash_completer(),
        )


def test_the_cockpit_layout_can_actually_draw_a_menu() -> None:
    """THE bug the operator reported: completions computed, nothing drawn.

    Asserted as an INVARIANT — the layout contains somewhere for the palette
    to render — rather than as a mechanism. This test originally required the
    root to be a FloatContainer, which was true while the menu was
    prompt_toolkit's Float widget and became false the moment the palette
    moved to a full-width row above the prompt. The mechanism changed; the
    thing worth protecting did not.

    Either shape satisfies it:
      * a FloatContainer carrying a CompletionsMenu float, or
      * a ConditionalContainer palette row participating in the layout."""
    from prompt_toolkit.layout import ConditionalContainer, FloatContainer

    app = _cockpit_app()
    root = app.layout.container

    if isinstance(root, FloatContainer):
        # THIRD mechanism for the same invariant (2026-07-27). The float used
        # to be prompt_toolkit's CompletionsMenu; it is now our page-style
        # palette, moved into a float so asynchronous canvas traffic cannot
        # pull it into a grid reflow. Either satisfies "there is somewhere for
        # completions to render", which is the thing worth protecting — the
        # widget class never was.
        assert root.floats, "FloatContainer root with no floats at all"
        return

    kids = list(getattr(root, "get_children", lambda: [])())
    assert any(isinstance(k, ConditionalContainer) for k in kids), (
        "the cockpit layout has neither a completions float nor a palette "
        "row — completions are computed and silently discarded"
    )


def test_the_cockpit_prompt_buffer_has_the_completer() -> None:
    from prompt_toolkit.layout.controls import BufferControl

    app = _cockpit_app()
    wired = []
    for window in app.layout.walk():
        control = getattr(window, "content", None)
        if isinstance(control, BufferControl):
            b = control.buffer
            wired.append((b.completer is not None, bool(b.complete_while_typing())))
    assert wired, "no input buffer found in the cockpit layout"
    assert any(has and typing for has, typing in wired), (
        f"prompt buffer lacks a completer or complete_while_typing: {wired}"
    )


def test_a_cockpit_without_a_completer_still_builds() -> None:
    """The palette is additive — a completer-less cockpit must not become a
    FloatContainer it does not need, and must still type."""
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout,
        build_bipartite_application,
    )
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        mux = BipartiteLayout(width=100, height=20, title="t")
        app = build_bipartite_application(mux, on_accept=lambda _t: None)
    assert app is not None


# --------------------------------------------------------------------------
# 5. aesthetics — brand styling, and honest descriptions
# --------------------------------------------------------------------------

def test_the_cockpit_carries_the_brand_style() -> None:
    """Without a Style, prompt_toolkit paints a filled light-grey listbox and
    a reverse-video toolbar — the two loudest things on a dark screen."""
    app = _cockpit_app()
    assert app.style is not None, "cockpit has no Style; menu renders default"
    rules = dict(app.style.style_rules)
    for cls in ("completion-menu.completion",
                "completion-menu.completion.current",
                "completion-menu.meta.completion",
                "bottom-toolbar"):
        assert cls in rules, f"{cls} unstyled — falls back to pt defaults"
    assert "noreverse" in rules["bottom-toolbar"], (
        "the footer is still reverse-video: a solid bar of colour that says "
        "nothing"
    )


def test_the_style_comes_from_the_one_palette() -> None:
    """DRY: the brand owns its colours in ui.theme, not restated per widget."""
    from backend.core.ouroboros.ui.theme import PALETTE, cockpit_prompt_style

    rules = dict(cockpit_prompt_style().style_rules)
    blob = " ".join(rules.values())
    assert PALETTE["venom_green"] in blob or "ansibrightgreen" in blob
    assert PALETTE["venom_purple"] in blob or "ansimagenta" in blob


@pytest.mark.parametrize("junk", [
    "NEVER raises.",
    "Parse ``/anticipate`` line. NEVER raises.",
    "Parse a ``/canvas`` line and dispatch. NEVER raises.",
    "§32.11 Slice 4 canonical entry point — auto-discovered.",
    "Canonical entry point — by",
    "/causal REPL",
    "/continuity REPL dispatcher",
    "+ dispatch",
])
def test_maintainer_prose_is_not_shown_as_help(junk: str) -> None:
    """These are contracts with whoever maintains the function. Shown in a
    menu they look like descriptions and answer nothing."""
    from backend.core.ouroboros.battle_test.repl_completion import _humanise

    assert _humanise(junk) == "", f"{junk!r} leaked into the palette"


def test_authored_help_is_preferred_over_any_docstring() -> None:
    """The only source written FOR an operator wins."""
    from backend.core.ouroboros.battle_test.repl_completion import _describe

    import backend.core.ouroboros.governance.moltbook_repl as mr
    assert _describe(mr.dispatch_moltbook_command) == (
        "read the agora feed — the residents' posts"
    )
    assert _describe(mr.dispatch_molt_command) == (
        "post to the agora as @the-human"
    )


def test_an_undescribed_verb_is_marked_not_fabricated() -> None:
    """SUPERSEDED 2026-07-26: was ``== ""``.

    The honesty requirement is unchanged and still the point — nothing may
    invent a description. What changed is how the absence is SPOKEN. A blank
    column is indistinguishable from a rendering fault and cannot be counted;
    ``[undocumented]`` is visible and greppable, so the remaining gaps can be
    listed instead of silently tolerated.

    Full coverage of the cascade that now sits in front of this lives in
    tests/cli/test_verb_usage_resolver.py."""
    from backend.core.ouroboros.battle_test.repl_completion import _describe
    from backend.core.ouroboros.battle_test.verb_usage import UNDOCUMENTED

    def dispatch_nothing_command(line: str):
        """Parse ``/nothing`` line and dispatch. NEVER raises."""

    assert _describe(dispatch_nothing_command) == UNDOCUMENTED


def test_modules_declare_palette_help_beside_their_aliases() -> None:
    """One place per module for palette metadata — the __aliases__ convention
    extended, not a second registry."""
    import backend.core.ouroboros.governance.moltbook_repl as mr

    assert isinstance(getattr(mr, "__verb_help__", None), dict)
    assert set(mr.__verb_help__) >= {"moltbook", "molt"}
