"""The remappable keymap — parser, config, resolution, hot reload, mounts.

One engine, every surface: bipartite cockpit exit keys, ov selection
bindings, and input_continuation's newline hatch all resolve their keys
here. These tests pin the four contracts that make that safe:

  1. the keystroke parser speaks Claude Code's syntax and refuses what a
     terminal cannot deliver;
  2. a broken config degrades to warnings + defaults, never to a dead
     cockpit;
  3. resolution honors unbind (null), rebinds, and Global blocks;
  4. mounts recompile on config change (hot reload) and bind_action
     falls back to declared defaults on any fault.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import keymap as km


@pytest.fixture()
def config_file(tmp_path, monkeypatch):
    """An isolated config file + a store that reloads instantly."""
    path = tmp_path / "keybindings.json"
    monkeypatch.setenv(km.CONFIG_PATH_ENV_VAR, str(path))
    monkeypatch.setenv(km.RELOAD_THROTTLE_ENV_VAR, "0.1")
    km.get_store().maybe_reload(force=True)
    yield path
    monkeypatch.delenv(km.CONFIG_PATH_ENV_VAR, raising=False)
    km.get_store().maybe_reload(force=True)


def _write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")
    km.get_store().maybe_reload(force=True)


# --------------------------------------------------------------------------
# 1. keystroke parser
# --------------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("ctrl+c", ("c-c",)),
    ("control+E", ("c-e",)),            # aliases + stylistic caps
    ("ctrl+K", ("c-k",)),               # modifier caps do NOT imply shift
    ("K", ("K",)),                      # standalone uppercase implies shift
    ("shift+k", ("K",)),
    ("shift+tab", ("s-tab",)),
    ("alt+enter", ("escape", "enter")),  # alt = escape prefix
    ("opt+x", ("escape", "x")),
    ("meta+p", ("escape", "p")),
    ("ctrl+x ctrl+e", ("c-x", "c-e")),   # chord
    ("escape", ("escape",)),
    ("esc", ("escape",)),
    ("return", ("enter",)),
    ("space", (" ",)),
    ("alt+space", ("escape", " ")),
    ("ctrl+up", ("c-up",)),
    ("ctrl+shift+left", ("c-s-left",)),
    ("pageup", ("pageup",)),
    ("f12", ("f12",)),
    ("backspace", ("backspace",)),
])
def test_parser_translates_cc_syntax(spec, expected) -> None:
    assert km.parse_keystroke(spec) == expected


@pytest.mark.parametrize("bad", [
    "", "   ",
    "cmd+p",            # super is not delivered by most terminals
    "wat+x",            # unknown modifier
    "ctrl+",            # no base key
    "ctrl+enter",       # enter does not take ctrl in a terminal
    "shift+escape",     # escape does not take shift
    "ctrl+notakey",
])
def test_parser_rejects_undeliverable(bad) -> None:
    with pytest.raises(km.KeystrokeError):
        km.parse_keystroke(bad)


def test_canonical_form_unifies_spellings() -> None:
    assert km.canonical_keystroke("control+E") == km.canonical_keystroke(
        "ctrl+e"
    )
    # The literal space key round-trips losslessly through the canon.
    assert km.canonical_keystroke("alt+space") == "escape space"


# --------------------------------------------------------------------------
# 2. config parse + validation — warnings, never a dead cockpit
# --------------------------------------------------------------------------

def test_invalid_json_is_one_warning_and_no_bindings() -> None:
    cfg = km.parse_config("{nope")
    assert cfg.blocks == {}
    assert len(cfg.warnings) == 1
    assert "JSON" in cfg.warnings[0]


def test_structural_problems_cost_one_entry_each() -> None:
    cfg = km.parse_config(json.dumps({"bindings": [
        {"context": "Chat", "bindings": {
            "ctrl+j": "chat:newline",       # good
            "ctrl+m": "chat:submit",        # reserved → skipped
            "ctrl+b": "deck:open",          # tmux conflict → kept + warned
            "garbage+q": "x:y",             # unparseable → skipped
            "ctrl+q": "notanaction",        # bad action id → skipped
        }},
        {"context": "Nowhere", "bindings": {"f5": "a:b"}},  # unknown ctx
        "not-a-block",
    ]}))
    chat = cfg.blocks["Chat"]
    assert chat["c-j"] == "chat:newline"
    assert "c-m" not in chat                      # reserved never lands
    assert chat["c-b"] == "deck:open"             # conflict is warn-only
    assert any("reserved" in w for w in cfg.warnings)
    assert any("tmux" in w for w in cfg.warnings)
    assert any("unknown modifier" in w for w in cfg.warnings)
    assert any("namespace:action" in w for w in cfg.warnings)
    assert any("unknown context" in w for w in cfg.warnings)
    assert any("not an object" in w for w in cfg.warnings)


def test_null_unbinds_and_duplicates_warn() -> None:
    cfg = km.parse_config(json.dumps({"bindings": [
        {"context": "Chat", "bindings": {"alt+enter": None}},
        {"context": "Chat", "bindings": {"alt+enter": "chat:newline"}},
    ]}))
    # last one wins, with a warning
    assert cfg.blocks["Chat"]["escape enter"] == "chat:newline"
    assert any("duplicate" in w for w in cfg.warnings)


# --------------------------------------------------------------------------
# 3. resolution — defaults × config
# --------------------------------------------------------------------------

def test_defaults_survive_an_absent_config(config_file) -> None:
    assert km.effective_key_sequences(
        "chat:newline", ("alt+enter",),
    ) == (("escape", "enter"),)


def test_null_unbind_removes_a_default(config_file) -> None:
    _write(config_file, {"bindings": [
        {"context": "Chat", "bindings": {"alt+enter": None}},
    ]})
    assert km.effective_key_sequences("chat:newline", ("alt+enter",)) == ()


def test_rebind_moves_the_key(config_file) -> None:
    _write(config_file, {"bindings": [
        {"context": "Chat", "bindings": {
            "alt+enter": None, "ctrl+j": "chat:newline",
        }},
    ]})
    assert km.effective_key_sequences(
        "chat:newline", ("alt+enter",),
    ) == (("c-j",),)


def test_reassigning_a_default_key_steals_it(config_file) -> None:
    # ctrl+p moves from gate:review to another action — gate:review
    # loses the key WITHOUT an explicit null.
    _write(config_file, {"bindings": [
        {"context": "Chat", "bindings": {"ctrl+p": "chat:newline"}},
    ]})
    assert km.effective_key_sequences("gate:review", ("ctrl+p",)) == ()
    assert (("c-p",) in km.effective_key_sequences(
        "chat:newline", ("alt+enter",),
    ))


def test_global_block_applies_to_every_context(config_file) -> None:
    _write(config_file, {"bindings": [
        {"context": "Global", "bindings": {"ctrl+o": None}},
    ]})
    assert km.effective_key_sequences(
        "deck:open", ("ctrl+o",), context="Deck",
    ) == ()


def test_master_flag_off_means_defaults_only(config_file, monkeypatch):
    _write(config_file, {"bindings": [
        {"context": "Chat", "bindings": {"alt+enter": None}},
    ]})
    monkeypatch.setenv(km.MASTER_FLAG_ENV_VAR, "false")
    km.get_store().maybe_reload(force=True)
    assert km.effective_key_sequences(
        "chat:newline", ("alt+enter",),
    ) == (("escape", "enter"),)
    monkeypatch.delenv(km.MASTER_FLAG_ENV_VAR)
    km.get_store().maybe_reload(force=True)


def test_a_bad_declared_default_drops_that_key_only() -> None:
    seqs = km.effective_key_sequences(
        "x:y", ("definitely+not+a+key", "ctrl+t"),
    )
    assert seqs == (("c-t",),)


# --------------------------------------------------------------------------
# 4. hot reload + mounts
# --------------------------------------------------------------------------

def test_store_generation_bumps_on_change(config_file) -> None:
    store = km.get_store()
    g0 = store.generation
    _write(config_file, {"bindings": []})
    assert store.generation > g0


def test_mount_compiles_and_recompiles_on_config_change(config_file) -> None:
    pytest.importorskip("prompt_toolkit")
    mount = km.KeymapMount("test-surface")
    fired = []
    mount.action("app:detach", ("ctrl+c",), context="Global")(
        lambda e: fired.append(1)
    )
    kb = mount.key_bindings()
    assert kb.get_bindings_for_keys(("c-c",))
    # Move the key; the SAME DynamicKeyBindings object must follow.
    _write(config_file, {"bindings": [
        {"context": "Global", "bindings": {
            "ctrl+c": None, "ctrl+g": "app:detach",
        }},
    ]})
    assert not kb.get_bindings_for_keys(("c-c",))
    assert kb.get_bindings_for_keys(("c-g",))


def test_bind_action_binds_defaults_into_a_plain_kb(config_file) -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()
    assert km.bind_action(
        kb, "deck:open", ("ctrl+o",), lambda e: None, context="Deck",
    )
    assert kb.get_bindings_for_keys(("c-o",))


def test_bind_action_survives_a_none_kb() -> None:
    assert km.bind_action(None, "a:b", ("ctrl+t",), lambda e: None) is False


# --------------------------------------------------------------------------
# 5. introspection + template
# --------------------------------------------------------------------------

def test_describe_flags_unknown_config_actions(config_file) -> None:
    km.register_action_spec("chat:newline", "Chat", ("alt+enter",), "nl")
    _write(config_file, {"bindings": [
        {"context": "Chat", "bindings": {"ctrl+y": "no:body"}},
    ]})
    info = km.describe_keymap()
    assert any("unknown action" in w for w in info["warnings"])
    # The catalog is process-global and first-registration-wins; other
    # suites may have registered chat:newline with its full default set
    # (alt+enter + ctrl+j). Pin membership, not an exact tuple.
    row = next(r for r in info["actions"] if r["action"] == "chat:newline")
    assert "escape enter" in row["keys"]
    assert row["customized"] is False


def test_template_write_and_keys_verb(config_file) -> None:
    from backend.core.ouroboros.governance.keys_repl import (
        dispatch_keys_command,
    )
    r = dispatch_keys_command("/keys init")
    assert r.ok and str(config_file) in r.text
    assert config_file.is_file()
    # init is idempotent
    assert dispatch_keys_command("/keys init").ok
    assert dispatch_keys_command("/keys").ok
    assert dispatch_keys_command("/keys path").ok
    assert dispatch_keys_command("/keys reload").ok
    assert dispatch_keys_command("/keys warnings").ok
    assert "Subcommands" in dispatch_keys_command("/keys help").text


def test_keys_verb_is_discoverable_by_the_dispatch_registry() -> None:
    """The naming-cage contract: keys_repl.py + dispatch_keys_command →
    the /keys verb registers with no other edit."""
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
        _VERB_TO_DISPATCHER, prime_registry,
    )
    prime_registry()
    assert "keys" in _VERB_TO_DISPATCHER


# ---------------------------------------------------------------------------
# CC's action names resolve to ov's, and ov's keep working
# ---------------------------------------------------------------------------


class TestActionAliases:
    """ALIASES, not renames. `agents:stopAll` and `chat:killAgents` are the
    same capability on the same chord.

    An operator arriving from CC writes CC's name in `keybindings.json` and
    expects it to bind; anyone who already wrote ov's name must not have
    their config silently stop applying. Renaming fixes the first at the cost
    of the second — and a config key mapped to an unknown action fails
    SILENTLY: the binding simply never appears.
    """

    def test_an_alias_resolves_both_ways(self):
        from backend.core.ouroboros.battle_test.keymap import aliases_for

        assert set(aliases_for("agents:stopAll")) == {
            "agents:stopAll", "chat:killAgents"}
        assert set(aliases_for("chat:killAgents")) == {
            "agents:stopAll", "chat:killAgents"}

    def test_an_unaliased_action_is_just_itself(self):
        from backend.core.ouroboros.battle_test.keymap import aliases_for

        assert aliases_for("app:detach") == ("app:detach",)
        assert aliases_for("nope:thing") == ("nope:thing",)

    def test_ccs_scroll_family_maps_to_the_viewer_table(self):
        """CC's `scroll:*` is ov's `transcript:*` — same movements, same
        defaults, different namespace only because ov's arrived with the
        viewer rather than with a scroll region."""
        from backend.core.ouroboros.battle_test.keymap import aliases_for

        for cc, ov in (("scroll:lineUp", "transcript:lineUp"),
                       ("scroll:bottom", "transcript:bottom"),
                       ("scroll:fullPageUp", "transcript:pageUp")):
            assert ov in aliases_for(cc)

    def test_a_config_written_with_ccs_name_binds_ovs_action(self, tmp_path,
                                                             monkeypatch):
        """THE point of the table. Without it the key maps to an action
        nothing declares, and nothing tells the operator."""
        import json

        from backend.core.ouroboros.battle_test import keymap as K

        cfg = tmp_path / "keybindings.json"
        cfg.write_text(json.dumps({"bindings": [
            {"context": "Global", "bindings": {"ctrl+q": "chat:killAgents"}},
        ]}))
        monkeypatch.setenv(K.CONFIG_PATH_ENV_VAR, str(cfg))
        K.get_store().maybe_reload()
        seqs = K.effective_key_sequences(
            "agents:stopAll", ("ctrl+x ctrl+k",), context="Global")
        flat = {" ".join(s) for s in seqs}
        assert any("c-q" in f for f in flat), flat

    def test_ovs_own_name_still_binds(self, tmp_path, monkeypatch):
        import json

        from backend.core.ouroboros.battle_test import keymap as K

        cfg = tmp_path / "keybindings.json"
        cfg.write_text(json.dumps({"bindings": [
            {"context": "Global", "bindings": {"ctrl+q": "agents:stopAll"}},
        ]}))
        monkeypatch.setenv(K.CONFIG_PATH_ENV_VAR, str(cfg))
        K.get_store().maybe_reload()
        seqs = K.effective_key_sequences(
            "agents:stopAll", ("ctrl+x ctrl+k",), context="Global")
        assert any("c-q" in " ".join(s) for s in seqs)

    def test_every_alias_points_at_a_real_action(self):
        """A table entry pointing at an id nothing declares is worse than no
        entry: it silently accepts a config that can never bind."""
        from backend.core.ouroboros.battle_test.keymap import ACTION_ALIASES

        assert ACTION_ALIASES
        for cc, ov in ACTION_ALIASES.items():
            assert ":" in cc and ":" in ov and cc != ov


class TestUndo:
    def test_chat_undo_is_bound(self):
        """CC binds `chat:undo` to Ctrl+_ / Ctrl+Shift+-. prompt_toolkit
        already ships the command; it was simply never bound, so the prompt
        accepted paragraphs with no way back from a mis-typed Ctrl+W."""
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            BipartiteLayout, build_bipartite_application,
        )
        mux = BipartiteLayout(width=80, height=14)
        app = build_bipartite_application(mux, on_accept=lambda _t: None)
        seqs = {" ".join(str(k).replace("Keys.", "") for k in b.keys)
                for b in app.key_bindings.bindings}
        assert any("Underscore" in s for s in seqs), sorted(seqs)

    def test_it_delegates_to_the_buffers_own_undo_stack(self):
        """A second history of the same text would disagree with the buffer
        the first time a completion inserted anything."""
        import inspect

        from backend.core.ouroboros.battle_test import bipartite_layout

        src = inspect.getsource(bipartite_layout.build_bipartite_application)
        assert 'get_by_name("undo")' in src
