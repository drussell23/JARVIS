"""Tests for repl_completion (Gap #7 Slice 3)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.repl_completion import (
    HISTORY_ENABLED_ENV_VAR,
    HISTORY_PATH_ENV_VAR,
    MASTER_FLAG_ENV_VAR,
    REPL_COMPLETION_SCHEMA_VERSION,
    CompletionWiring,
    VerbDescriptor,
    VerbRegistry,
    build_completer,
    build_completion_wiring,
    build_history,
    discover_verbs,
    is_completion_enabled,
    is_history_enabled,
    resolve_history_path,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for var in (
        MASTER_FLAG_ENV_VAR,
        HISTORY_PATH_ENV_VAR,
        HISTORY_ENABLED_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    yield


# ===========================================================================
# Schema + master flag
# ===========================================================================


def test_schema_version_pinned():
    assert REPL_COMPLETION_SCHEMA_VERSION == "repl_completion.v1"


def test_master_flag_default_on_post_graduation():
    """Slice 5 flipped this default-true (2026-05-04)."""
    assert is_completion_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    ("", True),                  # unset → default ON
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("garbage", True),           # not in off-token set → ON
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_master_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert is_completion_enabled() is expected


def test_history_enabled_default_true():
    """Persistent history is conventional and minimally invasive —
    default ON. Operators set =false for confidentiality."""
    assert is_history_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    ("", True),  # default-on
    ("true", True), ("1", True),
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_history_enabled_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(HISTORY_ENABLED_ENV_VAR, raw)
    assert is_history_enabled() is expected


# ===========================================================================
# resolve_history_path
# ===========================================================================


def test_resolve_history_path_default(tmp_path):
    """Default path is .jarvis/repl_history relative to cwd."""
    path = resolve_history_path()
    assert path is not None
    assert path.name == "repl_history"
    assert path.parent.name == ".jarvis"


def test_resolve_history_path_explicit_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom" / "history.txt"
    monkeypatch.setenv(HISTORY_PATH_ENV_VAR, str(custom))
    path = resolve_history_path()
    assert path == custom
    # Parent created
    assert path.parent.exists()


def test_resolve_history_path_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv(HISTORY_ENABLED_ENV_VAR, "false")
    assert resolve_history_path() is None


def test_resolve_history_path_handles_unwritable_parent(monkeypatch):
    """Bad path → returns None gracefully, never raises.

    ``/dev/null/...`` cannot be a parent directory — mkdir fails with
    NotADirectoryError. The helper must catch and return None.
    """
    monkeypatch.setenv(HISTORY_PATH_ENV_VAR, "/dev/null/cannot/be/parent")
    path = resolve_history_path()
    assert path is None


# ===========================================================================
# discover_verbs — auto-discovery from _handle_* methods
# ===========================================================================


class _FakeRepl:
    """Minimal fake to test discovery without booting the real REPL."""

    async def _handle_accept(self, line):
        """``/accept <op-id>`` — accept a pending Gap #4 review."""
        pass

    async def _handle_reject(self, line):
        """``/reject <op-id>`` — reject a pending Gap #4 review."""
        pass

    def _handle_review(self, line):
        """``/review`` — list pending reviews."""
        pass

    def _handle_mutation_gate(self, line):
        """``/mutation-gate`` — toggle mutation gate."""
        pass

    def _handle_expand(self, line):
        """``/expand <ref>`` — dispatches by ref prefix."""
        pass

    # Should NOT be discovered (doesn't match _handle_* prefix)
    def _other_method(self):
        pass

    def public_method(self):
        pass


def test_discover_verbs_walks_handle_methods():
    registry = discover_verbs(_FakeRepl())
    slash_forms = registry.slash_forms()
    assert "/accept" in slash_forms
    assert "/reject" in slash_forms
    assert "/review" in slash_forms
    assert "/expand" in slash_forms


def test_discover_verbs_underscore_to_hyphen():
    """``_handle_mutation_gate`` → ``/mutation-gate`` (not
    ``/mutation_gate``) per existing dispatch convention."""
    registry = discover_verbs(_FakeRepl())
    assert "/mutation-gate" in registry.slash_forms()
    assert "/mutation_gate" not in registry.slash_forms()


def test_discover_verbs_includes_builtins():
    """``/help``, ``/status``, ``/cost``, ``/posture``, ``/quit``
    are added as built-ins even though they have no _handle_* method."""
    registry = discover_verbs(_FakeRepl())
    forms = registry.slash_forms()
    for required in ("/help", "/status", "/cost", "/posture", "/quit"):
        assert required in forms


def test_discover_verbs_extracts_first_doc_line():
    """Description should be the first line of the method's docstring."""
    registry = discover_verbs(_FakeRepl())
    accept = registry.find("/accept")
    assert accept is not None
    assert "accept a pending" in accept.description.lower()


def test_discover_verbs_skips_non_handle_methods():
    """Methods without `_handle_` prefix must not become verbs."""
    registry = discover_verbs(_FakeRepl())
    forms = registry.slash_forms()
    assert "/other-method" not in forms
    assert "/public-method" not in forms


def test_discover_verbs_returns_sorted_alphabetically():
    """Stable ordering across runs — the dropdown must be navigable."""
    registry = discover_verbs(_FakeRepl())
    forms = list(registry.slash_forms())
    assert forms == sorted(forms)


def test_discover_verbs_handles_none_input():
    """Bad input → registry with just built-ins, never raises."""
    registry = discover_verbs(None)
    assert isinstance(registry, VerbRegistry)
    # Built-ins still present
    assert "/help" in registry.slash_forms()


def test_discover_verbs_handles_garbage_input():
    """Non-class input → registry with just built-ins."""
    registry = discover_verbs(42)  # type: ignore[arg-type]
    assert "/help" in registry.slash_forms()


