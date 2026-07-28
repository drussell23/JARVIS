"""The CC-docs remnant sweep — paste chips, emoji, vim mode, Ctrl+J,
custom statusline segment.

Everything else on the interactive-interface pages is either shipped
(nine PRs of this arc + the parallel session's) or a deliberate
divergence (themes, output styles, manual model pickers). These pin the
last five.
"""
from __future__ import annotations

import subprocess

import pytest

from backend.core.ouroboros.battle_test import emoji_shortcodes as emo
from backend.core.ouroboros.battle_test import paste_chips as pc


# --------------------------------------------------------------------------
# 1. paste chips
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_store():
    pc.reset_for_tests()
    yield
    pc.reset_for_tests()


def test_small_pastes_stay_inline() -> None:
    assert pc.should_collapse("one line") is False
    assert pc.should_collapse("a\nb\nc\nd") is False  # editable, on purpose


def test_big_pastes_collapse_and_expand_losslessly() -> None:
    blob = "\n".join(f"line {i}" for i in range(40))
    chip = pc.store_paste(blob)
    assert chip == "[Pasted text #1 +40 lines]"
    assert pc.expand_paste_chips(f"please read this: {chip}") == (
        f"please read this: {blob}"
    )


def test_char_threshold_collapses_single_line_walls() -> None:
    assert pc.should_collapse("x" * 1300) is True


def test_unknown_chip_stays_visible_never_a_silent_hole() -> None:
    text = "see [Pasted text #9 +5 lines]"
    assert pc.expand_paste_chips(text) == text


def test_store_is_bounded() -> None:
    for i in range(30):
        pc.store_paste(f"blob {i}\n" * 20)
    assert len(pc._STORE) <= pc._MAX_STORED


def test_master_flag_off_means_raw_insert(monkeypatch) -> None:
    monkeypatch.setenv(pc.MASTER_FLAG_ENV_VAR, "false")
    assert pc.is_paste_collapse_enabled() is False


def test_bracketed_paste_binding_installs() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    kb = KeyBindings()
    assert pc.install_paste_collapse(kb)
    assert kb.get_bindings_for_keys((Keys.BracketedPaste,))


# --------------------------------------------------------------------------
# 2. emoji shortcodes
# --------------------------------------------------------------------------

def _emoji(text: str):
    from prompt_toolkit.document import Document
    comp = emo.EmojiShortcodeCompleter()
    return list(comp.get_completions(Document(text, len(text)), None))


def test_fragment_gate() -> None:
    pytest.importorskip("prompt_toolkit")
    assert _emoji("deploy the :fi") != []
    assert _emoji("no colon here") == []
    assert _emoji("ratio 3:4") == []          # mid-word colon is prose
    assert _emoji(":f") == []                 # one char — too eager


def test_accept_replaces_fragment_with_the_character() -> None:
    pytest.importorskip("prompt_toolkit")
    (first, *_rest) = _emoji(":fire")
    assert first.text == "🔥"
    assert first.start_position == -len(":fire")


def test_prefix_ranks_above_containment() -> None:
    pytest.importorskip("prompt_toolkit")
    names = [c.text for c in _emoji(":lock")]
    assert names[0] == "🔒"                   # lock before unlock


def test_operator_extras_extend_the_table(monkeypatch) -> None:
    monkeypatch.setenv(emo.EXTRA_ENV_VAR, "molt=🐍, blank=, ok=🆗")
    table = emo.shortcode_table()
    assert table["molt"] == "🐍"


def test_emoji_completer_speaks_the_async_protocol() -> None:
    pytest.importorskip("prompt_toolkit")
    assert hasattr(emo.EmojiShortcodeCompleter(), "get_completions_async")


def test_emoji_rides_the_shared_completer_chain() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import repl_completion as rc
    src = inspect.getsource(rc.build_completer)
    assert "EmojiShortcodeCompleter" in src


# --------------------------------------------------------------------------
# 3. vim mode, Ctrl+J, statusline segment
# --------------------------------------------------------------------------

def test_editor_mode_reader(monkeypatch) -> None:
    pytest.importorskip("prompt_toolkit")
    from backend.core.ouroboros.battle_test import keymap as km
    monkeypatch.delenv(km.EDITOR_MODE_ENV_VAR, raising=False)
    assert km.editing_mode() is None          # default = emacs, unchanged
    monkeypatch.setenv(km.EDITOR_MODE_ENV_VAR, "vim")
    from prompt_toolkit.enums import EditingMode
    assert km.editing_mode() is EditingMode.VI


def test_all_three_surfaces_consult_the_one_reader() -> None:
    from pathlib import Path
    import backend.core.ouroboros.cli.ov as ov
    from backend.core.ouroboros.battle_test import (
        bipartite_layout,
        serpent_flow,
    )
    for mod in (ov, bipartite_layout, serpent_flow):
        assert "editing_mode" in Path(mod.__file__).read_text(), mod.__name__


def test_ctrl_j_is_a_newline_default() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import input_continuation as ic
    src = inspect.getsource(ic.install_newline_binding)
    assert '"alt+enter", "ctrl+j"' in src


def test_custom_statusline_segment(monkeypatch) -> None:
    from backend.core.ouroboros.battle_test import status_line as sl
    monkeypatch.delenv("JARVIS_STATUSLINE_CMD", raising=False)
    assert sl._custom_segment() == ""
    monkeypatch.setenv("JARVIS_STATUSLINE_CMD", "echo braavos ready")
    sl._CUSTOM_SEGMENT_CACHE.update({"at": 0.0, "text": ""})
    assert sl._custom_segment() == "braavos ready"
    # cached — a second call must not fork again inside the TTL
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: 1 / 0)
    assert sl._custom_segment() == "braavos ready"
