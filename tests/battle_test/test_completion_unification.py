"""Completion unification — one registry, one mention completer, every
surface completing the same vocabulary.

Pins the fixes for the audit gaps that let four palette PRs ship without
the operator seeing them:

  * gap #3 — ``discover_verbs`` (34) vs ``registry_from_dispatch`` (76)
    never reconciled → :func:`unified_registry` merges them;
  * gap #4 — TWO ``@``-mention completers merged into the attach path →
    ``build_completer(include_mentions=False)``;
  * gap #5 — dispatch verbs carried ``arg_spec=""`` so ~330 lines of arg
    completion were inert on the attach surface → choice atoms mined
    from the dispatcher body + the ``__verb_args__`` convention;
  * gap #6 — no auto-suggest anywhere → ``build_auto_suggest``;
  * gap #10 — ``cursor_arg_position`` split on every space, quoted or
    not.
"""
from __future__ import annotations

import sys
import types

import pytest

from backend.core.ouroboros.battle_test import repl_completion as rc


# --------------------------------------------------------------------------
# 1. choice atoms — mined vocabularies ride the spec grammar
# --------------------------------------------------------------------------

def test_optional_choice_atom_parses_to_static() -> None:
    (spec,) = rc.parse_arg_spec("[status|history|explain]")
    assert spec.kind is rc.ArgKind.STATIC
    assert spec.required is False
    assert spec.static_values == ("status", "history", "explain")


def test_required_choice_atom_parses_to_static() -> None:
    specs = rc.parse_arg_spec("<on|off> [--immediate]")
    assert specs[0].required is True
    assert specs[0].static_values == ("on", "off")
    assert specs[1].flag_form == "--immediate"


def test_choice_candidates_prefix_filter() -> None:
    (spec,) = rc.parse_arg_spec("[panel|banners|prefetch|status]")
    assert rc.get_arg_candidates(spec, "p") == ("panel", "prefetch")


def test_legacy_atoms_unchanged() -> None:
    specs = rc.parse_arg_spec("<op_id> [--immediate]")
    assert specs[0].name == "op_id" and specs[0].required
    assert specs[1].flag_form == "--immediate"


# --------------------------------------------------------------------------
# 2. quote-aware cursor position
# --------------------------------------------------------------------------

def test_cursor_position_quoted_fragment_is_one_slot() -> None:
    assert rc.cursor_arg_position('/attach "my fi') == (0, '"my fi')


def test_cursor_position_closed_quote_then_new_slot() -> None:
    idx, prefix = rc.cursor_arg_position('/attach "my file" ')
    assert (idx, prefix) == (1, "")


def test_cursor_position_naive_contract_preserved() -> None:
    assert rc.cursor_arg_position("/cancel ") == (0, "")
    assert rc.cursor_arg_position("/cancel ab c") == (1, "c")
    assert rc.cursor_arg_position("plain text") == (-1, "")


# --------------------------------------------------------------------------
# 3. dispatch arg specs — authored beats mined, mined beats nothing
# --------------------------------------------------------------------------

def _fake_module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def test_verb_args_convention_wins(monkeypatch) -> None:
    mod = _fake_module("_fake_verbargs_mod", __verb_args__={
        "cancel": "<op_id> [--immediate]",
    })
    def dispatch_cancel_command(line):  # noqa: ANN001, ARG001
        if line == "status":
            return 1
        if line == "history":
            return 2
    dispatch_cancel_command.__module__ = mod.__name__
    try:
        spec = rc._dispatch_arg_spec("cancel", dispatch_cancel_command)
        assert spec == "<op_id> [--immediate]"
    finally:
        del sys.modules[mod.__name__]


def test_mined_subcommands_become_a_choice_atom() -> None:
    def dispatch_demo_command(line):  # noqa: ANN001
        sub = (line or "").strip()
        if sub == "status":
            return "s"
        if sub == "history":
            return "h"
        return "?"
    spec = rc._dispatch_arg_spec("demo", dispatch_demo_command)
    assert spec == "[status|history]"
    (pos,) = rc.parse_arg_spec(spec)
    assert pos.static_values == ("status", "history")


def test_no_vocabulary_means_no_spec() -> None:
    def dispatch_empty_command(line):  # noqa: ANN001, ARG001
        return None
    assert rc._dispatch_arg_spec("empty", dispatch_empty_command) == ""


