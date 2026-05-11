"""§41.3 Slice 2/3 SerpentREPL wiring regression spine.

Asserts the 4 surgical insertion points into `serpent_flow.py`:

* Boot welcome banner — `SerpentFlow.boot_banner()` invokes
  `welcome_state.evaluate()` + `mark_seen()` after the minimal
  welcome renders.
* `/tutorial` verb — `SerpentREPL._handle_tutorial` exists and
  composes the registry + tutorial renderer.
* `--help` interception — dispatch source contains the universal
  --help intercept that routes to `format_verb_help`.
* Typo suggestion — dispatch tail invokes `suggest_for_typo` on
  unknown slash verbs and surfaces "did you mean".

These are AST-grep pins on the wiring source — the substrate-
level behavior is covered by the welcome_state + verb_registry
extension tests."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import (
    serpent_flow,
    repl_completion as rc,
    welcome_state as ws,
)


_SOURCE_PATH = Path(
    "backend/core/ouroboros/battle_test/serpent_flow.py"
)


def _source() -> str:
    return _SOURCE_PATH.read_text()


# --- _handle_tutorial method --------------------------------------------


def test_handle_tutorial_method_exists():
    assert hasattr(serpent_flow.SerpentREPL, "_handle_tutorial")


def test_handle_tutorial_is_callable():
    method = serpent_flow.SerpentREPL._handle_tutorial
    assert callable(method)


def test_handle_tutorial_signature_accepts_line():
    """Must accept the dispatch path's `line` argument."""
    sig = inspect.signature(serpent_flow.SerpentREPL._handle_tutorial)
    params = list(sig.parameters.keys())
    # `self` + `line` (optional)
    assert "self" in params
    assert "line" in params


def test_handle_tutorial_docstring_has_category_tag():
    """Bytes-pin: handler ships @category for its own descriptor."""
    doc = inspect.getdoc(serpent_flow.SerpentREPL._handle_tutorial) or ""
    assert "@category: introspection" in doc


def test_handle_tutorial_docstring_has_example():
    doc = inspect.getdoc(serpent_flow.SerpentREPL._handle_tutorial) or ""
    assert "@example: /tutorial" in doc


def test_tutorial_auto_discovers_into_registry():
    """The _handle_tutorial method must be picked up by
    discover_verbs since it follows the _handle_* convention.
    Substrate-level: build a mock REPL and verify."""
    class _MockREPL:
        def _handle_tutorial(self, line=""):
            """Tour.

            @category: introspection
            """
    reg = rc.discover_verbs(_MockREPL())
    found = reg.find("/tutorial")
    assert found is not None
    assert found.category is rc.VerbCategory.INTROSPECTION


# --- Boot welcome banner wiring -----------------------------------------


def test_boot_banner_wires_welcome_state():
    """Bytes-pin: boot_banner source contains the welcome_state
    composition seam (evaluate + render + mark_seen)."""
    src = _source()
    # The wiring lives near the render_minimal_welcome path.
    assert "welcome_state" in src
    assert "render_first_launch_banner" in src
    assert "mark_seen()" in src


def test_boot_banner_uses_should_show_predicate():
    """Bytes-pin: the wiring gates on should_show_expanded_banner
    (not raw phase comparison) so the WelcomeState decision is
    consulted, not duplicated."""
    src = _source()
    assert "should_show_expanded_banner" in src


def test_boot_banner_failure_is_defensive():
    """Bytes-pin: the welcome wiring is in a try/except so a
    welcome_state failure NEVER breaks boot."""
    src = _source()
    # Find the wiring block and assert it's wrapped in try/except.
    idx = src.find("render_first_launch_banner")
    # Search backward for try and forward for except
    pre = src[max(0, idx - 800):idx]
    post = src[idx:idx + 1200]
    assert "try:" in pre
    assert "except" in post


# --- --help interception wiring -----------------------------------------


def test_help_interceptor_present():
    src = _source()
    # Wiring should check for both `--help` and `-h` suffixes
    assert "endswith(\" --help\")" in src
    assert "endswith(\" -h\")" in src


def test_help_interceptor_calls_format_verb_help():
    """Bytes-pin: the intercept routes to format_verb_help."""
    src = _source()
    assert "format_verb_help" in src


def test_help_interceptor_before_try_dispatch():
    """Bytes-pin: --help intercept must fire BEFORE
    repl_dispatch_registry.try_dispatch — otherwise the registry
    could dispatch the verb (and run its side effects) before the
    help shortcut applies."""
    src = _source()
    help_idx = src.find("endswith(\" --help\")")
    try_dispatch_idx = src.find("try_dispatch(line)")
    assert help_idx > 0
    assert try_dispatch_idx > 0
    assert help_idx < try_dispatch_idx, (
        "--help intercept must come before try_dispatch in the "
        "dispatch chain"
    )


# --- Typo suggestion wiring ---------------------------------------------


def test_typo_suggestion_uses_suggest_for_typo():
    src = _source()
    assert "suggest_for_typo" in src


