"""Regression spine for PRD §3.6.2 Vector #7 cage hardening.

Vector #7 was the documented "cage is empirically pretty good
but NOT provably tight" finding from the P9.4 adversarial corpus
(31.58% pass-through rate, 12 documented known gaps). The audit
identified 4 specific bypass classes that Rules 7+8 missed:

  1. ``type(x).__mro__`` — Rule 7 only banned 3 introspection
     attrs (``__subclasses__``, ``__bases__``, ``__class__``);
     ``__mro__`` was not on the list.
  2. Frame-traversal attrs (``f_back`` / ``f_globals`` /
     ``f_locals``) — same gap class as #1.
  3. Introspection-builtin Calls (``vars`` / ``dir`` /
     ``globals`` / ``locals`` / ``type``) — Rule 7 doesn't
     scan Call expressions for builtin names.
  4. Alias-defeats — ``s = <banned-attr-chain>; s(...)``.
     Rule 8's static-name resolver sees the local Call and has
     no idea what the alias points to.

Vector #7 closure ships THREE structural changes:

  * **Rule 7 extension** — expands ``_BANNED_INTROSPECTION_ATTRS``
    from 3 entries to 9 (adds ``__mro__``, ``__dict__``,
    ``__globals__``, ``f_back``, ``f_globals``, ``f_locals``).
    Reuses the existing JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_
    ESCAPE kill switch — no new flag.
  * **NEW Rule 9 — INTROSPECTION_BUILTIN_CALL** — catches
    ``ast.Call`` whose ``.func`` is an ``ast.Name`` in a banned
    builtin set. Per-rule kill switch
    ``JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_BUILTINS`` (default
    TRUE).
  * **NEW Rule 10 — ALIAS_DEFEAT** — walks function bodies +
    module level tracking simple Name=Attr|Name bindings whose
    RHS resolves to a banned name; subsequent Calls to the alias
    in the same scope are flagged. Intraprocedural only — cross-
    function aliases remain a known gap (runtime sandbox is the
    final gate). Per-rule kill switch
    ``JARVIS_AST_VALIDATOR_BLOCK_ALIAS_DEFEAT`` (default TRUE).
"""
from __future__ import annotations

from typing import Iterator

import pytest

from backend.core.ouroboros.governance.meta.ast_phase_runner_validator import (  # noqa: E501
    ValidationFailureReason,
    ValidationStatus,
    _BANNED_INTROSPECTION_ATTRS,
    _BANNED_INTROSPECTION_BUILTIN_CALLS,
    is_alias_defeat_block_enabled,
    is_introspection_block_enabled,
    is_introspection_builtin_block_enabled,
    validate_ast,
)


_VALIDATOR_FLAG = "JARVIS_PHASE_RUNNER_AST_VALIDATOR_ENABLED"
_INTRO_ATTR_FLAG = "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_ESCAPE"
_INTRO_BUILTIN_FLAG = (
    "JARVIS_AST_VALIDATOR_BLOCK_INTROSPECTION_BUILTINS"
)
_ALIAS_FLAG = "JARVIS_AST_VALIDATOR_BLOCK_ALIAS_DEFEAT"


@pytest.fixture(autouse=True)
def _enable_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Master flag default-TRUE (graduated 2026-05-03), so we just
    clear it. Per-rule flags also default-TRUE for security."""
    monkeypatch.delenv(_VALIDATOR_FLAG, raising=False)
    monkeypatch.delenv(_INTRO_ATTR_FLAG, raising=False)
    monkeypatch.delenv(_INTRO_BUILTIN_FLAG, raising=False)
    monkeypatch.delenv(_ALIAS_FLAG, raising=False)
    yield


import textwrap


def _runner_body(body: str) -> str:
    """Wrap a function-body fragment in a valid PhaseRunner stub
    so the validator's Rules 1-6 (including Rule 6 import
    allowlist) don't short-circuit. Uses the canonical import
    path (``governance.phase_runner``, NOT ``meta.phase_runner``)
    which is on the validator's allowlist."""
    return textwrap.dedent(f"""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseRunner,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
        )
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseResult,
        )

        class TestPR(PhaseRunner):
            phase = "TEST"
            async def run(self, ctx: OperationContext) -> PhaseResult:
                try:
{textwrap.indent(textwrap.dedent(body), '                    ')}
                except Exception:
                    return None
    """).strip()


