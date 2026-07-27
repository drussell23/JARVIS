"""`@path` mentions on the surface the operator actually types into.

`repl_input_polish.extract_attachments` has parsed these since it shipped —
but it was wired into SerpentFlow's own REPL, which lives on a headless daemon
nobody types into. The operator's input surface never called it, so
`@backend/auth.py` travelled upstream as ordinary prose.

Ninth instance this session of a producer wired to a surface nobody watches.

And a mention the operator cannot TYPE is a feature only someone who already
knows the tree can use — so completion is half the work, not a nicety.
"""
from __future__ import annotations

import contextlib
import io
from pathlib import Path
from typing import Any, List

import pytest

from prompt_toolkit.document import Document

from backend.core.ouroboros.battle_test.repl_completion import (
    MentionPathCompleter,
)

_REPO = Path(__file__).resolve().parents[2]


def _ov():
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli import ov
    return ov


class _Client:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send_input(self, text: str) -> bool:
        self.sent.append(text)
        return True

    def send_audio(self, _cmd: str) -> bool:
        return True


def _complete(probe: str, limit: int = 5) -> List[str]:
    c = MentionPathCompleter(root=str(_REPO))
    return [x.text for x in c.get_completions(Document(probe), None)][:limit]


# --------------------------------------------------------------------------
# 1. the client recognises a mention
# --------------------------------------------------------------------------

def test_a_real_path_is_recognised() -> None:
    assert _ov()._extract_mentions("look at @backend/core/auth.py please") == [
        "backend/core/auth.py",
    ]


def test_prose_is_not_a_mention() -> None:
    """`@here` is a word. The parser that already decides this is the ONE
    that decides it — a second rule in the client would eventually disagree
    with the daemon about which is which."""
    assert _ov()._extract_mentions("hey @here can you look") == []


def test_several_mentions_keep_their_order() -> None:
    assert _ov()._extract_mentions("@a/b.py and @c/d.py") == ["a/b.py", "c/d.py"]


@pytest.mark.parametrize("junk", ["", None, 42, "   "])
def test_extraction_never_raises(junk: Any) -> None:
    assert isinstance(_ov()._extract_mentions(junk), list)


# --------------------------------------------------------------------------
# 2. the line is relayed UNCHANGED
# --------------------------------------------------------------------------

def test_the_daemon_receives_the_original_line() -> None:
    """Stripping the mention client-side would make the two surfaces disagree
    about what was said — and attachment resolution belongs on the daemon,
    which is where the files are."""
    ov = _ov()
    client, ui = _Client(), ov.AttachUI()
    ov._route_operator_line(client, ui, "fix @backend/core/auth.py now")
    assert client.sent == ["fix @backend/core/auth.py now"]


def test_the_operator_is_told_they_were_understood() -> None:
    """A mention that produces no acknowledgement is indistinguishable from
    one that was read as prose."""
    ov = _ov()
    ui = ov.AttachUI()
    ov._route_operator_line(_Client(), ui, "fix @backend/core/auth.py")
    assert "attached" in ui.prompt()


def test_a_line_with_no_mention_says_nothing() -> None:
    ov = _ov()
    ui = ov.AttachUI()
    ov._route_operator_line(_Client(), ui, "just a normal goal")
    assert "attached" not in ui.prompt()


def test_the_acknowledgement_stays_short() -> None:
    """Ten mentions must not push the deck off screen."""
    ov = _ov()
    ui = ov.AttachUI()
    line = " ".join(f"@a/f{i}.py" for i in range(10))
    ov._route_operator_line(_Client(), ui, line)
    flash = [ln for ln in ui.prompt().splitlines() if "attached" in ln]
    assert flash and len(flash[0]) < 120


# --------------------------------------------------------------------------
# 3. mentions are typeable
# --------------------------------------------------------------------------

def test_a_partial_path_completes() -> None:
    out = _complete("see @thin_cl")
    assert any("thin_client.py" in p for p in out)


def test_a_directory_prefix_completes() -> None:
    out = _complete("fix @backend/core/ouroboros/cli/o")
    assert any(p.endswith("ov.py") for p in out)


def test_prefix_matches_rank_above_mere_containment() -> None:
    """What the operator is typing beats what merely contains it."""
    out = _complete("@backend/core/ouroboros/cli/ov")
    assert out and out[0].startswith("backend/core/ouroboros/cli/ov")


def test_no_sigil_offers_nothing() -> None:
    """Completion must not fire on ordinary prose."""
    assert _complete("no sigil here") == []


def test_a_finished_mention_stops_completing() -> None:
    """An `@` earlier in a sentence must not re-open the menu on every later
    keystroke."""
    assert _complete("done @a/b.py and then") == []


def test_results_are_bounded() -> None:
    """A bare `@` matches everything; the menu must stay a menu."""
    assert len(_complete("@", limit=999)) <= 12


def test_it_is_rooted_in_the_REPO_not_the_cwd() -> None:
    """prompt_toolkit's PathCompleter completes from the process CWD and
    would offer `/etc/passwd`. Mentions name files the organism works on, so
    the walk is rooted there and cannot climb out — a completer offering what
    the daemon will refuse to read teaches nothing."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(MentionPathCompleter))
    assert "JARVIS_REPO_PATH" in src
    # AST, not substring: this class's own DOCSTRING explains that it is
    # deliberately NOT PathCompleter, and a text search cannot tell that
    # explanation from a use of it. (Use-vs-mention — the fifth time this
    # trap has caught a test in this codebase.)
    used = {
        alias.name
        for node in ast.walk(ast.parse(src))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "PathCompleter" not in used


def test_noise_directories_are_skipped() -> None:
    out = _complete("@", limit=999)
    assert not any(".git/" in p or "__pycache__" in p for p in out)


def test_completion_never_raises_on_a_bad_root() -> None:
    c = MentionPathCompleter(root="/nonexistent/nowhere")
    assert list(c.get_completions(Document("@x"), None)) == []


def test_the_verb_palette_still_works_alongside_it() -> None:
    """One surface, two vocabularies — `/` for verbs, `@` for files. Adding
    the second must not cost the first."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        from backend.core.ouroboros.cli.ov import _build_slash_completer
        completer = _build_slash_completer()
    inner = getattr(completer, "completer", completer)
    out = [c.text for c in inner.get_completions(Document("/"), None)]
    assert len(out) > 50