def test_typo_suggestion_only_fires_for_slash():
    """Bytes-pin: typo suggestion is gated by line.startswith('/').
    Non-slash lines (free-text prompts) must NOT trigger
    verb-suggestion noise."""
    src = _source()
    # Find the typo block and verify it's inside a startswith('/') guard
    idx = src.find("suggest_for_typo")
    pre = src[max(0, idx - 800):idx]
    assert "startswith(\"/\")" in pre


def test_typo_suggestion_does_not_fire_for_known_verbs():
    """Bytes-pin: the typo block checks `_typo_reg.find(...)
    is None` before suggesting — known verbs that fell through
    for OTHER reasons (e.g., dispatch error) must not get a
    misleading "did you mean"."""
    src = _source()
    idx = src.find("suggest_for_typo")
    # The find()-is-None check lives in the same wiring block —
    # use a wider window (the block spans ~30 lines of comments
    # + code).
    block = src[max(0, idx - 1200):idx + 1200]
    assert "find(" in block
    assert "is None" in block


def test_typo_suggestion_continues_on_match():
    """When suggestions are surfaced, the dispatch must `continue`
    — otherwise the external handler also fires (double-handling).
    Anchor on the call to `_typo_suggest(` which is unique to the
    typo wiring block."""
    src = _source()
    idx = src.find("_typo_suggest(")
    assert idx > 0
    post = src[idx:idx + 3000]
    assert "continue" in post


def test_typo_suggestion_surfaces_descriptor_hint():
    """§41.3 #19 — bytes-pin: the typo path renders the
    descriptor's actual usage + example via format_verb_hint
    instead of a generic '--help' instruction. NO new registry
    in the wiring — the data lives on VerbDescriptor."""
    src = _source()
    # The wiring lazy-imports format_verb_hint (as `_typo_hint`)
    # and renders its lines.
    assert "format_verb_hint" in src
    # The legacy generic instruction is gone.
    assert "append `--help`" not in src


def test_typo_suggestion_renders_top_candidate_hint():
    """Bytes-pin: the wiring looks up the *top* suggestion in
    the registry (via .find on `_candidates[0]`) and renders
    its hint — not a hardcoded verb name."""
    src = _source()
    idx = src.find("_typo_suggest(")
    body = src[idx:idx + 3000]
    assert "_candidates[0]" in body
    assert "_typo_hint" in body


# --- /tutorial dispatch wiring ------------------------------------------


def test_tutorial_dispatch_branch_present():
    """Bytes-pin: the dispatch chain has an explicit /tutorial
    handler branch (since _handle_tutorial isn't auto-routed by
    repl_dispatch_registry)."""
    src = _source()
    assert "/tutorial" in src
    # The dispatch must call self._handle_tutorial(line)
    assert "self._handle_tutorial(line)" in src


def test_tutorial_dispatch_accepts_bare_or_argv():
    """Bytes-pin: dispatch accepts both `/tutorial` and
    `/tutorial <category>` forms (and unprefixed `tutorial`)."""
    src = _source()
    # Look for the dispatch block
    idx = src.find("self._handle_tutorial")
    pre = src[max(0, idx - 400):idx]
    assert "/tutorial" in pre
    # Accept either the bare-form OR the argv-form check
    assert (
        "\"/tutorial\"" in pre or "'/tutorial'" in pre
    )
    assert "/tutorial " in pre  # argv form


# --- Smoke: instantiating SerpentREPL with the wiring -------------------


def test_serpent_repl_instantiable():
    """Smoke: SerpentREPL can still be constructed after the
    wiring. The dispatch chain edits + new method must not break
    class construction."""
    # Build a minimal flow stub to avoid heavy dependencies
    class _FakeConsole:
        def print(self, *a, **kw):
            pass

    class _FakeFlow:
        def __init__(self):
            self.console = _FakeConsole()

    repl = serpent_flow.SerpentREPL(
        flow=_FakeFlow(),
        on_command=None,
    )
    assert repl is not None
    assert hasattr(repl, "_handle_tutorial")
    assert hasattr(repl, "_flow")


def test_serpent_repl_tutorial_composes_substrate():
    """Smoke: SerpentREPL._handle_tutorial wires the substrate
    correctly. Invoke directly with a fake flow + verify
    rendering succeeds (NEVER raises)."""
    rendered_lines = []

    class _Console:
        def print(self, *args, **kwargs):
            if args:
                rendered_lines.append(str(args[0]))
            else:
                rendered_lines.append("")

    class _FakeFlow:
        def __init__(self):
            self.console = _Console()

    repl = serpent_flow.SerpentREPL(flow=_FakeFlow())
    # Call directly — should not raise
    repl._handle_tutorial("")
    # Some output rendered
    assert len(rendered_lines) > 0
    # Tutorial header surfaces
    text = "\n".join(rendered_lines)
    assert "Operator Tutorial" in text