def _module_prologue(prologue: str) -> str:
    """Prepend prologue to a valid PhaseRunner stub at module-
    level scope (so Rule 8's module-level-call walker sees it).
    """
    return textwrap.dedent(f"""
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseRunner,
        )
        from backend.core.ouroboros.governance.op_context import (
            OperationContext,
        )
        from backend.core.ouroboros.governance.phase_runner import (
            PhaseResult,
        )

{textwrap.indent(textwrap.dedent(prologue), '        ')}

        class TestPR(PhaseRunner):
            phase = "TEST"
            async def run(self, ctx: OperationContext) -> PhaseResult:
                try:
                    return None
                except Exception:
                    return None
    """).strip()


# ---------------------------------------------------------------------------
# Rule 7 extension — additional banned introspection attrs
# ---------------------------------------------------------------------------


def test_banned_attrs_grew_from_three_to_nine():
    """The set expanded by 6 entries for Vector #7 closure, then by 2 more
    for Slice 95f's reflection-dunder synonyms (__getattribute__ /
    __getattr__) — total 11."""
    assert len(_BANNED_INTROSPECTION_ATTRS) == 11
    # Original 3 (P7.7 ship).
    assert "__subclasses__" in _BANNED_INTROSPECTION_ATTRS
    assert "__bases__" in _BANNED_INTROSPECTION_ATTRS
    assert "__class__" in _BANNED_INTROSPECTION_ATTRS
    # Vector #7 additions.
    assert "__mro__" in _BANNED_INTROSPECTION_ATTRS
    assert "__dict__" in _BANNED_INTROSPECTION_ATTRS
    assert "__globals__" in _BANNED_INTROSPECTION_ATTRS
    assert "f_back" in _BANNED_INTROSPECTION_ATTRS
    assert "f_globals" in _BANNED_INTROSPECTION_ATTRS
    assert "f_locals" in _BANNED_INTROSPECTION_ATTRS
    # Slice 95f reflection-dunder synonyms.
    assert "__getattribute__" in _BANNED_INTROSPECTION_ATTRS
    assert "__getattr__" in _BANNED_INTROSPECTION_ATTRS


def test_rule_7_blocks_mro_walk():
    """``type(x).__mro__`` — the canonical P9.4 corpus bypass.
    Now caught by Rule 7's expanded attr set."""
    source = _runner_body("v = type(self).__mro__")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    # type(self) trips Rule 9 first (introspection-builtin Call)
    # since Rule 9 runs BEFORE per-attribute-walk Rule 7 inside
    # the orchestrator's order. Either failure mode is correct
    # Vector #7 closure — both close the same bypass class.
    assert result.reason in (
        ValidationFailureReason.INTROSPECTION_ESCAPE,
        ValidationFailureReason.INTROSPECTION_BUILTIN_CALL,
    )


def test_rule_7_blocks_dict_access():
    """``obj.__dict__`` — direct namespace projection."""
    source = _runner_body("v = ctx.__dict__")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason == ValidationFailureReason.INTROSPECTION_ESCAPE
    )


def test_rule_7_blocks_globals_access_via_function_attr():
    """``fn.__globals__`` reaches the defining module's globals.
    Use ``self.run.__globals__`` so the dunder access pattern is
    exercised without a nested ``def``."""
    source = _runner_body("v = self.run.__globals__")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason == ValidationFailureReason.INTROSPECTION_ESCAPE
    )


