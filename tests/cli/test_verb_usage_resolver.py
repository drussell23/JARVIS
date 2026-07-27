"""Operator help resolved from the code, with nothing authored by hand.

45 verbs showed a blank description. Authoring 45 ``__verb_help__`` strings
would have satisfied the UI while leaving the actual defect in place: there
was no runtime introspection layer, so the help already present in the source
— docstrings, signatures, and the dispatch vocabulary in the function bodies —
was never read.

The cascade, most-authored first:

    __verb_help__  ->  Operator:  ->  docstring prose  ->  mined subcommands
                   ->  derived signature  ->  [undocumented]

Every stage below the first two is DERIVED, so it cannot go stale: change what
a verb accepts and its palette entry changes with it.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import time
from typing import List

import pytest

from backend.core.ouroboros.battle_test.verb_usage import (
    UNDOCUMENTED,
    derive_usage,
    extract_operator_section,
    injected_parameters,
    mine_subcommands,
)


# --------------------------------------------------------------------------
# 1. semantic signature introspection — the two mandated assertions
# --------------------------------------------------------------------------

def test_an_injected_ctx_is_stripped_from_derived_usage() -> None:
    """MANDATE 4(1). ``ctx`` is wiring the router injects; the operator never
    types it, so a usage line that mentions it is actively misleading."""
    def my_verb(ctx, query: str, limit: int = 5):
        ...

    usage = derive_usage(my_verb, "my_verb")
    assert usage == "Usage: /my_verb <query> [limit]"
    assert "ctx" not in usage


def test_required_and_optional_get_the_right_brackets() -> None:
    """MANDATE 4(2). POSIX: ``<required>``, ``[optional]``."""
    def verb(ctx, source, dest, force: bool = False, depth: int = 1):
        ...

    assert derive_usage(verb, "verb") == (
        "Usage: /verb <source> <dest> [force] [depth]"
    )


@pytest.mark.parametrize("name", sorted(injected_parameters())[:12])
def test_every_injected_name_is_hidden(name: str) -> None:
    ns: dict = {}
    exec(f"def verb({name}, real):\n    ...", ns)      # noqa: S102
    usage = derive_usage(ns["verb"], "verb")
    assert usage == "Usage: /verb <real>", f"{name} leaked into {usage!r}"


def test_the_hidden_set_is_extendable_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The calling convention is allowed to evolve; this file should not need
    editing when it does."""
    def verb(tenant_id, query):
        ...

    assert "tenant_id" in derive_usage(verb, "verb")
    monkeypatch.setenv("JARVIS_VERB_USAGE_HIDE", "tenant_id")
    assert derive_usage(verb, "verb") == "Usage: /verb <query>"


def test_var_positional_renders_as_a_repeatable_optional() -> None:
    def verb(ctx, *paths):
        ...

    assert derive_usage(verb, "verb") == "Usage: /verb [paths...]"


def test_var_keyword_is_never_shown() -> None:
    def verb(ctx, name, **kwargs):
        ...

    assert derive_usage(verb, "verb") == "Usage: /verb <name>"


def test_keyword_only_without_a_default_is_required() -> None:
    """Python's calling convention is not the operator's concern — a
    parameter with no default must be supplied, so it renders as required."""
    def verb(ctx, *, target, limit=5):
        ...

    assert derive_usage(verb, "verb") == "Usage: /verb <target> [limit]"


def test_a_fully_injected_signature_yields_nothing_rather_than_a_bare_usage():
    """``Usage: /verb`` with no arguments tells the operator nothing they did
    not know from typing the verb, and would crowd out ``[undocumented]``."""
    def verb(line):
        ...

    assert derive_usage(verb, "verb") == ""


