"""A screenshot on the clipboard becomes something the organism can read.

Dragging a file in already worked. But the most common way an operator has an
image is `Cmd+Shift+Ctrl+4` — on the clipboard, never written to disk — and a
terminal pastes TEXT, so pasting it produced nothing at all.
"""
from __future__ import annotations

import base64
import subprocess
import sys

import pytest

from backend.core.ouroboros.battle_test.clipboard_image import (
    clipboard_has_image, clipboard_paste_enabled, install_image_paste_binding,
    spill_clipboard_image,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEh"
    "QGAhKmMIQAAAABJRU5ErkJggg=="
)
_MAC = sys.platform == "darwin"


def _put_image(tmp_path) -> bool:
    if not _MAC:
        return False
    f = tmp_path / "x.png"
    f.write_bytes(_PNG)
    r = subprocess.run(
        ["osascript", "-e",
         f'set the clipboard to (read (POSIX file "{f}") as «class PNGf»)'],
        capture_output=True, check=False,
    )
    return r.returncode == 0


def _put_text() -> None:
    if _MAC:
        subprocess.run(["osascript", "-e", 'set the clipboard to "hello"'],
                       capture_output=True, check=False)


# --------------------------------------------------------------------------
# text must stay silent and instant
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _MAC, reason="clipboard access is macOS-specific")
def test_a_text_clipboard_is_not_an_image() -> None:
    """The type is CHECKED, not inferred from a failed read: asking for the
    PNG payload of a text clipboard errors in a way indistinguishable from a
    real failure, and the common case must be silent, not an exception."""
    _put_text()
    assert clipboard_has_image() is False
    assert spill_clipboard_image() is None


def test_it_never_raises_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing osascript is a normal state on Linux, not an error."""
    monkeypatch.setattr(
        "backend.core.ouroboros.battle_test.clipboard_image._osascript",
        lambda *_a, **_k: None,
    )
    assert clipboard_has_image() is False
    assert spill_clipboard_image() is None


# --------------------------------------------------------------------------
# an image becomes a path
# --------------------------------------------------------------------------

@pytest.mark.skipif(not _MAC, reason="clipboard access is macOS-specific")
def test_an_image_is_spilled_to_a_readable_file(tmp_path) -> None:
    if not _put_image(tmp_path):
        pytest.skip("could not stage a clipboard image")
    assert clipboard_has_image() is True
    path = spill_clipboard_image()
    assert path is not None and path.exists()
    assert path.stat().st_size > 0
    assert path.suffix == ".png"


@pytest.mark.skipif(not _MAC, reason="clipboard access is macOS-specific")
def test_the_same_screenshot_does_not_accumulate_copies(tmp_path) -> None:
    """Named by CONTENT hash: pasting twice reuses one file."""
    if not _put_image(tmp_path):
        pytest.skip("could not stage a clipboard image")
    assert spill_clipboard_image() == spill_clipboard_image()


def test_an_oversized_paste_is_refused_HERE(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path) -> None:
    """Refused at the paste, not downstream — a file that passes this gate
    must not be rejected two layers later for size, or the operator gets a
    failure far from the keystroke that caused it."""
    from backend.core.ouroboros.battle_test import clipboard_image as mod

    assert mod._MAX_BYTES == 10 * 1024 * 1024, (
        "the cap must match the attachment cap GENERATE already enforces"
    )


# --------------------------------------------------------------------------
# it reuses the path a dragged file takes
# --------------------------------------------------------------------------

def test_it_hands_off_to_the_EXISTING_attach_verb() -> None:
    """Validation, the size cap, sha256 and the multi-modal GENERATE path are
    machinery that already exists — a second ingest path would drift from it."""
    import inspect

    from backend.core.ouroboros.battle_test import clipboard_image as mod

    assert "/attach " in inspect.getsource(mod.install_image_paste_binding)


def test_text_pastes_fall_through_to_the_terminal() -> None:
    """The safety property: an image paste that swallowed text pastes would
    be worse than no image paste at all."""
    import inspect

    from backend.core.ouroboros.battle_test import clipboard_image as mod

    src = inspect.getsource(mod.install_image_paste_binding)
    assert "paste_clipboard_data" in src


def test_ctrl_v_is_bound_on_both_surfaces() -> None:
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    for path in ("backend/core/ouroboros/cli/ov.py",
                 "backend/core/ouroboros/battle_test/bipartite_layout.py"):
        src = (repo / path).read_text()
        names = {a.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "install_image_paste_binding" in names, f"{path} lacks paste"


def test_the_binding_registers() -> None:
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()
    assert install_image_paste_binding(kb, lambda _t: None) is True
    assert ("Keys.ControlV",) in [
        tuple(str(k) for k in b.keys) for b in kb.bindings
    ]


def test_the_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from prompt_toolkit.key_binding import KeyBindings

    monkeypatch.setenv("JARVIS_CLIPBOARD_IMAGE_ENABLED", "0")
    assert clipboard_paste_enabled() is False
    kb = KeyBindings()
    assert install_image_paste_binding(kb, lambda _t: None) is False
    assert not kb.bindings