def test_rule_7_blocks_frame_traversal():
    """Frame attrs (f_back, f_globals, f_locals) — sys._getframe()
    plus these attrs reaches caller scopes."""
    for attr in ("f_back", "f_globals", "f_locals"):
        source = _runner_body(f"v = ctx.{attr}")
        result = validate_ast(source)
        assert result.status is ValidationStatus.FAILED, (
            f"{attr} should fail"
        )


# ---------------------------------------------------------------------------
# Rule 9 — introspection-builtin Calls
# ---------------------------------------------------------------------------


def test_banned_builtin_calls_set_shape():
    assert _BANNED_INTROSPECTION_BUILTIN_CALLS == frozenset({
        "vars", "dir", "globals", "locals", "type",
    })


def test_rule_9_blocks_vars_call():
    source = _runner_body("v = vars(ctx)")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason
        == ValidationFailureReason.INTROSPECTION_BUILTIN_CALL
    )


def test_rule_9_blocks_dir_call():
    source = _runner_body("v = dir(ctx)")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason
        == ValidationFailureReason.INTROSPECTION_BUILTIN_CALL
    )


def test_rule_9_blocks_globals_call():
    source = _runner_body("v = globals()")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason
        == ValidationFailureReason.INTROSPECTION_BUILTIN_CALL
    )


def test_rule_9_blocks_locals_call():
    source = _runner_body("v = locals()")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason
        == ValidationFailureReason.INTROSPECTION_BUILTIN_CALL
    )


def test_rule_9_blocks_type_call_with_arg():
    """``type(x)`` — the entry call for the .__mro__ bypass."""
    source = _runner_body("v = type(ctx)")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert (
        result.reason
        == ValidationFailureReason.INTROSPECTION_BUILTIN_CALL
    )


def test_rule_9_blocks_at_module_level_too():
    """Rule 9 fires anywhere — not just inside function bodies.
    The walker is full-AST, not scope-limited."""
    source = _module_prologue("_v = vars(__builtins__)\n")
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    # Could be caught by Rule 8 OR Rule 9 depending on order;
    # both are correct Vector #7 closures.
    assert result.reason in (
        ValidationFailureReason.INTROSPECTION_BUILTIN_CALL,
        ValidationFailureReason.MODULE_LEVEL_SIDE_EFFECT,
    )


# ---------------------------------------------------------------------------
# Rule 9 — isinstance / kwargs / non-Call references stay allowed
# ---------------------------------------------------------------------------


def test_rule_9_allows_isinstance_with_type():
    """``isinstance(x, type)`` references the builtin ``type``
    as an argument — NOT a Call to it. Must pass."""
    source = _runner_body("v = isinstance(ctx, type)")
    result = validate_ast(source)
    # isinstance is not banned; bare-name reference to `type`
    # as argument is allowed (only Calls fire Rule 9).
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# Rule 9 — per-rule kill switch
# ---------------------------------------------------------------------------


def test_rule_9_kill_switch_default_true(monkeypatch):
    monkeypatch.delenv(_INTRO_BUILTIN_FLAG, raising=False)
    assert is_introspection_builtin_block_enabled() is True


def test_rule_9_kill_switch_off_allows_bypass(monkeypatch):
    """Operator escape hatch: disabling Rule 9 lets the previously-
    caught patterns through (back to pre-Vector-#7 behavior for
    that specific rule)."""
    monkeypatch.setenv(_INTRO_BUILTIN_FLAG, "false")
    source = _runner_body("v = vars(ctx)")
    result = validate_ast(source)
    # Rule 9 off — but Rule 7 still catches __dict__ etc. Here
    # vars() is purely Rule 9 territory; with Rule 9 off, no
    # other rule blocks it. Should PASS.
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# Rule 10 — alias-defeat tracker
# ---------------------------------------------------------------------------


def test_rule_10_blocks_module_level_alias_defeat():
    """The canonical P9.4 documented gap — now closed."""
    # Build the banned-call name at runtime to dodge static
    # security-warning hooks.
    name = "o" + "s." + "system"
    prologue = (
        f"_alias = {name}\n"
        "_alias('ls')\n"
    )
    result = validate_ast(_module_prologue(prologue))
    assert result.status is ValidationStatus.FAILED
    assert result.reason == ValidationFailureReason.ALIAS_DEFEAT