def test_verb_registry_find_returns_none_for_unknown():
    registry = discover_verbs(_FakeRepl())
    assert registry.find("/never-existed") is None
    assert registry.find(None) is None  # type: ignore[arg-type]


def test_verb_descriptor_to_dict_shape():
    v = VerbDescriptor(
        slash_form="/test", handler_method="_handle_test",
        description="test verb",
    )
    d = v.to_dict()
    assert d["slash_form"] == "/test"
    assert d["handler_method"] == "_handle_test"
    assert d["description"] == "test verb"
    assert d["schema_version"] == REPL_COMPLETION_SCHEMA_VERSION


def test_verb_registry_frozen():
    registry = discover_verbs(_FakeRepl())
    with pytest.raises(Exception):
        registry.verbs = ()  # type: ignore[misc]


# ===========================================================================
# build_completer — slash-prefix gate
# ===========================================================================


def test_build_completer_returns_completer_object():
    registry = discover_verbs(_FakeRepl())
    completer = build_completer(registry)
    # When prompt_toolkit available, returns a Completer subclass instance
    assert completer is not None
    # Should have get_completions method
    assert callable(getattr(completer, "get_completions", None))


def test_completer_only_fires_on_slash_prefix():
    """Operator typing prose → no completions interleaved."""
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    registry = discover_verbs(_FakeRepl())
    completer = build_completer(registry)
    # Non-slash input
    doc = Document("hello world")
    completions = list(completer.get_completions(doc, CompleteEvent()))
    assert completions == []


def test_completer_returns_matches_for_slash_prefix():
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    registry = discover_verbs(_FakeRepl())
    completer = build_completer(registry)
    doc = Document("/")  # all verbs match
    completions = list(completer.get_completions(doc, CompleteEvent()))
    assert len(completions) > 0
    # Each completion has display + display_meta
    for c in completions:
        assert c.text.startswith("/")


def test_completer_filters_by_prefix():
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    registry = discover_verbs(_FakeRepl())
    completer = build_completer(registry)
    doc = Document("/acc")
    completions = list(completer.get_completions(doc, CompleteEvent()))
    # Only /accept matches
    texts = {c.text for c in completions}
    assert "/accept" in texts
    assert "/reject" not in texts


def test_completer_empty_registry():
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent

    empty = VerbRegistry(verbs=())
    completer = build_completer(empty)
    doc = Document("/a")
    completions = list(completer.get_completions(doc, CompleteEvent()))
    assert completions == []


# ===========================================================================
# build_history — FileHistory + fallback
# ===========================================================================


def test_build_history_file_history_when_path_writable(tmp_path):
    history_path = tmp_path / "history.txt"
    history = build_history(history_path)
    assert history is not None
    # FileHistory's filename attr (or equivalent) should reference our path
    fname = getattr(history, "filename", None)
    if fname is not None:
        assert str(history_path) in str(fname)


def test_build_history_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv(HISTORY_ENABLED_ENV_VAR, "false")
    assert build_history() is None


def test_build_history_uses_default_path_when_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    history = build_history(None)
    assert history is not None
    # Default path created
    assert (tmp_path / ".jarvis").exists()


# ===========================================================================
# build_completion_wiring — orchestrator
# ===========================================================================


def test_wiring_disabled_returns_none_completer(monkeypatch):
    """Explicit master-flag-off → completer is None, no history search."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "false")
    wiring = build_completion_wiring(_FakeRepl())
    assert isinstance(wiring, CompletionWiring)
    assert wiring.completer is None
    assert wiring.enable_history_search is False


def test_wiring_enabled_provides_completer_and_history(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.chdir(tmp_path)
    wiring = build_completion_wiring(_FakeRepl())
    assert wiring.completer is not None
    assert wiring.history is not None
    assert wiring.enable_history_search is True
    # Registry always populated regardless of flag state
    assert len(wiring.registry) > 0


def test_wiring_registry_always_populated():
    """Even with flag off, the registry is computed — surface for
    /help rendering in Slice 5."""
    wiring = build_completion_wiring(_FakeRepl())
    forms = wiring.registry.slash_forms()
    assert "/accept" in forms
    assert "/help" in forms


def test_wiring_handles_invalid_repl_gracefully():
    wiring = build_completion_wiring(None)
    assert isinstance(wiring, CompletionWiring)
    # Built-ins still discovered
    assert "/help" in wiring.registry.slash_forms()


# ===========================================================================
# Source-level regression — serpent_flow wires the completion
# ===========================================================================


_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def test_serpent_flow_imports_completion_wiring():
    src = _SERPENT_FLOW.read_text()
    assert "build_completion_wiring" in src


def test_serpent_flow_passes_completer_to_prompt_session():
    src = _SERPENT_FLOW.read_text()
    # The completion kwargs must be threaded into PromptSession
    assert "_completion_kwargs" in src
    assert "completer" in src
    assert "history" in src


def test_serpent_flow_no_legacy_history_search_disabled_clash():
    """The PromptSession call must NOT pass enable_history_search=False
    explicitly when the wiring may also pass it — would cause TypeError."""
    src = _SERPENT_FLOW.read_text()
    # The legacy explicit `enable_history_search=False` (immediately
    # before our wiring kwargs) must be removed; the value comes from
    # the wiring dict instead.
    # Find PromptSession( ... ) calls
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "PromptSession":
                # Count enable_history_search keyword args — must be 0
                # (the wiring kwargs dict supplies it instead)
                n_explicit = sum(
                    1 for kw in node.keywords
                    if kw.arg == "enable_history_search"
                )
                assert n_explicit == 0, (
                    "PromptSession(...) should NOT pass "
                    "enable_history_search=... directly; "
                    "let _completion_kwargs handle it"
                )