def test_decorators_do_not_mask_the_real_signature() -> None:
    """A wrapper reports ``(*args, **kwargs)`` — useless AND confident."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            return fn(*args, **kwargs)
        return inner

    @deco
    def verb(ctx, query, limit=3):
        ...

    assert derive_usage(verb, "verb") == "Usage: /verb <query> [limit]"


@pytest.mark.parametrize("bad", [None, 42, object(), len, print, lambda: None])
def test_derivation_never_raises_on_anything(bad) -> None:
    assert isinstance(derive_usage(bad, "x"), str)


# --------------------------------------------------------------------------
# 2. Operator: extraction — MANDATE 4, safe evaluation
# --------------------------------------------------------------------------

def test_the_operator_section_wins_and_is_joined() -> None:
    def verb(line):
        """Parse the /thing line. NEVER raises.

        Operator: inspect a running operation and
          print its phase timings.

        Args:
            line: raw text.
        """

    assert extract_operator_section(verb.__doc__) == (
        "inspect a running operation and print its phase timings."
    )


@pytest.mark.parametrize("doc", [
    None, "", "   ", 42, object(), b"bytes", ["a"], {"k": "v"},
])
def test_extraction_is_safe_on_malformed_docstrings(doc) -> None:
    """MANDATE 4(3): ``__doc__`` is ``None`` on a great many callables, and
    this layer runs over callables nobody curated."""
    assert extract_operator_section(doc) == ""


def test_a_docstring_with_no_operator_section_yields_nothing() -> None:
    assert extract_operator_section("Parse it.\n\nArgs:\n    x: y\n") == ""


def test_the_section_stops_at_the_next_heading() -> None:
    doc = "H.\n\nOperator: do the thing.\nReturns:\n    None\n"
    assert extract_operator_section(doc) == "do the thing."


def test_the_section_stops_at_a_blank_line() -> None:
    doc = "H.\n\nOperator: do the thing.\n\nUnrelated prose follows.\n"
    assert extract_operator_section(doc) == "do the thing."


def test_the_marker_is_case_and_indent_insensitive() -> None:
    assert extract_operator_section("\t  OPERATOR:  go\n") == "go"


# --------------------------------------------------------------------------
# 3. mined subcommand vocabulary
# --------------------------------------------------------------------------

def test_the_vocabulary_is_mined_from_the_body() -> None:
    """The signature is blind here — the dispatcher takes one string — so the
    real vocabulary lives in the comparisons the body makes."""
    def dispatch_thing_command(line):
        sub = line.split()[1] if " " in line else ""
        if sub == "status":
            return 1
        if sub == "history":
            return 2
        if sub in ("explain", "why"):
            return 3
        return 0

    assert mine_subcommands(dispatch_thing_command) == [
        "status", "history", "explain", "why",
    ]


def test_source_order_is_preserved() -> None:
    """Dispatchers put the common case first; that beats alphabetical."""
    def verb(line):
        cmd = line
        if cmd == "zebra":
            return 1
        if cmd == "apple":
            return 2

    assert mine_subcommands(verb) == ["zebra", "apple"]


def test_object_state_is_not_mistaken_for_a_vocabulary() -> None:
    """``self.mode == "fast"`` is internal logic that happens to use a
    string. Mining it produces confident nonsense."""
    class Thing:
        def verb(self, line):
            if self.mode == "fast":
                return 1
            if self.mode == "slow":
                return 2

    assert mine_subcommands(Thing.verb) == []


def test_a_single_literal_is_not_a_vocabulary() -> None:
    """One hit is far more likely an internal flag check."""
    def verb(line):
        sub = line
        if sub == "status":
            return 1

    assert mine_subcommands(verb) == []


def test_mining_is_bounded() -> None:
    body = "\n".join(f'    if sub == "s{i}":\n        return {i}'
                     for i in range(40))
    ns: dict = {}
    exec(f"def verb(line):\n    sub = line\n{body}", ns)   # noqa: S102
    assert len(mine_subcommands(ns["verb"])) <= 8


@pytest.mark.parametrize("bad", [None, 42, len, print, lambda x: x])
def test_mining_never_raises(bad) -> None:
    assert isinstance(mine_subcommands(bad), list)


def test_mining_survives_an_unreadable_source() -> None:
    """Functions defined via exec have no retrievable source file."""
    ns: dict = {}
    exec("def verb(line):\n    return 1", ns)              # noqa: S102
    assert mine_subcommands(ns["verb"]) == []


# --------------------------------------------------------------------------
# 4. the cascade, end to end
# --------------------------------------------------------------------------

def _describe(fn):
    from backend.core.ouroboros.battle_test.repl_completion import _describe as d
    return d(fn)


def test_authored_help_still_outranks_everything_derived() -> None:
    import backend.core.ouroboros.governance.moltbook_repl as mr

    assert _describe(mr.dispatch_moltbook_command) == (
        "read the agora feed — the residents' posts"
    )


def test_the_cascade_reaches_undocumented_only_when_truly_empty() -> None:
    """SUPERSEDES the earlier assertion that this returns ``""``.

    A blank column could not be distinguished from a rendering fault, and
    could not be counted. ``[undocumented]`` is the same honesty — it does
    NOT invent a description — while being visible and greppable."""
    def dispatch_nothing_command(line: str):
        """Parse ``/nothing`` line and dispatch. NEVER raises."""

    assert _describe(dispatch_nothing_command) == UNDOCUMENTED


def test_the_real_palette_is_substantially_hydrated() -> None:
    """The point of the exercise, measured on the shipped table."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.battle_test.repl_completion import (
            registry_from_dispatch,
        )
        reg = registry_from_dispatch()
    metas = [d.description for d in reg.verbs]
    assert len(metas) > 50
    blank = [m for m in metas if not str(m).strip()]
    assert blank == [], f"{len(blank)} verbs render an empty column"
    undocumented = [m for m in metas if m == UNDOCUMENTED]
    assert len(undocumented) <= 5, (
        f"{len(undocumented)}/{len(metas)} still undocumented — the "
        f"introspection layer is not reaching the real dispatch table"
    )


