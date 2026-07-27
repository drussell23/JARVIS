"""A dropped file becomes something the organism understands.

Terminals inject an ABSOLUTE PATH on drag-and-drop — often shell-quoted,
sometimes a `file://` URI, sometimes with escaped spaces. The operator saw
that string land in their prompt and had to know, unprompted, that O+V wants
`@relative` for repo files and `/attach /abs` for everything else.

    /Users/dj/repos/JARVIS/backend/auth.py  →  @backend/auth.py
    /Users/dj/Desktop/screenshot.png        →  /attach /Users/dj/Desktop/screenshot.png

Translating rather than transporting is the load-bearing choice. Carrying the
bytes would mean base64 over a UDS bridge shared with op chrome, the token
mirror and the heartbeat — a megabyte would block every one of them behind it.
The daemon already reads files by path and is the process holding the repo, so
a path is both smaller and more truthful than a copy.
"""
from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.battle_test.drop_translate import (
    drop_translation_enabled,
    install_drop_translation,
    normalise_dropped_path,
    translate_drop,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    img = tmp_path / "screenshot.png"
    img.write_bytes(b"\x89PNG")
    return img


@pytest.fixture()
def inside() -> Path:
    return _REPO / "docs" / "OV_STYLE_GUIDE.md"


class _Buffer:
    def __init__(self) -> None:
        self.text = ""

    def insert_text(self, data: Any, *_a: Any, **_k: Any) -> None:
        self.text += str(data)


# --------------------------------------------------------------------------
# 1. the two destinations
# --------------------------------------------------------------------------

def test_a_repo_file_becomes_a_mention(inside: Path) -> None:
    out, kind = translate_drop(str(inside), _REPO)
    assert kind == "mention"
    assert out == "@docs/OV_STYLE_GUIDE.md"


def test_a_file_outside_the_repo_becomes_an_attach(outside: Path) -> None:
    """`@mentions` are repo-relative by construction. Forcing one here would
    produce `@../../Desktop/x`, which the daemon refuses and the operator
    would not understand."""
    out, kind = translate_drop(str(outside), _REPO)
    assert kind == "attach"
    assert out == f"/attach {outside}"


def test_the_inside_check_uses_the_completers_rooting() -> None:
    """A path the `@` completer would offer and a path a drop translates to
    must never disagree about what counts as "in the repo"."""
    import inspect

    from backend.core.ouroboros.battle_test import drop_translate

    src = inspect.getsource(drop_translate)
    assert "JARVIS_REPO_PATH" in src


# --------------------------------------------------------------------------
# 2. the spellings a terminal actually produces
# --------------------------------------------------------------------------

def test_a_file_uri_is_understood(outside: Path) -> None:
    out, kind = translate_drop(f"file://{outside}", _REPO)
    assert kind == "attach" and str(outside) in out


def test_quotes_are_stripped(outside: Path) -> None:
    for quote in ("'", '"'):
        out, kind = translate_drop(f"{quote}{outside}{quote}", _REPO)
        assert kind == "attach", f"{quote}-quoted drop not recognised"


def test_escaped_spaces_are_unescaped() -> None:
    """macOS Terminal emits `my\\ file.png`, which does not resolve as-is."""
    assert normalise_dropped_path("/tmp/my\\ file.png") == "/tmp/my file.png"


def test_a_percent_encoded_uri_is_decoded(tmp_path: Path) -> None:
    spaced = tmp_path / "my shot.png"
    spaced.write_bytes(b"x")
    out, kind = translate_drop(f"file://{str(spaced).replace(' ', '%20')}", _REPO)
    assert kind == "attach" and "my shot.png" in out


# --------------------------------------------------------------------------
# 3. nothing is guessed
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "just a sentence about /Users/dj/thing",
    "backend/core/x.py",                       # relative — prose or a mention
    "/nope/does/not/exist.png",                # looks like a path, is not one
    "https://example.com/image.png",           # a URL, not a drop
    "",
])
def test_text_that_is_not_a_drop_is_left_ALONE(text: str) -> None:
    """Rewriting what the operator did not mean is worse than making them
    type the verb — it happens silently and they must notice to undo it."""
    out, kind = translate_drop(text, _REPO)
    assert kind == "" and out == text