def test_serpent_repl_tutorial_category_filter_smoke():
    """Smoke: category filter parses from the line."""
    rendered = []

    class _Console:
        def print(self, *args, **kwargs):
            if args:
                rendered.append(str(args[0]))

    class _FakeFlow:
        def __init__(self):
            self.console = _Console()

    repl = serpent_flow.SerpentREPL(flow=_FakeFlow())
    repl._handle_tutorial("/tutorial lifecycle")
    text = "\n".join(rendered)
    # Either the category renders OR the "no verbs in category"
    # message — both are acceptable outcomes depending on what
    # categories the live SerpentREPL exposes
    assert "LIFECYCLE" in text or "no verbs in category" in text


# --- Substrate-imports verification (defensive check) -------------------


def test_repl_completion_exports_helpers():
    """Sanity: the wiring imports these symbols lazily — make
    sure they're exported and reachable."""
    assert hasattr(rc, "discover_verbs")
    assert hasattr(rc, "format_verb_help")
    assert hasattr(rc, "suggest_for_typo")
    assert hasattr(rc, "fuzzy_match")


def test_welcome_state_exports_helpers():
    assert hasattr(ws, "evaluate")
    assert hasattr(ws, "mark_seen")
    assert hasattr(ws, "render_first_launch_banner")
    assert hasattr(ws, "render_tutorial")


# --- §41.3 Slice 3 #12 — inline `?` tooltip keybinding wiring -------------


def test_question_mark_keybinding_present():
    """Bytes-pin: SerpentREPL._loop registers a `?` keybinding."""
    src = _source()
    assert "@_repl_bindings.add(\"?\")" in src


def test_question_mark_binding_invokes_resolve():
    """Bytes-pin: the binding composes resolve_help_for_buffer
    from the registry — no parallel help logic in the wiring."""
    src = _source()
    idx = src.find("@_repl_bindings.add(\"?\")")
    assert idx > 0
    body = src[idx:idx + 3000]
    assert "resolve_help_for_buffer" in body
    assert "discover_verbs" in body


def test_question_mark_binding_falls_back_to_literal_insert():
    """Bytes-pin: when resolve returns None OR composer raises,
    the binding must fall back to `buf.insert_text('?')` rather
    than swallowing the keystroke."""
    src = _source()
    idx = src.find("@_repl_bindings.add(\"?\")")
    body = src[idx:idx + 3000]
    assert "buf.insert_text(\"?\")" in body


def test_question_mark_binding_uses_run_in_terminal():
    """Bytes-pin: rendering goes through prompt_toolkit's
    `run_in_terminal` so the help block lands above the prompt
    without clobbering the input buffer."""
    src = _source()
    idx = src.find("@_repl_bindings.add(\"?\")")
    body = src[idx:idx + 3000]
    assert "run_in_terminal" in body


def test_question_mark_binding_defensive_try_except():
    """Bytes-pin: the substrate composition is wrapped in
    try/except so a buggy resolve_help_for_buffer NEVER propagates
    into the prompt_toolkit event loop."""
    src = _source()
    idx = src.find("@_repl_bindings.add(\"?\")")
    body = src[idx:idx + 3000]
    # Two try blocks expected — the resolve call AND the
    # run_in_terminal invocation
    assert body.count("try:") >= 2
    assert body.count("except") >= 2


def test_repl_completion_inline_help_helpers_reachable():
    """Sanity: the symbols the binding lazy-imports are
    actually exported by repl_completion."""
    assert hasattr(rc, "resolve_help_for_buffer")
    assert hasattr(rc, "is_inline_help_enabled")
    assert hasattr(rc, "INLINE_HELP_ENABLED_ENV_VAR")


# --- §41.3 Slice 3 #14 — op_id arg-completion provider wiring -------------


def test_op_id_provider_registered_in_loop():
    """Bytes-pin: SerpentREPL._loop registers a `op_id` arg
    provider via register_arg_provider. Snapshot of GLS
    `_active_ops` powers `/cancel ` tab completion."""
    src = _source()
    assert "register_arg_provider" in src
    assert '"op_id"' in src


def test_op_id_provider_reads_active_ops():
    """Bytes-pin: provider composes `self._gls._active_ops`
    rather than building a parallel state surface."""
    src = _source()
    idx = src.find("_op_id_provider")
    assert idx > 0
    body = src[idx:idx + 2000]
    assert "_active_ops" in body


def test_op_id_provider_defensive():
    """Bytes-pin: provider is wrapped in try/except so a buggy
    GLS surface NEVER propagates into prompt_toolkit."""
    src = _source()
    idx = src.find("_op_id_provider")
    body = src[idx:idx + 2000]
    assert "try:" in body
    assert "except" in body


def test_op_id_provider_handles_none_gls():
    """Bytes-pin: when `self._gls` is None (headless / pre-boot),
    provider returns () rather than raising."""
    src = _source()
    idx = src.find("_op_id_provider")
    body = src[idx:idx + 2000]
    assert "is None" in body


def test_arg_completion_symbols_reachable():
    """Sanity: arg-completion substrate symbols exist."""
    assert hasattr(rc, "ArgKind")
    assert hasattr(rc, "ArgPositionSpec")
    assert hasattr(rc, "register_arg_provider")
    assert hasattr(rc, "parse_arg_spec")
    assert hasattr(rc, "get_arg_candidates")