def test_keys_verb_arrives_with_a_mined_spec() -> None:
    """End-to-end: the real dispatch registry hands /keys a completable
    subcommand vocabulary read from its own body."""
    reg = rc.registry_from_dispatch()
    verb = reg.find("/keys")
    assert verb is not None
    positions = rc.parse_arg_spec(verb.arg_spec)
    assert positions and "warnings" in positions[0].static_values


# --------------------------------------------------------------------------
# 4. unified registry — the gap #3 reconciliation
# --------------------------------------------------------------------------

class _FakeRepl:
    def _handle_cancel(self, op_id: str) -> None:
        """Cancel an in-flight operation cooperatively."""

    def _handle_bus(self) -> None:
        """A DISCOVERED description for a verb dispatch also has."""


def test_unified_registry_merges_both_sources() -> None:
    reg = rc.unified_registry(_FakeRepl())
    forms = {v.slash_form for v in reg.verbs}
    # from discover_verbs: the _handle_ walk + builtins
    assert "/cancel" in forms and "/help" in forms
    # from the dispatch table
    assert "/keys" in forms
    # sorted, deduplicated
    assert list(forms) and sorted(
        v.slash_form for v in reg.verbs
    ) == [v.slash_form for v in reg.verbs]


def test_unified_registry_prefers_data_over_blanks() -> None:
    reg = rc.unified_registry(_FakeRepl())
    cancel = reg.find("/cancel")
    # discover side derived <op_id> from the signature; dispatch side
    # (custom-handler exclusion) has no entry — spec must survive.
    assert "op_id" in cancel.arg_spec


def test_unified_registry_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv(rc.UNIFIED_REGISTRY_ENV_VAR, "false")
    reg = rc.unified_registry(_FakeRepl())
    forms = {v.slash_form for v in reg.verbs}
    assert "/cancel" in forms          # discovery still works
    assert "/keys" not in forms        # dispatch merge is off


def test_wiring_uses_the_unified_registry() -> None:
    wiring = rc.build_completion_wiring(_FakeRepl())
    forms = {v.slash_form for v in wiring.registry.verbs}
    assert "/cancel" in forms and "/keys" in forms


# --------------------------------------------------------------------------
# 5. one mention completer per surface (gap #4)
# --------------------------------------------------------------------------

def test_include_mentions_false_returns_bare_slash_completer() -> None:
    pytest.importorskip("prompt_toolkit")
    reg = rc.VerbRegistry(verbs=(
        rc.VerbDescriptor(slash_form="/x", handler_method="", description=""),
    ))
    bare = rc.build_completer(reg, include_mentions=False)
    # A merged completer is a _MergedCompleter; the bare one is our own
    # class. The attach path counts on this to add its repo-rooted
    # mention completer WITHOUT doubling the polish one.
    assert type(bare).__name__ == "_SlashCompleter"


def test_attach_completer_has_exactly_one_mention_source() -> None:
    pytest.importorskip("prompt_toolkit")
    completer = rc.build_attach_completer()
    assert completer is not None

    mention_like = []

    def _walk(c) -> None:
        name = type(c).__name__
        if "Mention" in name or "PathCompleter" in name:
            mention_like.append(name)
        for attr in ("completer", "completers", "_completer", "_completers"):
            inner = getattr(c, attr, None)
            if inner is None:
                continue
            if isinstance(inner, (list, tuple)):
                for i in inner:
                    _walk(i)
            else:
                _walk(inner)

    _walk(completer)
    assert mention_like == ["MentionPathCompleter"], mention_like


# --------------------------------------------------------------------------
# 6. auto-suggest + complete-while-typing wiring (gaps #6/#7)
# --------------------------------------------------------------------------

def test_auto_suggest_builds_threaded(monkeypatch) -> None:
    pytest.importorskip("prompt_toolkit")
    sugg = rc.build_auto_suggest()
    assert type(sugg).__name__ == "ThreadedAutoSuggest"
    monkeypatch.setenv(rc.AUTOSUGGEST_ENABLED_ENV_VAR, "false")
    assert rc.build_auto_suggest() is None
    monkeypatch.delenv(rc.AUTOSUGGEST_ENABLED_ENV_VAR)
    monkeypatch.setenv(rc.HISTORY_ENABLED_ENV_VAR, "false")
    assert rc.build_auto_suggest() is None  # nothing to suggest from