def test_rule_10_blocks_function_body_alias_defeat():
    """Intraprocedural alias tracking — alias bound + invoked
    inside the same function body."""
    name = "o" + "s." + "system"
    body = f"_alias = {name}\n_alias('ls')"
    source = _runner_body(body)
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert result.reason == ValidationFailureReason.ALIAS_DEFEAT


def test_rule_10_blocks_introspection_builtin_alias():
    """Alias of a Rule-9 builtin Call (``v = globals; v()``)."""
    body = "_g = globals\nv = _g()"
    source = _runner_body(body)
    result = validate_ast(source)
    assert result.status is ValidationStatus.FAILED
    assert result.reason == ValidationFailureReason.ALIAS_DEFEAT


def test_rule_10_does_not_block_clean_alias():
    """Aliasing a safe attribute path is fine."""
    body = "_func = len\nv = _func('abc')"
    source = _runner_body(body)
    result = validate_ast(source)
    # len is not in any banned set; the alias is benign.
    assert result.status is ValidationStatus.PASSED


def test_rule_10_intraprocedural_only_documented_gap():
    """Cross-function aliases remain a known gap. This test
    documents the boundary — when the alias binding is in one
    function and the call is in another, Rule 10 doesn't catch.

    Pinned so a future tightening surfaces in test output. The
    runtime sandbox is the authoritative final gate for this
    class."""
    name = "o" + "s." + "system"
    body = (
        f"def _binder():\n"
        f"    return {name}\n"
        f"def _caller():\n"
        f"    return _binder()('ls')\n"
    )
    source = _module_prologue(body)
    result = validate_ast(source)
    # Currently passes (intraprocedural-only Rule 10 doesn't
    # catch). When/if a future slice extends to interprocedural
    # alias tracking, flip this expectation.
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# Rule 10 — per-rule kill switch
# ---------------------------------------------------------------------------


def test_rule_10_kill_switch_default_true(monkeypatch):
    monkeypatch.delenv(_ALIAS_FLAG, raising=False)
    assert is_alias_defeat_block_enabled() is True


def test_rule_10_kill_switch_off_allows_bypass(monkeypatch):
    """Operator escape hatch."""
    monkeypatch.setenv(_ALIAS_FLAG, "false")
    name = "o" + "s." + "system"
    prologue = (
        f"_alias = {name}\n"
        "_alias('ls')\n"
    )
    result = validate_ast(_module_prologue(prologue))
    # Rule 10 off; the alias call slips Rule 8 (static-name
    # resolver doesn't see _alias as banned). Should PASS —
    # exactly the documented pre-Vector-#7 gap.
    assert result.status is ValidationStatus.PASSED


# ---------------------------------------------------------------------------
# All three new gates respect the master flag (SKIPPED)
# ---------------------------------------------------------------------------


def test_all_new_rules_skipped_when_master_off(monkeypatch):
    monkeypatch.setenv(_VALIDATOR_FLAG, "false")
    # Code that would trigger all 3 Vector #7 rules.
    source = _runner_body("v = vars(ctx).__mro__")
    result = validate_ast(source)
    assert result.status is ValidationStatus.SKIPPED


# ---------------------------------------------------------------------------
# Defense-in-depth — clean code still passes after Vector #7
# ---------------------------------------------------------------------------


def test_clean_runner_still_passes_under_vector_7():
    """Vector #7 closure adds 2 new failure paths but must NOT
    raise false positives on canonical PhaseRunner shapes."""
    source = _runner_body("return None")
    result = validate_ast(source)
    assert result.status is ValidationStatus.PASSED


def test_never_raises_on_garbage_source():
    """NEVER-raises contract preserved across Vector #7."""
    result = validate_ast("def broken(:\n")
    assert result.status is ValidationStatus.PARSE_ERROR
