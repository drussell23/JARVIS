"""Ctrl+R history search — a completion source, not a second overlay.

Pins the phase-2 contracts:

  * the completer is INERT until armed — ordinary typing never sees a
    history candidate;
  * armed, it serves history newest-first, deduped, substring-filtered,
    prefix-ranked, replacing the whole typed prefix on accept;
  * the controller disarms itself when the menu closes (buffer's own
    on_completions_changed event);
  * the keystroke routes through the remappable keymap
    (``history:search``, default Ctrl+R);
  * both attach surfaces and the daemon cockpit wire it from the SAME
    module — pinned at the bipartite construction seam.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import history_search as hs


class _FakeHistory:
    def __init__(self, entries):
        self._entries = list(entries)

    def get_strings(self):
        return list(self._entries)

    def load_history_strings(self):
        return reversed(self._entries)


def _completions(completer, text: str):
    from prompt_toolkit.document import Document
    return list(completer.get_completions(Document(text, len(text)), None))


# --------------------------------------------------------------------------
# 1. gating
# --------------------------------------------------------------------------

def test_disarmed_completer_yields_nothing() -> None:
    pytest.importorskip("prompt_toolkit")
    controller = hs.HistorySearchController()
    completer = hs.HistoryCompleter(controller, _FakeHistory(["/status"]))
    assert _completions(completer, "") == []


def test_master_flag_off_builds_nothing(monkeypatch) -> None:
    monkeypatch.setenv(hs.MASTER_FLAG_ENV_VAR, "false")
    assert hs.build_history_search(_FakeHistory(["x"])) == (None, None)


def test_no_history_builds_nothing() -> None:
    assert hs.build_history_search(None) == (None, None)


# --------------------------------------------------------------------------
# 2. armed behavior
# --------------------------------------------------------------------------

def _armed(entries):
    controller = hs.HistorySearchController()
    controller.arm()
    return hs.HistoryCompleter(controller, _FakeHistory(entries))


def test_newest_first_and_deduped() -> None:
    pytest.importorskip("prompt_toolkit")
    comp = _armed(["/cost", "/status", "/cost", "/posture"])
    texts = [c.text for c in _completions(comp, "")]
    assert texts == ["/posture", "/cost", "/status"]


def test_substring_filter_and_prefix_rank() -> None:
    pytest.importorskip("prompt_toolkit")
    comp = _armed(["run the soak", "/status", "status check please"])
    got = [c.text for c in _completions(comp, "status")]
    # prefix hit ranks above containment, both beat the non-match
    assert got == ["status check please", "/status"]


def test_accept_replaces_the_whole_typed_prefix() -> None:
    pytest.importorskip("prompt_toolkit")
    comp = _armed(["/breadcrumbs level 2"])
    (c,) = _completions(comp, "bread")
    assert c.start_position == -len("bread")


def test_multiline_entry_displays_first_line_accepts_all() -> None:
    pytest.importorskip("prompt_toolkit")
    entry = "line one\nline two"
    comp = _armed([entry])
    (c,) = _completions(comp, "")
    assert c.text == entry
    display = "".join(frag[1] for frag in c.display)
    assert display.startswith("line one") and "…" in display


def test_menu_close_disarms_via_buffer_event() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.buffer import Buffer
    controller = hs.HistorySearchController()
    buf = Buffer()
    controller.watch(buf)
    controller.arm()
    assert controller.active
    # the buffer's own event, fired the way prompt_toolkit fires it
    buf.on_completions_changed.fire()
    assert not controller.active  # complete_state is None → disarmed


# --------------------------------------------------------------------------
# 3. keymap + composition + surface wiring
# --------------------------------------------------------------------------

def test_install_binds_ctrl_r_through_the_keymap() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()
    controller = hs.HistorySearchController()
    assert hs.install_history_search(kb, controller)
    assert kb.get_bindings_for_keys(("c-r",))


def test_merge_rules() -> None:
    pytest.importorskip("prompt_toolkit")
    controller = hs.HistorySearchController()
    hc = hs.HistoryCompleter(controller, _FakeHistory([]))
    assert hs.merge_history_completer(None, None) is None
    assert hs.merge_history_completer(None, hc) is hc
    sentinel = object()
    assert hs.merge_history_completer(sentinel, None) is sentinel
    merged = hs.merge_history_completer(sentinel, hc)
    assert type(merged).__name__ == "_MergedCompleter"


def test_bipartite_cockpit_wires_history_search() -> None:
    """Pinned at source: the cockpit build merges the history completer
    BEFORE the TextArea exists and installs the action after the kb
    exists — the same one-seam discipline as the palette."""
    import inspect
    from backend.core.ouroboros.battle_test import bipartite_layout
    src = inspect.getsource(bipartite_layout.build_bipartite_application)
    assert src.index("build_history_search") < src.index("TextArea(")
    assert "install_history_search" in src


def test_fallback_surface_wires_history_search() -> None:
    from pathlib import Path
    import backend.core.ouroboros.cli.ov as ov
    src = Path(ov.__file__).read_text()
    assert "install_history_search" in src
    assert "merge_history_completer" in src