def test_wiring_carries_auto_suggest_and_cwt(tmp_path, monkeypatch) -> None:
    pytest.importorskip("prompt_toolkit")
    monkeypatch.setenv(rc.HISTORY_PATH_ENV_VAR, str(tmp_path / "hist"))
    wiring = rc.build_completion_wiring(None)
    assert wiring.auto_suggest is not None
    assert wiring.complete_while_typing is True
    assert type(wiring.completer).__name__ == "ThreadedCompleter"
    monkeypatch.setenv(rc.COMPLETE_WHILE_TYPING_ENV_VAR, "false")
    wiring2 = rc.build_completion_wiring(None)
    assert wiring2.complete_while_typing is False


# --------------------------------------------------------------------------
# 7. the bipartite cockpit gets history + ghost text (gaps #1/#2)
# --------------------------------------------------------------------------

def _build_app(**kwargs):
    import contextlib
    import io
    from backend.core.ouroboros.battle_test.bipartite_layout import (
        BipartiteLayout, build_bipartite_application,
    )
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        mux = BipartiteLayout(width=120, height=30, title="t")
        app = build_bipartite_application(
            mux, on_accept=lambda _t: None, **kwargs,
        )
    return app


def _prompt_buffer(app):
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import BufferControl
    for w in app.layout.find_all_windows():
        if isinstance(w, Window) and isinstance(w.content, BufferControl):
            return w.content.buffer
    return None


def test_bipartite_prompt_carries_history_and_suggest() -> None:
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.history import InMemoryHistory
    hist = InMemoryHistory()
    sugg = rc.build_auto_suggest()
    app = _build_app(history=hist, auto_suggest=sugg)
    buf = _prompt_buffer(app)
    assert buf is not None
    assert buf.history is hist
    # TextArea wraps auto_suggest in a DynamicAutoSuggest indirection —
    # resolve it and demand OUR object on the other side.
    resolver = getattr(buf.auto_suggest, "get_auto_suggest", None)
    resolved = resolver() if callable(resolver) else buf.auto_suggest
    assert resolved is sugg


def test_bipartite_prompt_defaults_stay_none_safe() -> None:
    pytest.importorskip("prompt_toolkit")
    app = _build_app()
    assert _prompt_buffer(app) is not None


# --------------------------------------------------------------------------
# 8. dynamic providers reach the DEFAULT surface + /expand completes refs
# --------------------------------------------------------------------------

def test_providers_register_before_the_fast_path() -> None:
    """The op_id/ref providers used to live in the legacy wiring block,
    which the bipartite fast-path returns without reaching — the
    default cockpit had no dynamic candidates. Pinned: registration is
    hoisted above the fast-path."""
    import inspect
    from backend.core.ouroboros.battle_test import serpent_flow
    src = inspect.getsource(serpent_flow.SerpentREPL._loop)
    assert (
        src.index("_register_completion_providers")
        < src.index("run_bipartite_repl")
    )


def test_register_completion_providers_lands_both_keys() -> None:
    import types
    from backend.core.ouroboros.battle_test import serpent_flow
    stub = types.SimpleNamespace(_gls=None)
    serpent_flow.SerpentREPL._register_completion_providers(stub)
    assert {"op_id", "ref"} <= set(rc.list_arg_providers())
    # both degrade to tuples, never raise, with no GLS / empty stores
    op = rc._ARG_PROVIDERS["op_id"]
    ref = rc._ARG_PROVIDERS["ref"]
    assert op("") == ()
    assert isinstance(ref(""), tuple)


def test_expand_declares_a_dynamic_ref_position() -> None:
    from backend.core.ouroboros.battle_test import serpent_flow
    tags = rc._parse_doc_tags(serpent_flow.SerpentREPL._handle_expand)
    assert tags["arg_spec"] == "[ref]"
    # with the provider registered (test above), [ref] classifies DYNAMIC
    (pos,) = rc.parse_arg_spec("[ref]")
    assert pos.kind is rc.ArgKind.DYNAMIC and pos.provider_key == "ref"