# --------------------------------------------------------------------------
# 5. MANDATE 3 — resolved once, never on a keystroke
# --------------------------------------------------------------------------

async def test_resolution_does_not_happen_on_the_keystroke_path() -> None:
    """The cascade reads source and parses ASTs. Paying that per keystroke
    would make typing stutter — the exact failure the palette is meant to
    avoid — so it must be resolved when the registry is built."""
    from prompt_toolkit.document import Document

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import _build_slash_completer
        completer = await asyncio.to_thread(_build_slash_completer)
    assert completer is not None
    inner = getattr(completer, "completer", completer)

    # Warm nothing deliberately: the FIRST keystroke must already be cheap.
    started = time.perf_counter()
    out = list(inner.get_completions(Document("/"), None))
    elapsed = time.perf_counter() - started

    assert len(out) > 50
    assert elapsed < 0.05, (
        f"first '/' took {elapsed*1000:.0f}ms — the cascade is being "
        f"evaluated per keystroke instead of cached at build"
    )


async def test_repeated_completions_stay_flat() -> None:
    """No lazy per-entry work that only shows up under a real typist."""
    from prompt_toolkit.document import Document

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import _build_slash_completer
        completer = await asyncio.to_thread(_build_slash_completer)
    inner = getattr(completer, "completer", completer)

    timings: List[float] = []
    for probe in ("/", "/m", "/mo", "/mol", "/molt", "/"):
        t0 = time.perf_counter()
        list(inner.get_completions(Document(probe), None))
        timings.append(time.perf_counter() - t0)
    assert max(timings) < 0.05, f"keystroke cost spiked: {timings}"


async def test_the_completer_builds_off_the_event_loop() -> None:
    """Building primes the registry, imports packages and parses ASTs — it
    must never run on the loop that is trying to draw the prompt."""
    from backend.core.ouroboros.battle_test.repl_completion import (
        build_attach_completer,
    )
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        completer = await asyncio.to_thread(build_attach_completer)
    assert type(completer).__name__ == "ThreadedCompleter", (
        "completion must be threaded; on the loop the keystroke that opens "
        "the menu is the one that freezes"
    )