def test_a_multiline_paste_is_never_a_drop() -> None:
    """A drop is one path. Treating a pasted diff as a filename would be
    absurd, and is exactly the silent rewrite this must not do."""
    pasted = "def f():\n    return /tmp/x.png\n"
    assert translate_drop(pasted, _REPO)[1] == ""


def test_an_unlisted_extension_is_left_alone() -> None:
    """Deliberately not "any file": a dropped binary or Makefile is usually a
    mistake, whereas an image or document is unambiguous about intent."""
    makefile = _REPO / "Makefile"
    if makefile.is_file():
        assert translate_drop(str(makefile), _REPO)[1] == ""


def test_the_extension_set_is_extendable_without_a_code_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the organism can ingest is allowed to grow."""
    odd = tmp_path / "capture.heic2"
    odd.write_bytes(b"x")
    assert translate_drop(str(odd), _REPO)[1] == ""
    monkeypatch.setenv("JARVIS_DROP_EXTRA_EXTENSIONS", "heic2")
    assert translate_drop(str(odd), _REPO)[1] == "attach"


@pytest.mark.parametrize("junk", [None, 42, object(), b"bytes"])
def test_translation_never_raises(junk: Any) -> None:
    out, kind = translate_drop(junk, _REPO)
    assert isinstance(out, str) and isinstance(kind, str)


# --------------------------------------------------------------------------
# 4. the interceptor
# --------------------------------------------------------------------------

async def test_a_dropped_path_is_translated_in_the_buffer(
    outside: Path,
) -> None:
    """MANDATE: simulate a paste of an absolute .png and assert the buffer
    holds the translated command, with nothing raised."""
    buf = _Buffer()
    assert install_drop_translation(buf, _REPO) is True
    buf.insert_text(str(outside))
    assert buf.text == f"/attach {outside}"


async def test_a_dropped_repo_file_becomes_a_mention_in_the_buffer(
    inside: Path,
) -> None:
    buf = _Buffer()
    install_drop_translation(buf, _REPO)
    buf.insert_text(str(inside))
    assert buf.text == "@docs/OV_STYLE_GUIDE.md"


async def test_ordinary_typing_is_untouched() -> None:
    """The check is skipped entirely for short inserts, so per-keystroke cost
    stays nil."""
    buf = _Buffer()
    install_drop_translation(buf, _REPO)
    for ch in "fix the flaky test":
        buf.insert_text(ch)
    assert buf.text == "fix the flaky test"


async def test_a_normal_paste_is_untouched() -> None:
    buf = _Buffer()
    install_drop_translation(buf, _REPO)
    buf.insert_text("please look at the auth flow")
    assert buf.text == "please look at the auth flow"


def test_installing_on_nothing_is_survivable() -> None:
    assert install_drop_translation(None, _REPO) is False


def test_a_translation_fault_still_inserts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paste must never fail to paste."""
    import backend.core.ouroboros.battle_test.drop_translate as dt

    monkeypatch.setattr(dt, "translate_drop", lambda *_a, **_k: 1 / 0)
    buf = _Buffer()
    dt.install_drop_translation(buf, _REPO)
    buf.insert_text("/some/absolute/path.png")
    assert buf.text == "/some/absolute/path.png"


def test_the_switch_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped file that does nothing is the worse default."""
    assert drop_translation_enabled() is True
    monkeypatch.setenv("JARVIS_DROP_TRANSLATE_ENABLED", "0")
    assert drop_translation_enabled() is False


def test_it_is_installed_on_the_clients_buffer() -> None:
    """Structural: an uninstalled interceptor is the wired-but-inert shape."""
    src = (_REPO / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "install_drop_translation(session.app.current_buffer)" in src


def test_no_file_bytes_are_read() -> None:
    """Translating rather than transporting: base64 over the UDS bridge would
    block op chrome, the token mirror and the heartbeat behind it."""
    src = (_REPO / "backend/core/ouroboros/battle_test/"
           "drop_translate.py").read_text()
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
    }
    assert not any("b64" in c or "read_bytes" in c for c in calls)