# --------------------------------------------------------------------------
# 8b. history hardening — singleton, perms, dedupe, bounded file
# --------------------------------------------------------------------------

@pytest.fixture()
def history_file(tmp_path, monkeypatch):
    path = tmp_path / "hist"
    monkeypatch.setenv(rc.HISTORY_PATH_ENV_VAR, str(path))
    rc.reset_history_cache_for_tests()
    yield path
    rc.reset_history_cache_for_tests()


def test_history_is_a_per_path_singleton(history_file) -> None:
    pytest.importorskip("prompt_toolkit")
    a = rc.build_history()
    b = rc.build_history()
    assert a is b  # two surfaces, ONE write-behind cache per file


def test_history_file_is_owner_only(history_file) -> None:
    pytest.importorskip("prompt_toolkit")
    import stat
    rc.build_history()
    mode = stat.S_IMODE(history_file.stat().st_mode)
    assert mode == 0o600


def test_history_refuses_blanks_and_immediate_repeats(history_file) -> None:
    pytest.importorskip("prompt_toolkit")
    hist = rc.build_history()
    hist.append_string("/status")
    hist.append_string("/status")     # immediate repeat — dropped
    hist.append_string("   ")         # blank — dropped
    hist.append_string("/cost")
    hist.append_string("/status")     # NOT consecutive — kept
    assert hist.get_strings() == ["/status", "/cost", "/status"]


def test_history_file_is_bounded_and_trim_is_entry_aware(
    history_file, monkeypatch,
) -> None:
    pytest.importorskip("prompt_toolkit")
    monkeypatch.setenv(rc.HISTORY_MAX_ENTRIES_ENV_VAR, "50")
    # Write 120 entries in FileHistory's own on-disk grammar.
    lines = []
    for i in range(120):
        lines.append(f"\n# 2026-07-27 00:00:{i:02d}\n")
        lines.append(f"+cmd-{i}\n")
    history_file.write_text("".join(lines))
    hist = rc.build_history()
    kept = list(hist.load_history_strings())   # newest-first
    assert len(kept) == 50
    assert kept[0] == "cmd-119" and kept[-1] == "cmd-70"


# --------------------------------------------------------------------------
# 9. /keys is answered by the process whose bindings it describes
# --------------------------------------------------------------------------

class _FakeClient:
    def __init__(self):
        self.sent = []
        self.audio = []

    def send_input(self, text):
        self.sent.append(text)

    def send_audio(self, cmd):
        self.audio.append(cmd)


class _FakeUI:
    def __init__(self):
        self.lines = []
        self.flashes = []

    def markup_sink(self, line, addressed=False):
        self.lines.append(line)

    def flash(self, msg, seconds=None):
        self.flashes.append(msg)

    def should_flush_on_input(self):
        return False


def test_keys_verb_is_intercepted_client_side() -> None:
    from backend.core.ouroboros.cli.ov import _route_operator_line
    client, ui = _FakeClient(), _FakeUI()
    outcome = _route_operator_line(client, ui, "/keys")
    assert outcome == "handled"
    assert client.sent == []                    # never crossed the bridge
    assert any("keymap" in ln for ln in ui.lines)


def test_keys_daemon_subcommand_forwards() -> None:
    from backend.core.ouroboros.cli.ov import _route_operator_line
    client, ui = _FakeClient(), _FakeUI()
    outcome = _route_operator_line(client, ui, "/keys daemon warnings")
    assert outcome == "sent"
    assert client.sent == ["/keys warnings"]
    assert ui.lines == []


def test_daemon_cockpit_fast_path_passes_the_wiring() -> None:
    """serpent_flow's bipartite fast-path must hand the SAME wiring the
    legacy PromptSession gets to run_bipartite_repl — pinned at source
    level because the fast-path cannot run headless."""
    import inspect
    from backend.core.ouroboros.battle_test import serpent_flow
    src = inspect.getsource(serpent_flow.SerpentREPL._loop)
    assert "build_completion_wiring" in src
    # The call site's kwargs contain nested parens (getattr defaults) —
    # bound the window by the `return` that follows the await instead.
    fast = src.split("await run_bipartite_repl(")[1].split("return")[0]
    for kw in ("completer=", "history=", "auto_suggest="):
        assert kw in fast, f"bipartite fast-path lost {kw}"
